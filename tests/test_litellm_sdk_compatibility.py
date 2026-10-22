"""Exercise the installed SDKs through a local OpenAI-compatible endpoint."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.mark.asyncio
async def test_real_litellm_request_transformation_with_installed_mcp(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from litellm import acompletion

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({
                "id": "chatcmpl-local-test", "object": "chat.completion",
                "created": 1, "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "compatibility-ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            result = await acompletion(
                model="openai/test-model", api_key="dummy-local-test",
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                messages=[{"role": "user", "content": "hello"}],
                timeout=5, num_retries=0,
            )
            assert result.choices[0].message.content == "compatibility-ok"
        finally:
            server.shutdown()
            thread.join(timeout=2)
