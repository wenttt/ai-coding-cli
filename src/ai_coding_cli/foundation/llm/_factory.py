"""build_adapter: construct an LLMAdapter from AdapterConfig."""

from __future__ import annotations

from ..config import AdapterConfig
from ._adapter import LLMAdapter
from ._mock import MockAdapter
from ._openai_compat import OpenAICompatibleAdapter


def build_adapter(config: AdapterConfig, *, request_timeout_seconds: float = 300.0) -> LLMAdapter:
    """Construct an adapter from a config block. Raises ValueError on
    misconfiguration (e.g. openai-compat missing base_url + api_key).
    """
    if config.kind == "mock":
        return MockAdapter(model_name=config.model_name)

    if config.kind == "openai-compat":
        if config.base_url is None:
            raise ValueError("openai-compat adapter requires base_url.")
        if config.api_key is None:
            raise ValueError("openai-compat adapter requires api_key.")
        return OpenAICompatibleAdapter(
            base_url=str(config.base_url),
            api_key=config.api_key.get_secret_value(),
            model_name=config.model_name,
            request_timeout_seconds=request_timeout_seconds,
        )

    if config.kind == "anthropic-native":
        # Lite ships without the native Anthropic adapter; Anthropic is
        # accessible via their OpenAI-compat shim or this build_adapter() call
        # can route to a native impl in Phase 2.
        raise ValueError(
            "anthropic-native adapter is reserved for the Standard profile; "
            "in Lite, use kind=openai-compat with Anthropic's OpenAI-compat shim."
        )

    raise ValueError(f"Unknown adapter kind: {config.kind!r}")
