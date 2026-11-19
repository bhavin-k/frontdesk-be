from recruitment.adapters.ai import StructuredAI
from recruitment.domain.screening import ReplyDraft, ReplyIntent


class ScreeningReplyClassifier(StructuredAI):
    def interpret(self, thread: str) -> ReplyIntent:
        return self.bounded_parse(thread, ReplyIntent)


class ScreeningReplyDrafter(StructuredAI):
    def draft(self, context: str) -> ReplyDraft:
        if (
            self.estimate_input_tokens(context, ReplyDraft, "") + self.output_tokens
            > self.request_tokens
        ):
            return ReplyDraft(acknowledgment=None)
        return self.parse(
            context,
            ReplyDraft,
            max_attempts=1,
            output_tokens=self.output_tokens,
            observe_usage=self._log_usage,
        )


class FakeScreeningClassifier:
    def interpret(self, thread: str) -> ReplyIntent:
        return ReplyIntent(
            intent="UNCLEAR",
            evidence="",
            slot_number=None,
            proposed_datetime=None,
            timezone=None,
            phone=None,
            question=None,
        )
