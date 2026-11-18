# Configure CV processing

The worker reads CVs with local CPU PaddleOCR. The parser turns the raw text into one role-agnostic CV profile. The matcher receives that profile with contact details masked and a job description from the CV's arrival time. It returns fit, score, matched and missing requirements, an experience check, and a reason.

## Install PaddleOCR for the worker

From the backend folder, run:

~~~powershell
uv sync --frozen --extra ocr
~~~

The extra pins PaddleOCR 3.7.0 and PaddlePaddle 3.3.1. Use Python 3.13 on a platform with a compatible CPU wheel, such as Windows x64 or Linux x64. Download the two official inference archives and extract their directories into the worker's `RECRUITMENT_OCR_MODELS_DIR`, which defaults to `.local/paddleocr` relative to the backend working directory.

| Archive | SHA-256 |
| --- | --- |
| [PP-OCRv5 mobile detection](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_infer.tar) | 50446e5d01ac2a73d5319c89513281f6578414c888c602f9af13f93feefffc58 |
| [PP-OCRv5 English mobile recognition](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/en_PP-OCRv5_mobile_rec_infer.tar) | e595b4cf2ffad19fbb5a61ba345d63939577a3ab8717b6e5995642590c9101b4 |

Keep each archive's `*_infer` directory and its inference files. The worker loads those files from disk. Start [ClamAV and the document converter](phase2-operations.md) to scan files and convert DOC or DOCX inputs to PDF before OCR.

## Set the LLM connection

Set `RECRUITMENT_PROVIDER_MODE=real` and configure `RECRUITMENT_LLM_API_KEY`, `RECRUITMENT_LLM_BASE_URL`, and `RECRUITMENT_LLM_PROVIDER`. The default endpoint is the OpenAI endpoint and accepts `RECRUITMENT_OPENAI_API_KEY` as a fallback. A custom HTTPS endpoint requires its own key.

Set `RECRUITMENT_PROFILE_MODEL` for CV parsing and `RECRUITMENT_GRADER_MODEL` for matching. The default API format selects Responses for OpenAI and Chat Completions for other endpoints. Use `json_schema` for structured output where available. Chat Completions also supports `json_object` and `text`. The backend validates both outputs locally in every mode.

Set `RECRUITMENT_PROFILE_CHAT_DISABLE_THINKING=true` to disable thinking for CV parsing on Chat Completions endpoints that support the `thinking` field. Set it to `false` when switching to an endpoint that does not support the field.

`RECRUITMENT_CV_TOKEN_CEILING` limits extracted CV text and defaults to 20,000. `RECRUITMENT_LLM_REQUEST_TOKENS` and `RECRUITMENT_LLM_OUTPUT_TOKENS` default to 32,768 and 3,072. Set them within the provider's limits. Unreadable files and invalid or exhausted model responses appear in recruiter review.

The worker logs provider-reported input, output and total tokens for each CV parser and JD matcher response that includes usage. Add the parser total and one matcher total per opening to get the usage for a CV. Include any extra responses from retries.

## Upgrade an existing database

Stop the API and worker and take a restorable database backup. Apply the migration using the existing migration role and database URL.

~~~powershell
uv run python scripts/migrate.py upgrade
uv run python scripts/migrate.py check
~~~

Revision 0015 removes the vector extension and data used exclusively by the retired scoring pipeline. It preserves CV originals, profiles, historical match scores and reasons, and recruiter decisions. The migration reconstructs opening scope for unfinished CVs from their receipt times and the retained intake periods. Earlier JD versions were not stored for those periods, so the migration uses the JD available at cutover. New intake records the JD as it stood when the CV arrived.

For an existing instance hosted on a vector image, move the upgraded database to stock PostgreSQL 16 with a logical dump and restore, preserving restricted roles and grants. Verify readiness before restarting workers. Restore the pre-upgrade backup and previous app version if a rollback is required. The empty database bootstrap creates revision 0015 directly on stock PostgreSQL 16.
