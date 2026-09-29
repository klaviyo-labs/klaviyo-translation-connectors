"""config.yaml loading: the shared TEMPLATE, secret-key rejection, and required-field errors."""
from pathlib import Path

import pytest

from klaviyo_tc.core.config import TEMPLATE, ConfigError, load_config

from conftest import PLACEHOLDER_FORMAT_CUSTOM

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_config_example_yaml_matches_the_init_template():
    """config.example.yaml (committed) and `klaviyo-tc init`'s output must never drift apart."""
    assert (REPO_ROOT / "config.example.yaml").read_text() == TEMPLATE


def test_template_round_trips_through_load_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(TEMPLATE)

    config = load_config("config.yaml")

    assert config.provider_config["placeholder_format_custom"] == PLACEHOLDER_FORMAT_CUSTOM
    assert config.klaviyo.revision == "2026-07-15.pre"
    assert config.klaviyo.revisions == {
        "flows": "2025-10-15", "tags": "2025-10-15", "templates": "2025-10-15", "universal_content": "2025-10-15"
    }
    assert config.locales == {"fr": "fr-FR", "de": "de-DE"}


@pytest.mark.parametrize("secret_key", ["api_key", "user_secret", "userSecret", "secret", "token"])
def test_load_config_rejects_secret_looking_keys(tmp_path, monkeypatch, secret_key):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        f"""klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
provider:
  name: smartling
providers:
  smartling:
    project_id: proj123
    {secret_key}: "should-not-be-here"
"""
    )

    with pytest.raises(ConfigError, match=f"providers.smartling.{secret_key}.*environment variable"):
        load_config("config.yaml")


def test_load_config_names_the_missing_project_id_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        """klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
provider:
  name: smartling
providers:
  smartling:
    project_id: ""
"""
    )

    with pytest.raises(ConfigError, match=r"^providers\.smartling\.project_id is required$"):
        load_config("config.yaml")


def test_load_config_names_missing_klaviyo_base_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        """klaviyo:
  revision: "2026-07-15.pre"
provider:
  name: smartling
providers:
  smartling:
    project_id: proj123
"""
    )

    with pytest.raises(ConfigError, match=r"^klaviyo\.base_url is required$"):
        load_config("config.yaml")
