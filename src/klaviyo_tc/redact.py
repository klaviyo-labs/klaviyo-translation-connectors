"""Scrub secret env var values, issued provider tokens, and auth headers before printing."""
import os
import re

SECRET_ENV_VARS = (
    "KLAVIYO_API_KEY",
    "SMARTLING_USER_IDENTIFIER",
    "SMARTLING_USER_SECRET",
)

# Tokens issued at runtime (not env vars); providers register these once obtained.
_dynamic_secrets: set[str] = set()

_AUTHORIZATION_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\n\r,;\"']+)")


def register_secret(value: str | None) -> None:
    if value:
        _dynamic_secrets.add(value)


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***REDACTED***")
    for value in _dynamic_secrets:
        text = text.replace(value, "***REDACTED***")
    text = _AUTHORIZATION_RE.sub(lambda m: m.group(1) + "***REDACTED***", text)
    return text
