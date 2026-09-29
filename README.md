# klaviyo-translation-connectors

[![CI](https://github.com/samviyo/klaviyo-translation-connectors/actions/workflows/ci.yml/badge.svg)](https://github.com/samviyo/klaviyo-translation-connectors/actions/workflows/ci.yml)
![coverage](https://img.shields.io/badge/coverage-see_pytest--cov_output-informational)

Reference connectors between [Klaviyo Translations](https://developers.klaviyo.com/)
(a **beta** API -- see [Limitations](#limitations)) and translation management
systems (TMS). `klaviyo-tc` pushes source strings out of Klaviyo Smart
Translations into your TMS and pulls finished translations back in.

This is provided as-is, as a starting point for your own integration. It is
**not an officially supported Klaviyo product**; it is not covered by Klaviyo
support SLAs, and it uses a beta Klaviyo API whose revision may change.

## Provider support

| Provider  | Status                |
|-----------|-----------------------|
| Smartling | Supported             |
| Phrase    | Planned / contributions welcome |
| Lokalise  | Planned / contributions welcome |
| Crowdin   | Planned / contributions welcome |
| XTM       | Planned / contributions welcome |

The sync engine (`klaviyo_tc/core/`) is provider-agnostic; see
[`docs/adding-a-provider.md`](docs/adding-a-provider.md) to add one.

## Prerequisites

- A Klaviyo private API key with the `translations:read` and
  `translations:write` scopes (`translations:write` is also what lets
  `--create-missing` create one). Add `campaigns:read`, `flows:read`,
  `tags:read`, and/or `templates:read` to use `--campaign`/`--flow`/`--tag`/
  `--template`/`--universal-content`.
- A Smartling plan with API access and a project-scoped API token
  (`userIdentifier` / `userSecret`).
- The target locales you want to translate enabled on each Klaviyo
  translation (`klaviyo-tc push` warns and skips any that aren't).

**New here? Follow the step-by-step [setup guide](docs/setup-guide.md).**

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Configure

```bash
cp config.example.yaml config.yaml   # or: .venv/bin/klaviyo-tc init
```

Edit `config.yaml`: set `providers.smartling.project_id` and map your Klaviyo
locales to Smartling locale ids under `locales:`. `config.yaml` itself is
git-ignored, since it holds account-specific ids; `config.example.yaml` is the
committed, placeholder-valued template both it and `init` are generated from.

Secrets are read only from environment variables, never from the config file,
state database, or logs. Loading a config file that has any key resembling a
secret (`api_key`, `user_secret`, `token`, etc., anywhere in the file) is a
hard error, on purpose:

```bash
export KLAVIYO_API_KEY=...
export SMARTLING_USER_IDENTIFIER=...
export SMARTLING_USER_SECRET=...
```

## Typical flow

```bash
klaviyo-tc push --all --channel email   # or --id <translation-id> ...
# linguists translate and publish in Smartling
klaviyo-tc pull                         # run on a cron once linguists publish
klaviyo-tc status                       # see active generations and last pull counts
```

`--dry-run` on `push`/`pull` computes and prints what would happen without
calling the provider or writing state.

## Scoped sync (campaign / flow / tag / template)

`push`/`pull`/`status` also accept a scope instead of `--id`/`--all`:

```bash
klaviyo-tc push --campaign <id> [--campaign <id> ...]   # combinable with --flow
klaviyo-tc push --flow <id> [--flow <id> ...]
klaviyo-tc push --tag "VIP"                              # every campaign and flow tagged "VIP"
klaviyo-tc push --template <id> [--template <id> ...]
klaviyo-tc push --templates --name-contains "Sale"        # every matching template
klaviyo-tc push --universal-content <id>
klaviyo-tc push --all-universal-content
klaviyo-tc push --campaign <id> --create-missing          # create a translation where one is missing
klaviyo-tc push --campaign <id> --force-resend             # resubmit even if nothing changed
klaviyo-tc push --campaign <id> --no-authorize              # create the job unauthorized in Smartling
klaviyo-tc pull --tag "VIP" --verbose                       # per-translation, per-locale detail
```

`--id`/`--all`/`--campaign`/`--flow`/`--tag`/`--template`/`--universal-content`
are mutually exclusive, except that `--campaign` and `--flow` may be combined
and repeated, and likewise `--template`/`--templates`/`--universal-content`/
`--all-universal-content`. A `push` over a scope submits everything it
resolves as **one** provider job (not one job per translation), skipping any
translation whose content hasn't changed since its last submitted push.
`--template`/`--templates` default to the `email` channel; pass
`--template-channel whatsapp` for WhatsApp templates. `SIMPLE`/`CODE`
templates are still synced but flagged as `single_html_body` in the summary
(see [Limitations](#limitations)). See
[`docs/setup-guide.md`](docs/setup-guide.md#sync-a-whole-campaign--flow--tag--template)
for a walkthrough, including the campaign/flow/tag relationship endpoints
this inferred from Klaviyo's general JSON:API conventions (not individually
verified against a live account -- see the caveat there and in
`klaviyo_tc/klaviyo.py`).

## Outcome meanings (`pull`)

| Outcome                | Meaning                                                          |
|-------------------------|-------------------------------------------------------------------|
| `written`               | Patched into Klaviyo.                                             |
| `unchanged`              | Already matches the current Klaviyo translation.                  |
| `conflict`               | Someone edited this translation in Klaviyo since the last pull; rerun with `--force` to overwrite. |
| `stale_source`           | The Klaviyo source string changed after this was pushed; re-push. |
| `placeholder_mismatch`   | `{{...}}`/`{%...%}` placeholders don't match the source; needs a linguist fix. |
| `deleted`                | The value no longer exists in Klaviyo.                             |
| `unknown_key`            | Key wasn't part of what this tool sent (unexpected TMS content).  |

## Limitations

- No webhooks: run `pull` on a schedule (e.g. cron) after linguists publish.
- SIMPLE/CODE templates store a single HTML body value per locale.
- Campaign/flow/tag scopes don't yet resolve universal content blocks *used
  inside* a campaign or flow message; an `--include-universal-content` flag
  for that is future work -- use `--universal-content`/`--all-universal-content`
  directly for now.
- Source edits after a push are reported as `stale_source` and skipped; re-push
  to pick up the new source text.
- Some TMS UIs only surface fully published locales; this tool reads
  per-locale completion directly so it can pick up partial progress.
- The Klaviyo API has no conditional write: `pull` re-fetches and re-checks a
  value immediately before PATCHing it, which narrows but cannot close the
  window for a concurrent edit in Klaviyo between that check and the write.

## Development

```bash
.venv/bin/pytest -q
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/adding-a-provider.md`](docs/adding-a-provider.md).
