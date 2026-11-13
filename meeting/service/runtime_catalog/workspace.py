"""RuntimeCatalogMixin 的「工作区注册表」域分片：登记 / 移除 / 清单读数与三档状态口径。

方法与方法间注释均自拆分前的 runtime_catalog.py 单文件 mixin 一字不改复制；
本文件自写内容只有模块 docstring、import、_log 一行与类声明行。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from meeting.service.runtime_shared import ActionError, _disk_str_or_none
from meeting.state import now_iso
from meeting.workspace import canonical_dir, classify_workspace_dir, has_offline_transcript

_log = logging.getLogger("meeting.service.runtime")


class RuntimeCatalogWorkspaceMixin:
    """「工作区注册表」：开 / 移 / 清单读数，三档 state 口径的唯一住家。"""

    # 工作区注册表（工作台收编节点）

    def _is_actively_recording(self, path: str | Path) -> bool:
        """`path` 这一场是不是**本进程此刻在录**的那一场（三档口径里 `recording` 的判据）。

        **判据只此一处**（`GET /api/sessions` 的行读数与会后动作的参数校验同源）：
        `top_state ∈ recording / alert` 且目录就是快照里那一场。盘上 `session.json` 停在
        `state="recording"` 的残档（被强杀在录音半路、本进程没在录它）**不是**录音中 ——
        那一场的读数是 `ended`（`_disk_record_cells()`）、会后动作照常受理。
        """
        session = self.snapshot.session
        return (
            self.snapshot.top_state in ("recording", "alert")
            and session is not None
            and os.path.normcase(canonical_dir(str(session.dir)))
            == os.path.normcase(canonical_dir(str(path)))
        )

    def _disk_record_cells(
        self, record: dict[str, Any] | None, path: str | Path
    ) -> tuple[str | None, str | None, str | None]:
        """工作区清单那三格的读数：`(state, date, mode)`。

        **`GET /api/sessions` 与 `GET /api/sessions/{id}` 前七格共用这一处**（契约 4.9
        写着前七格与 4.8 同一行同源、逐格语义见 4.8）：同一个事实在两处必须给同一个值。

        `state` 是**三档口径**（只表达「这场录音走到哪了」，落盘 / 转码 / 会后那几步
        不进这个口径）：

        - `"recording"` —— **仅当**这一行是本进程此刻在录的活跃场次（`_is_actively_recording()`）；
        - `"ended"` —— 盘上有 `session.json`（录音已经发生过；盘上 `state` 停在哪一档
          —— 收尾半路 / 收完 / 失败 / 录音半路的残档 —— 一律不进这个口径，那几步的进度另有读数）；
        - `null` —— 没有 `session.json`（空白工作区 / 尚未开过会）。

        `date` / `mode` 仍是盘上原文的字符串判读（盘上落成数字或对象时如实 `null`）。
        `dir_exists` 与 `kind` 由调用方照旧现场判，不折进这三档。
        """
        if self._is_actively_recording(path):
            return "recording", _disk_str_or_none(record, "date"), _disk_str_or_none(record, "mode")
        if record is None:
            return None, None, None
        return "ended", _disk_str_or_none(record, "date"), _disk_str_or_none(record, "mode")

    def _disk_segment_cells(self, record: dict[str, Any] | None) -> list[dict[str, Any]]:
        """盘上 `session.json` 的**段读数**（界面上「两段的时长与时间」的来源）。

        每段四格：`index` / `started_at` / `ended_at` / `duration_seconds`——
        段记录里给人看的就这四样（`pauses` / `sources` 是核对用的格子，不进界面）。
        盘上形状不对的那一条如实跳过；这一格只是把盘上的读数端出来给人看（第 6 条）。
        状态文件不在场时交回空表。
        """
        if not isinstance(record, dict):
            return []
        rows: list[dict[str, Any]] = []
        raw = record.get("segments")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            duration = item.get("duration_seconds")
            rows.append(
                {
                    "index": item.get("index") if isinstance(item.get("index"), int) else None,
                    "started_at": item.get("started_at")
                    if isinstance(item.get("started_at"), str)
                    else None,
                    "ended_at": item.get("ended_at")
                    if isinstance(item.get("ended_at"), str)
                    else None,
                    "duration_seconds": float(duration)
                    if isinstance(duration, (int, float))
                    else None,
                }
            )
        return rows

    def workspace_open(self, dir: Any, confirm: bool = False) -> dict[str, Any]:
        """`workspace_open`：打开/收编一个**已存在**的目录，登记进注册表。

        判别三档（`meeting/workspace.py` 的 `classify_workspace_dir()`）：

        - `empty` → 当一场新会，直接登记；
        - `meeting`（含**疑似会议目录**：有音频归档名但没有 `session.json`）→ 收编，
          直接登记；
        - `unknown`（非空、没有会议材料）→ 用户确认之后再登记：`confirm=False` 回
          `invalid_setting`(400)，**目录与盘面一个字节没动**；`confirm=True` 才落笔。
        - `missing` → `invalid_setting`(400)（已存在是硬前提，本服务不建目录）。

        已经登记过的路径只更新 `last_opened_at`（去重判据就是规范化路径）。
        成功返回进应答体的 `workspace` 键：`{dir, kind, suspected, first_time}`。
        """
        info = classify_workspace_dir(dir)
        kind = str(info["kind"])
        path = str(info["dir"])
        if kind == "missing":
            raise ActionError(
                "invalid_setting",
                f"目标工作区目录不存在（只接受已存在的目录，不自动建）：{path}",
                status=400,
                detail={"key": "dir", "value": None if dir is None else str(dir),
                        "legal": ["已存在的目录路径（空目录当新会；有会议材料的收编）"]},
            )
        if kind == "unknown" and not confirm:
            raise ActionError(
                "invalid_setting",
                "选中的目录不是空的、也没有会议材料（session.json 或 meeting_audio_*.pcm）："
                "确认把它当工作区收编之后才登记",
                status=400,
                detail={"key": "confirm", "value": None,
                        "legal": ["确认收编时带 confirm: true 重新发一次本动作"]},
            )
        stamp = now_iso()
        try:
            entry = self.registry.open(path, now=stamp)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"工作区注册表写不下去，这次打开没有生效：{type(exc).__name__}: {exc}",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return {
            "dir": entry["dir"],
            "kind": kind,
            "suspected": bool(info["suspected"]),
            "first_time": bool(entry["first_time"]),
        }

    def workspace_remove(self, dir: Any) -> dict[str, Any]:
        """`workspace_remove`：**只把这一条从注册表移走**；文件一个字节不动（第 5 条）。

        目录被事先移走/删掉的那一条也照样移（`removed: False` 只是"名单里本来没有"），
        重复删除安全。
        """
        return self.registry.remove(dir)

    def sessions_dict(self) -> dict[str, Any]:
        """`GET /api/sessions` 的应答体：注册表 + 每一行的现场读数 + 会后那一趟的全局读数。

        条目只记两个时刻（`added_at` / `last_opened_at`），**"目录还在不在、里面是什么"
        由本函数现场判**（注册表不反映目录内容）：`dir_exists` 由 `Path.is_dir()` 给、
        `kind` 由 `classify_workspace_dir()` 给；`state` 是三档口径
        （`"recording"` 仅当这一行就是本进程此刻在录的活跃场次；盘上有 `session.json`
        一律 `"ended"`；没有则 `null` —— 见 `_disk_record_cells()`），
        `date` / `mode` 只在盘上读得出来时带值。
        `recent` 是最近一次被打开/切换的那一条的目录（**只从注册表读**，不扫任何场次
        目录 —— 启动序列对它的口径见 `docs/state.md`「重启自洽」）。
        清单本身也按「最近在用」倒序给出（`registry.ordered()`）：最近一次被打开/切换
        的那一场排最前，判据与 `recent` 同一条链——界面照应答次序渲染、
        自己不重排，次序的口径正本在 `docs/contract.md` 4.8。

        `has_offline` 是「这一场有没有会后精细稿」那一格（R29）：判据是场次目录里在不在
        `meeting_offline_transcript_<source>.md`（`workspace.has_offline_transcript()`，
        只看名目、不读内容）。「出纪要」那个按钮按它渲染 —— 界面因此一眼看得出 AI 手上
        有没有那份更细的稿。**这一格走本端点，不塞进快照**（推送出去的东西只认那九个顶层键，
        前端两道硬门钉着）。

        顶层第三格 `post_jobs`（本节点重排）：`post_jobs_state()` 的原文
        —— 此刻服务进程里在飞的那一趟会后（`{kind, dir, started_at}`）或 `null`。它是
        界面那格全局「显卡忙不忙」的会后一半读数（另一半「本机正在录」从快照状态读，
        不走这里）；**每行十二键**，这一格在应答体顶层、不进任何一行，快照九键
        一个字不动。行里的 `segments` 是这场会**各段的时长与时间**（续接节点加上去的
        读数，盘上段记录原文四格，两段的会看得出接过）。
        """
        rows: list[dict[str, Any]] = []
        for entry in self.registry.ordered():
            path = str(entry["dir"])
            probe = classify_workspace_dir(path)
            record = self.store.read_session_json(path)
            state, date, mode = self._disk_record_cells(record, path)
            rows.append(
                {
                    "dir": path,
                    "session_id": None if record is None else record.get("session_id")
                    or Path(path).name,
                    "dir_exists": probe["kind"] != "missing",
                    "kind": probe["kind"],
                    "suspected": bool(probe["suspected"]),
                    "state": state,
                    "date": date,
                    "mode": mode,
                    "segments": self._disk_segment_cells(record),
                    "has_offline": has_offline_transcript(path),
                    "added_at": entry["added_at"],
                    "last_opened_at": entry["last_opened_at"],
                }
            )
        recent = self.registry.recent()
        return {
            "sessions": rows,
            "recent": None if recent is None else str(recent["dir"]),
            "post_jobs": self.post_jobs_state(),
        }

    # 会后的多趟活与转码工序（闸与登记的读法归 runtime_post / runtime_transcode）

    def _registry_entry_for(self, target: str) -> dict[str, Any] | None:
        """`target` 在注册表里那一条（判据与 `GET /api/sessions` **同一套**：`normcase` 后的
        规范路径）。没登记过 → `None`。"""
        wanted = os.path.normcase(canonical_dir(target))
        for entry in self.registry.entries():
            if os.path.normcase(canonical_dir(str(entry["dir"]))) == wanted:
                return entry
        return None

    def _note_registry_open(self, session_dir: str, *, why: str) -> None:
        """把开过的这场顺手在注册表上记一笔 `last_opened_at`（失败只警告、不阻塞录音）。"""
        try:
            self.registry.open(session_dir)
        except OSError as exc:
            _log.warning(
                "工作区注册表写不进去（%s: %s）；场次的 %s 照旧运转（%s）",
                type(exc).__name__, exc, session_dir, why,
            )
