"""Unit tests for llm_provider.py."""

import pytest
import sys
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field

from llm_provider import (
    ContentPart, TokenUsage, LLMProvider, create_provider,
    _is_retryable_generic, _is_rate_limit_generic,
    extract_pdf_text, _api_semaphore,
)

ANTHROPIC_MODEL = "claude-opus-4-7"
GEMINI_PRO_MODEL = "gemini-3.1-pro-preview"
GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
OPENAI_MODEL = "gpt-5.4"


# --- Test schema for structured output ---
class MockReview(BaseModel):
    verdict: str = Field(description="Verdict")
    summary: str = Field(default="", description="Summary")


# --- Dataclass Tests ---

class TestContentPart:
    def test_text_part(self):
        part = ContentPart(type="text", data="hello world")
        assert part.type == "text"
        assert part.data == "hello world"

    def test_pdf_part(self):
        part = ContentPart(type="pdf", data=b"%PDF-1.4", mime_type="application/pdf")
        assert part.type == "pdf"
        assert isinstance(part.data, bytes)
        assert part.mime_type == "application/pdf"

    def test_default_mime_type(self):
        part = ContentPart(type="text", data="test")
        assert part.mime_type == ""


class TestTokenUsage:
    def test_defaults(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.thinking_tokens == 0

    def test_custom_values(self):
        usage = TokenUsage(input_tokens=100, output_tokens=50, thinking_tokens=25)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.thinking_tokens == 25


# --- Retry Logic ---

class TestRetryLogic:
    def test_rate_limit(self):
        assert _is_retryable_generic(Exception("429 rate limit")) is True
        assert _is_rate_limit_generic(Exception("429 rate limit")) is True

    def test_server_error(self):
        assert _is_retryable_generic(Exception("500 Internal Server Error")) is True
        assert _is_rate_limit_generic(Exception("500 Internal Server Error")) is False

    def test_client_error_not_retryable(self):
        assert _is_retryable_generic(Exception("400 Bad Request")) is False

    def test_overloaded(self):
        assert _is_retryable_generic(Exception("overloaded")) is True

    def test_resource_exhausted(self):
        assert _is_retryable_generic(Exception("resource_exhausted")) is True
        assert _is_rate_limit_generic(Exception("resource_exhausted")) is True

    def test_502_gateway(self):
        assert _is_retryable_generic(Exception("502 Bad Gateway")) is True

    def test_capacity(self):
        assert _is_retryable_generic(Exception("capacity")) is True

    def test_auth_error_not_retryable(self):
        assert _is_retryable_generic(Exception("403 Forbidden")) is False


# --- Factory ---

class TestCreateProvider:
    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("nonexistent", "fake-key")

    def test_known_providers(self):
        for name in ["gemini", "anthropic", "openai"]:
            try:
                provider = create_provider(name, "test-key-placeholder")
                assert provider is not None
                assert provider.name  # has a name property
            except ImportError:
                pass  # SDK not installed

    def test_case_insensitive(self):
        try:
            provider = create_provider("Gemini", "test-key-placeholder")
            assert provider is not None
        except ImportError:
            pass

    def test_whitespace_stripped(self):
        try:
            provider = create_provider("  gemini  ", "test-key-placeholder")
            assert provider is not None
        except ImportError:
            pass


# --- Extract PDF Text ---

class TestExtractPdfText:
    def test_invalid_pdf(self):
        result = extract_pdf_text(b"not a real pdf")
        # Should return an error message, not crash
        assert "could not be extracted" in result.lower() or "extraction failed" in result.lower()

    def test_empty_bytes(self):
        result = extract_pdf_text(b"")
        assert isinstance(result, str)


# --- LLMProvider Base (generate_structured with retry) ---

class ConcreteProvider(LLMProvider):
    """Test provider that delegates to a configurable callable."""

    def __init__(self, generate_fn=None, retryable_fn=None, **kwargs):
        super().__init__(**kwargs)
        self._generate_fn = generate_fn
        self._retryable_fn = retryable_fn

    def _generate_once(self, model, contents, schema, thinking_budget=None, cache_name=None):
        if self._generate_fn:
            return self._generate_fn(model, contents, schema, thinking_budget, cache_name)
        return schema(verdict="Approved"), TokenUsage(input_tokens=10, output_tokens=5)

    def _is_retryable(self, error):
        if self._retryable_fn:
            return self._retryable_fn(error)
        return super()._is_retryable(error)

    @property
    def name(self):
        return "ConcreteTestProvider"


class TestGenerateStructured:
    def test_success_first_attempt(self):
        provider = ConcreteProvider()
        result, usage = provider.generate_structured(
            "test-model",
            [ContentPart(type="text", data="test")],
            MockReview,
        )
        assert result.verdict == "Approved"
        assert usage.input_tokens == 10

    def test_retry_on_retryable_error(self):
        call_count = 0

        def flaky_generate(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("503 Service Unavailable")
            return schema(verdict="OK"), TokenUsage()

        provider = ConcreteProvider(generate_fn=flaky_generate, max_retries=3)
        with patch("llm_provider.time.sleep"):  # skip actual sleep
            result, _ = provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert result.verdict == "OK"
        assert call_count == 2

    def test_no_retry_on_non_retryable_error(self):
        call_count = 0

        def failing_generate(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            raise Exception("400 Bad Request")

        provider = ConcreteProvider(generate_fn=failing_generate, max_retries=3)
        with pytest.raises(Exception, match="400 Bad Request"):
            provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert call_count == 1  # no retry

    def test_raises_after_max_retries(self):
        call_count = 0

        def always_fail(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            raise Exception("503 Service Unavailable")

        provider = ConcreteProvider(generate_fn=always_fail, max_retries=3)
        with patch("llm_provider.time.sleep"):
            with pytest.raises(Exception, match="503"):
                provider.generate_structured(
                    "test-model",
                    [ContentPart(type="text", data="test")],
                    MockReview,
                )
        assert call_count == 3

    def test_rate_limit_uses_longer_wait(self):
        """Rate limit errors should use at least 15s wait."""
        waits = []

        def track_sleep(seconds):
            waits.append(seconds)

        call_count = 0

        def rate_limited(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("429 rate limit exceeded")
            return schema(verdict="OK"), TokenUsage()

        provider = ConcreteProvider(generate_fn=rate_limited, max_retries=3)
        with patch("llm_provider.time.sleep", side_effect=track_sleep):
            result, _ = provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert result.verdict == "OK"
        assert all(w >= 15 for w in waits)  # rate limit waits are >= 15s


# --- Provider-Specific _is_retryable ---

class TestAnthropicRetryable:
    def test_529_overloaded(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("529 overloaded")) is True
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_normal_error_delegates_to_generic(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("400 Bad Request")) is False
            assert provider._is_retryable(Exception("503 error")) is True
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestOpenAIRetryable:
    def test_insufficient_quota_not_retryable(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("insufficient_quota")) is False
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_server_error_retryable(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("500 server error")) is True
        except ImportError:
            pytest.skip("openai SDK not installed")


# --- Content Conversion ---

class TestGeminiContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import GeminiProvider
            provider = GeminiProvider.__new__(GeminiProvider)
            from google.genai import types
            provider._types = types
            parts = [ContentPart(type="text", data="hello")]
            native = provider._to_native_contents(parts)
            assert native == ["hello"]
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import GeminiProvider
            provider = GeminiProvider.__new__(GeminiProvider)
            from google.genai import types
            provider._types = types
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            native = provider._to_native_contents(parts)
            assert len(native) == 1  # unknown skipped
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_pdf_content(self):
        try:
            from llm_provider import GeminiProvider
            from google.genai import types
            provider = GeminiProvider.__new__(GeminiProvider)
            provider._types = types
            parts = [ContentPart(type="pdf", data=b"%PDF-1.4 dummy", mime_type="application/pdf")]
            native = provider._to_native_contents(parts)
            assert len(native) == 1
            assert isinstance(native[0], types.Part)
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_image_content(self):
        try:
            from llm_provider import GeminiProvider
            from google.genai import types
            provider = GeminiProvider.__new__(GeminiProvider)
            provider._types = types
            parts = [ContentPart(type="image", data=b"\x89PNG", mime_type="image/png")]
            native = provider._to_native_contents(parts)
            assert len(native) == 1
            assert isinstance(native[0], types.Part)
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_unknown_type_logs_warning(self, caplog):
        try:
            from llm_provider import GeminiProvider
            from google.genai import types
            provider = GeminiProvider.__new__(GeminiProvider)
            provider._types = types
            with caplog.at_level("WARNING"):
                provider._to_native_contents([ContentPart(type="mystery", data="x")])
            assert any("mystery" in rec.message for rec in caplog.records)
        except ImportError:
            pytest.skip("google-genai SDK not installed")


class TestGeminiThinking:
    def _provider(self):
        from llm_provider import GeminiProvider
        provider = GeminiProvider.__new__(GeminiProvider)
        provider._thinking_warned = False
        return provider

    def test_thinking_model_detection(self):
        try:
            p = self._provider()
            assert p._is_thinking_model(GEMINI_PRO_MODEL)
            assert p._is_thinking_model("gemini-3-pro-preview")
            assert p._is_thinking_model(GEMINI_FLASH_MODEL)
            assert not p._is_thinking_model("gemini-2.5-pro")
            assert not p._is_thinking_model("gemini-1.5-flash")
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_default_thinking_level(self):
        try:
            from llm_provider import GeminiProvider
            assert GeminiProvider._default_thinking_level(GEMINI_PRO_MODEL) == "high"
            assert GeminiProvider._default_thinking_level("gemini-3-pro-preview") == "high"
            assert GeminiProvider._default_thinking_level(GEMINI_FLASH_MODEL) == "high"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_parsed_fallback_uses_text(self):
        """If response.parsed is None, fall back to parsing response.text."""
        try:
            from llm_provider import GeminiProvider
            from google.genai import types
            provider = GeminiProvider.__new__(GeminiProvider)
            provider._types = types
            provider._thinking_warned = False

            fake_response = SimpleNamespace(
                parsed=None,
                text='{"verdict":"OK","summary":"from-text"}',
                usage_metadata=None,
            )
            fake_client = SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **kwargs: fake_response
                )
            )
            provider.client = fake_client
            result, _ = provider._generate_once(
                GEMINI_FLASH_MODEL,
                [ContentPart(type="text", data="hi")],
                MockReview,
            )
            assert result.verdict == "OK"
            assert result.summary == "from-text"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_missing_parsed_and_text_raises(self):
        try:
            from llm_provider import GeminiProvider
            from google.genai import types
            provider = GeminiProvider.__new__(GeminiProvider)
            provider._types = types
            provider._thinking_warned = False

            fake_response = SimpleNamespace(parsed=None, text="", usage_metadata=None)
            fake_client = SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **kwargs: fake_response
                )
            )
            provider.client = fake_client
            with pytest.raises(ValueError):
                provider._generate_once(
                    GEMINI_FLASH_MODEL,
                    [ContentPart(type="text", data="hi")],
                    MockReview,
                )
        except ImportError:
            pytest.skip("google-genai SDK not installed")


class TestAnthropicContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="text", data="hello")]
            blocks = provider._to_content_blocks(parts)
            assert blocks == [{"type": "text", "text": "hello"}]
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_text_with_cache(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="text", data="hello")]
            blocks = provider._to_content_blocks(parts, cache_name="__anthropic_prompt_cache__")
            assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            blocks = provider._to_content_blocks(parts)
            assert len(blocks) == 1
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_pdf_content(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="pdf", data=b"%PDF-1.4", mime_type="application/pdf")]
            blocks = provider._to_content_blocks(parts)
            assert len(blocks) == 1
            block = blocks[0]
            assert block["type"] == "document"
            assert block["source"]["type"] == "base64"
            assert block["source"]["media_type"] == "application/pdf"
            assert isinstance(block["source"]["data"], str)
            assert "cache_control" not in block  # stripped when caching is off
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_pdf_content_with_cache(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="pdf", data=b"%PDF-1.4")]
            blocks = provider._to_content_blocks(parts, cache_name="__anthropic_prompt_cache__")
            assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_image_content(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="image", data=b"\x89PNG", mime_type="image/png")]
            blocks = provider._to_content_blocks(parts)
            assert len(blocks) == 1
            block = blocks[0]
            assert block["type"] == "image"
            assert block["source"]["type"] == "base64"
            assert block["source"]["media_type"] == "image/png"
            assert isinstance(block["source"]["data"], str)
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_unknown_type_logs_warning(self, caplog):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            with caplog.at_level("WARNING"):
                provider._to_content_blocks([ContentPart(type="mystery", data="x")])
            assert any("mystery" in rec.message for rec in caplog.records)
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestAnthropicThinking:
    def _provider(self):
        from llm_provider import AnthropicProvider
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._thinking_warned = False
        return provider

    def test_adaptive_model_detection(self):
        try:
            p = self._provider()
            assert p._is_adaptive_thinking_model(ANTHROPIC_MODEL)
            assert p._is_adaptive_thinking_model("claude-opus-4-6")
            assert p._is_adaptive_thinking_model("claude-sonnet-4-6")
            assert p._is_adaptive_thinking_model("claude-mythos-preview")
            assert not p._is_adaptive_thinking_model("claude-opus-4-5")
            assert not p._is_adaptive_thinking_model("claude-sonnet-4-5")
            assert not p._is_adaptive_thinking_model("claude-sonnet-3-7")
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_effort_for_budget(self):
        try:
            from llm_provider import AnthropicProvider
            assert AnthropicProvider._effort_for_budget(1) == "low"
            assert AnthropicProvider._effort_for_budget(2048) == "low"
            assert AnthropicProvider._effort_for_budget(2049) == "medium"
            assert AnthropicProvider._effort_for_budget(8192) == "medium"
            assert AnthropicProvider._effort_for_budget(8193) == "high"
            assert AnthropicProvider._effort_for_budget(10240) == "high"
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def _run_with_fake_client(self, model, thinking_budget):
        """Run _generate_once against a stub client and return the captured kwargs."""
        from llm_provider import AnthropicProvider
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._thinking_warned = False
        captured = {}
        tool_input = {"verdict": "OK", "summary": ""}
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=tool_input)],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_response

        provider.client = SimpleNamespace(
            messages=SimpleNamespace(create=fake_create)
        )
        provider._generate_once(
            model, [ContentPart(type="text", data="hi")], MockReview,
            thinking_budget=thinking_budget,
        )
        return captured

    def test_adaptive_request_uses_output_config(self):
        try:
            captured = self._run_with_fake_client(ANTHROPIC_MODEL, 10240)
            assert captured["thinking"] == {"type": "adaptive"}
            assert captured["output_config"] == {"effort": "high"}
            assert captured["tool_choice"] == {"type": "auto"}
            assert "temperature" not in captured
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_non_adaptive_model_skips_thinking(self):
        try:
            captured = self._run_with_fake_client("claude-opus-4-5", 10240)
            assert "thinking" not in captured
            assert "output_config" not in captured
            assert "temperature" not in captured
            # Without active thinking the deterministic forced tool_choice is preserved.
            assert captured["tool_choice"] == {"type": "tool", "name": "submit_review"}
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestOpenAIContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [ContentPart(type="text", data="hello")]
            messages = provider._to_input(parts)
            assert messages == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_pdf_sent_natively(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [ContentPart(type="pdf", data=b"fake pdf data")]
            messages = provider._to_input(parts)
            block = messages[0]["content"][0]
            assert block["type"] == "input_file"
            assert block["filename"].endswith(".pdf")
            assert block["file_data"].startswith("data:application/pdf;base64,")
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_image_uses_input_image(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [ContentPart(type="image", data=b"\x89PNG...", mime_type="image/png")]
            messages = provider._to_input(parts)
            block = messages[0]["content"][0]
            assert block["type"] == "input_image"
            assert block["image_url"].startswith("data:image/png;base64,")
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            messages = provider._to_input(parts)
            assert len(messages[0]["content"]) == 1
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_unknown_type_logs_warning(self, caplog):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            with caplog.at_level("WARNING"):
                provider._to_input([ContentPart(type="mystery", data="x")])
            assert any("mystery" in rec.message for rec in caplog.records)
        except ImportError:
            pytest.skip("openai SDK not installed")


class TestOpenAIReasoning:
    def _provider(self):
        from llm_provider import OpenAIProvider
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider._thinking_warned = False
        return provider

    def test_reasoning_model_detection(self):
        try:
            p = self._provider()
            assert p._is_reasoning_model(OPENAI_MODEL)
            assert p._is_reasoning_model("gpt-5.4-mini")
            assert p._is_reasoning_model("o1-preview")
            assert p._is_reasoning_model("o3-mini")
            assert p._is_reasoning_model("o4-mini")
            assert not p._is_reasoning_model("gpt-4o")
            assert not p._is_reasoning_model("gpt-4.1")
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_effort_for_budget(self):
        try:
            from llm_provider import OpenAIProvider
            assert OpenAIProvider._effort_for_budget(1) == "low"
            assert OpenAIProvider._effort_for_budget(2048) == "low"
            assert OpenAIProvider._effort_for_budget(2049) == "medium"
            assert OpenAIProvider._effort_for_budget(8192) == "medium"
            assert OpenAIProvider._effort_for_budget(8193) == "high"
            assert OpenAIProvider._effort_for_budget(10240) == "high"
        except ImportError:
            pytest.skip("openai SDK not installed")


# --- Request-kwargs capture tests ---
# These tests drive each provider's _generate_once against a SimpleNamespace
# client and assert the exact request payload. They catch regressions where
# an API field is renamed, dropped, or shaped incorrectly.


class TestGeminiRequestKwargs:
    def _run(self, model, thinking_budget=None, cache_name=None):
        from llm_provider import GeminiProvider
        from google.genai import types
        provider = GeminiProvider.__new__(GeminiProvider)
        provider._types = types
        provider._thinking_warned = False
        captured = {}
        fake_response = SimpleNamespace(
            parsed=MockReview(verdict="OK", summary=""),
            text='{"verdict":"OK","summary":""}',
            usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=5,
                thoughts_token_count=3,
            ),
        )

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return fake_response

        provider.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=fake_generate)
        )
        provider._generate_once(
            model,
            [ContentPart(type="text", data="hi")],
            MockReview,
            thinking_budget=thinking_budget,
            cache_name=cache_name,
        )
        return captured

    def test_response_schema_always_set(self):
        try:
            captured = self._run(GEMINI_PRO_MODEL)
            config = captured["config"]
            assert config.response_mime_type == "application/json"
            assert config.response_schema is MockReview
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    @staticmethod
    def _level_str(level):
        """Normalise the SDK's ThinkingLevel enum (or raw string) to a lowercase string."""
        raw = getattr(level, "value", level)
        return str(raw).lower()

    def test_gemini_3_uses_thinking_level(self):
        try:
            captured = self._run(GEMINI_PRO_MODEL, thinking_budget=10240)
            config = captured["config"]
            assert config.thinking_config is not None
            assert self._level_str(config.thinking_config.thinking_level) == "high"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_gemini_3_1_pro_defaults_to_high(self):
        try:
            captured = self._run(GEMINI_PRO_MODEL, thinking_budget=1024)
            config = captured["config"]
            assert self._level_str(config.thinking_config.thinking_level) == "high"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_gemini_3_flash_defaults_to_high_even_with_low_budget(self):
        try:
            captured = self._run(GEMINI_FLASH_MODEL, thinking_budget=1024)
            config = captured["config"]
            assert self._level_str(config.thinking_config.thinking_level) == "high"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_non_gemini_3_sets_no_thinking_config(self):
        try:
            captured = self._run("gemini-2.5-flash", thinking_budget=10240)
            config = captured["config"]
            assert config.thinking_config is None
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_gemini_3_sets_high_thinking_even_when_budget_zero(self):
        try:
            captured = self._run(GEMINI_PRO_MODEL, thinking_budget=0)
            config = captured["config"]
            assert self._level_str(config.thinking_config.thinking_level) == "high"
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_cached_content_passed_to_config(self):
        try:
            captured = self._run(GEMINI_PRO_MODEL, cache_name="cachedContents/abc")
            config = captured["config"]
            assert config.cached_content == "cachedContents/abc"
        except ImportError:
            pytest.skip("google-genai SDK not installed")


class TestGeminiTokenUsage:
    def _run_with_response(self, response):
        from llm_provider import GeminiProvider
        from google.genai import types
        provider = GeminiProvider.__new__(GeminiProvider)
        provider._types = types
        provider._thinking_warned = False
        provider.client = SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **k: response)
        )
        _, tok = provider._generate_once(
            GEMINI_PRO_MODEL,
            [ContentPart(type="text", data="hi")],
            MockReview,
        )
        return tok

    def test_populated_from_usage_metadata(self):
        try:
            response = SimpleNamespace(
                parsed=MockReview(verdict="OK"),
                text='{"verdict":"OK"}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=100,
                    candidates_token_count=50,
                    thoughts_token_count=25,
                ),
            )
            tok = self._run_with_response(response)
            assert tok.input_tokens == 100
            assert tok.output_tokens == 50
            assert tok.thinking_tokens == 25
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_missing_usage_metadata_defaults_to_zero(self):
        try:
            response = SimpleNamespace(
                parsed=MockReview(verdict="OK"),
                text='{"verdict":"OK"}',
                usage_metadata=None,
            )
            tok = self._run_with_response(response)
            assert tok.input_tokens == 0
            assert tok.output_tokens == 0
            assert tok.thinking_tokens == 0
        except ImportError:
            pytest.skip("google-genai SDK not installed")


class TestAnthropicRequestShape:
    def _run(self, model=ANTHROPIC_MODEL, thinking_budget=None, cache_name=None):
        from llm_provider import AnthropicProvider
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._thinking_warned = False
        captured = {}
        fake_response = SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input={"verdict": "OK", "summary": ""})],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake_response

        provider.client = SimpleNamespace(
            messages=SimpleNamespace(create=fake_create)
        )
        provider._generate_once(
            model,
            [ContentPart(type="text", data="hi")],
            MockReview,
            thinking_budget=thinking_budget,
            cache_name=cache_name,
        )
        return captured

    def test_tools_array_has_correct_shape(self):
        try:
            captured = self._run()
            tools = captured["tools"]
            assert len(tools) == 1
            tool = tools[0]
            assert set(tool.keys()) == {"name", "description", "input_schema"}
            assert tool["name"] == "submit_review"
            assert tool["input_schema"] == MockReview.model_json_schema()
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_max_tokens_set(self):
        try:
            captured = self._run()
            assert captured["max_tokens"] == 16384
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_nudge_appended_under_adaptive_thinking(self):
        try:
            captured = self._run(model=ANTHROPIC_MODEL, thinking_budget=10240)
            content = captured["messages"][0]["content"]
            tail = content[-1]
            assert tail["type"] == "text"
            assert "submit_review" in tail["text"]
            assert "exactly once" in tail["text"]
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_no_nudge_when_thinking_off(self):
        try:
            captured = self._run(model=ANTHROPIC_MODEL, thinking_budget=0)
            content = captured["messages"][0]["content"]
            assert all("submit_review" not in b.get("text", "") for b in content)
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestAnthropicTokenUsage:
    def _run_with_response(self, response):
        from llm_provider import AnthropicProvider
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider._thinking_warned = False
        provider.client = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **k: response)
        )
        return provider._generate_once(
            ANTHROPIC_MODEL,
            [ContentPart(type="text", data="hi")],
            MockReview,
        )

    def test_populated_from_usage(self):
        try:
            response = SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input={"verdict": "OK", "summary": ""})],
                usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            )
            _, tok = self._run_with_response(response)
            assert tok.input_tokens == 100
            assert tok.output_tokens == 50
            assert tok.thinking_tokens == 0  # Anthropic does not expose thinking tokens
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_missing_tool_block_raises(self):
        try:
            response = SimpleNamespace(
                content=[SimpleNamespace(type="text", text="no tool call here")],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
            with pytest.raises(ValueError, match="tool_use"):
                self._run_with_response(response)
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestOpenAIRequestKwargs:
    def _run(self, model, thinking_budget=None, contents=None):
        from llm_provider import OpenAIProvider
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider._thinking_warned = False
        captured = {}
        fake_response = SimpleNamespace(
            output_parsed=MockReview(verdict="OK", summary=""),
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                output_tokens_details=SimpleNamespace(reasoning_tokens=3),
            ),
        )

        def fake_parse(**kwargs):
            captured.update(kwargs)
            return fake_response

        provider.client = SimpleNamespace(
            responses=SimpleNamespace(parse=fake_parse)
        )
        provider._generate_once(
            model,
            contents or [ContentPart(type="text", data="hi")],
            MockReview,
            thinking_budget=thinking_budget,
        )
        return captured

    def test_text_format_always_set(self):
        try:
            captured = self._run("gpt-4o")
            assert captured["text_format"] is MockReview
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_input_is_list_of_role_blocks(self):
        try:
            captured = self._run("gpt-4o")
            inp = captured["input"]
            assert isinstance(inp, list)
            assert inp[0]["role"] == "user"
            assert isinstance(inp[0]["content"], list)
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_reasoning_passed_for_reasoning_models(self):
        try:
            captured = self._run(OPENAI_MODEL, thinking_budget=10240)
            assert captured["reasoning"] == {"effort": "high"}
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_reasoning_effort_low_for_small_budget(self):
        try:
            captured = self._run("o3-mini", thinking_budget=1024)
            assert captured["reasoning"] == {"effort": "low"}
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_no_reasoning_for_non_reasoning_models(self):
        try:
            captured = self._run("gpt-4o", thinking_budget=10240)
            assert "reasoning" not in captured
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_no_reasoning_when_budget_zero(self):
        try:
            captured = self._run(OPENAI_MODEL, thinking_budget=0)
            assert "reasoning" not in captured
        except ImportError:
            pytest.skip("openai SDK not installed")


class TestOpenAITokenUsage:
    def _run_with_response(self, response):
        from llm_provider import OpenAIProvider
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider._thinking_warned = False
        provider.client = SimpleNamespace(
            responses=SimpleNamespace(parse=lambda **k: response)
        )
        return provider._generate_once(
            OPENAI_MODEL,
            [ContentPart(type="text", data="hi")],
            MockReview,
        )

    def test_populated_with_reasoning_tokens(self):
        try:
            response = SimpleNamespace(
                output_parsed=MockReview(verdict="OK"),
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    output_tokens_details=SimpleNamespace(reasoning_tokens=25),
                ),
            )
            _, tok = self._run_with_response(response)
            assert tok.input_tokens == 100
            assert tok.output_tokens == 50
            assert tok.thinking_tokens == 25
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_no_reasoning_tokens_when_details_missing(self):
        try:
            response = SimpleNamespace(
                output_parsed=MockReview(verdict="OK"),
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    output_tokens_details=None,
                ),
            )
            _, tok = self._run_with_response(response)
            assert tok.input_tokens == 100
            assert tok.output_tokens == 50
            assert tok.thinking_tokens == 0
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_missing_parsed_raises(self):
        try:
            response = SimpleNamespace(output_parsed=None, usage=None)
            with pytest.raises(ValueError, match="parsed"):
                self._run_with_response(response)
        except ImportError:
            pytest.skip("openai SDK not installed")


# --- Cache Default Implementations ---

class TestCacheDefaults:
    def test_default_create_cache_returns_none(self):
        provider = ConcreteProvider()
        assert provider.create_cache("model", []) is None

    def test_default_delete_cache_is_noop(self):
        provider = ConcreteProvider()
        provider.delete_cache("some-cache")  # should not raise

    def test_anthropic_cache_returns_sentinel(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            result = provider.create_cache("model", [])
            assert result == "__anthropic_prompt_cache__"
        except ImportError:
            pytest.skip("anthropic SDK not installed")
