"""转码独立工序 mixin：flush 落定自动起、可暂停 / 继续 / 补跑的三只动作与进度读数。

转码已摘出收尾序列：它是这一场的**独立后台工序**（`meeting/capture/transcode.py` 是
判据与主流程的唯一一份，全仓共用）——flush 落定即自动在后台起，不占收尾预算、不挡
`start` 与会后动作；可暂停、可继续、可补跑。工序登记在 `(kind, dir) = ("transcode",
场次目录)` 那一格上：同一场只能有一道在跑，第二趟如实拒；**与转写、纪要、别的场次
互不设闸**。

三只动作（命名照 `post_*` 族的「域_动词」风格）：

  - `transcode_run` —— 补跑：从盘上凭据接着做（做完的段凭据认账、跳过不重转）；
  - `transcode_pause` —— 暂停：立起暂停旗标即回（受理即回），工序在**段边界**落 `paused`
    并就地等旗标摘下（同一条线程活着等，不散场）；
  - `transcode_resume` —— 继续：在飞且暂停中就摘旗，工序在段边界接着前进；本进程没有
    这道工序在飞（重启过、或暂停那一刻进程死了）就从盘上凭据接着做 —— **进度与暂停状态
    都活在盘上**（凭据 + `meeting_transcode_status.json`），不依赖进程内存。

「转码在飞不构成 `start` 的阻挡」是这一层的底线：这道工序不吃会中 / 会后那两张模型
的显卡（转码是 pydub / ffmpeg 的活），谁都不必等它。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from meeting.service.runtime_shared import (
    ActionError,
    PostJob,
    _slot_alive,
    name_of_kind,
)
from meeting.state import now_iso
from meeting.workspace import canonical_dir

_log = logging.getLogger("meeting.service.runtime")


class RuntimeTranscodeMixin:
    """转码独立工序 mixin。"""

    # -- (kind, dir) 槽位的公共读法（post 与 transcode 两个 mixin 共用）------

    def _post_job_slot(self, kind: str, target: str) -> PostJob | None:
        """`(kind, dir)` 那一格**还活着**的登记（死了 / 没有 → `None`）。"""
        job = self.post_jobs.get((str(kind), canonical_dir(target)))
        return job if _slot_alive(job) else None

    def _post_jobs_of_kind(self, kind: str) -> list[PostJob]:
        """某一种活全部**活着**的登记（按目录名排序，读数与拒跑句的次序因此稳定）。"""
        return [
            job
            for (job_kind, _dir), job in sorted(self.post_jobs.items())
            if job_kind == str(kind) and _slot_alive(job)
        ]

    def post_jobs_state(self) -> list[dict[str, Any]]:
        """全部在飞的活（`GET /api/sessions` 顶层 `post_jobs` 那一格的原文）。

        每趟三格 `{kind, dir, started_at}`；线程死透的那一格顺手记下 `finished_at`
        （「上一趟是哪一趟、什么时候收的」还要给 `GET /api/sessions/{id}` 的 `post`
        那一格用），但不再出现在这张清单里。
        """
        flying: list[dict[str, Any]] = []
        for (_kind, _dir), job in sorted(self.post_jobs.items()):
            if not isinstance(job, dict):
                continue
            thread = job.get("thread")
            if thread is not None and not thread.is_alive():
                if job.get("finished_at") is None:
                    job["finished_at"] = now_iso()
                continue
            flying.append(
                {
                    "kind": job["kind"],
                    "dir": job["dir"],
                    "started_at": job["started_at"],
                }
            )
        return flying

    # -- flush 落定之后的自动起 ---------------------------------------------

    def _start_transcode_after_flush(self) -> None:
        """收尾 `flush` 落定：这一场的转码在后台自动起（不占收尾预算、不挡任何人）。

        只在**真采集能力**（`capture_capabilities["transcode"]`）在场时起 —— 采集替身
        不写真实音频，没有 PCM 可转。同一场已有一道在跑时不起第二道（槽闸如实记一条
        log，不该发生在正常收尾序列里）。起线程失败只记 log：转码是补得回来的工序
        （凭据都在盘上），不该把它拖成收尾失败档。
        """
        session = self.snapshot.session
        if session is None:
            return
        target = str(session.dir)
        if not self.capture_capabilities.get("transcode"):
            _log.info("转码工序不起：这一场没有真实归档可转（采集替身）：%s", target)
            return
        if self._post_job_slot("transcode", target) is not None:
            _log.warning("转码工序已在跑，flush 落定不重复起：%s", target)
            return
        try:
            self._start_post_job("transcode", target, record_inflight=False)
            _log.info("转码工序已自动起（flush 落定）：%s", target)
        except Exception:  # pragma: no cover - 起不来不拖收尾
            _log.exception("转码工序自动起失败（不影响收尾；可随时补跑）：%s", target)

    # -- 线程体 -------------------------------------------------------------

    def _run_transcode_job(self, target: str, pause: threading.Event | None = None) -> None:
        """转码工序的线程体：`run_transcode()` 跑完即收工（判据与读数都在盘上）。

        `pause` 是暂停旗标（`transcode_pause` 立它）：以 callable 形状递进
        `run_transcode(pause=…)`，工序在**下一段开跑前**收场，状态落 `paused`。
        转码判据（凭据核对、重做、进度落盘）只有一份实现，本方法只是接线。
        """
        from meeting.capture import transcode as transcode_module

        summary = transcode_module.run_transcode(
            target,
            pause=(pause.is_set if pause is not None else None),
        )
        _log.info(
            "转码工序收工读数：%s state=%s（%s/%s 段）",
            target,
            summary.get("state"),
            summary.get("segments_done"),
            summary.get("segments_total"),
        )

    # -- 三只动作 -------------------------------------------------------------

    def _transcode_target(self, args: dict[str, Any] | None) -> str:
        """三只动作跑哪一场：给了 `dir` 按它（过注册表 / 目录判读）；省略时收尾刚落定那一场。

        与 `post_offline` 那一套判据同源（`_session_dir_for_post()`）；补跑对**本进程
        此刻在录的那一场**如实拒（PCM 还在长，凭据对不上账 —— 转码只对已落盘的段说得了话）。
        """
        target = self._session_dir_for_post(args)
        if self._is_actively_recording(target):
            raise ActionError(
                "invalid_setting",
                "这一场还在录音中：归档还没落盘，转码等收尾落定之后再跑",
                status=400,
                detail={
                    "key": "dir",
                    "value": str(target),
                    "legal": ["已落盘的一场（本进程在录的那一场不收）"],
                },
            )
        return target

    def transcode_run(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`transcode_run` 动作：补跑这一场的转码工序（从盘上凭据接着做）。

        拒绝顺序：① `dir` 校验（400）→ ② 同一场已有一道在跑（409）。**别的都不挡**：
        与 offline / minutes、与别的场次、与收尾都并行。
        """
        target = self._transcode_target(args)
        self._reject_kind_slot("transcode", target)
        self._start_post_job("transcode", target, record_inflight=False)
        return {"kind": "transcode", "dir": target, "started": True}

    def transcode_pause(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`transcode_pause` 动作：暂停这一场的转码工序（受理即回，段边界落 `paused`）。

        `args["dir"]` 可省：恰一道在跑时省略即停它；多场在跑时 `dir` 必给（400，不猜）。
        没有在跑的工序 → 409 `draining`（没有可暂停的）。立旗之后那条线程**在段边界
        等旗标摘下**（当前段照完、下一段不开跑），盘上状态落 `paused` —— 工序还活着，
        不是散场。
        """
        job = self._resolve_transcode_job(args, what="暂停")
        cancel = job.get("cancel")
        if isinstance(cancel, threading.Event):
            cancel.set()
        target = str(job.get("dir"))
        _log.info("按「暂停转码」立起暂停旗标：%s；工序在下一段开跑前收场（state=paused）", target)
        return {"kind": "transcode", "dir": target, "pausing": True}

    def transcode_resume(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`transcode_resume` 动作：继续这一场的转码工序。

        在飞且暂停中 → 摘旗，**同一条线程**在段边界接着前进（受理即回）；本进程没有
        这道工序在飞（重启过、或暂停那一刻进程死了）→ 从盘上凭据接着做（与补跑同一条
        路）。暂停状态活在盘上（`meeting_transcode_status.json` 的 `paused`），重启后
        照样续得上。
        """
        args = dict(args or {})
        raw_dir = args.get("dir")
        wanted = None if raw_dir is None or not str(raw_dir).strip() else canonical_dir(raw_dir)
        job = (
            self._post_job_slot("transcode", wanted)
            if wanted is not None
            else self._only_transcode_job("继续")
        )
        if job is not None:
            cancel = job.get("cancel")
            target = str(job.get("dir"))
            if isinstance(cancel, threading.Event):
                cancel.clear()
                _log.info("按「继续转码」摘下暂停旗标：%s（下一段开跑起接着前进）", target)
                return {"kind": "transcode", "dir": target, "resumed": True}
        # 没有在飞的工序：从盘上凭据接着做（补跑同一条路；盘上 `paused` / 半路的
        # `running` 都从这里续）
        return self.transcode_run({"dir": wanted} if wanted is not None else None)

    # -- 指向解析与读数 -------------------------------------------------------

    def _resolve_transcode_job(self, args: dict[str, Any] | None, *, what: str) -> PostJob:
        """暂停 / 继续指向的工序：`dir` 点名或恰一道在跑时省略；指不到就如实拒。"""
        args = dict(args or {})
        raw_dir = args.get("dir")
        wanted = None if raw_dir is None or not str(raw_dir).strip() else canonical_dir(raw_dir)
        job = (
            self._post_job_slot("transcode", wanted)
            if wanted is not None
            else self._only_transcode_job(what)
        )
        if job is None:
            raise ActionError(
                "draining",
                "没有正在跑的转码工序可"
                + what
                + "（暂停后的工序用「继续」接；做完的那一场重按「补跑」即可）",
                status=409,
                detail={},
            )
        return job

    def _only_transcode_job(self, what: str) -> PostJob | None:
        """省略 `dir` 时的指向：恰一道在跑才给它；多道在跑让 `dir` 点名（400，不猜）。"""
        flying = self._post_jobs_of_kind("transcode")
        if len(flying) > 1:
            raise ActionError(
                "invalid_setting",
                f"在跑的转码工序不止一场：用 dir 点名要{what}哪一场",
                status=400,
                detail={
                    "key": "dir",
                    "value": None,
                    "legal": [str(job.get("dir")) for job in flying],
                },
            )
        return flying[0] if flying else None

    def _transcode_progress_for(self, directory: str) -> dict[str, Any]:
        """这一场的转码工序读数（`GET /api/sessions/{id}` 的 `transcode` 那一格）。

        **判据读盘**（补充判据：进度 / 暂停状态要在重启后存活）：状态文件
        `meeting_transcode_status.json` 回答跑到哪一档、第几段；`in_process` 另答
        「本进程里此刻有没有这道工序」（与 `offline.in_process` 同一套读法）。跨进程
        算不出单调钟的差值，`elapsed_seconds` 只在本进程在飞时给。
        """
        from meeting.capture import transcode as transcode_module

        document = transcode_module.read_status(directory)
        state = "idle" if document is None else str(document.get("state"))
        job = self._post_job_slot("transcode", directory)
        elapsed: float | None = None
        if job is not None and isinstance(job.get("started_at"), str):
            elapsed = round(max(0.0, time.time() - _parse_iso_lossy(job["started_at"])), 3)
        sources: list[dict[str, Any]] = []
        if document is not None:
            raw_sources = document.get("sources")
            if isinstance(raw_sources, list):
                for item in raw_sources:
                    if not isinstance(item, dict):
                        continue
                    sources.append(
                        {
                            "source": str(item.get("source")),
                            "done": int(item.get("done") or 0),
                            "total": int(item.get("total") or 0),
                            "remaining": int(item.get("remaining") or 0),
                            "current_segment": item.get("current_segment"),
                        }
                    )
        return {
            "state": state,
            "in_process": job is not None,
            "started_at": None if document is None else document.get("started_at"),
            "finished_at": None if document is None else document.get("finished_at"),
            "elapsed_seconds": elapsed,
            "current_source": _current_source_of(document),
            "sources": sources,
        }


def _current_source_of(document: dict[str, Any] | None) -> str | None:
    """此刻在转哪一路：逐路格里 `current_segment` 不为空的那一路（至多一道在跑）。"""
    if not isinstance(document, dict):
        return None
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list):
        return None
    for item in raw_sources:
        if isinstance(item, dict) and item.get("current_segment") is not None:
            return str(item.get("source"))
    return None


def _parse_iso_lossy(stamp: str) -> float:
    """ISO 时刻 → epoch 秒（读不动就按「刚刚」算；这一格只作附注，不当判据）。"""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return time.time()
