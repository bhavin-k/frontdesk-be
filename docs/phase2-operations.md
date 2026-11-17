# Configure Phase 2 intake

Connect Gmail or Microsoft mailboxes, start the document services and configure CV processing.

## Choose local or real providers

Use fake providers and fictional CVs for local testing. Fake mailbox OAuth redirects within the local application, and the dashboard labels fake AI and mailbox modes.

For real OAuth intake, configure `RECRUITMENT_MAILBOX_PROVIDER_MODE=real`, provider registrations, a public HTTPS callback host and the independent credential key ring. Configure `RECRUITMENT_PROVIDER_MODE=real` for AI, local PaddleOCR and ClamAV processing. Production rejects either fake mode.

The API and worker read `.env` from the backend working directory. Process environment variables take precedence. Dedicated tests use their own database and provider settings. Store provider credentials in the server environment or a secret manager.

## Start malware scanning for real intake

Start the pinned ClamAV scanner from the backend root before processing real uploads or mailbox attachments.

```powershell
docker compose -f compose.phase2.yml --profile scanner up -d clamav
docker compose -f compose.phase2.yml --profile scanner ps clamav
```

Wait for `healthy` before sending test CVs. The first start loads and updates signature databases. The service binds only to `127.0.0.1:3310`, matching the backend defaults, and keeps signatures in a dedicated Docker volume. It restarts with Docker unless explicitly stopped. The image is pinned to ClamAV 1.5.4 and an immutable digest, with a 4 GiB memory limit following [ClamAV's Docker guidance](https://docs.clamav.net/manual/Installing/Docker.html).

If email intake shows `SCANNER_UNAVAILABLE`, the mailbox may have received the message while the CV counter remains unchanged: attachment scanning happens before CV storage. The worker retries bounded transient failures, then sends the attachment to review. Restore the scanner before an operator retries failed work through the application queue. A normal mailbox sync does not reset an exhausted attachment job.

An `AI_REQUEST_FAILED` after the original is stored is a separate provider issue. For example, an HTTP 401 from Groq means its configured key was rejected. Replace `RECRUITMENT_LLM_API_KEY` in `backend/.env`, restart the API and worker, and retry the failed processing through the operator workflow. Keep keys out of chat and logs; never switch real CVs to fake processing to hide an unavailable provider.

## PDFs containing embedded attachments

Readable PDFs can contain source files or other embedded attachments. The backend scans and preserves the original, then creates and scans a page-only copy before extraction or OCR. It never opens or indexes nested files. Extraction provenance records `+pdf-pages-v1` when the processing copy differs from the original. [ADR 0017](adr/0017-pdf-processing-copies.md) describes the policy and limits.

`INVALID_DOCUMENT` still blocks encrypted, unreadable or active PDFs and documents outside the supported bounds. `PDF_SANITIZATION_FAILED` sends a failed page-copy operation to review. A normal mailbox sync does not replay an exhausted attachment job; an operator must enqueue a fenced recovery through the existing outbox. Duplicate files, changed CVs and cross-role submissions retain their existing review rules.

## Register Gmail and Microsoft OAuth

Use the dashboard's public HTTPS origin for the app, OAuth callbacks and notifications. For local testing with ngrok, forward dashboard port **3100**. The Next.js notification bridge forwards provider requests to the private FastAPI service on port 8000.

Control available methods with `RECRUITMENT_ENABLED_MAILBOX_METHODS`. Defaults allow `gmail_oauth,microsoft_oauth`. Disabling a method removes its connection option and blocks its API actions, scheduling and worker ingestion. Existing connection history stays visible with a disabled notice. Candidates and evidence remain available.

Register these exact redirect paths on your application host:

- Gmail: `/api/recruitment/oauth/gmail/callback`
- Microsoft: `/api/recruitment/oauth/microsoft/callback`

Set `RECRUITMENT_PUBLIC_APP_URL` to that host. The existing Next.js authentication bridge receives the browser callback and forwards its code and state server-side. The backend requires the initiating recruiter and business membership.

Gmail needs an OAuth client, Gmail read-only permission and a Pub/Sub topic with Gmail watch publishing enabled. Configure authenticated push delivery to `/api/v1/mailbox-webhooks/gmail` on the public application host. Set the expected JWT audience and push service-account email.

Microsoft needs a confidential OAuth application with delegated `Mail.Read` and `User.Read`. MSAL manages offline access and PKCE. Set `RECRUITMENT_PUBLIC_WEBHOOK_URL` to the public application origin. The worker creates `/api/v1/mailbox-webhooks/microsoft/{connection_id}` subscriptions and verifies their client state. The Next.js gateway preserves Graph's POST validation handshake.

Every connection belongs to one business. The recruiter confirms an opening and source folder before collection starts. Only messages received from that point qualify. Reauthorization preserves progress; an explicit disconnect/reconnect or folder change starts a fresh source period. Shared Microsoft mailboxes remain outside Phase 2.

## Test with an actual Gmail Inbox through IMAP

In `backend/.env`, enable the testing method and real mailbox access:

```dotenv
RECRUITMENT_MAILBOX_PROVIDER_MODE=real
RECRUITMENT_ENABLED_MAILBOX_METHODS=imap,gmail_oauth,microsoft_oauth
RECRUITMENT_IMAP_POLL_SECONDS=60
RECRUITMENT_IMAP_TIMEOUT_SECONDS=10
RECRUITMENT_CREDENTIAL_ACTIVE_KEY=1
```

Configure `RECRUITMENT_CREDENTIAL_KEYS` as described below. Reuse an existing key ring rather than replacing it. If you have no key ring, run this once from `backend/` and store the output in that variable:

```powershell
uv run python -c "import base64,json,secrets; print(json.dumps({'1':base64.b64encode(secrets.token_bytes(32)).decode()}))"
```

Keep the value in the ignored backend `.env` or a secret manager. Never paste it into chat or source control. Set `RECRUITMENT_CREDENTIAL_KEYS` to the generated JSON object. Do not change this key on each restart.

1. Enable Google 2-Step Verification, then create an [App Password](https://myaccount.google.com/apppasswords). Some managed accounts restrict this option; see [Google's account requirements](https://support.google.com/mail/answer/185833?hl=en).
2. Restart both backend API and worker using the existing root launcher, and refresh the dashboard. IMAP needs no public callback, Pub/Sub topic or OAuth application registration.
3. Create an active opening, then open **Integrations → Connect Gmail with IMAP (testing)**.
4. Enter the Gmail address and App Password. Use the App Password field for this credential; the server encrypts it for that business. The application does not return it in API responses.
5. Verify the connection and confirm Inbox to start collection. The displayed IST time marks the beginning of eligible mail.
6. Send a supported CV from a different email address after collection starts. Self-sent mail is ignored. Inspect **Email intake**, **Candidates** and quoted evidence. **Check for new email** queues an immediate poll.

The adapter reads Inbox over verified TLS at `imap.gmail.com:993`, without marking messages as read. It checks exact message receipt time before downloading bodies. The original CV limit remains 10 MB; complete MIME emails have a 32 MiB bound. Oversized or malformed messages flag a mailbox failure; move the offending email out of Inbox before retrying. They never cause candidate rejection. Refresh expired credentials through **Authorize again → Update App Password** to preserve the cutoff and cursor.

Mailbox and AI modes are separate. Fake mode uses simulated CV profiles and match results. Use fictional CVs until you configure and verify real AI/OCR/scanning providers.

## Switch from IMAP to OAuth

Configure the Google or Microsoft OAuth registration above, then set:

```dotenv
RECRUITMENT_ENABLED_MAILBOX_METHODS=gmail_oauth,microsoft_oauth
```

Restart **both** API and worker, stopping instances that still use the old environment, then refresh the app. IMAP disappears from new connection options. The backend also blocks existing IMAP sync/configuration/credential actions and skips queued or scheduled intake. Existing IMAP cards show a disabled notice and allow disconnect.

Disconnect the old IMAP connection and wait for **Disconnected**. This removes its encrypted credential and releases the mailbox ownership reservation. Authorize Gmail or Outlook and confirm its folder. A new connection starts collecting from its activation time; it does not import history or mail received during the switching gap. CV deduplication still shares originals/applications across sources. Candidate data stays available under the existing retention policy. Revoke the unused App Password in Google account settings after disconnecting.

No additional migration is needed when changing the allowlist after installing revision `0011_mailbox_connection_methods`. Production mode rejects an allowlist containing IMAP. An empty allowlist disables all connection methods.

## Supply and rotate credential keys

Store `RECRUITMENT_CREDENTIAL_KEYS` as a JSON object whose values are base64-encoded random 32-byte keys. Set `RECRUITMENT_CREDENTIAL_ACTIVE_KEY` to its active entry. Generate and inject these through your secret manager; the repository contains no example credentials.

Keep older entries while stored credentials still use them. Newly saved credentials use the active key. Rotation requires preserving the old key until each grant has been rewritten or reauthorized. Business and connection identifiers bind ciphertext to its owner.

## Run isolated document conversion

Start the pinned Gotenberg service from the backend root.

```powershell
docker compose -f compose.phase2.yml up -d
```

The compose file pins Gotenberg `8.39.0` and Nginx `1.28.0` by digest. A loopback-only gateway on port 8300 forwards conversion and health requests to the internal converter. Gotenberg uses a private internal network, a read-only filesystem and bounded temporary storage. Chromium routes, download-from and outbound webhooks are disabled. See [Gotenberg configuration](https://gotenberg.dev/docs/configuration) for these flags and [the gateway decision](adr/0011-isolated-document-conversion.md) for the network boundary.

Point `RECRUITMENT_DOCUMENT_CONVERTER_URL` at that private service. Keep it inaccessible from public traffic. The worker converts legacy DOC and DOCX requiring OCR, validates the resulting PDF, scans it and stores it as a PostgreSQL derivative. Originals retain their 10 MB limit; converted PDFs have a 20 MB/100-page limit. Conversion failures enter review.

[Configure PaddleOCR and both LLM stages](ai-configuration.md) before processing real CVs.

## Inspect failures and retention

Use **Integrations**, **Email intake**, **Review queue** and candidate details to inspect progress. The mailbox scheduler catches up every 15 minutes and flags an hour without a successful sync. Workers retry bounded transient failures; expired grants require reauthorization. Processing errors never finalize rejection.

A CV with an unreadable document, invalid profile or failed match remains available for recruiter review. The worker saves successful opening results so retries can resume without rescoring them.
