import email
from email import policy

import httpx
import pytest
from click.testing import CliRunner

KLAVIYO_BASE = "https://a.klaviyo.com"
SMARTLING_BASE = "https://api.smartling.com"
PROJECT_ID = "proj123"

CONFIG_YAML = r"""klaviyo:
  base_url: https://a.klaviyo.com
  revision: "2026-07-15.pre"
  source_locale: en
  fallback_locale: en
  revisions:
    flows: "2025-10-15"
    tags: "2025-10-15"
    templates: "2025-10-15"
    universal_content: "2025-10-15"

provider:
  name: smartling

providers:
  smartling:
    base_url: https://api.smartling.com
    project_id: proj123
    string_format_paths: "html: *"
    placeholder_format_custom: ['\{\{[^}]+\}\}', '\{%[^%]+%\}']
    files_per_batch: 100

locales:
  fr: fr-FR
  es: es-ES

state:
  path: .klaviyo-tc/state.db
"""

FLOWS_REVISION = "2025-10-15"

PLACEHOLDER_FORMAT_CUSTOM = [r"\{\{[^}]+\}\}", r"\{%[^%]+%\}"]

SENTINEL_KLAVIYO_KEY = "sentinel-klaviyo-key-do-not-leak"
SENTINEL_SMARTLING_USER = "sentinel-smartling-user-do-not-leak"
SENTINEL_SMARTLING_SECRET = "sentinel-smartling-secret-do-not-leak"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An isolated cwd with config.yaml and secret env vars set to sentinels."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    monkeypatch.setenv("KLAVIYO_API_KEY", SENTINEL_KLAVIYO_KEY)
    monkeypatch.setenv("SMARTLING_USER_IDENTIFIER", SENTINEL_SMARTLING_USER)
    monkeypatch.setenv("SMARTLING_USER_SECRET", SENTINEL_SMARTLING_SECRET)
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


def mock_smartling_no_job_found(respx_mock):
    """No existing job matches by name: submit() falls through to create_job."""
    return respx_mock.get(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={"response": {"code": "SUCCESS", "data": {"items": []}}}
    )


def mock_smartling_auth(respx_mock):
    return respx_mock.post(f"{SMARTLING_BASE}/auth-api/v2/authenticate").respond(
        json={
            "response": {
                "code": "SUCCESS",
                "data": {
                    "accessToken": "access-token-1",
                    "expiresIn": 3600,
                    "refreshToken": "refresh-token-1",
                    "refreshExpiresIn": 7200,
                },
            }
        }
    )


def success_envelope(data):
    return {"response": {"code": "SUCCESS", "data": data}}


def klaviyo_translation_payload(translation_id, *, source_locale="en", target_locales=None, values=None):
    return {
        "data": {
            "type": "translation",
            "id": translation_id,
            "attributes": {
                "source_locale": source_locale,
                "target_locales": target_locales or [],
                "fallback_locale": source_locale,
                "channel": "email",
                "values": values or [],
            },
        }
    }


def mock_klaviyo_get_translation(respx_mock, translation_id, **kwargs):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/translations/{translation_id}/").respond(
        json=klaviyo_translation_payload(translation_id, **kwargs)
    )


def mock_klaviyo_patch_translation(respx_mock, translation_id):
    return respx_mock.patch(f"{KLAVIYO_BASE}/api/translations/{translation_id}/").respond(
        json=klaviyo_translation_payload(translation_id)
    )


def klaviyo_list_payload(items):
    return {"data": items, "links": {"next": None}}


def mock_klaviyo_get_campaign(respx_mock, campaign_id, name):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/campaigns/{campaign_id}/").respond(
        json={"data": {"type": "campaign", "id": campaign_id, "attributes": {"name": name}}}
    )


def mock_klaviyo_campaign_messages(respx_mock, campaign_id, messages):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/campaigns/{campaign_id}/campaign-messages").respond(
        json=klaviyo_list_payload(messages)
    )


def mock_klaviyo_campaign_variations(respx_mock, message_id, variations):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/campaign-messages/{message_id}/campaign-variations").respond(
        json=klaviyo_list_payload(variations)
    )


def mock_klaviyo_get_flow(respx_mock, flow_id, name):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/flows/{flow_id}/").respond(
        json={"data": {"type": "flow", "id": flow_id, "attributes": {"name": name}}}
    )


def mock_klaviyo_flow_actions(respx_mock, flow_id, actions):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/flows/{flow_id}/flow-actions").respond(
        json=klaviyo_list_payload(actions)
    )


def mock_klaviyo_flow_messages(respx_mock, action_id, messages):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/flow-actions/{action_id}/flow-messages").respond(
        json=klaviyo_list_payload(messages)
    )


def mock_klaviyo_find_tags(respx_mock, tags):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/tags/").respond(json=klaviyo_list_payload(tags))


def mock_klaviyo_tag_campaigns(respx_mock, tag_id, campaign_ids):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/tags/{tag_id}/relationships/campaigns").respond(
        json=klaviyo_list_payload([{"type": "campaign", "id": cid} for cid in campaign_ids])
    )


def mock_klaviyo_tag_flows(respx_mock, tag_id, flow_ids):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/tags/{tag_id}/relationships/flows").respond(
        json=klaviyo_list_payload([{"type": "flow", "id": fid} for fid in flow_ids])
    )


def mock_klaviyo_find_translation_for_resource(respx_mock, resource_id, translation_id=None):
    """Distinguishes resources by the `filter` query param, not just the shared path."""
    payload = klaviyo_list_payload([{"type": "translation", "id": translation_id}] if translation_id else [])
    return respx_mock.get(
        f"{KLAVIYO_BASE}/api/translations/", params={"filter": f'equals(related_resource_id,"{resource_id}")'}
    ).respond(json=payload)


def mock_klaviyo_get_template(respx_mock, template_id, name, editor_type="DRAG_AND_DROP"):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/templates/{template_id}/").respond(
        json={"data": {"type": "template", "id": template_id, "attributes": {"name": name, "editor_type": editor_type}}}
    )


def mock_klaviyo_list_templates(respx_mock, templates):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/templates/").respond(json=klaviyo_list_payload(templates))


def mock_klaviyo_get_universal_content_item(respx_mock, uc_id, name):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/template-universal-content/{uc_id}/").respond(
        json={"data": {"type": "template-universal-content", "id": uc_id, "attributes": {"name": name}}}
    )


def mock_klaviyo_list_universal_content(respx_mock, items):
    return respx_mock.get(f"{KLAVIYO_BASE}/api/template-universal-content/").respond(json=klaviyo_list_payload(items))


def mock_klaviyo_create_translation(respx_mock, created_id):
    return respx_mock.post(f"{KLAVIYO_BASE}/api/translations/").respond(
        status_code=201, json={"data": {"type": "translation", "id": created_id, "attributes": {}}}
    )


def parse_multipart(request) -> dict:
    """Decode an httpx multipart request into {field_name: [payload_bytes, ...]}."""
    header = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.message_from_bytes(header + request.content, policy=policy.default)
    fields: dict = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fields.setdefault(name, []).append(part.get_payload(decode=True))
    return fields
