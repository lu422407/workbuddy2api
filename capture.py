# -*- coding: utf-8 -*-
"""请求头抓包服务器（临时工具）。

用途：把 CodeBuddy / Claude Code / opencode 这类 CLI 指向本服务，
原样记录它们发出的 **路径、请求头、请求体**，并返回一个合法的 OpenAI 响应，
让 CLI 认为调用成功、继续跑完。用于"复刻客户端请求头"这类需求的事实采集。

用法:
    python capture.py [端口，默认 9999]
然后另一个终端:
    set CODEBUDDY_BASE_URL=http://127.0.0.1:9999
    codebuddy -p "say OK"
抓到的内容写在 capture-<端口>.log（含请求头，可能含 token —— 仅本机、用完即删）。
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
LOG = os.path.join(BASE, f"capture-{PORT}.log")


def _log(text: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self) -> bytes:
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _record(self, body: bytes) -> None:
        lines = ["", "=" * 70,
                 f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.command} {self.path}",
                 f"from {self.client_address[0]}:{self.client_address[1]}"]
        for k, v in self.headers.items():
            lines.append(f"  {k}: {v}")
        if body:
            try:
                doc = json.loads(body.decode("utf-8", "replace"))
                meta = {k: doc.get(k) for k in
                        ("model", "stream", "max_tokens", "temperature", "tool_choice")}
                lines.append(f"  body.meta: {json.dumps(meta, ensure_ascii=False)}")
            except Exception:  # noqa: BLE001
                lines.append(f"  body(raw): {body[:400]!r}")
        _log("\n".join(lines))

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._record(b"")
        self._json(200, {"object": "list", "data": [
            {"id": "capture-model", "object": "model", "created": int(time.time()),
             "owned_by": "capture"}]})

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        self._record(body)
        try:
            stream = bool(json.loads(body.decode("utf-8", "replace")).get("stream"))
        except Exception:  # noqa: BLE001
            stream = False
        now = int(time.time())
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            chunks = [
                {"id": "capture-1", "object": "chat.completion.chunk", "created": now,
                 "model": "capture-model",
                 "choices": [{"index": 0, "delta": {"role": "assistant",
                                                    "content": "OK"}, "finish_reason": None}]},
                {"id": "capture-1", "object": "chat.completion.chunk", "created": now,
                 "model": "capture-model",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._json(200, {
                "id": "capture-1", "object": "chat.completion", "created": now,
                "model": "capture-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def main():
    _log(f"capture server on http://127.0.0.1:{PORT} → {LOG}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
