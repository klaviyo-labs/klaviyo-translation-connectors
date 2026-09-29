import httpx
import respx

from klaviyo_tc.cli import cli

from conftest import (
    KLAVIYO_BASE,
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
    mock_smartling_no_job_found,
)

TRANSLATION_ID_1 = "campaign-variation::email::one"
TRANSLATION_ID_2 = "campaign-variation::email::two"


def test_init_writes_template_and_is_idempotent(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    config_path = tmp_path / "config.yaml"
    assert config_path.exists()
    original = config_path.read_text()

    result_again = runner.invoke(cli, ["init"])
    assert result_again.exit_code == 0
    assert "already exists" in result_again.output
    assert config_path.read_text() == original


def test_status_with_no_translations_tracked(project, runner):
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "no translations tracked yet" in result.output


def test_push_requires_a_scope_selector(project, runner):
    result = runner.invoke(cli, ["push"])
    assert result.exit_code != 0
    assert "--id, --all, --campaign/--flow, or --tag" in result.output


def test_push_id_and_all_are_mutually_exclusive(project, runner):
    result = runner.invoke(cli, ["push", "--id", "x", "--all"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


@respx.mock
def test_push_all_uses_channel_filter_and_lists_cursor_pages(project, runner):
    next_url = f"{KLAVIYO_BASE}/api/translations/?page%5Bcursor%5D=abc"
    list_route = respx.get(f"{KLAVIYO_BASE}/api/translations/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"type": "translation", "id": TRANSLATION_ID_1}],
                    "links": {"next": next_url},
                },
            ),
            httpx.Response(
                200,
                json={"data": [{"type": "translation", "id": TRANSLATION_ID_2}], "links": {"next": None}},
            ),
        ]
    )
    values_1 = [{"id": f"{TRANSLATION_ID_1}::subject", "source_value": "Hi", "translations": {}}]
    values_2 = [{"id": f"{TRANSLATION_ID_2}::subject", "source_value": "Hey", "translations": {}}]
    mock_klaviyo_get_translation(respx, TRANSLATION_ID_1, target_locales=["fr"], values=values_1)
    mock_klaviyo_get_translation(respx, TRANSLATION_ID_2, target_locales=["fr"], values=values_2)
    mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)
    respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
        json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    result = runner.invoke(cli, ["push", "--all", "--channel", "email"])
    assert result.exit_code == 0, result.output
    assert "submitted files: 2" in result.output
    assert "Klaviyo bulk" in result.output
    assert list_route.calls.call_count == 2

    from klaviyo_tc.core.state import State

    state = State(".klaviyo-tc/state.db")
    assert state.get_active_generation(TRANSLATION_ID_1) is not None
    assert state.get_active_generation(TRANSLATION_ID_2) is not None

    import urllib.parse

    first_request = list_route.calls[0].request
    assert 'equals(channel,"email")' in urllib.parse.unquote(str(first_request.url))
