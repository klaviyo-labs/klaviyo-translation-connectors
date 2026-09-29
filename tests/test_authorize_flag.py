"""providers.smartling.authorize and --no-authorize: authorized vs. unauthorized job batches."""
import json

import respx

from klaviyo_tc.cli import cli

from conftest import (
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
    mock_smartling_no_job_found,
    parse_multipart,
)

TRANSLATION_ID = "campaign-variation::email::01K1AUTH"
FILE_NAME = f"klaviyo/{TRANSLATION_ID.replace('::', '__')}.json"
VALUES = [{"id": f"{TRANSLATION_ID}::subject", "source_value": "Hi", "translations": {}}]


def _mock_job_batch_upload_poll():
    mock_smartling_no_job_found(respx)
    respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
        json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}
    )
    upload_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )
    return batch_route, upload_route


@respx.mock
def test_push_authorizes_the_batch_by_default(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    batch_route, upload_route = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["authorize"] is True

    fields = parse_multipart(upload_route.calls.last.request)
    assert "localeIdsToAuthorize[]" in fields


@respx.mock
def test_push_no_authorize_creates_unauthorized_batch_and_omits_locales(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    batch_route, upload_route = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--no-authorize"])
    assert result.exit_code == 0, result.output

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["authorize"] is False

    fields = parse_multipart(upload_route.calls.last.request)
    assert "localeIdsToAuthorize[]" not in fields


def _use_workflow(tmp_path, workflow_uid):
    config = tmp_path / "config.yaml"
    config.write_text(
        config.read_text().replace("  smartling:\n", f'  smartling:\n    workflow_uid: "{workflow_uid}"\n', 1)
    )


@respx.mock
def test_push_authorizes_into_the_configured_workflow(project, runner):
    _use_workflow(project, "abc123workflow")
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    batch_route, _ = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["localeWorkflows"] == [{"targetLocaleId": "fr-FR", "workflowUid": "abc123workflow"}]


@respx.mock
def test_no_authorize_ignores_the_configured_workflow(project, runner):
    _use_workflow(project, "abc123workflow")
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    batch_route, _ = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--no-authorize"])
    assert result.exit_code == 0, result.output

    assert "localeWorkflows" not in json.loads(batch_route.calls.last.request.content)
