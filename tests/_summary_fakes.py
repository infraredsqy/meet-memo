"""`tests/_summary_fakes.py` —— 会中总结链的跨文件共用假件（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点）：`live()` 在进程内
真运行时上把转写后端与总结客户端都换成这里那份假件（`health` 可切、一条真实请求都不发）。
原先 `test_summary_action.py` 与 `test_summary_buffer.py` 各养一份（后者是弱副本，
少 `timeouts` / `error` 读数），现在只留这一份正本，两个文件都从这里 import。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from tests import _helpers as helpers


#: 假客户端答的那份合法输出（三条固定标签齐备，跑一次就成功）
GOOD_BULLETS = json.dumps(
    {
        "bullets": [
            "【讲什么】在过下周的排期",
            "【讲到哪】说到第三项",
            "【丢掉什么】18:30 左右本地那一路有个识别失败的缺口",
        ]
    },
    ensure_ascii=False,
)


class LedgerClient:
    """**登记的假总结客户端**：`complete()` 只加计数、按脚本答话，一条请求都不发。

    `calls` 是用例读的那个数 —— 「判定次序真的拦在前面吗」这条判据就落在**它为 0** 上
    （不是「发出去失败了才报错」）。`messages` 留最近一次的入参，供断言窗口文本用。
    """

    def __init__(self, *, content: str = GOOD_BULLETS, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0
        self.timeouts: list[float] = []
        self.messages: list[dict[str, str]] = []

    def complete(self, messages, *, timeout_seconds: float) -> str:
        self.calls += 1
        self.timeouts.append(float(timeout_seconds))
        self.messages = [dict(message) for message in messages]
        if self.error is not None:
            raise self.error
        return self.content


class FakeTranscriber:
    """假转写后端：只实现 runtime 用得着的那几个协议成员（`health` 可切）。"""

    def __init__(self, *, health: str = "normal") -> None:
        self._health = health
        self.line_sink = None
        self.started = 0
        self.stopped = 0

    def bind(self, line_sink, *, config=None) -> None:
        self.line_sink = line_sink

    def start(self, mode: str, session_dir: str) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def health(self) -> str:
        return self._health

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None, *, health: str = "normal"):
    """起一个进程内已启动的运行时；`yield (client, runtime, transcriber, client_stub)`。

    转写后端换成假件（`health` 可切）：真转写后端（`auto`）没起过场次时报 `stopped`，
    而会中总结的正常路径要求转写健康 —— 假件让这一档可控，也让 503 那一档能单独造。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
        # 端点四件套配齐（密钥是**假值**：假客户端从注入点进，一个真实请求都不发出去）。
        # 不配齐的话每一档都会先撞「模型端点没配」那一关 —— 而那一关有它自己的用例。
        "summary.base_url": "http://127.0.0.1:1/v1",
        "summary.api_key": "test-key-not-used",
        "summary.model": "test-model",
    }
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        runtime = client.app.state.runtime
        transcriber = FakeTranscriber(health=health)
        runtime.transcriber_backend = transcriber
        transcriber.bind(runtime.bind_transcriber(transcriber), config=runtime._config_view())
        stub = LedgerClient()
        runtime.summary_client = stub
        yield client, runtime, transcriber, stub
