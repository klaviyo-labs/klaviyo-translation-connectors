"""Smartling implementation of the core `Provider` protocol.

Smartling-only concepts (jobs, batches, the file directives envelope) live
here; the sync engine never sees them.
"""
from __future__ import annotations

import json

from ..base import SubmitResult
from .client import SmartlingClient


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
        placeholder_format_custom: str,
        version: str = "0.0.0",
    ):
        self.string_format_paths = string_format_paths
        self.placeholder_format_custom = placeholder_format_custom
        self._client = SmartlingClient(base_url, user_identifier, user_secret, project_id, version=version)

    def _build_file_content(self, strings: dict[str, str]) -> dict:
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
    ) -> SubmitResult:
        state = dict(state or {})

        if not state.get("job_uid"):
            state["job_uid"] = self._client.create_job(
                job_name=f"klaviyo {translation_id}",
                target_locale_ids=target_locales,
                reference_number=reference,
            )
        if not state.get("batch_uid"):
            state["batch_uid"] = self._client.create_batch(state["job_uid"], file_name)

        content = self._build_file_content(strings)
        self._client.upload_file(
            state["batch_uid"], file_name, json.dumps(content, sort_keys=True).encode("utf-8"), target_locales
        )
        self._client.poll_batch(state["batch_uid"])
        state["file_uri"] = file_name

        return SubmitResult(state=state, submitted=True)

    def completed_locales(self, state: dict) -> list[str]:
        status = self._client.get_file_status(state["file_uri"])
        return [item["localeId"] for item in status.get("items", []) if item.get("completedStringCount")]

    def fetch(self, state: dict, locale: str) -> dict[str, str]:
        raw = self._client.download_file(locale, state["file_uri"])
        data = json.loads(raw)
        data.pop("smartling", None)
        return {key: value for key, value in data.items() if value}
