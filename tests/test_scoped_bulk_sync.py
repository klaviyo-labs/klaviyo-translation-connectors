"""Scope resolution (--campaign/--flow/--tag), create-missing, skip-unchanged,
batch-splitting, run-level crash-resume, and bulk-pull summaries.
"""
import json
import re

import httpx
import respx

from klaviyo_tc.cli import cli
from klaviyo_tc.core.state import State

from conftest import (
    KLAVIYO_BASE,
    PROJECT_ID,
    SMARTLING_BASE,
    klaviyo_list_payload,
    klaviyo_translation_payload,
    mock_klaviyo_campaign_messages,
    mock_klaviyo_campaign_variations,
    mock_klaviyo_create_translation,
    mock_klaviyo_find_tags,
    mock_klaviyo_find_translation_for_resource,
    mock_klaviyo_flow_actions,
    mock_klaviyo_flow_messages,
    mock_klaviyo_get_campaign,
    mock_klaviyo_get_flow,
    mock_klaviyo_get_translation,
    mock_klaviyo_tag_campaigns,
    mock_klaviyo_tag_flows,
    mock_smartling_auth,
    mock_smartling_no_job_found,
    parse_multipart,
)


def _mock_single_batch_smartling(job_uid="job-1", batch_uid="batch-1"):
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


# -- Scope resolution: --campaign ------------------------------------------------------


@respx.mock
def test_push_campaign_resolves_messages_and_variations_into_one_job(project, runner):
    campaign_id = "campaign-1"
    var_a = "campaign-variation::email::var-a"
    var_b = "campaign-variation::email::var-b"

    mock_klaviyo_get_campaign(respx, campaign_id, "Summer Sale")
    mock_klaviyo_campaign_messages(respx, campaign_id, [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_campaign_variations(
        respx, "msg-1", [{"id": "var-a"}, {"id": "var-b"}]
    )
    mock_klaviyo_find_translation_for_resource(respx, "var-a", var_a)
    mock_klaviyo_find_translation_for_resource(respx, "var-b", var_b)
    # Two files with different enabled locales, to prove per-file locales aren't shared.
    mock_klaviyo_get_translation(
        respx, var_a, target_locales=["fr", "es"],
        values=[{"id": f"{var_a}::subject", "source_value": "Hi", "translations": {}}],
    )
    mock_klaviyo_get_translation(
        respx, var_b, target_locales=["fr"],
        values=[{"id": f"{var_b}::subject", "source_value": "Hey", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--campaign", campaign_id])
    assert result.exit_code == 0, result.output
    assert "Scope: Summer Sale -- resolved: 2, no_translation: 0, created: 0, errors: 0" in result.output
    assert "submitted files: 2" in result.output
    assert "Job: Klaviyo: Summer Sale" in result.output

    assert job_route.calls.call_count == 1
    assert batch_route.calls.call_count == 1
    assert upload_route.calls.call_count == 2

    batch_body = json.loads(batch_route.calls.last.request.content)
    file_a = f"klaviyo/{var_a.replace('::', '__')}.json"
    file_b = f"klaviyo/{var_b.replace('::', '__')}.json"
    assert sorted(batch_body["fileUris"]) == sorted([file_a, file_b])

    locales_by_file = {}
    for call in upload_route.calls:
        fields = parse_multipart(call.request)
        locales_by_file[fields["fileUri"][0].decode()] = sorted(v.decode() for v in fields["localeIdsToAuthorize[]"])
    assert locales_by_file[file_a] == ["es-ES", "fr-FR"]
    assert locales_by_file[file_b] == ["fr-FR"]

    state = State(".klaviyo-tc/state.db")
    gen_a = state.get_active_generation(var_a)
    gen_b = state.get_active_generation(var_b)
    assert gen_a["run_id"] == gen_b["run_id"]


# -- Scope resolution: --flow ------------------------------------------------------


@respx.mock
def test_push_flow_resolves_actions_and_messages(project, runner):
    flow_id = "flow-1"
    translation_id = "flow-message::email::msg-1"

    get_flow_route = mock_klaviyo_get_flow(respx, flow_id, "Welcome Series")
    mock_klaviyo_flow_actions(respx, flow_id, [{"id": "action-1"}])
    mock_klaviyo_flow_messages(respx, "action-1", [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_find_translation_for_resource(respx, "msg-1", translation_id)
    mock_klaviyo_get_translation(
        respx, translation_id, target_locales=["fr"],
        values=[{"id": f"{translation_id}::subject", "source_value": "Welcome", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--flow", flow_id])
    assert result.exit_code == 0, result.output
    assert "submitted files: 1" in result.output
    assert "Job: Klaviyo: Welcome Series" in result.output

    # Flows use the GA revision override, independent of [klaviyo].revision.
    assert get_flow_route.calls.last.request.headers["revision"] == "2025-10-15"

    fields = parse_multipart(upload_route.calls.last.request)
    assert fields["fileUri"][0].decode() == f"klaviyo/{translation_id.replace('::', '__')}.json"


# -- Scope resolution: --tag -------------------------------------------------------


@respx.mock
def test_push_tag_resolves_both_campaigns_and_flows(project, runner):
    tag_name = "VIP"
    campaign_id = "campaign-1"
    flow_id = "flow-1"
    var_id = "campaign-variation::email::var-a"
    flow_msg_id = "flow-message::email::msg-1"

    mock_klaviyo_find_tags(respx, [{"id": "tag-1", "attributes": {"name": tag_name}}])
    mock_klaviyo_tag_campaigns(respx, "tag-1", [campaign_id])
    mock_klaviyo_tag_flows(respx, "tag-1", [flow_id])

    mock_klaviyo_get_campaign(respx, campaign_id, "Summer Sale")
    mock_klaviyo_campaign_messages(respx, campaign_id, [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_campaign_variations(respx, "msg-1", [{"id": "var-a"}])
    mock_klaviyo_find_translation_for_resource(respx, "var-a", var_id)
    mock_klaviyo_get_translation(
        respx, var_id, target_locales=["fr"],
        values=[{"id": f"{var_id}::subject", "source_value": "Hi", "translations": {}}],
    )

    mock_klaviyo_get_flow(respx, flow_id, "Welcome Series")
    mock_klaviyo_flow_actions(respx, flow_id, [{"id": "action-1"}])
    mock_klaviyo_flow_messages(respx, "action-1", [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_find_translation_for_resource(respx, "msg-1", flow_msg_id)
    mock_klaviyo_get_translation(
        respx, flow_msg_id, target_locales=["fr"],
        values=[{"id": f"{flow_msg_id}::subject", "source_value": "Welcome", "translations": {}}],
    )

    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--tag", tag_name])
    assert result.exit_code == 0, result.output
    assert "submitted files: 2" in result.output
    assert job_route.calls.call_count == 1
    assert upload_route.calls.call_count == 2


@respx.mock
def test_push_unknown_tag_errors(project, runner):
    mock_klaviyo_find_tags(respx, [])

    result = runner.invoke(cli, ["push", "--tag", "Nonexistent"])
    assert result.exit_code != 0
    assert "no tag named 'Nonexistent'" in result.output


# -- --create-missing ---------------------------------------------------------------


@respx.mock
def test_push_create_missing_posts_expected_body_and_pushes_it(project, runner):
    campaign_id = "campaign-1"
    var_id = "var-a"
    created_id = "campaign-variation::email::var-a"

    mock_klaviyo_get_campaign(respx, campaign_id, "Summer Sale")
    mock_klaviyo_campaign_messages(respx, campaign_id, [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_campaign_variations(respx, "msg-1", [{"id": var_id}])
    mock_klaviyo_find_translation_for_resource(respx, var_id, translation_id=None)
    create_route = mock_klaviyo_create_translation(respx, created_id)
    mock_klaviyo_get_translation(
        respx, created_id, target_locales=["fr", "es"],
        values=[{"id": f"{created_id}::subject", "source_value": "Hi", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--campaign", campaign_id, "--create-missing"])
    assert result.exit_code == 0, result.output
    assert "created: 1" in result.output
    assert "submitted files: 1" in result.output

    body = json.loads(create_route.calls.last.request.content)
    assert body == {
        "data": {
            "type": "translation",
            "attributes": {
                "channel": "email",
                "source_locale": "en",
                "target_locales": ["fr", "es"],
                "fallback_locale": "en",
            },
            "relationships": {
                "campaign-variation": {"data": {"type": "campaign-variation", "id": var_id}}
            },
        }
    }


@respx.mock
def test_push_create_missing_falls_back_to_lookup_on_409(project, runner):
    campaign_id = "campaign-1"
    var_id = "var-a"
    existing_id = "campaign-variation::email::var-a"

    mock_klaviyo_get_campaign(respx, campaign_id, "Summer Sale")
    mock_klaviyo_campaign_messages(respx, campaign_id, [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_campaign_variations(respx, "msg-1", [{"id": var_id}])

    find_route = respx.get(
        f"{KLAVIYO_BASE}/api/translations/", params={"filter": f'equals(related_resource_id,"{var_id}")'}
    ).mock(
        side_effect=[
            httpx.Response(200, json=klaviyo_list_payload([])),
            httpx.Response(200, json=klaviyo_list_payload([{"type": "translation", "id": existing_id}])),
        ]
    )
    create_route = respx.post(f"{KLAVIYO_BASE}/api/translations/").respond(
        status_code=409, json={"errors": [{"detail": "already exists"}]}
    )
    mock_klaviyo_get_translation(
        respx, existing_id, target_locales=["fr"],
        values=[{"id": f"{existing_id}::subject", "source_value": "Hi", "translations": {}}],
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--campaign", campaign_id, "--create-missing"])
    assert result.exit_code == 0, result.output
    assert "resolved: 1" in result.output
    assert find_route.calls.call_count == 2
    assert create_route.calls.call_count == 1


@respx.mock
def test_push_without_create_missing_reports_no_translation_and_does_not_push(project, runner):
    campaign_id = "campaign-1"
    mock_klaviyo_get_campaign(respx, campaign_id, "Summer Sale")
    mock_klaviyo_campaign_messages(respx, campaign_id, [{"id": "msg-1", "attributes": {"channel": "email"}}])
    mock_klaviyo_campaign_variations(respx, "msg-1", [{"id": "var-a"}])
    mock_klaviyo_find_translation_for_resource(respx, "var-a", translation_id=None)
    # No Smartling routes registered at all: any provider call fails the test.

    result = runner.invoke(cli, ["push", "--campaign", campaign_id])
    assert result.exit_code == 0, result.output
    assert "no_translation: 1" in result.output
    assert "submitted files: 0" in result.output
    assert "Job:" not in result.output


# -- Skip-unchanged / --force-resend -------------------------------------------------


@respx.mock
def test_push_skips_unchanged_on_rerun_and_force_resend_uploads_again(project, runner):
    translation_id = "campaign-variation::email::t1"
    mock_klaviyo_get_translation(
        respx, translation_id, target_locales=["fr"],
        values=[{"id": f"{translation_id}::subject", "source_value": "Hi", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    first = runner.invoke(cli, ["push", "--id", translation_id])
    assert first.exit_code == 0, first.output
    assert job_route.calls.call_count == 1
    assert upload_route.calls.call_count == 1

    second = runner.invoke(cli, ["push", "--id", translation_id])
    assert second.exit_code == 0, second.output
    assert "unchanged: 1" in second.output
    assert "submitted files: 0" in second.output
    # No new remote calls at all for unchanged content.
    assert job_route.calls.call_count == 1
    assert upload_route.calls.call_count == 1

    third = runner.invoke(cli, ["push", "--id", translation_id, "--force-resend"])
    assert third.exit_code == 0, third.output
    assert "submitted files: 1" in third.output
    assert upload_route.calls.call_count == 2


# -- Batch splitting at files_per_batch ----------------------------------------------


@respx.mock
def test_push_all_splits_over_100_files_into_two_batches_in_one_job(project, runner):
    ids = [f"campaign-variation::email::t{i:03d}" for i in range(101)]
    list_route = respx.get(f"{KLAVIYO_BASE}/api/translations/").respond(
        json=klaviyo_list_payload([{"type": "translation", "id": tid} for tid in ids])
    )

    def _get_translation_side_effect(request):
        match = re.search(r"/api/translations/([^/]+)/", request.url.path)
        translation_id = match.group(1)
        return httpx.Response(
            200,
            json=klaviyo_translation_payload(
                translation_id, target_locales=["fr"],
                values=[{"id": f"{translation_id}::subject", "source_value": "Hi", "translations": {}}],
            ),
        )

    respx.get(url__regex=rf"{re.escape(KLAVIYO_BASE)}/api/translations/[^/]+/").mock(
        side_effect=_get_translation_side_effect
    )

    mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)
    job_route = respx.post(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}
    )
    batch_route = respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").mock(
        side_effect=[
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-1"}}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"batchUid": "batch-2"}}}),
        ]
    )
    upload_route = respx.post(
        url__regex=rf"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/[^/]+/file$"
    ).respond(json={"response": {"code": "SUCCESS", "data": {}}})
    respx.get(
        url__regex=rf"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches/[^/]+$"
    ).respond(json={"response": {"code": "SUCCESS", "data": {"status": "COMPLETED"}}})

    result = runner.invoke(cli, ["push", "--all"])
    assert result.exit_code == 0, result.output
    assert "submitted files: 101" in result.output

    assert job_route.calls.call_count == 1
    assert batch_route.calls.call_count == 2
    assert upload_route.calls.call_count == 101

    sizes = sorted(len(json.loads(call.request.content)["fileUris"]) for call in batch_route.calls)
    assert sizes == [1, 100]
    assert list_route.calls.call_count == 1


# -- Run-level crash-resume (not per-translation) ------------------------------------


@respx.mock
def test_push_resumes_the_same_run_and_job_across_multiple_translations(project, runner):
    ids = ["campaign-variation::email::a", "campaign-variation::email::b"]
    for translation_id in ids:
        mock_klaviyo_get_translation(
            respx, translation_id, target_locales=["fr"],
            values=[{"id": f"{translation_id}::subject", "source_value": "Hi", "translations": {}}],
        )
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

    first = runner.invoke(cli, ["push", "--id", ids[0], "--id", ids[1]])
    assert first.exit_code != 0
    assert job_route.calls.call_count == 1
    assert job_search_route.calls.call_count == 1

    second = runner.invoke(cli, ["push", "--id", ids[0], "--id", ids[1]])
    assert second.exit_code == 0, second.output

    # One job for the whole run, created only once, even though it covers two translations.
    assert job_route.calls.call_count == 1
    assert job_search_route.calls.call_count == 1
    assert batch_route.calls.call_count == 2

    state = State(".klaviyo-tc/state.db")
    gen_a = state.get_active_generation(ids[0])
    gen_b = state.get_active_generation(ids[1])
    assert gen_a["run_id"] == gen_b["run_id"]
    run = state.get_run(gen_a["run_id"])
    assert run["provider_state"]["job_uid"] == "job-1"


# -- Bulk pull: aggregate summary + problem-only listing -----------------------------


def _seed_submitted_generation(translation_id, sent_snapshot, locales=None):
    state = State(".klaviyo-tc/state.db")
    file_name = f"klaviyo/{translation_id.replace('::', '__')}.json"
    generation_id = state.create_generation(translation_id, file_name, sent_snapshot, locales or {"fr": "fr-FR"})
    state.set_provider_state(generation_id, {"file_uri": file_name})
    state.set_status(generation_id, "submitted")
    return state, generation_id


@respx.mock
def test_pull_bulk_summary_counts_and_lists_only_problem_translations(project, runner):
    ok_id = "campaign-variation::email::ok"
    conflict_id = "campaign-variation::email::conflict"
    ok_key = f"{ok_id}::subject"
    conflict_key = f"{conflict_id}::subject"

    _seed_submitted_generation(ok_id, {ok_key: "Hello"})
    _seed_submitted_generation(conflict_id, {conflict_key: "Hello"})
    mock_smartling_auth(respx)

    def file_status(request):
        file_uri = request.url.params.get("fileUri")
        if ok_id.replace("::", "__") in file_uri:
            items = [{"localeId": "fr-FR", "completedStringCount": 1}]
        else:
            items = [{"localeId": "fr-FR", "completedStringCount": 1}]
        return httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"items": items}}})

    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/file/status").mock(side_effect=file_status)

    def download(request):
        file_uri = request.url.params.get("fileUri")
        if ok_id.replace("::", "__") in file_uri:
            return httpx.Response(200, json={"smartling": {}, ok_key: "Bonjour"})
        return httpx.Response(200, json={"smartling": {}, conflict_key: "Bonjour (new)"})

    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/locales/fr-FR/file").mock(side_effect=download)

    respx.get(f"{KLAVIYO_BASE}/api/translations/{ok_id}/").respond(
        json=klaviyo_translation_payload(ok_id, target_locales=["fr"], values=[{"id": ok_key, "source_value": "Hello", "translations": {}}])
    )
    # Someone hand-edited the conflict translation in Klaviyo since our last write.
    respx.get(f"{KLAVIYO_BASE}/api/translations/{conflict_id}/").respond(
        json=klaviyo_translation_payload(
            conflict_id, target_locales=["fr"],
            values=[{"id": conflict_key, "source_value": "Hello", "translations": {"fr": "Bonjour (manual)"}}],
        )
    )
    respx.patch(f"{KLAVIYO_BASE}/api/translations/{ok_id}/").respond(
        json={"data": {"type": "translation", "id": ok_id, "attributes": {}}}
    )

    result = runner.invoke(cli, ["pull", "--id", ok_id, "--id", conflict_id])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output
    assert "conflict=1" in result.output
    assert "Translations needing attention:" in result.output
    assert f"{conflict_id}: conflict=1" in result.output
    assert ok_id not in result.output.split("Translations needing attention:")[1]


def _mock_single_translation_pull(translation_id, key):
    _seed_submitted_generation(translation_id, {key: "Hello"})
    mock_smartling_auth(respx)
    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/file/status").respond(
        json={"response": {"code": "SUCCESS", "data": {"items": [{"localeId": "fr-FR", "completedStringCount": 1}]}}}
    )
    respx.get(f"{SMARTLING_BASE}/files-api/v2/projects/{PROJECT_ID}/locales/fr-FR/file").respond(
        json={"smartling": {}, key: "Bonjour"}
    )
    respx.get(f"{KLAVIYO_BASE}/api/translations/{translation_id}/").respond(
        json=klaviyo_translation_payload(
            translation_id, target_locales=["fr"], values=[{"id": key, "source_value": "Hello", "translations": {}}]
        )
    )
    respx.patch(f"{KLAVIYO_BASE}/api/translations/{translation_id}/").respond(
        json={"data": {"type": "translation", "id": translation_id, "attributes": {}}}
    )


@respx.mock
def test_pull_shows_no_per_translation_detail_without_verbose(project, runner):
    translation_id = "campaign-variation::email::t1"
    key = f"{translation_id}::subject"
    _mock_single_translation_pull(translation_id, key)

    result = runner.invoke(cli, ["pull", "--id", translation_id])
    assert result.exit_code == 0, result.output
    assert f"{translation_id} [fr]:" not in result.output
    assert "written=1" in result.output  # still shown in the aggregate summary


@respx.mock
def test_pull_verbose_shows_per_translation_detail(project, runner):
    translation_id = "campaign-variation::email::t1"
    key = f"{translation_id}::subject"
    _mock_single_translation_pull(translation_id, key)

    result = runner.invoke(cli, ["pull", "--id", translation_id, "--verbose"])
    assert result.exit_code == 0, result.output
    assert f"{translation_id} [fr]: written=1" in result.output
