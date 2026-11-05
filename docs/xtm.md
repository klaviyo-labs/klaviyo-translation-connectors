# Using XTM Cloud

`klaviyo-tc` can use [XTM Cloud](https://xtm.cloud/) as its translation management system (TMS)
in place of Smartling. Install the tool, create the Klaviyo key and find your translations as in the
main [setup guide](setup-guide.md). This page covers what's different for XTM: credentials, config,
and how a push and a pull map onto XTM projects.

> **Status: early access.** The XTM provider was built and tested against XTM's published REST API
> reference (version 26.4), but it hasn't yet been run against a live XTM instance. Try it on a test
> customer first, and see [Things to confirm on your instance](#things-to-confirm-on-your-instance).

## How it maps onto XTM

| `klaviyo-tc` | XTM |
|---|---|
| One `push` run (a tag, a campaign, `--all`, ...) | One **project**, owned by your configured **customer** |
| One Klaviyo translation (a message) | One JSON **source file** in that project |
| One target locale of that message | One **job** (file × target language) |
| `pull` | Reads each job's completion status, then generates and downloads the **target file** for finished jobs |

- A language is pulled once its job is **finished**, meaning it has completed every step of the
  workflow. Languages that are still in progress are skipped and picked up on a later `pull`.
- A push waits for XTM to **analyse** the uploaded files (parse them into segments). If analysis
  fails, the push fails and names the job. Run the same push again to resume.
- By default, the tool doesn't start the XTM workflow, so a project manager can review the project
  first. Set `start_workflow: true` to start it automatically. `push --no-authorize` overrides that
  for one run.

## 1. Get an API user

You need a user with API access and the project manager role (`pm`, or `customer_pm`) for the
customer you'll use.

1. In XTM, open **Users**, pick the user, and open **Access rights**. Set **Access through** to
   **User Interface and API** or **Only API**. This option appears only if your instance has an API licence.
2. Note the user's numeric **User ID**: click the blue **i** icon next to the user.
3. Note your **client name**, the company name you type at the XTM login page.

The tool authenticates with XTM-Basic (`POST /auth/token` with client name, user ID and password)
and sends `Authorization: XTM-Basic <token>` on every request.

## 2. Find your customer, template or workflow, and languages

- **Customer ID:** in XTM, open **Customers**. Every project is created for this customer.
- **Pick one of these:**
  - A **project template ID** (**Configuration → Project templates**). The template supplies the
    workflow and settings. Recommended, because it lets XTM admins change the workflow without
    touching this tool.
  - A **source language plus a workflow**, by ID (`workflow_id`) or by name (`workflow_name`).
    Workflows are under **Configuration → Workflow definitions**.
- **Language codes:** XTM uses codes like `fr_FR`, `de_DE` and `pt_BR`, with an underscore.
  The full list is in the [XTM language codes reference](https://help.xtm.cloud/en/xtm-cloud/26.4/en/language-codes.html).

## 3. Protect Klaviyo template tags

Klaviyo content contains HTML and template tags such as `{{ first_name }}` and
`{% if ... %}`. XTM handles HTML inside JSON by default, but it doesn't know Klaviyo's tag syntax.
Unless the tags are locked, linguists can edit them. `pull` catches that and reports
`placeholder_mismatch`, but it's better to prevent it.

Ask your XTM administrator (or XTM Support, {{EMAIL}}) for a JSON file filter template
that marks these patterns as non-translatable inline tags:

```
\{\{[^}]+\}\}
\{%[^%]+%\}
```

Put its ID in `providers.xtm.filter_template_id`, or attach it to the project template you use.

## 4. Configure

Run `klaviyo-tc init`, then edit `config.yaml`:

```yaml
provider:
  name: xtm

providers:
  xtm:
    # Your XTM URL plus /project-manager-api-rest
    base_url: https://yourcompany.xtm-intl.com/project-manager-api-rest
    customer_id: 123
    template_id: 456            # or leave 0 and set source_language plus workflow_id/workflow_name
    source_language: en_US
    workflow_id: 0
    workflow_name: ""
    filter_template_id: ""
    start_workflow: false
    files_per_upload: 50        # source files per upload request

locales:                        # Klaviyo locale: XTM language code
  fr: fr_FR
  de: de_DE
```

For XTM Cloud's shared instance, `base_url` is `https://api.xtm-cloud.com/project-manager-api-rest`.
To check which API version your instance runs, open `<your XTM URL>/rest-api` in a browser.

Then provide credentials as environment variables. Never put them in the config file: the loader
rejects any config key that looks like a password, secret or token.

```bash
export KLAVIYO_API_KEY="pk_..."
export XTM_CLIENT="YourCompany"
export XTM_USER_ID="12345"
export XTM_PASSWORD="..."
```

`XTM_PASSWORD` and the issued token are redacted from all output.

## 5. Push and pull

The commands are the same as for Smartling:

```bash
klaviyo-tc push --tag "Ready for translation" --create-missing --dry-run
klaviyo-tc push --tag "Ready for translation" --create-missing
# linguists translate in XTM until each job finishes its workflow
klaviyo-tc pull --tag "Ready for translation"
klaviyo-tc status
```

The project is named after the run (for example `Klaviyo- tag Ready for translation 1a2b3c4d`,
with characters XTM doesn't allow in names replaced by `-`). Its **reference ID** is the tool's
run ID.

## Resume and re-push behaviour

- The tool saves the project ID right after XTM creates the project, and records each upload as it
  goes. If a push is interrupted, run the same command again: it continues in the same project
  and only re-sends files that weren't uploaded or whose content has changed since.
- If the tool stopped between XTM creating the project and the tool saving its ID, the next run
  finds the project by its exact name (for the same customer) and uses it, instead of creating a
  second one.
- Re-uploads use `matchType=MATCH_NAMES`, so a changed file replaces the earlier upload instead of
  being added under a new name, and `reanalyseProject=YES`, so XTM parses it again.
- Changing a message after it was submitted, then pushing again, creates a **new** project for
  the new content (a new run). This is the same as Smartling, where each run gets its own job.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `XTM API error 401` | Wrong client name, user ID or password, or the user has no API access (step 1). XTM issues a token even for bad credentials, so the error appears on the first real call |
| `XTM API error 400: ...` on push | The reason comes from XTM: often an unknown customer, template or workflow, or a language the customer isn't set up for |
| `analysis failed for jobs` | XTM couldn't parse a file. Check the job in XTM; usually a file filter problem |
| `could not start workflow ... COST_NOT_APPROVED` | Your XTM setup requires cost approval before starting. Approve it in XTM, or use `start_workflow: false` |
| `no job for <file> in [xx_XX]` | XTM didn't create a job for that language. Check the customer's language combinations and the template's languages match `locales:` |
| A language never pulls | Its job hasn't finished every workflow step. Check the job's status in XTM |
| `placeholder_mismatch` | A `{{ }}` / `{% %}` tag changed in translation. Fix it in XTM, finish the job, and pull again; set up tag protection (step 3) |

## Things to confirm on your instance

These come from XTM's documentation, but haven't been run against a real instance yet:

- **Multiple target languages** are sent as one comma-separated `targetLanguages` field, as in
  XTM's knowledge-base example. If your instance needs the field repeated instead, project
  creation will fail with a 400.
- **Job file names:** pull matches jobs to messages by `fileName` from the project status. If your
  instance changes uploaded file names (for example by adding a path), report it.
- **Target ZIP layout:** the downloaded ZIP is searched for the file with the uploaded name, and
  otherwise for its only `.json` file.
- **Rate limits:** XTM doesn't document any. The tool retries 429, 500, 502, 503 and 504 with backoff.

## References

- XTM REST API reference: https://api.xtm-cloud.com/project-manager-api-rest/ (your instance: `<XTM URL>/rest-api`)
- Often-used API methods with examples: https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/4220158521
- Running your first REST API method (authentication): https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/4221304837
- Handling embedded syntax in source files: https://xtm-cloud.atlassian.net/wiki/spaces/EXKB/pages/4665704449
