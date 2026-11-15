from __future__ import annotations

import logging
import re
import threading
import time


LOGGER = logging.getLogger(__name__)
MAX_RATE_LIMIT_RETRIES = 2
_REQUEST_START_LOCK = threading.Lock()
_LAST_REQUEST_STARTED_AT = 0.0


def _wait_for_request_slot(min_interval_seconds: float) -> None:
    global _LAST_REQUEST_STARTED_AT

    with _REQUEST_START_LOCK:
        now = time.monotonic()
        wait_seconds = min_interval_seconds - (now - _LAST_REQUEST_STARTED_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_REQUEST_STARTED_AT = time.monotonic()


def _retry_delay_seconds(error: Exception, retry_number: int) -> float:
    response_json = getattr(error, "response_json", {})
    if isinstance(response_json, dict):
        api_error = response_json.get("error", {})
        details = api_error.get("details", []) if isinstance(api_error, dict) else []
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                retry_delay = detail.get("retryDelay")
                if isinstance(retry_delay, str):
                    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", retry_delay)
                    if match:
                        return max(1.0, float(match.group(1)))

    match = re.search(r"retry in\s+(\d+(?:\.\d+)?)\s*s", str(error), re.IGNORECASE)
    if match:
        return max(1.0, float(match.group(1)))

    return min(120.0, 60.0 * (2**retry_number))


class GoogleAIStudioClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        min_request_interval_seconds: float = 5.0,
    ) -> None:
        from google import genai
        from google.genai import types

        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds cannot be negative")

        self._types = types
        self._model = model
        self._min_request_interval_seconds = min_request_interval_seconds
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=max(1, int(timeout_seconds * 1000))),
        )

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        try:
            rate_limit_retries = 0
            service_unavailable_retried = False
            while True:
                _wait_for_request_slot(self._min_request_interval_seconds)
                try:
                    response = self._client.models.generate_content(
                        model=self._model,
                        contents=user_prompt,
                        config=self._types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0,
                            response_mime_type="application/json",
                        ),
                    )
                except Exception as error:
                    status_code = getattr(error, "code", None)
                    if status_code == 503:
                        if service_unavailable_retried:
                            raise
                        service_unavailable_retried = True
                        delay = self._min_request_interval_seconds
                        LOGGER.warning(
                            "Google AI Studio returned 503; retrying once in %.1f seconds",
                            delay,
                        )
                    elif status_code == 429:
                        if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                            raise
                        delay = _retry_delay_seconds(error, rate_limit_retries)
                        rate_limit_retries += 1
                        LOGGER.warning(
                            "Google AI Studio rate limit reached; retrying in %.1f seconds",
                            delay,
                        )
                    else:
                        raise
                    time.sleep(delay)
                    continue

                content = response.text
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Google AI Studio response did not contain text content")
                return content
        finally:
            self._client.close()
