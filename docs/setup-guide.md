# Setup guide

This guide takes you from nothing to a first round trip: Klaviyo → Smartling → Klaviyo.
Plan on about 30 minutes, most of it spent creating credentials.

- **Who it's for:** a developer or technical localization manager who can run a command-line tool.
- **What you need:**
  - Klaviyo account admin access, to create an API key.
  - A Smartling Project Manager (or higher) role on the project you'll use.

## 1. Check the prerequisites

| Requirement | How to check |
|---|---|
| Python 3.11 or newer | `python3 --version` |
| Git (pip uses it to install from GitHub) | `git --version` |
| Klaviyo Translations (beta) enabled on your account | Your Klaviyo account team can confirm access |
| Smartling plan that includes API access | Ask your Smartling account manager if unsure |
| A Smartling project whose target locales match the languages you translate in Klaviyo | Smartling dashboard → project settings |

## 2. Create a Klaviyo private API key

1. In Klaviyo, open **Settings → API keys** and create a **private** API key.
2. Give it **custom scopes**, with at least:
   - **Translations:** Read and Write (`translations:read`, `translations:write`).
   - Optionally, to sync by campaign/flow/tag/template instead of listing ids
     yourself: `campaigns:read`, `flows:read`, `tags:read`, `templates:read`.
3. Copy the key. It starts with `pk_`, and it's shown only once.

Use a dedicated key for this tool, so you can revoke it on its own.

## 3. Create a Smartling API token

1. In Smartling, open **Account Settings → API** and create an API token.
   - **Prefer a project-scoped token** limited to the one project you'll sync. It can't touch other projects.
2. Note the **User Identifier** and the **Token Secret**. The secret is shown only once.
3. Note the **Project ID**. It appears in the project's settings, and in the URL when you open the project in the dashboard.

## 4. Enable target locales on your Klaviyo content

The tool sends and writes only locales that are both mapped in `config.yaml` and enabled on the Klaviyo translation. Locales that aren't enabled are skipped with a warning when you **push**. A pull writes the locales that were included in the push it follows, so if you disable or unmap a locale, push again before the next pull.

If you use `--create-missing` (see [Sync a whole campaign / flow / tag / template](#sync-a-whole-campaign--flow--tag--template)), the translations it creates are enabled for every locale in your `config.yaml` automatically; this step only matters for translations that already exist.

To enable a locale, turn it on for the message or template in Klaviyo's translation settings. Alternatively, PATCH the translation's `target_locales` through the API.

## 5. Install

The tool isn't published to PyPI yet. Install it from GitHub into a virtual environment:

```bash
python3.12 -m venv .venv      # any Python 3.11+; the macOS system python3 may be older
.venv/bin/pip install "git+https://github.com/klaviyo-labs/klaviyo-translation-connectors"
.venv/bin/klaviyo-tc --help   # confirms the install
```

Optionally, give it a short alias. The absolute path means the `ktc` command is found from any directory:

```bash
alias ktc="$PWD/.venv/bin/klaviyo-tc"                          # this terminal only
echo "alias ktc=\"$PWD/.venv/bin/klaviyo-tc\"" >> ~/.zshrc     # every new terminal (or ~/.bashrc)
ktc --help
```

This guide writes the full `.venv/bin/klaviyo-tc` path, and `ktc` works anywhere it appears. Cron jobs and scripts don't read shell aliases, so keep the full path there.

The tool still reads `config.yaml` and keeps its state in `.klaviyo-tc/` **relative to the directory you run it from**. Always run it from the same directory (the one with your `config.yaml`), for manual pushes and scheduled pulls alike, so they share one state database.

To work on the code itself, clone the repo and run `.venv/bin/pip install -e ".[dev]"` instead.

## 6. Configure

In the directory where you'll run the tool:

```bash
.venv/bin/klaviyo-tc init      # writes config.yaml (in a clone of the repo you can also copy config.example.yaml)
```

Then edit `config.yaml`:

```yaml
providers:
  smartling:
    project_id: abc123def          # from step 3

locales:                            # Klaviyo locale: Smartling locale
  fr: fr-FR
  de: de-DE
  es: es-ES
```

- The keys under `locales:` are Klaviyo locale codes, as they appear in a translation's `target_locales`.
- The values are the Smartling locale IDs for your project.
- Only mapped locales are sent. A pull writes the locales from the push it follows, so push again after changing this mapping.
- Leave the other settings at their defaults unless you have a reason to change them.
- `config.yaml` holds no secrets, so it's safe to keep in your own deployment repo or config management.
- The tool refuses to load a config file with a key that looks like a secret
  (`api_key`, `secret`, `token`, etc. anywhere in it) -- secrets only ever
  come from the environment variables in the next section.

Secrets **never** go in the config file. Export them as environment variables:

```bash
export KLAVIYO_API_KEY="pk_..."
export SMARTLING_USER_IDENTIFIER="..."
export SMARTLING_USER_SECRET="..."
```

For scheduled runs, load these from your secret manager or CI secrets rather than a shell profile.

## 7. Find the translations to sync

Each Klaviyo translation has an ID, such as `campaign-variation::email::01KH...` or `template::email::XyZ123`.

List the first 100 of your account's translations (`-g` stops curl treating the brackets as a pattern):

```bash
curl -sg "https://a.klaviyo.com/api/translations/?page[size]=100" \
  -H "Authorization: Klaviyo-API-Key $KLAVIYO_API_KEY" \
  -H "revision: 2026-07-15.pre" \
  -H "accept: application/vnd.api+json" | python3 -m json.tool | grep '"id"'
```

If you have more than 100, the response's `links.next` URL returns the next page. It's usually easier not to list ids at all: sync by tag, campaign, flow or template instead (see [Sync a whole campaign / flow / tag / template](#sync-a-whole-campaign--flow--tag--template)), or everything in a channel with `push --all --channel email` (or `sms`, `mobile_push`, `whatsapp`).

## 8. Dry run

Preview what would be sent. A push dry run doesn't call Smartling and doesn't write to Klaviyo or the state database:

```bash
.venv/bin/klaviyo-tc push --id "template::email::XyZ123" --dry-run
```

The output lists each string key and its source text. Check that:

- the strings are the ones you expect;
- `{{ ... }}` and `{% ... %}` template tags appear inside the text. Smartling is told to protect them as placeholders.

## 9. First push

```bash
.venv/bin/klaviyo-tc push --id "template::email::XyZ123"
```

Each push creates one Smartling job, named `Klaviyo bulk <timestamp> <short id>` (scoped pushes use the scope, for example `Klaviyo: <tag name> <short id>`). The job holds one file per translation, named after the translation id with `::` replaced by `__`, for example `klaviyo/template__email__XyZ123.json`. It is authorized for your mapped locales.

- **In Smartling:** confirm the job appears and its strings look right.
- **Pushing again:** once the source has changed, pushing the same translation again uploads the new source to the same file.
- **Unchanged strings:** Smartling reuses its translation memory for them.

## 10. Translate, then pull

Once your linguists have published translations in Smartling:

```bash
.venv/bin/klaviyo-tc pull --dry-run   # preview: reads Smartling and Klaviyo, writes nothing to Klaviyo
.venv/bin/klaviyo-tc pull             # write into Klaviyo
.venv/bin/klaviyo-tc status
```

`pull` reads per-locale progress, so partly finished locales are picked up. It classifies every string and prints a count for each outcome, then lists the translations that need attention. Add `--verbose` for counts per translation and locale:

- `written`: sent to Klaviyo.
- `unchanged`: Klaviyo already has this translation.
- `conflict`: someone edited the value in Klaviyo after the push, or since the tool last wrote it. The Klaviyo version is kept. Rerun with `--force` to overwrite it. Values that were already there at push time are replaced without a conflict. For example, Klaviyo pre-fills a new translation with text it already has for matching strings elsewhere in the account.
- `stale_source`: the Klaviyo source text changed after the push. Push again.
- `placeholder_mismatch`: the translation adds, drops or changes a `{{ ... }}` variable, or adds, drops or reorders a `{% ... %}` tag. Variables may move within the sentence. Fix it in Smartling.
- `deleted` or `unknown_key`: the content no longer exists in Klaviyo, or wasn't part of the push.

Finally, preview the message in Klaviyo in each language before sending.

## 11. Run it on a schedule

Run `pull` periodically, for example every 15 to 30 minutes, and `push` whenever content is ready for translation.

The tool keeps its sync state in `.klaviyo-tc/state.db`. **The state must persist between runs**: it records what was sent and written. A pull only fetches translations this state says were pushed, so without it a scheduled pull finds nothing to do and imports nothing, and the record of what was written is also what protects Klaviyo edits. Run the tool on a machine or container with durable storage.

Example cron entry:

```cron
*/20 * * * * cd /opt/klaviyo-tc && ./.venv/bin/klaviyo-tc pull >> pull.log 2>&1
```

The environment variables must be available to the cron job, for example through a wrapper script that loads them from your secret manager.

Ephemeral CI runners such as GitHub Actions don't keep files between runs. If you use one, save and restore `.klaviyo-tc/state.db` yourself: as a cache or artifact, or in object storage. Otherwise every run starts with no history, and conflict protection can't work.

## Sync a whole campaign / flow / tag / template

Instead of listing translation ids yourself (step 7), you can point `push`/`pull`/`status`
at a campaign, a flow, a tag, a template, or universal content, and the tool resolves the
translations for you:

```bash
.venv/bin/klaviyo-tc push --campaign 01ABC...                # one campaign
.venv/bin/klaviyo-tc push --campaign 01ABC... --flow 01DEF... # combine campaigns and flows
.venv/bin/klaviyo-tc push --tag "Spring Launch"               # every tagged campaign and flow
.venv/bin/klaviyo-tc push --templates --name-contains "Sale"  # every template matching a filter
.venv/bin/klaviyo-tc push --all-universal-content              # every universal content block
```

A scoped `push` submits everything it resolves as **one** Smartling job, not one job per
translation, and skips any translation whose content hasn't changed since its last submitted
push (pass `--force-resend` to override that). `--create-missing` creates a Klaviyo translation
for a resolved campaign/flow/template/universal-content resource that doesn't have one yet;
without it, resources with no translation are reported as `no_translation` and left alone.
`--template`/`--templates` translations default to the `email` channel; pass
`--template-channel whatsapp` for WhatsApp templates. A `SIMPLE`/`CODE` template is still
synced, but flagged `single_html_body` in the summary since it stores one HTML body value
rather than per-string content.

How campaigns, flows and tags are resolved:

- **Campaigns** use Klaviyo's GA Campaigns API. Each campaign message's id is its translation's
  `campaign-variation` id. A campaign the GA API can't find is retried through the beta (omni)
  Campaigns API, which lists variations per message; that fallback hasn't been checked against a
  live omni campaign yet.
- **Flows** walk flow → flow-action → flow-message, and **tags** read the tag's campaign and flow
  relationships.
- If a request 404s or comes back empty where you expect data, check
  [`klaviyo_tc/klaviyo.py`](../src/klaviyo_tc/klaviyo.py) and
  [`klaviyo_tc/scopes.py`](../src/klaviyo_tc/scopes.py), and please open an issue.

`--dry-run` never writes to Klaviyo: with `--create-missing` it reports `would_create` instead.

Campaigns, flows, tags, templates and universal content are GA APIs with their own Klaviyo API revision,
set in `klaviyo.revisions` in `config.yaml`. Translations use the beta `klaviyo.revision`, as does the fallback for campaigns the GA API can't find.

By default, `push` authorizes the Smartling job for your mapped locales as it uploads each
file. Pass `--no-authorize` (or set `providers.smartling.authorize: false` in `config.yaml`)
to create the job unauthorized instead, and authorize it by hand in Smartling.

To authorize into a specific Smartling workflow (for example a machine-translation workflow)
rather than each locale's default, set `providers.smartling.workflow_uid` in `config.yaml`.
Find the workflow UID in Smartling under **Project Settings → Workflows**. It applies to every
mapped locale, and is ignored when the job is created unauthorized.

## Performance and load

Each command fetches translations in parallel, `klaviyo.concurrency` at a time (default 4, maximum 32).
Fetching a translation with its values makes Klaviyo render the message, which takes about 2 seconds
for a large drag-and-drop email. On a whole-account sweep (`--all`, `--templates` with no filter),
raising concurrency can push those renders past Klaviyo's time limit, and the tool reports them as errors.
Prefer scoped runs (`--tag`, `--campaign`, `--templates --updated-since`) for routine syncs.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` / `403` from Klaviyo | The key is missing the `translations:read`/`write` scopes, or your account doesn't have the Translations beta |
| `locale 'xx' not enabled on translation, skipping` | Enable that locale on the Klaviyo translation (step 4), or remove it from `locales:` |
| Smartling authentication error | Wrong User Identifier or Token Secret, or the token was revoked |
| Smartling `429` | Rate limited. The tool retries with backoff; for large pushes, push fewer translations at a time |
| Many `stale_source` outcomes | The source was edited after pushing. Push again, then pull after the new strings are translated |
| `pull` finds nothing to do after a push | It ran from a different directory, or the state database is missing or was reset (see step 11). Run from the directory holding `config.yaml` |
| `requires a different Python` during install | Your `python3` is older than 3.11. Create the virtual environment with `python3.11` or `python3.12` |
| Errors mentioning a timeout or `GetResourcesV2` on large runs | Klaviyo took too long rendering messages. Lower `klaviyo.concurrency`, or scope the run (`--tag`, `--updated-since`) |
| A scope reports `no_translation` | Those messages have no Klaviyo translation yet. Add `--create-missing` |
| `placeholder_mismatch` | A linguist changed a `{{ }}` or `{% %}` tag. Correct it in Smartling and publish again |

## Security notes

- Credentials are read only from environment variables. They are never stored in config or state, and they're redacted from output.
- Use a dedicated Klaviyo key and a project-scoped Smartling token, and rotate both periodically.
- Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).
