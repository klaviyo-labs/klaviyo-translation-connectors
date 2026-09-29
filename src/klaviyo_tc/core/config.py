"""Load config.yaml. No secrets ever live here -- those come from env vars."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_STATE_PATH = ".klaviyo-tc/state.db"
DEFAULT_LOCALE = "en"
# Flows, tags, and templates are GA endpoints with their own revision,
# independent of the beta campaign/translations revision above.
DEFAULT_REVISION_OVERRIDES = {
    "flows": "2025-10-15", "tags": "2025-10-15", "templates": "2025-10-15", "universal_content": "2025-10-15"
}
DEFAULT_FILES_PER_BATCH = 100
DEFAULT_AUTHORIZE = True

# YAML parses bare dates as `datetime.date`; normalize revisions back to `str`.
SECRET_KEY_MARKERS = ("apikey", "secret", "token")

# Single source of truth for config.example.yaml and `klaviyo-tc init`'s output.
TEMPLATE = r"""# Everything here is non-secret and safe to commit. Secrets come only from env vars:
# KLAVIYO_API_KEY, SMARTLING_USER_IDENTIFIER, SMARTLING_USER_SECRET
klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
  source_locale: en
  fallback_locale: en
  # Flows/tags/templates use their own GA revision, not klaviyo.revision.
  revisions:
    flows: "2025-10-15"
    tags: "2025-10-15"
    templates: "2025-10-15"
    universal_content: "2025-10-15"

provider:
  name: smartling

providers:
  smartling:
    base_url: https://api.smartling.com
    project_id: "your-project-id"
    account_uid: ""            # optional; future webhook use
    authorize: true             # false: create unauthorized jobs
    files_per_batch: 100
    string_format_paths: "html: *"
    # Sent to Smartling verbatim; our own parser ignores this.
    placeholder_format_custom: ['\{\{[^}]+\}\}', '\{%[^%]+%\}']

locales:                      # klaviyo locale: provider locale
  fr: fr-FR
  de: de-DE

state:
  path: .klaviyo-tc/state.db
"""


class ConfigError(Exception):
    pass


@dataclass
class KlaviyoConfig:
    base_url: str
    revision: str
    source_locale: str = DEFAULT_LOCALE
    fallback_locale: str = DEFAULT_LOCALE
    revisions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_REVISION_OVERRIDES))


@dataclass
class Config:
    klaviyo: KlaviyoConfig
    provider_name: str
    provider_config: dict
    locales: dict[str, str]
    state_path: str


def _as_str(value) -> str:
    """Undo YAML's implicit date/datetime parsing for revision-like scalars."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _require(value, path: str):
    if value in (None, ""):
        raise ConfigError(f"{path} is required")
    return value


def _check_no_secrets(node, path: str = "") -> None:
    """Refuse any key that looks like it's meant to hold a secret value."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_norm = str(key).lower().replace("_", "").replace("-", "")
            full_path = f"{path}.{key}" if path else str(key)
            if any(marker in key_norm for marker in SECRET_KEY_MARKERS):
                raise ConfigError(f"config key '{full_path}' looks like a secret; use an environment variable instead")
            _check_no_secrets(value, full_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_no_secrets(item, f"{path}[{index}]")


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"config file not found: {path} (run 'klaviyo-tc init')")
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    _check_no_secrets(raw)

    klaviyo_raw = raw.get("klaviyo") or {}
    base_url = _require(klaviyo_raw.get("base_url"), "klaviyo.base_url")
    revision = _require(_as_str(klaviyo_raw.get("revision")), "klaviyo.revision")

    provider_name = _require((raw.get("provider") or {}).get("name"), "provider.name")
    provider_config = (raw.get("providers") or {}).get(provider_name)
    if provider_config is None:
        raise ConfigError(f"providers.{provider_name} is required")
    if provider_name == "smartling":
        _require(provider_config.get("project_id"), "providers.smartling.project_id")

    revisions = dict(DEFAULT_REVISION_OVERRIDES)
    revisions.update({k: _as_str(v) for k, v in (klaviyo_raw.get("revisions") or {}).items()})

    return Config(
        klaviyo=KlaviyoConfig(
            base_url=base_url,
            revision=revision,
            source_locale=klaviyo_raw.get("source_locale", DEFAULT_LOCALE),
            fallback_locale=klaviyo_raw.get("fallback_locale", DEFAULT_LOCALE),
            revisions=revisions,
        ),
        provider_name=provider_name,
        provider_config=provider_config,
        locales=raw.get("locales") or {},
        state_path=(raw.get("state") or {}).get("path", DEFAULT_STATE_PATH),
    )
