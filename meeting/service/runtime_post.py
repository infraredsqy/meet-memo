"""会后作业 mixin：精细稿 / 纪要 / 停止 / 分钟层认领。

**闸按（种类, 场次）重排**（转码搬出收尾节点）：进程级一刀切（「任何一趟会后在飞，
别的按钮一律拒」）已废 —— 每一种活按 `(kind, dir)` 一趟一格，同一种重活在同一场上
只能有一趟、第二趟如实拒；**纪要免闸**（与转码、与其它场次、与别的会后动作都并行）；
唯一硬闸 = 本地大模型互斥（录音中拒「生成会后精细稿」，反向由 `start` 那一侧拒）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from meeting.backends.base import SOURCE_IDS
from meeting.service.runtime_shared import ActionError, PostJob, name_of_kind
from meeting.state import now_iso
from meeting.workspace import canonical_dir, has_offline_transcript

_log = logging.getLogger("meeting.service.runtime")

#: 纪要免闸下「同一场会后精细稿还在跑」的如实说破句（回执与界面读数同源，不许静默降级）
MINUTES_DEGRADED_NOTE = (
    "会后的精细稿还在跑：这一趟拿到的是会中稿；等它跑完再出纪要能对上会后稿"
)


def _mark_post_job_finished(job: PostJob | None, finished_at: str) -> None:
    """把会后那一趟的现场读数标成"已收工"（**不把那一格抹掉**）。

    判"有没有在飞"的那一问由 `ServiceRuntime.post_jobs_state()` 按 `thread.is_alive()`
    回答；这一格留着是为了让「上一趟是哪一趟、什么时候起的、什么时候收的」在跑完之后
    还读得到（`GET /api/sessions/{id}` 的 `post` 那一格）。
    """
    if not isinstance(job, dict):
        return
    if job.get("finished_at") is None:
        job["finished_at"] = str(finished_at)


class RuntimePostMixin:
    """会后作业 mixin。"""

    def _session_dir_for_post(self, args: dict[str, Any] | None = None) -> str:
        """这两个按钮跑的是哪一场。

        **给了 `args["dir"]` 就按它指定的那一场跑**（工作台里选中并按下按钮的那一行就是
        这一场：界面那一格 `has_offline` 说的也是它，动作与界面因此同源），该路径要过与
        `GET /api/sessions` 同一套注册表 / 目录判读的校验 —— 不在注册表里、目录不在原地、
        或这一场正是**本进程此刻在录**的那一场（`_is_actively_recording()`，与清单行读数
        同一个判据）都按 `invalid_setting`(400) 如实回错档；**盘上残档不拒**——盘上
        `session.json` 停在哪一档（收尾半路 `draining`、录音半路 `state="recording"` 的
        强杀残档）都不拦：收尾走到哪一步、录音有没有好好收场，都不是会后动作的闸（被强杀
        丢在半路的那一场，工作区读数就是「录音结束」，按钮照按）。本进程在录的那一场对
        `post_offline` / `post_minutes` 到不了这里——动作本体先走 `_reject_recording_post()`
        的运行时闸、按 `draining`(409) 拦下；这一道 400 只落到不走那道闸的
        `speaker_rename`（会中不提供改名）头上。
        **没给 `dir` 时退回原来那条**：跑**收尾刚走完、盘上停着的那一场**
        （`session` 还在，命令行与既有用例都走这一路）。

        没有场次时按 `draining` 档拒（`_session_dir_for_post()` 尾上那一档）。
        """
        raw = None if args is None else args.get("dir")
        if raw is not None and str(raw).strip():
            # 两档寻址判读（清单 / 原地）收在 `_session_dir_tier()` 一处（唯一出处）；
            # 这一面按契约定的是两档都 `invalid_setting`(400)，失败句与 detail 组装留下。
            target, tier, kind = self._session_dir_tier(raw)
            record = self.store.read_session_json(target)
            disk_state = None if record is None else str(record.get("state") or "")
            why: str | None = None
            if tier == "unregistered":
                why = ("这一场不在工作台清单里（`GET /api/sessions` 读的就是那份注册表）："
                       "先在界面上把它打开，或把 `dir` 指到清单里的一行")
            elif tier == "missing":
                why = "这一场的目录已不在原地"
            elif self._is_actively_recording(target):
                why = "这一场还在录音中：结束录音之后才能按"
            if why is not None:
                raise ActionError(
                    "invalid_setting",
                    why,
                    status=400,
                    detail={
                        "key": "dir",
                        "value": str(raw),
                        "legal": [
                            "工作台清单里、目录还在原地、不是本进程正在录的一场；"
                            "省略时跑收尾刚落定的那一场",
                        ],
                        "dir": target,
                        "kind": kind,
                        "state": disk_state or None,
                    },
                )
            return target
        session = self.snapshot.session
        if session is None:
            raise ActionError(
                "draining",
                "现在没有可以跑后会的场次（先录一场、再按结束录音）",
                status=409,
                detail={"session_id": None, "dir": None},
            )
        return str(session.dir)

    def _start_post_job(
        self,
        kind: str,
        target: str,
        *,
        claimed_minutes: bool = False,
        record_inflight: bool = True,
    ) -> None:
        """起一条 daemon 线程跑这一趟，并把它记进 `(kind, dir)` 那一格。

        线程体里的异常**只落 log**：这一趟的成不成由盘上的产物与状态文件回答
        （`meeting_offline_status.json` / `meeting_minutes_status.json` /
        `meeting_transcode_status.json`），与既有钩子那几条契约同一姿态。

        `claimed_minutes=True` 表示纪要那一层的 claim 已经在动作本体里同步认过（m2），
        线程体那一趟因此直接起线程、不再认第二次。

        **`cancel` 是打断那一趟的接缝**：offline 档起一条 `threading.Event`
        塞进登记，线程体把它以 `is_set` 的形状递给 `run_session(cancel=…)` ——
        旗标立起后线程在**下一块开跑前**收场，盘上状态由 `run_session` 写成
        `interrupted`。minutes 档没有这格（`None`）。转码档的 `cancel` 是**暂停旗标**
        （`transcode_pause` 立它；线程体递给 `run_transcode(pause=…)`，段落边界收场）。

        **起线程之前落一条「在飞」记录**（`meeting/service/postjobs.py`，配置目录的
        `post_jobs.json`；`record_inflight` 缺省真，转码工序传假 —— 那道重启核对只认
        offline / minutes 两档，转码的进度靠盘上凭据与状态文件自己活）。写不上只警告、
        不挡动作。
        """
        cancel = (
            threading.Event()
            if kind in ("offline", "transcode")
            else None
        )
        thread = threading.Thread(
            target=self._run_post_job,
            args=(kind, target, claimed_minutes, cancel),
            name=f"post-{kind}",
            daemon=True,
        )
        self.post_jobs[(str(kind), canonical_dir(target))] = {
            "kind": str(kind),
            "dir": str(target),
            "thread": thread,
            "started_at": now_iso(),
            "finished_at": None,
            #: 离线那一趟的逐块进度记录（下线那条链跑起来之后自己塞进来；其余是 `None`）
            "progress": None,
            #: 打断 / 暂停接缝：offline 与 transcode 是一条 `Event`；纪要那趟 `None`
            "cancel": cancel,
        }
        if record_inflight:
            try:
                from meeting.service import postjobs

                postjobs.record(kind, target, now=self.post_jobs[(str(kind), canonical_dir(target))]["started_at"])
            except Exception:  # 记录落不上只警告：安全网缺席不该挡住产品动作
                _log.warning("会后在飞记录落不上（不影响这一趟起跑）：%s", target, exc_info=True)
        thread.start()

    def _run_post_job(
        self,
        kind: str,
        target: str,
        claimed_minutes: bool = False,
        cancel: threading.Event | None = None,
    ) -> None:
        """线程体：跑会后的某一趟，跑完当场结束（模型用完就关，不留在显存里等下一次）。

        **收工那一刻把 `(kind, dir)` 那一格记成"已收工"而不是抹掉**：抹掉之后按「生成会后
        精细稿」跑完一场、再去读那一场的进度，读到的就是一片空白。判"有没有在飞"看
        `post_jobs_state()`（它按 `thread.is_alive()` 答），占位的那一格留着。

        收工**顺带把这一趟的在飞记录摘掉**（offline / minutes 那两档，只摘 `(kind, dir)`
        自己那一格）：记录还写着而线程活着收了工，说明这一趟是自己跑完的（没被杀），
        下一趟起跑时会落新记录 —— 留着旧记录会让下一次启动多做一次无谓的核对读盘；
        别的槽（还在飞的那几趟）原样保留。清不上只警告（安全网的那一半归启动核对兜）。
        """
        try:
            if kind == "offline":
                self._run_post_offline(target, cancel=cancel)
            elif kind == "transcode":
                self._run_transcode_job(target, pause=cancel)
            else:
                self._run_post_minutes(target, claimed=claimed_minutes)
        except BaseException:  # 后台线程的异常没有别的落点，兜到底
            _log.exception("会后的活儿（%s）抛出来了：%s", kind, target)
        finally:
            _mark_post_job_finished(
                self.post_jobs.get((str(kind), canonical_dir(target))), now_iso()
            )
            if kind in ("offline", "minutes"):
                try:
                    from meeting.service import postjobs

                    postjobs.clear(kind, target)
                except Exception:  # pragma: no cover - 清记录失败只警告
                    _log.warning("会后在飞记录清不掉（不影响这一趟收工）", exc_info=True)
            _log.info("会后的活儿（%s）收工：%s", kind, target)

    def _run_post_offline(self, target: str, cancel: threading.Event | None = None) -> None:
        """「生成会后精细稿」那一趟：**只跑离线那一条链，纪要一动不动**（R26）。

        跑的是与 `meeting post` 同一段实现（`offline.runner.run_session()`），但
        `minutes=False`：这一趟**不通知纪要那一层**（尾巴上那道
        `_announce_minutes()` 不拉起子会话）—— 纪要归另一个按钮。
        引擎按 `post.engine` / `post.model` / `post.dtype` 现造（缺省就是真离线模型）。

        **逐块进度记录在这一趟里立起来**：`run_session(..., progress=…)` 每跑完一块就往
        场次目录里那份 `meeting_offline_progress.jsonl` 上补一行，同一份现场读数由
        `GET /api/sessions/{id}` 随取随用（见 `session_status_dict()`）。

        `cancel` 是打断接缝：以 `is_set` 的 callable 形状递进
        `run_session(cancel=…)` —— 旗标立起后线程在下一块开跑前收场，终态
        `interrupted` 由 `run_session` 自己落。`None` = 现行为（不插任何检查）。
        """
        from meeting.offline import progress as progress_module
        from meeting.offline import runner as offline_runner
        from meeting.offline.runner.session_info import declared_sources, session_id_of

        # 场次元信息从它的家现取（`runner/session_info.py`），逐块进度记录自己
        # 不再反向问编排器。
        ledger = progress_module.OfflineProgressLedger(
            target,
            session_id=session_id_of(target),
            sources=declared_sources(target),
        )
        job = self.post_jobs.get(("offline", canonical_dir(target)))
        if isinstance(job, dict):
            job["progress"] = ledger
        _log.info("按「生成会后精细稿」起这一趟离线链：%s", target)
        offline_runner.run_session(
            target,
            minutes=False,
            progress=ledger,
            cancel=(cancel.is_set if cancel is not None else None),
        )

    def _run_post_minutes(self, target: str, *, claimed: bool = False) -> None:
        """「直接出纪要」那一趟：把**手上有的稿子**交给 DSH（R9：缺会后稿就按现有档位降级）。

        拉起点按 `minutes.hook` 解析（与收尾第 4 步原来那一处同源）；解析不出实现时
        没东西可拉，如实抛出去、由线程体记一条 log —— 动作本身在起线程之前就已经
        把「有没有实现」答完了（见 `post_minutes()`）。

        **`deliver=True` 是人工放行那一格**（隔离闸）：这一趟由用户亲手按「直接出纪要」
        发起，是真人工入口，放行标记在这里显式给真（`meeting post` 那条链则在
        `run_session(announce_minutes=True)` 里给真）。其余一切调用缺省假，纪要那一层
        不会自己跑起来。

        `claimed=True`：那一版 claim 已经由 `post_minutes()` 同步认过（m2），这里不再
        重复认一次 —— 否则会读到自己刚落下的 `running`、把这一趟自己拒掉。
        """
        from meeting.backends.registry import resolve_minutes_hook

        resolution = resolve_minutes_hook()
        if not resolution.has_implementation:
            raise RuntimeError(
                f"minutes.hook 解析不出实现（selector={resolution.selector}）：这一场不产生纪要"
            )
        _log.info("按「直接出纪要」拉起纪要那一层：%s（%s）", target, resolution.selector)
        if claimed:
            resolution.implementation.launch(target, claimed=True, deliver=True)
        else:
            resolution.implementation.launch(target, deliver=True)

    def _reject_start_on_offline_post(self) -> None:
        """唯一硬闸的**另一侧**：会后精细稿在飞时，「开始录音」如实拒。

        本地大模型互斥（`docs/design-intent.md` 第 21 条）：会后精细稿那条链跑在本地
        离线模型上，会中录音要拉起会中模型 —— 两份不并排。界面上按「开始录音」先弹
        「启动新会议会打断会后总结，是否继续」，确认才打断（`post_stop` 受理即回、
        当前块收尾后停、状态 `interrupted`）再起录 —— 服务侧这一道 409 拦的是没走
        弹窗的调用方（脚本、第二个页面），并给「先停再开」的顺序兜底。

        拒的是**任意一场**在飞的 offline（模型只有一份，跟哪一场无关）；detail 与
        「有一趟在飞」既有那一档同形（`{kind, dir, started_at}`）。
        """
        flying = self._post_jobs_of_kind("offline")
        if not flying:
            return
        job = flying[0]
        raise ActionError(
            "draining",
            f"会后的精细稿还在跑（{job.get('dir')}）：先按「停止」打断它（或等当前块收尾），"
            "再开始新一场",
            status=409,
            detail={
                "kind": str(job.get("kind")),
                "dir": str(job.get("dir")),
                "started_at": job.get("started_at"),
            },
        )

    def _reject_recording_post(self, target: str | None = None) -> None:
        """唯一硬闸的**这一侧**：这一场还在录音中时，会后两个按钮都如实拒。

        409 `draining`（理由句照旧口径「还在录音中」）：会还在录着，会后那两个按钮要等
        录音结束。**「落盘没落定」不再是闸** —— 收尾走到哪一步、成没成，都不拦会后动作。

        「这一场还在录音中」指**本进程此刻在录的那一场**（`_is_actively_recording()`）：
        `args["dir"]` 显式点名的那一场在录，同样落这一档（`detail` 带 `top_state` 与那
        一场的两格）。
        """
        if self.snapshot.top_state in ("recording", "alert"):
            session = self.snapshot.session
            raise ActionError(
                "draining",
                "这一场还在录音中：会后的两个按钮要等录音结束",
                status=409,
                detail={
                    "top_state": self.snapshot.top_state,
                    "session_id": None if session is None else session.id,
                    "dir": None if session is None else session.dir,
                },
            )
        if target is not None and self._is_actively_recording(target):
            raise ActionError(
                "draining",
                "这一场还在录音中：会后的两个按钮要等录音结束",
                status=409,
                detail={
                    "top_state": self.snapshot.top_state,
                    "session_id": Path(target).name,
                    "dir": str(target),
                },
            )

    def _reject_kind_slot(self, kind: str, target: str) -> None:
        """`(种类, 场次)` 的槽闸：同一种重活在同一场上只能有一趟，第二趟如实拒。

        409 `draining`，`detail = {kind, dir, started_at}`（与「有一趟在飞」既有那一档
        同形）。**别的场次、别的种类都不挡**（进程级一刀切已废）；纪要那趟另有盘上的
        claim 闸同步兜着（跨进程也拦得住）。
        """
        job = self._post_job_slot(kind, target)
        if job is not None:
            raise ActionError(
                "draining",
                f"这一场已经有一趟{name_of_kind(kind)}在跑：{target}；等它跑完再按",
                status=409,
                detail={
                    "kind": str(job.get("kind")),
                    "dir": str(job.get("dir")),
                    "started_at": job.get("started_at"),
                },
            )

    def post_offline(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`post_offline` 动作：「生成会后精细稿（走离线模型）」。

        跑的是会后**离线**那一条链（慢、占显卡、出带说话人编号的那一份稿）；这一趟
        **不碰纪要**（R26 的两个独立按钮）。起一条 daemon 线程即算启动成功，
        这一趟跑多久由盘上的离线状态文件回答。

        拒绝顺序固定：① 录音中（唯一硬闸的这一侧，409）→ ② 这台机器没接这一层
        （404）→ ③ `dir` 校验（400）→ ④ 同一场已有一趟在跑（409）。

        `args["dir"]` 给定时跑它指定的那一场（校验见 `_session_dir_for_post()`）。
        """
        self._reject_recording_post()
        resolution = self.post_resolution
        if resolution is None or resolution.skipped():
            raise ActionError(
                "action_unavailable",
                "这台机器上没有接会后精细稿这一层（post.hook 缺席或写成 null）",
                status=404,
                detail={"key": "post.hook", "selector": None if resolution is None else resolution.selector},
            )
        target = self._session_dir_for_post(args)
        self._reject_kind_slot("offline", target)
        self._start_post_job("offline", target)
        return {"kind": "offline", "dir": target}

    def post_minutes(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`post_minutes` 动作：「直接出纪要（拿手上有的稿子交给 DSH）」。

        **不补跑离线那一层**（R9 的「这一条是可选的」）：手上有会中稿就拿它出纪要，
        会后稿在的话两份一起给（R11 的两份对拍）；两份都没有时由纪要那一层按现成档位
        记 `failed` + 原因，不静默出一份空纪要。起一条 daemon 线程即算启动成功。

        **纪要免闸**：与转码、与其它场次、与别的会后动作都并行 —— 同一场上会后精细稿
        还在跑时这一趟**照常受理**，但回执**必须说破当时读到的输入档位**（`inputs` 格
        + `note` 那一句），不许静默降级。同一场已有 claim（`AlreadyRunningError`）的
        现行闸保留。

        `minutes.hook = null`（这台机器上不接纪要这一层）时当场拒（404 `action_unavailable`）。

        `args["dir"]` 给定时跑它指定的那一场（校验见 `_session_dir_for_post()`）。

        **在飞判据是同步的**（m2）：起线程之前先按 `minutes` 那一层**盘上的 claim 闸**
        （`MinutesHook.claim()` → `meeting/minutes/claim.py::mark_running()`，既有那一把锁，
        不另发明一套）同步认领一次 —— 上一趟还在飞时当场按 409 `draining`
        如实回一句「已经在飞」，而不是回 `{kind: 'minutes'}` 那个假成功（线程体里那条
        `AlreadyRunningError` 原先只落一条 log，动作本体早就把「已起」答出去了）。
        """
        self._reject_recording_post()
        from meeting.backends.registry import resolve_minutes_hook

        resolution = resolve_minutes_hook()
        if not resolution.has_implementation:
            raise ActionError(
                "action_unavailable",
                "这台机器上没有接纪要这一层（minutes.hook 缺席或写成 null）",
                status=404,
                detail={"key": "minutes.hook", "selector": resolution.selector},
            )
        target = self._session_dir_for_post(args)
        receipt: dict[str, Any] = {"kind": "minutes", "dir": target}
        receipt.update(self._minutes_inputs_tier(target))
        self._reject_kind_slot("minutes", target)
        claimed = self._claim_minutes_layer(resolution, target)
        try:
            self._start_post_job("minutes", target, claimed_minutes=claimed)
        except BaseException as exc:
            # 线没起来（`threading.Thread(...)` / `thread.start()` 这一类）：刚认下的那一版
            # claim 不能留在盘上 —— 留着就是一条永远在飞的记录，之后每一次按「直接出纪要」都被
            # claim 闸按 409 拒，界面上没有出路（m4）。退回它、再把异常原样上抛。
            if claimed:
                self._release_minutes_claim(resolution, target, exc)
            raise
        return receipt

    def _minutes_inputs_tier(self, target: str) -> dict[str, Any]:
        """这一场此刻的纪要输入档位与降级说破句（**动作受理时**当场读盘）。

        四档口径照纪要层：`both`（两份都在）/ `live_only`（只有会中稿）/
        `offline_only`（只有会后稿）/ `none`。**同一场的会后精细稿还在跑**时，这一趟
        注定拿不到它 —— 回执必须把这件事说破（`note` 一句），不许静默降级。
        """
        # 会中稿文件名是冻结契约，判读指向权威点 `meeting/minutes/chunk.py` 的
        # `LIVE_TRANSCRIPT_TEMPLATE`（读侧 `session_transcripts_dict()` 按它找稿）；
        # `minutes/prompt.py` 的 `KIND_FILE_TEMPLATES` 是写进清单给人看的那串
        # （`<source>` 占位、不可 format），不当路径用。
        from meeting.minutes.chunk import LIVE_TRANSCRIPT_TEMPLATE

        directory = Path(target)
        live = any(
            (directory / LIVE_TRANSCRIPT_TEMPLATE.format(source=source)).is_file()
            for source in SOURCE_IDS
        )
        offline = bool(has_offline_transcript(directory))
        if live and offline:
            tier = "both"
        elif live:
            tier = "live_only"
        elif offline:
            tier = "offline_only"
        else:
            tier = "none"
        receipt: dict[str, Any] = {"inputs": tier}
        if self._post_job_slot("offline", target) is not None:
            receipt["note"] = MINUTES_DEGRADED_NOTE
        return receipt

    def post_stop(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`post_stop` 动作：停掉正在跑的「生成会后精细稿」那一趟（R28）。

        **受理即回，不等块边界**：立起取消旗标就交回执 —— 线程在
        下一块开跑前收场（正在跑的那一块不掐：引擎是同步推理），盘上状态由
        `run_session` 自己写成 `interrupted`（终态写入只归它）。界面随后按读数看到
        状态翻档、按钮回到可按（重跑闸只拦 `running`，`interrupted` 天然放行）。

        **多趟在飞后的指向**（转码搬出收尾节点）：在飞的可以不止一场 —— `args["dir"]`
        点名停哪一场；省略时**恰好只有一场 offline 在飞**才停它，多场在飞按 400
        `invalid_setting` 让 `dir` 点名（不猜）。转码那一趟不归它停（暂停走
        `transcode_pause`，机制两样）；在飞的只有纪要那一趟时**如实拒**（机制两样，
        不假装停得了）。

        拒的四档（`code` 全落在既有九个里，不加新）：

          - 没有任何 offline 在飞（没有在飞的会后）→ 409 `draining`，`detail = {}`；
          - `args["dir"]` 点的那一场在飞的是纪要 → 409 `draining`，`detail` 带那一趟
            的 `{kind, dir, started_at}`；
          - 省略 `dir` 而多场 offline 在飞 → 400 `invalid_setting`，`legal` 列在飞那几场；
          - `args["dir"]` 指到既无 offline 也无 minutes 在飞的一场 → 409 `draining`。

        成功体与两个按钮同一信封（`post` 键），多一格 `stopping: true` 说出
        「停的是哪一场、受理了没有」——「停没停成」由盘上那份状态文件回答。
        """
        args = dict(args or {})
        raw_dir = args.get("dir")
        wanted = None if raw_dir is None or not str(raw_dir).strip() else canonical_dir(raw_dir)
        if wanted is None:
            # 省略 dir 的指向解析收在 `_only_flying_job()` 一份（转码的暂停/继续同一份）：
            # 恰一趟 offline 在飞才停它，多趟在飞按 400 让 dir 点名（不猜）。
            job = self._only_flying_job(
                "offline", what="停", flying_label="在飞的「生成会后精细稿」"
            )
            if job is None:
                minutes = self._post_jobs_of_kind("minutes")
                if minutes:
                    job = minutes[0]
                    raise ActionError(
                        "draining",
                        "在飞的是纪要那一趟：停的动作只接「生成会后精细稿」（offline）"
                        "——纪要那一层是另一套机制，本动作停不了它",
                        status=409,
                        detail={
                            "kind": str(job.get("kind")),
                            "dir": str(job.get("dir")),
                            "started_at": job.get("started_at"),
                        },
                    )
                raise ActionError(
                    "draining",
                    "没有正在跑的会后可停（最后一趟已经收工；被打断/跑完的那一场直接重按按钮即可）",
                    status=409,
                    detail={},
                )
        else:
            job = self._post_job_slot("offline", wanted)
            if job is None:
                minutes = self._post_job_slot("minutes", wanted)
                if minutes is not None:
                    raise ActionError(
                        "draining",
                        "在飞的是纪要那一趟：停的动作只接「生成会后精细稿」（offline）"
                        "——纪要那一层是另一套机制，本动作停不了它",
                        status=409,
                        detail={
                            "kind": str(minutes.get("kind")),
                            "dir": str(minutes.get("dir")),
                            "started_at": minutes.get("started_at"),
                        },
                    )
                raise ActionError(
                    "draining",
                    f"这一场没有在飞的会后可停：{wanted}（最后一趟已经收工；重按按钮即可）",
                    status=409,
                    detail={},
                )
        kind = str(job.get("kind"))
        target = str(job.get("dir"))
        started_at = job.get("started_at")
        cancel = job.get("cancel")
        if not isinstance(cancel, threading.Event):  # pragma: no cover - 旧现场兜底
            raise ActionError(
                "action_unavailable",
                "这一趟没有可立的取消旗标（旧现场）：等它自己收工，或重启服务",
                status=404,
                detail={},
            )
        cancel.set()
        _log.info("按「停掉会后」立起取消旗标：%s（offline）；线程在下一块开跑前收场", target)
        return {"kind": kind, "dir": target, "stopping": True}

    def _claim_minutes_layer(self, resolution: Any, target: str) -> bool:
        """起线程之前**同步**登记一次「纪要这一层在飞」（m2），**返回认没认成**。

        判据复用 `meeting/minutes/claim.py` 那把 claim 闸（实现经 `claim()` 暴露它，
        没有 `claim()` 的实现照旧走线程体那条路，返回值是 `False`）：盘上停着一版 `running`
        时这一趟当场按 409 `draining` 拒，`detail` 带上那一层的状态文件落点与既有那一版的原因。
        """
        claim = getattr(resolution.implementation, "claim", None)
        if not callable(claim):
            return False
        from meeting.minutes.claim import AlreadyRunningError
        from meeting.minutes.status import status_path

        try:
            claim(target)
        except AlreadyRunningError as exc:
            raise ActionError(
                "draining",
                f"纪要这一层已经在飞（{status_path(target).name} 停在 running）：这一趟不再拉起子会话",
                status=409,
                detail={
                    "kind": "minutes",
                    "dir": target,
                    "status_path": str(status_path(target)),
                    "reason": str(exc),
                },
            ) from exc
        return True

    def _release_minutes_claim(
        self, resolution: Any, target: str, exc: BaseException
    ) -> None:
        """起线程那一步抛出来时，把刚认下的那一版 claim **如实退回**（m4）。

        这一版是 `claim()` 同步落下的（`meeting/minutes/claim.py` 那把既有 claim 锁）；线没起来
        的话就没有人来收它，盘上会永远停在 `running`，之后每一趟都被 claim 闸按 409 拒
        （`--force` 是命令行那一趟的出路，界面上没有）。这里按 `MinutesHook._mark_failed()`
        那支**既有写法**把它补成 `failed` + 原因（同一份状态文件、同一个写入函数，
        不另发明一套锁、也不动 `claim()` 的语义），随后调用方原样上抛异常。

        实现没有 `_mark_failed()` 时（`claim()` 是可选能力，替身可以只实现它）直接按同一份
        状态文件的既有写法补一版：退回这一步失败只留一条 log，不掩盖原来那个异常。
        """
        mark_failed = getattr(resolution.implementation, "_mark_failed", None)
        if callable(mark_failed):
            mark_failed(target, exc)
            return
        try:
            from meeting.minutes import status as status_module

            status_module.mark_failed(target, f"{type(exc).__name__}: {exc}")
        except Exception:  # pragma: no cover - 退回这一步失败时不掩盖原来那个异常
            _log.exception("会后纪要这一版的 claim 退不回去（原异常照上面那条 log）")
