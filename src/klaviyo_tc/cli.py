"""klaviyo-tc CLI: init / push / pull / status."""
from __future__ import annotations

import functools
import json
import os
from datetime import datetime, timezone

import click

from . import __version__, scopes
from .core import engine
from .core.config import TEMPLATE, ConfigError, load_config
from .core.state import State
from .klaviyo import KlaviyoClient
from .providers.registry import get_provider_class
from .redact import redact


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise click.ClickException(f"missing required environment variable {name}")
    return value


def guarded(f):
    """Catch stray exceptions and redact secrets before they ever hit stdout/stderr."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except click.ClickException:
            raise
        except Exception as exc:  # noqa: BLE001 - last line of defense against secret leaks
            click.echo(redact(str(exc)), err=True)
            raise SystemExit(1) from None

    return wrapper


class Context:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self._config = None
        self._state = None
        self._klaviyo = None
        self._provider = None

    @property
    def config(self):
        if self._config is None:
            try:
                self._config = load_config(self.config_path)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from exc
        return self._config

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = State(self.config.state_path)
        return self._state

    @property
    def klaviyo(self) -> KlaviyoClient:
        if self._klaviyo is None:
            api_key = require_env("KLAVIYO_API_KEY")
            self._klaviyo = KlaviyoClient(
                self.config.klaviyo.base_url,
                api_key,
                self.config.klaviyo.revision,
                revisions=self.config.klaviyo.revisions,
            )
        return self._klaviyo

    @property
    def provider(self):
        if self._provider is None:
            name = self.config.provider_name
            provider_cls = get_provider_class(name)
            provider_config = self.config.provider_config
            # Only Smartling is wired up today; see docs/adding-a-provider.md for adding another.
            if name == "smartling":
                self._provider = provider_cls(
                    base_url=provider_config["base_url"],
                    project_id=provider_config["project_id"],
                    user_identifier=require_env("SMARTLING_USER_IDENTIFIER"),
                    user_secret=require_env("SMARTLING_USER_SECRET"),
                    string_format_paths=provider_config["string_format_paths"],
                    placeholder_format_custom=provider_config["placeholder_format_custom"],
                    files_per_batch=provider_config.get("files_per_batch", 100),
                    authorize=provider_config.get("authorize", True),
                    workflow_uid=provider_config.get("workflow_uid") or None,
                    version=__version__,
                )
            else:
                raise click.ClickException(f"provider '{name}' has no construction wiring in cli.py")
        return self._provider


pass_ctx = click.make_pass_decorator(Context)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_key_for_ids(ids: tuple[str, ...]) -> str:
    return "id:" + ",".join(sorted(ids))


def _scope_key_for_all(channel: str | None, resource_type: str | None) -> str:
    return f"all:channel={channel or ''}:resource_type={resource_type or ''}"


def _scope_key_for_campaigns_flows(campaign_ids: tuple[str, ...], flow_ids: tuple[str, ...]) -> str:
    parts = sorted(f"campaign:{c}" for c in campaign_ids) + sorted(f"flow:{f}" for f in flow_ids)
    return "scope:" + ",".join(parts)


def _scope_key_for_templates(
    template_ids: tuple[str, ...], templates_all: bool, universal_content_ids: tuple[str, ...],
    all_universal_content: bool, name_contains: str | None, updated_since: str | None,
) -> str:
    parts = sorted(f"template:{t}" for t in template_ids) + sorted(f"uc:{u}" for u in universal_content_ids)
    if templates_all:
        parts.append(f"templates_all:name_contains={name_contains or ''}:updated_since={updated_since or ''}")
    if all_universal_content:
        parts.append(f"all_uc:name_contains={name_contains or ''}")
    return "scope:" + ",".join(parts)


class Scope:
    """Resolved translation ids plus everything needed to report on and push them."""

    def __init__(self, translation_ids, description, scope_key=None, job_name_base=None, scope_result=None):
        self.translation_ids = translation_ids
        self.description = description
        self.scope_key = scope_key
        self.job_name_base = job_name_base
        self.scope_result = scope_result  # scopes.ScopeResult, only set for --campaign/--flow/--tag


def _scope_error_count(scope: Scope) -> int:
    if scope.scope_result is None:
        return 0
    return sum(1 for item in scope.scope_result.items if item.outcome == "error")


def resolve_scope(
    ctx: Context,
    *,
    ids: tuple[str, ...] = (),
    all_: bool = False,
    campaign_ids: tuple[str, ...] = (),
    flow_ids: tuple[str, ...] = (),
    tag_name: str | None = None,
    template_ids: tuple[str, ...] = (),
    templates_all: bool = False,
    universal_content_ids: tuple[str, ...] = (),
    all_universal_content: bool = False,
    name_contains: str | None = None,
    updated_since: str | None = None,
    template_channel: str = "email",
    channel: str | None = None,
    resource_type: str | None = None,
    create_missing: bool = False,
    dry_run: bool = False,
    default_to_tracked: bool = False,
) -> Scope:
    template_family = bool(template_ids) or templates_all or bool(universal_content_ids) or all_universal_content
    selected = sum([bool(ids), bool(all_), bool(campaign_ids or flow_ids), bool(tag_name), template_family])
    if selected == 0:
        if default_to_tracked:
            return Scope(ctx.state.list_translation_ids(), "all tracked translations")
        raise click.UsageError(
            "specify one of --id, --all, --campaign/--flow, or --tag "
            "(or --template/--templates/--universal-content/--all-universal-content)"
        )
    if selected > 1:
        raise click.UsageError(
            "--id, --all, --campaign/--flow, --tag, and --template/--universal-content are mutually exclusive"
        )

    if ids:
        return Scope(
            list(ids), f"{len(ids)} translation id(s)", _scope_key_for_ids(ids), f"Klaviyo bulk {_utc_timestamp()}"
        )

    if all_:
        translation_ids = [item["id"] for item in ctx.klaviyo.list_translations(channel=channel, resource_type=resource_type)]
        bits = [b for b in (f"channel={channel}" if channel else None, f"resource_type={resource_type}" if resource_type else None) if b]
        description = "all translations" + (f" ({', '.join(bits)})" if bits else "")
        return Scope(translation_ids, description, _scope_key_for_all(channel, resource_type), f"Klaviyo bulk {_utc_timestamp()}")

    if campaign_ids or flow_ids:
        scope_result = scopes.resolve_scope(
            ctx.klaviyo, campaign_ids=list(campaign_ids), flow_ids=list(flow_ids), create_missing=create_missing,
            dry_run=dry_run, config=ctx.config,
        )
        if len(campaign_ids) + len(flow_ids) == 1:
            job_name_base = f"Klaviyo: {scope_result.description}"
        else:
            job_name_base = f"Klaviyo bulk {_utc_timestamp()}"
        return Scope(
            scope_result.translation_ids,
            scope_result.description,
            _scope_key_for_campaigns_flows(campaign_ids, flow_ids),
            job_name_base,
            scope_result,
        )

    if tag_name:
        scope_result = scopes.resolve_scope(
            ctx.klaviyo, tag_name=tag_name, create_missing=create_missing, dry_run=dry_run, config=ctx.config
        )
        return Scope(
            scope_result.translation_ids, f"tag '{tag_name}'", f"tag:{tag_name}", f"Klaviyo: {tag_name}", scope_result
        )

    scope_result = scopes.resolve_scope(
        ctx.klaviyo,
        template_ids=list(template_ids),
        templates_all=templates_all,
        universal_content_ids=list(universal_content_ids),
        all_universal_content=all_universal_content,
        name_contains=name_contains,
        updated_since=updated_since,
        template_channel=template_channel,
        create_missing=create_missing,
        dry_run=dry_run,
        config=ctx.config,
    )
    named_singles = len(template_ids) + len(universal_content_ids)
    if named_singles == 1 and not templates_all and not all_universal_content:
        job_name_base = f"Klaviyo: {scope_result.description}"
    else:
        job_name_base = f"Klaviyo templates {_utc_timestamp()}"
    return Scope(
        scope_result.translation_ids,
        scope_result.description,
        _scope_key_for_templates(
            template_ids, templates_all, universal_content_ids, all_universal_content, name_contains, updated_since
        ),
        job_name_base,
        scope_result,
    )


def _print_resolution_summary(scope: Scope) -> None:
    if scope.scope_result is None:
        return
    by_outcome: dict[str, int] = {}
    for item in scope.scope_result.items:
        by_outcome[item.outcome] = by_outcome.get(item.outcome, 0) + 1
        if item.outcome == "error":
            click.echo(f"warning: {item.resource.resource_type} {item.resource.resource_id}: {item.detail}", err=True)
        if item.single_html_body:
            click.echo(
                f"warning: {item.resource.resource_type} {item.resource.resource_id} "
                f"({item.resource.parent_name}): {item.resource.editor_type} editor stores a single HTML body value",
                err=True,
            )
    click.echo(
        f"Scope: {scope.description} -- resolved: {by_outcome.get('resolved', 0) + by_outcome.get('created', 0)}, "
        f"no_translation: {by_outcome.get('no_translation', 0)}, created: {by_outcome.get('created', 0)}, "
        f"errors: {by_outcome.get('error', 0)}, single_html_body: {len(scope.scope_result.single_html_body_items)}"
        + (f", would_create: {by_outcome['would_create']}" if by_outcome.get("would_create") else "")
    )


def template_scope_options(f):
    """Shared --template/--templates/--universal-content options for push/pull/status."""
    f = click.option("--template", "template_ids", multiple=True, help="Template id (repeatable)")(f)
    f = click.option("--templates", "templates_all", is_flag=True, help="Every template matching --name-contains/--updated-since")(f)
    f = click.option("--universal-content", "universal_content_ids", multiple=True, help="Universal content id (repeatable)")(f)
    f = click.option(
        "--all-universal-content", "all_universal_content", is_flag=True,
        help="Every universal content block matching --name-contains",
    )(f)
    f = click.option("--name-contains", "name_contains", default=None, help="Filter --templates/--all-universal-content by name")(f)
    f = click.option("--updated-since", "updated_since", default=None, help="Filter --templates to items updated after this ISO date")(f)
    f = click.option(
        "--template-channel", "template_channel", default="email", show_default=True,
        help="Channel for --template/--templates (email or whatsapp)",
    )(f)
    return f


@click.group()
@click.option("--config", "config_path", default="config.yaml", show_default=True, help="Path to config file")
@click.pass_context
def cli(click_ctx, config_path):
    """Sync Klaviyo Smart Translations with a translation management system."""
    click_ctx.obj = Context(config_path)


@cli.command()
@guarded
def init():
    """Write a config.yaml template if one does not already exist."""
    path = "config.yaml"
    if os.path.exists(path):
        click.echo(f"{path} already exists, not overwriting")
        return
    with open(path, "w") as f:
        f.write(TEMPLATE)
    click.echo(f"wrote {path}")


@cli.command()
@click.option("--id", "ids", multiple=True, help="Translation id (repeatable)")
@click.option("--all", "all_", is_flag=True, help="Push every translation matching --channel/--resource-type")
@click.option("--campaign", "campaign_ids", multiple=True, help="Campaign id (repeatable, combinable with --flow)")
@click.option("--flow", "flow_ids", multiple=True, help="Flow id (repeatable, combinable with --campaign)")
@click.option("--tag", "tag_name", default=None, help="Tag name: all campaigns and flows tagged with it")
@template_scope_options
@click.option("--channel", default=None)
@click.option("--resource-type", "resource_type", default=None)
@click.option("--job-name", "job_name_override", default=None, help="Override the default provider job name")
@click.option("--create-missing", is_flag=True, help="Create a translation for a resource that doesn't have one yet")
@click.option("--force-resend", is_flag=True, help="Resubmit even if nothing changed since the last submitted push")
@click.option(
    "--no-authorize", "no_authorize", is_flag=True,
    help="Create the provider job unauthorized, overriding providers.smartling.authorize",
)
@click.option("--dry-run", is_flag=True)
@pass_ctx
@guarded
def push(
    ctx: Context, ids, all_, campaign_ids, flow_ids, tag_name,
    template_ids, templates_all, universal_content_ids, all_universal_content, name_contains, updated_since, template_channel,
    channel, resource_type, job_name_override, create_missing, force_resend, no_authorize, dry_run,
):
    """Push Klaviyo source strings to the configured provider: one job per scope run."""
    scope = resolve_scope(
        ctx, ids=ids, all_=all_, campaign_ids=campaign_ids, flow_ids=flow_ids, tag_name=tag_name,
        template_ids=template_ids, templates_all=templates_all, universal_content_ids=universal_content_ids,
        all_universal_content=all_universal_content, name_contains=name_contains, updated_since=updated_since,
        template_channel=template_channel, channel=channel, resource_type=resource_type, create_missing=create_missing,
        dry_run=dry_run,
    )
    _print_resolution_summary(scope)

    provider = None if dry_run else ctx.provider
    if provider is not None and no_authorize:
        provider.authorize = False
    state = None if dry_run else ctx.state

    result = engine.push_scope(
        config=ctx.config,
        state=state,
        klaviyo=ctx.klaviyo,
        provider=provider,
        translation_ids=scope.translation_ids,
        scope_description=scope.description,
        scope_key=scope.scope_key or "",
        job_name_base=job_name_override or scope.job_name_base or f"Klaviyo bulk {_utc_timestamp()}",
        force_resend=force_resend,
        dry_run=dry_run,
    )

    for item in result.items:
        for locale in item.skipped_locales:
            click.echo(f"warning: {item.translation_id}: locale '{locale}' not enabled on translation, skipping", err=True)
        if item.outcome == "error":
            click.echo(redact(f"{item.translation_id}: error: {item.detail}"), err=True)

    scope_errors = _scope_error_count(scope)
    if dry_run:
        for translation_id, strings in result.dry_run_files.items():
            click.echo(f"{translation_id}:")
            click.echo(json.dumps(strings, indent=2, sort_keys=True))
        if result.errors or scope_errors:
            raise SystemExit(1)
        return

    click.echo(
        f"Push summary -- scope: {result.scope_description}, unchanged: {result.unchanged}, "
        f"submitted files: {result.submitted_files}, skipped locales: {result.skipped_locales}, errors: {result.errors}"
    )
    if result.job_name:
        click.echo(f"Job: {result.job_name} (run {result.run_id})")

    if result.errors or scope_errors:
        raise SystemExit(1)


@cli.command()
@click.option("--id", "ids", multiple=True, help="Translation id (repeatable); default is every tracked translation")
@click.option("--campaign", "campaign_ids", multiple=True)
@click.option("--flow", "flow_ids", multiple=True)
@click.option("--tag", "tag_name", default=None)
@template_scope_options
@click.option("--force", is_flag=True, help="Overwrite translations edited in Klaviyo since the last pull")
@click.option("--verbose", is_flag=True, help="Show per-translation, per-locale outcome counts")
@click.option("--dry-run", is_flag=True)
@pass_ctx
@guarded
def pull(
    ctx: Context, ids, campaign_ids, flow_ids, tag_name,
    template_ids, templates_all, universal_content_ids, all_universal_content, name_contains, updated_since, template_channel,
    force, verbose, dry_run,
):
    """Pull completed translations back into Klaviyo."""
    scope = resolve_scope(
        ctx, ids=ids, campaign_ids=campaign_ids, flow_ids=flow_ids, tag_name=tag_name,
        template_ids=template_ids, templates_all=templates_all, universal_content_ids=universal_content_ids,
        all_universal_content=all_universal_content, name_contains=name_contains, updated_since=updated_since,
        template_channel=template_channel, default_to_tracked=True,
    )
    _print_resolution_summary(scope)

    result = engine.pull_scope(
        config=ctx.config, state=ctx.state, klaviyo=ctx.klaviyo, provider=ctx.provider,
        translation_ids=scope.translation_ids, force=force, dry_run=dry_run,
    )

    for translation_id, detail in result.errors.items():
        click.echo(redact(f"{translation_id}: error: {detail}"), err=True)

    if verbose:
        for translation_id, pull_result in result.per_translation.items():
            if pull_result.skipped:
                click.echo(f"{translation_id}: no submitted generation, skipping")
                continue
            if not pull_result.locale_outcomes:
                click.echo(f"{translation_id}: nothing ready to pull")
                continue
            for locale_outcome in pull_result.locale_outcomes:
                summary = ", ".join(
                    f"{name}={locale_outcome.counts[name]}" for name in engine.OUTCOME_ORDER if locale_outcome.counts.get(name)
                )
                click.echo(f"{translation_id} [{locale_outcome.locale}]: {summary or 'nothing to do'}")

    totals = result.aggregate_counts
    summary = ", ".join(f"{name}={totals[name]}" for name in engine.OUTCOME_ORDER if totals.get(name))
    click.echo(f"Pull summary -- scope: {result.scope_description}: {summary or 'nothing to do'}")

    problems = result.problem_translations
    if problems:
        click.echo("Translations needing attention:")
        for translation_id, counts in problems.items():
            detail = ", ".join(f"{name}={count}" for name, count in counts.items())
            click.echo(f"  {translation_id}: {detail}")

    if result.errors or _scope_error_count(scope):
        raise SystemExit(1)


@cli.command()
@click.option("--id", "ids", multiple=True)
@click.option("--campaign", "campaign_ids", multiple=True)
@click.option("--flow", "flow_ids", multiple=True)
@click.option("--tag", "tag_name", default=None)
@template_scope_options
@pass_ctx
@guarded
def status(
    ctx: Context, ids, campaign_ids, flow_ids, tag_name,
    template_ids, templates_all, universal_content_ids, all_universal_content, name_contains, updated_since, template_channel,
):
    """Show tracked translations, their active generation, and last pull counts."""
    scope = resolve_scope(
        ctx, ids=ids, campaign_ids=campaign_ids, flow_ids=flow_ids, tag_name=tag_name,
        template_ids=template_ids, templates_all=templates_all, universal_content_ids=universal_content_ids,
        all_universal_content=all_universal_content, name_contains=name_contains, updated_since=updated_since,
        template_channel=template_channel, default_to_tracked=True,
    )
    _print_resolution_summary(scope)

    tracked = set(ctx.state.list_translation_ids())
    translation_ids = [t for t in scope.translation_ids if t in tracked] if scope.scope_result or ids else scope.translation_ids
    if not translation_ids:
        click.echo("no translations tracked yet")
        return
    for translation_id in translation_ids:
        generation = ctx.state.get_active_generation(translation_id)
        if generation is None:
            click.echo(f"{translation_id}: no submitted generation")
            continue
        pulls = ctx.state.get_pull_status(generation["id"])
        pull_str = ", ".join(f"{p['locale']}={p['written']}/{p['downloaded']}" for p in pulls) or "no pulls yet"
        run_bit = f" run={generation['run_id']}" if generation.get("run_id") else ""
        click.echo(f"{translation_id}: generation={generation['id']}{run_bit} status={generation['status']} pulls=[{pull_str}]")


if __name__ == "__main__":
    cli()
