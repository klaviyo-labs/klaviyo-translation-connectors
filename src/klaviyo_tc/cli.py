"""klaviyo-tc CLI: init / push / pull / status."""
from __future__ import annotations

import functools
import json
import os

import click

from . import __version__
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
            self._klaviyo = KlaviyoClient(self.config.klaviyo.base_url, api_key, self.config.klaviyo.revision)
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
                    version=__version__,
                )
            else:
                raise click.ClickException(f"provider '{name}' has no construction wiring in cli.py")
        return self._provider


pass_ctx = click.make_pass_decorator(Context)


@click.group()
@click.option("--config", "config_path", default="klaviyo-tc.toml", show_default=True, help="Path to config file")
@click.pass_context
def cli(click_ctx, config_path):
    """Sync Klaviyo Smart Translations with a translation management system."""
    click_ctx.obj = Context(config_path)


@cli.command()
@guarded
def init():
    """Write a klaviyo-tc.toml template if one does not already exist."""
    path = "klaviyo-tc.toml"
    if os.path.exists(path):
        click.echo(f"{path} already exists, not overwriting")
        return
    with open(path, "w") as f:
        f.write(TEMPLATE)
    click.echo(f"wrote {path}")


@cli.command()
@click.option("--id", "ids", multiple=True, help="Translation id (repeatable)")
@click.option("--all", "all_", is_flag=True, help="Push every translation matching --channel/--resource-type")
@click.option("--channel", default=None)
@click.option("--resource-type", "resource_type", default=None)
@click.option("--dry-run", is_flag=True)
@pass_ctx
@guarded
def push(ctx: Context, ids, all_, channel, resource_type, dry_run):
    """Push Klaviyo source strings to the configured provider."""
    if not ids and not all_:
        raise click.UsageError("specify --id or --all")
    if ids and all_:
        raise click.UsageError("--id and --all are mutually exclusive")

    if all_:
        translation_ids = [item["id"] for item in ctx.klaviyo.list_translations(channel=channel, resource_type=resource_type)]
    else:
        translation_ids = list(ids)

    # Dry-run must not touch state or the provider, so don't build them at all.
    state = None if dry_run else ctx.state
    provider = None if dry_run else ctx.provider

    had_error = False
    for translation_id in translation_ids:
        try:
            result = engine.push_translation(
                config=ctx.config,
                state=state,
                klaviyo=ctx.klaviyo,
                provider=provider,
                translation_id=translation_id,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - keep going for the remaining ids
            had_error = True
            click.echo(redact(f"{translation_id}: error: {exc}"), err=True)
            continue

        for locale in result.skipped_locales:
            click.echo(
                f"warning: {translation_id}: locale '{locale}' not enabled on translation, skipping", err=True
            )

        if dry_run:
            click.echo(json.dumps(result.strings, indent=2, sort_keys=True))
        else:
            click.echo(f"{translation_id}: submitted (generation {result.generation_id})")

    if had_error:
        raise SystemExit(1)


@cli.command()
@click.option("--id", "ids", multiple=True, help="Translation id (repeatable); default is every tracked translation")
@click.option("--force", is_flag=True, help="Overwrite translations edited in Klaviyo since the last pull")
@click.option("--dry-run", is_flag=True)
@pass_ctx
@guarded
def pull(ctx: Context, ids, force, dry_run):
    """Pull completed translations back into Klaviyo."""
    translation_ids = list(ids) if ids else ctx.state.list_translation_ids()

    had_error = False
    for translation_id in translation_ids:
        try:
            result = engine.pull_translation(
                config=ctx.config,
                state=ctx.state,
                klaviyo=ctx.klaviyo,
                provider=ctx.provider,
                translation_id=translation_id,
                force=force,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - keep going for the remaining ids
            had_error = True
            click.echo(redact(f"{translation_id}: error: {exc}"), err=True)
            continue

        if result.skipped:
            click.echo(f"{translation_id}: no submitted generation, skipping")
            continue
        if not result.locale_outcomes:
            click.echo(f"{translation_id}: nothing ready to pull")
            continue
        for locale_outcome in result.locale_outcomes:
            summary = ", ".join(
                f"{name}={locale_outcome.counts[name]}"
                for name in engine.OUTCOME_ORDER
                if locale_outcome.counts.get(name)
            )
            click.echo(f"{translation_id} [{locale_outcome.locale}]: {summary or 'nothing to do'}")

    if had_error:
        raise SystemExit(1)


@cli.command()
@pass_ctx
@guarded
def status(ctx: Context):
    """Show tracked translations, their active generation, and last pull counts."""
    translation_ids = ctx.state.list_translation_ids()
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
        click.echo(f"{translation_id}: generation={generation['id']} status={generation['status']} pulls=[{pull_str}]")


if __name__ == "__main__":
    cli()
