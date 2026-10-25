"""会中总结 mixin：会中「按一下总结」那一次模型调用及其等待/记录（自 meeting/service/runtime.py 机械拆分，逐行照抄）。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from meeting.asr import pipeline as asr_pipeline
from meeting.service.runtime_shared import ActionError, _DaemonThreadPoolExecutor
from meeting.state import now_iso
from meeting.summary import (
    WINDOW_CHOICES,
    ModelClient,
    SummaryOutcome,
    SummarySettings,
    run_summary,
    sections_for,
)

_log = logging.getLogger("meeting.service.runtime")

#: 会中总结那一次模型调用专用的工作线程条数（`1987cb20` phase2 的复审裁定）。
#: 那次模型调用是一条同步的 `urllib.request`，跑在事件循环上会把 `/api/state`、三个 SSE
#: 通道、心跳与健康节拍一起按住（复审实测：假客户端 sleep 3 秒，另一条 `GET /api/state`
#: 等了 2.70 秒）——设计章 4.5 的「其余照常」要避免的正是这个。取 2 而不是 1：
#: deadline 到点时 `_summary_wait()` 放弃的是等待、已在飞的那次照跑到底，弃等之后紧接着
#: 按下的第二次总结需要另有一条线程可用，否则自己的预算全白等在队列里。

SUMMARY_EXECUTOR_MAX_WORKERS = 2


class _AttemptsSeen:
    """把总结客户端包一层，**只记一件事**：`complete()` 被调了几次。

    会中总结的尝试次数由内核 `run_summary()` 逐个 attempt 累加，正常路径上那两格
    （`elapsed_seconds` / `attempts`）随 `SummaryOutcome.detail` 一起给出来。走到
    「墙钟预算到点、等待被放弃」那一档时内核还没返回，它的登记读不到 —— 契约那一档要的
    仍是 `{elapsed_seconds, attempts}`，于是**在交给内核的客户端上**记一份。

    读数为什么可信：内核每一次 attempt 都先经 `complete()` 才可能卡住（`runner.py` 的循环
    第一件事就是这次调用），因此到点时这里记下的就是**已经发出去的那几次**。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.attempts = 0

    def complete(self, messages: Any, *, timeout_seconds: float) -> str:
        self.attempts += 1
        return self._client.complete(messages, timeout_seconds=timeout_seconds)


class RuntimeSummaryMixin:
    """会中总结 mixin。"""


    def summaries_dict(self) -> dict[str, Any]:
        """`GET /api/summaries` 的应答体：本场按过的、**成功产出的**那几次（按时间先后）。

        没有场次或一次都没按过时 `session_id` 为 `null`、`entries` 是空数组。每一格的形状由
        `phase2/task.md` 的接口口径写死（`window` / `window_label` / `at` / `elapsed_seconds` /
        `what` / `where` / `lost` —— **没有 `lines` 与 `bullets`**）。
        """
        return {
            "session_id": self._session_id_for_summary(),
            "entries": [dict(entry) for entry in self.summary_records],
        }

    def _summary_executor_or_create(self) -> _DaemonThreadPoolExecutor:
        """会中总结那台执行器（**惰性创建**：不按下总结的进程一个线程都不起）。"""
        if self._summary_executor is None:
            self._summary_executor = _DaemonThreadPoolExecutor(
                max_workers=SUMMARY_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="summary-call",
            )
        return self._summary_executor

    async def _summary_wait(
        self,
        *,
        window: str,
        settings: SummarySettings,
        client: Any,
        started_at: str | None,
    ) -> SummaryOutcome:
        """把一次**同步**的会中总结调用丢进工作线程，连同 `asyncio.wait_for` 一起等。

        形状照收尾那套先例（`_drain_wait()` + `_DaemonThreadPoolExecutor`）：

          - `run_summary(...)` 经 `loop.run_in_executor(self._summary_executor_or_create(), …)`
            提交，返回的线程 Future 由 `asyncio.wrap_future()` 包成可等待对象 —— 事件循环在
            这一段只负责等，`/api/state`、三个 SSE 通道、心跳与健康节拍照旧转；
          - 等待上限 = `settings.deadline_seconds`（配置里那条 `summary.deadline_seconds`，
            默认 25 秒）。它与内核 `run_summary()` 现算剩余量用的是同一条线，**一秒都不多给**；
          - 到点时放弃的是**等待**：已经交给工作线程的那次调用照跑到底（与 `_drain_wait()` 的
            `cancel()` 口径同一句），因此紧接着按下的第二次总结由池里另一条线程接住
            （`SUMMARY_EXECUTOR_MAX_WORKERS` 取 2 的来路）。

        `client` 外面包一层 `_AttemptsSeen` 登记：走到超时这一档时内核的
        `SummaryOutcome.detail` 已经拿不到了，`attempts` 只能从这里读（见那个类的说明）。

        到点的报法与内核判「预算用尽」那一档同形：`ActionError("summary_failed", …, 502,
        detail={"elapsed_seconds", "attempts"})`。`summarize()` 那个 try 里有一条
        `except ActionError: raise` 专门放它过去（否则会被「动手之后出的意外」那一条重新包成
        `{error: ...}`，`detail` 的形状就偏了）。
        """
        loop = asyncio.get_running_loop()
        seen = _AttemptsSeen(client)
        lines = self.summary_buffer.lines()
        pending = asyncio.wrap_future(
            loop.run_in_executor(
                self._summary_executor_or_create(),
                lambda: run_summary(
                    lines,
                    window=window,
                    settings=settings,
                    client=seen,
                    started_at=started_at,
                ),
            )
        )
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                pending, timeout=max(0.0, float(settings.deadline_seconds))
            )
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            attempts = max(1, seen.attempts)
            _log.warning(
                "会中总结等满预算（%ss）还没结果：按 summary_failed 如实上报"
                "（实测等了 %ss、试了 %s 次）",
                settings.deadline_seconds, round(elapsed, 3), attempts,
            )
            raise ActionError(
                "summary_failed",
                f"等了 {elapsed:.1f} 秒（预算 {settings.deadline_seconds:g} 秒）还没有结果，"
                f"试了 {attempts} 次",
                status=502,
                detail={"elapsed_seconds": round(elapsed, 3), "attempts": attempts},
            ) from exc

    async def summarize(self, args: dict[str, Any]) -> dict[str, Any]:
        """`summarize`：按一下，把最近这段时间的实时转写读一遍，给回三小段。

        **判定的先后是固定的**（照 `start` 的先例，`phase2/task.md` 的接口口径）：

          1. 判态 —— 没有在录的场次（`idle` / `ended`）→ `not_recording`(409)；
          2. 校验 `window` —— 取值不在四档里 → `invalid_setting`(400)（**不静默落缺省档**：
             动作层的非法取值是一句话就说清的事，悄悄替人挑一档会让人以为按对了）；
          3. 看转写健康 —— `stopped`（转写停了 / 没起过）→ `transcript_unavailable`(503)；
          4. 看模型端点配置 —— 端点三件套缺一个 → `summary_failed`(502)，`detail.missing`
             点名缺哪几个键；
          5. **才动手**。

        **前四档没过，一个请求都不发出去、一条记录都不写**（判据是用例里那个登记的假客户端
        读数为 0）。第 3 / 4 档的读数在读到的当口就判掉：判定与动手之间不再把后端或配置问第二遍。

        **这是一条协程，而且「动手」那一段经 `_summary_wait()` 丢进工作线程**
        （复审 minor 6 的直接修复）：`run_summary()` 是一次**同步**调用（真客户端是
        `urllib.request`），放在事件循环上跑就把 `/api/state`、三个 SSE 通道、心跳与健康节拍
        一起按住 —— 真端点预算默认 25 秒就是按住 25 秒，而设计章 4.5 对 `summary-running`
        那一档写的正是「总结区 `正在总结刚才的 15 分钟…`；**其余照常**」。
        挪出循环之后，总结期间别处的请求照常应答（判据见
        `tests/test_summary_action.py` 里那条量往返耗时的用例）。

        **预算语义一个字不改**：那一次调用的等待上限就是配置里那条
        `summary.deadline_seconds`（默认 25 秒），与内核 `run_summary()` 现算剩余量时用的
        是同一条线；到点时报的还是 `{elapsed_seconds, attempts}` 那一档（契约 4.7 与
        `docs/contract.md` 章 4 的 `summary_failed` 表）。

        **总结是只读的**：成功与失败两条路都不调 `commit()`、不涨 `revision`、不广播 ——
        快照里一个字都不变（结果走这个应答体，记录走 `GET /api/summaries`）。

        返回值就是应答体里那个 `summary` 子对象（`routes.py` 把它挂在通用信封上）。
        """
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "现在没有在录的场次，总结动作无效", status=409)

        window = args.get("window")
        choice = str(window).strip() if window is not None else None
        if choice is not None and choice not in WINDOW_CHOICES:
            raise ActionError(
                "invalid_setting",
                f"window 取值非法：{window!r}（只能是 {' / '.join(WINDOW_CHOICES)}）",
                status=400,
                detail={"key": "window", "value": window, "legal": list(WINDOW_CHOICES)},
            )

        settings = SummarySettings.from_config(self._config_view())

        # 转写健康度：**这一档的读数只读一次**（判定与动手之间不许再问一遍后端）
        health = None
        if self.transcriber_backend is not None:
            try:
                health = asr_pipeline.validate_transcript_health(self.transcriber_backend.health())
            except Exception:
                _log.warning("会中总结：转写后端 health() 读不出来", exc_info=True)
                health = None
        if health in (None, "stopped"):
            raise ActionError(
                "transcript_unavailable",
                "转写没在跑（没起过，或者已经停了）：会中总结吃的是实时转写，现在没有可用的那一路",
                status=503,
                detail={"health": "stopped"},
            )

        missing = tuple(settings.missing)
        if missing:
            # 预设模式（缺的是 `summary.presets_json[名字].格`）与平铺模式各说各的指引：
            # 让人去补的那份配置，与报出来的名字对得上。
            if any(name.startswith("summary.presets_json[") for name in missing):
                hint = "（把这套预设缺的那几格补上，或者先在设置里切到别的一套再来一次）"
            else:
                hint = "（配好 summary.base_url / summary.api_key / summary.model 之后再来一次）"
            raise ActionError(
                "summary_failed",
                f"模型端点没配齐，缺这些配置键：{'、'.join(missing)}{hint}",
                status=502,
                detail={"missing": list(missing)},
            )

        session = getattr(self.snapshot, "session", None)
        started_at = None if session is None else str(session.started_at)
        try:
            client = self.summary_client
            if client is None:
                client = ModelClient.from_settings(settings)
            outcome = await self._summary_wait(
                window=choice if choice is not None else settings.default_window,
                settings=settings,
                client=client,
                started_at=started_at,
            )
        except ActionError:
            # 已按契约分好档的那一条（`_summary_wait()` 判到预算到点）：原样上抛，
            # `detail` 就是契约里那两格，不许在这里被重新包成 {error: ...}
            raise
        except Exception as exc:
            # 「才动手」之后出的意外同样按这一档报（配置与态都已经过了，这不是参数错）
            _log.exception("会中总结在「动手」之后出的意外（如实回 summary_failed）")
            raise ActionError(
                "summary_failed",
                f"会中总结失败：{type(exc).__name__}: {exc}",
                status=502,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        if not outcome.ok:
            raise ActionError(
                "summary_failed",
                outcome.error or "会中总结没拿到结果",
                status=502,
                detail=dict(outcome.detail),
            )

        entry = self._summary_result(outcome)
        _log.info(
            "会中总结完成：window=%s（%s）lines=%s attempts=%s elapsed=%ss",
            entry["window"], entry["window_label"], outcome.lines,
            int(outcome.attempts), entry["elapsed_seconds"],
        )
        # **只记成功产出的那几次**（失败没有「那几条」可记）；记录活在内存里，不落文件。
        # 记录这一条按 `GET /api/summaries` 写死的形状来（`phase2/task.md` 那一份没有 `lines`），
        # 与上面交给应答体的那一份是**两个形状**：应答体多一格窗口行数。
        self.summary_records.append(self._summary_entry(outcome))
        return entry

    @staticmethod
    def _summary_result(outcome: SummaryOutcome) -> dict[str, Any]:
        """`SummaryOutcome` → 应答体里那个 `summary` 子对象（逐键照接口口径）。

        形状写死（`phase2/task.md` 的成功体）：`window` / `window_label` / `at` /
        `elapsed_seconds` / `lines` / `what` / `where` / `lost`。**`ok` / `bullets` / `attempts` /
        `detail` 不进应答体** —— 失败那几档走失败信封的 `code` + `detail`。`at` 由这里现取
        （`now_iso()`，与快照里那些时刻同一个格式），此后不再变。
        """
        return {
            "window": str(outcome.window),
            "window_label": str(outcome.window_label),
            "at": now_iso(),
            "elapsed_seconds": float(outcome.elapsed_seconds),
            "lines": int(outcome.lines),
            **sections_for(outcome),
        }

    @classmethod
    def _summary_entry(cls, outcome: SummaryOutcome) -> dict[str, Any]:
        """`SummaryOutcome` → 记录表里那一条（`GET /api/summaries` 写死的形状）。

        与 `_summary_result()` 同一份来源、少一格 `lines`（那一份是 `GET` 的口径里没有的）。
        """
        result = cls._summary_result(outcome)
        result.pop("lines")
        return result
