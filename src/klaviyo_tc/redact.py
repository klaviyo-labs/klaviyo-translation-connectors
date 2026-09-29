"""Scrub secret env var values out of anything we're about to print."""
import os

SECRET_ENV_VARS = (
    "KLAVIYO_API_KEY",
    "SMARTLING_USER_IDENTIFIER",
    "SMARTLING_USER_SECRET",
)


def redact(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***REDACTED***")
    return text
