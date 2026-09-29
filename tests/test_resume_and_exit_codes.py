"""State upgrades, resume after source edits or scope changes, and failure exit codes."""
import json
import sqlite3

import httpx
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.state import State
from klaviyo_tc.providers.base import FileSpec
from klaviyo_tc.providers.smartling.provider import SmartlingProvider

from conftest import (
    KLAVIYO_BASE,
    PLACEHOLDER_FORMAT_CUSTOM,
    PROJECT_ID,
    SMARTLING_BASE,
    klaviyo_translation_payload,
    mock_klaviyo_find_translation_for_resource,
    mock_klaviyo_flow_actions,
    mock_klaviyo_flow_messages,
    mock_klaviyo_get_flow,
    mock_smartling_auth,
    mock_smartling_no_job_found,
    parse_multipart,
)

TRANSLATION_ID = "campaign-variation::email::t1"
FILE_NAME = f"klaviyo/{TRANSLATION_ID.replace('::', '__')}.json"


def test_state_opens_a_database_created_before_runs_existed(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE generations (
            id TEXT PRIMARY KEY, translation_id TEXT NOT NULL, file_name TEXT NOT NULL,
            sent_snapshot TEXT NOT NULL, locales TEXT NOT NULL, status TEXT NOT NULL,
            provider_state TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        """
    )
    conn.close()

    state = State(str(path))
    generation_id = state.create_generation(TRANSLATION_ID, FILE_NAME, {"k": "v"}, {"fr": "fr-FR"}, run_id="run-1")
    generation = state.get_generation(generation_id)
    assert generation["run_id"] == "run-1"
    assert generation["baseline"] == {}


def _translation(source_value):
    return httpx.Response(
        200,
        json=klaviyo_translation_payload(
            TRANSLATION_ID, target_locales=["fr"],
            values=[{"id": f"{TRANSLATION_ID}::subject", "source_value": source_value, "translations": {}}],
        ),
    )


@respx.mock
def test_resume_reuploads_a_file_whose_source_changed_after_upload(project, runner):
    respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").mock(
        side_effect=[_translation("Hi"), _translation("Hello")]
    )
    mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)
    respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").mock(
        side_effect=[
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-2"}}}),
        ]
    )
    upload_1 = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    upload_2 = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-2/file").respond(
        json={"response": {"code": "SUCCESS", "data": {}}}
    )
    # The first run crashes while polling, after its upload landed.
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-1").mock(
        side_effect=[
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"status": "FAILED"}}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}),
        ]
    )
    respx.get(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/batch-2").respond(
        json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}}
    )

    assert runner.invoke(cli, ["push", "--id", TRANSLATION_ID]).exit_code != 0
    second = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert second.exit_code == 0, second.output

    assert batch_route.calls.call_count == 2
    assert upload_1.calls.call_count == 1
    assert upload_2.calls.call_count == 1
    uploaded = json.loads(parse_multipart(upload_2.calls.last.request)["file"][0])
    assert uploaded[f"{TRANSLATION_ID}::subject"] == "Hello"

    generation = State(".klaviyo-tc/state.db").get_active_generation(TRANSLATION_ID)
    assert generation["sent_snapshot"] == {f"{TRANSLATION_ID}::subject": "Hello"}


class _RecordingClient:
    def __init__(self):
        self.uploads = []

    def upload_file(self, batch_uid, file_uri, content, locale_ids, authorize=True):
        self.uploads.append((batch_uid, file_uri))

    def poll_batch(self, batch_uid):
        pass


def test_resume_skips_a_batched_file_that_left_the_scope():
    provider = SmartlingProvider(
        base_url=SMARTLING_BASE, project_id=PROJECT_ID, user_identifier="u", user_secret="s",
        string_format_paths="html: *", placeholder_format_custom=PLACEHOLDER_FORMAT_CUSTOM,
    )
    provider._client = _RecordingClient()
    state = {
        "job_uid": "job-1",
        "batches": [{"batch_uid": "batch-1", "file_names": ["a.json", "b.json"], "uploaded": [], "digests": {}}],
    }

    result = provider.submit_many(
        run_ref="run-1", job_name="job", files=[FileSpec("a", "a.json", {"k": "v"}, ["fr-FR"])], state=state
    )

    assert result.submitted
    assert provider._client.uploads == [("batch-1", "a.json")]


def test_provider_rejects_a_non_positive_batch_size():
    try:
        SmartlingProvider(
            base_url=SMARTLING_BASE, project_id=PROJECT_ID, user_identifier="u", user_secret="s",
            string_format_paths="html: *", placeholder_format_custom=PLACEHOLDER_FORMAT_CUSTOM, files_per_batch=-1,
        )
    except ValueError as exc:
        assert "files_per_batch" in str(exc)
    else:
        raise AssertionError("expected ValueError")


@respx.mock
def test_push_exits_nonzero_when_create_missing_fails(project, runner):
    mock_klaviyo_get_flow(respx, "flow-1", "Welcome Series")
    mock_klaviyo_flow_actions(respx, "flow-1", [{"id": "action-1"}])
    mock_klaviyo_flow_messages(respx, "action-1", [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_find_translation_for_resource(respx, "msg-1", translation_id=None)
    respx.post(f"{KLAVIYO_BASE}/api/translations/").respond(status_code=400, json={"errors": [{"detail": "bad"}]})

    result = runner.invoke(cli, ["push", "--flow", "flow-1", "--create-missing"])
    assert result.exit_code != 0
    assert "errors: 1" in result.output


@respx.mock
def test_dry_run_exits_nonzero_when_a_translation_cannot_be_read(project, runner):
    respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        status_code=404, json={"errors": [{"detail": "not found"}]}
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID, "--dry-run"])
    assert result.exit_code != 0
