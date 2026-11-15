from __future__ import annotations

import logging
import time
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)


class OpenAICompatibleClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        min_request_interval_seconds: float = 5.0,
    ) -> None:
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds cannot be negative")

        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._min_request_interval_seconds = min_request_interval_seconds

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        for attempt in range(2):
            try:
                response = httpx.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 503 or attempt == 1:
                    raise
                LOGGER.warning(
                    "LLM provider returned 503; retrying once in %.1f seconds",
                    self._min_request_interval_seconds,
                )
                time.sleep(self._min_request_interval_seconds)
                continue
            break

        try:
            content: Any = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(
                "OpenAI-compatible response is missing choices[0].message.content"
            ) from error
        if not isinstance(content, str):
            raise ValueError("OpenAI-compatible response content must be a string")
        return content
