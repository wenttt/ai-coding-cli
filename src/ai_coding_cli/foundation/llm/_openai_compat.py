"""OpenAI-compatible LLM adapter. See ADR-0014.

Works against any endpoint that speaks the OpenAI chat completions protocol:
- OpenAI itself
- Azure OpenAI
- vLLM / Together / Groq / Fireworks
- Anthropic via their OpenAI-compat shim
- A company's internal LLM gateway
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import tiktoken
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageToolCall

from ..errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMContextOverflowError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from ._adapter import CacheHints, LLMResponse, ToolCall


class OpenAICompatibleAdapter:
    """Adapter for any OpenAI-protocol chat completions endpoint."""

    provider_name = "openai-compat"
    supports_tool_calling = True
    supports_streaming = True
    supports_prompt_cache_hints = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        request_timeout_seconds: float = 300.0,
        ca_bundle_path: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.model_name = model_name
        self._timeout = request_timeout_seconds

        # Respect REQUESTS_CA_BUNDLE / SSL_CERT_FILE if set via httpx config
        if ca_bundle_path:
            http_client = httpx.AsyncClient(verify=ca_bundle_path, timeout=request_timeout_seconds)
        else:
            http_client = httpx.AsyncClient(timeout=request_timeout_seconds)

        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            default_headers=extra_headers,
            max_retries=0,  # we handle retries at the Agent Core level
        )

        self._encoder = _get_encoder_for_model(model_name)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 300.0,
        cache_hints: CacheHints | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout_seconds,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            raise LLMRateLimitError(
                "LLM provider rate-limited the request.",
                cause=exc,
                provider=self.provider_name,
                model=self.model_name,
                retry_after_seconds=None,
            ) from exc
        except APITimeoutError as exc:
            raise LLMTimeoutError(
                "LLM call timed out.",
                cause=exc,
                provider=self.provider_name,
                model=self.model_name,
                timeout_seconds=timeout_seconds,
            ) from exc
        except AuthenticationError as exc:
            raise LLMAuthError(
                "LLM provider returned 401/403.",
                cause=exc,
                provider=self.provider_name,
            ) from exc
        except BadRequestError as exc:
            # Distinguish context overflow from generic bad request.
            if _is_context_overflow(exc):
                raise LLMContextOverflowError(
                    "Context window exceeded.",
                    cause=exc,
                    provider=self.provider_name,
                    model=self.model_name,
                ) from exc
            raise LLMBadRequestError(
                f"LLM provider returned 400: {exc}",
                cause=exc,
                provider=self.provider_name,
            ) from exc
        except APIStatusError as exc:
            # 5xx etc.
            if 500 <= getattr(exc, "status_code", 0) < 600:
                raise LLMServerError(
                    f"LLM provider returned {exc.status_code}.",
                    cause=exc,
                    provider=self.provider_name,
                    model=self.model_name,
                ) from exc
            raise LLMInvalidResponseError(
                f"LLM provider returned unexpected status: {exc}",
                cause=exc,
                provider=self.provider_name,
            ) from exc
        except APIConnectionError as exc:
            raise LLMServerError(
                "LLM provider connection error.",
                cause=exc,
                provider=self.provider_name,
                model=self.model_name,
            ) from exc

        try:
            choice = response.choices[0]
        except (AttributeError, IndexError) as exc:
            raise LLMInvalidResponseError(
                "LLM response had no choices.",
                cause=exc,
                provider=self.provider_name,
            ) from exc

        msg = choice.message
        tool_calls = _parse_tool_calls(msg.tool_calls)
        usage = response.usage

        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            cache_hit_tokens=_get_cache_hit_tokens(usage),
            raw_provider_response=response.model_dump() if hasattr(response, "model_dump") else None,
        )

    async def summarize_for_compaction(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,
        max_tokens: int = 4000,
    ) -> str:
        """One-shot LLM summarization. Used by the Compactor (ADR-0011)."""
        summary_messages: list[dict[str, Any]] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": _render_messages_for_summary(messages)},
        ]
        response = await self.complete(
            messages=summary_messages,
            tools=None,
            max_tokens=max_tokens,
            temperature=0.0,
            cache_hints=None,
        )
        return response.content or ""

    def count_tokens(self, content: str | list[dict[str, Any]]) -> int:
        """Approximate token count using tiktoken.

        For messages, this counts each role + content. It's an over-estimate
        (tool schema overhead is not captured) but safe.
        """
        if isinstance(content, str):
            return len(self._encoder.encode(content))
        total = 0
        for msg in content:
            total += 4  # role + structural overhead per message
            for value in msg.values():
                if isinstance(value, str):
                    total += len(self._encoder.encode(value))
                elif isinstance(value, list) or isinstance(value, dict):
                    total += len(self._encoder.encode(json.dumps(value, ensure_ascii=False, default=str)))
        return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_encoder_for_model(model_name: str) -> Any:
    """Return a tiktoken encoder for the model; fall back to cl100k_base."""
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def _parse_tool_calls(
    raw: list[ChatCompletionMessageToolCall] | None,
) -> list[ToolCall]:
    if not raw:
        return []
    out: list[ToolCall] = []
    for tc in raw:
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except json.JSONDecodeError:
            args = {"_raw": tc.function.arguments}
        out.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
    return out


def _is_context_overflow(exc: BadRequestError) -> bool:
    """Heuristic: many providers return 400 with 'context_length' or similar."""
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "context length",
            "context window",
            "maximum context",
            "context_length_exceeded",
            "too many tokens",
        )
    )


def _get_cache_hit_tokens(usage: Any) -> int:
    """Extract cache-hit token count from provider usage object, if reported."""
    if usage is None:
        return 0
    # OpenAI reports cache hits at usage.prompt_tokens_details.cached_tokens
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            return int(cached)
    return 0


def _render_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Flatten messages into a single string for the summarization prompt."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(f"[{role}] {content}")
        else:
            parts.append(f"[{role}] {json.dumps(content, ensure_ascii=False, default=str)}")
    return "\n\n".join(parts)
