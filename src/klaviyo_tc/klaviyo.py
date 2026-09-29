"""Klaviyo API client: Translations (beta), plus Campaigns/Flows/Tags/Templates for scope resolution.

Endpoint contract (verified from Klaviyo source):
- GET /api/translations/ is cursor paginated and rejects `additional-fields`.
- GET /api/translations/{id}/?additional-fields[translation]=values returns
  `values` (id/source_value/translations per string) alongside target_locales.
- PATCH /api/translations/{id}/ updates `values` in place, chunked to <=100.
- GET /api/templates/ and /api/template-universal-content/ (Klaviyo's GA
  Templates API) support `contains(name,...)` / `greater-than(updated,...)`
  filters; single-item `get_template` follows from the plural route.

- A GA (2025-10-15) campaign message's id is its translation's
  `campaign-variation` id; beta revisions return 404 for GA campaigns
  (verified live). Omni campaigns fall back to message -> variation traversal,
  which is not yet verified against a live account.
- Flow -> action -> message and tag -> campaigns/flows paths were verified live.
`get_universal_content_item` (a single-item GET) is inferred by symmetry with
`get_template` and was not given in the Templates API contract either.
"""
from __future__ import annotations

import random
import time
from typing import Iterator

import httpx

MAX_ATTEMPTS = 6
PATCH_CHUNK_SIZE = 100


class KlaviyoAPIError(Exception):
    def __init__(self, status_code: int | None, body: str):
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
    def __init__(
        self,
        base_url: str,
        api_key: str,
        revision: str,
        revisions: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        self.revision = revision
        # Per-resource-family revision overrides (e.g. "flows", "tags").
        self.revisions = revisions or {}
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

    def _request(self, method: str, url: str, revision: str | None = None, **kwargs) -> httpx.Response:
        if revision:
            headers = kwargs.pop("headers", None) or {}
            headers["revision"] = revision
            kwargs["headers"] = headers
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

    def _paginated(self, url: str, params: dict | None = None, revision: str | None = None) -> Iterator[dict]:
        while url:
            response = self._request("GET", url, params=params, revision=revision)
            payload = response.json()
            for item in payload.get("data", []):
                yield item
            url = (payload.get("links") or {}).get("next")
            params = None  # the cursor link already carries all query params

    # -- Translations -----------------------------------------------------

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
        yield from self._paginated("/api/translations/", params=params)

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

    def find_translation_for_resource(self, resource_id: str) -> dict | None:
        params = {"filter": f'equals(related_resource_id,"{resource_id}")'}
        matches = list(self._paginated("/api/translations/", params=params))
        return matches[0] if matches else None

    def create_translation(
        self,
        *,
        channel: str,
        resource_type: str,
        resource_id: str,
        source_locale: str,
        target_locales: list[str],
        fallback_locale: str,
    ) -> dict:
        body = {
            "data": {
                "type": "translation",
                "attributes": {
                    "channel": channel,
                    "source_locale": source_locale,
                    "target_locales": target_locales,
                    "fallback_locale": fallback_locale,
                },
                "relationships": {resource_type: {"data": {"type": resource_type, "id": resource_id}}},
            }
        }
        response = self._request("POST", "/api/translations/", json=body)
        return response.json()["data"]

    # -- Campaigns ----------------------------------------------------------

    def _campaign_revision(self, omni: bool) -> str | None:
        # Beta (omni) revisions can't see campaigns made with the GA Campaigns API, and vice versa.
        return self.revision if omni else self.revisions.get("campaigns")

    def get_campaign(self, campaign_id: str, *, omni: bool = False) -> dict:
        response = self._request("GET", f"/api/campaigns/{campaign_id}/", revision=self._campaign_revision(omni))
        return response.json()["data"]

    def list_campaign_messages(self, campaign_id: str, *, omni: bool = False) -> list[dict]:
        return list(
            self._paginated(f"/api/campaigns/{campaign_id}/campaign-messages", revision=self._campaign_revision(omni))
        )

    def list_campaign_variations(self, message_id: str) -> list[dict]:
        return list(self._paginated(f"/api/campaign-messages/{message_id}/campaign-variations", revision=self.revision))

    # -- Flows (GA revision override) ---------------------------------------

    def get_flow(self, flow_id: str) -> dict:
        response = self._request("GET", f"/api/flows/{flow_id}/", revision=self.revisions.get("flows"))
        return response.json()["data"]

    def list_flow_actions(self, flow_id: str) -> list[dict]:
        return list(
            self._paginated(f"/api/flows/{flow_id}/flow-actions", revision=self.revisions.get("flows"))
        )

    def list_flow_messages(self, action_id: str) -> list[dict]:
        return list(
            self._paginated(f"/api/flow-actions/{action_id}/flow-messages", revision=self.revisions.get("flows"))
        )

    # -- Tags (GA revision override) -----------------------------------------

    def find_tags_by_name(self, name: str) -> list[dict]:
        params = {"filter": f'equals(name,"{name}")'}
        return list(self._paginated("/api/tags/", params=params, revision=self.revisions.get("tags")))

    def list_tag_campaign_ids(self, tag_id: str) -> list[str]:
        items = self._paginated(
            f"/api/tags/{tag_id}/relationships/campaigns", revision=self.revisions.get("tags")
        )
        return [item["id"] for item in items]

    def list_tag_flow_ids(self, tag_id: str) -> list[str]:
        items = self._paginated(f"/api/tags/{tag_id}/relationships/flows", revision=self.revisions.get("tags"))
        return [item["id"] for item in items]

    # -- Templates / universal content (GA revision overrides) ---------------

    def get_template(self, template_id: str) -> dict:
        response = self._request(
            "GET", f"/api/templates/{template_id}/", revision=self.revisions.get("templates")
        )
        return response.json()["data"]

    def list_templates(self, name_contains: str | None = None, updated_since: str | None = None) -> list[dict]:
        filters = []
        if name_contains:
            filters.append(f'contains(name,"{name_contains}")')
        if updated_since:
            filters.append(f'greater-than(updated,{updated_since})')
        params: dict | None = None
        if len(filters) == 1:
            params = {"filter": filters[0]}
        elif len(filters) > 1:
            params = {"filter": f"and({','.join(filters)})"}
        return list(self._paginated("/api/templates/", params=params, revision=self.revisions.get("templates")))

    def get_universal_content_item(self, uc_id: str) -> dict:
        response = self._request(
            "GET", f"/api/template-universal-content/{uc_id}/", revision=self.revisions.get("universal_content")
        )
        return response.json()["data"]

    def list_universal_content(self, name_contains: str | None = None) -> list[dict]:
        params = {"filter": f'contains(name,"{name_contains}")'} if name_contains else None
        return list(
            self._paginated(
                "/api/template-universal-content/", params=params, revision=self.revisions.get("universal_content")
            )
        )
