"""LLM Provider abstraction for multi-provider support.

Supports Gemini, Anthropic (Claude), and OpenAI (GPT) as backends.
Provides text generation and JSON generation for the summary pipeline.
"""

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class TokenUsage:
    """Provider-agnostic token usage."""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


_api_semaphore = threading.Semaphore(5)


def _is_retryable_generic(error: Exception) -> bool:
    error_str = str(error).lower()
    return any(s in error_str for s in [
        '429', 'rate limit', 'resource_exhausted',
        '500', '502', '503', '504',
        'overloaded', 'capacity', 'server_error',
    ])


def _is_rate_limit_generic(error: Exception) -> bool:
    error_str = str(error).lower()
    return '429' in error_str or 'rate limit' in error_str or 'resource_exhausted' in error_str


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._thinking_warned = False

    @abstractmethod
    def _generate_text_once(self, model: str, prompt: str, json_mode: bool = False) -> Tuple[str, TokenUsage]:
        """Single attempt. Returns (text_response, usage)."""

    def _is_retryable(self, error: Exception) -> bool:
        return _is_retryable_generic(error)

    def _with_retry(self, fn):
        """Execute fn with retry, rate limiting, and backoff."""
        with _api_semaphore:
            for attempt in range(self.max_retries):
                try:
                    return fn()
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise
                    if not self._is_retryable(e):
                        raise
                    wait_time = 2 ** (attempt + 1)
                    if _is_rate_limit_generic(e):
                        wait_time = max(wait_time, 15)
                    logging.warning(f"Retryable API error (attempt {attempt + 1}/{self.max_retries}): {e}")
                    time.sleep(wait_time)

    def generate_text(self, model: str, prompt: str) -> Tuple[Optional[str], TokenUsage]:
        """Generate a text response with retry. Returns (text, usage)."""
        try:
            return self._with_retry(lambda: self._generate_text_once(model, prompt, json_mode=False))
        except Exception as e:
            logging.error(f"API call failed after retries: {e}")
            return None, TokenUsage()

    def generate_json(self, model: str, prompt: str) -> Tuple[Optional[str], TokenUsage]:
        """Generate a JSON response with retry. Returns (json_string, usage)."""
        try:
            return self._with_retry(lambda: self._generate_text_once(model, prompt, json_mode=True))
        except Exception as e:
            logging.error(f"API call failed after retries: {e}")
            return None, TokenUsage()

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        from google import genai
        from google.genai import types
        self._types = types
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def _generate_text_once(self, model, prompt, json_mode=False):
        config_kwargs = {}
        if json_mode:
            config_kwargs['response_mime_type'] = 'application/json'
        config = self._types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        if config:
            response = self.client.models.generate_content(model=model, contents=prompt, config=config)
        else:
            response = self.client.models.generate_content(model=model, contents=prompt)

        usage = TokenUsage()
        meta = getattr(response, 'usage_metadata', None)
        if meta:
            usage.input_tokens = getattr(meta, 'prompt_token_count', 0) or 0
            usage.output_tokens = getattr(meta, 'candidates_token_count', 0) or 0
            usage.thinking_tokens = getattr(meta, 'thoughts_token_count', 0) or 0

        return response.text, usage


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        import anthropic
        self.client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    def _generate_text_once(self, model, prompt, json_mode=False):
        kwargs = {
            'model': model,
            'max_tokens': 8192,
            'messages': [{'role': 'user', 'content': prompt}],
        }

        response = self.client.messages.create(**kwargs)
        text = response.content[0].text if response.content else ""

        usage = TokenUsage()
        if hasattr(response, 'usage'):
            usage.input_tokens = getattr(response.usage, 'input_tokens', 0) or 0
            usage.output_tokens = getattr(response.usage, 'output_tokens', 0) or 0

        return text, usage

    def _is_retryable(self, error):
        error_str = str(error).lower()
        if '529' in error_str or 'overloaded' in error_str:
            return True
        return _is_retryable_generic(error)


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        from openai import OpenAI
        self.client = OpenAI(api_key=OPENAI_API_KEY)

    def _generate_text_once(self, model, prompt, json_mode=False):
        kwargs = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if json_mode:
            kwargs['response_format'] = {"type": "json_object"}

        response = self.client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""

        usage = TokenUsage()
        if hasattr(response, 'usage') and response.usage:
            usage.input_tokens = getattr(response.usage, 'prompt_tokens', 0) or 0
            usage.output_tokens = getattr(response.usage, 'completion_tokens', 0) or 0

        return text, usage

    def _is_retryable(self, error):
        if 'insufficient_quota' in str(error).lower():
            return False
        return _is_retryable_generic(error)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    'gemini': GeminiProvider,
    'anthropic': AnthropicProvider,
    'openai': OpenAIProvider,
}


def create_provider(provider_name: str, api_key: str, **kwargs) -> LLMProvider:
    """Factory function to create an LLM provider by name."""
    name = provider_name.lower().strip()
    if name not in _PROVIDERS:
        supported = ', '.join(sorted(_PROVIDERS.keys()))
        raise ValueError(f"Unknown provider '{provider_name}'. Supported: {supported}")
    return _PROVIDERS[name](api_key=api_key, **kwargs)
