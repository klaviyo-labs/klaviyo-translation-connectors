"""--template/--templates/--universal-content/--all-universal-content scopes."""
import json
import urllib.parse

import httpx
import respx

from klaviyo_tc.cli import cli

from conftest import (
    KLAVIYO_BASE,
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_create_translation,
    mock_klaviyo_find_translation_for_resource,
    mock_klaviyo_get_template,
    mock_klaviyo_get_translation,
    mock_klaviyo_get_universal_content_item,
    mock_klaviyo_list_universal_content,
    mock_smartling_auth,
    mock_smartling_no_job_found,
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


@respx.mock
def test_push_template_resolves_and_submits_one_job(project, runner):
    template_id = "template-1"
    translation_id = "template::email::template-1"

    mock_klaviyo_get_template(respx, template_id, "Welcome Email")
    mock_klaviyo_find_translation_for_resource(respx, template_id, translation_id)
    mock_klaviyo_get_translation(
        respx, translation_id, target_locales=["fr"],
        values=[{"id": f"{translation_id}::subject", "source_value": "Welcome", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--template", template_id])
    assert result.exit_code == 0, result.output
    assert "submitted files: 1" in result.output
    assert "Job: Klaviyo: Welcome Email" in result.output
    assert job_route.calls.call_count == 1
    assert upload_route.calls.call_count == 1


@respx.mock
def test_push_templates_builds_name_contains_filter_and_paginates(project, runner):
    next_url = f"{KLAVIYO_BASE}/api/templates/?page%5Bcursor%5D=abc"
    list_route = respx.get(f"{KLAVIYO_BASE}/api/templates/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"type": "template", "id": "t1", "attributes": {"name": "Sale A", "editor_type": "DRAG_AND_DROP"}}],
                    "links": {"next": next_url},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [{"type": "template", "id": "t2", "attributes": {"name": "Sale B", "editor_type": "DRAG_AND_DROP"}}],
                    "links": {"next": None},
                },
            ),
        ]
    )
    t1_id, t2_id = "template::email::t1", "template::email::t2"
    mock_klaviyo_find_translation_for_resource(respx, "t1", t1_id)
    mock_klaviyo_find_translation_for_resource(respx, "t2", t2_id)
    mock_klaviyo_get_translation(
        respx, t1_id, target_locales=["fr"], values=[{"id": f"{t1_id}::subject", "source_value": "A", "translations": {}}]
    )
    mock_klaviyo_get_translation(
        respx, t2_id, target_locales=["fr"], values=[{"id": f"{t2_id}::subject", "source_value": "B", "translations": {}}]
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--templates", "--name-contains", "Sale"])
    assert result.exit_code == 0, result.output
    assert "submitted files: 2" in result.output
    assert "Job: Klaviyo templates" in result.output
    assert list_route.calls.call_count == 2

    first_request = list_route.calls[0].request
    assert 'contains(name,"Sale")' in urllib.parse.unquote(str(first_request.url))


@respx.mock
def test_push_templates_updated_since_filter_is_combined_with_name_contains(project, runner):
    list_route = respx.get(f"{KLAVIYO_BASE}/api/templates/").respond(json={"data": [], "links": {"next": None}})

    result = runner.invoke(
        cli, ["push", "--templates", "--name-contains", "Sale", "--updated-since", "2026-01-01T00:00:00Z"]
    )
    assert result.exit_code == 0, result.output
    query = urllib.parse.unquote(str(list_route.calls.last.request.url))
    assert 'and(contains(name,"Sale"),greater-than(updated,2026-01-01T00:00:00Z))' in query


@respx.mock
def test_push_template_create_missing_defaults_to_email_channel(project, runner):
    template_id = "template-1"
    created_id = "template::email::template-1"
    mock_klaviyo_get_template(respx, template_id, "Welcome Email")
    mock_klaviyo_find_translation_for_resource(respx, template_id, translation_id=None)
    create_route = mock_klaviyo_create_translation(respx, created_id)
    mock_klaviyo_get_translation(
        respx, created_id, target_locales=["fr"], values=[{"id": f"{created_id}::subject", "source_value": "Hi", "translations": {}}]
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--template", template_id, "--create-missing"])
    assert result.exit_code == 0, result.output
    body = json.loads(create_route.calls.last.request.content)
    assert body["data"]["attributes"]["channel"] == "email"
    assert body["data"]["relationships"] == {"template": {"data": {"type": "template", "id": template_id}}}


@respx.mock
def test_push_template_create_missing_with_whatsapp_channel(project, runner):
    template_id = "template-1"
    created_id = "template::whatsapp::template-1"
    mock_klaviyo_get_template(respx, template_id, "WhatsApp Welcome")
    mock_klaviyo_find_translation_for_resource(respx, template_id, translation_id=None)
    create_route = mock_klaviyo_create_translation(respx, created_id)
    mock_klaviyo_get_translation(
        respx, created_id, target_locales=["fr"], values=[{"id": f"{created_id}::subject", "source_value": "Hi", "translations": {}}]
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--template", template_id, "--create-missing", "--template-channel", "whatsapp"])
    assert result.exit_code == 0, result.output
    body = json.loads(create_route.calls.last.request.content)
    assert body["data"]["attributes"]["channel"] == "whatsapp"


@respx.mock
def test_push_universal_content_single_item(project, runner):
    uc_id = "uc-1"
    translation_id = "template-universal-content::email::uc-1"
    mock_klaviyo_get_universal_content_item(respx, uc_id, "Header Block")
    mock_klaviyo_find_translation_for_resource(respx, uc_id, translation_id)
    mock_klaviyo_get_translation(
        respx, translation_id, target_locales=["fr"],
        values=[{"id": f"{translation_id}::body", "source_value": "Header", "translations": {}}],
    )
    mock_smartling_auth(respx)
    job_route, batch_route, upload_route, status_route = _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--universal-content", uc_id])
    assert result.exit_code == 0, result.output
    assert "submitted files: 1" in result.output
    assert "Job: Klaviyo: Header Block" in result.output


@respx.mock
def test_push_all_universal_content(project, runner):
    uc1_id, uc2_id = "template-universal-content::email::uc1", "template-universal-content::email::uc2"
    mock_klaviyo_list_universal_content(
        respx,
        [
            {"type": "template-universal-content", "id": "uc1", "attributes": {"name": "Block 1"}},
            {"type": "template-universal-content", "id": "uc2", "attributes": {"name": "Block 2"}},
        ],
    )
    mock_klaviyo_find_translation_for_resource(respx, "uc1", uc1_id)
    mock_klaviyo_find_translation_for_resource(respx, "uc2", uc2_id)
    mock_klaviyo_get_translation(
        respx, uc1_id, target_locales=["fr"], values=[{"id": f"{uc1_id}::body", "source_value": "A", "translations": {}}]
    )
    mock_klaviyo_get_translation(
        respx, uc2_id, target_locales=["fr"], values=[{"id": f"{uc2_id}::body", "source_value": "B", "translations": {}}]
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--all-universal-content"])
    assert result.exit_code == 0, result.output
    assert "submitted files: 2" in result.output
    assert "Job: Klaviyo templates" in result.output


@respx.mock
def test_push_code_template_warns_and_counts_single_html_body(project, runner):
    template_id = "template-1"
    translation_id = "template::email::template-1"
    mock_klaviyo_get_template(respx, template_id, "Legacy Code Template", editor_type="CODE")
    mock_klaviyo_find_translation_for_resource(respx, template_id, translation_id)
    mock_klaviyo_get_translation(
        respx, translation_id, target_locales=["fr"],
        values=[{"id": f"{translation_id}::body", "source_value": "<html>Hi</html>", "translations": {}}],
    )
    mock_smartling_auth(respx)
    _mock_single_batch_smartling()

    result = runner.invoke(cli, ["push", "--template", template_id])
    assert result.exit_code == 0, result.output
    assert "CODE editor stores a single HTML body value" in result.output
    assert "single_html_body: 1" in result.output


@respx.mock
def test_push_template_and_universal_content_scopes_are_mutually_exclusive_with_id(project, runner):
    result = runner.invoke(cli, ["push", "--id", "x", "--template", "template-1"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
