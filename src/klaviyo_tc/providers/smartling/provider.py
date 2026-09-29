"""Smartling implementation of the core `Provider` protocol.

Smartling-only concepts (jobs, batches, the file directives envelope) live
here; the sync engine never sees them.
"""
from __future__ import annotations

import json

from ..base import FileSpec, SubmitResult
from .client import SmartlingClient

DEFAULT_FILES_PER_BATCH = 100


def _noop_checkpoint(_state: dict) -> None:
    pass


class SmartlingProvider:
    name = "smartling"

    def __init__(
        self,
        *,
        base_url: str,
        project_id: str,
        user_identifier: str,
        user_secret: str,
        string_format_paths: str,
        placeholder_format_custom: list[str],
        files_per_batch: int = DEFAULT_FILES_PER_BATCH,
        authorize: bool = True,
        workflow_uid: str | None = None,
        version: str = "0.0.0",
    ):
        self.string_format_paths = string_format_paths
        self.placeholder_format_custom = placeholder_format_custom
        self.files_per_batch = files_per_batch
        self.authorize = authorize
        self.workflow_uid = workflow_uid or None
        self._client = SmartlingClient(base_url, user_identifier, user_secret, project_id, version=version)

    def _build_file_content(self, strings: dict[str, str]) -> dict:
        # "smartling" must be the first key; dict insertion order, not sorted.
        content = {
            "smartling": {
                "string_format_paths": self.string_format_paths,
                "placeholder_format_custom": self.placeholder_format_custom,
            }
        }
        content.update(strings)
        return content

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
        # Job names are project-unique in Smartling: fold in the reference (generation id).
        job_name = f"klaviyo {translation_id} {reference[:8]}"
        file_spec = FileSpec(translation_id, file_name, strings, target_locales)
        result = self.submit_many(
            run_ref=reference, job_name=job_name, files=[file_spec], state=state, checkpoint=checkpoint
        )
        # completed_locales/fetch key off "file_uri"; submit_many tracks batches instead.
        result.state["file_uri"] = file_name
        return result

    def _file_state(self, file_name: str) -> dict:
        return {"file_uri": file_name}

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
        files_by_name = {f.file_name: f for f in files}

        if not state.get("job_uid"):
            all_locales = sorted({locale for f in files for locale in f.target_locales})
            state["job_uid"] = self._client.find_job_by_name(job_name) or self._client.create_job(
                job_name=job_name,
                target_locale_ids=all_locales,
                reference_number=run_ref,
            )
            checkpoint(dict(state))

        batches: list[dict] = state.setdefault("batches", [])
        batched_names = {name for batch in batches for name in batch["file_names"]}
        unbatched = [f for f in files if f.file_name not in batched_names]

        for start in range(0, len(unbatched), self.files_per_batch):
            group = unbatched[start : start + self.files_per_batch]
            batch_uid = self._client.create_batch(
                state["job_uid"],
                [f.file_name for f in group],
                authorize=self.authorize,
                locale_ids=sorted({locale for f in group for locale in f.target_locales}),
                workflow_uid=self.workflow_uid,
            )
            batches.append({"batch_uid": batch_uid, "file_names": [f.file_name for f in group], "uploaded": []})
            checkpoint(dict(state))

        # Iterate every batch (not just new ones) so a crash mid-upload resumes cleanly.
        for batch in batches:
            uploaded = set(batch["uploaded"])
            for name in batch["file_names"]:
                if name in uploaded:
                    continue
                file_spec = files_by_name[name]
                content = self._build_file_content(file_spec.strings)
                # No sort_keys: preserves the "smartling" directives as the first key.
                self._client.upload_file(
                    batch["batch_uid"], name, json.dumps(content).encode("utf-8"), file_spec.target_locales,
                    authorize=self.authorize,
                )
                batch["uploaded"].append(name)
                checkpoint(dict(state))

        for batch in batches:
            self._client.poll_batch(batch["batch_uid"])

        file_states = {f.file_name: self._file_state(f.file_name) for f in files}
        return SubmitResult(state=state, submitted=True, file_states=file_states)

    def completed_locales(self, state: dict) -> list[str]:
        status = self._client.get_file_status(state["file_uri"])
        return [item["localeId"] for item in status.get("items", []) if item.get("completedStringCount")]

    def fetch(self, state: dict, locale: str) -> dict[str, str]:
        raw = self._client.download_file(locale, state["file_uri"])
        data = json.loads(raw)
        data.pop("smartling", None)
        return {key: value for key, value in data.items() if value}
