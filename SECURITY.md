# Security

Please report suspected security vulnerabilities to security@klaviyo.com
rather than opening a public issue. We will acknowledge your report and
follow up as soon as we can.

Never commit Klaviyo API keys, Smartling (or other provider) credentials, or
any other secret to this repository. This tool reads all secrets from
environment variables (`KLAVIYO_API_KEY`, `SMARTLING_USER_IDENTIFIER`,
`SMARTLING_USER_SECRET`) and never writes them to its config file, state
database, or logs.
