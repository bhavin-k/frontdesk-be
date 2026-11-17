# Frontdesk backend

FastAPI and a background worker run CV intake, job matching, and recruiter review. PostgreSQL stores business data, applications, and jobs.

## What works today

- Business accounts, recruiter access, and job openings with versioned requirements.
- CV uploads and configured Gmail or Outlook intake, with duplicate detection, local PaddleOCR for PDF, Word, JPEG and PNG files.
- A role-agnostic CV profile and one fit result per opening that was active when the CV arrived.
- Recruiter Select and Reject decisions, with configurable retention for candidate data.

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

Use `.env.example` to configure live services. [AI setup](docs/ai-configuration.md) · [Mailboxes and document services](docs/phase2-operations.md)


For an existing database, stop the API and worker and take a backup before [applying revision 0015](docs/ai-configuration.md#upgrade-an-existing-database).

## Run with a shared Neon database

Ask your database operator for the four restricted connection URLs after they apply the backend migrations. Set these variables in `backend/.env` using `postgresql+psycopg://` and keep the TLS options from the supplied URLs.

| Variable | PostgreSQL role |
| --- | --- |
| `RECRUITMENT_DATABASE_URL` | `recruitment_api` |
| `RECRUITMENT_AUTH_DATABASE_URL` | `recruitment_auth` |
| `RECRUITMENT_WORKER_DATABASE_URL` | `recruitment_worker` |
| `RECRUITMENT_DISPATCHER_DATABASE_URL` | `recruitment_dispatcher` |

For a local frontend, set `RECRUITMENT_AUTH_DATABASE_URL` in `frontend/.env.local` to the same database. Use the `recruitment_auth` role with `postgresql://`. Keep both files out of Git.

From `backend/`, install dependencies and start the API. Use this command for Neon because `node scripts/dev.mjs` supplies local database URLs.

```powershell
uv sync --frozen
uv run --frozen recruitment-api
```

An existing worker connected to the shared database can process jobs from every API instance. To run a worker, configure its [AI providers](docs/ai-configuration.md) and [mailbox services](docs/phase2-operations.md), then start it in a separate terminal from `backend/`.

```powershell
uv run --frozen recruitment-worker
```
