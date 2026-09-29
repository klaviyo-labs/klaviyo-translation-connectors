"""Load klaviyo-tc.toml. No secrets ever live here -- those come from env vars."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = "klaviyo-tc.toml"
DEFAULT_STATE_PATH = ".klaviyo-tc/state.db"

# Written by `klaviyo-tc init`. Raw string keeps regex backslashes unescaped in the file.
TEMPLATE = r"""[klaviyo]
base_url = "https://a.klaviyo.com"
revision = "2026-07-15.pre"
# Klaviyo's own placeholder syntax ({{var}}, {% tag %}); used to detect
# translations that dropped or reordered placeholders, regardless of provider.
placeholder_pattern = '\{\{[^}]+\}\}|\{%[^%]+%\}'

[provider]
name = "smartling"

[providers.smartling]
base_url = "https://api.smartling.com"
project_id = ""
string_format_paths = "html: *"
placeholder_format_custom = '\{\{[^}]+\}\}|\{%[^%]+%\}'

[locales]
fr = "fr-FR"

[state]
path = ".klaviyo-tc/state.db"
"""


class ConfigError(Exception):
    pass


@dataclass
class KlaviyoConfig:
    base_url: str
    revision: str
    placeholder_pattern: str


@dataclass
class Config:
    klaviyo: KlaviyoConfig
    provider_name: str
    provider_config: dict
    locales: dict[str, str]
    state_path: str


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"config file not found: {path} (run 'klaviyo-tc init')")
    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    try:
        klaviyo_raw = raw["klaviyo"]
        provider_name = raw["provider"]["name"]
        provider_config = raw["providers"][provider_name]
    except KeyError as exc:
        raise ConfigError(f"missing config section: {exc}") from exc

    return Config(
        klaviyo=KlaviyoConfig(
            base_url=klaviyo_raw["base_url"],
            revision=klaviyo_raw["revision"],
            placeholder_pattern=klaviyo_raw["placeholder_pattern"],
        ),
        provider_name=provider_name,
        provider_config=provider_config,
        locales=raw.get("locales", {}),
        state_path=raw.get("state", {}).get("path", DEFAULT_STATE_PATH),
    )
