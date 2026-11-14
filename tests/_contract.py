"""`contract/fixtures/` 的采集、掩码与落盘比对（pytest 不收集本文件）。

**确定性是掩码的全部目的**：fixtures 由真实服务路径采集，但采集里的墙钟、路径、端口、
电平值每次都不同；掩码之后，同一份代码连跑两次 `--regen-contract` 落盘的字节必须逐字相同
（`revision` 保留真实值 —— 它由固定的迁移与提交次数决定，同样确定）。

**占位符要形状诚实**：这一格掩成 `<session-id>`，占位符自己就说明「这里放的是目录名」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests._helpers import REPO_ROOT

#: fixtures 落点（仓库根下新建的 `contract/fixtures/`，git 跟踪）
FIXTURES_ROOT = REPO_ROOT / "contract" / "fixtures"

#: ISO 时间戳（started_at / since / lost_since / last_line_at / server_time …）
ISO_PLACEHOLDER = "<iso>"
#: 路径 / 场次目录路径（`log_path` / `dir` / `session_dir`）
PATH_PLACEHOLDER = "<path>"
#: 场次目录**名**（`session.id` 与失败 `detail.session_id`）：人给的工作区目录名，形状任意
SESSION_ID_PLACEHOLDER = "<session-id>"
PORT_PLACEHOLDER = "<port>"
URL_PLACEHOLDER = "<url>"
LEVEL_PLACEHOLDER = "<dbfs>"

#: ISO 时间戳的整串形状（`now_iso()` 的秒精度 + 时区；容忍带小数与 `Z` 的写法）
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
#: 自由文本里内嵌的盘符绝对路径（Windows 形态；lookbehind 挡掉 `http:` 这类 scheme）
#:
#: **平台边界是有意的**：本项目的产物路径一律是 Windows 形态（`meeting/service/*` 给出的
#: `log_path` / `dir` 都从 `Path` 来，本仓只在 Windows 上跑），因此只认
#: `<盘符>:/` 与 `<盘符>:\` 两种开端。POSIX 形态（`/home/...`）与 UNC（`\\server\...`）
#: **不在判据里** —— 收进来只会让「内嵌路径」这条规则在非 Windows 上悄悄改口径，而 fixtures
#: 是随仓库进版本控制的确定性产物，判据一旦随平台变，同一份代码在不同平台落盘的字节就不同。
_INLINE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s，。；！？：、「」『』【】《》\"']+"
)

#: 这些键的秒数一律落 0（走秒的量，fixtures 里不留具体值）
ZERO_KEYS = frozenset(
    {"elapsed_seconds", "duration_seconds", "seconds_since_last_tick", "wall_seconds",
     "total_seconds", "seconds"}
)
#: 这些键是电平（dBFS），值换占位
LEVEL_KEYS = frozenset({"level_dbfs", "peak_dbfs", "dbfs", "peak", "bar"})
#: 这些键是路径 / 场次目录路径（`session_id` **不在其中**：它是目录名，见 `SESSION_ID_KEYS`）
PATH_KEYS = frozenset({"log_path", "dir", "session_dir", "materials_dir"})
#: 这些键是**场次目录名**（裸 `YYMMDD` / `YYMMDD-N`）：`session.id` 的键名是 `id`、靠键路径判，
#: 而动作失败 `detail.session_id` 直接以键名出现，两者掩成同一个形状诚实的占位符 `<session-id>`。
SESSION_ID_KEYS = frozenset({"session_id"})
#: 包络数组（只保结构与长度）
ENVELOPE_KEY = "envelope"


def normalize(value: Any, *, key: str = "", path: tuple[str, ...] = ()) -> Any:
    """按掩码规则递归重写一份 JSON 形态的值。

    `key` 是当前这一格的键名、`path` 是从根到这一格的键路径 —— `session.id` 之所以能
    与 `sources[*].id` 区分开，靠的就是路径（前者掩码、后者是取值域字面量，必须保留）。
    """
    if isinstance(value, dict):
        return {
            name: normalize(item, key=name, path=(*path, name))
            for name, item in value.items()
        }
    if isinstance(value, list):
        if key == ENVELOPE_KEY:
            # 只保结构（`bar == envelope[-1]` 的结构断言仍成立）
            return [LEVEL_PLACEHOLDER] * len(value)
        return [normalize(item, key=key, path=path) for item in value]
    if isinstance(value, str):
        if key in PATH_KEYS:
            return PATH_PLACEHOLDER
        if key in SESSION_ID_KEYS:
            return SESSION_ID_PLACEHOLDER
        if key == "id" and len(path) >= 2 and path[-2] == "session":
            return SESSION_ID_PLACEHOLDER
        if key == "url":
            return URL_PLACEHOLDER
        if _ISO_RE.match(value):
            return ISO_PLACEHOLDER
        return _INLINE_PATH_RE.sub(PATH_PLACEHOLDER, value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if key == "port":
            return PORT_PLACEHOLDER
        if key == "url":
            return URL_PLACEHOLDER
        if key in ZERO_KEYS:
            return 0
        if key in LEVEL_KEYS:
            return LEVEL_PLACEHOLDER
        return value
    return value


def normalize_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """把 `parse_sse_frame()` 的产物收成 fixtures 形态（`data` 掩码）。"""
    return {
        "event": frame.get("event"),
        "id": frame.get("id"),
        "data": normalize(frame.get("data")),
    }


def fixture_path(rel: str) -> Path:
    """一个 fixtures 文件的落点（`rel` 相对 `contract/fixtures/`，一律正斜杠写法）。"""
    return FIXTURES_ROOT.joinpath(*rel.split("/"))


def _dump_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def emit(records: dict[str, Any], *, regen: bool) -> None:
    if regen:
        for rel in sorted(records):
            target = fixture_path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_dump_text(records[rel]), encoding="utf-8")
        return

    problems: list[str] = []
    for rel in sorted(records):
        target = fixture_path(rel)
        if not target.exists():
            problems.append(
                f"- {rel}: fixtures 文件不存在（先跑 "
                f"`python -m pytest tests/test_contract_fixtures --regen-contract` 生成）"
            )
            continue
        try:
            on_disk = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"- {rel}: fixtures 文件读不出来（{type(exc).__name__}: {exc}）")
            continue
        if on_disk != records[rel]:
            problems.append(
                f"- {rel}: 与实时采集不一致（服务行为或掩码规则变了）\n"
                f"    fixtures: {_dump_text(on_disk)!r}\n"
                f"    实时采集: {_dump_text(records[rel])!r}"
            )
    assert not problems, (
        "契约 fixtures 与服务实际行为漂移（改了服务行为就重跑 --regen-contract 并复核差异）：\n"
        + "\n".join(problems)
    )


#: 假总结客户端答的那份固定输出（三条固定标签齐备；与窗口文本无关，因此样本逐字确定）
SUMMARY_STUB_CONTENT = (
    '{"bullets": ["【讲什么】在过下周的排期", "【讲到哪】说到第三项",'
    ' "【丢掉什么】有一处识别失败的缺口"]}'
)


class SummaryStub:
    """**假总结客户端**（会中总结的注入点 `runtime.summary_client` 收到的那一个）。

    真端点那一档由一次性探针跑，契约样本记的是**形状**，模型的话归那一档。
    """

    def __init__(self, content: str = SUMMARY_STUB_CONTENT) -> None:
        self.content = content
        self.calls = 0

    def complete(self, messages, *, timeout_seconds: float) -> str:
        del messages, timeout_seconds
        self.calls += 1
        return self.content


#: 写出去的那份模块名（`transcriber.backend` 取 `<名>:PeriodicTranscriber`）
TRANSCRIBER_STUB_MODULE_NAME = "meeting_contract_transcriber"
STUB_EMIT_INTERVAL_SECONDS = 0.3

_STUB_MODULE_SOURCE = '''\
"""contract fixtures 采集用的假转写后端（由 tests/_contract.py 写出，勿手改）。

经 `PYTHONPATH` + 配置键 `transcriber.backend`（`module:Class` 语法）挂进真服务子进程：
`start()` 起一条线程，每隔 {interval} 秒经 line_sink 产一行内容固定的转写
（`ts` 也取固定值：ts 的语义归后端，fixtures 留的是帧形状；固定值让两次采集逐字一致）。
只 import 标准库与 `meeting.backends.base`（子进程里都可用）。
"""

from __future__ import annotations

import threading

from meeting.backends.base import TranscriptLine

_TEXT = "假转写后端的样本行"
_INTERVAL = {interval}


class PeriodicTranscriber:
    """定时产一行转写的假后端（协议成员与 `meeting.backends.base.TranscriberBackend` 同形）。"""

    def __init__(self) -> None:
        self._sink = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def bind(self, line_sink, *, config=None) -> None:
        self._sink = line_sink

    def start(self, mode: str, session_dir: str) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="contract-stub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> str:
        return "normal"

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0

    def _run(self) -> None:
        while not self._stop.is_set():
            sink = self._sink
            if sink is not None:
                sink(TranscriptLine(ts=1.5, source="mic", text=_TEXT, kind="line"))
            self._stop.wait(_INTERVAL)
'''


def write_transcriber_stub(directory: Path) -> str:
    """把假转写后端写进 `directory`。

    调用方负责把 `directory` 拼进子进程的 `PYTHONPATH`（仓库根 + 该目录）。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = _STUB_MODULE_SOURCE.format(interval=STUB_EMIT_INTERVAL_SECONDS)
    (directory / f"{TRANSCRIBER_STUB_MODULE_NAME}.py").write_text(source, encoding="utf-8")
    return f"{TRANSCRIBER_STUB_MODULE_NAME}:PeriodicTranscriber"


__all__ = [
    "FIXTURES_ROOT",
    "ISO_PLACEHOLDER",
    "LEVEL_PLACEHOLDER",
    "PATH_PLACEHOLDER",
    "PORT_PLACEHOLDER",
    "SESSION_ID_PLACEHOLDER",
    "SUMMARY_STUB_CONTENT",
    "SummaryStub",
    "URL_PLACEHOLDER",
    "emit",
    "fixture_path",
    "normalize",
    "normalize_frame",
    "write_transcriber_stub",
]
