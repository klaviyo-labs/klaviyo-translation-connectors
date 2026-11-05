"""XTM Cloud REST API client: XTM-Basic auth, projects, source upload, status, target download.

Written against the XTM REST API reference (OpenAPI spec version 26.4):
  https://api.xtm-cloud.com/project-manager-api-rest/  (spec JSON: .../documentation/xtm-docs)
and the XTM Knowledge Base examples:
  https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/4220158521  (Often-used API methods)
  https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/4221304837  (Running your first REST API method)

Every endpoint lives under the instance's `/project-manager-api-rest` prefix, which is part of
`base_url`. Errors come back as `{"reason": "..."}` (RSErrorResponse) with a 4xx/5xx status.
XTM-Basic tokens are permanent, so there's no expiry to track: re-authenticate once on a 401.
"""
from __future__ import annotations

import io
import json
import random
import threading
import time
import zipfile

import httpx

from ...redact import register_secret

MAX_ATTEMPTS = 6
# The spec documents no rate limits or 429; 503 means "try again later". 429 is retried defensively.
RETRY_STATUSES = {429, 500, 502, 503, 504}
JOB_ANALYSIS_FAILURES = {"ERROR"}
GENERATED_FILE_FAILURES = {"ERROR"}


class XtmError(Exception):
    def __init__(self, status_code: int | None, detail):
        super().__init__(f"XTM API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(0.05 * (2**attempt), 1.0) + random.uniform(0, 0.02)


def _reason(response: httpx.Response):
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict) and payload.get("reason"):
        return payload["reason"]
    return payload


class XtmClient:
    def __init__(
        self,
        base_url: str,
        client_name: str,
        user_id: int,
        password: str,
        version: str = "0.0.0",
        timeout: float = 60.0,
        poll_interval: float = 3.0,
        poll_timeout: float = 120.0,
    ):
        self.client_name = client_name
        self.user_id = user_id
        self.password = password
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"User-Agent": f"klaviyo-translation-connectors/{version}"},
            timeout=timeout,
        )
        self._token: str | None = None
        self._auth_lock = threading.Lock()

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            response = self._client.request(method, url, **kwargs)
            if response.status_code in RETRY_STATUSES:
                if attempt >= MAX_ATTEMPTS:
                    return response
                time.sleep(_retry_delay(response, attempt))
                continue
            return response

    def _authenticate(self) -> None:
        # POST /auth/token (RSTokenGenerateRequest): {client, password, userId:int} -> {token}.
        # XTM encodes whatever it's given here, so bad credentials only fail on the next real call.
        response = self._send(
            "POST",
            "/auth/token",
            json={"client": self.client_name, "userId": self.user_id, "password": self.password},
        )
        if response.status_code >= 400:
            raise XtmError(response.status_code, _reason(response))
        token = response.json().get("token")
        if not token:
            raise XtmError(response.status_code, "auth response had no token")
        self._token = token
        # Issued tokens aren't known secrets ahead of time; register them so they're redacted too.
        register_secret(self._token)

    def _ensure_token(self, stale_token: str | None = None) -> str:
        # Serialized so concurrent workers share one authentication instead of racing.
        with self._auth_lock:
            if not self._token or (stale_token is not None and self._token == stale_token):
                self._authenticate()
            return self._token

    def _authed(self, method: str, url: str, retried: bool = False, **kwargs) -> httpx.Response:
        token = self._ensure_token()
        headers = kwargs.pop("headers", None) or {}
        headers["Authorization"] = f"XTM-Basic {token}"
        response = self._send(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and not retried:
            self._ensure_token(stale_token=token)
            return self._authed(method, url, retried=True, **kwargs)
        return response

    def _call(self, method: str, url: str, **kwargs):
        response = self._authed(method, url, **kwargs)
        if response.status_code >= 400:
            raise XtmError(response.status_code, _reason(response))
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise XtmError(response.status_code, response.text) from None

    # -- Projects -----------------------------------------------------------------------

    def find_projects_by_name(self, name: str, customer_id: int) -> list[int]:
        """IDs of projects whose name is exactly `name` (GET /projects; name matching may be fuzzy)."""
        data = self._call("GET", "/projects", params={"name": name, "customerIds": [customer_id]})
        return [p["id"] for p in data or [] if p.get("name") == name and p.get("id") is not None]

    def get_project(self, project_id: int) -> dict:
        return self._call("GET", f"/projects/{project_id}")

    def create_project(self, fields: dict, files: list[tuple[str, bytes, list[str] | None]]) -> dict:
        """POST /projects (multipart/form-data) -> RSProjectCreationResponse {projectId, jobs[]}.

        `files` is [(xtm_file_name, content, per_file_target_languages_or_None)], sent as
        `translationFiles[i].file` (+ `translationFiles[i].targetLanguages`).
        """
        data = dict(fields)
        multipart = []
        for index, (name, content, languages) in enumerate(files):
            multipart.append((f"translationFiles[{index}].file", (name, content, "application/json")))
            if languages:
                data[f"translationFiles[{index}].targetLanguages"] = ",".join(languages)
        return self._call("POST", "/projects", data=data, files=multipart)

    def upload_sources(self, project_id: int, files: list[tuple[str, bytes, list[str] | None]]) -> dict:
        """POST /projects/{id}/files/sources/upload (multipart) -> RSProjectFileUploadResponse.

        `matchType=MATCH_NAMES` replaces a same-named file instead of adding a renamed copy, so a
        retried upload overwrites rather than duplicates. `reanalyseProject=YES` (default NO) so a
        replaced file is re-parsed before we wait on analysis.
        """
        data = {}
        multipart = []
        for index, (name, content, languages) in enumerate(files):
            multipart.append((f"files[{index}].file", (name, content, "application/json")))
            if languages:
                data[f"files[{index}].targetLanguages"] = ",".join(languages)
        return self._call(
            "POST",
            f"/projects/{project_id}/files/sources/upload",
            params={"matchType": "MATCH_NAMES", "reanalyseProject": "YES"},
            data=data,
            files=multipart,
        )

    def get_analysis(self, project_id: int) -> dict:
        """GET /projects/{id}/analysis?fetchLevel=JOBS -> RSProjectAnalysisResponse."""
        return self._call("GET", f"/projects/{project_id}/analysis", params={"fetchLevel": "JOBS"})

    def poll_analysis(self, project_id: int, file_names: set[str]) -> None:
        """Wait until XTM has analysed (parsed) our files; fail if any of their jobs errored."""
        deadline = time.time() + self.poll_timeout
        while True:
            data = self.get_analysis(project_id)
            ours = [job for job in data.get("jobs") or [] if job.get("fileName") in file_names]
            failed = [job for job in ours if job.get("status") in JOB_ANALYSIS_FAILURES]
            if failed:
                raise XtmError(None, f"project {project_id} analysis failed for jobs: {failed}")
            pending = [job for job in ours if job.get("status") == "IN_PROGRESS"]
            if data.get("status") == "FINISHED" or (ours and not pending):
                return
            if time.time() >= deadline:
                raise XtmError(None, f"project {project_id} analysis did not finish within {self.poll_timeout}s")
            time.sleep(self.poll_interval)

    def start_workflow(self, project_id: int) -> dict:
        """POST /projects/{id}/workflow/start: activate the first workflow step for every job."""
        return self._call("POST", f"/projects/{project_id}/workflow/start")

    def get_status(self, project_id: int) -> dict:
        """GET /projects/{id}/status?fetchLevel=JOBS -> RSProjectStatusResponse."""
        return self._call("GET", f"/projects/{project_id}/status", params={"fetchLevel": "JOBS"})

    # -- Target files -------------------------------------------------------------------

    def generate_target(self, project_id: int, job_id: int) -> int:
        """POST /projects/{id}/files/generate?fileType=TARGET&jobIds=... -> [{fileId, jobId, fileType}]."""
        data = self._call(
            "POST", f"/projects/{project_id}/files/generate", params={"fileType": "TARGET", "jobIds": [job_id]}
        )
        for item in data or []:
            if item.get("jobId") == job_id and item.get("fileId") is not None:
                return item["fileId"]
        if data and data[0].get("fileId") is not None:
            return data[0]["fileId"]
        raise XtmError(None, f"file generation for job {job_id} returned no file id: {data}")

    def poll_generated_file(self, project_id: int, file_id: int) -> None:
        """GET /projects/{id}/files/status?fileIds=...&fileScope=JOB until FINISHED."""
        deadline = time.time() + self.poll_timeout
        while True:
            data = self._call(
                "GET",
                f"/projects/{project_id}/files/status",
                params={"fileIds": [file_id], "fileScope": "JOB", "fileType": "TARGET"},
            )
            statuses = [item for item in data or [] if item.get("fileId") == file_id] or list(data or [])
            status = statuses[0].get("status") if statuses else None
            if status == "FINISHED":
                return
            if status in GENERATED_FILE_FAILURES:
                raise XtmError(None, f"generating file {file_id} failed: {statuses[0].get('message')}")
            if time.time() >= deadline:
                raise XtmError(None, f"file {file_id} was not generated within {self.poll_timeout}s")
            time.sleep(self.poll_interval)

    def download_file(self, project_id: int, file_id: int) -> bytes:
        """GET /projects/{id}/files/{fileId}/download?fileScope=JOB -> ZIP (application/octet-stream)."""
        response = self._authed(
            "GET", f"/projects/{project_id}/files/{file_id}/download", params={"fileScope": "JOB"}
        )
        if response.status_code >= 400:
            raise XtmError(response.status_code, _reason(response))
        return response.content


def extract_json_from_zip(archive: bytes, expected_name: str) -> dict:
    """Return the parsed JSON target file inside a downloaded ZIP.

    Prefer a member whose basename matches the uploaded file name; fall back to the only `.json`
    member. The ZIP's internal folder layout isn't documented, so don't depend on it.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            members = [info for info in zf.infolist() if not info.is_dir()]
            json_members = [info for info in members if info.filename.lower().endswith(".json")]
            chosen = next((m for m in members if m.filename.rsplit("/", 1)[-1] == expected_name), None)
            if chosen is None and len(json_members) == 1:
                chosen = json_members[0]
            if chosen is None:
                names = [m.filename for m in members]
                raise XtmError(None, f"downloaded archive has no target file for {expected_name}: {names}")
            raw = zf.read(chosen)
    except zipfile.BadZipFile:
        raise XtmError(None, f"downloaded target for {expected_name} is not a ZIP archive") from None
    return json.loads(raw.decode("utf-8-sig"))
