"""Gemini implementation of the assistant (Google Gen AI Python SDK)."""

from __future__ import annotations

import logging
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from ..config import Settings, ThinkingLevel
from ..models import Classification, ClientRecord, ContextSource, DraftResult, InboundEmail
from . import prompts
from .base import AIResult, AssistantError, AssistantRefusal, CallUsage

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# Finish reasons that mean Gemini would not answer this email: route it to a person.
BLOCKED_FINISH_REASONS = {
    types.FinishReason.SAFETY,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.SPII,
    types.FinishReason.RECITATION,
}


class GeminiAssistant:
    name = "gemini"

    def __init__(self, settings: Settings, client: genai.Client | None = None) -> None:
        self.settings = settings
        # The SDK also reads GEMINI_API_KEY / GOOGLE_API_KEY from the environment when api_key is None.
        # Retries cover 429s, 5xx and connection errors with backoff before we give up on this poll.
        self.client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=120_000, retry_options=types.HttpRetryOptions(attempts=3)),
        )
        profile = settings.business_profile()
        self.classify_system = prompts.CLASSIFY_SYSTEM.format(business_profile=profile)
        self.draft_system = prompts.DRAFT_SYSTEM.format(business_profile=profile)

    def _call(
        self, *, system: str, content: str, output: type[M], thinking: ThinkingLevel, max_tokens: int
    ) -> AIResult[M]:
        config = types.GenerateContentConfig(
            # The system instruction is identical for every email, so Gemini's implicit caching reuses it;
            # the email itself comes after it in the request.
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=output,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking.upper())),
            max_output_tokens=max_tokens,
            # No tools are declared; switch off automatic function calling (and its log warning).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self.client.models.generate_content(
                model=self.settings.gemini_model, contents=content, config=config
            )
        except errors.ClientError as exc:
            # 429 (rate limit / quota) is worth retrying; other 4xx (bad request, auth) are not.
            retryable = exc.code == 429
            raise AssistantError(f"Gemini API error {exc.code}: {exc.message}", retryable=retryable) from exc
        except errors.APIError as exc:
            raise AssistantError(f"Gemini API error {exc.code}: {exc.message}") from exc
        except Exception as exc:  # transport errors (timeouts, DNS, resets) from the HTTP layer
            raise AssistantError(f"Could not reach the Gemini API: {exc}") from exc

        meta = response.usage_metadata
        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason if candidate else None
        usage = CallUsage(
            model=response.model_version or self.settings.gemini_model,
            input_tokens=(meta.prompt_token_count or 0) if meta else 0,
            output_tokens=((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
            if meta
            else 0,
            cache_read_tokens=(meta.cached_content_token_count or 0) if meta else 0,
            stop_reason=finish.value if finish else None,
        )
        if response.prompt_feedback and response.prompt_feedback.block_reason:
            reason = response.prompt_feedback.block_reason.value
            raise AssistantRefusal(f"Gemini blocked this email (reason: {reason})", usage)
        if finish in BLOCKED_FINISH_REASONS:
            raise AssistantRefusal(f"Gemini declined this email (finish reason: {finish.value})", usage)
        if finish == types.FinishReason.MAX_TOKENS:
            raise AssistantError("The response was cut off at max_output_tokens", usage)
        text = response.text
        if not text:
            raise AssistantError("The response had no text", usage)
        try:
            value = output.model_validate_json(text)
        except ValidationError as exc:
            raise AssistantError(
                f"The response did not match the {output.__name__} schema: {exc}", usage
            ) from exc
        return AIResult(value=value, usage=usage)

    def classify(self, email: InboundEmail, client: ClientRecord | None) -> AIResult[Classification]:
        result = self._call(
            system=self.classify_system,
            content=prompts.classify_input(email, client),
            output=Classification,
            thinking=self.settings.triage_thinking,
            max_tokens=8_000,
        )
        result.value.confidence = min(max(result.value.confidence, 0.0), 1.0)
        return result

    def draft(
        self,
        email: InboundEmail,
        classification: Classification,
        client: ClientRecord | None,
        sources: list[ContextSource],
        instruction: str | None = None,
    ) -> AIResult[DraftResult]:
        result = self._call(
            system=self.draft_system,
            content=prompts.draft_input(email, classification, sources, instruction),
            output=DraftResult,
            thinking=self.settings.draft_thinking,
            max_tokens=16_000,
        )
        result.value.confidence = min(max(result.value.confidence, 0.0), 1.0)
        return result
