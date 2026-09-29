# Contributing

Thanks for your interest in `klaviyo-translation-connectors`.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Guidelines

- Keep `klaviyo_tc/core` free of any concepts specific to one provider (job/batch
  ids, file formats, directives). Provider-specific logic belongs under
  `klaviyo_tc/providers/<name>/`.
- Add tests through the CLI (`click.testing.CliRunner`) against mocked HTTP
  (`respx`); assert on the requests made and on state, not on internals.
- Run `.venv/bin/pytest -q` before opening a pull request.
- See `docs/adding-a-provider.md` before adding a new translation provider.

## Reporting bugs / requesting features

Open a GitHub issue with a clear description and, where relevant, the Klaviyo
API revision and provider you were using.

## Security

See `SECURITY.md` for how to report vulnerabilities.
