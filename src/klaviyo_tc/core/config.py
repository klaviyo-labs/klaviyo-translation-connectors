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
    "campaigns": "2025-10-15", "flows": "2025-10-15", "tags": "2025-10-15", "templates": "2025-10-15", "universal_content": "2025-10-15"
}
DEFAULT_FILES_PER_BATCH = 100
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 32
DEFAULT_AUTHORIZE = True

# YAML parses bare dates as `datetime.date`; normalize revisions back to `str`.
SECRET_KEY_MARKERS = ("apikey", "secret", "token", "password")
# Non-secret keys each provider can't run without; checked when that provider is selected.
REQUIRED_PROVIDER_KEYS = {
    "smartling": ("project_id",),
    "xtm": ("base_url", "customer_id"),
}

# Single source of truth for config.example.yaml and `klaviyo-tc init`'s output.
TEMPLATE = r"""# Everything here is non-secret and safe to commit. Secrets come only from env vars:
# KLAVIYO_API_KEY, plus SMARTLING_USER_IDENTIFIER and SMARTLING_USER_SECRET (Smartling)
# or XTM_CLIENT, XTM_USER_ID and XTM_PASSWORD (XTM).
klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
  source_locale: en
  fallback_locale: en
  concurrency: 4              # parallel requests; whole-account sweeps load Klaviyo rendering
  # Flows/tags/templates use their own GA revision, not klaviyo.revision.
  revisions:
    campaigns: "2025-10-15"
    flows: "2025-10-15"
    tags: "2025-10-15"
    templates: "2025-10-15"
    universal_content: "2025-10-15"

provider:
  name: smartling             # or: xtm

providers:
  smartling:
    base_url: https://api.smartling.com
    project_id: "your-project-id"
    account_uid: ""            # optional; future webhook use
    authorize: true             # false: create unauthorized jobs
    workflow_uid: ""            # optional; authorize into this workflow instead of the locale default
    files_per_batch: 100
    string_format_paths: "html: *"
    # Sent to Smartling verbatim; our own parser ignores this.
    placeholder_format_custom: ['\{\{[^}]+\}\}', '\{%[^%]+%\}']

  xtm:
    # Your XTM URL plus /project-manager-api-rest, e.g. https://yourcompany.xtm-intl.com/project-manager-api-rest
    base_url: https://api.xtm-cloud.com/project-manager-api-rest
    customer_id: 0              # XTM customer that owns the projects
    # Either a project template (languages, workflow and settings come from it) ...
    template_id: 0              # 0 = no template
    # ... or a source language plus a workflow, by id or by name.
    source_language: en_US
    workflow_id: 0              # 0 = use workflow_name instead
    workflow_name: ""
    filter_template_id: ""      # optional; an XTM file filter that protects {{ }} and {% %} tags
    start_workflow: false       # true: start the XTM workflow once files are analysed
    files_per_upload: 50

locales:                      # klaviyo locale: provider locale (Smartling fr-FR, XTM fr_FR)
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
    concurrency: int = DEFAULT_CONCURRENCY


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


def _check_xtm(provider_config: dict) -> None:
    """XTM needs a template, or a source language plus a workflow (see POST /projects)."""
    for key in ("customer_id", "template_id", "workflow_id"):
        value = provider_config.get(key)
        if value not in (None, "") and (not isinstance(value, int) or isinstance(value, bool)):
            raise ConfigError(f"providers.xtm.{key} must be a whole number, got {value!r}")
    if not provider_config.get("customer_id"):
        raise ConfigError("providers.xtm.customer_id is required")
    if provider_config.get("template_id"):
        return
    if not provider_config.get("source_language"):
        raise ConfigError("providers.xtm.source_language is required when providers.xtm.template_id is not set")
    if not (provider_config.get("workflow_id") or provider_config.get("workflow_name")):
        raise ConfigError("providers.xtm needs template_id, workflow_id or workflow_name to pick the project's workflow")


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
    for key in REQUIRED_PROVIDER_KEYS.get(provider_name, ()):
        _require(provider_config.get(key), f"providers.{provider_name}.{key}")
    if provider_name == "xtm":
        _check_xtm(provider_config)

    concurrency = klaviyo_raw.get("concurrency", DEFAULT_CONCURRENCY)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ConfigError(f"klaviyo.concurrency must be an integer from 1 to {MAX_CONCURRENCY}, got {concurrency!r}")

    revisions = dict(DEFAULT_REVISION_OVERRIDES)
    revisions.update({k: _as_str(v) for k, v in (klaviyo_raw.get("revisions") or {}).items()})

    return Config(
        klaviyo=KlaviyoConfig(
            base_url=base_url,
            revision=revision,
            source_locale=klaviyo_raw.get("source_locale", DEFAULT_LOCALE),
            fallback_locale=klaviyo_raw.get("fallback_locale", DEFAULT_LOCALE),
            revisions=revisions,
            concurrency=concurrency,
        ),
        provider_name=provider_name,
        provider_config=provider_config,
        locales=raw.get("locales") or {},
        state_path=(raw.get("state") or {}).get("path", DEFAULT_STATE_PATH),
    )
