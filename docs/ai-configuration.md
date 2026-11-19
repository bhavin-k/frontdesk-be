# Configure CV processing

The worker reads CVs with local CPU PaddleOCR. The parser turns the raw text into one role-agnostic CV profile. The matcher grades evidence against the saved JD checklist from the CV's arrival time. Backend code calculates fit, score, matched and missing requirements, the experience check, and the explanation. Recruiters see coverage and evidence alongside the score.

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

Use `RECRUITMENT_GRADER_CHAT_DISABLE_THINKING=true` for the same option on JD matching. Both options default to `false` in application settings. The example enables them for its configured endpoint.

The parser preserves compact project evidence in the existing profile fields. The matcher credits academic projects where the JD accepts them and gives preferred qualifications less weight. Backend code compares documented professional years with the saved numeric minimum. A failed minimum forces `exp_ok=false`, `fit=false`, and a score of at most 14, with the required and documented years in the reason. A blank or zero minimum applies no numeric floor.

`RECRUITMENT_CV_TOKEN_CEILING` limits extracted CV text and defaults to 20,000. `RECRUITMENT_LLM_REQUEST_TOKENS` and `RECRUITMENT_LLM_OUTPUT_TOKENS` default to 32,768 and 3,072. Set them within the provider's limits. Unreadable files and invalid or exhausted model responses appear in recruiter review.

The worker logs provider-reported input, output and total tokens for each CV parser and JD matcher response that includes usage. Add the parser total and one matcher total per opening to get the usage for a CV. Include any extra responses from retries.

## Coverage scoring

Apply revision `0018_consistent_scoring` before starting the updated API or worker. When a recruiter saves a JD, the configured grader model creates a required/preferred checklist. The backend combines duplicate labels and saves the checklist in the tenant's immutable cache. Identical JD content reuses that checklist. Older intake periods keep their frozen JD text; the first unfinished CV prepares their checklist once. Completed assessments retain their original scores.

The `coverage-v1` policy awards required criteria 1 for met, 0.5 for partial, and 0 for undocumented. Required criteria have equal weights. Backend code interpolates the base score through `(0%, 0)`, `(50%, 50)`, `(75%, 81)`, `(90%, 90)`, and `(100%, 96)`. Preferred coverage adds up to two points. Distinct additional skills with documented, role-relevant evidence add one point each, up to two. Required gaps cap the score at 94. Experience below the saved minimum caps it at 14 and forces not fit. Otherwise, coverage of at least 75% qualifies as fit.

The model groups related JD tools and responsibilities into capabilities. It assesses demonstrated work, project outcomes, and transferable responsibilities rather than counting exact keywords. Relevant academic implementations can meet fresher requirements. A matching role title alone does not qualify a CV, and explicit mandatory tools still require evidence. The model's evidence judgments remain reviewable in the dashboard.

Each new CV keeps its arrival-time JD snapshot and scoring policy version. New openings include their saved checklist in that snapshot. Evaluations also record the model configuration. One evidence-grading call per opening returns criterion IDs, grades, and IDs from the supplied CV evidence. The backend resolves those IDs to original profile text and stores the grades with the calculation. Invalid IDs or incomplete grades require review. Saved results and completed roles survive retries; repeated requests make no new grading call. Fresh model judgments can differ, so repeatability comes from reusing the saved grades.

The CV profile format stays unchanged. The matcher receives compact evidence entries without the profile's contact fields. Long summaries retain all their project details across consecutive entries. JD preparation adds one model request per new JD content version, separate from each CV's parser and grading requests. Token logs identify it as `jd-checklist-v1`; matching uses `jd-evidence-v1`.

## Upgrade an existing database

Stop the API and worker and take a restorable database backup. Apply the migration using the existing migration role and database URL.

~~~powershell
uv run python scripts/migrate.py upgrade
uv run python scripts/migrate.py check
~~~

Revision 0015 removes the vector extension and data used exclusively by the retired scoring pipeline. It preserves CV originals, profiles, historical match scores and reasons, and recruiter decisions. The migration reconstructs opening scope for unfinished CVs from their receipt times and the retained intake periods. Earlier JD versions were not stored for those periods, so the migration uses the JD available at cutover. New intake records the JD as it stood when the CV arrived.

For an existing instance hosted on a vector image, move the upgraded database to stock PostgreSQL 16 with a logical dump and restore, preserving restricted roles and grants. Verify readiness before restarting workers. Restore the pre-upgrade backup and previous app version if a rollback is required. The empty database bootstrap creates revision 0015 directly on stock PostgreSQL 16.
