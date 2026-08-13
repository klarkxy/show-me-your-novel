"""Small dual-protocol client used by the benchmark runners.

The configured New API instance exposes both OpenAI Chat Completions and
Anthropic Messages.  Models select their exact wire protocol in ``config.yaml``;
the client never guesses from a model name.  Optional generation controls are
sent only when the caller explicitly supplies them.  Anthropic's required
``max_tokens`` field is tracked separately from those optional controls.

Credentials are accepted by the client (or loaded from ``API_URL`` and
``API_KEY`` at runtime) but are never included in return values or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import http.client
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.request

import yaml


API_URL_ENV = "API_URL"
API_KEY_ENV = "API_KEY"
DEFAULT_TIMEOUT = 180
OPENAI_CHAT_COMPLETIONS = "openai-chat-completions"
ANTHROPIC_MESSAGES = "anthropic-messages"
ANTHROPIC_VERSION = "2023-06-01"

_PROTECTED_BODY_FIELDS = frozenset({"model", "messages"})
_ANTHROPIC_PROTECTED_BODY_FIELDS = frozenset({"model", "messages", "system"})
_ANTHROPIC_REQUEST_FIELDS = frozenset(
    {
        "max_tokens",
        "metadata",
        "output_config",
        "service_tier",
        "stop_sequences",
        "stream",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    }
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "headers",
        "proxy_authorization",
        "token",
    }
)
PROVIDER_DEFAULTS_TRACKING_KEY = "provider_request_defaults"


class LLMAPIError(RuntimeError):
    """A safe-to-log error raised by the New API client."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        raw_response: Mapping[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        # This may contain private reasoning. Callers may persist it only under
        # ignored local audit storage; it is never interpolated into the error.
        self.raw_response = dict(raw_response) if raw_response is not None else None


class ModelPreflightError(LLMAPIError):
    """Raised when one or more configured wire model ids are unavailable."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(f"模型预检失败，缺少：{', '.join(self.missing)}")


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After seconds or an HTTP date without retaining the header."""

    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class ChatResult:
    """Normalized non-streaming result from either supported wire protocol."""

    content: str
    reasoning_content: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    requested_model: str = ""
    response_model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    protocol: str = OPENAI_CHAT_COMPLETIONS
    endpoint_path: str = "/v1/chat/completions"
    latency_ms: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ModelPreflight:
    """Result of comparing configured wire ids with ``GET /v1/models``."""

    required: tuple[str, ...]
    available: frozenset[str]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing

    def require_available(self) -> None:
        if self.missing:
            raise ModelPreflightError(self.missing)


def normalize_api_url(api_url: str) -> str:
    """Return an API base ending in ``/v1`` without inventing other paths."""

    base = api_url.strip().rstrip("/")
    if not base:
        raise ValueError("API URL 不能为空")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def model_protocol(model_cfg: Mapping[str, Any]) -> str:
    """Return the explicitly configured protocol, defaulting to the legacy O port."""

    protocol = str(model_cfg.get("protocol") or OPENAI_CHAT_COMPLETIONS).strip()
    if protocol not in {OPENAI_CHAT_COMPLETIONS, ANTHROPIC_MESSAGES}:
        raise ValueError(f"不支持的模型协议：{protocol}")
    return protocol


def protocol_endpoint_path(protocol: str) -> str:
    """Return the public path associated with a validated wire protocol."""

    if protocol == OPENAI_CHAT_COMPLETIONS:
        return "/v1/chat/completions"
    if protocol == ANTHROPIC_MESSAGES:
        return "/v1/messages"
    raise ValueError(f"不支持的模型协议：{protocol}")


def load_env_file(env_file: Path) -> dict[str, str]:
    """Load a small dotenv file without logging names or values."""

    values: dict[str, str] = {}
    if not env_file.exists():
        return values
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def load_config(config_path: Path) -> dict[str, Any]:
    """Load and minimally validate the benchmark YAML configuration."""

    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件：{config_path}")
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not isinstance(config, dict):
        raise ValueError("配置文件顶层必须是对象")
    if not isinstance(config.get("providers"), Mapping):
        raise ValueError("配置文件缺少 providers")
    return config


def _registry_entry(
    config: Mapping[str, Any], registry: str, entry_id: str
) -> dict[str, Any]:
    entries = config.get(registry)
    if not isinstance(entries, list):
        raise ValueError(f"config.yaml 中的 {registry} 必须是数组")
    alias_matches: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        if entry.get("id") == entry_id:
            return entry
        aliases = entry.get("aliases") or []
        if isinstance(aliases, list) and entry_id in aliases:
            alias_matches.append(entry)
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        raise ValueError(f"{registry} 中的别名不唯一：{entry_id}")
    raise ValueError(f"config.yaml 的 {registry} 中未找到 id：{entry_id}")


def get_model_config(config: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    """Resolve an exact stable generator id or explicitly declared alias."""

    return _registry_entry(config, "models", model_id)


def get_judge_config(config: Mapping[str, Any], judge_id: str) -> dict[str, Any]:
    """Resolve an exact fixed judge id or explicitly declared alias."""

    return _registry_entry(config, "judges", judge_id)


def _normalized_field_name(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _find_sensitive_field(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_field_name(key)
            if normalized in _SENSITIVE_FIELD_NAMES:
                return str(key)
            found = _find_sensitive_field(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_sensitive_field(nested)
            if found:
                return found
    return None


def protocol_required_parameters(model_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return fields required by the selected wire protocol."""

    protocol = model_protocol(model_cfg)
    raw = model_cfg.get("protocol_required")
    if raw is None:
        required: dict[str, Any] = {}
    elif isinstance(raw, Mapping):
        required = dict(raw)
    else:
        raise ValueError("model.protocol_required 必须是对象")

    sensitive = _find_sensitive_field(required)
    if sensitive:
        raise ValueError(f"protocol_required 含敏感字段：{sensitive}")
    if protocol == OPENAI_CHAT_COMPLETIONS:
        if required:
            raise ValueError("OpenAI Chat Completions 不接受 protocol_required")
        return {}

    unexpected = sorted(set(required) - {"max_tokens"})
    if unexpected:
        raise ValueError(
            "Anthropic Messages 的 protocol_required 含未知字段："
            + ", ".join(unexpected)
        )
    max_tokens = required.get("max_tokens")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens <= 0
    ):
        raise ValueError(
            "Anthropic Messages 必须配置正整数 protocol_required.max_tokens"
        )
    return {"max_tokens": max_tokens}


def with_provider_request_defaults(
    config: Mapping[str, Any], model_cfg: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach effective provider defaults to the experiment identity.

    ``ChatClient.complete`` already applies these values on the wire.  Tracking
    them beside the model config ensures run and scoring hashes change when the
    provider-level sampling defaults change.
    """

    result = dict(model_cfg)
    model_protocol(result)
    protocol_required_parameters(result)
    provider_id = str(result.get("provider") or "new-api")
    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        raise ValueError("配置文件缺少 providers")
    provider = providers.get(provider_id)
    if not isinstance(provider, Mapping):
        raise ValueError(f"config.yaml 中未找到 provider：{provider_id}")
    defaults = provider.get("request_defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ValueError(f"provider {provider_id} 的 request_defaults 必须是对象")
    sensitive = _find_sensitive_field(defaults)
    if sensitive:
        raise ValueError(f"request_defaults 含敏感字段：{sensitive}")
    if defaults:
        result[PROVIDER_DEFAULTS_TRACKING_KEY] = dict(defaults)
    else:
        result.pop(PROVIDER_DEFAULTS_TRACKING_KEY, None)
    return result


_PRIVATE_BLOCK_TYPES = frozenset(
    {"thinking", "reasoning", "analysis", "redacted_thinking"}
)
_INLINE_PRIVATE_TAG = re.compile(
    r"<(?P<tag>think|thinking|analysis|reasoning|redacted[_-]?thinking)\b[^>]*>"
    r"(?P<body>.*?)(?:</(?P=tag)\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_PRIVATE_CLOSING_TAG = re.compile(
    r"</?(?:think|thinking|analysis|reasoning|redacted[_-]?thinking)\b[^>]*>",
    re.IGNORECASE,
)
_LEGACY_PRIVATE_BLOCK = re.compile(
    r"\[思考过程\](?P<body>.*?)(?:\[/思考过程\]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PREFIXED_PRIVATE_BLOCK = re.compile(
    r"(?:\A|\n)\s*(?:reasoning_content|思考过程)\s*[:：]\s*(?P<body>.*?)"
    r"(?=(?:\n\s*```(?:json)?|\n\s*\{)|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def split_inline_reasoning_text(text: str) -> tuple[str, str]:
    """Separate tagged private reasoning leaked into a string content field.

    Several OpenAI-compatible adapters return reasoning as typed blocks or a
    dedicated field, but others serialize it into ``content`` using XML-like
    tags.  Closed and unclosed private blocks are removed from the public text;
    their bodies remain available to ignored local audit storage.
    """

    public = text
    private_parts: list[str] = []

    def remove_block(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        if body:
            private_parts.append(body)
        return "\n"

    for pattern in (
        _INLINE_PRIVATE_TAG,
        _LEGACY_PRIVATE_BLOCK,
        _PREFIXED_PRIVATE_BLOCK,
    ):
        public = pattern.sub(remove_block, public)
    # Never forward a dangling private tag even when an adapter emitted a
    # malformed close/open pair.  Its original bytes remain in raw_response.
    public = _INLINE_PRIVATE_CLOSING_TAG.sub("", public)
    public = re.sub(r"\[/思考过程\]", "", public, flags=re.IGNORECASE)
    return public.strip(), "\n".join(private_parts).strip()


def _coerce_content_text(value: object) -> str:
    """Extract publishable text while dropping private reasoning blocks."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        block_type = _normalized_field_name(value.get("type", ""))
        if block_type in _PRIVATE_BLOCK_TYPES:
            return ""
        for key in ("text", "output_text", "content"):
            nested = value.get(key)
            if isinstance(nested, (str, Mapping, list, tuple)):
                return _coerce_content_text(nested)
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(_coerce_content_text(item) for item in value)
    return str(value)


def _coerce_reasoning_text(value: object) -> str:
    """Extract text from a field already designated as private reasoning."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("thinking", "reasoning", "reasoning_content", "text", "content"):
            nested = value.get(key)
            if isinstance(nested, (str, Mapping, list, tuple)):
                return _coerce_reasoning_text(nested)
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(_coerce_reasoning_text(item) for item in value)
    return str(value)


def _extract_reasoning_blocks(value: object) -> str:
    """Collect private blocks embedded beside public content blocks."""

    if isinstance(value, (list, tuple)):
        return "".join(_extract_reasoning_blocks(item) for item in value)
    if not isinstance(value, Mapping):
        return ""
    block_type = _normalized_field_name(value.get("type", ""))
    if block_type in _PRIVATE_BLOCK_TYPES:
        return _coerce_reasoning_text(value)
    pieces = [
        _coerce_reasoning_text(value.get(key))
        for key in ("thinking", "reasoning", "reasoning_content")
        if value.get(key) is not None
    ]
    nested = value.get("content")
    if isinstance(nested, (Mapping, list, tuple)):
        pieces.append(_extract_reasoning_blocks(nested))
    return "".join(pieces)


def _coerce_tool_input_to_schema(value: Any, schema: object) -> Any:
    """Undo gateway stringification of nested Anthropic tool containers.

    Some OpenAI-compatible gateways preserve the outer ``tool_use.input``
    object but JSON-encode nested object/array fields as strings.  Decode only
    where the declared input schema says a container is expected; downstream
    domain validation remains authoritative.
    """

    if not isinstance(schema, Mapping):
        return value
    schema_type = schema.get("type")
    if schema_type in {"object", "array"} and isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if schema_type == "object" and isinstance(value, Mapping):
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return dict(value)
        return {
            str(key): _coerce_tool_input_to_schema(child, properties.get(key))
            for key, child in value.items()
        }
    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        return [_coerce_tool_input_to_schema(child, item_schema) for child in value]
    return value


def _openai_stream_response(
    events: Sequence[Mapping[str, Any]], *, saw_done: bool
) -> dict[str, Any]:
    """Reconstruct one Chat Completions response from SSE delta events."""

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    response_id: str | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    for event in events:
        if event.get("error"):
            raise LLMAPIError("LLM API 流式响应包含错误对象")
        if response_id is None and event.get("id") is not None:
            response_id = str(event["id"])
        if response_model is None and event.get("model") is not None:
            response_model = str(event["model"])
        event_usage = event.get("usage")
        if isinstance(event_usage, Mapping):
            usage.update(event_usage)
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0]
        if not isinstance(first, Mapping):
            continue
        delta = first.get("delta")
        if not isinstance(delta, Mapping):
            delta = first.get("message")
        if isinstance(delta, Mapping):
            raw_content = delta.get("content")
            content_parts.append(_coerce_content_text(raw_content))
            for candidate in (
                delta.get("reasoning_content"),
                delta.get("reasoning"),
                _extract_reasoning_blocks(raw_content),
            ):
                text = _coerce_reasoning_text(candidate)
                if text:
                    reasoning_parts.append(text)
        if first.get("finish_reason") is not None:
            finish_reason = str(first["finish_reason"])

    terminal = saw_done or finish_reason is not None
    reconstructed: dict[str, Any] = {
        "id": response_id,
        "model": response_model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": "".join(content_parts),
                    "reasoning_content": "".join(reasoning_parts),
                },
            }
        ],
        "usage": usage,
        "_stream": {"terminal": terminal, "events": [dict(item) for item in events]},
    }
    if not terminal:
        raise LLMAPIError(
            "LLM API 流式响应在终止事件前中断",
            raw_response=reconstructed,
        )
    return reconstructed


def _anthropic_stream_response(
    events: Sequence[Mapping[str, Any]], *, saw_done: bool
) -> dict[str, Any]:
    """Reconstruct one Anthropic Messages response from native SSE events."""

    response_id: str | None = None
    response_model: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = {}
    block_types: dict[int, str] = {}
    block_text: dict[int, list[str]] = {}
    block_ids: dict[int, str] = {}
    block_names: dict[int, str] = {}
    block_inputs: dict[int, Any] = {}
    terminal = saw_done
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "error" or event.get("error"):
            raise LLMAPIError("LLM API 流式响应包含错误对象")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, Mapping):
                if message.get("id") is not None:
                    response_id = str(message["id"])
                if message.get("model") is not None:
                    response_model = str(message["model"])
                initial_usage = message.get("usage")
                if isinstance(initial_usage, Mapping):
                    usage.update(initial_usage)
        elif event_type == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if isinstance(index, int) and isinstance(block, Mapping):
                block_type = str(block.get("type") or "text")
                block_types[index] = block_type
                if block.get("id") is not None:
                    block_ids[index] = str(block["id"])
                if block.get("name") is not None:
                    block_names[index] = str(block["name"])
                if block_type == "tool_use":
                    block_inputs[index] = block.get("input")
                    block_text.setdefault(index, [])
                else:
                    initial = _coerce_reasoning_text(block) if block_type in _PRIVATE_BLOCK_TYPES else _coerce_content_text(block)
                    block_text.setdefault(index, []).append(initial)
        elif event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta")
            if isinstance(index, int) and isinstance(delta, Mapping):
                delta_type = str(delta.get("type") or "")
                if delta_type == "thinking_delta":
                    block_types[index] = "thinking"
                    text = _coerce_reasoning_text(delta.get("thinking"))
                elif delta_type == "text_delta":
                    block_types.setdefault(index, "text")
                    text = _coerce_content_text(delta.get("text"))
                elif delta_type == "input_json_delta":
                    block_types.setdefault(index, "tool_use")
                    partial = delta.get("partial_json")
                    text = partial if isinstance(partial, str) else ""
                else:
                    block_type = block_types.get(index, "text")
                    text = _coerce_reasoning_text(delta) if block_type in _PRIVATE_BLOCK_TYPES else _coerce_content_text(delta)
                block_text.setdefault(index, []).append(text)
        elif event_type == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
                stop_reason = str(delta["stop_reason"])
            event_usage = event.get("usage")
            if isinstance(event_usage, Mapping):
                usage.update(event_usage)
        elif event_type == "message_stop":
            terminal = True

    content: list[dict[str, Any]] = []
    for index in sorted(block_text):
        block_type = block_types.get(index, "text")
        text = "".join(block_text[index])
        if block_type == "tool_use":
            raw_input = block_inputs.get(index)
            if text:
                try:
                    raw_input = json.loads(text)
                except json.JSONDecodeError:
                    raw_input = text
            content.append(
                {
                    "type": "tool_use",
                    "id": block_ids.get(index),
                    "name": block_names.get(index),
                    "input": raw_input,
                }
            )
        elif block_type in _PRIVATE_BLOCK_TYPES:
            content.append({"type": block_type, "thinking": text})
        else:
            content.append({"type": block_type, "text": text})
    reconstructed: dict[str, Any] = {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": response_model,
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage,
        "_stream": {"terminal": terminal, "events": [dict(item) for item in events]},
    }
    if not terminal:
        raise LLMAPIError(
            "LLM API 流式响应在终止事件前中断",
            raw_response=reconstructed,
        )
    return reconstructed


class OpenAIChatClient:
    """Synchronous client for streaming and non-streaming New API responses."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        stream: bool = False,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("API key 不能为空")
        self.api_url = normalize_api_url(api_url)
        self._api_key = api_key
        self.timeout = timeout
        self.stream = bool(stream)
        self._urlopen = urlopen or urllib.request.urlopen

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(api_url={self.api_url!r}, "
            f"api_key='<redacted>', timeout={self.timeout!r})"
        )

    @classmethod
    def from_env(
        cls,
        *,
        api_url_env: str = API_URL_ENV,
        api_key_env: str = API_KEY_ENV,
        environ: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        urlopen: Callable[..., Any] | None = None,
    ) -> "OpenAIChatClient":
        env = os.environ if environ is None else environ
        api_url = env.get(api_url_env)
        api_key = env.get(api_key_env)
        missing = [name for name, value in ((api_url_env, api_url), (api_key_env, api_key)) if not value]
        if missing:
            raise LLMAPIError(f"缺少环境变量：{', '.join(missing)}")
        return cls(
            api_url=api_url or "",
            api_key=api_key or "",
            timeout=timeout,
            stream=False,
            urlopen=urlopen,
        )

    def _request_sse(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any],
        request_headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read an SSE response without exposing partial private output."""

        url = f"{self.api_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "show-me-your-novel/2.0 (python-urllib)",
        }
        if request_headers is None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers.update({str(key): str(value) for key, value in request_headers.items()})
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method=method,
        )
        events: list[dict[str, Any]] = []
        data_lines: list[str] = []
        saw_done = False

        def dispatch() -> None:
            nonlocal saw_done
            if not data_lines:
                return
            data = "\n".join(data_lines).strip()
            data_lines.clear()
            if not data:
                return
            if data == "[DONE]":
                saw_done = True
                return
            try:
                decoded = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LLMAPIError(
                    "LLM API 返回了无效 SSE JSON",
                    raw_response={"_stream": {"terminal": False, "events": events}},
                ) from exc
            if not isinstance(decoded, dict):
                raise LLMAPIError("LLM API SSE 事件顶层结构不是对象")
            events.append(decoded)

        try:
            with self._urlopen(request, timeout=timeout or self.timeout) as response:
                for raw_line in response:
                    if isinstance(raw_line, bytes):
                        line = raw_line.decode("utf-8")
                    else:
                        line = str(raw_line)
                    line = line.rstrip("\r\n")
                    if not line:
                        dispatch()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif line.startswith(":"):
                        continue
                dispatch()
        except LLMAPIError:
            raise
        except urllib.error.HTTPError as exc:
            raise LLMAPIError(
                f"LLM API 返回 HTTP {exc.code}",
                status_code=exc.code,
                retry_after_seconds=parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers is not None else None
                ),
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMAPIError("无法连接 LLM API") from exc
        except TimeoutError as exc:
            raise LLMAPIError("LLM API 请求超时") from exc
        except UnicodeDecodeError as exc:
            raise LLMAPIError("LLM API 返回了无效 UTF-8 SSE") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise LLMAPIError(
                "LLM API 流式连接在终止事件前中断",
                raw_response={"_stream": {"terminal": False, "events": events}},
            ) from exc
        if not events:
            raise LLMAPIError("LLM API 返回空 SSE 事件流")
        return events, saw_done

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        request_headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_url}/{path.lstrip('/')}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "show-me-your-novel/2.0 (python-urllib)",
        }
        if request_headers is None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers.update({str(key): str(value) for key, value in request_headers.items()})
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not include the upstream body: it is not needed for control flow
            # and an unusual proxy could echo request credentials into it.
            raise LLMAPIError(
                f"LLM API 返回 HTTP {exc.code}",
                status_code=exc.code,
                retry_after_seconds=parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers is not None else None
                ),
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMAPIError("无法连接 LLM API") from exc
        except TimeoutError as exc:
            raise LLMAPIError("LLM API 请求超时") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise LLMAPIError("LLM API 连接在读取响应时中断") from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMAPIError("LLM API 返回了无效 JSON") from exc
        if not isinstance(decoded, dict):
            raise LLMAPIError("LLM API 返回的顶层结构不是对象")
        if decoded.get("error"):
            # Keep the error safe to log; do not relay arbitrary upstream text.
            raise LLMAPIError("LLM API 返回错误对象")
        return decoded

    def chat_completion(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        request_params: Mapping[str, Any] | None = None,
        stream: bool | None = None,
        timeout: int | None = None,
    ) -> ChatResult:
        """Call ``/v1/chat/completions`` with only explicit request params."""

        if not model.strip():
            raise ValueError("model 不能为空")
        if not messages:
            raise ValueError("messages 不能为空")

        params = dict(request_params or {})
        protected = sorted(_PROTECTED_BODY_FIELDS.intersection(params))
        if protected:
            raise ValueError(f"request_params 不得覆盖：{', '.join(protected)}")
        sensitive = _find_sensitive_field(params)
        if sensitive:
            raise ValueError(f"request_params 含敏感字段：{sensitive}")
        if "stream" in params:
            raise ValueError("stream 是传输选项，不得放入 request_params")

        use_stream = self.stream if stream is None else bool(stream)

        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
        }
        payload.update(params)
        if use_stream:
            payload["stream"] = True
        started = perf_counter()
        if use_stream:
            events, saw_done = self._request_sse(
                "POST", "chat/completions", payload=payload, timeout=timeout
            )
            response = _openai_stream_response(events, saw_done=saw_done)
        else:
            response = self._request_json(
                "POST", "chat/completions", payload=payload, timeout=timeout
            )
        latency_ms = round((perf_counter() - started) * 1000)

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMAPIError("LLM API 返回空 choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise LLMAPIError("LLM API 返回了无效 choice")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise LLMAPIError("LLM API choice 缺少 message")

        raw_content = message.get("content")
        content = _coerce_content_text(raw_content)
        content, inline_reasoning = split_inline_reasoning_text(content)
        separate_reasoning = _coerce_reasoning_text(
            message.get("reasoning_content", message.get("reasoning"))
        )
        embedded_reasoning = _extract_reasoning_blocks(raw_content)
        reasoning = "\n".join(
            part.strip()
            for part in (separate_reasoning, embedded_reasoning, inline_reasoning)
            if part and part.strip()
        )
        if not content.strip():
            finish_reason = first.get("finish_reason")
            reasoning_present = bool(reasoning)
            suffix = (
                f"（finish_reason={finish_reason or 'unknown'}，"
                f"reasoning={'present' if reasoning_present else 'absent'}）"
            )
            raise LLMAPIError(
                "LLM API 返回空内容" + suffix,
                raw_response=response,
            )
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}

        return ChatResult(
            content=content,
            reasoning_content=reasoning,
            usage=dict(usage),
            requested_model=model,
            response_model=(
                str(response["model"]) if response.get("model") is not None else None
            ),
            response_id=(
                str(response["id"]) if response.get("id") is not None else None
            ),
            finish_reason=(
                str(first["finish_reason"])
                if first.get("finish_reason") is not None
                else None
            ),
            native_finish_reason=(
                str(first["finish_reason"])
                if first.get("finish_reason") is not None
                else None
            ),
            protocol=OPENAI_CHAT_COMPLETIONS,
            endpoint_path=protocol_endpoint_path(OPENAI_CHAT_COMPLETIONS),
            latency_ms=latency_ms,
            raw_response=response,
        )

    def anthropic_message(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        request_params: Mapping[str, Any],
        stream: bool | None = None,
        timeout: int | None = None,
    ) -> ChatResult:
        """Call ``/v1/messages`` using an explicit Anthropic Messages payload."""

        if not model.strip():
            raise ValueError("model 不能为空")
        if not messages:
            raise ValueError("messages 不能为空")

        params = dict(request_params)
        protected = sorted(_ANTHROPIC_PROTECTED_BODY_FIELDS.intersection(params))
        if protected:
            raise ValueError(f"request_params 不得覆盖：{', '.join(protected)}")
        sensitive = _find_sensitive_field(params)
        if sensitive:
            raise ValueError(f"request_params 含敏感字段：{sensitive}")
        if "stream" in params:
            raise ValueError("stream 是传输选项，不得放入 request_params")
        unsupported = sorted(set(params) - _ANTHROPIC_REQUEST_FIELDS)
        if unsupported:
            raise ValueError(
                "Anthropic Messages 不支持请求字段：" + ", ".join(unsupported)
            )
        max_tokens = params.get("max_tokens")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise ValueError("Anthropic Messages 的 max_tokens 必须是正整数")

        system_parts: list[str] = []
        wire_messages: list[dict[str, Any]] = []
        conversation_started = False
        for raw_message in messages:
            if not isinstance(raw_message, Mapping):
                raise ValueError("messages 中的元素必须是对象")
            role = str(raw_message.get("role") or "").strip()
            content = raw_message.get("content")
            if role == "system":
                if conversation_started:
                    raise ValueError("Anthropic system message 必须位于对话开头")
                system_text = _coerce_content_text(content).strip()
                if system_text:
                    system_parts.append(system_text)
                continue
            if role not in {"user", "assistant"}:
                raise ValueError(f"Anthropic Messages 不支持 role={role or 'missing'}")
            conversation_started = True
            wire_messages.append({"role": role, "content": content})
        if not wire_messages:
            raise ValueError("Anthropic Messages 缺少 user/assistant 消息")

        payload: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        payload.update(params)
        use_stream = self.stream if stream is None else bool(stream)
        if use_stream:
            payload["stream"] = True

        started = perf_counter()
        request_headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if use_stream:
            events, saw_done = self._request_sse(
                "POST",
                "messages",
                payload=payload,
                request_headers=request_headers,
                timeout=timeout,
            )
            response = _anthropic_stream_response(events, saw_done=saw_done)
        else:
            response = self._request_json(
                "POST",
                "messages",
                payload=payload,
                request_headers=request_headers,
                timeout=timeout,
            )
        latency_ms = round((perf_counter() - started) * 1000)

        raw_content = response.get("content")
        if not isinstance(raw_content, list):
            raise LLMAPIError("LLM API Messages 响应缺少 content 数组")
        content = _coerce_content_text(raw_content)
        forced_tool_content: str | None = None
        tool_choice = params.get("tool_choice")
        if isinstance(tool_choice, Mapping) and tool_choice.get("type") == "tool":
            tool_name = tool_choice.get("name")
            matching_tools = [
                block
                for block in raw_content
                if isinstance(block, Mapping)
                and block.get("type") == "tool_use"
                and block.get("name") == tool_name
                and isinstance(block.get("input"), Mapping)
            ]
            if len(matching_tools) == 1:
                input_schema: object = None
                tools = params.get("tools")
                if isinstance(tools, list):
                    for tool in tools:
                        if isinstance(tool, Mapping) and tool.get("name") == tool_name:
                            input_schema = tool.get("input_schema")
                            break
                normalized_input = _coerce_tool_input_to_schema(
                    matching_tools[0]["input"], input_schema
                )
                forced_tool_content = json.dumps(
                    normalized_input,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                content = forced_tool_content
        content, inline_reasoning = split_inline_reasoning_text(content)
        embedded_reasoning = _extract_reasoning_blocks(raw_content)
        reasoning = "\n".join(
            part.strip()
            for part in (embedded_reasoning, inline_reasoning)
            if part and part.strip()
        )
        native_stop_reason = (
            str(response["stop_reason"])
            if response.get("stop_reason") is not None
            else None
        )
        finish_reason = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
        }.get(native_stop_reason, native_stop_reason)
        if forced_tool_content is not None and native_stop_reason == "tool_use":
            finish_reason = "stop"
        if not content.strip():
            suffix = (
                f"（finish_reason={finish_reason or 'unknown'}，"
                f"reasoning={'present' if reasoning else 'absent'}）"
            )
            raise LLMAPIError(
                "LLM API 返回空内容" + suffix,
                raw_response=response,
            )

        native_usage = response.get("usage")
        usage: dict[str, Any] = (
            dict(native_usage) if isinstance(native_usage, Mapping) else {}
        )

        def token_count(name: str) -> int:
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                return 0
            return max(0, value)

        prompt_tokens = sum(
            token_count(name)
            for name in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        completion_tokens = token_count("output_tokens")
        usage.update(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        )

        return ChatResult(
            content=content,
            reasoning_content=reasoning,
            usage=usage,
            requested_model=model,
            response_model=(
                str(response["model"]) if response.get("model") is not None else None
            ),
            response_id=(
                str(response["id"]) if response.get("id") is not None else None
            ),
            finish_reason=finish_reason,
            native_finish_reason=native_stop_reason,
            protocol=ANTHROPIC_MESSAGES,
            endpoint_path=protocol_endpoint_path(ANTHROPIC_MESSAGES),
            latency_ms=latency_ms,
            raw_response=response,
        )

    def list_models(self, *, timeout: int | None = None) -> frozenset[str]:
        """Return exact wire model ids advertised by ``GET /v1/models``."""

        response = self._request_json("GET", "models", timeout=timeout)
        data = response.get("data")
        if not isinstance(data, list):
            raise LLMAPIError("模型列表缺少 data 数组")
        model_ids: set[str] = set()
        for item in data:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                model_id = item["id"].strip()
                if model_id:
                    model_ids.add(model_id)
        if not model_ids:
            raise LLMAPIError("模型列表为空")
        return frozenset(model_ids)

    def preflight_models(
        self,
        required_models: Iterable[str],
        *,
        timeout: int | None = None,
    ) -> ModelPreflight:
        """Check exact configured wire ids without fuzzy alias substitution."""

        required = tuple(dict.fromkeys(model.strip() for model in required_models if model.strip()))
        if not required:
            raise ValueError("required_models 不能为空")
        available = self.list_models(timeout=timeout)
        missing = tuple(model for model in required if model not in available)
        return ModelPreflight(required=required, available=available, missing=missing)


class ChatClient(OpenAIChatClient):
    """Config-aware facade shared by generation and judging runners."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        stream: bool = False,
        request_defaults: Mapping[str, Any] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(
            api_url,
            api_key,
            timeout=timeout,
            stream=stream,
            urlopen=urlopen,
        )
        self.request_defaults = dict(request_defaults or {})
        sensitive = _find_sensitive_field(self.request_defaults)
        if sensitive:
            raise ValueError(f"request_defaults 含敏感字段：{sensitive}")

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        *,
        provider_id: str = "new-api",
        urlopen: Callable[..., Any] | None = None,
    ) -> "ChatClient":
        providers = config.get("providers")
        if not isinstance(providers, Mapping) or provider_id not in providers:
            raise LLMAPIError(f"config.yaml 中未找到 provider：{provider_id}")
        raw_provider = providers[provider_id]
        if not isinstance(raw_provider, Mapping):
            raise LLMAPIError(f"provider {provider_id} 配置无效")
        provider = dict(raw_provider)

        runtime_env = dict(env or {})
        runtime_env.update(os.environ)
        api_url_env = str(provider.get("base_url_env") or API_URL_ENV)
        api_key_env = str(provider.get("api_key_env") or API_KEY_ENV)
        api_url = runtime_env.get(api_url_env) or provider.get("base_url")
        api_key = runtime_env.get(api_key_env)
        missing = [
            name
            for name, value in ((api_url_env, api_url), (api_key_env, api_key))
            if not value
        ]
        if missing:
            raise LLMAPIError(f"缺少环境变量：{', '.join(missing)}")

        request_defaults = provider.get("request_defaults") or {}
        if not isinstance(request_defaults, Mapping):
            raise LLMAPIError(f"provider {provider_id} 的 request_defaults 必须是对象")
        try:
            timeout = int(provider.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError) as exc:
            raise LLMAPIError(f"provider {provider_id} 的 timeout 无效") from exc
        stream = provider.get("stream", False)
        if not isinstance(stream, bool):
            raise LLMAPIError(f"provider {provider_id} 的 stream 必须是布尔值")
        return cls(
            str(api_url),
            str(api_key),
            timeout=timeout,
            stream=stream,
            request_defaults=request_defaults,
            urlopen=urlopen,
        )

    @staticmethod
    def _request_mapping(value: object, *, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} 必须是对象")
        return dict(value)

    def complete(
        self,
        model_cfg: Mapping[str, Any],
        messages: Sequence[Mapping[str, Any]],
        *,
        stage: str,
        request_overrides: Mapping[str, Any] | None = None,
        stream: bool | None = None,
        timeout: int | None = None,
    ) -> ChatResult:
        """Merge explicit config layers and perform one model completion.

        Precedence is exactly ``provider.request_defaults < model.request <
        model.stages[stage] < request_overrides``.  The stage name itself is
        local metadata and is never sent upstream.  Protocol-required fields
        are separate and cannot be overridden by these optional layers.
        """

        wire_model = model_cfg.get("model")
        if not isinstance(wire_model, str) or not wire_model.strip():
            raise ValueError("模型配置缺少 model")
        protocol = model_protocol(model_cfg)
        required = protocol_required_parameters(model_cfg)

        params = dict(self.request_defaults)
        params.update(
            self._request_mapping(model_cfg.get("request"), label="model.request")
        )
        stages = model_cfg.get("stages") or {}
        if not isinstance(stages, Mapping):
            raise ValueError("model.stages 必须是对象")
        params.update(
            self._request_mapping(stages.get(stage), label=f"model.stages.{stage}")
        )
        params.update(
            self._request_mapping(request_overrides, label="request_overrides")
        )
        conflicts = sorted(set(required).intersection(params))
        if conflicts:
            raise ValueError(
                "协议必填参数不得被可选请求层覆盖：" + ", ".join(conflicts)
            )
        if protocol == OPENAI_CHAT_COMPLETIONS:
            return self.chat_completion(
                model=wire_model,
                messages=messages,
                request_params=params,
                stream=stream,
                timeout=timeout,
            )
        return self.anthropic_message(
            model=wire_model,
            messages=messages,
            request_params={**required, **params},
            stream=stream,
            timeout=timeout,
        )
