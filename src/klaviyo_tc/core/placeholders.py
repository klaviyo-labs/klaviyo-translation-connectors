"""Placeholder-signature check shared by every provider (Klaviyo's own syntax)."""
from __future__ import annotations

import re
from collections import Counter


def _normalize(token: str) -> str:
    return " ".join(token.split())


def signature(text: str, pattern: str) -> tuple[Counter, list[str]]:
    """Return (multiset of {{...}} tokens, ordered list of {%...%} tokens)."""
    curly: list[str] = []
    percent: list[str] = []
    for match in re.finditer(pattern, text):
        token = _normalize(match.group(0))
        if match.group(0).startswith("{{"):
            curly.append(token)
        elif match.group(0).startswith("{%"):
            percent.append(token)
    return Counter(curly), percent


def matches(source: str, translation: str, pattern: str) -> bool:
    """{{vars}} may reorder (multiset); {% tags %} must keep their order."""
    source_curly, source_percent = signature(source, pattern)
    translation_curly, translation_percent = signature(translation, pattern)
    return source_curly == translation_curly and source_percent == translation_percent
