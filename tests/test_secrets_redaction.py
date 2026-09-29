import respx

from klaviyo_tc.cli import cli

from conftest import (
    KLAVIYO_BASE,
    PROJECT_ID,
    SENTINEL_KLAVIYO_KEY,
    SENTINEL_SMARTLING_SECRET,
    SENTINEL_SMARTLING_USER,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
)

TRANSLATION_ID = "campaign-variation::email::01K1EXAMPLE"


@respx.mock
def test_klaviyo_error_output_never_contains_secrets(project, runner):
    # Error bodies can echo request details back; nothing secret should reach output.
    respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        status_code=401,
        text=f"unauthorized, saw header Authorization: Klaviyo-API-Key {SENTINEL_KLAVIYO_KEY}",
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert SENTINEL_KLAVIYO_KEY not in result.output
    assert SENTINEL_SMARTLING_USER not in result.output
    assert SENTINEL_SMARTLING_SECRET not in result.output


@respx.mock
def test_smartling_error_output_never_contains_secrets(project, runner):
    respx.get(f"{KLAVIYO_BASE}/api/translations/{TRANSLATION_ID}/").respond(
        json={
            "data": {
                "type": "translation",
                "id": TRANSLATION_ID,
                "attributes": {
                    "source_locale": "en",
                    "target_locales": ["fr"],
                    "fallback_locale": "en",
                    "channel": "email",
                    "values": [{"id": f"{TRANSLATION_ID}::subject", "source_value": "Hi", "translations": {}}],
                },
            }
        }
    )
    respx.post("https://api.smartling.com/auth-api/v2/authenticate").respond(
        status_code=401,
        text=f"bad credentials for {SENTINEL_SMARTLING_USER} / {SENTINEL_SMARTLING_SECRET}",
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert SENTINEL_KLAVIYO_KEY not in result.output
    assert SENTINEL_SMARTLING_USER not in result.output
    assert SENTINEL_SMARTLING_SECRET not in result.output


@respx.mock
def test_issued_access_token_echoed_in_an_error_body_is_redacted(project, runner):
    # Not an env-var secret; the client must register it for redaction on issue.
    issued_token = "issued-access-token-should-never-leak"
    mock_klaviyo_get_translation(
        respx,
        TRANSLATION_ID,
        target_locales=["fr"],
        values=[{"id": f"{TRANSLATION_ID}::subject", "source_value": "Hi", "translations": {}}],
    )
    respx.post(f"{SMARTLING_BASE}/auth-api/v2/authenticate").respond(
        json={
            "response": {
                "code": "SUCCESS",
                "data": {
                    "accessToken": issued_token,
                    "expiresIn": 3600,
                    "refreshToken": "refresh-token-1",
                    "refreshExpiresIn": 7200,
                },
            }
        }
    )
    respx.get(f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs").respond(
        json={
            "response": {
                "code": "PERMISSION_DENIED",
                "errors": [f"rejected request with Authorization: Bearer {issued_token}"],
            }
        }
    )

    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert issued_token not in result.output
    assert SENTINEL_KLAVIYO_KEY not in result.output


def test_missing_env_var_error_names_the_var_only(project, runner, monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "KLAVIYO_API_KEY" in result.output
    assert SENTINEL_SMARTLING_USER not in result.output
