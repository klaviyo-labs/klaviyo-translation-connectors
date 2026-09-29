import respx

from klaviyo_tc.cli import cli

from conftest import (
    KLAVIYO_BASE,
    SENTINEL_KLAVIYO_KEY,
    SENTINEL_SMARTLING_SECRET,
    SENTINEL_SMARTLING_USER,
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


def test_missing_env_var_error_names_the_var_only(project, runner, monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    result = runner.invoke(cli, ["push", "--id", TRANSLATION_ID])
    assert result.exit_code != 0
    assert "KLAVIYO_API_KEY" in result.output
    assert SENTINEL_SMARTLING_USER not in result.output
