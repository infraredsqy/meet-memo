"""`tests/_minutes_fakes.py` —— 会后纪要 / post hook 链的跨文件共用假件（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点）：`GOOD_FINAL` 是
「一份过得了判据的收尾稿」这份共用样本；`RecordingLauncher` / `FakeHook` / `FakeResolution`
/ `RecordingConverter` 假掉「拉起子会话 / 解析钩子 / 转码」三处接缝；`write_minutes_done()`
/ `write_final()` / `DelayedHookImplementation` 摆出「真 `MinutesHook` 先落 `running`、
隔一会儿落终态」的那个时序现场。原先分别住在 `test_minutes_launch.py` / `test_cli_post.py`
/ `test_post_idempotency.py`、被这条链上的用例文件互相 import 的正本搬到这里，用法一律
`from tests._minutes_fakes import ...`。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from meeting.minutes import status as minutes_status
from meeting.minutes.launch import APPENDIX_HEADING, UNKNOWN_HEADING
from meeting.offline.minutes_tail import MINUTES_FINAL_NAME, MINUTES_STATUS_NAME


#: 一份合格的纪要（正文 + 那张表里有一行数据行 + `### 猜不出的`）
GOOD_FINAL = "\n".join(
    [
        "# 250919 会议纪要 — 2025-09-19",
        "",
        "## 会议概要",
        "",
        "这一场定了两件事。",
        "",
        APPENDIX_HEADING,
        "",
        "| 路 | 编号 | 人名 | 把握 | 依据 |",
        "| --- | --- | --- | --- | --- |",
        "| loopback | S01 | 张三 | 中 | 会上自称负责部署 |",
        "",
        UNKNOWN_HEADING,
        "",
        "- loopback S03：只被叫过「那个谁」",
        "",
    ]
)


class RecordingLauncher:
    """假启动器：只登记（被调了几次、每次的场次目录），**什么也不起**。

    它替的是「把 headless DSH 子会话拉起来」那一步 —— 本文件里一次真拉起都不该发生，
    因此这份登记就是「这一趟有没有再花一次钱」的权威读法。
    """

    def __init__(self, *, result: Any = None) -> None:
        self.calls: list[str] = []
        self.result = result

    def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(str(session_dir))
        return self.result


class FakeHook:
    """假钩子实现（`minutes.hook = auto` 那一格解析出来的东西）：`launch()` 只登记。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch(self, session_dir: str) -> None:
        self.calls.append(str(session_dir))


class FakeResolution:
    """`resolve_minutes_hook()` 的替身：冒充「在场且有实现」那一档。"""

    def __init__(self, hook: FakeHook) -> None:
        self.key = "minutes.hook"
        self.selector = "auto"
        self.target = "fake:MinutesHook"
        self.has_implementation = True
        self.implementation = hook

    def skipped(self) -> bool:
        return False


class RecordingConverter:
    """假转码器：登记 + 按场景写/不写那份 MP3（真 ffmpeg 在不在场与用例无关）。"""

    def __init__(self, *, body: bytes = b"fake-mp3", raises: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.body = body
        self.raises = raises

    def __call__(self, pcm_path: Any, mp3_path: Any) -> bool:
        self.calls.append((str(pcm_path), str(mp3_path)))
        if self.raises:
            raise RuntimeError(self.raises)
        Path(mp3_path).write_bytes(self.body)
        return True


def write_minutes_done(session: Path, *, state: str = "done") -> Path:
    """按纪要那一层自己的写方落一份状态文件（走 `write_session_status()`）。"""
    minutes_status.write_session_status(
        session,
        state=state,
        mode="offline_only",
        branch="headless",
        started_at="2025-09-19T22:00:00",
        artifacts={},
    )
    return session / MINUTES_STATUS_NAME


def write_final(session: Path) -> Path:
    """落一份过得了四条判据的 `meeting_final.md`。"""
    path = session / MINUTES_FINAL_NAME
    path.write_text(GOOD_FINAL, encoding="utf-8")
    return path


class DelayedHookImplementation:
    """**真 `MinutesHook` 的时序形状**：`launch()` 起一条 daemon 线程就返回，终态隔一会儿才落。

    真启动器（`meeting/backends/minutes.py::MinutesHook.launch`）返回即算启动成功，
    线程体第一件事是把状态文件落成 `running`（`meeting/minutes/runner.py:190`），整条链要跑
    几十分钟。这台替身照那个形状来：线程体先落 `running`、隔 `latency` 秒再落终态，
    终态是 `done` 时**先写 `meeting_final.md` 再写状态文件**（真链也是先交出产物、由启动器
    写终态那一版）。

    `latency` 是**被模拟的那个子会话的耗时**，用它造出「拉起那一刻盘上还是 `running`」
    这个现场；用例侧的等待落在产品代码那条轮询上（`_wait_for_settled_minutes()`），
    因此这里不必用 `wait_until()` 去赌时序。
    """

    def __init__(self, *, latency: float = 0.1, state: str = "done") -> None:
        self.calls: list[str] = []
        self.latency = float(latency)
        self.state = str(state)
        self.threads: list[threading.Thread] = []

    def launch(self, session_dir: Any) -> None:
        self.calls.append(str(session_dir))
        session = Path(session_dir)

        def body() -> None:
            write_minutes_done(session, state="running")
            if self.latency:
                time.sleep(self.latency)
            if self.state == "done":
                write_final(session)
            write_minutes_done(session, state=self.state)

        thread = threading.Thread(target=body, daemon=True)
        self.threads.append(thread)
        thread.start()
