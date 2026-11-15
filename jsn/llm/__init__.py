from __future__ import annotations

import os
from typing import Any

from .base import LLMClient
from .google_ai_studio import GoogleAIStudioClient
from .openai_compatible import OpenAICompatibleClient


def create_llm_client(config: dict[str, Any]) -> LLMClient:
    provider = str(config.get("provider", "openai_compatible")).strip().lower()
    supported = {"google_ai_studio", "openai_compatible"}
    if provider not in supported:
        choices = ", ".join(sorted(supported))
        raise ValueError(f"Unsupported LLM provider {provider!r}; choose one of: {choices}")

    api_key_env = str(config.get("api_key_env", "LLM_API_KEY"))
    model_env = str(config.get("model_env", "LLM_MODEL"))
    api_key = os.getenv(api_key_env, "").strip()
    model = os.getenv(model_env, "").strip()
    missing = [name for name, value in ((api_key_env, api_key), (model_env, model)) if not value]
    if missing:
        raise RuntimeError(
            f"Set these environment variables for LLM provider {provider!r}: "
            f"{', '.join(missing)}"
        )

    timeout_seconds = float(config.get("timeout_seconds", 60))
    min_request_interval_seconds = float(
        config.get("min_request_interval_seconds", 5)
    )
    if provider == "google_ai_studio":
        return GoogleAIStudioClient(
            api_key,
            model,
            timeout_seconds,
            min_request_interval_seconds,
        )

    compatible_config = config.get("openai_compatible", {})
    endpoint_env = str(
        compatible_config.get(
            "endpoint_env",
            config.get("endpoint_env", "LLM_OPENAI_COMPATIBLE_URL"),
        )
    )
    endpoint = os.getenv(endpoint_env, "").strip()
    if not endpoint and endpoint_env == "LLM_OPENAI_COMPATIBLE_URL":
        endpoint = os.getenv("LLM_CHAT_COMPLETIONS_URL", "").strip()
    if not endpoint:
        raise RuntimeError(
            f"Set {endpoint_env} for LLM provider 'openai_compatible'"
        )
    return OpenAICompatibleClient(
        endpoint,
        api_key,
        model,
        timeout_seconds,
        min_request_interval_seconds,
    )


__all__ = ["create_llm_client"]
