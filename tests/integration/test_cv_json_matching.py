import io
from collections import Counter
from uuid import uuid4

import psycopg
import pytest
from PIL import Image

from recruitment.adapters.documents import DOCX
from recruitment.adapters.fakes import FakeMatcher
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import RemoteAttachment, RemoteMessage
from tests.integration.test_mail_intake import connect, fake, new_mail
from tests.support import docx_bytes, pdf_bytes


def add_opening(context, title, description="Python development", minimum=0):
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={
            "title": title,
            "job_description": description,
            "details": {"total_experience_years": minimum},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def patch_opening(context, opening, description):
    response = context.client.patch(
        f"{context.base}/openings/{opening['id']}",
        headers=context.headers,
        json={
            "title": opening["title"],
            "job_description": description,
            "expected_revision": opening["revision"],
        },
    )
    assert response.status_code == 200, response.text


def close_opening(context, opening):
    response = context.client.post(
        f"{context.base}/openings/{opening['id']}/actions",
        headers=context.headers,
        json={"action": "close", "expected_revision": opening["revision"]},
    )
    assert response.status_code == 200, response.text


def evaluation(context, upload_id):
    upload = context.client.get(
        f"{context.base}/cv-uploads/{upload_id}", headers=context.headers
    ).json()
    response = context.client.get(
        f"{context.base}/applications/{upload['application_id']}/match-results",
        headers=context.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["evaluation"]


@pytest.mark.parametrize("from_mailbox", [False, True])
def test_match_scope_is_the_arrival_snapshot(context, cv, monkeypatch, from_mailbox):
    frozen = add_opening(context, "Senior", minimum=20)
    to_close = add_opening(context, "Available on arrival")
    already_closed = add_opening(context, "Closed before arrival")
    close_opening(context, already_closed)
    parses = []
    calls = []
    parse = context.providers.profiles.profile
    match = context.providers.matcher.match

    def recording_parse(text):
        parses.append(text)
        return parse(text)

    def recording_match(job, profile):
        calls.append((job.title, job.description))
        return match(job, profile)

    monkeypatch.setattr(context.providers.profiles, "profile", recording_parse)
    monkeypatch.setattr(context.providers.matcher, "match", recording_match)
    if from_mailbox:
        connect(context)
        new_mail(context, cv)
    else:
        upload = context.upload(cv).json()["upload_id"]

    added = add_opening(context, "Created after arrival")
    patch_opening(context, frozen, "Excel reporting and spreadsheets")
    close_opening(context, to_close)
    context.drain()
    if from_mailbox:
        upload = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
            "items"
        ][0]["upload_id"]

    original = evaluation(context, upload)
    roles = {role["opening_title"]: role["match_result"] for role in original["roles"]}
    assert set(roles) == {"Python Developer", frozen["title"], to_close["title"]}
    assert roles["Senior"]["exp_ok"] is False and roles["Senior"]["fit"] is False
    assert ("Senior", "Python development") in calls
    assert original["recommendation"] == "needs_review"
    assert len(parses) == 1 and len(calls) == 3

    add_opening(context, "Created after processing")
    context.drain()
    assert len(parses) == 1 and len(calls) == 3
    assert evaluation(context, upload) == original

    next_cv = context.upload(
        pdf_bytes("Next Candidate\nnext@example.org\nFive years in Python")
    ).json()["upload_id"]
    context.drain()
    titles = {role["opening_title"] for role in evaluation(context, next_cv)["roles"]}
    assert added["title"] in titles
    assert to_close["title"] not in titles and already_closed["title"] not in titles
    assert ("Senior", "Excel reporting and spreadsheets") in calls


def test_retry_reuses_profile_and_completed_opening_results(context, cv, admin_url, monkeypatch):
    add_opening(context, "Second opening")
    parses = []
    parse = context.providers.profiles.profile

    def recording_parse(text):
        parses.append(text)
        return parse(text)

    class TransientSecondMatch(FakeMatcher):
        def __init__(self):
            self.calls = []

        def match(self, job, profile):
            self.calls.append(job.title)
            if len(self.calls) == 2:
                raise ProviderError("AI_UNAVAILABLE", retryable=True)
            return super().match(job, profile)

    matcher = TransientSecondMatch()
    context.providers.matcher = matcher
    monkeypatch.setattr(context.providers.profiles, "profile", recording_parse)
    upload = context.upload(cv).json()["upload_id"]
    assert context.worker().run_once()
    with psycopg.connect(admin_url) as db:
        # Keep using profiles saved before the compact parser prompt was deployed.
        db.execute(
            "UPDATE resume_extractions SET prompt_version='cv-parser-v1' WHERE upload_id=%s",
            (upload,),
        )
        db.execute("UPDATE job_runs SET available_at=now() WHERE state='retry_wait'")
    context.drain()
    assert len(parses) == 1
    assert matcher.calls[1] == matcher.calls[2]
    assert sorted(Counter(matcher.calls).values()) == [1, 2]
    result = evaluation(context, upload)
    assert len(result["roles"]) == 2
    assert all(role["match_result"]["fit"] for role in result["roles"])

    # Even a repeated event with a different deduplication key reuses the same evaluation.
    with psycopg.connect(admin_url) as db:
        db.execute(
            """
            INSERT INTO job_runs(id,tenant_id,kind,aggregate_id,dedupe_key,state,
                                 attempts,available_at,created_at)
            VALUES(gen_random_uuid(),%s,'cv.uploaded',%s,%s,'queued',0,now(),now())
        """,
            (context.tenant, upload, "duplicate:" + str(uuid4())),
        )
    context.drain()
    assert evaluation(context, upload) == result
    assert len(parses) == 1 and len(matcher.calls) == 3


@pytest.mark.parametrize("mime, format", [("image/png", "PNG"), ("image/jpeg", "JPEG")])
def test_images_share_intake_and_inline_images_are_skipped(context, mime, format):
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buffer, format=format)
    image = buffer.getvalue()
    uploaded = context.upload(image, mime)
    assert uploaded.status_code == 202, uploaded.text
    context.drain()

    connect(context)
    message_id = str(uuid4())
    suffix = "png" if format == "PNG" else "jpg"
    fake(context).messages.append(
        RemoteMessage(
            id=message_id,
            received_at=utcnow(),
            attachments=[
                RemoteAttachment("logo", f"logo.{suffix}", mime, len(image), inline=True),
                RemoteAttachment("cv", f"candidate.{suffix}", mime, len(image)),
            ],
        )
    )
    fake(context).files[(message_id, "cv")] = image
    context.drain()
    assert fake(context).downloads == [(message_id, "cv")]
    recorded = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()
    assert len(recorded["items"]) == 1
    assert recorded["items"][0]["upload_id"] == uploaded.json()["upload_id"]
    response = context.client.get(
        f"{context.base}/cv-uploads/{uploaded.json()['upload_id']}/file", headers=context.headers
    )
    assert response.content == image
    assert response.headers["content-type"] == mime


def test_unreadable_ocr_text_goes_to_review_before_parsing(context, monkeypatch):
    def unexpected_parse(_text):
        pytest.fail("Punctuation-only OCR text must not reach the CV parser.")

    monkeypatch.setattr(context.providers.profiles, "profile", unexpected_parse)
    uploaded = context.upload(docx_bytes("??? ?? ???"), DOCX)
    assert uploaded.status_code == 202, uploaded.text
    context.drain()
    result = context.client.get(
        f"{context.base}/cv-uploads/{uploaded.json()['upload_id']}", headers=context.headers
    ).json()
    assert result["status"] == "needs_review"
    assert result["review_reason"] == "LOW_CONFIDENCE_EXTRACTION"
    assert result["application_id"] is None
