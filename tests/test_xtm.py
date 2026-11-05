"""XTM provider driven through the CLI against respx-mocked XTM REST endpoints.

Mock shapes follow the XTM REST API spec (v26.4):
https://api.xtm-cloud.com/project-manager-api-rest/documentation/xtm-docs
"""
import io
import json
import zipfile

import httpx
import pytest
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.config import ConfigError, load_config
from klaviyo_tc.core.state import State
from klaviyo_tc.providers.base import FileSpec
from klaviyo_tc.providers.xtm.client import XtmError, extract_json_from_zip
from klaviyo_tc.providers.xtm.provider import XtmProvider, project_name_for, xtm_file_name_for

from conftest import (
    mock_klaviyo_get_translation,
    mock_klaviyo_patch_translation,
    parse_multipart,
)

XTM_BASE = "https://xtm.example.com/project-manager-api-rest"
CUSTOMER_ID = 42
PROJECT = 9001

XTM_CONFIG_YAML = f"""klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
  source_locale: en
  fallback_locale: en

provider:
  name: xtm

providers:
  xtm:
    base_url: {XTM_BASE}
    customer_id: {CUSTOMER_ID}
    source_language: en_US
    workflow_name: translate
    files_per_upload: 50

locales:
  fr: fr_FR
  es: es_ES

state:
  path: .klaviyo-tc/state.db
"""

SENTINEL_XTM_CLIENT = "sentinel-xtm-client"
SENTINEL_XTM_PASSWORD = "sentinel-xtm-password-do-not-leak"
SENTINEL_XTM_TOKEN = "sentinel-xtm-token-do-not-leak"

TRANSLATION_ID = "campaign-variation::email::01K1EXAMPLE"
FILE_NAME = f"klaviyo/{TRANSLATION_ID.replace('::', '__')}.json"
XTM_FILE = xtm_file_name_for(FILE_NAME)
SUBJECT_KEY = f"{TRANSLATION_ID}::subject"
VALUES = [{"id": SUBJECT_KEY, "source_value": "Hello {{ first_name }}", "translations": {}}]


@pytest.fixture
def xtm_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(XTM_CONFIG_YAML)
    monkeypatch.setenv("KLAVIYO_API_KEY", "sentinel-klaviyo-key-do-not-leak")
    monkeypatch.setenv("XTM_CLIENT", SENTINEL_XTM_CLIENT)
    monkeypatch.setenv("XTM_USER_ID", "77")
    monkeypatch.setenv("XTM_PASSWORD", SENTINEL_XTM_PASSWORD)
    monkeypatch.setattr("klaviyo_tc.providers.xtm.client.time.sleep", lambda _s: None)
    return tmp_path


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("klaviyo_tc.providers.xtm.client.time.sleep", lambda _s: None)


def mock_auth(token=SENTINEL_XTM_TOKEN):
    return respx.post(f"{XTM_BASE}/auth/token").respond(json={"token": token})


def mock_no_existing_project():
    return respx.get(f"{XTM_BASE}/projects").respond(json=[])


def mock_create_project(jobs):
    return respx.post(f"{XTM_BASE}/projects").respond(
        status_code=201, json={"projectId": PROJECT, "name": "n", "jobs": jobs}
    )


def mock_analysis(*statuses):
    responses = [
        httpx.Response(
            200, json={"projectId": PROJECT, "status": s, "jobs": [{"jobId": 1, "fileName": XTM_FILE, "status": s}]}
        )
        for s in statuses
    ]
    return respx.get(f"{XTM_BASE}/projects/{PROJECT}/analysis").mock(side_effect=responses)


def mock_analysis_finished():
    return respx.get(f"{XTM_BASE}/projects/{PROJECT}/analysis").respond(json={"status": "FINISHED", "jobs": []})


def status_payload(jobs):
    return {
        "projectId": PROJECT,
        "completionStatus": "IN_PROGRESS",
        "jobs": [
            {"jobId": job_id, "fileName": name, "targetLanguage": lang, "completionStatus": status}
            for job_id, name, lang, status in jobs
        ],
    }


def mock_status(jobs):
    return respx.get(f"{XTM_BASE}/projects/{PROJECT}/status").respond(json=status_payload(jobs))


def zip_of(name, payload: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"fr_FR/{name}", json.dumps(payload))
    return buffer.getvalue()


def _push_happy_path():
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    auth = mock_auth()
    mock_no_existing_project()
    create = mock_create_project([{"jobId": 1, "fileName": XTM_FILE, "targetLanguage": "fr_FR"}])
    mock_analysis("IN_PROGRESS", "FINISHED")
    mock_status([(1, XTM_FILE, "fr_FR", "IN_PROGRESS"), (2, XTM_FILE, "es_ES", "IN_PROGRESS")])
    return auth, create


def _provider(**overrides):
    kwargs = dict(
        base_url=XTM_BASE, client_name="c", user_id=1, password="p", customer_id=CUSTOMER_ID,
        source_language="en_US", workflow_name="translate", files_per_upload=1,
    )
    kwargs.update(overrides)
    return XtmProvider(**kwargs)


# -- Push ------------------------------------------------------------------------------


@respx.mock
def test_push_creates_one_project_with_documented_multipart_fields(xtm_project, runner):
    auth, create = _push_happy_path()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output

    assert json.loads(auth.calls.last.request.content) == {
        "client": SENTINEL_XTM_CLIENT, "userId": 77, "password": SENTINEL_XTM_PASSWORD,
    }
    request = create.calls.last.request
    assert request.headers["authorization"] == f"XTM-Basic {SENTINEL_XTM_TOKEN}"
    fields = parse_multipart(request)
    assert fields["customerId"] == [str(CUSTOMER_ID).encode()]
    assert fields["sourceLanguage"] == [b"en_US"]
    assert (fields["targetLanguages[0]"], fields["targetLanguages[1]"]) == ([b"es_ES"], [b"fr_FR"])
    assert fields["workflowName"] == [b"translate"]
    assert b"/" not in fields["name"][0] and b":" not in fields["name"][0]
    assert json.loads(fields["translationFiles[0].file"][0]) == {SUBJECT_KEY: "Hello {{ first_name }}"}
    # Every language the project has, so no per-file override is sent.
    assert not any(key.startswith("translationFiles[0].targetLanguages") for key in fields)

    state = State(".klaviyo-tc/state.db")
    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation["provider_state"] == {
        "project_id": PROJECT, "xtm_file_name": XTM_FILE, "jobs": {"fr_FR": 1, "es_ES": 2},
    }


@respx.mock
def test_push_with_template_sends_template_id_and_no_workflow(xtm_project, runner):
    (xtm_project / "config.yaml").write_text(
        XTM_CONFIG_YAML.replace("    source_language: en_US\n    workflow_name: translate\n", "    template_id: 5\n")
    )
    _auth, create = _push_happy_path()

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    fields = parse_multipart(create.calls.last.request)
    assert fields["templateId"] == [b"5"]
    assert "workflowName" not in fields and "workflowId" not in fields and "sourceLanguage" not in fields


@respx.mock
def test_push_starts_workflow_only_when_configured_and_not_with_no_authorize(xtm_project, runner):
    (xtm_project / "config.yaml").write_text(
        XTM_CONFIG_YAML.replace("files_per_upload: 50", "files_per_upload: 50\n    start_workflow: true")
    )
    _push_happy_path()
    start = respx.post(f"{XTM_BASE}/projects/{PROJECT}/workflow/start").respond(
        json={"projectId": PROJECT, "status": True}
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--no-authorize", "--force-resend"])
    assert result.exit_code == 0, result.output
    assert start.calls.call_count == 0

    mock_analysis("FINISHED")
    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--force-resend"])
    assert result.exit_code == 0, result.output
    assert start.calls.call_count == 1


@respx.mock
def test_push_fails_when_xtm_cannot_start_the_workflow(xtm_project, runner):
    (xtm_project / "config.yaml").write_text(
        XTM_CONFIG_YAML.replace("files_per_upload: 50", "files_per_upload: 50\n    start_workflow: true")
    )
    _push_happy_path()
    respx.post(f"{XTM_BASE}/projects/{PROJECT}/workflow/start").respond(
        json={"projectId": PROJECT, "status": False, "errorType": "COST_NOT_APPROVED"}
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "COST_NOT_APPROVED" in result.output


@respx.mock
def test_expired_token_triggers_one_reauth_and_retry(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    auth = respx.post(f"{XTM_BASE}/auth/token").mock(
        side_effect=[httpx.Response(200, json={"token": "t1"}), httpx.Response(200, json={"token": "t2"})]
    )
    find = respx.get(f"{XTM_BASE}/projects").mock(
        side_effect=[httpx.Response(401, json={"reason": "Invalid token"}), httpx.Response(200, json=[])]
    )
    mock_create_project([])
    mock_analysis("FINISHED")
    mock_status([(1, XTM_FILE, "fr_FR", "IN_PROGRESS")])

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    assert auth.calls.call_count == 2
    assert find.calls[1].request.headers["authorization"] == "XTM-Basic t2"


@respx.mock
def test_retries_503_then_succeeds(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_auth()
    respx.get(f"{XTM_BASE}/projects").mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[])])
    mock_create_project([])
    mock_analysis("FINISHED")
    mock_status([(1, XTM_FILE, "fr_FR", "IN_PROGRESS")])

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output


@respx.mock
def test_analysis_error_fails_the_push_and_keeps_the_project_for_resume(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_auth()
    mock_no_existing_project()
    create = mock_create_project([])
    mock_analysis("ERROR")

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "analysis failed" in result.output

    # Resume: no second project, no re-upload of unchanged content, only the analysis is retried.
    mock_analysis("FINISHED")
    mock_status([(1, XTM_FILE, "fr_FR", "IN_PROGRESS")])
    upload = respx.post(f"{XTM_BASE}/projects/{PROJECT}/files/sources/upload").respond(json={"projectId": PROJECT})
    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    assert create.calls.call_count == 1
    assert upload.calls.call_count == 0


@respx.mock
def test_resume_reuploads_a_file_whose_source_changed_with_match_names(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_auth()
    mock_no_existing_project()
    mock_create_project([])
    mock_analysis("ERROR")
    assert runner.invoke(cli, ["push", "--id", TRANSLATION_ID]).exit_code != 0

    changed = [{"id": SUBJECT_KEY, "source_value": "Hi {{ first_name }}", "translations": {}}]
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=changed)
    upload = respx.post(f"{XTM_BASE}/projects/{PROJECT}/files/sources/upload").respond(json={"projectId": PROJECT})
    mock_analysis("FINISHED")
    mock_status([(1, XTM_FILE, "fr_FR", "IN_PROGRESS")])

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code == 0, result.output
    request = upload.calls.last.request
    assert request.url.params["matchType"] == "MATCH_NAMES"
    assert request.url.params["reanalyseProject"] == "YES"
    assert json.loads(parse_multipart(request)["files[0].file"][0]) == {SUBJECT_KEY: "Hi {{ first_name }}"}


@respx.mock
def test_crash_after_create_adopts_the_project_by_name_and_resends_files(no_sleep):
    mock_auth()
    name = project_name_for("Klaviyo: tag X abcd1234")
    respx.get(f"{XTM_BASE}/projects").respond(json=[{"id": PROJECT, "name": name}, {"id": 1, "name": name + " 2"}])
    create = respx.post(f"{XTM_BASE}/projects").respond(status_code=201, json={"projectId": 1})
    upload = respx.post(f"{XTM_BASE}/projects/{PROJECT}/files/sources/upload").respond(json={"projectId": PROJECT})
    mock_analysis_finished()
    respx.get(f"{XTM_BASE}/projects/{PROJECT}/status").respond(
        json=status_payload(
            [(1, "klaviyo__a.json", "fr_FR", "IN_PROGRESS"), (2, "klaviyo__b.json", "fr_FR", "IN_PROGRESS")]
        )
    )
    checkpoints = []
    files = [
        FileSpec("a", "klaviyo/a.json", {"k": "v"}, ["fr_FR"]),
        FileSpec("b", "klaviyo/b.json", {"k": "w"}, ["fr_FR"]),
    ]

    result = _provider().submit_many(
        run_ref="run", job_name="Klaviyo: tag X abcd1234", files=files, checkpoint=checkpoints.append
    )

    assert create.calls.call_count == 0
    assert upload.calls.call_count == 2  # files_per_upload=1
    assert checkpoints[0]["project_id"] == PROJECT
    assert result.file_states["klaviyo/b.json"] == {
        "project_id": PROJECT, "xtm_file_name": "klaviyo__b.json", "jobs": {"fr_FR": 2},
    }


@respx.mock
def test_files_with_fewer_languages_send_a_per_file_override(no_sleep):
    mock_auth()
    mock_no_existing_project()
    create = respx.post(f"{XTM_BASE}/projects").respond(status_code=201, json={"projectId": PROJECT})
    mock_analysis_finished()
    respx.get(f"{XTM_BASE}/projects/{PROJECT}/status").respond(
        json=status_payload(
            [(1, "klaviyo__a.json", "fr_FR", "X"), (2, "klaviyo__a.json", "es_ES", "X"), (3, "klaviyo__b.json", "fr_FR", "X")]
        )
    )
    files = [
        FileSpec("a", "klaviyo/a.json", {"k": "v"}, ["fr_FR", "es_ES"]),
        FileSpec("b", "klaviyo/b.json", {"k": "w"}, ["fr_FR"]),
    ]

    _provider(files_per_upload=10).submit_many(run_ref="r", job_name="j", files=files)

    fields = parse_multipart(create.calls.last.request)
    assert not any(key.startswith("translationFiles[0].targetLanguages") for key in fields)
    assert fields["translationFiles[1].targetLanguages[0]"] == [b"fr_FR"]


@respx.mock
def test_push_errors_when_xtm_created_no_job_for_a_language(no_sleep):
    mock_auth()
    mock_no_existing_project()
    respx.post(f"{XTM_BASE}/projects").respond(status_code=201, json={"projectId": PROJECT})
    mock_analysis_finished()
    respx.get(f"{XTM_BASE}/projects/{PROJECT}/status").respond(
        json=status_payload([(1, "klaviyo__a.json", "fr_FR", "CANCELLED")])
    )
    with pytest.raises(XtmError, match="no job for klaviyo__a.json"):
        _provider().submit_many(
            run_ref="r", job_name="j", files=[FileSpec("a", "klaviyo/a.json", {"k": "v"}, ["fr_FR"])]
        )


@respx.mock
def test_single_submit_keeps_per_file_keys_in_its_state(no_sleep):
    mock_auth()
    mock_no_existing_project()
    create = respx.post(f"{XTM_BASE}/projects").respond(status_code=201, json={"projectId": PROJECT})
    mock_analysis_finished()
    respx.get(f"{XTM_BASE}/projects/{PROJECT}/status").respond(json=status_payload([(1, XTM_FILE, "fr_FR", "X")]))

    result = _provider().submit(
        translation_id=TRANSLATION_ID, file_name=FILE_NAME, strings={SUBJECT_KEY: "Hi"},
        target_locales=["fr_FR"], reference="0123456789abcdef",
    )

    assert result.state["project_id"] == PROJECT and result.state["jobs"] == {"fr_FR": 1}
    assert parse_multipart(create.calls.last.request)["referenceId"] == [b"0123456789abcdef"]


@respx.mock
def test_create_project_error_reason_is_reported(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_auth()
    mock_no_existing_project()
    respx.post(f"{XTM_BASE}/projects").respond(status_code=400, json={"reason": "Customer does not exist"})

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "XTM API error 400: Customer does not exist" in result.output
    assert SENTINEL_XTM_PASSWORD not in result.output


@respx.mock
def test_issued_token_echoed_in_an_error_body_is_redacted(xtm_project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    mock_auth()
    respx.get(f"{XTM_BASE}/projects").respond(status_code=400, json={"reason": f"bad token {SENTINEL_XTM_TOKEN}"})

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert SENTINEL_XTM_TOKEN not in result.output


def test_provider_requires_a_workflow_or_template():
    with pytest.raises(ValueError, match="template_id, workflow_id or workflow_name"):
        _provider(workflow_name=None)
    with pytest.raises(ValueError, match="source_language"):
        _provider(source_language=None)
    with pytest.raises(ValueError, match="files_per_upload"):
        _provider(files_per_upload=0)


def test_project_name_strips_characters_xtm_rejects():
    assert project_name_for('Klaviyo: a/b*c?"d<e>|{f}\tg') == "Klaviyo- a-b-c--d-e---f--g"


# -- Pull ------------------------------------------------------------------------------


def _seed(jobs=None):
    state = State(".klaviyo-tc/state.db")
    generation_id = state.create_generation(
        TRANSLATION_ID, FILE_NAME, {SUBJECT_KEY: "Hello {{ first_name }}"}, {"fr": "fr_FR", "es": "es_ES"}
    )
    state.set_provider_state(
        generation_id, {"project_id": PROJECT, "xtm_file_name": XTM_FILE, "jobs": jobs or {"fr_FR": 1, "es_ES": 2}}
    )
    state.set_status(generation_id, "submitted")


@respx.mock
def test_pull_generates_downloads_and_writes_only_finished_languages(xtm_project, runner):
    _seed()
    mock_auth()
    mock_status([(1, XTM_FILE, "fr_FR", "FINISHED"), (2, XTM_FILE, "es_ES", "IN_PROGRESS")])
    generate = respx.post(f"{XTM_BASE}/projects/{PROJECT}/files/generate").respond(
        json=[{"fileId": 555, "jobId": 1, "fileType": "TARGET"}]
    )
    file_status = respx.get(f"{XTM_BASE}/projects/{PROJECT}/files/status").mock(
        side_effect=[
            httpx.Response(200, json=[{"fileId": 555, "jobId": 1, "status": "IN_PROGRESS"}]),
            httpx.Response(200, json=[{"fileId": 555, "jobId": 1, "status": "FINISHED"}]),
        ]
    )
    download = respx.get(f"{XTM_BASE}/projects/{PROJECT}/files/555/download").respond(
        content=zip_of(XTM_FILE, {SUBJECT_KEY: "Bonjour {{ first_name }}"})
    )
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr", "es"], values=VALUES)
    patch = mock_klaviyo_patch_translation(respx, TRANSLATION_ID)

    result = runner.invoke(cli, ["pull", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output

    assert generate.calls.last.request.url.params["fileType"] == "TARGET"
    assert generate.calls.last.request.url.params.get_list("jobIds") == ["1"]
    assert file_status.calls.last.request.url.params["fileScope"] == "JOB"
    assert download.calls.last.request.url.params["fileScope"] == "JOB"
    body = json.loads(patch.calls.last.request.content)
    assert body["data"]["attributes"]["values"] == [
        {"id": SUBJECT_KEY, "translations": {"fr": "Bonjour {{ first_name }}"}}
    ]


@respx.mock
def test_pull_fails_that_translation_when_file_generation_errors(xtm_project, runner):
    _seed({"fr_FR": 1})
    mock_auth()
    mock_status([(1, XTM_FILE, "fr_FR", "FINISHED")])
    respx.post(f"{XTM_BASE}/projects/{PROJECT}/files/generate").respond(json=[{"fileId": 555, "jobId": 1}])
    respx.get(f"{XTM_BASE}/projects/{PROJECT}/files/status").respond(
        json=[{"fileId": 555, "status": "ERROR", "message": "filter failed"}]
    )
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code != 0
    assert "filter failed" in result.output


def test_extract_json_from_zip_matches_by_basename_or_single_json():
    assert extract_json_from_zip(zip_of("a.json", {"k": "v"}), "a.json") == {"k": "v"}
    assert extract_json_from_zip(zip_of("renamed.json", {"k": "v"}), "a.json") == {"k": "v"}
    with pytest.raises(XtmError, match="not a ZIP"):
        extract_json_from_zip(b"{}", "a.json")
