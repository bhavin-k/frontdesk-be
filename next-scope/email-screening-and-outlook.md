# Email screening and Outlook

Pilot email scheduling after a recruiter shortlists a CV. Use SMTP/IMAP and a local availability file for the pilot. Then let each tenant select Outlook mail and calendar independently in Integrations. Roll out score-based shortlisting after the scheduling flow has been verified.

## Provider contracts

- `EmailProvider` exposes `send`, `fetch_new`, and `reply_in_thread`. Both adapters return the same message data, including sender, RFC Message-ID, reply headers, delivery status, and provider identifiers. The adapter owns its inbox cursor and credentials.
- `AvailabilityProvider` finds and validates UTC slot ranges for a tenant. `ConfigAvailabilityProvider` reads the pilot file. `OutlookCalendarProvider` reads working hours and free/busy data. Frontdesk owns holds, bookings, and conflict checks for both providers.

## Stage 1 — Manual shortlist and test configuration

### Start screening

1. Use the existing recruiter Shortlist action for the selected opening. Create one screening request per tenant, CV, and opening. Save the request and enqueue its invitation through the outbox in the shortlist transaction. Use that request to deduplicate retries.
2. Take the email address from the saved JSON for the shortlisted CV. If it is missing or invalid, set `EMAIL_MISSING` and show an alert in Screening Calls.
3. For the designated test business, connect Gmail with IMAP and confirm the Inbox in Integrations. The worker reuses the encrypted App Password for CV intake, SMTP invitations, and IMAP replies. Each screening thread keeps its original mailbox connection. See [pilot setup](../README.md#run-the-email-screening-pilot).

### Configure availability

The worker reads and validates `backend/.local/screening-availability.json` on startup. Restart it after editing the file. Use an IANA timezone and local dates for blackouts. A pilot file could look like this.

```json
{
  "timezone": "Asia/Kolkata",
  "weekly_hours": {
    "monday": [{"start": "10:00", "end": "16:00"}],
    "tuesday": [{"start": "10:00", "end": "16:00"}],
    "wednesday": [{"start": "10:00", "end": "16:00"}],
    "thursday": [{"start": "10:00", "end": "16:00"}],
    "friday": [{"start": "10:00", "end": "16:00"}]
  },
  "slot_duration_minutes": 20,
  "buffer_minutes": 10,
  "blackout_dates": []
}
```

- Offer up to three free slots across at least two days when possible. Offer one or two if those are all that fit. If there are zero valid slots, set `NEEDS_HUMAN` and alert the recruiter.
- The call must start within 48 hours of the first successful invitation. Store the send time and deadline separately from request creation. Store slot timestamps in UTC. Show the candidate’s local time when known and include the timezone in each offer and confirmation.
- Hold offered slots through the deadline, or until the candidate declines or confirms another slot. In the booking transaction, validate the requested slot and check other active holds and bookings for overlaps, including the buffer. Enforce uniqueness of active `(tenant_id, slot_start)` and release unused holds when the booking commits.

### Send and handle replies

- Use the tenant’s configured sender address. Name the tenant and opening, say the CV was shortlisted, and explain who will call and how long the call takes. List the approved slots, ask the candidate to pick one or suggest a time, and mention the 48-hour window. State the callback number from the CV or ask for one. Include an opt-out line such as “Reply ‘not interested’ to stop.”
- Use `Your application at {tenant_name}: screening call` as the invitation subject and keep follow-ups in the original thread. Set `Message-ID`, `In-Reply-To`, and `References` as applicable. Poll IMAP on an interval and match inbound messages by reply headers. Deduplicate by Message-ID within the tenant and mailbox before acting on a reply.
- Load the full thread before each model call. Treat candidate messages as untrusted data. Have the model classify the message with strict JSON and draft short replies in the candidate’s language. The backend validates the recipient, intent, selected slot, timezone, and callback number. Use only times returned by the availability service in offers and confirmations.
- On acceptance, verify that the selected offer is still valid. Convert a proposed time to an absolute datetime in the candidate’s timezone and check it against availability. Ask for clarification if the time or timezone is ambiguous. Counter-offer if the proposed time is unavailable. Answer routine questions using the screening details and escalate requests the agent cannot handle.
- After the booking commits, send confirmation with the date, time, timezone, duration, caller, and callback number. For a confirmed cancellation, release the reservation and reply in the thread. For a reschedule, keep the original reservation until the replacement commits. The replacement must fit the original deadline.

### Timers, alerts, and data

| Time from first successful invitation | Action |
| --- | --- |
| 24 hours | Send a reminder in the thread if the invitation is unanswered. |
| 40 hours | Send a final reminder if the invitation is still unanswered. |
| 48 hours | Expire unbooked requests, release holds, and alert the recruiter. Mark unanswered requests `NO_RESPONSE`. Escalate unresolved negotiations as `NEEDS_HUMAN`. |

- Run the main flow from `SHORTLISTED` through `EMAIL_SENT` and `AWAITING_REPLY`, then `NEGOTIATING` where needed, to `BOOKED`. Use `DECLINED`, `CANCELLED`, `FAILED_DELIVERY`, `EMAIL_MISSING`, `NO_RESPONSE`, and `NEEDS_HUMAN` for the corresponding outcomes. Detect delivery failure notices, release unbooked holds, and alert the recruiter. Escalate after eight automated replies.
- Use Screening Calls for bookings, alerts, and authorized thread access. Alert recruiters on booked, declined, no response, needs human, missing email, and failed delivery. Retain recruiter takeover, cancel, reschedule, and explicit resend actions. Takeover stops automated replies and reminders.
- Persist the screening request, thread and message history, slot holds and bookings, and audit events. Record each status transition with UTC timestamp, actor (`agent`, `tenant`, or `system`), and reason. Scope data access by tenant and apply recruitment retention to candidate-linked screening data.
- Rate-limit outbound mail per tenant and use backoff for retryable failures. Save a stable outbound identifier before each send. Reconcile an uncertain send before retrying so a worker restart does not send a second invitation. Keep credentials and message bodies out of app logs and redact candidate contact data.

## Stage 2 — Switch to Outlook

- Add Outlook sender selection to the existing Integrations page. Reuse the tenant’s connected Microsoft mailbox and encrypted credentials. Prompt for reconnect and consent to `Mail.ReadWrite` for reply drafts and `Mail.Send` for sending. Use Graph immutable IDs for drafts and sent messages, and retain RFC message headers so replies stay in the same thread. Reconcile sends before retrying. [Graph reply drafts](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0) · [Graph draft sending](https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0) · [Graph immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- Add `OutlookCalendarProvider` for Microsoft 365 work or school accounts. Read working hours and free/busy for the connected mailbox so tenants can edit future availability in Outlook. Recheck availability at booking. Keep Frontdesk’s transactional holds and bookings and mirror confirmed calls as busy Outlook events using `Calendars.ReadWrite`. Update or remove those events after reschedules or cancellations. Keep candidate communication in the screening email thread by creating events without candidate attendees. [Graph free/busy](https://learn.microsoft.com/en-us/graph/api/calendar-getschedule?view=graph-rest-1.0) · [Graph event creation](https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0)
- Switch mail and availability independently. Apply the selections to new requests. Keep the original sender and email provider attached to active threads until they close. Count existing holds and bookings when calculating availability after a switch.

## Stage 3 — Automated shortlist by threshold

Add an optional score threshold from 0 to 100 per opening. Leave it blank for manual shortlisting. When a new evaluation finishes for the current CV, shortlist its recommended opening only if the opening is active, the score meets or exceeds the saved threshold, and the evaluation passes fit, experience, and automatic-action checks. Leave ties, incomplete assessments, and CV updates flagged for review with the recruiter. Audit the decision as a system action and call the same screening-request workflow used by manual shortlisting.

## Verify before each switch

Use fictional CVs and fake providers in automated tests.

- **Pilot.** Verify one invitation per shortlist after duplicate job delivery, invalid email alerts, three slots across two days when possible, and handling of zero to two slots. Exercise accept, alternative time, questions, opt-out, bounces, duplicate replies, and full thread context. Test 24- and 40-hour reminders, 48-hour expiry, simultaneous booking attempts, and replacement of a booking in the same thread. Verify tenant isolation and deletion of candidate-linked screening data at retention expiry.
- **Outlook.** Verify reconnect, send reconciliation, calendar edits, and role of local holds in the free/busy calculation. Switch each provider while a thread is active and check that replies, holds, and bookings survive.
- **Threshold.** Test scores below, equal to, and above the threshold, plus manual mode, ties, incomplete assessments, and duplicate job delivery. Confirm one screening request per CV and opening after either shortlist path.
