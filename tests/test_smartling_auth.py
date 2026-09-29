import httpx
import respx

from klaviyo_tc.cli import cli

from conftest import (
    PROJECT_ID,
    SMARTLING_BASE,
    mock_klaviyo_get_translation,
    mock_smartling_auth,
    mock_smartling_no_job_found,
)

TRANSLATION_ID = "campaign-variation::email::01K1EXAMPLE"
VALUES = [{"id": f"{TRANSLATION_ID}::subject", "source_value": "Hello", "translations": {}}]


@respx.mock
def test_expired_token_triggers_one_reauth_and_retry(project, runner):
    mock_klaviyo_get_translation(respx, TRANSLATION_ID, target_locales=["fr"], values=VALUES)
    auth_route = mock_smartling_auth(respx)
    mock_smartling_no_job_found(respx)

    job_url = f"{SMARTLING_BASE}/jobs-api/v3/projects/{PROJECT_ID}/jobs"
    # First call with the (stale) token is rejected; after re-auth, the retry succeeds.
    job_route = respx.post(job_url).mock(
        side_effect=[
            httpx.Response(401, json={"response": {"code": "AUTHENTICATION_ERROR", "errors": []}}),
            httpx.Response(200, json={"response": {"code": "SUCCESS", "data": {"translationJobUid": "job-1"}}}),
        ]
    )
    respx.post(f"{SMARTLING_BASE}/job-batches-api/v2/projects/{PROJECT_ID}/batches").respond(
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
    assert job_route.calls.call_count == 2
    assert auth_route.calls.call_count == 2  # once up front, once after the 401
