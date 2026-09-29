import email
from email import policy

import pytest
from click.testing import CliRunner

KLAVIYO_BASE = "https://a.klaviyo.com"
SMARTLING_BASE = "https://api.smartling.com"
PROJECT_ID = "proj123"

CONFIG_TOML = r"""[klaviyo]
base_url = "https://a.klaviyo.com"
revision = "2026-07-15.pre"
placeholder_pattern = '\{\{[^}]+\}\}|\{%[^%]+%\}'

[provider]
name = "smartling"

[providers.smartling]
base_url = "https://api.smartling.com"
project_id = "proj123"
string_format_paths = "html: *"
placeholder_format_custom = '\{\{[^}]+\}\}|\{%[^%]+%\}'

[locales]
fr = "fr-FR"
es = "es-ES"

[state]
path = ".klaviyo-tc/state.db"
"""

SENTINEL_KLAVIYO_KEY = "sentinel-klaviyo-key-do-not-leak"
SENTINEL_SMARTLING_USER = "sentinel-smartling-user-do-not-leak"
SENTINEL_SMARTLING_SECRET = "sentinel-smartling-secret-do-not-leak"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An isolated cwd with klaviyo-tc.toml and secret env vars set to sentinels."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "klaviyo-tc.toml").write_text(CONFIG_TOML)
    monkeypatch.setenv("KLAVIYO_API_KEY", SENTINEL_KLAVIYO_KEY)
    monkeypatch.setenv("SMARTLING_USER_IDENTIFIER", SENTINEL_SMARTLING_USER)
    monkeypatch.setenv("SMARTLING_USER_SECRET", SENTINEL_SMARTLING_SECRET)
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


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


def parse_multipart(request) -> dict:
    """Decode an httpx multipart request into {field_name: [payload_bytes, ...]}."""
    header = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.message_from_bytes(header + request.content, policy=policy.default)
    fields: dict = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fields.setdefault(name, []).append(part.get_payload(decode=True))
    return fields
