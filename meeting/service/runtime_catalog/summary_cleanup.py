"""RuntimeCatalogMixin 的「会中总结清理」域分片：缓冲与本场记录表的清理。"""
from __future__ import annotations

import logging

_log = logging.getLogger("meeting.service.runtime")


class RuntimeCatalogSummaryCleanupMixin:
    def _clear_session_summary(self, *, reason: str) -> None:
        """把会中总结那两样**一起**清成空：内存缓冲（`summary_buffer`）与本场记录表
        （`summary_records`）。

        两样必须一起清 —— 只清缓冲会留下「记录里那几条还挂着、它们指的窗口已经取不回」的
        现场，只清记录又会把上一场的行留给这一场。
        **不动** `summary_client`（那是注入进来的协作者，与本场的数据无关）。
        """
        had_lines = len(self.summary_buffer)
        had_records = len(self.summary_records)
        self.summary_buffer.clear()
        self.summary_records.clear()
        if had_lines or had_records:
            _log.info(
                "会中总结的两笔内存已清空（%s）：转写缓冲 %s 行、本场记录 %s 条",
                reason, had_lines, had_records,
            )

    def _session_id_for_summary(self) -> str | None:
        """本场总结记录归属的场次目录名（没有场次时 `null`）。"""
        session = getattr(self.snapshot, "session", None)
        return None if session is None else str(session.id)
