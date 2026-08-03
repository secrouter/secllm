"""A tiny OpenAI-compatible server that stands in for vLLM (GPU-free dev/test).

Serves ``/health``, ``/v1/models``, and ``/v1/chat/completions`` (+ ``/v1/completions``),
echoing the prompt. Supports streaming (SSE). Zero dependencies — stdlib only — so the
supervisor can spawn it exactly like a real vLLM worker.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _make_handler(model_id: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self.send_response(200)
                self.end_headers()
                return
            if self.path == "/v1/models":
                self._json(200, {"object": "list", "data": [
                    {"id": model_id, "object": "model", "owned_by": "secllm-mock"}]})
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path not in ("/v1/chat/completions", "/v1/completions"):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                req = {}
            prompt = ""
            for m in req.get("messages", []) or []:
                if m.get("role") == "user":
                    prompt = str(m.get("content", ""))
            reply = f"[mock:{model_id}] echo: {prompt}"[:400]
            created = int(time.time())

            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def sse(delta, finish=None):
                    chunk = {
                        "id": "mock", "object": "chat.completion.chunk", "created": created,
                        "model": model_id,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()

                sse({"role": "assistant"})
                for word in reply.split(" "):
                    sse({"content": word + " "})
                sse({}, finish="stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            self._json(200, {
                "id": "mock", "object": "chat.completion", "created": created, "model": model_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt.split()),
                          "completion_tokens": len(reply.split()),
                          "total_tokens": len(prompt.split()) + len(reply.split())},
            })

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", required=True)
    args = ap.parse_args()
    ThreadingHTTPServer((args.host, args.port), _make_handler(args.model)).serve_forever()


if __name__ == "__main__":
    main()
