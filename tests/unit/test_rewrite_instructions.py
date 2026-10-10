import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "rewrite_instructions.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("rewrite_instructions_module", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response


class _RecordingClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._response


class _SequenceClient:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return self._responses.pop(0)


def test_call_llm_parses_sse_chat_completion_response():
    module = _load_module()
    content = "rewritten instruction body"
    sse_body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"rewritten "}}]}',
            'data: {"choices":[{"delta":{"content":"instruction "}}]}',
            'data: {"choices":[{"delta":{"content":"body"}}]}',
            "data: [DONE]",
            "",
        ]
    )
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
    )
    client = _FakeClient(response)
    cfg = module.LLMConfig(api_key="test-key", max_retries=1)

    result = asyncio.run(module.call_llm(client, cfg, "system", "user"))

    assert result == content


def test_parse_llm_response_parses_json_content_blocks():
    module = _load_module()
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "rewritten "},
                            {"type": "text", "text": "instruction"},
                        ]
                    }
                }
            ]
        },
    )

    assert module._parse_llm_response(response) == "rewritten instruction"


def test_parse_llm_response_rejects_null_content():
    module = _load_module()
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={"choices": [{"message": {"content": None}}]},
    )

    try:
        module._parse_llm_response(response)
    except module.LLMError as exc:
        assert "unexpected response shape" in str(exc)
    else:
        raise AssertionError("expected LLMError for null content")


def test_call_llm_parses_sse_content_blocks():
    module = _load_module()
    sse_body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":[{"type":"text","text":"rewritten "}]}}]}',
            'data: {"choices":[{"delta":{"content":[{"type":"text","text":"instruction"}]}}]}',
            "data: [DONE]",
            "",
        ]
    )
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
    )
    client = _FakeClient(response)
    cfg = module.LLMConfig(api_key="test-key", max_retries=1)

    result = asyncio.run(module.call_llm(client, cfg, "system", "user"))

    assert result == "rewritten instruction"


def test_call_llm_sends_stream_flag_when_enabled():
    module = _load_module()
    response = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={"choices": [{"message": {"content": "rewritten instruction"}}]},
    )
    client = _RecordingClient(response)
    cfg = module.LLMConfig(api_key="test-key", max_retries=1, stream=True)

    result = asyncio.run(module.call_llm(client, cfg, "system", "user"))

    assert result == "rewritten instruction"
    _, kwargs = client.calls[0]
    assert kwargs["json"]["stream"] is True


def test_call_llm_retries_proxy_wrapped_bad_response_status_code():
    module = _load_module()
    first = httpx.Response(
        400,
        headers={"Content-Type": "application/json"},
        json={
            "error": {
                "message": "openai_error",
                "type": "bad_response_status_code",
                "param": "",
                "code": "bad_response_status_code",
            }
        },
    )
    second = httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        json={"choices": [{"message": {"content": "rewritten instruction"}}]},
    )
    client = _SequenceClient([first, second])
    cfg = module.LLMConfig(api_key="test-key", max_retries=2)

    result = asyncio.run(module.call_llm(client, cfg, "system", "user"))

    assert result == "rewritten instruction"
    assert client.calls == 2
