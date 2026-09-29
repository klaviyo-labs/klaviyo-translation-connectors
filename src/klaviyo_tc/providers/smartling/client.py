"""Smartling REST API client: auth, jobs, batches, file upload/status/download.

Envelope contract: `{"response": {"code": "SUCCESS", "data": {...}}}`; any other
code is an error. Auth token is cached in memory and refreshed on a 401.
"""
from __future__ import annotations

import random
import time

import httpx

MAX_ATTEMPTS = 6
FAILURE_BATCH_STATUSES = {"FAILED", "CANCELLED"}


class SmartlingError(Exception):
    def __init__(self, status_code: int | None, detail):
        super().__init__(f"Smartling API error {status_code}: {detail}")
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
        if info.get("code") != "SUCCESS":
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

    def _ensure_token(self) -> None:
        if not self._access_token or time.time() >= self._expires_at:
            self._authenticate()

    def _authed(self, method: str, url: str, retried: bool = False, **kwargs) -> httpx.Response:
        self._ensure_token()
        headers = kwargs.pop("headers", None) or {}
        headers["Authorization"] = f"Bearer {self._access_token}"
        response = self._send(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and not retried:
            self._authenticate()
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

    def create_batch(self, translation_job_uid: str, file_uri: str) -> str:
        data = self._call(
            "POST",
            f"/job-batches-api/v2/projects/{self.project_id}/batches",
            json={"authorize": True, "translationJobUid": translation_job_uid, "fileUris": [file_uri]},
        )
        return data["batchUid"]

    def upload_file(self, batch_uid: str, file_uri: str, content: bytes, locale_ids: list[str]) -> None:
        files = {"file": (file_uri, content, "application/json")}
        # httpx expands a list value into repeated form fields of the same name.
        data = {"fileUri": file_uri, "fileType": "json", "localeIdsToAuthorize[]": list(locale_ids)}
        self._call(
            "POST",
            f"/job-batches-api/v2/projects/{self.project_id}/batches/{batch_uid}/file",
            files=files,
            data=data,
        )

    def get_batch_status(self, batch_uid: str) -> str:
        data = self._call("GET", f"/job-batches-api/v2/projects/{self.project_id}/batches/{batch_uid}")
        return data["status"]

    def poll_batch(self, batch_uid: str) -> None:
        deadline = time.time() + self.poll_timeout
        while True:
            status = self.get_batch_status(batch_uid)
            if status == "COMPLETED":
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
