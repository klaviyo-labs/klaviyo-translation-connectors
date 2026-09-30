# klaviyo-translation-connectors

[![CI](https://github.com/klaviyo-labs/klaviyo-translation-connectors/actions/workflows/ci.yml/badge.svg)](https://github.com/klaviyo-labs/klaviyo-translation-connectors/actions/workflows/ci.yml)
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

| You need | Notes |
|---|---|
| Python 3.11 or newer | Check with `python3 --version`. On macOS the system `python3` may be older; use `python3.11`/`python3.12` explicitly. |
| Klaviyo Translations (beta) on your account | Your Klaviyo account team can confirm it's enabled. |
| A Klaviyo **private API key** | Scopes: `translations:read`, `translations:write`, plus `campaigns:read`, `flows:read`, `tags:read` and `templates:read` to sync by campaign, flow, tag or template. |
| A Smartling plan with API access | A **project-scoped** API token (User Identifier + Token Secret) and the project ID. |
| Somewhere to run it on a schedule | Any machine or container with **durable storage**: the tool keeps its sync state in `.klaviyo-tc/state.db`. |

**New here? The step-by-step [setup guide](docs/setup-guide.md) walks through creating each credential.**

## Quick start

```bash
# 1. Install (into its own virtual environment)
python3.12 -m venv .venv
.venv/bin/pip install "git+https://github.com/klaviyo-labs/klaviyo-translation-connectors"

# 2. Create config.yaml, then set your Smartling project_id and locale mapping in it
.venv/bin/klaviyo-tc init

# 3. Provide credentials as environment variables (never in the config file)
export KLAVIYO_API_KEY="pk_..."
export SMARTLING_USER_IDENTIFIER="..."
export SMARTLING_USER_SECRET="..."

# 4. Preview, then send one tagged set of campaigns and flows to Smartling
.venv/bin/klaviyo-tc push --tag "Ready for translation" --create-missing --dry-run
.venv/bin/klaviyo-tc push --tag "Ready for translation" --create-missing

# 5. After linguists publish in Smartling, write the translations into Klaviyo
.venv/bin/klaviyo-tc pull --tag "Ready for translation"
.venv/bin/klaviyo-tc status
```

`config.yaml` holds only non-secret ids. The tool refuses to load a config file
containing anything that looks like a secret (`api_key`, `user_secret`, `token`, ...),
and it never writes credentials to state or logs.

Always preview each language in Klaviyo before sending a translated message.

## Recommended workflow

1. **Tag content when it's ready.** Your team adds a Klaviyo tag such as
   `Ready for translation` to campaigns and flows. That is the only manual step.
2. **Push on demand or nightly:** `klaviyo-tc push --tag "Ready for translation" --create-missing`.
   Everything the tag covers goes to Smartling as **one job**; content that hasn't
   changed since its last push is skipped, and Smartling's translation memory covers
   strings it has seen before.
3. **Pull on a schedule**, e.g. every 20 minutes:

   ```cron
   */20 * * * * cd /opt/klaviyo-tc && ./run-pull.sh >> pull.log 2>&1
   ```

   where `run-pull.sh` loads the three environment variables from your secret
   manager and runs `.venv/bin/klaviyo-tc pull`.
4. **Review exceptions.** A pull prints one summary line, and lists only the
   translations that need a person (see [Outcome meanings](#outcome-meanings-pull)).

Templates work the same way: `push --templates --name-contains "..."` or
`--updated-since 2026-10-01`.

**Safety guarantees**

- A translation someone edits in Klaviyo *after* it was pushed is never
  overwritten; `pull` reports it as `conflict`. Values Klaviyo pre-fills into a
  new translation are replaced normally.
- A translation that adds, drops or changes a `{{ ... }}` or `{% ... %}` tag is
  rejected as `placeholder_mismatch`.
- If the English source changes after a push, `pull` skips it as `stale_source`
  until you push again.
- A push that fails part-way resumes the same Smartling job when you rerun it.
- Every command accepts `--dry-run`, and rerunning any command is safe.

## Typical commands

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
for a walkthrough. Campaigns resolve through Klaviyo's GA Campaigns API, where
a campaign message's id is its translation's `campaign-variation` id.

## Outcome meanings (`pull`)

| Outcome                | Meaning                                                          |
|-------------------------|-------------------------------------------------------------------|
| `written`               | Patched into Klaviyo.                                             |
| `unchanged`              | Already matches the current Klaviyo translation.                  |
| `conflict`               | Someone edited this translation in Klaviyo after it was pushed (or since the tool last wrote it); rerun with `--force` to overwrite. |
| `stale_source`           | The Klaviyo source string changed after this was pushed; re-push. |
| `placeholder_mismatch`   | `{{...}}`/`{%...%}` placeholders don't match the source; needs a linguist fix. |
| `deleted`                | The value no longer exists in Klaviyo.                             |
| `unknown_key`            | Key wasn't part of what this tool sent (unexpected TMS content).  |

## Limitations

- No webhooks: run `pull` on a schedule (e.g. cron) after linguists publish.
- It pins a **beta** Klaviyo API revision (`klaviyo.revision` in `config.yaml`).
  When Klaviyo retires it, update that value (and this tool).
- Whole-account sweeps (`--all`, or `--templates` with no filter) make Klaviyo
  render every message; keep `klaviyo.concurrency` low (default 4) and prefer
  scoped runs for routine syncs.
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
git clone https://github.com/klaviyo-labs/klaviyo-translation-connectors
cd klaviyo-translation-connectors
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`docs/adding-a-provider.md`](docs/adding-a-provider.md).
