"""Provider-agnostic push/pull orchestration.

Knows Klaviyo translations and the `Provider` protocol; never imports a
specific provider package.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import placeholders
from .config import Config
from .state import State

OUTCOME_ORDER = (
    "written",
    "unchanged",
    "conflict",
    "stale_source",
    "placeholder_mismatch",
    "deleted",
    "unknown_key",
)


@dataclass
class PushResult:
    translation_id: str
    dry_run: bool
    file_name: str
    strings: dict[str, str]
    skipped_locales: list[str]
    generation_id: str | None = None
    submitted: bool = False


@dataclass
class LocalePullOutcome:
    locale: str
    counts: dict[str, int]


@dataclass
class PullResult:
    translation_id: str
    dry_run: bool
    skipped: bool = False
    locale_outcomes: list[LocalePullOutcome] = field(default_factory=list)


def _file_name_for(translation_id: str) -> str:
    return f"klaviyo/{translation_id.replace('::', '__')}.json"


def push_translation(*, config: Config, state: State, klaviyo, provider, translation_id: str, dry_run: bool) -> PushResult:
    data = klaviyo.get_translation(translation_id)
    attrs = data["attributes"]
    target_locales = attrs.get("target_locales", [])
    values = attrs.get("values", [])

    included = {k: v for k, v in config.locales.items() if k in target_locales}
    skipped_locales = [k for k in config.locales if k not in included]

    strings = {v["id"]: v["source_value"] for v in values if v.get("source_value")}
    file_name = _file_name_for(translation_id)

    result = PushResult(
        translation_id=translation_id,
        dry_run=dry_run,
        file_name=file_name,
        strings=strings,
        skipped_locales=skipped_locales,
    )
    if dry_run:
        return result

    generation = state.get_pending_generation(translation_id)
    if generation is None:
        generation_id = state.create_generation(translation_id, file_name, strings, included)
        generation = state.get_generation(generation_id)

    submit_result = provider.submit(
        translation_id=translation_id,
        file_name=file_name,
        strings=strings,
        target_locales=list(included.values()),
        reference=generation["id"],
        state=generation["provider_state"],
    )
    state.set_provider_state(generation["id"], submit_result.state)
    if submit_result.submitted:
        state.set_status(generation["id"], "submitted")

    result.generation_id = generation["id"]
    result.submitted = submit_result.submitted
    return result


def _classify(
    *, value_id: str, translated: str, sent_snapshot: dict, current_values: dict, klaviyo_locale: str,
    pattern: str, state: State, translation_id: str, force: bool,
) -> str:
    if value_id not in sent_snapshot:
        return "unknown_key"
    current = current_values.get(value_id)
    if current is None:
        return "deleted"
    if current.get("source_value") != sent_snapshot[value_id]:
        return "stale_source"
    if not placeholders.matches(sent_snapshot[value_id], translated, pattern):
        return "placeholder_mismatch"

    existing = (current.get("translations") or {}).get(klaviyo_locale, "")
    last_written = state.get_written(translation_id, value_id, klaviyo_locale)
    if existing and existing != last_written and not force:
        return "conflict"
    if existing == translated:
        return "unchanged"
    return "written"


def pull_translation(
    *, config: Config, state: State, klaviyo, provider, translation_id: str, force: bool, dry_run: bool
) -> PullResult:
    generation = state.get_active_generation(translation_id)
    if generation is None:
        return PullResult(translation_id=translation_id, dry_run=dry_run, skipped=True)

    completed_locales = set(provider.completed_locales(generation["provider_state"]))
    klaviyo_data = klaviyo.get_translation(translation_id)
    current_values = {v["id"]: v for v in klaviyo_data["attributes"].get("values", [])}
    pattern = config.klaviyo.placeholder_pattern

    result = PullResult(translation_id=translation_id, dry_run=dry_run)

    for klaviyo_locale, provider_locale in generation["locales"].items():
        if provider_locale not in completed_locales:
            continue

        downloaded = provider.fetch(generation["provider_state"], provider_locale)
        counts: Counter = Counter()
        to_patch = []

        for value_id, translated in downloaded.items():
            if not translated:
                continue
            outcome = _classify(
                value_id=value_id,
                translated=translated,
                sent_snapshot=generation["sent_snapshot"],
                current_values=current_values,
                klaviyo_locale=klaviyo_locale,
                pattern=pattern,
                state=state,
                translation_id=translation_id,
                force=force,
            )
            counts[outcome] += 1
            if outcome == "written":
                to_patch.append({"id": value_id, "translations": {klaviyo_locale: translated}})

        if not dry_run:
            if to_patch:
                klaviyo.patch_translation(translation_id, to_patch)
                for entry in to_patch:
                    state.record_written(
                        translation_id, entry["id"], klaviyo_locale, entry["translations"][klaviyo_locale], generation["id"]
                    )
            state.record_pull_status(generation["id"], klaviyo_locale, len(downloaded), counts["written"])

        result.locale_outcomes.append(LocalePullOutcome(locale=klaviyo_locale, counts=dict(counts)))

    return result
