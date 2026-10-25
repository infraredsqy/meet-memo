"""``ServiceRuntime`` 的事实摄取 mixin：心跳 / 电平 / 转写 / 告警 / 捕获缺口。

模块级两个节拍常量与第 1298-1829 行的方法体自 ``meeting/service/runtime.py`` 一字不改复制；
本文件自写内容只有模块 docstring、import 与 ``_log`` 一行、类声明行。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from meeting.asr import pipeline as asr_pipeline
from meeting.backends.base import (
    FrameGap,
    LevelFrame,
    SourceFacts,
    SourceLost,
    TranscriptLine,
)
from meeting.session import state_for
from meeting.service.runtime_drain import DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
from meeting.state import (
    Alert,
    InvalidTransitionError,
    begin_alert,
    now_iso,
    recover_source,
    sample_levels,
)

_log = logging.getLogger("meeting.service.runtime")

#: `levels` 抽帧节奏：对外一律 **4 次/秒**（0.25 秒一帧，两路合在一帧里发）
LEVELS_INTERVAL_SECONDS = 0.25

#: 转写健康度节拍的间隔（秒）。管的是「转写挂了多久能被看见」：每拍读的是两个纯读
#: （标志位 + 队列长度），读数没变时零副作用。取 0.5 秒：界面健康位本来就按秒级刷新，
#: 半秒滞后看不出差别；再往上加只是让「转写停了」多等一拍。用例走的就是这条节拍
#: （不另开接缝），整文件耗时也由它限住。
TRANSCRIBER_HEALTH_INTERVAL_SECONDS = 0.5


class RuntimeFactsMixin:
    """事实摄取 mixin：心跳与帧时钟、电平告警接线、转写刷新与提示级、帧空洞记录。"""

    # 心跳

    def recording_alive(self) -> bool | None:
        """`recording_alive`：`recording` / `alert` 态按帧时钟算；其余态为 `null`。

        「还没有第一帧」单独一档（de899128，开场误报的修法）：首帧比 `start` 晚约 0.5s 才来，
        旧口径把这段算成 `False`，10s 心跳落进竞态窗口就误弹「采集已停止」（约 1/6）。现在
        自本场 start 起算同一只帧时钟窗口（`max(2.0s, 3 × frame_interval)`）：窗口内没首帧
        → `None`（未知）；超窗仍无首帧 → `False`——「别把未知做成永不报」。`session_start_mono`
        缺席时如实报 `False`：让「采不到」不可能被忽略（design-intent 第 1 条）。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            return None
        if self.capture_backend is None:
            return None
        from meeting.state import frame_clock_window

        window = frame_clock_window(self.frame_interval)
        if self.last_frame_ts is None:
            if self.session_start_mono is None:
                return False
            if (time.monotonic() - self.session_start_mono) <= window:
                return None
            return False
        return (time.monotonic() - self.last_frame_ts) <= window

    def seconds_since_last_tick(self) -> float | None:
        """距最近一帧事实的秒数；**一帧都没收到过就是 `null`**。"""
        if self.last_frame_ts is None:
            return None
        return round(max(0.0, time.monotonic() - self.last_frame_ts), 3)

    def heartbeat_dict(self) -> dict[str, Any]:
        """`GET /api/heartbeat` 的应答（端口探测 + 界面判迟到 / 失联）。"""
        from meeting.service.app import SERVICE_ID

        return {
            "service": SERVICE_ID,
            "server_time": now_iso(),
            "revision": int(self.snapshot.revision),
            "top_state": self.snapshot.top_state,
            "recording_alive": self.recording_alive(),
            "seconds_since_last_tick": self.seconds_since_last_tick(),
            "capture_gaps": self.capture_gaps_view(),
        }

    # 帧空洞记录（gil-stall-hole）

    #: 「最近列表」的条数上限（有界：更早的只留在累计数里）
    CAPTURE_GAP_RECENT_LIMIT = 10

    def capture_gaps_view(self) -> dict[str, Any]:
        """心跳字段 `capture_gaps` 的产出（本场累计 + 有界最近列表）。

        - `total_count` / `total_seconds`：本场空洞的累计次数与总秒数（合并过的回合只算
          一次；超过最近列表上限的那几条只在这里留数）；
        - `recent`：最近至多 10 条，按发生先后排，每条 `{"at", "seconds"}` —— `at` 是
          空洞被记录那一刻的墙钟（ISO，解冻后第一拍 ≈ 空洞结束的时刻）。

        空洞是**历史事实**，与 `recording_alive`（「现在采不采得到」）语义互不覆盖：
        恢复之后 `recording_alive` 回 `true`，空洞照旧在记录里。任何态都给这个字段，
        没有空洞时是空记录（`{"total_count": 0, "total_seconds": 0.0, "recent": []}`），
        不给 `null`。
        """
        return {
            "total_count": int(self._capture_gap_totals["count"]),
            "total_seconds": round(float(self._capture_gap_totals["seconds"]), 3),
            "recent": [
                {"at": entry["at"], "seconds": round(float(entry["seconds"]), 3)}
                for entry in self._capture_gaps[-self.CAPTURE_GAP_RECENT_LIMIT:]
            ],
        }

    def _reset_capture_gaps(self) -> None:
        """本场起场清空记录（与 `last_frame_ts` 同点调；空洞是「本场」的记录）。"""
        self._capture_gaps = []
        self._capture_gap_totals = {"count": 0, "seconds": 0.0}

    def _apply_frame_gap(self, fact: FrameGap) -> None:
        """一条帧空洞事实落进记录：区间重叠的合并为一条（同一空洞回合只算一次）。

        检测在生产侧（`meeting/capture/gaps.py`），这里只管登记：
        - **区间**：`[detected - seconds, detected]`（发帧线程报的发现时刻与时长，
          均是采集侧单调钟口径 —— 与 `LevelFrame.ts` 同源，不拿墙钟做差）；
        - **合并**：新区间与记录里最后一条重叠（`interval_start <= last_end`）就是同一
          回合 —— 两路刻度线在同一次整进程冻结里各报一条、或解冻后紧邻的几拍，都在
          这里并成一条，累计秒数只加区间的**增量**；
        - **不提交**：记录只进心跳应答，不进快照 —— `revision` 原地不动、不广播，
          与 `last_frame_ts` 同一档（心跳域的记录不参与状态迁移）。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        seconds = max(0.0, float(fact.seconds))
        detected = float(fact.detected)
        interval_start = detected - seconds
        interval_end = detected
        last = self._capture_gaps[-1] if self._capture_gaps else None
        if last is not None and interval_start <= last["_end"]:
            # 同一空洞回合：并进最后一条（区间取并集、`at` 沿用先记下的那条）
            merged_start = min(float(last["_start"]), interval_start)
            merged_end = max(float(last["_end"]), interval_end)
            delta = max(0.0, (merged_end - merged_start) - float(last["seconds"]))
            last["_start"] = merged_start
            last["_end"] = merged_end
            last["seconds"] = merged_end - merged_start
            self._capture_gap_totals["seconds"] = float(self._capture_gap_totals["seconds"]) + delta
            _log.info(
                "帧空洞合并进上一条：现在 %.3fs（%s 报 %.3fs，同一回合）",
                float(last["seconds"]), fact.source, seconds,
            )
            return
        self._capture_gaps.append(
            {"at": now_iso(), "seconds": seconds, "_start": interval_start, "_end": interval_end}
        )
        # 列表本体也按「最近列表」截断（合并只需最后一条；累计数在 `_capture_gap_totals`，不设界）
        if len(self._capture_gaps) > self.CAPTURE_GAP_RECENT_LIMIT:
            del self._capture_gaps[: len(self._capture_gaps) - self.CAPTURE_GAP_RECENT_LIMIT]
        self._capture_gap_totals["count"] = int(self._capture_gap_totals["count"]) + 1
        self._capture_gap_totals["seconds"] = float(self._capture_gap_totals["seconds"]) + seconds
        from meeting.state import frame_clock_window

        _log.warning(
            "帧空洞：本场第 %d 次、%.3fs（窗口 %.3fs，%s 的刻度线在解冻后第一拍报出）",
            int(self._capture_gap_totals["count"]), seconds,
            frame_clock_window(self.frame_interval), fact.source,
        )

    # 事实分派

    def apply_fact(self, fact: Any) -> None:
        """**事实入循环后的唯一处理点**（后端线程经 `call_soon_threadsafe` 送到这里）。"""
        try:
            if isinstance(fact, LevelFrame):
                self._apply_level_frame(fact)
            elif isinstance(fact, FrameGap):
                self._apply_frame_gap(fact)
            elif isinstance(fact, SourceLost):
                self._apply_source_lost(fact)
            elif isinstance(fact, SourceFacts):
                self._apply_source_facts(fact)
            elif isinstance(fact, TranscriptLine):
                self._apply_transcript_line(fact)
            else:
                _log.warning("收到不认识的事实 %r（忽略，不影响状态机）", type(fact).__name__)
        except InvalidTransitionError as exc:
            # 状态机拒绝的事实：不涨 revision、不写盘、不广播（记一条 warning）
            _log.warning("事实被状态机拒绝（不影响 revision）：%s", exc)

    def _ensure_ticker(self) -> None:
        if self._ticker is not None or self._loop is None:
            return
        self._ticker = self._loop.create_task(self._levels_loop())

    def _ensure_transcriber_health_ticker(self) -> None:
        """起转写健康度节拍（幂等；没有事件循环时什么都不做）。

        **由 `start_session()` 的收尾那一步起**（`startup()` 里不起）。理由是这个节拍要盯的
        只有一件事 —— worker 停摆之后不再有行到来、快照得有人刷 —— 而那件事只在场次起来之后
        才谈得上。反向摆（`startup()` 里就起）会多出一条时序依赖：它的**第一拍**落在什么时刻
        取决于「用户多快按下开始」，空闲档 `stopped -> normal` 那一次迁移因此会落在这一场第一个
        提交的前后两种位置，`revision` 跟着差 1（复审 m3 的修法上曾实测到这一点）。
        场次起来之后这一跳已经由 `start_session()` 自己带走（那一刷就在 `commit()` 前面），
        节拍随后只管「期间有没有迁移」。
        """
        if self._transcriber_ticker is not None or self._loop is None:
            return
        self._transcriber_ticker = self._loop.create_task(self._transcriber_health_loop())

    async def _transcriber_health_loop(self) -> None:
        """转写健康度节拍：每 0.5 秒把 `transcript` 三格刷一遍，健康度迁移就 `commit()`。

        为什么要有这一条（`phase2/task.md` §7「一行到来时与 worker 停摆时」）：刷新原先只挂
        「一行到来」，而 worker 停摆恰恰不再有行到来——真「转写挂了」时快照会一直停在
        `normal`（`buffered_lines` 同理，停摆时积压只增不减）。起拍由 `start_session()` 起
        （空闲档初值与 `stopped -> normal` 那一跳由 `startup()` 的 ⑤b 与 `start_session()`
        自己带走，复审 m3）。与水位节拍分开：那一条只在录音态跑，转写在不在与状态无关。
        拍子的副作用只有快照：`commit()` 只发生在 `health` 真迁移或提示级键集合变化那一刻
        （契约第 3.3 节），三条用例钉住（读快照纯读 / 停摆刷上快照 / 积压量不投票）。
        """
        try:
            while True:
                await asyncio.sleep(TRANSCRIBER_HEALTH_INTERVAL_SECONDS)
                try:
                    if self._refresh_transcript_snapshot():
                        self.commit()
                except Exception:  # noqa: BLE001 - 一拍刷不动不许把节拍打死
                    _log.warning("转写健康度节拍这一拍刷不动（继续下一拍）", exc_info=True)
        except asyncio.CancelledError:  # pragma: no cover - 关停时取消
            raise

    async def _levels_loop(self) -> None:
        """`levels` 抽帧：**4 次/秒**（降采样落在 runtime 侧，hub 只搬运与背压）。"""
        try:
            while True:
                await asyncio.sleep(LEVELS_INTERVAL_SECONDS)
                if self.snapshot.top_state in ("recording", "alert"):
                    self.hub.broadcast_levels(
                        {
                            "revision": int(self.snapshot.revision),
                            "frames": sample_levels(self.snapshot),
                        }
                    )
        except asyncio.CancelledError:  # pragma: no cover - 关停时取消
            raise

    def _apply_level_frame(self, frame: LevelFrame) -> None:
        """电平帧：更新瞬时值 / 峰值保持 / 包络，刷新帧时钟，交给 `alerts.py` 判定。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        self._ensure_ticker()
        item = self.snapshot.source(frame.source)
        item.push_level(float(frame.ts), float(frame.dbfs), float(frame.peak))
        self.last_frame_ts = time.monotonic()
        self.last_fact_at = self.last_frame_ts

        # 用户主动关掉的那一路（`health = stopped`）：静音电平帧不进静默判定——关掉是
        # 机主的主动选择，不是「这一路采不到」（设计意图第 17、9 条）。切断必须在帧进
        # 判定器之前完成；包络与瞬时读数照旧更新，帧时钟照旧刷新（写静音的管线活着）。
        if item.health == "stopped":
            return
        changed = False
        judgement = self.judge.on_level_frame(frame.source, float(frame.ts), float(frame.dbfs))
        if judgement.alert is not None:
            self._enter_alert(frame.source, judgement.alert, float(frame.ts))
            changed = True
        if judgement.recovered and self.snapshot.source(frame.source).health == "lost":
            # 该路连续有信号达 `alert.recover_seconds`
            if recover_source(self.snapshot, source=frame.source):
                _log.info("状态迁移：alert -> recording（%s 连续有信号达恢复阈值）", frame.source)
            else:
                _log.info(
                    "一路恢复：%s 回到 restored（还有别的路在告警，top_state 保持 alert）",
                    frame.source,
                )
            self._log_health_migrations(
                source=frame.source,
                reason="连续有信号达恢复阈值",
                threshold=(
                    "恢复阈值 alert.recover_seconds"
                    f"={self.judge.thresholds.recover_seconds}s"
                ),
            )
            self.judge.mark_recovered(frame.source)
            changed = True
        if changed:
            self.commit()

    def _enter_alert(self, source: str, alert: Alert, ts: float) -> bool:
        """把一条致命级判定落到快照上（迁移 / 更新都由 `begin_alert()` 一处做）。

        返回 `True` 表示**顶层真的发生了迁移**（调用方随后调一次 `commit()`）。
        """
        item = self.snapshot.source(source)
        if item.health == "lost" and self.snapshot.top_state == "alert":
            # 同一路还在告警里：只刷新那一条（同时只留一条致命级）
            self.snapshot.alerts.critical = alert
            return False
        migrated = begin_alert(self.snapshot, source=source, alert=alert, lost_since=now_iso())
        _log.warning(
            "告警起：%s（%s），阈值 silence_seconds=%ss / silence_dbfs=%s dBFS，"
            "muted=%s（出声开关 alarm.sound=%s）",
            alert.message, source, self.judge.thresholds.silence_seconds,
            self.judge.thresholds.silence_dbfs, alert.muted, self._value("alarm.sound"),
        )
        if migrated:
            _log.info("状态迁移：recording -> alert（%s）", source)
        self._log_health_migrations(
            source=source,
            reason=f"这一路起致命级告警：{alert.message}",
            threshold=(
                "静默阈值 alert.silence_seconds"
                f"={self.judge.thresholds.silence_seconds}s / silence_dbfs"
                f"={self.judge.thresholds.silence_dbfs} dBFS"
            ),
        )
        return True

    def _apply_source_lost(self, fact: SourceLost) -> None:
        """后端报「这一路的流中断了」：立刻起致命级（**服务绝不因此结束会话**）。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        if self.snapshot.source(fact.source).health == "stopped":
            # 用户已关的那一路：这条流中断是「用户关流」在途的余波（关掉前后那条线程
            # 退出时报的），不是故障——不进告警、也不把已关路的 health 写回 lost
            # （旧行为会把徽标拉回「信号丢失」）。真死的那一路（没被用户关过）不经过
            # 这一格，既有的整套致命级判定原样。
            _log.info(
                "用户已关的 %s 这一路来了一条流中断事实：按「关流余波」处理，不进告警",
                fact.source,
            )
            return
        self._ensure_ticker()
        now = time.monotonic()
        alert = self.judge.on_source_lost(fact.source, now, fact.reason)
        self._enter_alert(fact.source, alert, now)
        self.commit()

    def _apply_source_facts(self, fact: SourceFacts) -> None:
        """流存活事实：**只是事实输入，不刷新帧时钟**（`recording_alive` 由帧时钟算）。"""
        if self.snapshot.top_state not in ("recording", "alert"):
            return
        self._ensure_ticker()
        _log.debug("流存活事实：%s stream_alive=%s", fact.source, fact.stream_alive)

    def _apply_transcript_line(self, line: Any) -> None:
        """一行转写：进 `transcript` 字段并推 `transcript` 通道（不参与状态迁移）。

        三格（`9e5db89c` phase2 §7）在一行到来时就地刷新（判定权收在
        `_refresh_transcript_snapshot()` 一处）；worker 停摆没有行可等，另有健康度节拍
        与 `startup()` ⑤b 两条不依赖行的刷新路径。读路径不刷（纯读）。提交资格只有
        `health` 一格（契约第 3.3 节：`transcript` 通道不参与状态迁移）——一行到来、
        积压量变、失败计数加一都不给 `revision` 添抖动；顺风路径上每行只广播
        `transcript` 通道，积压量与失败计数照旧写进快照。
        """
        try:
            text = getattr(line, "text", "")
            self.snapshot.transcript.last_line_at = now_iso()
            if str(text).startswith("[[识别失败"):
                # 占位符是失败档的唯一形状（`docs/v1-lineage.md` 的 B12）：
                # 计数只在这里 +1，与后端报的失败原因同源（同一份文本）
                self.snapshot.transcript.failed_segments += 1
            changed = self._refresh_transcript_snapshot()
            self.hub.broadcast_transcript(
                {
                    "revision": int(self.snapshot.revision),
                    "line": {
                        "ts": float(getattr(line, "ts", 0.0)),
                        "source": str(getattr(line, "source", "")),
                        "text": text,
                        "kind": str(getattr(line, "kind", "line")),
                    },
                }
            )
            if changed:
                self.commit()
        except Exception:
            _log.exception("处理一行转写时抛错（不阻塞录音、不影响状态机）")

    def _refresh_transcript_snapshot(self) -> bool:
        """把 `transcript` 的三个字段刷成当下的取值；返回「状态有没有真的迁移」。

        返回值描述两件会给 `revision` 投票的事：`health` 的迁移（调用方据此 `commit()`）与
        `alerts.hints` 的键集合变化（提示条的出现 / 消失要借 `state` 通道可见）。
        `buffered_lines` 与 `failed_segments` 照样写进快照但不投票——「积压 5 段变 4 段」
        不是状态迁移；`duration_seconds` 的刷新同理（0.5 秒节拍每拍都发生）。

        四个调用点（`phase2/task.md` §7「一行到来时与 worker 停摆时」）：`startup()` ⑤b
        取初值（否则空闲态要等节拍第一拍才翻成 `stopped`，带时序依赖，复审 m3）；
        `start_session()`（`stopped -> normal` 那一跳跟着本场自己的提交走）；
        `_apply_transcript_line()`（顺风路径的迁移不等下一拍）；`_transcriber_health_loop()`
        （worker 停摆之后没有行到来）。不挂读路径：刷新会经 `commit()` 提交，让 `GET
        /api/state` 改状态会把「读一次涨 revision」变成契约假命题。

        本函数是全仓唯一的转写刷新落点（启动、开场、每行、节拍四处都经过它），提示级接线
        只在这里做一次（每次都调判定器，提示条的 `duration_seconds` 要一直刷新；只在
        `recording` / `alert` 态发，见 `_sync_transcript_hints()`）。后端方法缺席或抛错：
        `health` 按 `normal`、`backlog` 按 0 并各留 warning——不许把状态机带崩。

        健康度走转写域自己的校验器（`meeting.asr.pipeline.validate_transcript_health`，
        三取值）：采集域的 `validate_health()` 认探针七取值，拿它校转写域会让快照长出前端
        schema 不认的第四个值。两个域各自冻结。
        """
        backend = self.transcriber_backend
        health = "normal"
        backlog = 0
        if backend is not None:
            try:
                health = asr_pipeline.validate_transcript_health(backend.health())
            except Exception:
                _log.warning("转写后端 health() 读不出来（按 normal 落快照）", exc_info=True)
                health = "normal"
            try:
                backlog = max(0, int(backend.backlog_depth()))
            except Exception:
                _log.warning("转写后端 backlog_depth() 读不出来（按 0 落快照）", exc_info=True)
                backlog = 0

        transcript = self.snapshot.transcript
        changed = False
        if transcript.health != health:
            _log.info(
                "转写健康迁移：%s -> %s（积压 %s 段，门槛 %s 段，单段超时 %ss）",
                transcript.health, health, backlog,
                getattr(backend, "behind_threshold", None), self._transcriber_timeout(backend),
            )
            transcript.health = health
            changed = True
        # 积压量只写进快照，不参与状态迁移、不涨 `revision`（契约第 3.3 节）：
        # 它几乎每行都在变，让它触发 commit 等于给 state 通道加高频抖动。
        if transcript.buffered_lines != backlog:
            transcript.buffered_lines = backlog
        if self._sync_transcript_hints(health=health, backlog=backlog):
            changed = True
        return changed

    def _clear_hints(self) -> None:
        """把提示级清成空——快照那一格与判定器那份登记一起清（唯一入口）。

        三样同进同退（判定器条目与记时表、快照 `alerts.hints`、键集合登记 `_hint_keys`）：
        只清一两样会让「快照与登记是同一份真相」有一刻不成立，旧登记还会让下一场误判（复审 m3）。
        不改致命级与两条电平计数。
        """
        self.judge.clear_hints()
        self.snapshot.alerts.hints = []
        self._hint_keys = ()

    def _sync_transcript_hints(self, *, health: str, backlog: int) -> bool:
        """把已经算好的 `health` 与积压段数交给判定器，再把它算出的提示级摆进快照。

        返回「提示级的键集合有没有变」，调用方据此决定要不要 `commit()`；同一键集合里
        `duration_seconds` 的刷新不算变化。离开 `recording` / `alert` 之后提示级恒为空
        （`docs/ui-design.md` 章 2.5：空闲态与结束档的告警区不出现）——空闲态那个
        `health == "stopped"` 报的是「真转写后端还没起过场次」，与录音中「转写停了」是两种
        情形，无条件交给判定器等于把它升成一条用户可见的告警条。每次都调判定器不挑时机：
        提示条要在窗口内一直刷新 `duration_seconds`（判定器的 `_merge_hint()` 本就幂等）。
        `health` / `backlog` 用调用方刚读过的同一份读数，不在这里再问一次后端。
        提示级来源与阈值见判定器（`docs/state.md` 第 6 章）。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            if not self.snapshot.alerts.hints and not self._hint_keys:
                return False
            _log.info(
                "离开录音 / 告警态（top_state=%s）：提示级清空（原 %s 条）",
                self.snapshot.top_state,
                len(self.snapshot.alerts.hints),
            )
            self._clear_hints()
            return True
        self.judge.on_transcript_health(health)
        self.judge.on_backlog_depth(backlog)
        hints = self.judge.hints()
        keys = tuple((item.level, str(item.source), str(item.message)) for item in hints)
        if keys == self._hint_keys:
            return False
        _log.info(
            "提示级键集合变化：%s -> %s（快照 %s 条）",
            len(self._hint_keys),
            len(keys),
            len(hints),
        )
        self._hint_keys = keys
        self.snapshot.alerts.hints = hints
        return True

    def _transcriber_timeout(self, backend: Any) -> float:
        """转写后端的 `segment_timeout()`（读不出来按缺省；只给日志用）。"""
        try:
            return float(backend.segment_timeout())
        except Exception:  # noqa: BLE001 - 读数失败不影响别的结论
            return float(DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS)

    # 场次生命周期

    def _session_state_on_disk(self) -> str:
        """盘上这一版该写哪个 `state`（映射只在 `session.state_for()` 一处）。"""
        failed = (
            self.snapshot.ended is not None
            and any(step.status == "failed" for step in self.snapshot.ended.steps)
        )
        return state_for(
            top_state=self.snapshot.top_state,
            ended_phase=None if self.snapshot.ended is None else self.snapshot.ended.phase,
            has_failed_step=failed,
        )
