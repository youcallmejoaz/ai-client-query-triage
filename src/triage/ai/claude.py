"""Claude implementation of the assistant (Anthropic Python SDK)."""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from anthropic.types.beta import BetaMessageParam, BetaOutputConfigParam, BetaTextBlockParam
from pydantic import BaseModel, ValidationError

from ..config import Effort, Settings
from ..models import Classification, ClientRecord, ContextSource, DraftResult, InboundEmail
from . import prompts
from .base import AIResult, AssistantError, AssistantRefusal, CallUsage

log = logging.getLogger(__name__)

# `fallbacks: "default"` re-runs a request the safety classifiers decline on Anthropic's recommended
# fallback model, inside the same call, instead of returning a refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

M = TypeVar("M", bound=BaseModel)


class ClaudeAssistant:
    name = "claude"

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self.settings = settings
        # max_retries covers 429/5xx/connection errors with backoff before we give up on this poll.
        self.client = client or anthropic.Anthropic(max_retries=3, timeout=120)
        profile = settings.business_profile()
        self.classify_system = prompts.CLASSIFY_SYSTEM.format(business_profile=profile)
        self.draft_system = prompts.DRAFT_SYSTEM.format(business_profile=profile)
        self._schemas: dict[type[BaseModel], dict[str, Any]] = {}

    def _schema(self, model: type[BaseModel]) -> dict[str, Any]:
        if model not in self._schemas:
            self._schemas[model] = anthropic.transform_schema(model)
        return self._schemas[model]

    def _call(
        self, *, system: str, content: str, output: type[M], effort: Effort, max_tokens: int
    ) -> AIResult[M]:
        use_fallbacks = self.settings.anthropic_fallbacks == "default"
        # The system prompt is identical for every email, so it is cached; the email follows it.
        system_blocks: list[BetaTextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[BetaMessageParam] = [{"role": "user", "content": content}]
        output_config: BetaOutputConfigParam = {
            "effort": effort,
            "format": {"type": "json_schema", "schema": self._schema(output)},
        }
        try:
            response = self.client.beta.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config=output_config,
                betas=[FALLBACK_BETA] if use_fallbacks else anthropic.omit,
                fallbacks="default" if use_fallbacks else anthropic.omit,
            )
        except anthropic.RateLimitError as exc:
            raise AssistantError(f"Rate limited by the Claude API: {exc}") from exc
        except anthropic.BadRequestError as exc:
            # A malformed request will not succeed on retry; surface it for a person to look at.
            raise AssistantError(f"Claude API rejected the request: {exc}", retryable=False) from exc
        except anthropic.APIStatusError as exc:
            raise AssistantError(f"Claude API error {exc.status_code}: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise AssistantError(f"Could not reach the Claude API: {exc}") from exc

        usage = CallUsage(
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_tokens=response.usage.cache_creation_input_tokens or 0,
            stop_reason=response.stop_reason,
        )
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise AssistantRefusal(
                f"Claude declined this email (category: {category or 'unspecified'})", usage
            )
        if response.stop_reason == "max_tokens":
            raise AssistantError("The response was cut off at max_tokens", usage)
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise AssistantError("The response had no text block", usage)
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
            effort=self.settings.triage_effort,
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
            effort=self.settings.draft_effort,
            max_tokens=16_000,
        )
        result.value.confidence = min(max(result.value.confidence, 0.0), 1.0)
        return result
