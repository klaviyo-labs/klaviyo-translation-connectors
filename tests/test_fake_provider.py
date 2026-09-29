"""Drives the sync engine directly against a tiny in-memory provider (no HTTP,
no Smartling) to prove `klaviyo_tc.core` never depends on a specific provider.
"""
import respx

from klaviyo_tc.core import engine
from klaviyo_tc.core.config import Config, KlaviyoConfig
from klaviyo_tc.core.state import State
from klaviyo_tc.klaviyo import KlaviyoClient
from klaviyo_tc.providers.base import FileSpec, SubmitResult

from conftest import KLAVIYO_BASE, mock_klaviyo_get_translation, mock_klaviyo_patch_translation

TRANSLATION_ID = "campaign-variation::email::01K1FAKE"
SUBJECT_KEY = f"{TRANSLATION_ID}::subject"
TRANSLATION_ID_2 = "campaign-variation::email::01K1FAKE2"
SUBJECT_KEY_2 = f"{TRANSLATION_ID_2}::subject"


class FakeProvider:
    """Stores submitted strings in memory instead of calling any real API."""

    name = "fake"

    def __init__(self):
        self.storage: dict[str, dict[str, str]] = {}  # file_name -> {locale: {value_id: value}}
        self.jobs: dict[str, list[str]] = {}  # job_name -> file_names, so tests can see "one job per run"

    def submit(self, *, translation_id, file_name, strings, target_locales, reference, state=None, checkpoint=None):
        result = self.submit_many(
            run_ref=reference, job_name=file_name, files=[FileSpec(translation_id, file_name, strings, target_locales)],
            state=state, checkpoint=checkpoint,
        )
        # submit() persists run-level state directly; fold in this file's own state here.
        result.state.update(result.file_states[file_name])
        return result

    def submit_many(self, *, run_ref, job_name, files, state=None, checkpoint=None):
        state = dict(state or {})
        file_states = {}
        for file_spec in files:
            self.storage.setdefault(file_spec.file_name, {})
            file_states[file_spec.file_name] = {
                "file_name": file_spec.file_name, "locales": sorted(file_spec.target_locales)
            }
        self.jobs.setdefault(job_name, []).extend(f.file_name for f in files)
        if checkpoint is not None:
            checkpoint(dict(state))
        return SubmitResult(state=state, submitted=True, file_states=file_states)

    def translate(self, file_name: str, locale: str, value_id: str, value: str) -> None:
        """Test helper standing in for a linguist finishing a translation."""
        self.storage.setdefault(file_name, {}).setdefault(locale, {})[value_id] = value

    def completed_locales(self, state: dict) -> list[str]:
        return [locale for locale in state["locales"] if self.storage.get(state["file_name"], {}).get(locale)]

    def fetch(self, state: dict, locale: str) -> dict[str, str]:
        return dict(self.storage.get(state["file_name"], {}).get(locale, {}))


def _config(tmp_path):
    return Config(
        klaviyo=KlaviyoConfig(
            base_url=KLAVIYO_BASE,
            revision="2026-07-15.pre",
        ),
        provider_name="fake",
        provider_config={},
        locales={"fr": "fr"},
        state_path=str(tmp_path / "state.db"),
    )


@respx.mock
def test_push_and_pull_through_engine_with_fake_provider(tmp_path):
    config = _config(tmp_path)
    state = State(config.state_path)
    klaviyo = KlaviyoClient(config.klaviyo.base_url, "fake-key", config.klaviyo.revision)
    provider = FakeProvider()

    mock_klaviyo_get_translation(
        respx,
        TRANSLATION_ID,
        target_locales=["fr"],
        values=[{"id": SUBJECT_KEY, "source_value": "Hello {{ name }}", "translations": {}}],
    )

    push_result = engine.push_translation(
        config=config, state=state, klaviyo=klaviyo, provider=provider, translation_id=TRANSLATION_ID, dry_run=False
    )
    assert push_result.submitted
    assert push_result.strings == {SUBJECT_KEY: "Hello {{ name }}"}

    generation = state.get_active_generation(TRANSLATION_ID)
    assert generation is not None
    file_name = generation["file_name"]

    # Nothing translated yet: pull should find no completed locales.
    pull_result = engine.pull_translation(
        config=config, state=state, klaviyo=klaviyo, provider=provider,
        translation_id=TRANSLATION_ID, force=False, dry_run=False,
    )
    assert pull_result.locale_outcomes == []

    # The "linguist" finishes the fr translation.
    provider.translate(file_name, "fr", SUBJECT_KEY, "Bonjour {{ name }}")

    patch_route = mock_klaviyo_patch_translation(respx, TRANSLATION_ID)

    pull_result = engine.pull_translation(
        config=config, state=state, klaviyo=klaviyo, provider=provider,
        translation_id=TRANSLATION_ID, force=False, dry_run=False,
    )
    assert len(pull_result.locale_outcomes) == 1
    assert pull_result.locale_outcomes[0].locale == "fr"
    assert pull_result.locale_outcomes[0].counts == {"written": 1}
    assert patch_route.calls.call_count == 1
    assert state.get_written(TRANSLATION_ID, SUBJECT_KEY, "fr") == "Bonjour {{ name }}"


@respx.mock
def test_bulk_push_and_pull_through_engine_with_fake_provider(tmp_path):
    """The one-job-per-run bulk engine is provider-agnostic too, not just single-translation push."""
    config = _config(tmp_path)
    state = State(config.state_path)
    klaviyo = KlaviyoClient(config.klaviyo.base_url, "fake-key", config.klaviyo.revision)
    provider = FakeProvider()

    mock_klaviyo_get_translation(
        respx, TRANSLATION_ID, target_locales=["fr"],
        values=[{"id": SUBJECT_KEY, "source_value": "Hello", "translations": {}}],
    )
    mock_klaviyo_get_translation(
        respx, TRANSLATION_ID_2, target_locales=["fr"],
        values=[{"id": SUBJECT_KEY_2, "source_value": "Hi", "translations": {}}],
    )

    push_result = engine.push_scope(
        config=config, state=state, klaviyo=klaviyo, provider=provider,
        translation_ids=[TRANSLATION_ID, TRANSLATION_ID_2], scope_description="2 translation id(s)",
        scope_key="id:both", job_name_base="fake bulk run", force_resend=False, dry_run=False,
    )
    assert push_result.submitted_files == 2
    assert len(provider.jobs) == 1, "both files must land in a single provider job"

    gen_1 = state.get_active_generation(TRANSLATION_ID)
    gen_2 = state.get_active_generation(TRANSLATION_ID_2)
    assert gen_1["run_id"] == gen_2["run_id"]
    assert gen_1["provider_state"]["file_name"] == gen_1["file_name"]

    provider.translate(gen_1["file_name"], "fr", SUBJECT_KEY, "Bonjour")
    mock_klaviyo_patch_translation(respx, TRANSLATION_ID)

    pull_result = engine.pull_scope(
        config=config, state=state, klaviyo=klaviyo, provider=provider,
        translation_ids=[TRANSLATION_ID, TRANSLATION_ID_2], force=False, dry_run=False,
    )
    assert pull_result.aggregate_counts == {"written": 1}
    assert pull_result.problem_translations == {}
