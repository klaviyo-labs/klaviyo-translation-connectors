"""Smartling REST API client: auth, jobs, batches, file upload/status/download.

Envelope contract: `{"response": {"code": "SUCCESS"|"ACCEPTED", "data": {...}}}`;
any other code is an error. Auth token is cached in memory and refreshed on a 401.
"""
from __future__ import annotations

import json
import random
import threading
import time

import httpx

from ...redact import register_secret

MAX_ATTEMPTS = 6
SUCCESS_CODES = {"SUCCESS", "ACCEPTED"}
FAILURE_BATCH_STATUSES = {"FAILED", "CANCELLED"}
FAILURE_FILE_STATUSES = {"FAILED", "ERROR"}


class SmartlingError(Exception):
    def __init__(self, status_code: int | None, detail):
        super().__init__(f"Smartling API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _has_errors(value) -> bool:
    # Batch status sends errors as JSON-encoded strings, so success reads '{}', not an empty value.
    if isinstance(value, str):
        return value.strip() not in ("", "{}", "[]", "null")
    return bool(value)


def _only_no_content(general_errors) -> bool:
    # Every string was already translated, so the job had nothing to add; the files still uploaded.
    try:
        parsed = json.loads(general_errors) if isinstance(general_errors, str) else general_errors
        messages = [e.get("message") for e in parsed.get("errors", [])]
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(messages) and all(m == "Job has no content" for m in messages)


def _file_failed(file_info: dict) -> bool:
    if file_info.get("status") in FAILURE_FILE_STATUSES:
        return True
    return _has_errors(file_info.get("errors"))


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(0.05 * (2**attempt), 1.0) + random.uniform(0, 0.02)


class SmartlingClient:
    def __init__(
        self,
        base_url: str,
        user_identifier: str,
        user_secret: str,
        project_id: str,
        version: str = "0.0.0",
        timeout: float = 30.0,
        poll_interval: float = 3.0,
        poll_timeout: float = 120.0,
    ):
        self.project_id = project_id
        self.user_identifier = user_identifier
        self.user_secret = user_secret
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"User-Agent": f"klaviyo-translation-connectors/{version}"},
            timeout=timeout,
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._auth_lock = threading.Lock()

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            response = self._client.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    return response
                time.sleep(_retry_delay(response, attempt))
                continue
            return response

    def _envelope(self, response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise SmartlingError(response.status_code, response.text) from None
        info = payload.get("response", {})
        if info.get("code") not in SUCCESS_CODES:
            raise SmartlingError(response.status_code, info.get("errors") or payload)
        return info.get("data") or {}

    def _authenticate(self) -> None:
        response = self._send(
            "POST",
            "/auth-api/v2/authenticate",
            json={"userIdentifier": self.user_identifier, "userSecret": self.user_secret},
        )
        data = self._envelope(response)
        self._access_token = data["accessToken"]
        self._refresh_token = data["refreshToken"]
        self._expires_at = time.time() + data["expiresIn"] - 30  # renew a little early
        # Issued tokens aren't known secrets ahead of time; register them so they're redacted too.
        register_secret(self._access_token)
        register_secret(self._refresh_token)

    def _ensure_token(self, stale_token: str | None = None) -> str:
        # Serialized so concurrent workers share one refresh instead of racing to authenticate.
        with self._auth_lock:
            expired = not self._access_token or time.time() >= self._expires_at
            if expired or (stale_token is not None and self._access_token == stale_token):
                self._authenticate()
            return self._access_token

    def _authed(self, method: str, url: str, retried: bool = False, **kwargs) -> httpx.Response:
        token = self._ensure_token()
        headers = kwargs.pop("headers", None) or {}
        headers["Authorization"] = f"Bearer {token}"
        response = self._send(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and not retried:
            self._ensure_token(stale_token=token)
            return self._authed(method, url, retried=True, **kwargs)
        return response

    def _call(self, method: str, url: str, **kwargs) -> dict:
        return self._envelope(self._authed(method, url, **kwargs))

    def create_job(self, job_name: str, target_locale_ids: list[str], reference_number: str) -> str:
        data = self._call(
            "POST",
            f"/jobs-api/v3/projects/{self.project_id}/jobs",
            json={
                "jobName": job_name,
                "targetLocaleIds": target_locale_ids,
                "referenceNumber": reference_number,
            },
        )
        return data["translationJobUid"]

    def find_job_by_name(self, job_name: str) -> str | None:
        """Adopt an existing job of this name (job names are project-unique)."""
        data = self._call(
            "GET", f"/jobs-api/v3/projects/{self.project_id}/jobs", params={"jobName": job_name}
        )
        for item in data.get("items", []):
            if item.get("jobName") == job_name:
                return item.get("translationJobUid")
        return None

    def create_batch(
        self,
        translation_job_uid: str,
        file_uris: list[str],
        authorize: bool = True,
        locale_ids: list[str] | None = None,
        workflow_uid: str | None = None,
    ) -> str:
        body = {"authorize": authorize, "translationJobUid": translation_job_uid, "fileUris": list(file_uris)}
        if authorize and workflow_uid:
            body["localeWorkflows"] = [
                {"targetLocaleId": locale, "workflowUid": workflow_uid} for locale in locale_ids or []
            ]
        data = self._call("POST", f"/job-batches-api/v2/projects/{self.project_id}/batches", json=body)
        return data["batchUid"]

    def upload_file(
        self, batch_uid: str, file_uri: str, content: bytes, locale_ids: list[str], authorize: bool = True
    ) -> None:
        files = {"file": (file_uri, content, "application/json")}
        # httpx expands a list value into repeated form fields of the same name.
        data = {"fileUri": file_uri, "fileType": "json"}
        if authorize:
            data["localeIdsToAuthorize[]"] = list(locale_ids)
        self._call(
            "POST",
            f"/job-batches-api/v2/projects/{self.project_id}/batches/{batch_uid}/file",
            files=files,
            data=data,
        )

    def get_batch_status(self, batch_uid: str) -> dict:
        return self._call("GET", f"/job-batches-api/v2/projects/{self.project_id}/batches/{batch_uid}")

    def poll_batch(self, batch_uid: str) -> None:
        deadline = time.time() + self.poll_timeout
        while True:
            data = self.get_batch_status(batch_uid)
            status = data.get("status")
            if status == "COMPLETED":
                general_errors = data.get("generalErrors")
                failed_files = [f for f in data.get("files", []) if _file_failed(f)]
                if (_has_errors(general_errors) and not _only_no_content(general_errors)) or failed_files:
                    raise SmartlingError(
                        None, f"batch {batch_uid} completed with errors: general={general_errors} files={failed_files}"
                    )
                return
            if status in FAILURE_BATCH_STATUSES:
                raise SmartlingError(None, f"batch {batch_uid} failed with status {status}")
            if time.time() >= deadline:
                raise SmartlingError(None, f"batch {batch_uid} did not complete within {self.poll_timeout}s")
            time.sleep(self.poll_interval)

    def get_file_status(self, file_uri: str) -> dict:
        return self._call(
            "GET",
            f"/files-api/v2/projects/{self.project_id}/file/status",
            params={"fileUri": file_uri},
        )

    def download_file(self, locale_id: str, file_uri: str) -> bytes:
        response = self._authed(
            "GET",
            f"/files-api/v2/projects/{self.project_id}/locales/{locale_id}/file",
            params={"fileUri": file_uri, "retrievalType": "published", "includeOriginalStrings": "false"},
        )
        if response.status_code >= 400:
            raise SmartlingError(response.status_code, response.text)
        return response.content
