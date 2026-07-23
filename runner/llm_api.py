"""Small OpenAI-compatible client used by the benchmark runners.

The configured New API instance exposes a single protocol surface:
``/v1/chat/completions``.  This module deliberately stays provider-agnostic and
only sends request parameters that the caller explicitly supplies.

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

_PROTECTED_BODY_FIELDS = frozenset({"model", "messages"})
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
    """Normalized non-streaming Chat Completions result."""

    content: str
    reasoning_content: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    requested_model: str = ""
    response_model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
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


def with_provider_request_defaults(
    config: Mapping[str, Any], model_cfg: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach effective provider defaults to the experiment identity.

    ``ChatClient.complete`` already applies these values on the wire.  Tracking
    them beside the model config ensures run and scoring hashes change when the
    provider-level sampling defaults change.
    """

    result = dict(model_cfg)
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


class OpenAIChatClient:
    """Synchronous, non-streaming client for a New API endpoint."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("API key 不能为空")
        self.api_url = normalize_api_url(api_url)
        self._api_key = api_key
        self.timeout = timeout
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
            urlopen=urlopen,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_url}/{path.lstrip('/')}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "show-me-your-novel/2.0 (python-urllib)",
            },
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
        if params.get("stream") is True:
            raise ValueError("当前客户端仅支持非流式响应")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
        }
        payload.update(params)
        started = perf_counter()
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
        request_defaults: Mapping[str, Any] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(api_url, api_key, timeout=timeout, urlopen=urlopen)
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
        return cls(
            str(api_url),
            str(api_key),
            timeout=timeout,
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
        timeout: int | None = None,
    ) -> ChatResult:
        """Merge explicit config layers and perform one chat completion.

        Precedence is exactly ``provider.request_defaults < model.request <
        model.stages[stage] < request_overrides``.  The stage name itself is
        local metadata and is never sent upstream.
        """

        wire_model = model_cfg.get("model")
        if not isinstance(wire_model, str) or not wire_model.strip():
            raise ValueError("模型配置缺少 model")

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
        return self.chat_completion(
            model=wire_model,
            messages=messages,
            request_params=params,
            timeout=timeout,
        )
