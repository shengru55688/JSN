# JSN

JSN (Job Search and Notification system) collects LinkedIn job alert emails from a dedicated Gmail label, deduplicates listings in SQLite, scores new jobs with an LLM, and posts one weekly summary thread to a Discord Forum.

JSN reads LinkedIn alert emails and attempts to fetch public job descriptions. Missing work details are shown as `未提供`, and each entry links to its original LinkedIn listing.

## Configuration

Create your private `config/settings.yaml` from `config/settings.example.yaml`, then set the Gmail label, skills, desired job titles, location and schedule requirements, WFH preferences, score threshold, and database retention age. Add at least one skill or job title before running. The personal settings file is ignored by Git; do not replace it with the example after customizing it.

The Gmail label defaults to `LinkedIn Job Alerts`. Create a Gmail filter that applies this label to LinkedIn job alert emails, and make sure the label is visible in IMAP in Gmail's label settings. The reader opens the label read-only and does not mark messages as read.

LLM providers are selected with `llm.provider` in `config/settings.yaml`. The default example uses Google AI Studio through the `google-genai` SDK; it needs an API key and model name, but no endpoint URL. The `openai_compatible` provider remains available for services that implement Chat Completions and uses `LLM_OPENAI_COMPATIBLE_URL`. Benefits, schedule, and work mode without supporting evidence in the supplied source text are displayed as `未提供`.

## Run locally with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run all commands below from the repository root.

### 1. Install dependencies and create local configuration

```sh
uv sync
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
```

Run the copy commands only for first-time setup so you do not overwrite existing local settings. Edit `config/settings.yaml` with your job-search profile and Gmail label. Add at least one skill or job title. The file is ignored by Git and is not included in the repository.

### 2. Prepare Gmail IMAP access

Enable 2-Step Verification for your Google Account, then create an [app password](https://support.google.com/accounts/answer/185833) for JSN. Put the Gmail address in `GMAIL_ADDRESS` and the app password in `GMAIL_APP_PASSWORD` in `.env`. Use the same account that receives the LinkedIn alerts. Google Workspace administrators or some account security settings can prevent the use of app passwords; check your account settings if the option is unavailable.

Gmail app passwords allow broad IMAP access to the account, so keep the password private. Google can revoke app passwords after account security changes; create a new one if login stops working. For existing users, the reader preserves Gmail message IDs so previously processed messages in the SQLite database remain deduplicated.

### 3. Configure the remaining services and run JSN

Set `LLM_API_KEY` to your Google AI Studio API key and `LLM_MODEL` to a model available to your account in `.env`. The sample settings use `provider: google_ai_studio`; no API URL is needed. For another OpenAI-compatible provider, change the provider in `config/settings.yaml` and set `LLM_OPENAI_COMPATIBLE_URL`. Also set the Discord Forum webhook URL. Make sure the Gmail label named by `gmail.label_name` exists and is applied to LinkedIn job alert emails. Then run the main pipeline:

```sh
uv run --env-file .env jsn
```

To inspect the exact data payload before making LLM requests, run:

```sh
uv run --env-file .env jsn --preview-llm-input
```

The preview prints up to three JSON payloads containing the job data, profile, and filters sent to the LLM. It attempts to fetch public LinkedIn job descriptions first and shows whether each fetch succeeded. It exits without calling the LLM or posting to Discord and does not mark newly fetched email as processed; fetched descriptions may be cached in the local database. If there are no new emails, it previews existing pending jobs from the local database. Pass a number to change the preview limit, for example `--preview-llm-input 10`.

To test the LLM connection while sending only one job, run:

```sh
uv run --env-file .env jsn --test-llm
```

This mode ingests new Gmail alerts into the local database, then sends exactly one pending job to the configured LLM. It saves and prints that job's result, then exits without scoring other jobs or posting to Discord. The selected job is the newest pending job, with a job whose LinkedIn description was just fetched preferred. A failed LLM request leaves the job pending so you can retry it. Invalid JSON or provider errors may trigger retries. The regular `uv run --env-file .env jsn` command still scores all pending jobs.

To exercise one complete Gmail-to-Discord flow, run:

```sh
uv run --env-file .env jsn --test-pipeline
```

This reads one recent email from the configured label, parses its first job card, optionally fetches its public LinkedIn description, evaluates it with the LLM, and posts a real Discord thread titled `【流程測試】`. Only jobs with a `pass` or `review` decision appear in the report; a rejected job produces an empty test report. It uses a temporary SQLite database, so the normal database and processed-message state are unchanged. If no email or job card is found, it exits without calling the LLM or Discord. Each run can create another test thread and consume LLM quota.

To search recent job alerts until a job needs review, run:

```sh
uv run --env-file .env jsn --test-find-review
```

This evaluates unique jobs one at a time and posts the first `review` result in a Discord thread titled `【待確認職缺測試】`. It uses a temporary database and leaves the normal processing state unchanged. If no job needs review, it does not post. It can make multiple LLM requests and consume substantial quota.

To test Discord separately, run:

```sh
uv run --env-file .env jsn --test-discord
```

This creates one clearly labelled test thread in the Forum channel configured by `DISCORD_FORUM_WEBHOOK_URL`. It does not read Gmail, call the LLM, or access the SQLite database. The command can also take a different webhook environment variable name, for example `--test-discord MY_DISCORD_WEBHOOK`.

To post a report-format test thread with three already-scored jobs, run:

```sh
uv run --env-file .env jsn --test-discord-report
```

This posts a clearly labelled test thread using the same formatting as the weekly report. The first post contains the summary, then each eligible job appears in its own reply. An unusually long job continues in the next reply with its title and a continuation number. Discord Forum replies are a flat list, so continuation replies appear immediately after their job's reply rather than nested under it. The command only selects jobs already scored as `pass` or `review`; it makes no LLM calls and does not mark jobs as reported in SQLite. If fewer than three eligible jobs are available, it stops without posting. Pass a number to change the required count, for example `--test-discord-report 1`.

Before scoring, the full pipeline attempts to fetch descriptions from up to three pending LinkedIn job pages per run. It caches successful descriptions and retries unsuccessful fetches after seven days. Only publicly accessible LinkedIn pages are fetched; the program does not send login cookies or bypass access restrictions. The full pipeline then sends job-alert text and any fetched descriptions to the configured LLM for scoring, and may publish the current week's report to the configured Discord Forum. Local runs use the local SQLite database. Tune `job_details.max_per_run`, `job_details.timeout_seconds`, `job_details.retry_after_days`, and `llm.min_request_interval_seconds` in `config/settings.yaml` if needed. To run the unit tests separately, use `uv run pytest`.

The `.env` and `config/settings.yaml` files are private local configuration and are ignored by Git. Keep them on your machine; do not commit or share them.

## GitHub Actions setup

The workflow runs every Monday at 08:00 Taiwan time and supports manual runs. GitHub Actions schedules use UTC; change the cron expression in `.github/workflows/weekly.yml` to change the time.

Add these repository secrets and variables before enabling the workflow:

- Secrets `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` for Gmail IMAP.
- Secret `JSN_SETTINGS_YAML` containing the full YAML content of your private settings; set `database.path` to `/tmp/jsn/jobs.sqlite3` for the Actions runner.
- Secrets `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` for an R2 S3 API token with access to the state object.
- Variables `R2_ACCOUNT_ID`, `R2_BUCKET_NAME`, and `R2_OBJECT_KEY` (for example, `jsn/jobs.sqlite3`).
- Secret `LLM_API_KEY` and variable `LLM_MODEL` for the selected LLM provider and model. For Google AI Studio, use a Google AI Studio API key and choose `provider: google_ai_studio` in the private settings file.
- Variable `LLM_OPENAI_COMPATIBLE_URL` only when `llm.provider` is `openai_compatible`.
- Secret `DISCORD_FORUM_WEBHOOK_URL` for a webhook attached to the Discord Forum channel.

For Google AI Studio, `llm.min_request_interval_seconds` sets the minimum time between request starts. It defaults to 5 seconds (about 12 requests per minute) to stay below a 15 requests-per-minute free-tier limit. A Google `429` response is retried using Google's suggested wait time when available. Both supported providers retry a `503` response once after waiting for `llm.min_request_interval_seconds`. Requests from other apps using the same API key still count toward the same quota.

The SQLite database stores processed email IDs, deduplicated listings, scores, and posted report weeks. GitHub Actions restores it from a private Cloudflare R2 object to `/tmp/jsn/jobs.sqlite3` before running JSN, then uploads it back after the run, including when the collection step fails. The workflow does not commit the database to the repository. For local runs, set `database.path` in your private settings to an absolute path outside the checkout if you also want the local database outside the repository.

## Discord report behavior

Each ISO week gets one Forum thread. If a report already exists for that week, another manual run will process new email but will not create a duplicate weekly thread; unreported jobs remain eligible for the next report.
