"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import json
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

import httpx
import json_repair
import litellm
from litellm import acompletion
from loguru import logger

from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from analyst_runtime.providers.registry import find_by_model, find_by_name, find_gateway
from analyst_runtime.utils.tool_calls import sanitize_tool_name

_LLM_MAX_ATTEMPTS = 3
_LLM_RETRY_DELAYS_SECONDS = (0.5, 1.5)
_PROVIDER_BILLING_ERROR_CODE = "ANALYST-RUNTIME-BILLING-001"
_PROVIDER_ERROR_CODE = "ANALYST-RUNTIME-PROVIDER-001"
_request_credentials: ContextVar[tuple[str, str | None, str | None] | None] = ContextVar(
    "analyst_runtime_request_credentials",
    default=None,
)


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        cache_log_path: Path | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.cache_log_path = cache_log_path
        if cache_log_path:
            cache_log_path.parent.mkdir(parents=True, exist_ok=True)

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def set_request_credentials(
        self,
        *,
        api_key: str,
        api_base: str | None = None,
        provider: str | None = None,
    ) -> Token:
        return _request_credentials.set((api_key, api_base, provider))

    def set_request_provider(self, *, provider: str) -> Token | None:
        spec = find_by_name(provider)
        if spec is None:
            return None
        api_key = os.environ.get(spec.env_key, "").strip() if spec.env_key else ""
        return _request_credentials.set(
            (api_key, self._request_provider_base(provider), provider)
        )

    def reset_request_credentials(self, token: object | None) -> None:
        if isinstance(token, Token):
            _request_credentials.reset(token)

    async def verify_request_credentials(self, *, api_key: str, provider: str) -> bool:
        """Verify BYOK without moving provider HTTP behavior into the Web or API."""
        spec = find_by_name(provider)
        if spec is None or not spec.accepts_request_credentials:
            return False
        base_url = spec.default_api_base.rstrip("/")
        if not base_url:
            return False
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    follow_redirects=False,
                    timeout=10.0,
                )
            return response.is_success
        except httpx.HTTPError:
            return False

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        request_credentials = _request_credentials.get()
        request_provider = request_credentials[2] if request_credentials else None
        if request_provider:
            request_spec = find_by_name(request_provider)
            if request_spec and request_spec.is_gateway:
                if request_spec.strip_model_prefix:
                    model = model.split("/")[-1]
                prefix = request_spec.litellm_prefix
                if prefix and not model.startswith(f"{prefix}/"):
                    model = f"{prefix}/{model}"
                return model
        if self._gateway and not request_provider:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"

    @staticmethod
    def _strip_cache_control(messages: list[dict]) -> list[dict]:
        """Return a copy of messages with cache_control removed from all content blocks."""
        result = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = [
                    {k: v for k, v in block.items() if k != "cache_control"}
                    if isinstance(block, dict)
                    else block
                    for block in content
                ]
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        request_credentials = _request_credentials.get()
        request_provider = request_credentials[2] if request_credentials else None
        spec = find_by_name(request_provider) if request_provider else self._gateway
        spec = spec or find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = self._resolve_model(model or self.default_model)

        # Clamp max_tokens to at least 1 — negative or zero values cause
        # LiteLLM to reject the request with "max_tokens must be at least 1".
        max_tokens = max(1, max_tokens)

        # Claude uses explicit cache_control. Nebius/OpenAI-compatible routes use
        # server-side prefix reuse; removing Claude markers does NOT disable caching.
        # Do not send unsupported TTL/breakpoint options to those gateways.
        supports_caching = "anthropic" in model or "claude" in model
        if not supports_caching:
            messages = self._strip_cache_control(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)

        # Pass api_key directly — more reliable than env vars alone
        request_credentials = _request_credentials.get()
        request_api_key = request_credentials[0] if request_credentials else self.api_key
        request_api_base = self.api_base
        if request_credentials:
            _, explicit_api_base, request_provider = request_credentials
            request_api_base = explicit_api_base or self._request_provider_base(request_provider)
        if request_credentials is not None:
            # Passing an explicit empty value is safer than falling through to a
            # different provider's process-wide environment credential.
            kwargs["api_key"] = request_api_key
        elif request_api_key:
            kwargs["api_key"] = request_api_key

        # Pass api_base for custom endpoints
        if request_api_base:
            kwargs["api_base"] = request_api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        if tools:
            # Prompt caching (cache_control) is only supported on Anthropic/Claude models.
            if "anthropic" in model or "claude" in model:
                tools_with_cache = [t.copy() for t in tools]
                tools_with_cache[-1] = {
                    **tools_with_cache[-1],
                    "cache_control": {"type": "ephemeral", "ttl": "5m"},
                }
                kwargs["tools"] = tools_with_cache
            else:
                kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
            try:
                async with asyncio.timeout(300):
                    response = await acompletion(**kwargs)
                parsed = self._parse_response(response)
                parsed.retry_count = attempt - 1
                return parsed
            except Exception as error:
                last_error = error
                transient = isinstance(error, TimeoutError) or self._is_transient_error(error)
                if not transient or attempt == _LLM_MAX_ATTEMPTS:
                    logger.exception(
                        "LLM error after {}/{} attempts (model={}): {}",
                        attempt,
                        _LLM_MAX_ATTEMPTS,
                        model,
                        error,
                    )
                    break
                delay = _LLM_RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    "Transient LLM error on attempt {}/{} (model={}): {}. "
                    "Retrying in {}s without discarding agent context.",
                    attempt,
                    _LLM_MAX_ATTEMPTS,
                    model,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)

        message = str(last_error) if last_error else "unknown provider error"
        return LLMResponse(
            content=f"Error calling LLM: {message}",
            error_code=self._provider_error_code(message),
            finish_reason="error",
            retry_count=max(0, attempt - 1),
        )

    @staticmethod
    def _provider_error_code(message: str) -> str:
        normalized = message.lower()
        billing_markers = (
            "余额不足",
            "无可用资源包",
            "insufficient balance",
            "insufficient credit",
            "insufficient quota",
            "free trial quota for the service has been exhausted",
            "postpaid billing is not enabled",
            "key limit exceeded (total limit)",
            "免费体验额度已耗尽",
            "未开启后付费",
        )
        if any(marker in normalized for marker in billing_markers):
            return _PROVIDER_BILLING_ERROR_CODE
        return _PROVIDER_ERROR_CODE

    @staticmethod
    def _request_provider_base(provider: str | None) -> str | None:
        """Resolve the endpoint for a trusted per-run provider selection."""
        if not provider:
            return None
        spec = find_by_name(provider)
        if spec is None or not spec.accepts_request_credentials:
            return None
        for env_name in spec.request_base_env_names():
            configured = os.environ.get(env_name, "").strip()
            if configured:
                return configured
        return spec.default_api_base if spec and spec.default_api_base else None

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        message = str(error).lower()
        permanent_markers = (
            "authentication",
            "invalid api key",
            "permission denied",
            "unsupported model",
        )
        if any(marker in message for marker in permanent_markers):
            return False
        transient_markers = (
            "connection",
            "empty",
            "expecting value",
            "rate limit",
            "server error",
            "temporarily",
            "timeout",
            "unable to get json response",
        )
        return any(marker in message for marker in transient_markers)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json_repair.loads(args)

                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=sanitize_tool_name(tc.function.name),
                        arguments=args,
                    )
                )

        usage = {}
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = {
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
            }
            cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            details = getattr(u, "prompt_tokens_details", None)
            cached_tokens = (
                details.get("cached_tokens")
                if isinstance(details, dict)
                else getattr(details, "cached_tokens", None)
            )
            if cached_tokens is not None:
                cache_read = cached_tokens
            deepseek_cache_hit = getattr(u, "prompt_cache_hit_tokens", None)
            deepseek_cache_miss = getattr(u, "prompt_cache_miss_tokens", None)
            if deepseek_cache_hit is not None or deepseek_cache_miss is not None:
                deepseek_cache_hit = deepseek_cache_hit or 0
                deepseek_cache_miss = deepseek_cache_miss or 0
                usage["prompt_cache_hit_tokens"] = deepseek_cache_hit
                usage["prompt_cache_miss_tokens"] = deepseek_cache_miss
                cache_read = deepseek_cache_hit
            if (
                cache_write
                or cache_read
                or deepseek_cache_miss is not None
                or cached_tokens is not None
            ):
                usage["cache_write_tokens"] = cache_write
                usage["cache_read_tokens"] = cache_read
                logger.info(
                    "LLM cache: prompt={} cache_write={} cache_read={} cache_miss={}",
                    u.prompt_tokens,
                    cache_write,
                    cache_read,
                    deepseek_cache_miss or 0,
                )
            if self.cache_log_path:
                self._write_cache_log(
                    model=getattr(response, "model", "") or self.default_model,
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    cache_write=cache_write,
                    cache_read=cache_read,
                )

        reasoning_content = getattr(message, "reasoning_content", None)

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            message_id=getattr(response, "id", "") or "",
        )

    def _write_cache_log(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_write: int,
        cache_read: int,
    ) -> None:
        """Append one JSONL record to the per-workspace cache stats debug log.

        Each line captures the raw token counts for a single LLM call so you can
        later compute monthly savings and cache hit rates:
          hit_rate  = sum(cache_read_tokens) / sum(prompt_tokens)
          savings   ≈ cache_read_tokens * (input_price - cache_read_price) per call
        """
        from datetime import datetime, timezone

        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cache_write_tokens": cache_write,
                "cache_read_tokens": cache_read,
            }
            with open(self.cache_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("Failed to write cache log: %s", exc)

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
