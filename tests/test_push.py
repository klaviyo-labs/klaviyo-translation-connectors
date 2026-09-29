import json

import httpx
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.state import State

from conftest import (
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
    mock_smartling_no_job_found,
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
    mock_smartling_no_job_found(respx)
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
    raw_file_bytes = fields["file"][0]
    file_content = json.loads(raw_file_bytes)
    assert file_content == {
        "smartling": {
            "string_format_paths": "html: *",
            "placeholder_format_custom": [r"\{\{[^}]+\}\}", r"\{%[^%]+%\}"],
        },
        f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}",
        f"{TRANSLATION_ID}::body": "Welcome!",
    }
    # "smartling" must be the first key on the wire, not just present somewhere.
    assert raw_file_bytes.lstrip().startswith(b'{"smartling"')
    assert isinstance(file_content["smartling"]["placeholder_format_custom"], list)
    assert fields["fileUri"][0].decode() == FILE_NAME
    assert fields["fileType"][0].decode() == "json"
    assert sorted(v.decode() for v in fields["localeIdsToAuthorize[]"]) == ["es-ES", "fr-FR"]

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["translationJobUid"] == "job-1"
    assert batch_body["fileUris"] == [FILE_NAME]

    assert status_route.calls.call_count == 1

    state = State(".klaviyo-tc/state.db")
    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation is not None
    assert generation["status"] == "submitted"

    job_body = json.loads(job_route.calls.last.request.content)
    assert job_body["jobName"] == f"klaviyo {TRANSLATION_ID} {generation['id'][:8]}"
    assert sorted(job_body["targetLocaleIds"]) == ["es-ES", "fr-FR"]
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
def test_push_resume_after_batch_creation_fails_reuses_the_same_job(project, runner):
    """Job creation is checkpointed immediately, so a later batch failure never re-creates the job."""
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    mock_smartling_auth(respx)
    job_search_route = mock_smartling_no_job_found(respx)
    job_route = respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").mock(
        side_effect=[
            httpx.Response(200, json={"response": {"code": "VALIDATION_ERROR", "errors": ["boom"]}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}),
        ]
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    first = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert first.exit_code != 0
    assert job_route.calls.call_count == 1
    assert job_search_route.calls.call_count == 1

    second = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert second.exit_code == 0, second.output

    # The job was created only once, on the first (failed) attempt.
    assert job_route.calls.call_count == 1
    assert job_search_route.calls.call_count == 1
    assert batch_route.calls.call_count == 2

    final_batch_body = json.loads(batch_route.calls[-1].request.content)
    assert final_batch_body["translationJobUid"] == "job-1"

    state = State(".klaviyo-tc/state.db")
    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation["provider_state"]["job_uid"] == "job-1"
    assert generation["provider_state"]["batch_uid"] == "batch-1"


@respx.mock
def test_push_adopts_existing_job_found_by_name_instead_of_creating_a_new_one(project, runner):
    """Defends the narrow race where a job was created remotely but the local checkpoint was lost."""
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    mock_smartling_auth(respx)

    state = State(".klaviyo-tc/state.db")
    generation_id = state.create_generation(
        TRANSLATION_ID,
        FILE_NAME,
        {f"{TRANSLATION_ID}::subject": "Hello {{ first_name }}", f"{TRANSLATION_ID}::body": "Welcome!"},
        {"fr": "fr-FR", "es": "es-ES"},
    )
    expected_job_name = f"klaviyo {TRANSLATION_ID} {generation_id[:8]}"

    job_search_route = respx.get(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={
            "response": {
                "code": "SUCCESS",
                "data": {"items": [{"jobName": expected_job_name, "translationJobUid": "adopted-job"}]},
            }
        }
    )
    # No job-creation route registered: creating a new job would fail respx matching.
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
    assert job_search_route.calls.call_count == 1

    batch_body = json.loads(batch_route.calls.last.request.content)
    assert batch_body["translationJobUid"] == "adopted-job"

    generation = state.get_generation(generation_id)
    assert generation["provider_state"]["job_uid"] == "adopted-job"


@respx.mock
def test_push_retry_after_source_change_uploads_new_source_and_updates_snapshot(project, runner):
    """A retry after the source changed must upload and record the new source, not the old one."""
    subject_key = f"{TRANSLATION_ID}::subject"
    payload_a = {
        "data": {
            "type": "translation",
            "id": TRANSLATION_ID,
            "attributes": {
                "source_locale": "en",
                "target_locales": ["fr"],
                "fallback_locale": "en",
                "channel": "email",
                "values": [{"id": subject_key, "source_value": "Source A", "translations": {}}],
            },
        }
    }
    payload_b = {
        "data": {
            "type": "translation",
            "id": TRANSLATION_ID,
            "attributes": {
                "source_locale": "en",
                "target_locales": ["fr"],
                "fallback_locale": "en",
                "channel": "email",
                "values": [{"id": subject_key, "source_value": "Source B", "translations": {}}],
            },
        }
    }
    get_route = respx.get(f"https://a.klaviyo.com/api/translations/{TRANSLATION_ID}/").mock(
        side_effect=[httpx.Response(200, json=payload_a), httpx.Response(200, json=payload_b)]
    )
    mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)
    respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").mock(
        side_effect=[
            httpx.Response(200, json={"response": {"code": "VALIDATION_ERROR", "errors": ["boom"]}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}),
        ]
    )
    upload_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    first = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert first.exit_code != 0

    second = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert second.exit_code == 0, second.output
    assert get_route.calls.call_count == 2

    fields = parse_multipart(upload_route.calls.last.request)
    uploaded = json.loads(fields["file"][0])
    assert uploaded[subject_key] == "Source B"

    state = State(".klaviyo-tc/state.db")
    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation["sent_snapshot"] == {subject_key: "Source B"}


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


@respx.mock
def test_push_succeeds_when_upload_responds_202_accepted(project, runner):
    # Smartling's batch-file upload can return HTTP 202 with response.code=ACCEPTED.
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)
    respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
        json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}
    )
    upload_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        status_code=202, json={"response": {"code": "ACCEPTED", "data": {}}}
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    assert upload_route.calls.call_count == 1


@respx.mock
def test_push_fails_when_batch_completes_with_general_errors(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
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
        json={
            "response": {
                "code": "SUCCESS",
                "data": {"status": "COMPLETED", "generalErrors": ["quota exceeded"], "files": []},
            }
        }
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "quota exceeded" in result.output

    state = State(".klaviyo-tc/state.db")
    assert state.get_active_generation(TRANSLATION_ID) is None


@respx.mock
def test_push_fails_when_batch_completes_with_a_failed_file(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
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
        json={
            "response": {
                "code": "SUCCESS",
                "data": {
                    "status": "COMPLETED",
                    "generalErrors": [],
                    "files": [{"fileUri": FILE_NAME, "status": "FAILED", "errors": ["bad format"]}],
                },
            }
        }
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "bad format" in result.output

    state = State(".klaviyo-tc/state.db")
    assert state.get_active_generation(TRANSLATION_ID) is None
