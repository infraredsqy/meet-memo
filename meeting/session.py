"""场次目录与 `session.json` 的原子写。

口径：没有输出根，一场会的
落点 = start 显式给出的工作区目录（已存在）；本模块不建目录、不解析目录名、不按日期命名。
`session.id` 就是目录名；日期是元信息（`session.json` 的 `date` 字段，从开场时刻取），
从目录名语义里退出来。`session.json` 原子写（同目录临时文件 + `os.replace` + `flush`），
每个阶段变更都写一次。顶层四态与 `session.json` 的 `state` 是两套词汇，映射只允许出现
在本模块的 `state_for()`；反方向只判集合成员关系，不把 `state` 读成 `top_state`。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from meeting.atomicio import write_text_atomic
from meeting.backends.base import SOURCE_IDS
from meeting.state import DRAIN_STEP_IDS

_log = logging.getLogger("meeting.session")

#: `session.json` 的文件名
SESSION_FILE_NAME = "session.json"

#: 段序号在归档文件名里的位数（三位，按名排序 = 按序号排序；一段 = 一次录制）
SEGMENT_NUMBER_WIDTH = 3


def archive_name(source: str, segment: int = 1) -> str:
    """一路一段的归档文件名 = `meeting_audio_<source>_<段号:03d>.pcm`。

    段的口径：一段 = 一次录制（start→finalize），序号从 1
    起；暂停、会中关某一路、某一路没开都不切段——那一路写静音而已。续接用下一个序号开新
    文件，不追加旧文件。
    """
    return f"meeting_audio_{source}_{int(segment):0{SEGMENT_NUMBER_WIDTH}d}.pcm"


def archive_segment_number(name: str, source: str) -> int | None:
    """从归档文件名主干里认出段序号；不是这一路的段文件名主干时交回 `None`。

    认名规则与 `archive_name()` 同一条（不带后缀，后缀由调用方剥好——`.pcm` 与 `.mp3`
    同一套序号）。
    """
    prefix = f"meeting_audio_{source}_"
    if not name.startswith(prefix):
        return None
    digits = name[len(prefix):]
    if len(digits) != SEGMENT_NUMBER_WIDTH or not digits.isdigit():
        return None
    return int(digits)


def segment_archive_paths(
    session_dir: Path | str, source: str, *, suffix: str = ".pcm"
) -> list[Path]:
    """这一路盘上实际存在的各段归档（缺省 PCM，可要 `.mp3`），按段序号升序。

    消费端的段列表来自这里：枚举盘上的段文件就是「这场会录了几段」的现场事实，
    序号升序拼起来 = 连续时间轴（暂停区间写的是数字零，天然连续）。
    """
    root = Path(session_dir)
    found: list[tuple[int, Path]] = []
    try:
        items = list(root.iterdir())
    except OSError:
        return []
    for item in items:
        name = item.name
        if not name.endswith(suffix):
            continue
        number = archive_segment_number(name[: -len(suffix)], source)
        if number is not None and item.is_file():
            found.append((number, item))
    found.sort(key=lambda pair: pair[0])
    return [path for _number, path in found]


def segment_numbers(session_dir: Path | str, source: str, *, suffix: str = ".pcm") -> list[int]:
    """这一路盘上实有各段的序号（升序）；目录不存在 / 一个段都没有时交回空表。

    序号只从盘上实有的段文件认出来（与 `segment_archive_paths()` 同一条认名规则），
    不读 `session.json`——元信息不当判据。
    """
    return [
        number
        for number in (
            archive_segment_number(path.name[: -len(suffix)], source)
            for path in segment_archive_paths(session_dir, source, suffix=suffix)
        )
        if number is not None
    ]


def next_segment_number(
    session_dir: Path | str, sources: Iterable[str] = SOURCE_IDS
) -> int:
    """下一个没被用过的段号 = 各路盘上实有段号的最大值 + 1；没有段时从 1 起。

    段号是一场会的（两路共一个计器，两路的第 N 段拼成同一段时间轴）。先定段号、
    再开写侧是硬次序：`open_pcm()` 是截断写，段号解析晚于打开就会把上一段的 PCM
    清成零字节（没有任何报警的那一种丢）。
    """
    highest = 0
    for source in sources:
        for number in segment_numbers(session_dir, source):
            highest = max(highest, number)
    return highest + 1


def state_for(*, top_state: str, ended_phase: str | None = None, has_failed_step: bool = False) -> str:
    """顶层状态 / 收尾阶段 → `session.json` 的 `state`（映射的唯一落点）。

    四档固定：`recording` / `alert` → `recording`（告警不是盘上的
    一档，会话还在录）；`ended.phase == "draining"` → `draining`；`done` 且无失败步 →
    `done`；有 `failed` 步 → `failed`。`idle` 与认不出的取值在这里抛 `ValueError`：
    盘上的 `state` 描述的是真开过的那一场，兜底成 `recording` 会留下一版「写着在录、
    实际没录」的记录。
    """
    if top_state == "recording":
        return "recording"
    if top_state == "alert":
        # 告警不是 `session.json` 的一档：会话仍在录，盘上写 `recording`
        return "recording"
    if top_state != "ended":
        raise ValueError(
            f"state_for() 只认 recording / alert / ended 三档出发态，收到 {top_state!r}"
            "（idle 时没有场次可写盘）"
        )
    if has_failed_step:
        return "failed"
    return "done" if ended_phase == "done" else "draining"


def build_session_record(
    *,
    session_id: str,
    session_dir: str,
    mode: str,
    state: str,
    created_at: str,
    started_at: str,
    devices: dict[str, str | None] | None = None,
    steps: list[dict[str, Any]] | None = None,
    ended_at: str | None = None,
    interrupted: bool = False,
    pid: int | None = None,
    segments: list[dict[str, Any]] | None = None,
    audio_duration_seconds: float = 0.0,
    archives: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """组装一份字段一个不省的 `session.json`（三步各一条、每条四键齐）。

    `date` 是元信息字段：从开场时刻取，只记录、
    不参与寻址与校验。段记录两件套：`segments` 每段一条（序号、起止、
    哪几路、段内暂停区间、在录时长），一段 = 一次录制、暂停不切段，未写完的段
    `ended_at` 与 `duration_seconds` 为 `None`；`audio_duration_seconds` 是权威时长 =
    实际录到的音频时长（不含暂停），写端唯一的自洽式是它 = 各段 `duration_seconds` 之和；
    除此之外元信息一律不当判据（墙钟跨度、空档时长不校验、不改结论）。

    `archives` 是每一路的段文件名表（序号升序）；不给时按「恰好一段」声明——开场第一版
    落盘时还没有段记录，段 1 的文件名由命名规则确定；续接时由 runtime 合并旧表再传。
    """
    devices = devices or {}
    if steps is None:
        steps = [
            {"id": step_id, "status": "waiting", "detail": "", "error": None}
            for step_id in DRAIN_STEP_IDS
        ]
    return {
        "session_id": session_id,
        "dir": session_dir,
        "date": str(created_at)[:10],
        "mode": mode,
        "state": state,
        "created_at": created_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "sources": [
            {
                "id": source,
                "device": devices.get(source),
                "archives": list(
                    (archives or {}).get(source, [archive_name(source, 1)])
                ),
            }
            for source in SOURCE_IDS
        ],
        "segments": [dict(segment) for segment in (segments or [])],
        "audio_duration_seconds": round(float(audio_duration_seconds), 3),
        "drain": {"steps": [dict(step) for step in steps]},
        "interrupted": bool(interrupted),
        "service": {
            "pid": os.getpid() if pid is None else int(pid),
            "started_at": started_at,
        },
    }


def steps_from_ended(ended: Any) -> list[dict[str, Any]]:
    """把快照 `ended.steps` 转成盘上的 `drain.steps`（四键齐）。"""
    steps: list[dict[str, Any]] = []
    for step in ended.steps:
        steps.append(
            {
                "id": step.id,
                "status": step.status,
                "detail": step.detail,
                "error": step.error,
            }
        )
    return steps


def normalize_dir(value: str) -> str:
    """把目录文本算成**规范的绝对路径**（目录寻址两侧共用的唯一出处）。

    `expanduser()` 展开 `~`；相对路径锚到进程当前目录；末尾 `normpath()` 收拢 `..`
    与重复分隔符。不判存在、不建目录——更严的两条校验（必给、目录已存在）只归
    `resolve_workspace_dir()`（start 路径）管；注册表侧的判别与去重
    （`workspace.canonical_dir()`）不重复它们、也不做存在性分支。
    """
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return os.path.normpath(str(path))


def resolve_workspace_dir(value: Any) -> str:
    """把 start 请求 / 命令行给的目标工作区目录算成绝对路径（不建目录）。

    校验两件事：必给、目录已存在，一条不符合就抛 `ValueError`（message 直接进动作层的
    `invalid_setting`）。目录里已有 `session.json` 不再拒绝：对着录过的
    目录按 start 就是续接，段记录与权威时长由 runtime 合并（`start_session()`）。
    """
    text = None if value is None else str(value).strip()
    if not text:
        raise ValueError(
            "缺目标工作区目录：start 必须显式给一个已存在的目录（args.dir），"
            "本服务不设默认落点、也不建目录"
        )
    path = Path(normalize_dir(text))
    if not path.is_dir():
        raise ValueError(f"目标工作区目录不存在（只接受已存在的目录，不自动建）：{path}")
    return str(path)


class SessionStore:
    """一份场次的档案（原子写 / 读 `session.json`）；落点由调用方给，本类不自知根。"""

    def session_file(self, session_dir: str) -> Path:
        return Path(session_dir) / SESSION_FILE_NAME

    def write_session_json(self, session_dir: str, record: dict[str, Any]) -> str:
        """原子写 `session.json`（同目录临时文件 + `os.replace` + fsync，走 atomicio 共用实现）。"""
        target = self.session_file(session_dir)
        write_text_atomic(
            target,
            json.dumps(record, ensure_ascii=False, indent=2),
            fsync=True,
        )
        return str(target)

    def read_session_json(self, session_dir: str) -> dict[str, Any] | None:
        """读一份场次记录；读不出来一律当「未命中」（返回 `None`，异常不冒到调用方）。"""
        try:
            raw = self.session_file(session_dir).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def mark_interrupted(self, session_dir: str, record: dict[str, Any]) -> str:
        """强制打断的先落盘入口：把 `state: "draining"` + `interrupted: true` 那一版写到盘上。

        薄封装：只覆盖三个参数（`state` / `interrupted` / `ended_at=None`），
        其余走 `write_session_json()` 同一原子写、不新开句柄。原 `steps` 原样保留、
        不伪造终态、不写 `done`、不写 `ended_at`。
        """
        data = dict(record)
        data["state"] = "draining"
        data["interrupted"] = True
        data["ended_at"] = None
        return self.write_session_json(session_dir, data)

    def remove_session_dir(self, session_dir: str) -> None:
        """`start` 写盘失败后把这一场刚写的东西清掉（主 log 的留痕由调用方写）。

        场内清：只删本场写入的 `session.json`，不动工作区目录本身——目录是用户给的，
        软件不删它。临时副本归 `write_session_json()` 的 `finally` 兜底。
        """
        session_file = self.session_file(session_dir)
        try:
            session_file.unlink()
        except OSError:
            pass


__all__ = [
    "SESSION_FILE_NAME",
    "SEGMENT_NUMBER_WIDTH",
    "SessionStore",
    "archive_name",
    "archive_segment_number",
    "build_session_record",
    "next_segment_number",
    "normalize_dir",
    "resolve_workspace_dir",
    "segment_archive_paths",
    "segment_numbers",
    "state_for",
    "steps_from_ended",
]
