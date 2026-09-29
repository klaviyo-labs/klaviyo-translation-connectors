"""Klaviyo V3 Translations API (beta) client.

Endpoint contract (verified from Klaviyo source):
- GET /api/translations/ is cursor paginated and rejects `additional-fields`.
- GET /api/translations/{id}/?additional-fields[translation]=values returns
  `values` (id/source_value/translations per string) alongside target_locales.
- PATCH /api/translations/{id}/ updates `values` in place, chunked to <=100.
"""
from __future__ import annotations

import random
import time
from typing import Iterator

import httpx

MAX_ATTEMPTS = 6
PATCH_CHUNK_SIZE = 100


class KlaviyoAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"Klaviyo API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(0.05 * (2**attempt), 1.0) + random.uniform(0, 0.02)


class KlaviyoClient:
    def __init__(self, base_url: str, api_key: str, revision: str, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Klaviyo-API-Key {api_key}",
                "revision": revision,
                "accept": "application/vnd.api+json",
                "content-type": "application/vnd.api+json",
            },
            timeout=timeout,
        )

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            response = self._client.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise KlaviyoAPIError(response.status_code, response.text)
                time.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise KlaviyoAPIError(response.status_code, response.text)
            return response

    def list_translations(self, channel: str | None = None, resource_type: str | None = None) -> Iterator[dict]:
        filters = []
        if channel:
            filters.append(f'equals(channel,"{channel}")')
        if resource_type:
            filters.append(f'equals(resource_type,"{resource_type}")')
        params: dict | None = {"page[size]": 100}
        if len(filters) == 1:
            params["filter"] = filters[0]
        elif len(filters) > 1:
            params["filter"] = f"and({','.join(filters)})"

        url = "/api/translations/"
        while url:
            response = self._request("GET", url, params=params)
            payload = response.json()
            for item in payload.get("data", []):
                yield item
            url = (payload.get("links") or {}).get("next")
            params = None  # the cursor link already carries all query params

    def get_translation(self, translation_id: str) -> dict:
        response = self._request(
            "GET",
            f"/api/translations/{translation_id}/",
            params={"additional-fields[translation]": "values"},
        )
        return response.json()["data"]

    def patch_translation(self, translation_id: str, values: list[dict]) -> None:
        for start in range(0, len(values), PATCH_CHUNK_SIZE):
            chunk = values[start : start + PATCH_CHUNK_SIZE]
            body = {
                "data": {
                    "type": "translation",
                    "id": translation_id,
                    "attributes": {"values": chunk},
                }
            }
            self._request("PATCH", f"/api/translations/{translation_id}/", json=body)
