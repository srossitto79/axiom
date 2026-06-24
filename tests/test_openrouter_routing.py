"""Tests for OpenRouter provider adapter + provider:model routing convention."""


from forven.agents.providers import (
    OpenAIProvider,
    OpenRouterProvider,
    get_provider,
)
from forven.ai import _split_provider_model_prefix, normalize_provider_and_model


class TestProviderModelPrefix:
    def test_openrouter_prefix(self):
        provider, model = _split_provider_model_prefix("openrouter:openai/gpt-4o")
        assert provider == "openrouter"
        assert model == "openai/gpt-4o"

    def test_openrouter_prefix_anthropic(self):
        provider, model = _split_provider_model_prefix("openrouter:anthropic/claude-sonnet-4")
        assert provider == "openrouter"
        assert model == "anthropic/claude-sonnet-4"

    def test_openai_prefix(self):
        provider, model = _split_provider_model_prefix("openai:gpt-4o")
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_no_prefix_passthrough(self):
        provider, model = _split_provider_model_prefix("gpt-4o")
        assert provider is None
        assert model == "gpt-4o"

    def test_slash_form_is_not_a_prefix(self):
        """vendor/model is OpenRouter's id form — must NOT be parsed as prefix."""
        provider, model = _split_provider_model_prefix("openai/gpt-4o")
        assert provider is None
        assert model == "openai/gpt-4o"

    def test_unknown_prefix_kept_as_is(self):
        """An unknown 'foo:bar' is not split — caller may legitimately use colons."""
        provider, model = _split_provider_model_prefix("foo:bar")
        assert provider is None
        assert model == "foo:bar"

    def test_alias_prefix_normalized(self):
        provider, model = _split_provider_model_prefix("open-router:openai/gpt-4o")
        assert provider == "openrouter"
        assert model == "openai/gpt-4o"

    def test_empty_string(self):
        provider, model = _split_provider_model_prefix("")
        assert provider is None
        assert model == ""

    def test_none(self):
        provider, model = _split_provider_model_prefix(None)
        assert provider is None
        assert model == ""


class TestNormalizeProviderModelOpenRouter:
    def test_explicit_openrouter_provider_passes_model_through(self):
        provider, model = normalize_provider_and_model("openrouter", "anthropic/claude-sonnet-4")
        assert provider == "openrouter"
        assert model == "anthropic/claude-sonnet-4"

    def test_prefix_overrides_explicit_provider(self):
        """openrouter:X model wins over an explicit provider= argument."""
        provider, model = normalize_provider_and_model("openai", "openrouter:openai/gpt-4o")
        assert provider == "openrouter"
        assert model == "openai/gpt-4o"

    def test_prefix_alone_routes_correctly(self):
        provider, model = normalize_provider_and_model(None, "openrouter:openai/gpt-4o-mini")
        assert provider == "openrouter"
        assert model == "openai/gpt-4o-mini"

    def test_openai_prefix_routes_to_openai(self):
        provider, model = normalize_provider_and_model(None, "openai:gpt-4o")
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_no_prefix_no_provider_falls_through(self):
        """Plain gpt-4o still routes to openai via existing inference."""
        provider, model = normalize_provider_and_model(None, "gpt-4o")
        assert provider == "openai"


class TestOpenRouterProviderAdapter:
    def test_factory_returns_openrouter(self):
        prov = get_provider("openrouter")
        assert isinstance(prov, OpenRouterProvider)
        assert isinstance(prov, OpenAIProvider)  # subclass relationship

    def test_endpoint_overridden(self):
        prov = OpenRouterProvider()
        assert prov.ENDPOINT == "https://openrouter.ai/api/v1/chat/completions"

    def test_extra_headers_present(self):
        prov = OpenRouterProvider()
        headers = prov._extra_headers()
        assert "HTTP-Referer" in headers
        assert "X-Title" in headers
        assert headers["X-Title"] == "Forven"

    def test_openai_provider_has_no_extra_headers(self):
        """Default base behavior: no extra headers — clean OpenAI calls."""
        prov = OpenAIProvider()
        assert prov._extra_headers() == {}

    def test_factory_unknown_raises(self):
        """Unknown provider must fail closed (no silent default to OpenAI) — a
        fail-open default could spend on a provider the operator never chose."""
        import pytest

        with pytest.raises(ValueError):
            get_provider("does-not-exist")
