"""GeminiProvider unit tests.

Confirms the Google Gemini round-trip via its OpenAI-compatible endpoint:
- inherits OpenAI function-calling format,
- ``ENDPOINT`` resolves through the profile-driven base URL,
- ``base_url`` profile override redirects the request,
- factory wiring resolves ``gemini`` to ``GeminiProvider``.

Gemini's OpenAI-compatible base URL already includes ``/v1beta/openai``, so
``ENDPOINT`` appends only ``/chat/completions``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from forven.agents.providers import (
    GeminiProvider,
    OpenAIProvider,
    ProviderResponse,
    ToolCall,
    get_provider,
)


def _mock_chat_completion(
    content: str = "",
    tool_calls: list[dict] | None = None,
) -> MagicMock:
    message: dict = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    })
    return resp


def test_factory_resolves_gemini() -> None:
    provider = get_provider("gemini")
    assert isinstance(provider, GeminiProvider)
    assert isinstance(provider, OpenAIProvider)


def test_gemini_default_endpoint() -> None:
    with patch("forven.agents.providers.get_profile", return_value=None):
        provider = GeminiProvider()
        assert provider.ENDPOINT == (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )


def test_gemini_endpoint_override() -> None:
    with patch(
        "forven.agents.providers.get_profile",
        return_value={"base_url": "https://proxy.example.com/"},
    ):
        provider = GeminiProvider()
        assert provider.ENDPOINT == "https://proxy.example.com/chat/completions"


def test_gemini_call_returns_tool_calls() -> None:
    provider = GeminiProvider()
    raw_tc = [{
        "id": "call_xyz",
        "type": "function",
        "function": {
            "name": "list_strategies",
            "arguments": json.dumps({"limit": 5}),
        },
    }]
    fake_resp = _mock_chat_completion(content="Calling tool.", tool_calls=raw_tc)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch("forven.agents.providers.httpx.AsyncClient", return_value=mock_client), \
         patch("forven.agents.providers.get_profile", return_value=None):
        result = asyncio.run(provider.call(
            model_id="gemini-2.5-flash",
            messages=[{"role": "user", "content": "list strategies"}],
            system="you are helpful",
            tools=[{"name": "list_strategies", "description": "x", "input_schema": {}}],
            token="gemini-test-key",
        ))

    assert isinstance(result, ProviderResponse)
    assert "Calling tool." in result.text
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.id == "call_xyz"
    assert call.name == "list_strategies"
    assert call.input == {"limit": 5}
    assert result.stop is False  # tool calls present

    # Verify request shape
    call_args = mock_client.post.call_args
    assert call_args[0][0] == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer gemini-test-key"
    body = call_args[1]["json"]
    assert body["model"] == "gemini-2.5-flash"
    # System prepended into messages array (OpenAI format)
    assert body["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "list_strategies"
    assert body["tool_choice"] == "auto"


def test_gemini_endpoint_override_routes_request() -> None:
    """Profile base_url override must reach the actual HTTP call site."""
    provider = GeminiProvider()
    fake_resp = _mock_chat_completion(content="ok")

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch("forven.agents.providers.httpx.AsyncClient", return_value=mock_client), \
         patch(
             "forven.agents.providers.get_profile",
             return_value={"base_url": "https://proxy.example.com"},
         ):
        asyncio.run(provider.call(
            model_id="gemini-2.0-flash",
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[],
            token="gemini",
        ))

    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://proxy.example.com/chat/completions"
