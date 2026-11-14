"""假 DSH web 端点（用例的支持件，**不被 pytest 收集**）。

它是一件**真的 HTTP 服务**，起在回环的临时端口上：`meeting/minutes/dsh_web.py` 那三条 RPC
按真实形状（斜杠端点、`{…, payload:{args:{request:{…}}}}` 信封、`Cookie` 头）打过来，
这里逐条记下请求、按登记的「回法」回一份 `server-response` 信封。

**与真实 DSH 的关系**：它只照抄本层用到的那三条端点的**形状**（不含任何 DSH 代码）。
用例靠这一件把
「我们发出去的到底是什么」钉住 —— 真连 3080 那条路由 `tests/conftest.py` 的第七道兜底
（`_no_real_dsh_web_call`）拦下。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: 回环地址（假端点与真实那台都在这上面，因此判据是「回环 + 端口不是 3080」）
HOST = "127.0.0.1"

#: 三条交付端点的成功回法（`value` 那一格；DSH 的应答就是这个形状）
WORKSPACE_VALUE = {
    "workspace": {
        "workspaceId": "ws-260927",
        "title": "260927",
        "path": "C:/meetings/260927",
    }
}
SESSION_VALUE = {"sessionId": "session-abc"}
PROMPT_VALUE = {"accepted": True}

#: 存活探针那个只读端点的回法（**无参数**）
LIVENESS_VALUE = {"providers": []}

#: 一次「什么都通」的登记表（探针 + 三条交付端点）
HAPPY_ROUTES: dict[str, Any] = {
    "session/modelCatalog": LIVENESS_VALUE,
    "workspace/create": WORKSPACE_VALUE,
    "session/create": SESSION_VALUE,
    "session/prompt": PROMPT_VALUE,
}


class FakeDshWeb:
    """一件假 DSH web 端点：记请求、回信封、起停都随 `with` 走。

    `routes` 是 `{斜杠端点: 回法}`，回法三种写法：

      - `dict` → 当成 `value` 回 `ok=true`；
      - `("error", code, message)` → 回 `ok=false` 与那一对 code / message；
      - `(状态码, 文本)` → 直接回那个 HTTP 状态与文本（401 / 404 那两档用它）。

    没登记的端点回 404（与真实 DSH「方法名对不上回 404」同形）。
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = dict(HAPPY_ROUTES if routes is None else routes)
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeDshWeb":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静音：用例输出只留断言结果
                pass

            def do_POST(self) -> None:  # noqa: N802 - 标准库要求这个名字
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                fake.requests.append(
                    {
                        "path": self.path,
                        "method": body.get("method"),
                        "cookie": self.headers.get("Cookie"),
                        "body": body,
                    }
                )
                spec = fake.routes.get(str(body.get("method")))
                if spec is None:
                    self._plain(404, "not found")
                    return
                if isinstance(spec, tuple) and len(spec) == 2 and isinstance(spec[0], int):
                    self._plain(int(spec[0]), str(spec[1]))
                    return
                if isinstance(spec, tuple) and spec and spec[0] == "error":
                    result: dict[str, Any] = {
                        "ok": False,
                        "error": {"code": spec[1], "message": spec[2]},
                    }
                else:
                    result = {"ok": True, "value": spec}
                self._json(
                    {
                        "type": "server-response",
                        "rpcId": body.get("rpcId"),
                        "result": result,
                    }
                )

            def _plain(self, status: int, text: str) -> None:
                payload = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, envelope: dict[str, Any]) -> None:
                payload = json.dumps(envelope).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer((HOST, 0), Handler)
        # `serve_forever` 的轮询间隔默认 0.5s，关服务端时 `shutdown()` 要等它转到下一个
        # 轮询点；调到 50ms，十几条用例收场都落在百毫秒内（单文件 ≤10 秒那条纪律）。
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        """这一件假端点的基址（端口由系统挑，回环）。"""
        assert self._server is not None
        return f"http://{HOST}:{self._server.server_address[1]}"

    def methods(self) -> list[str]:
        """按先后次序交回被调到的端点名。"""
        return [str(item["method"]) for item in self.requests]

    def request(self, method: str) -> dict[str, Any]:
        """某一条端点的请求（没被调到时当场报错，免得用例拿 `None` 继续往下跑）。"""
        for item in self.requests:
            if item["method"] == method:
                return item
        raise AssertionError(f"{method} 没有被调到；这一趟调的是 {self.methods()}")


def request_of(item: dict[str, Any]) -> dict[str, Any]:
    """一条请求里的端点参数（`payload.args.request` 那一格）。"""
    return dict(item["body"]["payload"]["args"]["request"])


__all__ = [
    "HAPPY_ROUTES",
    "HOST",
    "LIVENESS_VALUE",
    "PROMPT_VALUE",
    "SESSION_VALUE",
    "WORKSPACE_VALUE",
    "FakeDshWeb",
    "request_of",
]
