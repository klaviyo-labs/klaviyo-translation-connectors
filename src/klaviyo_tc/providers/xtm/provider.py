"""XTM Cloud implementation of the core `Provider` protocol.

One push run becomes one XTM project. Each Klaviyo translation is one JSON source file in it,
and XTM creates one job per (file, target language). XTM-only concepts (customers, templates,
workflows, analysis, generated target files) live here; the sync engine never sees them.

Run state (opaque to the engine):
    {"project_id": int, "uploads": {xtm_file_name: content_digest},
     "analysed": bool, "workflow_started": bool}
Per-file state handed to `completed_locales`/`fetch`:
    {"project_id": int, "xtm_file_name": str, "jobs": {xtm_language: job_id}}
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from ..base import FileSpec, SubmitResult
from .client import XtmClient, XtmError, extract_json_from_zip

DEFAULT_FILES_PER_UPLOAD = 50
DEFAULT_POLL_TIMEOUT = 300.0
# A pull asks for status once per translation; they usually share one project, so reuse it briefly.
STATUS_CACHE_SECONDS = 30.0
# Characters the API rejects in project names (RSProjectCreationModelAttributeRequest.name).
_FORBIDDEN_NAME_CHARS = re.compile(r'[\\,/:*?"<>|{}\t]')


def _noop_checkpoint(_state: dict) -> None:
    pass


def project_name_for(job_name: str) -> str:
    """Deterministic, API-safe XTM project name for an engine job name."""
    cleaned = _FORBIDDEN_NAME_CHARS.sub("-", job_name)
    return re.sub(r"\s+", " ", cleaned).strip()


def xtm_file_name_for(file_name: str) -> str:
    """Engine file names look like `klaviyo/<id>.json`; XTM gets a flat, slash-free name."""
    return file_name.replace("/", "__")


class XtmProvider:
    name = "xtm"

    def __init__(
        self,
        *,
        base_url: str,
        client_name: str,
        user_id: int,
        password: str,
        customer_id: int,
        source_language: str | None = None,
        template_id: int | None = None,
        workflow_id: int | None = None,
        workflow_name: str | None = None,
        filter_template_id: str | None = None,
        start_workflow: bool = False,
        files_per_upload: int = DEFAULT_FILES_PER_UPLOAD,
        poll_timeout: float = DEFAULT_POLL_TIMEOUT,
        version: str = "0.0.0",
    ):
        if not isinstance(files_per_upload, int) or isinstance(files_per_upload, bool) or files_per_upload < 1:
            raise ValueError(f"files_per_upload must be a positive integer, got {files_per_upload!r}")
        if template_id is None and not source_language:
            raise ValueError("source_language is required when template_id is not set")
        if template_id is None and workflow_id is None and not workflow_name:
            raise ValueError("one of template_id, workflow_id or workflow_name is required")
        self.customer_id = customer_id
        self.source_language = source_language
        self.template_id = template_id
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.filter_template_id = filter_template_id
        # Named `authorize` for parity with Smartling so `push --no-authorize` works for both:
        # True starts the XTM workflow once files are analysed.
        self.authorize = start_workflow
        self.files_per_upload = files_per_upload
        self._client = XtmClient(
            base_url, client_name, user_id, password, version=version, poll_timeout=poll_timeout
        )
        self._status_cache: dict[int, tuple[float, dict]] = {}
        self._status_lock = threading.Lock()

    # -- Push ---------------------------------------------------------------------------

    def submit(
        self,
        *,
        translation_id: str,
        file_name: str,
        strings: dict[str, str],
        target_locales: list[str],
        reference: str,
        state: dict | None = None,
        checkpoint=None,
    ) -> SubmitResult:
        # Fold in the reference (generation id) so a re-push gets its own project.
        job_name = f"klaviyo {translation_id} {reference[:8]}"
        file_spec = FileSpec(translation_id, file_name, strings, target_locales)
        result = self.submit_many(
            run_ref=reference, job_name=job_name, files=[file_spec], state=state, checkpoint=checkpoint
        )
        # completed_locales/fetch read the per-file keys; keep them alongside the run bookkeeping.
        result.state.update(result.file_states[file_name])
        return result

    def _project_fields(self, name: str, run_ref: str, files: list[FileSpec]) -> dict:
        fields: dict = {"name": name, "customerId": str(self.customer_id), "referenceId": run_ref}
        if self.template_id is not None:
            fields["templateId"] = str(self.template_id)
        if self.source_language:
            fields["sourceLanguage"] = self.source_language
        fields["targetLanguages"] = sorted({locale for f in files for locale in f.target_locales})
        if self.workflow_id is not None:
            fields["workflowId"] = str(self.workflow_id)
        elif self.workflow_name:
            fields["workflowName"] = self.workflow_name
        if self.filter_template_id:
            fields["filterTemplateId"] = str(self.filter_template_id)
        return fields

    def _upload_entry(self, spec: FileSpec, payload: bytes, all_languages: list[str]):
        languages = sorted(spec.target_locales)
        # Only narrow a file's languages when it needs fewer than the project has.
        return (xtm_file_name_for(spec.file_name), payload, languages if languages != all_languages else None)

    def submit_many(
        self,
        *,
        run_ref: str,
        job_name: str,
        files: list[FileSpec],
        state: dict | None = None,
        checkpoint=None,
    ) -> SubmitResult:
        state = dict(state or {})
        checkpoint = checkpoint or _noop_checkpoint
        uploads: dict[str, str] = dict(state.get("uploads") or {})
        state["uploads"] = uploads
        all_languages = sorted({locale for f in files for locale in f.target_locales})

        payloads = {f.file_name: json.dumps(f.strings, ensure_ascii=False).encode("utf-8") for f in files}
        digests = {name: hashlib.blake2b(p, digest_size=16).hexdigest() for name, p in payloads.items()}
        pending = [f for f in files if uploads.get(xtm_file_name_for(f.file_name)) != digests[f.file_name]]
        project_name = project_name_for(job_name)

        if not state.get("project_id"):
            # A crash between "create" and the checkpoint below leaves a project we never recorded;
            # adopt it by its deterministic name. Its uploads are unknown, so re-send them all
            # (uploads replace same-named files rather than duplicating them).
            adopted = self._client.find_projects_by_name(project_name, self.customer_id)
            if adopted:
                state["project_id"] = max(adopted)
                checkpoint(dict(state))
            else:
                first, pending = pending[: self.files_per_upload], pending[self.files_per_upload :]
                created = self._client.create_project(
                    self._project_fields(project_name, run_ref, files),
                    [self._upload_entry(f, payloads[f.file_name], all_languages) for f in first],
                )
                state["project_id"] = created["projectId"]
                for f in first:
                    uploads[xtm_file_name_for(f.file_name)] = digests[f.file_name]
                state["analysed"] = False
                checkpoint(dict(state))

        project_id = state["project_id"]
        for start in range(0, len(pending), self.files_per_upload):
            group = pending[start : start + self.files_per_upload]
            self._client.upload_sources(
                project_id, [self._upload_entry(f, payloads[f.file_name], all_languages) for f in group]
            )
            for f in group:
                uploads[xtm_file_name_for(f.file_name)] = digests[f.file_name]
            state["analysed"] = False
            checkpoint(dict(state))

        xtm_names = {xtm_file_name_for(f.file_name) for f in files}
        if not state.get("analysed"):
            # Analysis is where XTM parses the JSON; a failure here means it couldn't read a file.
            self._client.poll_analysis(project_id, xtm_names)
            state["analysed"] = True
            checkpoint(dict(state))

        if self.authorize and not state.get("workflow_started"):
            started = self._client.start_workflow(project_id)
            if started and started.get("status") is False:
                raise XtmError(None, f"could not start workflow for project {project_id}: {started.get('errorType')}")
            state["workflow_started"] = True
            checkpoint(dict(state))

        jobs_by_file = self._jobs_by_file(self._client.get_status(project_id))
        file_states = {}
        for f in files:
            xtm_name = xtm_file_name_for(f.file_name)
            jobs = {lang: job for lang, job in jobs_by_file.get(xtm_name, {}).items() if lang in f.target_locales}
            missing = sorted(set(f.target_locales) - set(jobs))
            if missing:
                raise XtmError(None, f"project {project_id} has no job for {xtm_name} in {missing}")
            file_states[f.file_name] = {"project_id": project_id, "xtm_file_name": xtm_name, "jobs": jobs}
        return SubmitResult(state=state, submitted=True, file_states=file_states)

    @staticmethod
    def _jobs_by_file(status: dict) -> dict[str, dict[str, int]]:
        jobs: dict[str, dict[str, int]] = {}
        for job in status.get("jobs") or []:
            if job.get("completionStatus") in ("CANCELLED", "DELETED"):
                continue
            jobs.setdefault(job.get("fileName"), {})[job.get("targetLanguage")] = job.get("jobId")
        return jobs

    # -- Pull ---------------------------------------------------------------------------

    def _status(self, project_id: int) -> dict:
        now = time.monotonic()
        with self._status_lock:
            cached = self._status_cache.get(project_id)
            if cached and now - cached[0] < STATUS_CACHE_SECONDS:
                return cached[1]
        status = self._client.get_status(project_id)
        with self._status_lock:
            self._status_cache[project_id] = (now, status)
        return status

    def completed_locales(self, state: dict) -> list[str]:
        """Languages whose job for this file has finished every workflow step."""
        jobs = state.get("jobs") or {}
        by_id = {job.get("jobId"): job for job in self._status(state["project_id"]).get("jobs") or []}
        return [
            lang for lang, job_id in jobs.items() if (by_id.get(job_id) or {}).get("completionStatus") == "FINISHED"
        ]

    def fetch(self, state: dict, locale: str) -> dict[str, str]:
        project_id = state["project_id"]
        file_id = self._client.generate_target(project_id, state["jobs"][locale])
        self._client.poll_generated_file(project_id, file_id)
        archive = self._client.download_file(project_id, file_id)
        data = extract_json_from_zip(archive, state["xtm_file_name"])
        return {key: value for key, value in data.items() if isinstance(value, str) and value}
