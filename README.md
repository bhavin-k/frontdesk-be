# Recruitment backend

FastAPI and a background worker run CV intake, job matching, and recruiter review. PostgreSQL stores business data, applications, and jobs.

## What works today

- Business accounts, recruiter access, and job openings with versioned requirements.
- CV uploads and configured Gmail or Outlook intake, with duplicate detection, document parsing, and OCR when needed.
- Experience checks and full-text scoring against job criteria, with verified CV quotes and source locations.
- Recruiter Select and Reject decisions, with configurable retention for candidate data.

## How the worker runs

The API handles authenticated requests and queues work in PostgreSQL. Run the worker as a separate process to sync mailboxes, prepare job requirements, process CVs, and run retention jobs. The worker records results and retries temporary failures.

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
docker pull pgvector/pgvector@sha256:0dd3149a06e4579a0d70d8fef9945636cd514005e7237deb27f9b67886f8dc4f
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
