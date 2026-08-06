"""A real OpenAI-compatible inference server backed by Apple's MLX (Metal) — the macOS-native
counterpart to the ``vllm`` backend (see this package's ``__init__.py`` module docstring).

Loads ONE model via ``mlx_lm`` at process startup — blocking, so the process doesn't start
listening until the model is actually ready. The supervisor's health poll just sees "connection
refused" until then, then healthy the moment it can actually serve (matching the generous
``SECLLM_STARTUP_GRACE`` default, 600s, for a large model's first-time download). Model
switching is handled by the supervisor killing this process and spawning a new one for the new
model — the same as ``vllm serve``, not an in-process swap.

Serves ``/health``, ``/v1/models``, ``/v1/chat/completions`` (+ ``/v1/completions``), streaming
(SSE) and non-streaming, in the exact same response shape as :mod:`secllm.backends.mock_server`
(the reference implementation every backend mirrors) — but with real generated text and real
token counts instead of an echo.

MLX's GPU/Metal stream is THREAD-LOCAL — it's only set up on whatever thread called ``load()``
(here, the main thread). Generating on any other thread fails outright ("There is no
Stream(gpu, 1) in current thread", confirmed empirically), not just racily — so unlike
mock_server's plain ``ThreadingHTTPServer``, this uses a single-threaded ``HTTPServer``: every
request, including generation, runs on the same (main) thread MLX was initialized on. This also
naturally serializes generation, which a shared model's KV cache needs anyway.

``--max-context`` (optional; unset = no cap, the pre-existing behavior) is enforced HERE, not
just documented — MLX itself has no ``--max-model-len``-equivalent flag, so a context cap only
means something if this server does the work: :func:`_fit_prompt_to_context` drops the OLDEST
non-system messages (keeping the system message and the most recent turns) until the templated
prompt's token count fits, and :func:`_generate` additionally clamps ``max_tokens`` so
``prompt_tokens + max_tokens`` never exceeds the cap.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

DEFAULT_MAX_TOKENS = 1024


def _fit_prompt_to_context(tokenizer, messages: list[dict], max_context: int) -> tuple[list[int], int]:
    """Render ``messages`` via the model's own chat template — ``apply_chat_template`` returns
    TOKEN IDS directly here (verified against the installed mlx_lm; ``stream_generate`` accepts
    ``List[int]`` natively, so no separate encode/decode round-trip is needed). If the result
    is longer than ``max_context`` tokens, drop the OLDEST non-system message and re-render,
    repeating until it fits (or only one message is left, which is returned regardless — best
    effort, nothing left to drop). ``max_context <= 0`` means "no cap" (unset ``--max-context``,
    the pre-existing behavior) — skips all of this.

    Returns ``(ids, prompt_tokens)``.
    """
    if max_context <= 0:
        ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True)
        return ids, len(ids)
    system = [m for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]
    while True:
        ids = tokenizer.apply_chat_template(system + turns, add_generation_prompt=True)
        if len(ids) <= max_context or len(turns) <= 1:
            return ids, len(ids)
        turns = turns[1:]  # drop the oldest remaining turn, keep the most recent context


def _prompt_from_request(req: dict) -> list[dict]:
    """OpenAI chat ``messages[]`` (preferred) or a plain ``prompt`` string (legacy
    /v1/completions callers), normalized to a ``messages[]`` list either way."""
    messages = req.get("messages") or []
    if not messages:
        messages = [{"role": "user", "content": str(req.get("prompt", ""))}]
    return messages


def _generate(model, tokenizer, ids: list[int], prompt_tokens: int, req: dict, max_context: int):
    """Runs stream_generate to completion, yielding (text_piece, prompt_tokens,
    completion_tokens_so_far) — one shared code path for both the streaming and non-streaming
    HTTP handlers below. Safe to call directly (no lock): the server itself is single-threaded
    (see module docstring), so there's never a concurrent call to serialize against."""
    max_tokens = int(req.get("max_tokens") or DEFAULT_MAX_TOKENS)
    if max_context > 0:
        # Clamp so prompt_tokens + max_tokens never exceeds the cap — a lone huge max_tokens
        # request against an already-near-the-cap prompt would otherwise still overrun it.
        max_tokens = max(1, min(max_tokens, max_context - prompt_tokens))
    kwargs = {"max_tokens": max_tokens}
    if req.get("temperature") is not None:
        # stream_generate has no direct temperature= kwarg — it takes a sampler callable
        # (generate_step's `sampler=`), built via mlx_lm.sample_utils.make_sampler(temp=...).
        kwargs["sampler"] = make_sampler(temp=float(req["temperature"]))
    completion_tokens = 0
    for response in stream_generate(model, tokenizer, ids, **kwargs):
        # GenerationResponse already tracks both counts natively — no need to count ourselves.
        prompt_tokens = response.prompt_tokens or prompt_tokens
        completion_tokens = response.generation_tokens or completion_tokens
        yield response.text, prompt_tokens, completion_tokens


def _make_handler(model, tokenizer, model_id: str, max_context: int):
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
                    {"id": model_id, "object": "model", "owned_by": "secllm-mlx"}]})
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
            try:
                messages = _prompt_from_request(req)
                ids, prompt_tokens = _fit_prompt_to_context(tokenizer, messages, max_context)
            except Exception as e:  # noqa: BLE001 — malformed chat history et al.
                self._json(400, {"error": {"message": f"invalid request: {e}"}})
                return
            created = int(time.time())

            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def sse(delta, finish=None):
                    chunk = {
                        "id": "mlx", "object": "chat.completion.chunk", "created": created,
                        "model": model_id,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()

                sse({"role": "assistant"})
                try:
                    for text, _pt, _ct in _generate(
                        model, tokenizer, ids, prompt_tokens, req, max_context
                    ):
                        if text:
                            sse({"content": text})
                except Exception as e:  # noqa: BLE001 — surface as a normal SSE error chunk
                    sse({"content": f"\n[error: {e}]"})
                sse({}, finish="stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            try:
                reply: list[str] = []
                completion_tokens = 0
                for text, prompt_tokens, completion_tokens in _generate(
                    model, tokenizer, ids, prompt_tokens, req, max_context
                ):
                    reply.append(text)
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": {"message": str(e)}})
                return
            content = "".join(reply)
            self._json(200, {
                "id": "mlx", "object": "chat.completion", "created": created, "model": model_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens,
                          "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
            })

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--model", required=True, help="catalog id — echoed in responses")
    ap.add_argument("--hf-model", required=True, help="HF repo id mlx_lm loads")
    ap.add_argument("--max-context", type=int, default=0,
                     help="cap on prompt+completion tokens; 0 (default) = no cap. MLX has no "
                          "native flag for this — enforced in-process (see module docstring).")
    args = ap.parse_args()
    # Blocking, on purpose — see module docstring: nothing listens until this returns, and
    # everything after runs single-threaded on this same thread (MLX's stream is thread-local).
    model, tokenizer = load(args.hf_model)
    HTTPServer(
        (args.host, args.port), _make_handler(model, tokenizer, args.model, args.max_context)
    ).serve_forever()


if __name__ == "__main__":
    main()
