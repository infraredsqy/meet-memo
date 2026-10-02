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
from typing import Any, Iterable

from meeting.backends.base import SOURCE_IDS
from meeting.state import DRAIN_STEP_IDS

_log = logging.getLogger("meeting.session")

#: `session.json` 的文件名
SESSION_FILE_NAME = "session.json"

#: 段序号在归档文件名里的位数（三位，按名排序 = 按序号排序；一段 = 一次录制）
SEGMENT_NUMBER_WIDTH = 3


def archive_name(source: str, segment: int = 1) -> str:
    """一路**一段**的归档文件名 = `meeting_audio_<source>_<段号:03d>.pcm`。

    段的口径（`docs/design-intent.md` 第 9 条）：**一段 = 一次录制（start→finalize）**，
    序号从 1 起；暂停、会中关掉某一路、某一路没开都不切段——那一路写静音而已。
    段只表达「这场会录了几次」：续接（同一工作区再录一段）用**下一个序号**开新文件，
    不追加旧文件——「续接=追加到旧文件末尾」是 V1 的旧口径，已被第 9 条翻案。
    """
    return f"meeting_audio_{source}_{int(segment):0{SEGMENT_NUMBER_WIDTH}d}.pcm"


def archive_segment_number(name: str, source: str) -> int | None:
    """从归档文件名主干里认出段序号；不是这一路的段文件名主干时交回 `None`。

    认名规则与 `archive_name()` 同一条：`meeting_audio_<source>_<三位序号>`（不带后缀，
    后缀由调用方剥好——`.pcm` 与 `.mp3` 同一套序号）。
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
    """这一路**盘上实际存在**的各段归档（缺省 PCM，可要 `.mp3`），按段序号升序。

    消费端（会中转写的尾读、会后链的取音频）的**段列表来自这里**：按确定的命名规则
    枚举盘上的段文件就是「这场会录了几段」的现场事实，序号升序拼起来 = 连续时间轴
    （暂停区间写的是数字零，天然连续）。目录不存在 / 一个段都没有时交回空表。
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

    `next_segment_number()` 的底账：序号只从**盘上实有的段文件**认出来
    （`segment_archive_paths()` 同一条认名规则），不读 `session.json`——
    元信息不当判据，盘上的段文件才是「这场会录了几段」的现场事实。
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
    """下一个**没被用过**的段号 = 各路盘上实有段号的最大值 + 1；没有段时从 1 起。

    段号是**一场会**的（两路共一个计器，两路的第 N 段拼成同一段时间轴），
    因此取各路的最大值再 +1：开场（空目录）得 1；续接（同一工作区再录一段）得
    已有段号 + 1——**先定段号、再开写侧**是硬次序：`open_pcm()` 是截断写，
    段号解析晚于打开就会把上一段的 PCM 清成零字节（没有任何报警的那一种丢）。
    """
    highest = 0
    for source in sources:
        for number in segment_numbers(session_dir, source):
            highest = max(highest, number)
    return highest + 1


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
    segments: list[dict[str, Any]] | None = None,
    audio_duration_seconds: float = 0.0,
    archives: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """组装一份字段一个不省的 `session.json`（四步各一条、每条四键齐）。

    `date` 是**元信息**字段（`docs/design-intent.md` 第 3、6 条）：日期仍是每一场的信息，
    从开场的时刻取，只记录、不参与任何寻址与不参与任何校验。

    段账（本节点的两件套，第 6、7、9 条）：

      - `segments` 是**段列表**：每段一条（序号、起止、哪几路、段内暂停区间、在录时长）。
        一段 = 一次录制；暂停不切段，段内的暂停靠 `pauses` 区间说话。**未写完的段**
        （还在录）`ended_at` 与 `duration_seconds` 为 `None`；
      - `audio_duration_seconds` 是**权威时长** = 实际录到的音频时长（不含暂停），
        写端唯一的自洽式：它 = 各段 `duration_seconds` 之和（未写完的段不计入）。
        除此之外元信息一律不当判据：墙钟跨度、空档时长、段与墙钟的关系，**不校验、
        不对账、不改结论**（第 6 条）。

    `archives` 是每一路的段文件名表（序号升序）；不给时按「恰好一段」声明——开场
    第一版落盘时引擎还没起、此刻还没有段账，段 1 的文件名由命名规则确定。续接
    （同一工作区再录一段）时由 runtime 把旧表与新段名合并进来再传。
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


def resolve_workspace_dir(value: Any) -> str:
    """把 start 请求 / 命令行给的目标工作区目录算成**绝对路径**（不建目录）。

    校验两件事：必给、目录已存在。一条不符合就抛 `ValueError`
    （message 直接进动作层的 `invalid_setting`）。

    目录里**已有** `session.json` 不再拒绝（`docs/design-intent.md` 第 9 条翻案）：
    一个工作区 = 一场会（第 3 条），对着录过的目录按 start 就是**续接**——
    同一场会再录一段，段账与权威时长由 runtime 合并（`start_session()`）。
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
    "SEGMENT_NUMBER_WIDTH",
    "SessionRecord",
    "SessionStore",
    "archive_name",
    "archive_segment_number",
    "build_session_record",
    "next_segment_number",
    "resolve_workspace_dir",
    "segment_archive_paths",
    "segment_numbers",
    "state_for",
    "steps_from_ended",
]
