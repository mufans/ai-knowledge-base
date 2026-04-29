"""Unified LLM calling client supporting DeepSeek, Qwen, and OpenAI providers.

This module provides a thin abstraction over OpenAI-compatible chat completion
APIs, with automatic retry, cost estimation, and a convenience ``quick_chat``
helper for one-shot prompts.

Usage::

    from pipeline.model_client import quick_chat

    answer = quick_chat("What is retrieval-augmented generation?")
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# .env loader (no third-party dependency)
# ---------------------------------------------------------------------------

_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """Load a ``.env`` file into ``os.environ`` (skips already-set keys).

    Searches upward from this file's directory to the project root for a
    ``.env`` file.  Values are only set if the key is not already present in
    the environment, so system env vars always win over ``.env`` entries.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    # Walk upward from this file to find .env
    current = Path(__file__).resolve().parent
    for _ in range(10):  # safety limit
        candidate = current / ".env"
        if candidate.is_file():
            _parse_dotenv(candidate)
            logger.debug("Loaded .env from %s", candidate)
            return
        parent = current.parent
        if parent == current:
            break
        current = parent


def _parse_dotenv(path: Path) -> None:
    """Parse a ``.env`` file and set unset keys into ``os.environ``."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

# Environment variable names per provider
_PROVIDER_ENV: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "DEEPSEEK_BASE_URL",
        "api_key": "DEEPSEEK_API_KEY",
        "model": "DEEPSEEK_MODEL",
    },
    "qwen": {
        "base_url": "DASHSCOPE_BASE_URL",
        "api_key": "DASHSCOPE_API_KEY",
        "model": "DASHSCOPE_MODEL",
    },
    "openai": {
        "base_url": "OPENAI_BASE_URL",
        "api_key": "OPENAI_API_KEY",
        "model": "OPENAI_MODEL",
    },
}

# Hardcoded defaults (used when env vars are not set)
_PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}

# Pricing: USD per 1M tokens (prompt / completion)
_PRICING: Dict[str, Dict[str, float]] = {
    # DeepSeek (per 1M tokens, cache miss pricing)
    # deepseek-chat ≡ deepseek-v4-flash (non-thinking)
    # deepseek-reasoner ≡ deepseek-v4-flash (thinking)
    "deepseek-chat": {"prompt": 0.14, "completion": 0.28},
    "deepseek-reasoner": {"prompt": 0.14, "completion": 0.28},
    "deepseek-v4-flash": {"prompt": 0.14, "completion": 0.28},
    # deepseek-v4-pro (75% off 至 2026-05-31，原价 prompt=1.74, completion=3.48)
    "deepseek-v4-pro": {"prompt": 0.435, "completion": 0.87},
    # Qwen
    "qwen-max": {"prompt": 1.60, "completion": 6.40},
    "qwen-plus": {"prompt": 0.40, "completion": 1.20},
    "qwen-turbo": {"prompt": 0.10, "completion": 0.30},
    # OpenAI
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "o3-mini": {"prompt": 1.10, "completion": 4.40},
}

_DEFAULT_PROVIDER = "deepseek"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Usage:
    """Token usage statistics returned by the LLM."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclasses.dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str
    usage: Usage
    model: str
    provider: str


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------


class LLMProvider(abc.ABC):
    """Abstract base class for LLM providers."""

    @abc.abstractmethod
    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: A list of message dicts with ``role`` and ``content``.
            model: Model identifier.  If empty the provider picks a default.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the completion.
            timeout: HTTP request timeout in seconds.

        Returns:
            An ``LLMResponse`` instance.
        """


# ---------------------------------------------------------------------------
# OpenAI-compatible implementation
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(LLMProvider):
    """Provider that targets the OpenAI chat-completions HTTP endpoint.

    Works with DeepSeek, Qwen (DashScope), and OpenAI since all three expose
    a ``/v1/chat/completions`` endpoint compatible with the OpenAI schema.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        provider_name: str,
        default_model: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider_name = provider_name
        self._default_model = default_model

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> LLMResponse:
        if not model:
            model = self._default_model

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        logger.debug(
            "LLM request: provider=%s model=%s messages=%d",
            self._provider_name,
            model,
            len(messages),
        )

        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        raw_usage = data.get("usage", {})

        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
        )

        logger.debug(
            "LLM response: tokens=%d cost=$%.6f",
            usage.total_tokens,
            estimate_cost(usage, model),
        )

        return LLMResponse(
            content=content,
            usage=usage,
            model=data.get("model", model),
            provider=self._provider_name,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_provider(provider: Optional[str] = None) -> LLMProvider:
    """Create an ``LLMProvider`` instance from environment configuration.

    Reads configuration from (in priority order):
      1. System environment variables
      2. ``.env`` file in the project root
      3. Hardcoded defaults in this module

    The following environment variables are consulted per-provider:

    ==================  ===================  =====================
    Provider            API key env          Base URL env
    ==================  ===================  =====================
    ``deepseek``        ``DEEPSEEK_API_KEY``  ``DEEPSEEK_BASE_URL``
    ``qwen``            ``DASHSCOPE_API_KEY`` ``DASHSCOPE_BASE_URL``
    ``openai``          ``OPENAI_API_KEY``    ``OPENAI_BASE_URL``
    ==================  ===================  =====================

    Additionally ``LLM_PROVIDER`` selects the provider, and
    ``<PROVIDER>_MODEL`` overrides the default model name.

    Args:
        provider: Provider name (``deepseek``, ``qwen``, or ``openai``).
            When ``None``, reads the ``LLM_PROVIDER`` environment variable,
            falling back to ``deepseek``.

    Returns:
        A configured ``OpenAICompatibleProvider``.

    Raises:
        ValueError: If the provider name is unknown.
        EnvironmentError: If the required API key is not set.
    """
    _load_dotenv()

    name = (provider or os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER)).lower()
    if name not in _PROVIDER_ENV:
        raise ValueError(
            f"Unknown provider '{name}'. Choose from: {', '.join(_PROVIDER_ENV)}"
        )

    env_names = _PROVIDER_ENV[name]
    defaults = _PROVIDER_DEFAULTS[name]

    api_key = os.environ.get(env_names["api_key"], "")
    if not api_key:
        raise EnvironmentError(
            f"API key not found. Set the {env_names['api_key']} environment variable "
            f"(or add it to your .env file)."
        )

    base_url = os.environ.get(env_names["base_url"], "") or defaults["base_url"]
    model = os.environ.get(env_names["model"], "") or defaults["model"]

    return OpenAICompatibleProvider(
        base_url=base_url,
        api_key=api_key,
        provider_name=name,
        default_model=model,
    )


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


def chat_with_retry(
    provider: LLMProvider,
    messages: Sequence[Dict[str, str]],
    *,
    retries: int = 3,
    backoff_base: float = 2.0,
    **kwargs: Any,
) -> LLMResponse:
    """Call ``provider.chat`` with exponential-backoff retry on transient errors.

    Retries are attempted on HTTP 5xx responses and network-level errors only.

    Args:
        provider: The ``LLMProvider`` to use.
        messages: Chat messages.
        retries: Maximum number of retry attempts.
        backoff_base: Base for the exponential backoff (seconds).
        **kwargs: Forwarded to ``provider.chat``.

    Returns:
        The ``LLMResponse`` from a successful call.

    Raises:
        The last encountered exception after all retries are exhausted.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            return provider.chat(messages, **kwargs)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code < 500:
                raise
            last_exc = exc
            logger.warning("HTTP %d on attempt %d/%d", code, attempt + 1, retries + 1)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as exc:
            last_exc = exc
            logger.warning(
                "Network error (%s) on attempt %d/%d",
                type(exc).__name__,
                attempt + 1,
                retries + 1,
            )

        if attempt < retries:
            delay = backoff_base ** attempt
            logger.info("Retrying in %.1fs ...", delay)
            time.sleep(delay)

    assert last_exc is not None  # for type checkers
    raise last_exc


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_cost(usage: Usage, model: str) -> float:
    """Estimate the USD cost for a given usage and model.

    Falls back to zero if the model is not in the pricing table.

    Args:
        usage: Token usage statistics.
        model: Model identifier used in the request.

    Returns:
        Estimated cost in USD.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        return 0.0
    prompt_cost = usage.prompt_tokens * pricing["prompt"] / 1_000_000
    completion_cost = usage.completion_tokens * pricing["completion"] / 1_000_000
    return prompt_cost + completion_cost


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------


def quick_chat(
    prompt: str,
    *,
    model: str = "",
    system: str = "",
    **kwargs: Any,
) -> str:
    """One-shot chat helper — create a provider, call with retry, return text.

    Args:
        prompt: The user prompt.
        model: Model identifier (empty = provider default).
        system: Optional system prompt.
        **kwargs: Forwarded to ``chat_with_retry`` (e.g. ``temperature``,
            ``max_tokens``, ``retries``).

    Returns:
        The assistant's reply text.
    """
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    provider = create_provider()
    response = chat_with_retry(provider, messages, model=model, **kwargs)
    return response.content


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    _load_dotenv()

    provider_name = os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER)
    env_names = _PROVIDER_ENV.get(provider_name, {})
    defaults = _PROVIDER_DEFAULTS.get(provider_name, {})
    api_key_env = env_names.get("api_key", "")
    has_key = bool(os.environ.get(api_key_env))
    base_url = os.environ.get(env_names.get("base_url", "")) or defaults.get("base_url", "N/A")
    model = os.environ.get(env_names.get("model", "")) or defaults.get("model", "N/A")

    print(f"Provider : {provider_name}")
    print(f"Base URL : {base_url}")
    print(f"Model    : {model}")
    print(f"API key  : {api_key_env} ({'set' if has_key else 'NOT SET'})")
    print()

    if not has_key:
        print(
            f"Skipping live test — set {api_key_env} to run an actual LLM call.\n"
            f"Example:  {api_key_env}=sk-xxx python3 pipeline/model_client.py\n"
            f"Or add it to a .env file in the project root."
        )
    else:
        try:
            provider = create_provider()
            messages = [{"role": "user", "content": "请用一句话介绍你自己"}]
            resp = chat_with_retry(provider, messages)
            print(f"Model    : {resp.model}")
            print(f"Content  : {resp.content}")
            print(
                f"Usage    : {resp.usage.prompt_tokens} prompt + "
                f"{resp.usage.completion_tokens} completion = "
                f"{resp.usage.total_tokens} total"
            )
            print(f"Cost     : ${estimate_cost(resp.usage, resp.model):.6f}")
        except Exception as exc:
            print(f"Error: {exc}")
