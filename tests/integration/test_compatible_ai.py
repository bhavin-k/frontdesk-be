import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from recruitment.adapters.ai import CVParser, JDMatcher
from recruitment.adapters.fakes import FakeProfiles
from recruitment.adapters.providers import MATCHER_PROMPT, PARSER_PROMPT


def test_pipeline_passes_only_structured_masked_profile_to_matcher(context, cv):
    client = MagicMock()
    profile = FakeProfiles().profile(
        "Fictional Candidate\nfictional@example.org\n4 years in Python"
    )
    profile.summary = "Maintained Python services for a café."
    match = {
        "fit": True,
        "score": 96,
        "matched": ["Python"],
        "missing": [],
        "exp_ok": True,
        "reason": "The CV lists Python development.",
    }

    def completion(content):
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(refusal=None, content=content),
                )
            ],
        )

    client.chat.completions.create.side_effect = [
        completion("invalid JSON"),
        completion(profile.model_dump_json()),
        completion(json.dumps(match)),
    ]
    args = (client, "test-model")
    context.providers.profiles = CVParser(*args, PARSER_PROMPT, "chat_completions", "json_object")
    context.providers.matcher = JDMatcher(*args, MATCHER_PROMPT, "chat_completions", "json_object")
    upload = context.upload(cv).json()
    context.drain()
    assert context.result(upload["upload_id"])["score"] == 96

    parse, retry, score = client.chat.completions.create.call_args_list
    raw = json.loads(parse.kwargs["messages"][1]["content"])
    assert set(raw) == {"cv_reference_text"} and "candidate@example.org" in raw["cv_reference_text"]
    assert parse.kwargs["messages"][1] == retry.kwargs["messages"][1]
    serialized = score.kwargs["messages"][1]["content"]
    payload = json.loads(serialized)
    assert serialized == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert payload["job"]["title"] == "Python Developer"
    assert not {"email", "name", "phone", "certifications"} & payload["cv"].keys()
    assert payload["cv"]["total_experience_years"] == 4
    assert payload["cv"]["summary"] == profile.summary
    assert all(not call.kwargs["store"] for call in (parse, retry, score))
