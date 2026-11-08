from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.config import get_settings
from app.llm import FakeLLM, LLMAuthError, LLMError, ToolResult, ToolSpec, get_llm
from app.llm.anthropic_client import AnthropicLLM

SEARCH = ToolSpec("search_documents", "Search", {"type": "object", "properties": {}})


def test_fake_returns_scripted_replies_in_order_and_records_calls():
    fake = FakeLLM([
        FakeLLM.tool_call("search_documents", {"query": "paying customers"}),
        FakeLLM.text("final answer"),
    ])
    r1 = fake.chat("sys", [fake.user_message("q")], tools=[SEARCH])
    assert r1.stop_reason == "tool_use"
    assert r1.tool_calls[0].name == "search_documents"
    assert r1.tool_calls[0].input == {"query": "paying customers"}

    r2 = fake.chat("sys", [fake.user_message("q")])
    assert (r2.stop_reason, r2.text) == ("end_turn", "final answer")

    assert [c["method"] for c in fake.calls] == ["chat", "chat"]
    assert fake.calls[0]["tools"] == ["search_documents"]


def test_fake_multiple_tool_calls_in_one_turn():
    r = FakeLLM.tool_call("get_metrics", {}, ("get_conflicts", {}))
    assert [c.name for c in r.tool_calls] == ["get_metrics", "get_conflicts"]
    assert r.tool_calls[0].id != r.tool_calls[1].id


def test_fake_reply_can_be_computed_from_the_call():
    fake = FakeLLM([lambda call: {"echo": call["prompt"]}])
    assert fake.complete_json("sys", "hello", {}) == {"echo": "hello"}


def test_fake_raises_when_script_runs_out():
    with pytest.raises(LLMError, match="no scripted reply"):
        FakeLLM().chat("sys", [])


def test_fake_default_reply_used_when_script_is_empty():
    fake = FakeLLM(default={"metrics": []})
    assert fake.complete_json("s", "p", {}) == {"metrics": []}
    assert fake.complete_json("s", "p", {}) == {"metrics": []}


def test_get_llm_uses_model_per_purpose(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_EXTRACT_MODEL", "extract-model")
    monkeypatch.setenv("LLM_CHAT_MODEL", "chat-model")
    assert get_llm("extract").model == "extract-model"
    assert get_llm("chat").model == "chat-model"
    with pytest.raises(ValueError):
        get_llm("other")


def test_missing_credentials_become_llm_auth_error(monkeypatch):
    class NoKeySDK:
        def _fail(**kw):
            raise TypeError('"Could not resolve authentication method. Expected one of api_key..."')
        messages = SimpleNamespace(create=_fail)
        beta = SimpleNamespace(messages=SimpleNamespace(create=_fail))
    with pytest.raises(LLMAuthError, match="API key invalid or missing"):
        AnthropicLLM("m", client=NoKeySDK()).chat("sys", [])


def sdk_that_raises(error):
    def fail(**kw):
        raise error
    return SimpleNamespace(messages=SimpleNamespace(create=fail), beta=SimpleNamespace(messages=SimpleNamespace(create=fail)))


def test_rejected_key_becomes_llm_auth_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=request), body=None)
    with pytest.raises(LLMAuthError, match="API key invalid or missing. Check .env and restart the server."):
        AnthropicLLM("m", client=sdk_that_raises(error)).complete_json("sys", "prompt", {"type": "object"})


def test_network_failure_becomes_llm_error():
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    with pytest.raises(LLMError, match="Could not reach Anthropic API") as caught:
        AnthropicLLM("m", client=sdk_that_raises(error)).complete_json("sys", "prompt", {"type": "object"})
    assert not isinstance(caught.value, LLMAuthError)


def test_get_llm_picks_provider_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert isinstance(get_llm(), FakeLLM)
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ValueError):
        get_llm()


def test_settings_read_model_names_from_env(monkeypatch):
    monkeypatch.setenv("LLM_EXTRACT_MODEL", "extract-model")
    monkeypatch.setenv("LLM_CHAT_MODEL", "chat-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "my-embedder")
    s = get_settings()
    assert (s.llm_extract_model, s.llm_chat_model, s.embedding_model) == \
        ("extract-model", "chat-model", "my-embedder")
    monkeypatch.delenv("EMBEDDING_MODEL")
    assert get_settings().embedding_model == "sentence-transformers/all-MiniLM-L6-v2"


# --- AnthropicLLM, with the SDK client replaced by a stub (no network) ---------

def sdk_message(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


class StubSDK:
    """Mimics anthropic.Anthropic just enough: records create() kwargs, returns a canned message."""

    def __init__(self, message):
        self.kwargs = None
        create = lambda **kw: (setattr(self, "kwargs", kw), message)[1]
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_anthropic_converts_tool_use_and_keeps_full_content():
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text="Let me search."),
        SimpleNamespace(type="tool_use", id="tu_1", name="search_documents", input={"query": "x"}),
    ]
    stub = StubSDK(sdk_message(content, "tool_use"))
    llm = AnthropicLLM("claude-opus-5-5", client=stub)

    r = llm.chat("sys", [llm.user_message("q")], tools=[SEARCH])

    assert r.stop_reason == "tool_use"
    assert r.text == "Let me search."
    assert (r.tool_calls[0].id, r.tool_calls[0].name) == ("tu_1", "search_documents")
    assert r.assistant_message["content"] is content  # thinking block preserved for the next turn
    assert stub.kwargs["model"] == "claude-opus-5-5"
    assert stub.kwargs["tools"][0]["name"] == "search_documents"
    assert stub.kwargs["output_config"] == {"effort": "medium"}
    assert stub.kwargs["fallbacks"] == "default"


def test_anthropic_tool_results_message_format():
    msg = AnthropicLLM("m", client=StubSDK(None)).tool_results_message(
        [ToolResult("tu_1", '{"ok": true}'), ToolResult("tu_2", "boom", is_error=True)])
    assert msg["role"] == "user"
    assert msg["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1",
                                 "content": '{"ok": true}', "is_error": False}
    assert msg["content"][1]["is_error"] is True
    with_note = AnthropicLLM("m", client=StubSDK(None)).tool_results_message([ToolResult("tu_1", "{}")], "stop")
    assert with_note["content"][-1] == {"type": "text", "text": "stop"}


def test_anthropic_complete_json_parses_and_sends_schema():
    stub = StubSDK(sdk_message([SimpleNamespace(type="text", text='{"metrics": []}')], "end_turn"))
    llm = AnthropicLLM("m", fallbacks="", client=stub)
    schema = {"type": "object"}
    assert llm.complete_json("sys", "doc", schema) == {"metrics": []}
    assert stub.kwargs["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert "fallbacks" not in stub.kwargs


def test_anthropic_complete_json_raises_on_refusal():
    stub = StubSDK(sdk_message([], "refusal"))
    with pytest.raises(LLMError, match="refusal"):
        AnthropicLLM("m", client=stub).complete_json("sys", "doc", {})


def test_anthropic_client_sums_token_usage_and_estimates_cost():
    from types import SimpleNamespace

    from app.llm.anthropic_client import AnthropicLLM
    from app.llm.pricing import estimate_usd
    from app.usage import CountingLLM

    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=500)
    message = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=usage)
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: message))
    llm = CountingLLM(AnthropicLLM("claude-opus-5-5", fallbacks="", client=sdk))
    llm.chat("s", [{"role": "user", "content": "hi"}])
    llm.chat("s", [{"role": "user", "content": "hi"}])
    summary = llm.summary()
    assert summary["tokens"] == {"input_tokens": 2000, "output_tokens": 400, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 1000}
    # 2,000 x $4 + 400 x $20 + 1,000 x $0.20, per million tokens
    assert summary["estimated_usd"] == round((2000 * 4 + 400 * 20 + 1000 * 0.2) / 1e6, 4)
    assert estimate_usd("unknown-model", summary["tokens"]) is None
