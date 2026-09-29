import json

import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.state import State

from conftest import (
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
    parse_multipart,
)

TRANSLATION_ID = "campaign-variation::email::01K1EXAMPLE"
FILE_NAME = f"klaviyo/{TRANSLATION_ID.replace('::', '__')}.json"

VALUES = [
    {"id": f"{TRANSLATION_ID}::subject", "source_value": "Hello {{ first_name }}", "translations": {}},
    {"id": f"{TRANSLATION_ID}::preheader", "source_value": "", "translations": {}},
    {"id": f"{TRANSLATION_ID}::body", "source_value": "Welcome!", "translations": {}},
]


def _mock_job_batch_upload_poll(job_uid="job-1", batch_uid="batch-1"):
    job_route = respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": job_uid}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
        json={"response": {"code": "SUCCESS", "data": {"batchUid": batch_uid}}}
    )
    upload_route = respx.post(
        f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/{batch_uid}/file"
    ).respond(json={"response": {"code": "SUCCESS", "data": {}}})
    status_route = respx.get(
        f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/{batch_uid}"
    ).respond(json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}})
    return job_route, batch_route, upload_route, status_route


@respx.mock
def test_push_builds_exact_file_and_submits(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output

    fields = parse_multipart(upload_route.calls.last.request)
    file_content = json.loads(fields["file"][0])
    assert file_content == {
        "smartling": {
            "string_format_paths": "html: *",
            "placeholder_format_custom": r"\{\{[^}]+\}\}|\{%[^%]+%\}",
        },
        f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}",
        f"{TRANSLATION_ID}::body": "Welcome!",
    }
    assert fields["fileUri"][0].decode() == FILE_NAME
    assert fields["fileType"][0].decode() == "json"
    assert sorted(v.decode() for v in fields["localeIdsToAuthorize[]"]) == ["es-ES", "fr-FR"]

    job_body = json.loads(job_route.calls.last.request.content)
    assert job_body["jobName"] == f"klaviyo {TRANSLATION_ID}"
    assert sorted(job_body["targetLocaleIds"]) == ["es-ES", "fr-FR"]

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["translationJobUid"] == "job-1"
    assert batch_body["fileUris"] == [FILE_NAME]

    assert status_route.calls.call_count == 1

    state = State(".klaviyo-tc/state.db")
    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation is not None
    assert generation["status"] == "submitted"
    assert job_body["referenceNumber"] == generation["id"]
    assert generation["sent_snapshot"] == {
        f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}",
        f"{TRANSLATION_ID}::body": "Welcome!",
    }


@respx.mock
def test_push_dry_run_makes_no_writes(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    # No Smartling routes registered at all: any call to Smartling fails the test.

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--dry-run"])
    assert result.exit_code == 0, result.output

    printed = json.loads(result.output)
    assert printed == {
        f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}",
        f"{TRANSLATION_ID}::body": "Welcome!",
    }
    import os

    assert not os.path.exists(".klaviyo-tc/state.db")


@respx.mock
def test_push_warns_and_skips_locale_not_enabled_on_translation(project, runner):
    # Translation only enables fr; the project also configures es.
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    assert "locale 'es' not enabled" in result.output

    job_body = json.loads(job_route.calls.last.request.content)
    assert job_body["targetLocaleIds"] == ["fr-FR"]


@respx.mock
def test_push_resume_after_crash_reuses_existing_job(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    mock_smartling_auth(respx)

    # Simulate a crash after job creation but before the batch was created.
    state = State(".klaviyo-tc/state.db")
    generation_id = state.create_generation(
        TRANSLATION_ID,
        FILE_NAME,
        {f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}", f"{TRANSLATION_ID}::body": "Welcome!"},
        {"fr": "fr-FR", "es": "es-ES"},
    )
    state.set_provider_state(generation_id, {"job_uid": "existing-job"})

    # No job-creation route registered: a new job call would fail respx matching.
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
        json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["translationJobUid"] == "existing-job"

    generation = state.get_generation(generation_id)
    assert generation["status"] == "submitted"
    assert generation["provider_state"]["job_uid"] == "existing-job"
    assert generation["provider_state"]["batch_uid"] == "batch-1"


@respx.mock
def test_push_all_error_on_one_translation_still_processes_others_and_exits_nonzero(project, runner):
    ok_id = "campaign-variation::email::ok"
    bad_id = "campaign-variation::email::bad"
    mock_klaviyo_get_translation(respx, ok_id, target_locales=["fr"], values=VALUES)
    respx.get(f"https://a.klaviyo.com/api/translations/{bad_id}/").respond(status_code=500, text="boom")
    mock_smartling_auth(respx)
    _mock_job_batch_upload_poll()

    result = runner.invoke(cli, ["push", "--id", bad_id, "--id", ok_id])
    assert result.exit_code != 0

    state = State(".klaviyo-tc/state.db")
    assert state.get_active_generation(ok_id) is not None
    assert state.get_active_generation(bad_id) is None
