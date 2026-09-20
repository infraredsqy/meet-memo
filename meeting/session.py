"""场次目录、`session.json` 的原子写与「扫未收尾场次」（phase2 §3.2）。

口径（phase2 冻结，phase3 只往盘上写值、不改字段名与形状）：

  - 输出根 = 配置键 `output.root`，**根不存在时在 `start` 那一刻建**（递归 `mkdir`），
    服务启动与扫描都不建目录；
  - 场次目录名 = `YYMMDD`，同名已存在就 `YYMMDD-2`、`YYMMDD-3`……**让位不复用**
    （`mkdir` 不带 `exist_ok`，靠文件系统的原子性裁决并发 `start`）；
  - `session.id` **就是目录名**，`dir` 是场次目录路径；
  - `session.json` **原子写**：同目录临时文件 + `os.replace` + `flush`；
    每个阶段变更都写一次；
  - 启动扫描**只取最新的一场**，判据只有一条：`state ∈ {recording, draining}` 或
    `interrupted: true`；命中即挂 `notice.restart_unfinished`，并把那份记录**改写**成
    `state: "draining"` + `interrupted: true`（原 `steps` 原样保留、不伪造终态），
    **绝不自动续录、绝不把状态恢复成「录音中」**。

顶层四态与 `session.json` 的 `state` 是两套词汇，**映射只允许出现在本模块**
（`state_for()`），不出现在 `state.py` / `routes.py` / `hub.py` / `/debug`。
反方向（读回）**只判集合成员关系**，不做反查映射、不把 `state` 读成 `top_state`。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.backends.base import SOURCE_IDS
from meeting.state import DRAIN_STEP_IDS, now_iso

_log = logging.getLogger("meeting.session")

#: `session.json` 的文件名
SESSION_FILE_NAME = "session.json"

#: 场次目录名的日期形状（`YYMMDD`）
SESSION_DIR_DATE_FORMAT = "%y%m%d"

#: 「未收尾」的判据：`state` 落在这两档里，或 `interrupted` 为真
UNFINISHED_STATES: tuple[str, ...] = ("recording", "draining")


def archive_name(source: str) -> str:
    """两路归档文件名 = `meeting_audio_<source>.pcm`（音频字节的写入归 `dca4539b`）。"""
    return f"meeting_audio_{source}.pcm"


#: 重启未收尾场次的提示文案（照设计稿 4.5 的原文形状）
RESTART_UNFINISHED_TEXT = "服务重启过。上一场的收尾没走完，音频在 {session_dir}，日志在 {log_path}。"


def restart_unfinished_text(session_dir: str, log_path: str) -> str:
    return RESTART_UNFINISHED_TEXT.format(session_dir=session_dir, log_path=log_path)


def state_for(*, top_state: str, ended_phase: str | None = None, has_failed_step: bool = False) -> str:
    """顶层状态 / 收尾阶段 → `session.json` 的 `state`（**映射的唯一落点**）。

    规则（phase2 §7.1 高危点 1，四档固定）：
      进 `recording` → `recording`；`alert` 同样落 `recording`（告警不是盘上的一档，
      会话还在录）；`ended.phase == "draining"` → `draining`；`ended.phase == "done"`
      且无失败步 → `done`；存在 `failed` 步 → `failed`。

    `idle` 与其余认不出的取值**在这里抛 `ValueError`**：盘上的 `state` 描述的是**真的开过的
    那一场会**，`idle` 时手上没有场次可写；把它兜底成 `recording` 会在盘上留下一版「写着在录、
    实际没录」的记录（R2 最不能犯的错）。
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


@dataclass
class SessionRecord:
    """一份 `session.json` 的盘上内容（dict 形状，字段名与形状都是冻结项）。"""

    data: dict[str, Any]

    @property
    def state(self) -> str | None:
        return self.data.get("state")

    @property
    def interrupted(self) -> Any:
        return self.data.get("interrupted")

    @property
    def steps(self) -> list[dict[str, Any]]:
        drain = self.data.get("drain")
        if not isinstance(drain, dict):
            return []
        steps = drain.get("steps")
        return steps if isinstance(steps, list) else []

    def unfinished(self) -> bool:
        """判据只有这一条：`state ∈ {recording, draining}` 或 `interrupted: true`。"""
        return self.state in UNFINISHED_STATES or self.interrupted is True


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
) -> dict[str, Any]:
    """组装一份字段一个不省的 `session.json`（四步各一条、每条四键齐）。"""
    devices = devices or {}
    if steps is None:
        steps = [
            {"id": step_id, "status": "waiting", "detail": "", "error": None}
            for step_id in DRAIN_STEP_IDS
        ]
    return {
        "session_id": session_id,
        "dir": session_dir,
        "mode": mode,
        "state": state,
        "created_at": created_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "sources": [
            {
                "id": source,
                "device": devices.get(source),
                "archive": archive_name(source),
            }
            for source in SOURCE_IDS
        ],
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


class SessionStore:
    """输出根下的场次档案（分配目录名、原子写 / 读 `session.json`、扫未收尾）。"""

    def __init__(self, out_root: str) -> None:
        self.out_root = str(out_root)

    # -- 目录分配 ---------------------------------------------------------

    def preview_next_dir(self, *, today: datetime | None = None) -> str:
        """算下一场的目录路径（**只算不建**：调用后目录必须不存在）。

        与随后真正 `start` 时算出的名字规则一致（同名已存在就让位到 `-2`、`-3`…）。
        """
        base = (today or datetime.now()).strftime(SESSION_DIR_DATE_FORMAT)
        index = 1
        while True:
            name = base if index == 1 else f"{base}-{index}"
            candidate = Path(self.out_root) / name
            if not candidate.exists():
                return str(candidate)
            index += 1

    def create_session_dir(self, *, today: datetime | None = None) -> tuple[str, str]:
        """建场次目录并返回 `(目录名, 目录路径)`；已存在就让位（不复用）。

        **`mkdir` 不带 `exist_ok`**：靠文件系统的原子性裁决并发 `start`（算不出同一个名字）。
        """
        base = (today or datetime.now()).strftime(SESSION_DIR_DATE_FORMAT)
        Path(self.out_root).mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            name = base if index == 1 else f"{base}-{index}"
            candidate = Path(self.out_root) / name
            try:
                candidate.mkdir()
            except FileExistsError:
                index += 1
                continue
            return name, str(candidate)

    # -- 读写 -------------------------------------------------------------

    def session_file(self, session_dir: str) -> Path:
        return Path(session_dir) / SESSION_FILE_NAME

    def write_session_json(self, session_dir: str, record: dict[str, Any]) -> str:
        """原子写 `session.json`（同目录临时文件 + `os.replace` + `flush`）。"""
        target = self.session_file(session_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{SESSION_FILE_NAME}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - 清理失败不影响结果
                    pass
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

    # -- 扫未收尾 ---------------------------------------------------------

    def load(self, session_dir: str) -> tuple[SessionRecord | None, str | None]:
        """读一份场次记录并回答两个问题：记录本身 + 「读不出来」的说明。

        条数口径（phase1 的观测面，phase2 照旧）：
          - `session.json` **根本不存在** → 与「输出根下没有场次目录」同口径，
            **算未命中且不落任何 log**（说明为 `None`）；
          - 文件存在却读不出来 / 解码失败 / 顶层不是对象 → 同样按未命中处理，
            但**必须留下一行「读不出来…按未命中处理」的 WARNING**（说明非 `None`）。
        """
        target = self.session_file(session_dir)
        if not target.exists():
            return None, None
        try:
            raw = target.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if not isinstance(data, dict):
            return None, "session.json 顶层不是 JSON 对象"
        return SessionRecord(data), None

    def latest_session_dir(self) -> str | None:
        """输出根下的最新一场（按目录名的字典序取最大者；`YYMMDD` 命名下与时间序一致）。"""
        root = Path(self.out_root)
        if not root.is_dir():
            return None
        try:
            entries = [item for item in root.iterdir() if item.is_dir()]
        except OSError:  # pragma: no cover - 输出根不可读时视为没有场次
            return None
        if not entries:
            return None
        return str(max(entries, key=lambda item: item.name))

    def scan_unfinished(self) -> tuple[str | None, str | None]:
        """扫未收尾场次，返回 `(命中的场次目录, 那行 WARNING 的内容)`。

        不建目录、不自动录音、不自动续录；命中的改写由 `mark_unfinished()` 做。
        """
        latest = self.latest_session_dir()
        if latest is None:
            return None, None
        record, error = self.load(latest)
        if error is not None:
            _log.warning(
                "启动扫描：最新一场 %s 的 session.json 读不出来（%s），按未命中处理",
                latest,
                error,
            )
            return None, None
        if record is None or not record.unfinished():
            return None, None
        return latest, (
            f"启动扫描：发现未收尾的场次 {latest}"
            f"（扫到的 state={record.state!r}，interrupted={record.interrupted!r}）"
            "；本 Phase 不自动录音、不建目录"
        )

    def mark_unfinished(self, session_dir: str, record: SessionRecord) -> dict[str, Any]:
        """把命中的那份记录改写成「收尾中 + 已打断」，**原 `steps` 原样保留**。

        原子写（手法同 `write_session_json()`）；**只写这一处**：场次目录里其余文件逐字不变、
        不新建任何目录、不动别的场次。
        """
        data = dict(record.data)
        data["state"] = "draining"
        data["interrupted"] = True
        self.write_session_json(session_dir, data)
        return data

    def remove_session_dir(self, session_dir: str) -> None:
        """`start` 写盘失败后把刚建的场次目录清掉（主 log 的留痕由调用方写）。"""
        shutil.rmtree(session_dir, ignore_errors=True)


def output_root(config: Any = None) -> str:
    """解析输出根（`output.root`）；相对路径按**当前工作目录**解析（与 `web.dist_dir` 同口径）。"""
    if config is None:
        config = config_module.current()
    value = str(config.get("output.root")) if config is not None else str(
        config_module.default_for("output.root")
    )
    return value if os.path.isabs(value) else os.path.abspath(value)


__all__ = [
    "RESTART_UNFINISHED_TEXT",
    "SESSION_FILE_NAME",
    "SESSION_DIR_DATE_FORMAT",
    "UNFINISHED_STATES",
    "SessionRecord",
    "SessionStore",
    "archive_name",
    "build_session_record",
    "output_root",
    "restart_unfinished_text",
    "state_for",
    "steps_from_ended",
]
