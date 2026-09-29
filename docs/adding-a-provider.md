# Adding a translation provider

`klaviyo_tc.core` never imports a specific provider. It talks to whichever one
is configured through `klaviyo_tc.providers.base.Provider`:

```python
class Provider(Protocol):
    name: str

    def submit(self, *, translation_id, file_name, strings, target_locales,
               reference, state=None, checkpoint=None) -> SubmitResult: ...
    def submit_many(self, *, run_ref, job_name, files: list[FileSpec],
                     state=None, checkpoint=None) -> SubmitResult: ...
    def completed_locales(self, state: dict) -> list[str]: ...
    def fetch(self, state: dict, locale: str) -> dict[str, str]: ...
```

- `submit` sends (or resumes sending) `strings` (a flat `value_id -> source_value`
  dict) for translation into `target_locales` (provider-side locale codes).
  `reference` is a stable id (the sync engine's generation id) safe to use for
  idempotency (e.g. fold it into a remote job name, since names are often
  unique per project). `state` is whatever your own `submit` returned last
  time this translation was pushed and didn't finish (e.g. after a crash);
  return `None` from your own state on first use. The engine persists
  `SubmitResult.state` as opaque JSON on its own state row and passes it back
  on the next `submit` call, so encode enough there (job ids, upload status,
  etc.) to resume correctly. Call `checkpoint(state)` after each remote step
  that changes `state` (e.g. right after a job or batch is created) so the
  engine persists that progress immediately, before the next step runs --
  otherwise a crash between two steps re-does work that already succeeded
  remotely. `checkpoint` may be `None` in tests; guard for that. Set
  `SubmitResult.submitted = False` if more `submit` calls are still needed
  before the engine should consider the push complete.
- `submit_many` submits a whole scope run (campaign/flow/tag/`--all`/multiple
  `--id`) as **one** provider job instead of one job per translation. `files`
  is a list of `FileSpec(translation_id, file_name, strings, target_locales)`.
  `run_ref` and `state`/`checkpoint` mirror `submit`'s resumability contract,
  but at the run level (e.g. job id, per-batch upload progress). Set
  `SubmitResult.file_states[file_name]` for every file: it's the opaque state
  `completed_locales`/`fetch` will later receive for that one translation's
  generation -- the run-level `state` may hold bookkeeping (job/batch ids)
  that has nothing to do with any single file, so the engine never reuses it
  directly. `submit` may implement itself via `submit_many` with a single
  `FileSpec` (see `SmartlingProvider.submit` for a worked example).
- `completed_locales` reports which of the submitted locales have translations
  ready to download, given the same `state` dict.
- `fetch` returns the translated strings for one locale. Keys absent or empty
  are treated as still untranslated.

Your provider owns everything about its own wire format and API (file
envelopes, directives, auth) -- none of that belongs in `klaviyo_tc.core`.

## Steps

1. Create `klaviyo_tc/providers/<name>/` with your HTTP client and a class
   implementing `Provider` (see `klaviyo_tc/providers/smartling/` for a
   worked example: `client.py` is the plain REST client, `provider.py`
   adapts it to the protocol).
2. Register it in `klaviyo_tc/providers/registry.py`.
3. Add a `providers.<name>` section to the config schema
   (`klaviyo_tc/core/config.py`'s `TEMPLATE`, mirrored in `config.example.yaml`)
   for whatever settings your API needs (project id, format directives, etc.)
   -- never secrets; `load_config` rejects any key that looks like one.
4. Wire secret env vars and construction for your provider in
   `klaviyo_tc.cli.Context.provider`.
5. Add tests (`click.testing.CliRunner` driving the CLI against `respx`-mocked
   HTTP) asserting on the requests your provider makes and on the resulting
   state, mirroring `tests/test_push.py` / `tests/test_pull.py` /
   `tests/test_scoped_bulk_sync.py`.

Nothing else changes: the sync engine, outcome classification (`unchanged`,
`conflict`, `stale_source`, `placeholder_mismatch`, `deleted`, `unknown_key`),
and CLI commands work with any provider that satisfies the protocol above --
see `tests/test_fake_provider.py` for a minimal in-memory provider exercised
through the same engine.
