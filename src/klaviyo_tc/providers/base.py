"""The protocol every translation provider implements to plug into the sync engine.

See docs/adding-a-provider.md for a walkthrough.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SubmitResult:
    """Provider-owned continuation state; the engine persists `state` as opaque JSON."""

    state: dict
    submitted: bool = True


@runtime_checkable
class Provider(Protocol):
    name: str

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
        """Submit (or resume, given a prior partial `state`) strings for translation."""
        ...

    def completed_locales(self, state: dict) -> list[str]:
        """Provider locales with translations ready to fetch, given persisted `state`."""
        ...

    def fetch(self, state: dict, locale: str) -> dict[str, str]:
        """Translated strings for `locale`; empty/missing values mean untranslated."""
        ...
