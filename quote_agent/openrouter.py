"""Minimal OpenRouter chat client using only Python's standard library."""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter fails or returns an unexpected response."""


@dataclass(frozen=True)
class OpenRouterReply:
    """The subset of an OpenRouter response needed by the agent loop."""

    message: dict[str, Any]
    model: str
    usage: dict[str, Any]


def load_api_key(path: str | Path | None = None) -> str:
    """Load the API key from the environment, then a local ignored file."""

    environment_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if environment_key:
        return _validate_api_key(environment_key)

    configured = path or os.environ.get("OPENROUTER_API_KEY_FILE")
    key_path = Path(configured) if configured else Path(__file__).resolve().parents[1] / "or_key.txt"
    if not key_path.exists():
        raise OpenRouterError(
            "OpenRouter key not found; set OPENROUTER_API_KEY or OPENROUTER_API_KEY_FILE"
        )
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        warnings.warn(
            f"{key_path} is readable by other users; consider chmod 600",
            stacklevel=2,
        )
    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        raise OpenRouterError(f"OpenRouter key file is empty: {key_path}")
    return _validate_api_key(key)


def _validate_api_key(key: str) -> str:
    """Remove one known copy artifact, then validate without exposing the key."""

    copied_with_prompt_marker = re.fullmatch(r"(sk-or-v1-[0-9a-fA-F]{64})[^0-9a-fA-F]", key)
    if copied_with_prompt_marker:
        # A terminal prompt marker is easy to capture when copying from a shell.
        # We tolerate exactly one such character rather than broadly sanitizing
        # malformed credentials and potentially hiding a real configuration bug.
        warnings.warn(
            "Removed one trailing non-hex character from the OpenRouter key",
            stacklevel=2,
        )
        key = copied_with_prompt_marker.group(1)
    if not re.fullmatch(r"sk-or-v1-[0-9a-fA-F]{64}", key):
        raise OpenRouterError(
            "OpenRouter key is malformed; expected sk-or-v1- followed by exactly "
            "64 hexadecimal characters"
        )
    return key


class OpenRouterClient:
    """Small client that enforces private routing and structured responses."""

    def __init__(self, api_key: str, *, timeout_seconds: float = 90.0) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_model: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int = 2_500,
    ) -> OpenRouterReply:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            # Fail closed: use only providers that promise zero-data-retention
            # and do not allow OpenRouter to silently relax our parameters.
            "provider": {
                "zdr": True,
                "data_collection": "deny",
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_model:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": _strict_response_schema(response_model.model_json_schema()),
                },
            }

        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "X-Title": "Local WWI Quote Agent",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A short response excerpt is useful for debugging and cannot reveal
            # the Authorization header, which is never part of the response body.
            detail = exc.read().decode("utf-8", errors="replace")[:1_000]
            raise OpenRouterError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        try:
            message = result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"unexpected OpenRouter response: {result!r}"[:1_000]) from exc
        return OpenRouterReply(
            message=message,
            model=str(result.get("model", model)),
            usage=result.get("usage", {}),
        )


def _strict_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make all object fields explicit on the wire, including nullable fields.

    Local Pydantic defaults remain useful for application code, but a provider
    must not omit extracted lines and silently rely on those defaults.
    """
    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(item) for key, item in value.items() if key != "default"}
        if result.get("type") == "object" and "properties" in result:
            result["required"] = list(result["properties"])
            result["additionalProperties"] = False
        return result
    return visit(schema)


def parse_structured_message(reply: OpenRouterReply, model: type[BaseModel]) -> BaseModel:
    """Validate JSON message content against the requested Pydantic model."""

    content = reply.message.get("content")
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("OpenRouter returned no structured message content")
    try:
        return model.model_validate_json(content)
    except Exception as exc:
        raise OpenRouterError(f"invalid structured response for {model.__name__}: {exc}") from exc
