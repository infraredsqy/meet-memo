"""场次目录与 `session.json` 的原子写。

口径（「没有默认落点」定稿，`docs/design-intent.md` 第 3、4 条）：

  - **没有输出根**：一场会的落点 = 它的**工作区目录**（一个已存在的目录），由 start 的
    请求 / 命令行**显式**给出；本模块**不建目录、不解析目录名、不按日期命名**；
  - `session.id` **就是目录名**，`dir` 是场次目录的绝对路径；
  - 日期是每一场的**元信息**（`session.json` 的 `date` 字段，从开场的时刻取），
    从目录名的语义里退出来（第 3 条：目录名只读）；
  - `session.json` **原子写**：同目录临时文件 + `os.replace` + `flush`；
    每个阶段变更都写一次。

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
from pathlib import Path
from typing import Any

from meeting.backends.base import SOURCE_IDS
from meeting.state import DRAIN_STEP_IDS

_log = logging.getLogger("meeting.session")

#: `session.json` 的文件名
SESSION_FILE_NAME = "session.json"


def archive_name(source: str) -> str:
    """两路归档文件名 = `meeting_audio_<source>.pcm`（音频字节的写入归 `dca4539b`）。"""
    return f"meeting_audio_{source}.pcm"


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
    """组装一份字段一个不省的 `session.json`（四步各一条、每条四键齐）。

    `date` 是**元信息**字段（`docs/design-intent.md` 第 3、6 条）：日期仍是每一场的信息，
    从开场的时刻取，只记录、不参与任何寻址与不参与任何校验。
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


def resolve_workspace_dir(value: Any) -> str:
    """把 start 请求 / 命令行给的目标工作区目录算成**绝对路径**（不建目录）。

    校验三件事：必给、目录已存在、里面没有另一场的 `session.json`。
    一条不符合就抛 `ValueError`（message 直接进动作层的 `invalid_setting`）。
    """
    text = None if value is None else str(value).strip()
    if not text:
        raise ValueError(
            "缺目标工作区目录：start 必须显式给一个已存在的目录（args.dir），"
            "本服务不设默认落点、也不建目录"
        )
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.normpath(str(path)))
    if not path.is_dir():
        raise ValueError(f"目标工作区目录不存在（只接受已存在的目录，不自动建）：{path}")
    if (path / SESSION_FILE_NAME).exists():
        raise ValueError(f"目标工作区目录里已经有一场会的 {SESSION_FILE_NAME}：{path}")
    return str(path)


class SessionStore:
    """一份场次的档案（原子写 / 读 `session.json`）；**落点由调用方给，本类不自知根**。"""

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

    # -- 读 ---------------------------------------------------------------

    def load(self, session_dir: str) -> tuple[SessionRecord | None, str | None]:
        """读一份场次记录并回答两个问题：记录本身 + 「读不出来」的说明。

        条数口径（phase1 的观测面，phase2 照旧）：
          - `session.json` **根本不存在** → **算未命中且不落任何 log**（说明为 `None`）；
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

    def mark_interrupted(self, session_dir: str, record: dict[str, Any]) -> str:
        """强制打断的**先落盘**入口：把 `state: "draining"` + `interrupted: true` 那一版写到盘上。

        **薄封装**（phase3 §2.4 / §4 的落地形态）：内部只做「组装 `state: "draining"` +
        `interrupted: true` 的参数」再调用 `write_session_json()` —— 与 `mark_unfinished()`
        同一入口、同一原子写、**不新开文件句柄**。`record` 是**现场记录**（runtime 取出现场
        交过来的那一份），这里只覆盖上面那三个参数：

          - `state = "draining"`、`interrupted = True`、`ended_at = None`；
          - **原 `steps` 原样保留**（`record["drain"]["steps"]` 不动）、**不伪造终态**、
            **不写 `done`**、**不写 `ended_at`**。

        返回写下去的路径（`write_session_json()` 的返回值）。
        """
        data = dict(record)
        data["state"] = "draining"
        data["interrupted"] = True
        data["ended_at"] = None
        return self.write_session_json(session_dir, data)

    def remove_session_dir(self, session_dir: str) -> None:
        """`start` 写盘失败后把这一场刚写的东西清掉（主 log 的留痕由调用方写）。

        **场内**清：只删本场写入的 `session.json` 与它的临时副本，不动工作区目录本身；
        目录是用户给的，软件不删它。
        """
        session_file = self.session_file(session_dir)
        try:
            session_file.unlink()
        except OSError:
            pass


__all__ = [
    "SESSION_FILE_NAME",
    "SessionRecord",
    "SessionStore",
    "archive_name",
    "build_session_record",
    "resolve_workspace_dir",
    "state_for",
    "steps_from_ended",
]
