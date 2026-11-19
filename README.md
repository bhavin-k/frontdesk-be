# Frontdesk backend

FastAPI and a background worker run CV intake, job matching, and recruiter review. PostgreSQL stores business data, applications, and jobs.

## What works today

- Business accounts, recruiter access, and job openings with versioned requirements.
- CV uploads and configured Gmail or Outlook intake, with duplicate detection, local PaddleOCR for PDF, Word, JPEG and PNG files.
- A role-agnostic CV profile and one fit result per opening that was active when the CV arrived.
- Recruiter Select and Reject decisions, with configurable retention for candidate data.
- An opt-in email scheduling pilot for one test business. Recruiters can review screening threads and bookings in **Screening calls**.

## How the worker runs

The API handles authenticated requests and queues work in PostgreSQL. Run the worker as a separate process to sync mailboxes, process CVs, and run retention jobs. The worker records results and retries temporary failures.

## Folder structure

```text
src/recruitment/
  api/        FastAPI routes and request schemas
  services/   Workflows for intake, matching, and review
  domain/     Rules for scoring, evidence, and retention
  ports/      Interfaces for providers and the job queue
  adapters/   PostgreSQL, mailbox, AI, and document integrations
  jobs/       Worker dispatch and job handling
  core/       Settings and shared backend utilities
  prompts/    AI prompt templates
  assets/     Tokenizer data
alembic/      Database migrations
scripts/      Local startup and maintenance commands
tests/        Backend checks and fictional fixtures
docs/         Configuration guides and architecture decisions
docker/       Gateway config for compose.phase2.yml
```

## Run locally

Install Python 3.13 and uv 0.10.3, then start Docker. From the backend folder, run the API in one terminal.

```powershell
uv sync --frozen
uv run python scripts/test_database.py start
uv run python scripts/migrate.py upgrade
uv run python scripts/dev.py api
```

From the same folder, start the worker in a second terminal.

```powershell
uv run python scripts/dev.py worker
```

Open the [API docs](http://127.0.0.1:8000/docs). On a fresh checkout, this setup uses a disposable database and fake providers. Use fictional CVs.

Use `.env.example` to configure live services. [AI setup](docs/ai-configuration.md) Â· [Mailboxes and document services](docs/phase2-operations.md)

## Run the email screening pilot

For the diagram, candidate reply conditions, statuses, and timing rules, read the [email screening workflow](docs/email-screening-workflow.md).

Apply Alembic revision `0017_screening_mailbox` before starting the API and worker. For a shared database, back it up and stop the old workers before the upgrade. Run `uv run --frozen python scripts/migrate.py upgrade head` with the schema-owner URL in `RECRUITMENT_MIGRATION_DATABASE_URL`.

1. Set `RECRUITMENT_SCREENING_AVAILABILITY_FILE=config/screening-availability.json` to use the tracked [pilot availability file](config/screening-availability.json) from the backend folder. Alternatively, copy it into `.local/screening-availability.json` for private local settings. Set the working days, hours, and blackout dates for the test business. The default call length is 20 minutes with a 10-minute buffer. Restart the worker after changes.
2. Set `RECRUITMENT_SCREENING_TEST_TENANT_ID` to the test business UUID in the backend API and worker environment. Leave it blank to disable the pilot. Include `imap` in `RECRUITMENT_ENABLED_MAILBOX_METHODS` and configure the credential key ring for both processes.
3. In the test business's **Integrations**, choose **Connect Gmail with IMAP (testing)**, verify with a Google App Password, and confirm the Inbox. This connection collects CVs, sends screening invitations, and receives replies. The worker uses the encrypted App Password from the connection. Remove the old screening `SMTP_*` and `IMAP_*` credentials from `.env`.
4. Use real mailbox and LLM provider modes to exercise the pilot with fictional CVs and recipient addresses the test team controls. Shortlist a CV and open **Screening calls**. The thread shows its sending mailbox. Replies and confirmations stay in the same email thread. The test business hiring team makes the call.

An emailed CV uses its intake mailbox. An uploaded CV uses the opening's unique connected Gmail IMAP mailbox when several are eligible, or the only eligible connection in the business. Unavailable or ambiguous connections require recruiter review. Existing threads keep their original sender. The migration links legacy threads to a connected Gmail IMAP mailbox with the same address and business. Gmail and Outlook OAuth have intake access only in this pilot.

Only the configured business's new shortlist decisions queue invitations. Workers configured for the pilot claim its screening jobs alongside their normal work. The configured grader model classifies replies, and Frontdesk renders English email templates. When SMTP cannot confirm whether it accepted a message, the request shows **Needs recruiter** for review. The pilot sends up to 30 messages per hour by default.

For a database that predates the CV JSON pipeline, follow the [revision 0015 cutover instructions](docs/ai-configuration.md#upgrade-an-existing-database) before enabling the pilot.

## Run with a shared Neon database

Ask your database operator for the four restricted connection URLs after they apply the backend migrations. Set these variables in `backend/.env` using `postgresql+psycopg://` and keep the TLS options from the supplied URLs.

| Variable | PostgreSQL role |
| --- | --- |
| `RECRUITMENT_DATABASE_URL` | `recruitment_api` |
| `RECRUITMENT_AUTH_DATABASE_URL` | `recruitment_auth` |
| `RECRUITMENT_WORKER_DATABASE_URL` | `recruitment_worker` |
| `RECRUITMENT_DISPATCHER_DATABASE_URL` | `recruitment_dispatcher` |

For a local frontend, set `RECRUITMENT_AUTH_DATABASE_URL` in `frontend/.env.local` to the same database. Use the `recruitment_auth` role with `postgresql://`. Keep both files out of Git.

Dashboard reads batch related records and select only response fields. Each backend transaction sets UTC, tenant and user context together with transaction-local settings. The frontend keeps idle auth connections for up to two minutes to avoid reconnecting between visits; it still validates sessions on requests. No shared session or tenant-data cache is introduced. Remote connection setup and network latency can still affect cold requests.

The Candidates page uses the authenticated `GET /api/v1/dashboard-bootstrap` read, with an optional `tenant_id`. It returns current memberships, the authorized selected business and the first 20 candidates. Authentication and memberships share one auth transaction; candidate data uses the existing API-role transaction and forced RLS. A failed candidate read returns a sanitized `candidate_error` and no candidate page, allowing an explicit retry without presenting an empty list as a successful result. Existing list and decision endpoints remain available.

For a remembered business, its RLS-protected read can overlap membership loading after the session is validated. Each task owns its database session, and results are published only after both access checks succeed. Development timing stages can overlap, so their durations should not be added to calculate total request time.

API startup prepares one API-role connection and one auth-role connection before reporting startup complete. Preparation time is logged separately from request time. Development bootstrap and liveness responses expose numeric `Server-Timing` diagnostics; they contain no credentials, candidate details or SQL. Normal startup commands and database URLs are unchanged.

From `backend/`, install dependencies and start the API. Use this command for Neon because `node scripts/dev.mjs` supplies local database URLs.

```powershell
uv sync --frozen
uv run --frozen recruitment-api
```

An existing worker connected to the shared database can process jobs from every API instance. To run a worker, configure its [AI providers](docs/ai-configuration.md) and [mailbox services](docs/phase2-operations.md), then start it in a separate terminal from `backend/`.

```powershell
uv run --frozen recruitment-worker
```
