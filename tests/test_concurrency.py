"""Concurrent pull/push across many translations, and klaviyo.concurrency validation."""
import re

import httpx
import pytest
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.config import ConfigError, load_config
from klaviyo_tc.core.state import State

from conftest import (
    KLAVIYO_BASE,
    PROJECT_ID,
    SMARTLING_BASE,
    klaviyo_translation_payload,
    mock_smartling_auth,
)

IDS = [f"campaign-variation::email::t{i:02d}" for i in range(24)]


def _seed(state, translation_id):
    file_name = f"klaviyo/{translation_id.replace('::', '__')}.json"
    generation_id = state.create_generation(translation_id, file_name, {f"{translation_id}::subject": "Hello"}, {"fr": "fr-FR"})
    state.set_provider_state(generation_id, {"file_uri": file_name})
    state.set_status(generation_id, "submitted")


def _translation_id_from(url_path: str) -> str:
    return re.search(r"/api/translations/([^/]+)/", url_path).group(1)


@respx.mock
def test_pull_many_translations_concurrently_records_every_write(project, runner):
    state = State(".klaviyo-tc/state.db")
    for translation_id in IDS:
        _seed(state, translation_id)
    mock_smartling_auth(respx)
    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/file/status").respond(
        json={"response": {"code": "SUCCESS", "data": {"items": [{"localeId": "fr-FR", "completedStringCount": 1}]}}}
    )

    def download(request):
        translation_id = request.url.params["fileUri"].removeprefix("klaviyo/").removesuffix(".json").replace("__", "::")
        return httpx.Response(200, json={"smartling": {}, f"{translation_id}::subject": f"Bonjour {translation_id[-3:]}"})

    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/locales/fr-FR/file").mock(side_effect=download)

    def get_translation(request):
        translation_id = _translation_id_from(request.url.path)
        return httpx.Response(200, json=klaviyo_translation_payload(
            translation_id, target_locales=["fr"],
            values=[{"id": f"{translation_id}::subject", "source_value": "Hello", "translations": {}}],
        ))

    respx.get(url__regex=rf"{KLAVIYO_BASE}/api/translations/[^/]+/").mock(side_effect=get_translation)
    patch_route = respx.patch(url__regex=rf"{KLAVIYO_BASE}/api/translations/[^/]+/").respond(json={"data": {}})

    result = runner.invoke(cli, ["pull", *[arg for tid in IDS for arg in ("--id", tid)]])
    assert result.exit_code == 0, result.output
    assert f"written={len(IDS)}" in result.output
    assert patch_route.calls.call_count == len(IDS)

    fresh = State(".klaviyo-tc/state.db")
    for translation_id in IDS:
        assert fresh.get_written(translation_id, f"{translation_id}::subject", "fr") == f"Bonjour {translation_id[-3:]}"


@respx.mock
def test_dry_run_push_keeps_input_order_under_concurrency(project, runner):
    def get_translation(request):
        translation_id = _translation_id_from(request.url.path)
        return httpx.Response(200, json=klaviyo_translation_payload(
            translation_id, target_locales=["fr"],
            values=[{"id": f"{translation_id}::subject", "source_value": "Hi", "translations": {}}],
        ))

    respx.get(url__regex=rf"{KLAVIYO_BASE}/api/translations/[^/]+/").mock(side_effect=get_translation)

    result = runner.invoke(cli, ["push", *[arg for tid in IDS for arg in ("--id", tid)], "--dry-run"])
    assert result.exit_code == 0, result.output
    printed = [line[:-1] for line in result.output.splitlines() if line.startswith("campaign-variation::") and line.endswith(":")]
    assert printed == IDS


@pytest.mark.parametrize("value", [0, -1, 33, "8", True])
def test_concurrency_must_be_a_bounded_integer(project, value):
    config = project / "config.yaml"
    config.write_text(config.read_text().replace("klaviyo:\n", f"klaviyo:\n  concurrency: {value!r}\n", 1))
    with pytest.raises(ConfigError, match="klaviyo.concurrency"):
        load_config("config.yaml")
