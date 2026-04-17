"""LLM Provider abstraction for multi-provider support.

Supports Gemini, Anthropic (Claude), and OpenAI (GPT) as review backends.
Each provider handles structured output, multimodal content, caching,
and extended thinking according to its native API.
"""

import json
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from pydantic import BaseModel


@dataclass
class ContentPart:
    """Provider-agnostic content part."""
    type: str          # "text", "pdf", "image"
    data: Union[str, bytes]
    mime_type: str = ""


@dataclass
class TokenUsage:
    """Provider-agnostic token usage."""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


# Rate limiter shared across all providers
_api_semaphore = threading.Semaphore(3)


def _is_retryable_generic(error: Exception) -> bool:
    """Generic retryability check based on error message."""
    error_str = str(error).lower()
    return any(s in error_str for s in [
        '429', 'rate limit', 'resource_exhausted',
        '500', '502', '503', '504',
        'overloaded', 'capacity', 'server_error',
    ])


def _is_rate_limit_generic(error: Exception) -> bool:
    error_str = str(error).lower()
    return '429' in error_str or 'rate limit' in error_str or 'resource_exhausted' in error_str


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pymupdf. Fallback for providers without native PDF support."""
    try:
        import pymupdf
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n\n".join(text_parts)
    except ImportError:
        logging.warning("pymupdf not installed. PDF text extraction unavailable. Install with: pip install pymupdf")
        return "[PDF content could not be extracted — pymupdf not installed]"
    except Exception as e:
        logging.warning(f"PDF text extraction failed: {e}")
        return f"[PDF content could not be extracted: {e}]"


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self._thinking_warned = False

    @abstractmethod
    def _generate_once(
        self,
        model: str,
        contents: List[ContentPart],
        schema: Type[BaseModel],
        thinking_budget: Optional[int] = None,
        cache_name: Optional[str] = None,
    ) -> Tuple[BaseModel, TokenUsage]:
        """Single attempt at structured generation. Implemented by each provider."""

    def generate_structured(
        self,
        model: str,
        contents: List[ContentPart],
        schema: Type[BaseModel],
        thinking_budget: Optional[int] = None,
        cache_name: Optional[str] = None,
    ) -> Tuple[BaseModel, TokenUsage]:
        """Generate structured output with retry and rate limiting.
        Returns (parsed_pydantic_object, token_usage)."""
        with _api_semaphore:
            for attempt in range(self.max_retries):
                try:
                    return self._generate_once(model, contents, schema, thinking_budget, cache_name)
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise
                    if not self._is_retryable(e):
                        raise
                    wait_time = 2 ** (attempt + 1) + random.random()
                    if _is_rate_limit_generic(e):
                        wait_time = max(wait_time, 15) + random.uniform(0, 2)
                    logging.warning(f"Retryable API error (attempt {attempt + 1}/{self.max_retries}): {e}")
                    time.sleep(wait_time)

    def _is_retryable(self, error: Exception) -> bool:
        """Override for provider-specific retryability checks."""
        return _is_retryable_generic(error)

    def create_cache(self, model: str, contents: List[ContentPart], ttl: int = 3600) -> Optional[str]:
        """Create a content cache. Returns cache name/handle, or None if unsupported."""
        return None

    def delete_cache(self, cache_name: str) -> None:
        """Delete a content cache."""
        pass

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ---------------------------------------------------------------------------
# Gemini Provider
# ---------------------------------------------------------------------------

class GeminiProvider(LLMProvider):
    """Google Gemini API provider.

    Scoped to the Gemini 3 family (e.g. `gemini-3.1-pro-preview`,
    `gemini-3-flash-preview`), which uses
    `ThinkingConfig(thinking_level=...)` rather than the Gemini 2.5-style
    integer `thinking_budget`. For the supported Gemini 3 review models, we
    explicitly request `high` thinking by default.
    """

    # Model-name prefixes that accept ThinkingConfig(thinking_level=...).
    _THINKING_MODEL_PREFIXES = ("gemini-3",)

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        from google import genai
        from google.genai import types
        self._genai = genai
        self._types = types
        self.client = genai.Client(api_key=api_key)

    def _to_native_contents(self, contents: List[ContentPart]) -> list:
        """Convert ContentParts to Gemini's native format (list of str/Part)."""
        native = []
        for part in contents:
            if part.type == "text":
                native.append(part.data)
            elif part.type == "pdf":
                native.append(self._types.Part.from_bytes(
                    data=part.data, mime_type="application/pdf"
                ))
            elif part.type == "image":
                native.append(self._types.Part.from_bytes(
                    data=part.data, mime_type=part.mime_type or "image/png"
                ))
            else:
                logging.warning(f"Unknown ContentPart type '{part.type}' — skipping")
        return native

    def _is_thinking_model(self, model: str) -> bool:
        return model.startswith(self._THINKING_MODEL_PREFIXES)

    @staticmethod
    def _default_thinking_level(model: str) -> str:
        """Return the default Gemini 3 thinking level for supported review models."""
        return "high"

    def _generate_once(self, model, contents, schema, thinking_budget=None, cache_name=None):
        kwargs = {
            'response_mime_type': 'application/json',
            'response_schema': schema,
        }
        if cache_name:
            kwargs['cached_content'] = cache_name
        if self._is_thinking_model(model):
            kwargs['thinking_config'] = self._types.ThinkingConfig(
                thinking_level=self._default_thinking_level(model)
            )
        elif thinking_budget and not self._thinking_warned:
                logging.warning(
                    f"Model '{model}' is outside the supported Gemini 3 family; "
                    f"thinking_budget will be ignored."
                )
                self._thinking_warned = True
        config = self._types.GenerateContentConfig(**kwargs)

        native_contents = self._to_native_contents(contents)
        response = self.client.models.generate_content(
            model=model, contents=native_contents, config=config
        )

        usage = TokenUsage()
        meta = getattr(response, 'usage_metadata', None)
        if meta:
            usage.input_tokens = getattr(meta, 'prompt_token_count', 0) or 0
            usage.output_tokens = getattr(meta, 'candidates_token_count', 0) or 0
            usage.thinking_tokens = getattr(meta, 'thoughts_token_count', 0) or 0

        parsed = getattr(response, 'parsed', None)
        if parsed is None:
            text = getattr(response, 'text', None)
            if not text:
                raise ValueError("Gemini response contained neither parsed output nor text")
            parsed = schema.model_validate_json(text)
        return parsed, usage

    def create_cache(self, model, contents, ttl=3600):
        try:
            native_contents = self._to_native_contents(contents)
            cache = self.client.caches.create(
                model=model,
                config=self._types.CreateCachedContentConfig(
                    contents=native_contents,
                    ttl=f"{ttl}s",
                    display_name="External Context Cache"
                )
            )
            logging.info(f"Created Gemini context cache: {cache.name}")
            return cache.name
        except Exception as e:
            logging.warning(f"Gemini cache creation failed: {e}")
            return None

    def delete_cache(self, cache_name):
        try:
            self.client.caches.delete(name=cache_name)
            logging.info("Deleted Gemini context cache.")
        except Exception as e:
            logging.warning(f"Failed to delete Gemini cache: {e}")


# ---------------------------------------------------------------------------
# Anthropic Provider
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider. Uses tool use for structured output.

    Scoped to adaptive-thinking models (Opus 4.7, Opus 4.6, Sonnet 4.6, Mythos),
    which use `thinking={type: "adaptive"}` together with `output_config.effort`.
    Older models are outside scope; if supplied, thinking is skipped with a
    one-time warning.
    """

    _ADAPTIVE_THINKING_MODEL_PREFIXES = (
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-mythos",
    )

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)

    def _is_adaptive_thinking_model(self, model: str) -> bool:
        return model.startswith(self._ADAPTIVE_THINKING_MODEL_PREFIXES)

    @staticmethod
    def _effort_for_budget(thinking_budget: int) -> str:
        """Map an Anthropic-style token budget to an adaptive-thinking effort level."""
        if thinking_budget <= 2048:
            return "low"
        if thinking_budget <= 8192:
            return "medium"
        return "high"

    def _to_content_blocks(self, contents: List[ContentPart], cache_name: Optional[str] = None) -> list:
        """Convert ContentParts to Anthropic content blocks."""
        import base64
        blocks = []
        for part in contents:
            if part.type == "text":
                block = {"type": "text", "text": part.data}
                if cache_name:
                    block["cache_control"] = {"type": "ephemeral"}
                blocks.append(block)
            elif part.type == "pdf":
                blocks.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(part.data).decode("utf-8"),
                    },
                    "cache_control": {"type": "ephemeral"} if cache_name else None,
                })
            elif part.type == "image":
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime_type or "image/png",
                        "data": base64.standard_b64encode(part.data).decode("utf-8"),
                    },
                })
            else:
                logging.warning(f"Unknown ContentPart type '{part.type}' — skipping")
        # Strip None cache_control fields
        for block in blocks:
            if isinstance(block, dict) and block.get("cache_control") is None:
                block.pop("cache_control", None)
        return blocks

    def _generate_once(self, model, contents, schema, thinking_budget=None, cache_name=None):
        content_blocks = self._to_content_blocks(contents, cache_name)
        tool_schema = schema.model_json_schema()

        use_adaptive_thinking = bool(thinking_budget) and self._is_adaptive_thinking_model(model)
        if thinking_budget and not use_adaptive_thinking and not self._thinking_warned:
            logging.warning(
                f"Model '{model}' is outside the supported adaptive-thinking family "
                f"(Opus 4.7/4.6, Sonnet 4.6, Mythos); thinking_budget will be ignored."
            )
            self._thinking_warned = True

        # Anthropic forbids forced tool_choice ('any' or specific 'tool') while
        # extended thinking is active. Fall back to 'auto' in that case and
        # steer the model with an explicit instruction.
        if use_adaptive_thinking:
            content_blocks = content_blocks + [{
                "type": "text",
                "text": (
                    f"Respond by calling the `submit_review` tool exactly once "
                    f"with a valid {schema.__name__} object. Do not emit the "
                    f"result as plain text."
                ),
            }]
            tool_choice = {'type': 'auto'}
        else:
            tool_choice = {'type': 'tool', 'name': 'submit_review'}

        kwargs = {
            'model': model,
            'max_tokens': 16384,
            'messages': [{'role': 'user', 'content': content_blocks}],
            'tools': [{
                'name': 'submit_review',
                'description': f'Submit structured results as a {schema.__name__} object.',
                'input_schema': tool_schema,
            }],
            'tool_choice': tool_choice,
        }

        if use_adaptive_thinking:
            kwargs['thinking'] = {'type': 'adaptive'}
            kwargs['output_config'] = {'effort': self._effort_for_budget(thinking_budget)}

        response = self.client.messages.create(**kwargs)

        # Extract tool use result
        tool_block = next((b for b in response.content if b.type == 'tool_use'), None)
        if tool_block is None:
            raise ValueError("Anthropic response did not contain a tool_use block")
        parsed = schema.model_validate(tool_block.input)

        usage = TokenUsage()
        if hasattr(response, 'usage'):
            usage.input_tokens = getattr(response.usage, 'input_tokens', 0) or 0
            usage.output_tokens = getattr(response.usage, 'output_tokens', 0) or 0
            # Anthropic does not expose thinking tokens in the usage object;
            # cache_creation_input_tokens is for prompt caching, not thinking.
            # We leave thinking_tokens as 0 for Anthropic.

        return parsed, usage

    def create_cache(self, model, contents, ttl=3600):
        # Anthropic uses per-request prompt caching via cache_control headers.
        # Return a sentinel so callers know caching is "active" (content blocks
        # will be annotated with cache_control at send time).
        return "__anthropic_prompt_cache__"

    def delete_cache(self, cache_name):
        pass  # Anthropic prompt caching is per-request, no cleanup needed

    def _is_retryable(self, error: Exception) -> bool:
        error_str = str(error).lower()
        if any(s in error_str for s in ['529', 'overloaded']):
            return True
        return _is_retryable_generic(error)


# ---------------------------------------------------------------------------
# OpenAI Provider
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider):
    """OpenAI GPT API provider. Uses the Responses API with structured output.

    Reasoning-capable models (o1/o3/o4/gpt-5 families) map `thinking_budget` to
    a `reasoning.effort` level. Non-reasoning models ignore `thinking_budget`
    with a one-time warning. PDFs are sent natively via `input_file`.
    """

    # Model-name prefixes that accept the `reasoning` parameter.
    _REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def __init__(self, api_key: str, **kwargs):
        super().__init__(**kwargs)
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)

    def _to_input(self, contents: List[ContentPart]) -> list:
        """Convert ContentParts to Responses API input blocks."""
        import base64
        content_blocks = []
        for part in contents:
            if part.type == "text":
                content_blocks.append({"type": "input_text", "text": part.data})
            elif part.type == "pdf":
                b64 = base64.standard_b64encode(part.data).decode("utf-8")
                content_blocks.append({
                    "type": "input_file",
                    "filename": "document.pdf",
                    "file_data": f"data:application/pdf;base64,{b64}",
                })
            elif part.type == "image":
                b64 = base64.standard_b64encode(part.data).decode("utf-8")
                mime = part.mime_type or "image/png"
                content_blocks.append({
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{b64}",
                })
            else:
                logging.warning(f"Unknown ContentPart type '{part.type}' — skipping")
        return [{"role": "user", "content": content_blocks}]

    def _is_reasoning_model(self, model: str) -> bool:
        return model.startswith(self._REASONING_MODEL_PREFIXES)

    @staticmethod
    def _effort_for_budget(thinking_budget: int) -> str:
        """Map an Anthropic-style token budget to an OpenAI effort level."""
        if thinking_budget <= 2048:
            return "low"
        if thinking_budget <= 8192:
            return "medium"
        return "high"

    def _generate_once(self, model, contents, schema, thinking_budget=None, cache_name=None):
        kwargs = {
            'model': model,
            'input': self._to_input(contents),
            'text_format': schema,
        }

        if thinking_budget:
            if self._is_reasoning_model(model):
                kwargs['reasoning'] = {'effort': self._effort_for_budget(thinking_budget)}
            elif not self._thinking_warned:
                logging.warning(
                    f"Model '{model}' is not a reasoning model; thinking_budget will be ignored."
                )
                self._thinking_warned = True

        response = self.client.responses.parse(**kwargs)

        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("OpenAI response did not contain parsed structured output")

        usage = TokenUsage()
        if getattr(response, 'usage', None):
            usage.input_tokens = getattr(response.usage, 'input_tokens', 0) or 0
            usage.output_tokens = getattr(response.usage, 'output_tokens', 0) or 0
            details = getattr(response.usage, 'output_tokens_details', None)
            if details is not None:
                usage.thinking_tokens = getattr(details, 'reasoning_tokens', 0) or 0

        return parsed, usage

    def _is_retryable(self, error: Exception) -> bool:
        error_str = str(error).lower()
        if 'insufficient_quota' in error_str:
            return False  # quota exceeded is not retryable
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
