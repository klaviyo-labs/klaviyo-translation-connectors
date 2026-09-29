"""Placeholder-signature check shared by every provider (Klaviyo's own syntax).

A hand-written scanner rather than a regex: `{{ }}`/`{% %}` tags can contain
quoted string literals with their own braces/percents (`{% if x == "10%" %}`,
`{{ v|default:"}" }}`), so the true tag end must skip over quoted spans.
"""
from __future__ import annotations

from collections import Counter

_OPEN_CURLY = "{{"
_CLOSE_CURLY = "}}"
_OPEN_PERCENT = "{%"
_CLOSE_PERCENT = "%}"


def _find_tag_close(text: str, start: int, closer: str) -> int | None:
    """Index of `closer`'s first char at or after `start`, skipping quoted spans."""
    quote = None
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if text.startswith(closer, i):
            return i
        i += 1
    return None


def _normalize_outside_quotes(token: str) -> str:
    """Collapse whitespace runs to one space, but only outside quoted spans."""
    result: list[str] = []
    quote = None
    pending_space = False
    for ch in token:
        if quote:
            result.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote = ch
            result.append(ch)
            continue
        if ch.isspace():
            if result:
                pending_space = True
            continue
        if pending_space:
            result.append(" ")
            pending_space = False
        result.append(ch)
    return "".join(result)


def signature(text: str) -> tuple[Counter, list[str]]:
    """Return (multiset of {{...}} tokens, ordered list of {%...%} tokens)."""
    curly: list[str] = []
    percent: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(_OPEN_CURLY, i):
            close = _find_tag_close(text, i + len(_OPEN_CURLY), _CLOSE_CURLY)
            end = close + len(_CLOSE_CURLY) if close is not None else n
            curly.append(_normalize_outside_quotes(text[i:end]))
            i = end
        elif text.startswith(_OPEN_PERCENT, i):
            close = _find_tag_close(text, i + len(_OPEN_PERCENT), _CLOSE_PERCENT)
            end = close + len(_CLOSE_PERCENT) if close is not None else n
            percent.append(_normalize_outside_quotes(text[i:end]))
            i = end
        else:
            i += 1
    return Counter(curly), percent


def matches(source: str, translation: str) -> bool:
    """{{vars}} may reorder (multiset); {% tags %} must keep their order."""
    source_curly, source_percent = signature(source)
    translation_curly, translation_percent = signature(translation)
    return source_curly == translation_curly and source_percent == translation_percent
