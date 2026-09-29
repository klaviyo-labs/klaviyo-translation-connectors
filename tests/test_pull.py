import json

import httpx
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.state import State

from conftest import KLAVIYO_BASE, PROJECT_ID, SMARTLING_BASE, klaviyo_translation_payload, mock_smartling_auth

TRANSLATION_ID = "campaign-variation::email::01K1EXAMPLE"
FILE_NAME = f"klaviyo/{TRANSLATION_ID.replace('::', '__')}.json"
SUBJECT_KEY = f"{TRANSLATION_ID}::subject"
BODY_KEY = f"{TRANSLATION_ID}::body"


def _seed_submitted_generation(sent_snapshot, locales=None):
    state = State(".klaviyo-tc/state.db")
    generation_id = state.create_generation(TRANSLATION_ID, FILE_NAME, sent_snapshot, locales or {"fr": "fr-FR"})
    state.set_provider_state(generation_id, {"job_uid": "job-1", "batch_uid": "batch-1", "file_uri": FILE_NAME})
    state.set_status(generation_id, "submitted")
    return state, generation_id


def _mock_file_status(locales_completed):
    items = [{"localeId": locale, "completedStringCount": count} for locale, count in locales_completed.items()]
    return respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/file/status").respond(
        json={"response": {"code": "SUCCESS", "data": {"items": items, "totalStringCount": len(items)}}}
    )


def _mock_download(locale, payload: dict):
    return respx.get(
        f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/locales/{locale}/file"
    ).respond(json=payload)


def _mock_klaviyo_current_values(values):
    return respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={
            "data": {
                "type": "translation",
                "id": TRANSLATION_ID,
                "attributes": {
                    "source_locale": "en",
                    "target_locales": ["fr"],
                    "fallback_locale": "en",
                    "channel": "email",
                    "values": values,
                },
            }
        }
    )


@respx.mock
def test_pull_writes_translation_via_patch(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello {{ first_name }}"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour {{ first_name }}"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello {{ first_name }}", "translations": {}}])
    patch_route = respx.patch(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}}
    )

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output

    body = json.loads(patch_route.calls.last.request.content)
    assert body == {
        "data": {
            "type": "translation",
            "id": TRANSLATION_ID,
            "attributes": {"values": [{"id": SUBJECT_KEY, "translations": {"fr": "Bonjour {{ first_name }}"}}]},
        }
    }

    state = State(".klaviyo-tc/state.db")
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") == "Bonjour {{ first_name }}"


@respx.mock
def test_pull_chunks_patch_requests_at_100(project, runner):
    keys = [f"{TRANSLATION_ID}::v{i}" for i in range(150)]
    sent_snapshot = {k: f"source {i}" for i, k in enumerate(keys)}
    downloaded = {k: f"translated {i}" for i, k in enumerate(keys)}
    current_values = [{"id": k, "source_value": f"source {i}", "translations": {}} for i, k in enumerate(keys)]

    _seed_submitted_generation(sent_snapshot)
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 150})
    _mock_download("fr-FR", {"smartling": {}, **downloaded})
    _mock_klaviyo_current_values(current_values)
    patch_route = respx.patch(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}}
    )

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert patch_route.calls.call_count == 2

    sizes = sorted(len(json.loads(call.request.content)["data"]["attributes"]["values"]) for call in patch_route.calls)
    assert sizes == [50, 100]


@respx.mock
def test_pull_dry_run_makes_no_writes(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello {{ first_name }}"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour {{ first_name }}"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello {{ first_name }}", "translations": {}}])
    # No PATCH route registered: a PATCH call here would fail respx matching.

    result = runner.invoke(cli, ["pull", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output

    state = State(".klaviyo-tc/state.db")
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") is None
    assert state.get_pull_status(state.get_active_generation(TRANSLATION_ID)["id"]) == []


@respx.mock
def test_pull_stale_source_is_skipped(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello {{ first_name }}"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour {{ first_name }}"})
    # Source changed in Klaviyo since the push.
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hi {{ first_name }}", "translations": {}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "stale_source=1" in result.output


@respx.mock
def test_pull_placeholder_mismatch_reordered_percent_tags(project, runner):
    source = "{% if a %}A{% endif %}"
    translated = "{% endif %}A{% if a %}"
    _seed_submitted_generation({SUBJECT_KEY: source})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: translated})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": source, "translations": {}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "placeholder_mismatch=1" in result.output


@respx.mock
def test_pull_placeholder_mismatch_duplicated_curly_var(project, runner):
    source = "{{ x }} once"
    translated = "{{ x }} {{ x }} deux fois"
    _seed_submitted_generation({SUBJECT_KEY: source})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: translated})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": source, "translations": {}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "placeholder_mismatch=1" in result.output


@respx.mock
def test_pull_conflict_when_klaviyo_edited_since_last_write_and_force_overrides(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour (new)"})
    # Someone hand-edited the fr translation in Klaviyo; it doesn't match what we last wrote (nothing, here).
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": "Bonjour (manual)"}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "conflict=1" in result.output

    state = State(".klaviyo-tc/state.db")
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") is None


@respx.mock
def test_pull_force_overrides_conflict(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour (new)"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": "Bonjour (manual)"}}])
    respx.patch(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}}
    )

    result = runner.invoke(cli, ["pull", "--force"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output


@respx.mock
def test_pull_unchanged_when_identical_to_current(project, runner):
    state, generation_id = _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    # This tool already wrote "Bonjour" on an earlier pull; pulling it again is a no-op, not a conflict.
    state.record_written(TRANSLATION_ID, SUBJECT_KEY, "fr", "Bonjour", generation_id)
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": "Bonjour"}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "unchanged=1" in result.output


@respx.mock
def test_pull_deleted_when_key_no_longer_in_klaviyo(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour"})
    _mock_klaviyo_current_values([])  # value_id removed from Klaviyo entirely

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "deleted=1" in result.output


@respx.mock
def test_pull_unknown_key_not_in_sent_snapshot(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, f"{TRANSLATION_ID}::other": "Bonjour"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "unknown_key=1" in result.output


@respx.mock
def test_pull_conflict_when_written_value_was_since_cleared_in_klaviyo(project, runner):
    # A `written` record exists but Klaviyo's value differs (cleared to empty) -> conflict.
    state, generation_id = _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    state.record_written(TRANSLATION_ID, SUBJECT_KEY, "fr", "Bonjour", generation_id)
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour (v2)"})
    # Someone cleared the fr translation in Klaviyo after we wrote "Bonjour".
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": ""}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "conflict=1" in result.output
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") == "Bonjour"


@respx.mock
def test_pull_identical_value_is_unchanged_even_with_a_written_record(project, runner):
    # Same setup, but the download equals what Klaviyo has -> unchanged, never conflict.
    state, generation_id = _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    state.record_written(TRANSLATION_ID, SUBJECT_KEY, "fr", "Bonjour", generation_id)
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": "Bonjour"}}])

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "unchanged=1" in result.output
    assert "conflict" not in result.output


@respx.mock
def test_pull_rechecks_before_patch_and_skips_a_concurrent_klaviyo_edit(project, runner):
    # Someone edits Klaviyo directly between our first classify and the PATCH.
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour"})

    first_fetch = klaviyo_translation_payload(
        TRANSLATION_ID, target_locales=["fr"], values=[{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {}}]
    )
    concurrently_edited = klaviyo_translation_payload(
        TRANSLATION_ID,
        target_locales=["fr"],
        values=[{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {"fr": "Bonjour (edited by a human)"}}],
    )
    get_route = respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").mock(
        side_effect=[
            httpx.Response(200, json=first_fetch),  # initial classify: looks writable
            httpx.Response(200, json=concurrently_edited),  # re-fetch right before PATCH
        ]
    )
    # No PATCH route registered: a PATCH here would fail respx matching.

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0, result.output
    assert "conflict=1" in result.output
    assert "written=1" not in result.output
    assert get_route.calls.call_count == 2

    state = State(".klaviyo-tc/state.db")
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") is None


@respx.mock
def test_pull_records_written_per_chunk_so_a_later_chunk_failure_keeps_earlier_writes(project, runner):
    # 101 values (chunks of 100 + 1); chunk 2's PATCH fails but chunk 1 must stick.
    keys = [f"{TRANSLATION_ID}::v{i}" for i in range(101)]
    sent_snapshot = {k: f"source {i}" for i, k in enumerate(keys)}
    downloaded = {k: f"translated {i}" for i, k in enumerate(keys)}
    empty_values = [{"id": k, "source_value": f"source {i}", "translations": {}} for i, k in enumerate(keys)]

    _seed_submitted_generation(sent_snapshot)
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 101})
    _mock_download("fr-FR", {"smartling": {}, **downloaded})
    get_route = _mock_klaviyo_current_values(empty_values)
    patch_route = respx.patch(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}}),
            httpx.Response(400, json={"errors": [{"detail": "boom"}]}),
        ]
    )

    first = runner.invoke(cli, ["pull"])
    assert first.exit_code != 0
    assert patch_route.calls.call_count == 2

    state = State(".klaviyo-tc/state.db")
    chunk_1_keys = keys[:100]
    chunk_2_key = keys[100]
    for key in chunk_1_keys:
        i = int(key.rsplit("v", 1)[1])
        assert state.get_written(TRANSLATION_ID, key, "fr") == f"translated {i}"
    assert state.get_written(TRANSLATION_ID, chunk_2_key, "fr") is None

    # Klaviyo now reflects the 100 successful writes; the 101st is still empty.
    updated_values = [
        {
            "id": k,
            "source_value": f"source {i}",
            "translations": {"fr": f"translated {i}"} if k in chunk_1_keys else {},
        }
        for i, k in enumerate(keys)
    ]
    get_route.respond(json=klaviyo_translation_payload(TRANSLATION_ID, target_locales=["fr"], values=updated_values))
    patch_route.respond(json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}})

    second = runner.invoke(cli, ["pull"])
    assert second.exit_code == 0, second.output
    assert "conflict" not in second.output
    assert state.get_written(TRANSLATION_ID, chunk_2_key, "fr") == f"translated 100"


@respx.mock
def test_pull_no_submitted_generation_is_skipped(project, runner):
    result = runner.invoke(cli, ["pull", "--id", "campaign-variation::email::never-pushed"])
    assert result.exit_code == 0, result.output
    assert "no submitted generation" in result.output


@respx.mock
def test_status_shows_generation_and_pull_counts(project, runner):
    _seed_submitted_generation({SUBJECT_KEY: "Hello"})
    mock_smartling_auth(respx)
    _mock_file_status({"fr-FR": 1})
    _mock_download("fr-FR", {"smartling": {}, SUBJECT_KEY: "Bonjour"})
    _mock_klaviyo_current_values([{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {}}])
    respx.patch(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={"data": {"type": "translation", "id": TRANSLATION_ID, "attributes": {}}}
    )
    assert runner.invoke(cli, ["pull"]).exit_code == 0

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert TRANSLATION_ID in result.output
    assert "fr=1/1" in result.output
