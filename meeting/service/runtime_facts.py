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

#: 转写健康度节拍的间隔（秒）。
#:
#: **取值依据**：这一条管的是「转写挂了这件事多久能被看见」。取值只影响**滞后**，不影响开销 ——
#: 每一拍读的是 `health()` + `backlog_depth()` 两个**纯读**（几个标志位 + 一个队列长度），
#: 读数没变时一个副作用都不发生（不写快照、不提交、不落 log）。
#:
#: 取 0.5 秒的理由：界面里转写健康位（`transcript.health`）与失联横幅本来就按秒级刷新
#: （`heartbeat.interval_seconds` 默认 10 秒、可配 1–60 秒），半秒的滞后看不出差别；
#: 再往上加（1 秒以上）只会让「转写停了」这件事在 `/debug` 与界面上多等一拍，
#: 换不来任何好处。**用例走的就是这条节拍**（不另开接缝），因此整文件耗时也由它限住。
TRANSCRIBER_HEALTH_INTERVAL_SECONDS = 0.5


class RuntimeFactsMixin:
    """事实摄取 mixin：心跳与帧时钟、电平告警接线、转写刷新与提示级、帧空洞账。"""

    # -- 心跳 -------------------------------------------------------------

    def recording_alive(self) -> bool | None:
        """`recording_alive`：`recording` / `alert` 态按帧时钟算；其余态为 `null`。

        「还没有第一帧」单独一档（de899128，开场误报的修法）：首帧正常要比 `start`
        晚约 0.5s 才来，旧口径把这一段直接算成 `False`，界面的 10s 心跳落进这段
        竞态窗口就误弹「采集已停止」（约 1/6）。现在自**本场 start** 起算同一只
        帧时钟窗口（`max(2.0s, 3 × frame_interval)`，公式不新造）：

        - 窗口内还没收到首帧 → `None`（未知）；
        - 超窗仍无首帧 → `False`（=停）——「别把未知做成永不报」，真饿死仍报得出。

        `session_start_mono` 缺席（理论上只在 start 之前的瞬间）按无法确知处理，
        如实报 `False`：界面的第一职责是让「采不到」不可能被忽略（design-intent 第 1 条）。
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

    # -- 帧空洞账（gil-stall-hole） ---------------------------------------

    #: 「最近列表」的条数上限（有界：更早的只留在累计数里）
    CAPTURE_GAP_RECENT_LIMIT = 10

    def capture_gaps_view(self) -> dict[str, Any]:
        """心跳字段 `capture_gaps` 的产出（本场累计 + 有界最近列表）。

        - `total_count` / `total_seconds`：本场空洞的累计次数与总秒数（合并过的回合只算
          一次；超过最近列表上限的那几条只在这里留数）；
        - `recent`：最近至多 10 条，按发生先后排，每条 `{"at", "seconds"}` —— `at` 是
          空洞被记录那一刻的墙钟（ISO，解冻后第一拍 ≈ 空洞结束的时刻）。

        空洞是**历史事实**，与 `recording_alive`（「现在采不采得到」）语义互不覆盖：
        恢复之后 `recording_alive` 回 `true`，空洞照旧在账上。任何态都给这个字段，
        没有空洞时是零账（`{"total_count": 0, "total_seconds": 0.0, "recent": []}`），
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
        """本场起场清账（与 `last_frame_ts` 同点调；空洞是「本场」的账）。"""
        self._capture_gaps = []
        self._capture_gap_totals = {"count": 0, "seconds": 0.0}

    def _apply_frame_gap(self, fact: FrameGap) -> None:
        """一条帧空洞事实落账：区间重叠的合并为一条（同一空洞回合只算一次）。

        检测在生产侧（`meeting/capture/gaps.py`），这里只管记账：
        - **区间**：`[detected - seconds, detected]`（发帧线程报的发现时刻与时长，
          均是采集侧单调钟口径 —— 与 `LevelFrame.ts` 同源，不拿墙钟做差）；
        - **合并**：新区间与账上最后一条重叠（`interval_start <= last_end`）就是同一
          回合 —— 两路刻度线在同一次整进程冻结里各报一条、或解冻后紧邻的几拍，都在
          这里并成一条，累计秒数只加区间的**增量**；
        - **不提交**：账只进心跳应答，不进快照 —— `revision` 原地不动、不广播，
          与 `last_frame_ts` 同一档（心跳域的账不参与状态迁移）。
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

    # -- 事实分派 ---------------------------------------------------------

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
        """转写健康度节拍：**每 `TRANSCRIBER_HEALTH_INTERVAL_SECONDS` 秒**把 `transcript`
        的三格刷一遍（`_refresh_transcript_snapshot()`），健康度迁移就 `commit()`。

        **为什么要有这一条**（`phase2/task.md` §7：「一行到来时**与 worker 停摆时**把它们写对」）：
        刷新原先只挂在「一行到来」上，而 **worker 停摆恰恰不会再有行到来** —— 没有行的原因
        就是它停了。于是真出现「转写挂了」时，快照里的 `health` 会一直停在 `normal`
        （界面 / `/debug` 都看这一格），直到有人重启或下一场开局；`buffered_lines` 同理
        （停摆时积压只增不减，正是最该看见的时候）。
        （**起拍时刻**：这条节拍由 `start_session()` 起，第一拍落在那一场开始之后半秒 ——
        空闲档的初值与 `stopped -> normal` 那一跳都不归它管，分别由 `startup()` 的 ⑤b 与
        `start_session()` 自己带走，见那两处。复审 m3。）

        **与水位那一档（`_levels_loop`）分开**：那一条只在 `recording` / `alert` 态跑，
        而「转写起没起来、还在不在」这件事与会话状态无关（idle 时也要如实报）。

        **拍子的副作用只有快照**：`_refresh_transcript_snapshot()` 只在 `health` 真迁移、
        或 `alerts.hints` 的键集合变化时返回 `True`，于是 `commit()`（`revision +1` + 一次
        `state` 广播）只可能发生在状态真的变了那一刻；积压量变化、以及持续提示条的
        `duration_seconds` 刷新照旧只写快照，`revision` 原地不动（契约第 3.3 节）。
        这一条由三条用例钉住：`test_snapshot_reads_do_not_refresh_and_do_not_push_the_revision`
        （读快照是纯读）、`test_worker_stall_reaches_the_snapshot_without_any_new_line`
        （本拍子真的把停摆刷上去了）与 `test_backlog_changes_reach_the_snapshot_without_touching_the_revision`
        （积压量不投票）。
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
        """一行转写：进 `transcript` 字段并推 `transcript` 通道（**不参与状态迁移**）。

        **本单元补上的三格**（`9e5db89c` phase2 §7）：`transcript.health` /
        `buffered_lines` / `failed_segments` 在此之前生产代码里一处都没写过 ——
        一行到来时就地刷新一遍（判定权全部收在 `_refresh_transcript_snapshot()` 一处）。
        **worker 停摆那一档没有行可等**，所以另有一条不依赖行的刷新路径：健康度节拍
        `_transcriber_health_loop()`（`startup()` 的 ⑤b 再取一次初值）。
        **读路径不刷**（`_snapshot_json()` 是纯读，一个字都不问转写后端），理由见
        `_refresh_transcript_snapshot()` 的 docstring。
        三个字段的**提交资格只有 `health` 一格**：契约第 3.3 节写明 `transcript` 通道
        不参与状态迁移（`docs/contract.md:208`），所以「一行到来」「积压量变了」
        「失败计数多了一格」都不许给 `revision` 添抖动 ——
        `commit()` 在这里的唯一触发条件是 `health` 的**迁移**（`normal ↔ behind ↔ stopped`），
        那才是状态真的变了；顺风路径上的每一行都只广播 `transcript` 通道、
        `revision` 原地不动，而积压量与失败计数照旧写进快照（经 `/api/state` 读得到）。
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
        """把 `transcript` 的三个字段刷成**当下**的取值；返回「状态有没有真的迁移」。

        读的是转写后端自己的两个计数（`health()` / `backlog_depth()`）。
        返回值描述**两件会进快照 `revision` 的事**：
          - `health` 的**迁移**（`normal ↔ behind ↔ stopped`）—— 调用方据此 `commit()`
            （契约第 3.3 节：`transcript` 通道不参与状态迁移，一行到来 / 积压量变化都不该给
            `revision` 添抖动）；
          - `alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限挤掉，以及离开
            `recording` / `alert` 时整格清空）—— 界面要看得见提示条的出现与消失，
            因此也借既有 `state` 通道提交一次。
        `buffered_lines` 与 `failed_segments` 照样写进快照（`/api/state` 读得到），
        但它们**不给 `revision` 投票** —— 「积压 5 段变 4 段」不是一次状态迁移；同理，同一个
        键集合里 `duration_seconds` 的刷新也不投票（那正是 0.5 秒节拍每拍都会发生的事）。

        **四个调用点**（`phase2/task.md` §7：「一行到来时**与 worker 停摆时**」）：

          - `startup()` 的 ⑤b：**初值取自后端**一次 —— 不这样做的话快照初值就是
            dataclass 默认的 `normal`，空闲态要等健康度节拍第一拍（0.5 秒后）才翻成
            `stopped` 并多提交一次（复审 m3），空闲档于是带上一条时序依赖；
          - `start_session()`：转写后端刚起（`stopped -> normal`）时就地刷一遍，
            让这次迁移跟着那一场自己的 `commit()` 走，而不是等下一拍（否则整场里会有一段
            「转写在跑、快照还写着 `stopped`」的窗口）；
          - `_apply_transcript_line()`：一行到来时就地刷一遍（顺风路径上 `health` 的迁移
            立刻 `commit()`，不必等下一拍）；
          - `_transcriber_health_loop()`：**低频节拍**（`TRANSCRIBER_HEALTH_INTERVAL_SECONDS`）
            也刷一遍 —— worker 停摆之后不再有行到来，只靠上一个调用点的话这一档永远刷不到
            快照上，界面 / `/debug` 会一直看到 `health = normal`（而「转写挂了」正是最该看见的）。

        **为什么不挂在读路径上**：`to_dict()` / `_snapshot_json()` 是快照的产出者，
        而刷新会经 `commit()` 提交（`revision +1` + 一次 `state` 广播）—— 让 `GET /api/state`
        这种只读请求去改状态，一来把「读一次就涨 `revision`」变成契约假命题，
        二来 `GET /api/heartbeat` 与随后的 `GET /api/state` 会读到两个不同的 `revision`。
        节拍与读请求完全解耦，读路径因此保持纯读。

        两个调用点都只写快照、只由上面那两件事触发 `commit()`，因此拍多少下都不会给
        `revision` 添抖动（本函数在读数没变、提示级键集合没变时是纯读）。`startup()` 那一次
        丢掉返回值，于是它连「迁移」都不提交（那一刻还没有任何状态迁移可谈）；
        `start_session()` 那一次同样丢掉返回值，因为那一场自己的 `commit()` 就在后面几步。

        **提示级在这里接线**：算出 `health` 与 `backlog` 之后交给 `_sync_transcript_hints()`
        （它调判定器、再把 `alerts.hints` 摆进快照）。本函数是全仓唯一的转写刷新落点 ——
        启动、开场、每行、0.5 秒节拍四处都经过它 —— 所以接线只在这里做一次就够，
        四条路径都会带上提示级。**每次都调判定器**（不是只在 health 迁移那一刻）：
        提示条要在窗口内一直刷新 `duration_seconds`。
        **只有在 `recording` / `alert` 态才发提示级**：`top_state ∈ {idle, ended}` 时
        `_sync_transcript_hints()` 不把读数交给判定器、并把那一格清空（理由与依据见它的
        docstring：空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过场次」，
        与录音中「转写停了」是两件事）。

        两个后端方法都可能缺席（`null` 替身之外的第三方实现）或抛错：`health` 读不出来按
        `normal`、`backlog_depth` 读不出来按 `0`，并各留一条 warning —— 快照里宁可少一格
        数字，也不许因为转写侧的读数把状态机带崩。

        **健康度走的是转写域自己的校验器**（`meeting.asr.pipeline.validate_transcript_health`，
        三取值 `normal` / `behind` / `stopped`）：采集域那个 `validate_health()` 认的是探针
        七取值，越界落 `probe_failed` —— 拿它校转写域会让快照里长出前端 schema
        （`TRANSCRIPT_HEALTHS`）不认的第四个值。两个域各自冻结，校验器就各归各的。
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
        # **积压量只写进快照，不参与状态迁移、不涨 `revision`**（契约第 3.3 节：
        # `transcript` 通道不参与状态迁移；`phase2/task.md` §7：这两个计数只在 `/debug`
        # 与 log 里看着用）。积压量几乎每一行都在变，让它触发 `commit()` 等于给 `state`
        # 通道加一份高频抖动，并把契约给界面的那句「`id` 变了就是状态变了」变成假命题。
        # 写进快照之后照样经 `/api/state` 的读路径可见。
        if transcript.buffered_lines != backlog:
            transcript.buffered_lines = backlog
        if self._sync_transcript_hints(health=health, backlog=backlog):
            changed = True
        return changed

    def _clear_hints(self) -> None:
        """把提示级清成空 —— **快照那一格与判定器那份记账一起清**（唯一入口）。

        三样东西必须同进同退：判定器的条目与记时表（`judge.clear_hints()`）、快照的
        `alerts.hints`、本类那份键集合记账（`_hint_keys`）。只清其中一两样的话，
        「快照与记账是同一份真相」这条不变量会有一刻不成立，旧记账还会让下一场的键集合判据
        误判（复审 m3）。**不改**致命级与两条电平计数。
        """
        self.judge.clear_hints()
        self.snapshot.alerts.hints = []
        self._hint_keys = ()

    def _sync_transcript_hints(self, *, health: str, backlog: int) -> bool:
        """把**已经算好的** `health` 与积压段数交给判定器，再把它算出的提示级摆进快照。

        返回「提示级的**键集合**有没有变」（新增 / 移除 / 被上限挤掉都算变），调用方据此
        决定要不要 `commit()`；同一个键集合里 `duration_seconds` 的刷新不算变化。

        **离开 `recording` / `alert` 之后提示级恒为空**（`docs/ui-design.md` 章 2.5 那张表：
        空闲态与结束档的「告警区」不出现）。判定权随状态收口：`top_state ∈ {idle, ended}`
        时**不把读数交给判定器**，并把已经挂着的那几条连记账一起清掉（`_clear_hints()`，
        键集合因此算作变了一次、由调用点借既有 `state` 通道提交一次，界面看得见它消失）。

        **为什么要收这一道口**：空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过
        场次」（`docs/state.md` 第 4 章那两格 fixtures 的既有口径），与录音中「转写停了」属于
        两种情形；相位一接线时把算好的 health 无条件交给判定器，等于把前者升成了一条用户可见的
        告警条 —— 设计稿里空闲态压根没有告警区。这条提示因此只在
        `top_state ∈ {recording, alert}` 上成立，离开这两个态就归零、下一场 `start` 从零开始。

        **每次都调判定器，不挑时机**：提示条要在窗口内一直刷新 `duration_seconds`
        （「已经落后 12 秒」这句话得跟着走），只在 health 迁移那一刻调一次的话，条目会停在
        `duration_seconds = 0`。判定器那边 `_merge_hint()` 本来就幂等（同一个三元组沿用原来
        那一条、只刷时长），所以每拍都调一遍是它的正常用法。

        `health` 与 `backlog` 由调用方传进来（`_refresh_transcript_snapshot()` 刚读过的同一份
        读数），**不在这里再问一次后端** —— 读路径是纯读、快照刷新也不该把后端的读数翻倍。

        提示级的来源与阈值都在判定器里（`docs/state.md` 第 6 章）：健康度 `normal / behind /
        stopped`，积压 `≥ BACKLOG_HINT_THRESHOLD`，两类都定在 `source = transcript` 上、靠
        `message` 分辨，各自有一条对称的清除路径。
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

    # -- 场次生命周期 -----------------------------------------------------

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
