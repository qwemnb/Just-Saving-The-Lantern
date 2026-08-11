"""OpenAI configuration, client construction, and safe serialization helpers."""

from __future__ import annotations

import dataclasses
import inspect
import os
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv


OPENAI_TIMEOUT_SECONDS = 120.0
OPENAI_MAX_RETRIES = 0


@dataclasses.dataclass(frozen=True)
class OpenAIEnvironment:
    """The two environment values needed for a Helios provider call."""

    api_key: str | None
    model: str | None


def load_openai_environment(
    dotenv_path: Path | str | None = None,
) -> OpenAIEnvironment:
    """Load ``.env`` without replacing process environment values."""

    if dotenv_path is None:
        load_dotenv(override=False)
    else:
        load_dotenv(dotenv_path=dotenv_path, override=False)

    return OpenAIEnvironment(
        api_key=os.environ.get("OPENAI_API_KEY"),
        model=os.environ.get("HELIOS_OPENAI_MODEL"),
    )


def create_openai_client(
    api_key: str,
    *,
    client_class: Callable[..., Any] | None = None,
) -> Any:
    """Construct the production async client with the milestone policy."""

    if client_class is None:
        from openai import AsyncOpenAI

        client_class = AsyncOpenAI

    return client_class(
        api_key=api_key,
        timeout=OPENAI_TIMEOUT_SECONDS,
        max_retries=OPENAI_MAX_RETRIES,
    )


async def create_response(client: Any, request: Mapping[str, Any]) -> Any:
    """Make the one allowed Responses API call for a turn."""

    return await client.responses.create(**dict(request))


async def close_client(client: Any) -> None:
    """Close an SDK client when it exposes a close operation."""

    close = getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def sanitize_json_value(value: Any, *, secrets: Sequence[str] = ()) -> Any:
    """Convert SDK data to JSON-safe values and redact known secrets."""

    normalized_secrets = tuple(secret for secret in secrets if secret)
    json_value = _to_json_value(value)
    return _redact_secrets(json_value, normalized_secrets)


def serialize_provider_response(response: Any, *, api_key: str) -> dict[str, Any]:
    """Serialize the complete SDK response while removing a known API key."""

    if hasattr(response, "model_dump"):
        value = response.model_dump(mode="json")
    elif hasattr(response, "to_dict"):
        value = response.to_dict()
    elif isinstance(response, Mapping):
        value = dict(response)
    else:
        value = {
            key: getattr(response, key)
            for key in _RESPONSE_FALLBACK_FIELDS
            if hasattr(response, key)
        }

    sanitized = sanitize_json_value(value, secrets=(api_key,))
    if not isinstance(sanitized, dict):
        raise TypeError("Provider response did not serialize to a JSON object")
    return sanitized


def safe_exception_diagnostics(
    exception: BaseException,
    *,
    reason: str,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    """Return allowlisted diagnostics without persisting exception text."""

    diagnostics: dict[str, Any] = {
        "error_class": type(exception).__name__,
        "reason": reason,
        "summary": (
            "The provider request timed out."
            if reason == "provider_timeout"
            else "The provider request failed."
        ),
    }

    for source_name, target_name in (
        ("status_code", "http_status"),
        ("code", "provider_error_code"),
        ("request_id", "provider_request_id"),
    ):
        value = getattr(exception, source_name, None)
        if isinstance(value, (str, int)) and value != "":
            diagnostics[target_name] = value

    body = getattr(exception, "body", None)
    if isinstance(body, Mapping):
        provider_error = body.get("error")
        if isinstance(provider_error, Mapping):
            code = provider_error.get("code")
            if (
                "provider_error_code" not in diagnostics
                and isinstance(code, (str, int))
                and code != ""
            ):
                diagnostics["provider_error_code"] = code

    sanitized = sanitize_json_value(diagnostics, secrets=secrets)
    if not isinstance(sanitized, dict):
        raise TypeError("Exception diagnostics did not serialize to an object")
    return sanitized


def _to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _to_json_value(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_json_value(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return _to_json_value(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _to_json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _to_json_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")


def _redact_secrets(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item, secrets) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _redact_secrets(item, secrets)
            for key, item in value.items()
        }
    return value


_RESPONSE_FALLBACK_FIELDS = (
    "id",
    "object",
    "created_at",
    "status",
    "error",
    "incomplete_details",
    "instructions",
    "max_output_tokens",
    "model",
    "output",
    "parallel_tool_calls",
    "previous_response_id",
    "reasoning",
    "store",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_p",
    "truncation",
    "usage",
    "metadata",
    "service_tier",
)

