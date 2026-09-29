"""Provider-agnostic push/pull orchestration.

Knows Klaviyo translations and the `Provider` protocol; never imports a
specific provider package.
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from ..klaviyo import PATCH_CHUNK_SIZE
from ..providers.base import FileSpec
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
PROBLEM_OUTCOMES = ("conflict", "stale_source", "placeholder_mismatch", "deleted", "unknown_key")


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


def _compute_push_content(*, config: Config, klaviyo, translation_id: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return (strings, included_locales, skipped_locales) for one translation."""
    data = klaviyo.get_translation(translation_id)
    attrs = data["attributes"]
    target_locales = attrs.get("target_locales", [])
    values = attrs.get("values", [])

    included = {k: v for k, v in config.locales.items() if k in target_locales}
    skipped_locales = [k for k in config.locales if k not in included]
    strings = {v["id"]: v["source_value"] for v in values if v.get("source_value")}
    return strings, included, skipped_locales


def push_translation(*, config: Config, state: State, klaviyo, provider, translation_id: str, dry_run: bool) -> PushResult:
    strings, included, skipped_locales = _compute_push_content(config=config, klaviyo=klaviyo, translation_id=translation_id)
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
    elif generation["sent_snapshot"] != strings or generation["locales"] != included:
        # Resuming: keep the snapshot in sync with what we're about to (re)upload.
        state.update_generation_snapshot(generation["id"], strings, included)
        generation["sent_snapshot"] = strings
        generation["locales"] = included

    def checkpoint(provider_state: dict) -> None:
        # Persisted immediately so a crash mid-submit resumes past what already succeeded.
        state.set_provider_state(generation["id"], provider_state)

    submit_result = provider.submit(
        translation_id=translation_id,
        file_name=file_name,
        strings=strings,
        target_locales=list(included.values()),
        reference=generation["id"],
        state=generation["provider_state"],
        checkpoint=checkpoint,
    )
    state.set_provider_state(generation["id"], submit_result.state)
    if submit_result.submitted:
        state.set_status(generation["id"], "submitted")

    result.generation_id = generation["id"]
    result.submitted = submit_result.submitted
    return result


def _classify(
    *, value_id: str, translated: str, sent_snapshot: dict, current_values: dict, klaviyo_locale: str,
    state: State, translation_id: str, force: bool,
) -> str:
    if value_id not in sent_snapshot:
        return "unknown_key"
    current = current_values.get(value_id)
    if current is None:
        return "deleted"
    if current.get("source_value") != sent_snapshot[value_id]:
        return "stale_source"
    if not placeholders.matches(sent_snapshot[value_id], translated):
        return "placeholder_mismatch"

    existing = (current.get("translations") or {}).get(klaviyo_locale, "")
    if existing == translated:
        return "unchanged"

    # A `written` record but a different (possibly cleared/empty) current
    # value means someone touched it in Klaviyo since our last write.
    last_written = state.get_written(translation_id, value_id, klaviyo_locale)
    normalized_last_written = last_written if last_written is not None else ""
    if existing != normalized_last_written and not force:
        return "conflict"
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
                state=state,
                translation_id=translation_id,
                force=force,
            )
            counts[outcome] += 1
            if outcome == "written":
                to_patch.append({"id": value_id, "translations": {klaviyo_locale: translated}})

        if not dry_run and to_patch:
            # No conditional write in the Klaviyo API: re-check right before writing.
            fresh_data = klaviyo.get_translation(translation_id)
            fresh_values = {v["id"]: v for v in fresh_data["attributes"].get("values", [])}
            still_eligible = []
            for entry in to_patch:
                value_id = entry["id"]
                translated = entry["translations"][klaviyo_locale]
                fresh_outcome = _classify(
                    value_id=value_id,
                    translated=translated,
                    sent_snapshot=generation["sent_snapshot"],
                    current_values=fresh_values,
                    klaviyo_locale=klaviyo_locale,
                    state=state,
                    translation_id=translation_id,
                    force=force,
                )
                if fresh_outcome == "written":
                    still_eligible.append(entry)
                else:
                    counts["written"] -= 1
                    counts[fresh_outcome] += 1
            to_patch = still_eligible

            for start in range(0, len(to_patch), PATCH_CHUNK_SIZE):
                chunk = to_patch[start : start + PATCH_CHUNK_SIZE]
                klaviyo.patch_translation(translation_id, chunk)
                # Record per chunk: a later chunk failing shouldn't lose this one's writes.
                for entry in chunk:
                    state.record_written(
                        translation_id, entry["id"], klaviyo_locale, entry["translations"][klaviyo_locale], generation["id"]
                    )

        if not dry_run:
            state.record_pull_status(generation["id"], klaviyo_locale, len(downloaded), counts["written"])

        result.locale_outcomes.append(LocalePullOutcome(locale=klaviyo_locale, counts=dict(counts)))

    return result


# -- Bulk (scope-based) push: one provider job per run, not one per translation --------


@dataclass
class BulkPushItem:
    translation_id: str
    outcome: str  # "submitted" | "unchanged" | "error"
    skipped_locales: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class BulkPushResult:
    scope_description: str
    unchanged: int = 0
    submitted_files: int = 0
    errors: int = 0
    skipped_locales: int = 0
    job_name: str | None = None
    run_id: str | None = None
    items: list[BulkPushItem] = field(default_factory=list)
    dry_run_files: dict[str, dict[str, str]] = field(default_factory=dict)


def push_scope(
    *,
    config: Config,
    state: State,
    klaviyo,
    provider,
    translation_ids: list[str],
    scope_description: str,
    scope_key: str,
    job_name_base: str,
    force_resend: bool,
    dry_run: bool,
) -> BulkPushResult:
    result = BulkPushResult(scope_description=scope_description)

    per_translation: dict[str, tuple[dict[str, str], dict[str, str], list[str]]] = {}
    for translation_id in translation_ids:
        try:
            per_translation[translation_id] = _compute_push_content(config=config, klaviyo=klaviyo, translation_id=translation_id)
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest of the scope
            result.errors += 1
            result.items.append(BulkPushItem(translation_id, "error", detail=str(exc)))

    if dry_run:
        for translation_id, (strings, _included, skipped) in per_translation.items():
            result.dry_run_files[translation_id] = strings
            result.skipped_locales += len(skipped)
            result.items.append(BulkPushItem(translation_id, "dry-run", skipped_locales=skipped))
        return result

    to_submit = []
    for translation_id, (strings, included, skipped) in per_translation.items():
        result.skipped_locales += len(skipped)
        active = state.get_active_generation(translation_id)
        unchanged = active is not None and active["sent_snapshot"] == strings and active["locales"] == included
        if unchanged and not force_resend:
            result.unchanged += 1
            result.items.append(BulkPushItem(translation_id, "unchanged", skipped_locales=skipped))
            continue
        to_submit.append((translation_id, strings, included, skipped))

    if not to_submit:
        return result

    existing_run = state.get_pending_run(scope_key)
    if existing_run is None:
        run_id = str(uuid.uuid4())
        job_name = f"{job_name_base} {run_id[:8]}"
        state.create_run(run_id, job_name, scope_key, scope_description)
        run_provider_state = None
    else:
        run_id = existing_run["id"]
        job_name = existing_run["job_name"]
        run_provider_state = existing_run["provider_state"]

    result.run_id = run_id
    result.job_name = job_name

    existing_generations = {g["translation_id"]: g for g in state.list_generations_for_run(run_id)}
    generations: dict[str, dict] = {}
    files: list[FileSpec] = []
    for translation_id, strings, included, skipped in to_submit:
        file_name = _file_name_for(translation_id)
        generation = existing_generations.get(translation_id)
        if generation is None:
            generation_id = state.create_generation(translation_id, file_name, strings, included, run_id=run_id)
            generation = state.get_generation(generation_id)
        elif generation["sent_snapshot"] != strings or generation["locales"] != included:
            state.update_generation_snapshot(generation["id"], strings, included)
            generation["sent_snapshot"] = strings
            generation["locales"] = included
        generations[translation_id] = generation
        files.append(FileSpec(translation_id, file_name, strings, list(included.values())))

    def checkpoint(provider_state: dict) -> None:
        state.set_run_provider_state(run_id, provider_state)

    submit_result = provider.submit_many(
        run_ref=run_id, job_name=job_name, files=files, state=run_provider_state, checkpoint=checkpoint
    )
    state.set_run_provider_state(run_id, submit_result.state)
    if submit_result.submitted:
        state.set_run_status(run_id, "submitted")
        file_states = submit_result.file_states or {}
        for translation_id, strings, included, skipped in to_submit:
            generation = generations[translation_id]
            file_name = _file_name_for(translation_id)
            # Never inspect provider-owned state shapes here: the provider decides them.
            state.set_provider_state(generation["id"], file_states.get(file_name, {}))
            state.set_status(generation["id"], "submitted")
            result.submitted_files += 1
            result.items.append(BulkPushItem(translation_id, "submitted", skipped_locales=skipped))

    return result


# -- Bulk (scope-based) pull: aggregate outcome counts across many translations --------


@dataclass
class BulkPullResult:
    scope_description: str
    per_translation: dict[str, PullResult] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def aggregate_counts(self) -> dict[str, int]:
        totals: Counter = Counter()
        for pull_result in self.per_translation.values():
            for locale_outcome in pull_result.locale_outcomes:
                totals.update(locale_outcome.counts)
        return dict(totals)

    @property
    def problem_translations(self) -> dict[str, dict[str, int]]:
        """translation_id -> {outcome: count} for translations with any problem outcome."""
        problems: dict[str, dict[str, int]] = {}
        for translation_id, pull_result in self.per_translation.items():
            totals: Counter = Counter()
            for locale_outcome in pull_result.locale_outcomes:
                for outcome in PROBLEM_OUTCOMES:
                    if locale_outcome.counts.get(outcome):
                        totals[outcome] += locale_outcome.counts[outcome]
            if totals:
                problems[translation_id] = dict(totals)
        return problems


def pull_scope(
    *, config: Config, state: State, klaviyo, provider, translation_ids: list[str], force: bool, dry_run: bool
) -> BulkPullResult:
    result = BulkPullResult(scope_description=f"{len(translation_ids)} translation(s)")
    for translation_id in translation_ids:
        try:
            result.per_translation[translation_id] = pull_translation(
                config=config, state=state, klaviyo=klaviyo, provider=provider,
                translation_id=translation_id, force=force, dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest of the scope
            result.errors[translation_id] = str(exc)
    return result
