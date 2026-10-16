"""场次生命周期 mixin：起 / 停 / 静音 / 暂停 / 继续 / set_source / 写盘。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from meeting import logging_setup
from meeting.backends.base import SOURCE_IDS
from meeting.session import (
    SESSION_FILE_NAME,
    archive_name,
    build_session_record,
    next_segment_number,
    resolve_workspace_dir,
    steps_from_ended,
)
from meeting.service.runtime_shared import (
    DEFAULT_SESSION_MODE,
    SESSION_MODES,
    ActionError,
    _DaemonThreadPoolExecutor,
)
from meeting.state import (
    DRAIN_STEP_IDS,
    Alert,
    InvalidTransitionError,
    SessionInfo,
    begin_drain,
    now_iso,
    start_recording,
)

_log = logging.getLogger("meeting.service.runtime")


#: 会中模型「收 / 放」那两次调用专用的工作线程条数（`docs/design-intent.md` 第 21 条）。
#:
#: **为什么这条也要离开事件循环**：暂停与继续都是要**当场应答**的按钮动作，而它们各自要调
#: 一次会中模型的生命周期方法 —— `release_gpu()` 要等在飞的推理收干净（上界 5 秒）、
#: `acquire_gpu()` 之后紧接着就是权重的加载（秒级）。直接跑在事件循环上，暂停/继续这几秒里
#: `/api/heartbeat`、`/api/state`、SSE 与其它动作一起停摆；界面的心跳探测会因此判成「失联」
#: （第 17 条那一条致命级）。因此两次调用都提交到后台，动作本身当场返回
#: （与「生成会后精细稿」/「直接出纪要」起一条 daemon 线程即算启动成功同一姿态）。
#:
#: **为什么是 1 而不是几条**：这两次调用是**同一格模型引用**上的一进一出，必须按用户按下的
#: 顺序排队执行 —— 先「暂停 → 释放」，后「继续 → 拉起」。单线程池给出这条 FIFO 顺序
#: （收尾那台池取 4 条，要的是「被放弃的那一步 + 后续步」并行可用，与这里要的东西正相反）。
MODEL_EXECUTOR_MAX_WORKERS = 1


class RuntimeSessionsMixin:
    """场次生命周期 mixin：起 / 停 / 静音 / 暂停 / 继续 / set_source / 写盘。"""

    def _session_record(
        self,
        session: SessionInfo,
        *,
        state: str,
        ended_at: str | None,
        interrupted: bool,
        steps: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """按当前快照组装一份 `session.json` 的内容（盘上那一版的唯一组装点）。

        段记录从采集引擎现场取（`segments` / 各路的段文件名表），并**与开场读到的旧记录
        合并**（`_resumed_record`）：续接（同一工作区再录一段，第 9 条）时旧段的原记录
        一个字不动地保留，本段（引擎里那一条）追加在后面；引擎还没起（开场第一版落盘
        那一刻）时本段还没有记录，只把**即将开出的段文件名**按命名规则预告进去
        （`next_segment_number()`——与引擎构造时定的段号同一条算式，盘上状态未变，两处同值）。

        `audio_duration_seconds` 是**权威时长**（第 7 条）：= 合并后各段
        `duration_seconds` 之和，**未写完的段不计入**——它是这份文件里唯一被当判据的
        元信息，按段列表现算而不是沿用旧合计，旧段记录残缺时也能自愈。
        场次身份三格（`date` / `created_at` / `started_at` / `mode`）在续接档沿用
        **开场那一次**的值（一场会一个身份；本段自己的起止戳在 `segments` 里），
        新一场取本场的值。除此之外元信息一律不当判据（第 6 条）。
        """
        devices = {item.id: item.device_name for item in self.snapshot.sources}
        # `getattr` 而非属性访问：替身（null/dev）没有 `.session` 这一位，那一刻就是「还没有段」
        engine = (
            getattr(self.capture_backend, "session", None)
            if self.capture_backend is not None
            else None
        )
        previous = self._resumed_record if isinstance(self._resumed_record, dict) else {}
        old_sources = (
            previous.get("sources") if isinstance(previous.get("sources"), list) else []
        )
        old_archives = {
            str(item.get("id")): [str(name) for name in (item.get("archives") or [])]
            for item in old_sources
            if isinstance(item, dict) and item.get("id") is not None
        }
        old_segments = [
            dict(item)
            for item in (previous.get("segments") or [])
            if isinstance(item, dict)
        ]
        if engine is not None:
            new_segments = engine.segment_records()
            new_archives = {
                source: list(engine.archives_for(source)) for source in SOURCE_IDS
            }
        else:
            new_segments = []
            upcoming = next_segment_number(session.dir, SOURCE_IDS)
            new_archives = {
                source: [archive_name(source, upcoming)] for source in SOURCE_IDS
            }
        segments = old_segments + new_segments
        # 旧表 + 本段名，保序去重（替身不落盘时新旧可能同名；段表里同名两行就成了谎话）
        archives = {
            source: list(
                dict.fromkeys(old_archives.get(source, []) + new_archives[source])
            )
            for source in SOURCE_IDS
        }
        duration = sum(
            float(item["duration_seconds"])
            for item in segments
            if isinstance(item.get("duration_seconds"), (int, float))
        )
        return build_session_record(
            session_id=session.id,
            session_dir=session.dir,
            mode=str(previous.get("mode") or session.mode),
            state=state,
            created_at=str(previous.get("created_at") or session.started_at),
            started_at=str(previous.get("started_at") or session.started_at),
            devices=devices,
            steps=steps,
            ended_at=ended_at,
            interrupted=interrupted,
            segments=segments,
            audio_duration_seconds=duration,
            archives=archives,
        )

    def _write_session(
        self,
        *,
        ended_at: str | None = None,
        interrupted: bool | None = None,
        state: str | None = None,
    ) -> str:
        """把当前快照写成盘上那一版 `session.json`（**原子的那一处**）。

        `interrupted=None`（普通调用点）时沿用盘上已经写着的那一版 —— 于是第三个 Phase 的
        强制打断路径先落盘的 `interrupted: true` **不会被取消后的登记抹回否**
        （phase2 §3.5.6 的「三条不覆盖约束」之一）。

        `state=None`（普通调用点、也是绝大多数调用点）时盘上那一档由
        `_session_state_on_disk()` 经 `state_for()` 的四档映射算出来。**只有取消路径**
        （`_settle_step(..., cancelled=True)`）显式传 `state="draining"`：它把这一档**钉住**，
        不让映射接管。理由正是 §3.5 第 3 条的第二条不覆盖约束（「不许把 `state` 从 `draining`
        改成别的值」）—— 现场里前面已经有 `failed` 步、在飞那一步又被 `cancel()` 打断时，
        `state_for()` 的 `has_failed_step → failed` 那一档会算出 `"failed"`，把
        `mark_interrupted()` 刚写下的 `draining` 覆盖掉；而 phase2 冻结的
        `state_for()` 四档映射本身是跨 Phase 契约、**不许在这里改**。
        取消路径因此只钉这一档的**写值**，映射与四档取值域一个字不动。
        """
        session = self.snapshot.session
        if session is None:  # pragma: no cover - 调用点都有会话
            raise InvalidTransitionError("没有会话可写盘", top_state=self.snapshot.top_state)
        if interrupted is None:
            existing = self.store.read_session_json(session.dir) or {}
            interrupted = bool(existing.get("interrupted", False))
        steps = steps_from_ended(self.snapshot.ended) if self.snapshot.ended is not None else None
        record = self._session_record(
            session,
            state=self._session_state_on_disk() if state is None else str(state),
            ended_at=ended_at,
            interrupted=interrupted,
            steps=steps,
        )
        return self.store.write_session_json(session.dir, record)

    def start_session(self, mode: str | None = None, dir: Any = None) -> SessionInfo:
        """`start`：判态 → 校验落点与 `mode` → 判后端可用性 → 写第一版文件 → 进 `recording`。

        落点 = 请求**显式**给的目标工作区目录（`args.dir`，已存在的目录）：本方法**不建目录、
        不按日期命名、没有默认落点**（`docs/design-intent.md` 第 3、4 条）。

        目标目录里**已有** `session.json` 时这一场是**续接**（第 9 条：一段 = 一次录制，
        同一工作区再录一段）：开场先把旧记录读进来（`_resumed_record`），本段的段记录与
        旧段合并着写盘；旧段的归档一个字节不碰（采集引擎按下一个段号开**新**文件）。
        新一场与续接场的差别只在「有没有旧记录可合并」，后面的流程一条不分岔。
        """
        mode = DEFAULT_SESSION_MODE if mode is None else mode

        # ① 判态三档：已录 / 收尾闸（收窄后）/ 硬闸（会后精细稿在飞）
        if self.snapshot.top_state in ("recording", "alert"):
            raise self._already_recording_error()
        if self.snapshot.top_state == "ended":
            self._reject_start_during_drain()
        self._reject_start_on_offline_post()

        # ② 校验落点（**显式给、已存在**；不建目录）。已有记录的那一档 = 续接：
        #    先把旧记录读进来（本场落盘的每一版都要跟它合并），再动任何东西
        try:
            session_dir = resolve_workspace_dir(dir)
        except ValueError as exc:
            raise ActionError(
                "invalid_setting", str(exc), status=400,
                detail={"key": "dir", "value": None if dir is None else str(dir),
                        "legal": "已存在的目录（本服务不设默认落点、不建目录）"},
            ) from exc
        session_id = Path(session_dir).name
        resumed_record = self.store.read_session_json(session_dir)
        resuming = resumed_record is not None
        self._resumed_record = resumed_record
        # 本段将开出的段号（与引擎构造时同一条算式；此刻引擎还没建，盘上还没有本段文件）
        planned_segment = next_segment_number(session_dir, SOURCE_IDS)

        # ② 校验 args（**在建目录之前**）
        if mode not in SESSION_MODES:
            raise ActionError(
                "invalid_setting",
                f"mode 取值非法：{mode!r}（只能是 {' / '.join(SESSION_MODES)}）",
                status=400,
                detail={"key": "mode", "value": mode, "legal": list(SESSION_MODES)},
            )

        # ③ 判后端可用性
        backend = self.capture_backend
        if backend is None:  # pragma: no cover - registry 保证有替身
            raise ActionError(
                "capture_unavailable", "采集后端未接入：当前没有可用的采集实现", status=503
            )

        # ④ **先把第一版 session.json 落稳** → 挂场次 log → 起采集 → 进 recording
        session = SessionInfo(
            id=session_id,
            dir=session_dir,
            started_at=now_iso(),
            mode=mode,
            started_monotonic=time.monotonic(),
            elapsed_seconds=0.0,
            frozen=False,
        )
        devices = {item.id: item.device_name for item in self.snapshot.sources}
        try:
            # 第一版盘上内容先写稳（此时快照还没动：写不进去就什么都没发生过）
            self.store.write_session_json(
                session.dir,
                self._session_record(
                    session,
                    state="recording",
                    ended_at=None,
                    interrupted=False,
                    steps=[
                        {"id": step_id, "status": "waiting", "detail": "", "error": None}
                        for step_id in DRAIN_STEP_IDS
                    ],
                ),
            )
        except Exception as exc:
            # **不进入 recording**。新场：把刚写下的那份 session.json 收走并留痕
            # （目录是用户的，只清本场写入的文件，不删工作区目录本身）。
            # 续接：**旧记录原样在盘上**（原子写没有半截态，这一写没碰到目标文件），
            # 什么都不收——收走它等于把用户那场会抹掉。
            if not resuming:
                self.store.remove_session_dir(session_dir)
            _log.error(
                "场次开始失败：%s 的 %s 写不进去（%s: %s）：%s",
                session_id, SESSION_FILE_NAME, type(exc).__name__, exc,
                "旧记录原样保留（续接档，未动盘）" if resuming
                else f"已收走本场写入的 {SESSION_FILE_NAME}",
            )
            raise ActionError(
                "capture_unavailable",
                f"{SESSION_FILE_NAME} 写不进工作区目录，"
                + ("旧记录原样保留，本场放弃" if resuming
                   else "已收走本场写入的文件并放弃这一场"),
                status=503,
                detail={"session_id": session_id, "dir": session_dir,
                        "error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        # 盘上第一版已稳，这才把状态推进到 recording
        self.judge.reset_for_new_session()
        # 新一场从零开始：判定器那份提示级登记已经被上面清掉，快照那一格与本类那份键集合
        # 登记同步归零（三样一起清，见 `_clear_hints()`）—— 上一场的提示级不许跨场留下，
        # 也不许让本场第一次刷新把它们当成「键集合没变」（复审 m3）。
        self._clear_hints()
        # 会中总结那两样同样从零开始（缓冲与记录表**走同一个入口**清，见
        # `_clear_session_summary()`）：上一场的行与那几次总结不许跟着进这一场。
        self._clear_session_summary(reason="新的一场开始")
        start_recording(self.snapshot, session=session, mode=mode, devices=devices)
        _log.info(
            "场次开始：%s（目录 %s，mode=%s%s）",
            session_id, session_dir, mode,
            f"，续接第 {planned_segment} 段（旧记录已合并）" if resuming else "",
        )
        # `idle → recording` 的副作用会把两路 `health` 推到 `capturing`（phase2 §3.5.2），
        # 逐路落一条（阈值取 `mode`：本场采哪几路是这一档的口径）
        self._log_health_migrations(
            reason=f"场次开始（mode={mode}）",
            threshold=f"本场 mode={mode}（采到的路进 capturing）",
        )

        try:
            # 采集起不来 → 如实回 `capture_unavailable`(503)，**收走本场已写入的文件**
            # （不删用户的工作区目录本身；态不对 / 后端不可用时盘上不留这一场的记录）
            backend.start(mode, session_dir)
        except Exception as exc:
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            _log.error("采集起不来：%s（场次 %s）", message, session_id)
            if resuming:
                # 续接失败：把旧记录**原样写回**（第一版已被本轮覆盖），再收走本轮
                # 建出的段文件——工作区回到按下去之前的样子，旧段一个字节不丢
                try:
                    self.store.write_session_json(session_dir, resumed_record or {})
                except OSError:
                    _log.error(
                        "续接失败后旧 %s 写回失败（%s）：%s",
                        SESSION_FILE_NAME, session_dir,
                        "盘上留着的是本场第一版（含旧段记录），旧段归档完好",
                    )
                for source in SOURCE_IDS:
                    planned = Path(session_dir) / archive_name(source, planned_segment)
                    try:
                        planned.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        _log.warning("续接失败清场：%s 收不走（如实登记）", planned)
                _log.warning(
                    "已把 %s 的旧记录写回、本轮段文件收走（采集起不来，工作区回到原样）",
                    session_dir,
                )
            else:
                self.store.remove_session_dir(session_dir)
                _log.warning("已收走本场写入的 %s：%s（采集起不来，盘上不留这一场）",
                             SESSION_FILE_NAME, session_dir)
            self._abort_session(session)
            raise ActionError(
                "capture_unavailable",
                str(message),
                status=503,
                detail={"session_id": session_id, "dir": session_dir, "reason": str(message)},
            ) from exc

        try:
            logging_setup.attach_session_log(session_dir)
        except OSError as exc:
            _log.error(
                "挂场次 log 失败（%s: %s）；录音照常继续，这一场没有场次 log", type(exc).__name__, exc
            )
        # 这一场开成了：注册表顺手记一笔 last_opened_at（失败只警告、不阻塞录音）
        self._note_registry_open(session_dir, why="场次开始")

        if self.transcriber_backend is not None:
            try:
                self.transcriber_backend.start(mode, session_dir)
            except Exception:
                # 转写起步失败**不阻塞录音**（转写坏了可以重跑）
                _log.exception("转写后端起步失败（不阻塞录音）")
            # 场次起来这一刻就地刷一遍 `transcript`（判定权仍在 `_refresh_transcript_snapshot()`）：
            # 转写后端刚从 `stopped` 变成在跑，而 `commit()` 就在下面 —— 顺手带上这一格。
            # 这一刷是**必须**的：不刷的话那一次迁移要等健康度节拍，而节拍第一拍落在什么时刻
            # 取决于「用户多快按下开始」，于是空闲档 `stopped -> normal` 这一跳会落在这一场的
            # 第一个提交之前、之后两种位置（`revision` 因此差 1）—— 一条把机器快慢烤进
            # golden 的时序依赖。刷在提交之前，这一跳就固定由这一场的提交带走。
            # `buffered_lines` 照旧只写快照、不参与这次提交。
            self._refresh_transcript_snapshot()

        self.last_frame_ts = None
        self.session_start_mono = time.monotonic()
        # 帧空洞记录也是「本场」的记录：与 `last_frame_ts` 同点清（上一场的空洞不进这一场）
        self._reset_capture_gaps()
        self.commit()
        # 健康度节拍**从这一场开始起**（见 `_ensure_transcriber_health_ticker()` 的说明）
        self._ensure_transcriber_health_ticker()
        return session

    def _abort_session(self, session: SessionInfo) -> None:
        """采集起不来：把这一场撤回空闲态（场次目录已由调用方清掉）。"""
        try:
            logging_setup.detach_session_log()
        except OSError:  # pragma: no cover - 摘不掉也不阻塞
            pass
        self.snapshot.session = None
        self.snapshot.ended = None
        self.snapshot.top_state = "idle"
        # 撤回空闲态：提示级不再成立（提示条只在 recording / alert 上出现），连登记一起清，
        # 于是下面那次提交广播出去的就是「空闲态、告警区不出现」那一份。
        self._clear_hints()
        for item in self.snapshot.sources:
            item.health = "unprobed"
            item.envelope = []
        _log.info("场次撤回空闲态（%s）", session.id)
        self._log_health_migrations(
            reason=f"采集起不来，场次撤回空闲态（{session.id}）",
            threshold="无阈值（撤回这一步不看阈值）",
        )
        self.commit()

    def stop_session(self, reason: str = "user") -> asyncio.Task | None:
        """`stop`：冻结时长 → 进 `ended·draining` → 起收尾协程（句柄存 `_drain_task`）。"""
        if reason not in ("user", "ctrl_c"):
            raise ActionError(
                "not_recording",
                f"stop_session 的 reason 取值非法：{reason!r}（只能是 user / ctrl_c）",
                status=409,
            )
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，结束动作无效", status=409)

        session = self.snapshot.session
        if session is not None:
            # 暂停中结束：先把这一段暂停结掉（秒数计入时长），再冻结本场秒数 ——
            # 与引擎那一侧 `finalize()` 关掉暂停区间是同一件事的两半。
            if session.paused:
                self._pause_span(session, moment=time.monotonic())
                self.judge.resume_session()
            session.elapsed_seconds = self._session_elapsed_seconds(session)
        begin_drain(self.snapshot, reason=reason)
        # 进 `ended` 这一刻提示级一并归零（提示条只在 recording / alert 上成立）：清在下面那次
        # 提交之前，于是广播出去的「结束档」那一份里 `alerts.hints` 就是空数组 —— 不必等
        # 0.5 秒那一拍来收尾（收尾三步期间没有任何转写刷新，等节拍会留下一段「已结束、
        # 还挂着提示条」的窗口）。
        self._clear_hints()
        _log.info(
            "场次结束：%s（目录 %s，reason=%s，本场 %ss）",
            session.id if session else "?", session.dir if session else "?", reason,
            round(session.elapsed_seconds, 3) if session else "?",
        )
        # `→ ended` 的副作用把两路 `health` 推到 `stopped`（phase2 §3.5.2），逐路落一条
        self._log_health_migrations(
            reason=f"场次结束、进收尾（reason={reason}）",
            threshold="无阈值（收尾这一步不看阈值）",
        )
        self.commit()
        self._drain_task = asyncio.get_running_loop().create_task(self.drain())
        return self._drain_task

    def mute_session(self) -> None:
        """会话级静音：**只影响出声**，不改 `alarm.sound` 持久化值。

        `alerts.*.muted` 不在这里手写 `True`：它由 `commit()` 按当下重算（判定权收在
        `AlertJudge.refresh_muted()` 一处），因此本动作只翻开关，快照随之变 `True`。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，静音动作无效", status=409)
        self.judge.mute_session = True
        _log.info("本场已静音（会话级；下一场 start 自动复位，持久化开关 alarm.sound 未改）")
        self.commit()

    # -- 暂停 / 继续 / 会中开关某一路（R24 / R25） --------------------------

    def _recording_session(self, what: str) -> SessionInfo:
        """本场还没收尾的那一份 `session`（没有就回 `not_recording` 409）。

        与 `mute_session()` 同一档判据：只有 `recording` / `alert` 两态有事可做。
        """
        session = self.snapshot.session
        if self.snapshot.top_state not in ("recording", "alert") or session is None:
            raise ActionError("not_recording", f"现在没有在录的场次，{what}无效", status=409)
        return session

    def _pause_span(self, session: SessionInfo, *, moment: float) -> None:
        """把当前这一段暂停结掉（`resume` 与 `stop` 两处共用）：秒数计入时长、标记复位。"""
        if session.paused_at_monotonic is not None:
            session.paused_seconds += max(0.0, float(moment) - float(session.paused_at_monotonic))
        session.paused_at_monotonic = None
        session.paused = False

    def _model_executor_or_create(self) -> _DaemonThreadPoolExecutor:
        """会中模型「收 / 放」那台执行器（**惰性创建**：没人按过暂停/继续的进程一个线程都不起）。"""
        if self._model_executor is None:
            self._model_executor = _DaemonThreadPoolExecutor(
                max_workers=MODEL_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="model-lifecycle",
            )
        return self._model_executor

    def _submit_model_lifecycle(self, method_name: str, *, what: str) -> None:
        """把一次「会中模型的收 / 放」提交到后台（暂停 → `release_gpu()`；继续 → `acquire_gpu()`）。

        **为什么不在这里直接调**：第 21 条要的是「当场结束」—— 按钮按下去，模型当刻松手，
        界面照旧转。释放要等在飞的推理收干净（上界 5 秒），拉起之后紧接着就是权重的加载
        （秒级）；这两段工夫若花在事件循环上，暂停/继续那几秒里 `/api/heartbeat`、
        `/api/state`、SSE 与其它动作会一起停摆，界面的心跳探测因此判成「失联」
        （第 17 条那一条致命级）。于是把它们交给后台那条线程慢慢做，取舍与收尾三步、
        会中总结那两处同源。

        **提交进单线程池**（`MODEL_EXECUTOR_MAX_WORKERS` = 1）：一进一出按用户按下的顺序
        排队执行 —— 先「暂停 → 释放」、后「继续 → 拉起」，不会反序落到同一格模型引用上。

        **防御式取法**：协议里没有这两个方法（`docs/backend-protocols.md` 第 3 章把它们记成
        可选成员），替身后端（`null` / `dev`）省掉这一格照样跑得通 —— 取不到、不可调用就跳过。

        线程体里的异常**只落 log**：暂停与继续这两个动作不许因为模型那一侧出事而失败
        （R1 的立场：转写坏了可以重跑，录音照跑）。
        """
        call = getattr(self.transcriber_backend, method_name, None)
        if not callable(call):
            return

        def run() -> None:
            try:
                call()
            except Exception:  # noqa: BLE001 - 后台线程的异常没有别的落点
                _log.exception("会中模型%s失败（如实登记，不静默）", what)

        try:
            self._model_executor_or_create().submit(run)
        except Exception:  # noqa: BLE001 - 排不进后台也不许把按钮动作拖失败
            _log.exception("会中模型%s没能排进后台（如实登记，不静默）", what)

    def pause_session(self) -> None:
        """`pause`：这一场暂停 —— 归档改写静音、不录内容（第 8 条）。

        幂等（已经在暂停中时什么都不做）。快照里落在 `session.paused` 这一格上，
        顶层四态一个字不动（第 3 条没有第五个状态）；暂停的那一段时间同时从时长读数里
        扣掉（`_session_elapsed_seconds()`），并交给 `AlertJudge` 把静默判定冻住 ——
        安静房间里暂停三分钟不该弹一条「这一路静默已达 180 秒」（第 17 条）。

        **会中那个模型在这一刻结束**（第 21 条：暂停当场结束模型，继续时再拉起来）：
        经 `_submit_model_lifecycle()` 把 `release_gpu()` 提交到后台，于是暂停这一段它
        退出显存，而按下的这一下当场应答。
        """
        session = self._recording_session("暂停动作")
        if session.paused:
            return
        moment = time.monotonic()
        session.elapsed_seconds = self._session_elapsed_seconds(session, moment=moment)
        session.paused = True
        session.paused_at_monotonic = moment
        self.judge.pause_session()
        pauser = getattr(self.capture_backend, "pause", None)
        if callable(pauser):
            pauser()
        # 会中那个模型**当场结束**（第 21 条）：暂停这一段它退出显存，继续时再拉起来。
        # 调用提交到后台（见 `_submit_model_lifecycle()`）：释放要等在飞的推理收干净，
        # 而暂停这个按钮按下去要当场应答。
        self._submit_model_lifecycle("release_gpu", what="让出显存（暂停期间不占显卡）")
        _log.info(
            "本场已暂停：%s 往归档写数字静音、不录内容（暂停的那几段不计入静默判定，也不进时长）",
            session.id,
        )
        self.commit()

    def resume_session(self) -> None:
        """`resume`：继续 —— 归档回到录内容，时长接着走（幂等）。

        会中那个模型也在这一刻**重新拉起来**（第 21 条：又要用它了）：经
        `_submit_model_lifecycle()` 把 `acquire_gpu()` 提交到后台，与暂停那一半配对。
        """
        session = self._recording_session("继续动作")
        if not session.paused:
            return
        moment = time.monotonic()
        self._pause_span(session, moment=moment)
        session.elapsed_seconds = self._session_elapsed_seconds(session, moment=moment)
        self.judge.resume_session()
        resumer = getattr(self.capture_backend, "resume", None)
        if callable(resumer):
            resumer()
        # 继续 = 又要用它了：把会中模型重新拉起来（与暂停那一半配对，第 21 条）。
        # 同样提交到后台：拉起来之后紧接着就是权重的加载，这个动作当场应答。
        self._submit_model_lifecycle("acquire_gpu", what="重新拉起（继续之后接着用）")
        _log.info("本场已继续：%s 的归档回到录内容（暂停 %ss 已计入时长）", session.id, round(session.paused_seconds, 3))
        self.commit()

    def set_source(self, args: dict[str, Any]) -> None:
        """`set_source`：会中关掉某一路 / 重新打开某一路（第 9 条：那一路写静音，段不切）。

        `active=false` 关掉：那一路停流、改写静音，**不报 `SourceLost`、不进告警**
        （用户按的按钮与「这一路真死了」是两件事）。**关断即收场**：这一路挂着的致命级
        当场清空——横幅消失、告警胶囊回「录音中」，不是「点了没反应」；清完若还有别的路
        真在 `lost`，致命级换挂那一路（顶层停在 `alert`，真采不到的那一路不许跟着这
        一下一起从界面上消失）。

        `active=true` 重新打开：重新挑设备、重开流；**开不起来如实回 `capture_unavailable`(503)**
        （设备被别的程序占着那一档，按按钮的人必须看得见），那一路照旧停在「已关掉」。
        开成了就是**干净开场**：直接回 `capturing`，不回 `lost`、不落「曾中断 / 曾关掉」
        的残留横幅，静默计数从零起算（机主拍板「恢复即收场」，不留痕）。

        快照里这一路的落点是**复用现有的 `health = stopped` 档**（「这一路没在采」的读数本来
        就有这一格，本节点不为它发明新的取值）；打开之后回到 `capturing`。
        """
        session = self._recording_session("会中开关某一路的动作")
        source = args.get("source")
        if source not in SOURCE_IDS:
            raise ActionError(
                "invalid_setting",
                f"source 取值非法：{source!r}（只能是 {' / '.join(SOURCE_IDS)}）",
                status=400,
                detail={"key": "source", "value": source, "legal": list(SOURCE_IDS)},
            )
        active = args.get("active")
        if not isinstance(active, bool):
            raise ActionError(
                "invalid_setting",
                f"active 取值非法：{active!r}（只能是 true / false）",
                status=400,
                detail={"key": "active", "value": active, "legal": [True, False]},
            )
        # 本场没采这一路（`mode` 开场就定死了那几路）：关它 / 开它都没有对象
        engine = getattr(self.capture_backend, "session", None)
        requested = tuple(getattr(engine, "requested", ()) or ())
        if requested and source not in requested:
            raise ActionError(
                "invalid_setting",
                f"这一场采的是 {'、'.join(requested)}，{source} 这一路本场没有采",
                status=400,
                detail={"key": "source", "value": source, "legal": list(requested)},
            )

        item = self.snapshot.source(source)
        if not active:
            if item.health == "stopped":
                return
            deactivate = getattr(self.capture_backend, "deactivate_source", None)
            if callable(deactivate):
                deactivate(source)
            item.health = "stopped"
            item.restored = False
            item.lost_since = None
            # **关断即收场**：这一路挂着的致命级当场清空（机主点关掉，横幅要当场消失、
            # 胶囊回「录音中」，不是「点了没反应」）。只清这一路自己的那一条——致命级
            # 同时只留一条，横幅是别路的不动。清完若还有别的路真在 `lost`，致命级换挂
            # 那一路、顶层停在 `alert`：真采不到的那一路的横幅不能跟着这一下一起收
            # （设计意图第 1 条：采不到的那一路不许被忽略）。
            critical = self.snapshot.alerts.critical
            if critical is not None and critical.source == source:
                self.snapshot.alerts.critical = None
                remaining = [one for one in self.snapshot.sources if one.health == "lost"]
                if remaining:
                    self.snapshot.alerts.critical = self._resurface_lost_alert(remaining[0].id)
                elif self.snapshot.top_state == "alert":
                    self.snapshot.top_state = "recording"
            # 这一路的判定记录（静默 / 恢复累计、抑制窗口、在告警记号）一并收场：帧从这一刻
            # 起不再进判定（切断在 `_apply_level_frame` 的帧入口），旧记录留着只会污染重开
            # 之后的干净开场。
            self.judge.reset_source(source)
            _log.info("会中关掉 %s 这一路：改写静音（不报故障、不进告警）", source)
            self._log_health_migrations(
                source=source,
                reason="用户会中主动关掉这一路（写静音）",
                threshold="无阈值（用户按的按钮不走告警判定）",
            )
            self.commit()
            return

        if item.health != "stopped":
            return
        activate = getattr(self.capture_backend, "activate_source", None)
        try:
            if callable(activate):
                activate(source)
        except Exception as exc:  # noqa: BLE001 - 打不开这一路如实回执（设备被占着就是这一档）
            reason = f"{type(exc).__name__}: {exc}"
            _log.error("重新打开 %s 这一路失败：%s（那一路照旧停在已关掉）", source, reason)
            raise ActionError(
                "capture_unavailable",
                f"{source} 这一路打不开：{reason}",
                status=503,
                detail={"source": source, "error": reason},
            ) from exc
        # **重开 = 干净开场**（机主拍板「恢复即收场」）：直接回 `capturing`，不回 `lost`、
        # 不落「曾中断 / 曾关掉」的残留横幅，静默计数从零起算。旧口径「关掉之前还挂着
        # 致命级时回 `lost` 走自然恢复」随「关断即收场」退场——关断已把这一路的致命级
        # 清空，重开那一刻不存在「这一路自己的横幅」可恢复；判定记录在关断那一刻已收走，
        # 这里再收一道是幂等的（真有信号时的正常恢复读数照给，与此无关）。
        self.judge.reset_source(source)
        item.health = "capturing"
        _log.info("会中重新打开 %s 这一路：读线程回到读源（health=capturing，干净开场）", source)
        self._log_health_migrations(
            source=source,
            reason="用户会中重新打开这一路",
            threshold="无阈值（打开设备不看阈值）",
        )
        self.commit()

    def _resurface_lost_alert(self, source: str) -> Alert:
        """把仍然真在 `lost` 的那一路的致命级重新摆回横幅（**致命级换挂**）。

        关掉横幅上那一路时，同场若还有**别的**路真在 `lost`：它的告警对象早被「同时只留
        一条致命级」覆盖掉了，但原文还在判定器的登记里（`event_message()` 原话照搬）、
        出事的时刻还在快照上（`lost_since`）——换挂的一条照这两样如实组装，不另造文案、
        不改「从几点起」。`muted` 只给当下初值，`commit()` 那一次重算说了算。
        """
        item = self.snapshot.source(source)
        return Alert(
            level="critical",
            source=source,
            since=item.lost_since or now_iso(),
            duration_seconds=0.0,
            message=self.judge.event_message(source) or f"{source} 这一路的采集流中断了",
            muted=self.judge.is_muted(source),
        )
