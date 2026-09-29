"""Provider registry keyed by the `[provider].name` config value."""
from __future__ import annotations

from .smartling.provider import SmartlingProvider

REGISTRY: dict[str, type] = {
    "smartling": SmartlingProvider,
}


def get_provider_class(name: str) -> type:
    try:
        return REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown provider '{name}' (available: {available})") from None
