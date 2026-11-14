"""RuntimeCatalogMixin 的「稿子正文」域分片：GET /api/sessions/{id}/transcripts。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from meeting.service.runtime_shared import _disk_str_or_none


def _transcript_declared_sources(record: dict[str, Any] | None) -> tuple[str, ...]:
    """这一场**采了哪几路**（`session.json` 的 `mode` 判；读不到按两路全集）。

    缺稿话的判据只此一处：`mode` 是 `mic` / `loopback` 单路时，另一路的稿**不可能存在**
    （那一截音频从没录过），缺稿如实落 `unrecorded`；`mode` 是 `both`、或盘上读不出
    `mode`（残档、旧档）时按两路全集——那一档的缺稿是「文件不在」，如实落 `absent`。
    """
    mode = _disk_str_or_none(record, "mode")
    if mode == "mic":
        return ("mic",)
    if mode == "loopback":
        return ("loopback",)
    return ("mic", "loopback")


def _read_transcript_body(path: Path) -> str | None:
    """读一份稿的正文；**文件不在或读不动都交回 `None`**（调用方按缺稿答）。

    `utf-8` + `errors="replace"`（与 `meeting/minutes/chunk.py` 的
    `read_transcript_text()` 同一口径）：转写里混进半个坏字节时折成替换字符如实显示，
    不让一份稿把整只端点带走。目录读不动的那一档与「文件不在」同样按缺稿答 ——
    这一格服务的是界面上的一句话，不把端点带走（`has_offline_transcript()` 的先例）。
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


class RuntimeCatalogTranscriptsMixin:
    def session_transcripts_dict(self, target: Any) -> dict[str, Any]:
        """`GET /api/sessions/{id}/transcripts` 的应答体：**这一场的稿子正文**。

        两份稿（**平级**，谁也不叫"权威"）× 两路各一份，文件名是上游冻结的契约
        （`meeting/minutes/chunk.py` 同一套模板，按名字找、不猜）。

        每一路**只回答两件事之一**：文件在 → `text` 带正文原样（`utf-8` +
        `errors="replace"`，与纪要切块读稿同一口径——半个坏字节折成替换字符，
        不让一份稿把整只端点带走）；文件不在 → `reason` 带缺稿的那一档：
        `unrecorded`（这一场 `session.json` 的 `mode` 没采这一路——没有它的稿是天经地义）
        或 `absent`（采了这一路但盘上没有这一份：会中稿被清 / 会后稿还没生成过）。
        缺稿话由界面按 `reason` 翻（标签与句子归界面，服务不摆没有数的空格）。

        `{id}` 的寻址与失败口径跟 `session_status_dict()` 同一套（404 两档：
        目录不在原地 / 不在工作台清单里）。**纯读**：不提交、不涨 `revision`、不广播。
        """
        from meeting.minutes.chunk import (
            KIND_IDS,
            LIVE_TRANSCRIPT_TEMPLATE,
            OFFLINE_TRANSCRIPT_TEMPLATE,
            SOURCE_IDS,
        )

        directory = self._require_session_dir(
            target,
            unregistered_message=(
                "这一场不在工作台清单里（`GET /api/sessions` 读的就是那份注册表）："
                "先在界面上把它打开，再把会话 id 指过来"
            ),
        )
        record = self.store.read_session_json(directory)
        declared = _transcript_declared_sources(record)
        base = Path(directory)
        kinds: list[dict[str, Any]] = []
        for kind, template in (
            (KIND_IDS[0], LIVE_TRANSCRIPT_TEMPLATE),
            (KIND_IDS[1], OFFLINE_TRANSCRIPT_TEMPLATE),
        ):
            sources: list[dict[str, Any]] = []
            for source in SOURCE_IDS:
                path = base / template.format(source=source)
                cell: dict[str, Any] = {"source": str(source)}
                text = _read_transcript_body(path)
                if text is not None:
                    cell["text"] = text
                elif source not in declared:
                    cell["reason"] = "unrecorded"
                else:
                    cell["reason"] = "absent"
                sources.append(cell)
            kinds.append({"kind": str(kind), "sources": sources})
        return {"dir": str(directory), "kinds": kinds}
