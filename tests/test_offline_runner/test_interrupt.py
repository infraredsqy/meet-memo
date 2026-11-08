from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting.offline import artifacts
from meeting.offline.engine import OfflineSegment

from meeting.offline.runner import (
    CHUNK_DIR_NAME,
    REASON_ALL_CHUNKS_FAILED,
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_ENGINE_LOAD_ERROR,
    REASON_NO_AUDIO,
    REASON_NO_SEGMENTS,
    REASON_NOT_RUN,
    declared_sources,
    plan_chunks,
    read_chunk_seconds,
    resolve_audio,
    resolve_engine,
    run_session,
    write_chunk_wav,
)

from tests._offline_fakes import (
    CHUNK_SECONDS,
    SAMPLE_RATE,
    FakeConfig,
    FakeEngine,
    LoadingEngine,
    chunk_ranges,
    silence_session,
    sparse_pcm,
    synth_session,
)

from tests.test_offline_runner._common import (
    read_status,
)

#: 慢档标记（68927c27 收尾轮）：会后离线链的契约验收，归收尾跑。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 打断（b1c6a58e）：协作式块边界停止 + interrupted 终态 + 缺口注记
# ---------------------------------------------------------------------------


class StopAfterFirstEngine:
    """假引擎：第 1 块照常跑，跑完把取消旗标立起来（第 2 块开跑前应命中检查点）。"""

    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel
        self.calls: list[str] = []

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        self.cancel.set()
        return [OfflineSegment(0.0, 1.0, "S01", f"{Path(audio_path).name} 的一句")]


def test_run_session_stops_at_the_next_chunk_boundary_when_the_cancel_flag_fires(
    tmp_path,
) -> None:
    """分块跑：旗标在第 1 块中立起 → 第 2 块开跑前收场，整场落 `interrupted`。

    判据逐条对着 spec 的完成判定：状态写「被打断」、不再有下一块、半份材料（逐块记录）
    还在、缺口读得出（哪一路跑到第几块、哪些路没开跑、正式稿没落）。
    """
    from meeting.offline.progress import OfflineProgressLedger, ledger_path, read_progress

    root = synth_session(tmp_path / "250919", seconds=3.0)
    cancel = threading.Event()
    engine = StopAfterFirstEngine(cancel)
    progress = OfflineProgressLedger(root, session_id="250919", sources=["mic", "loopback"])

    summary = run_session(
        root,
        engine=engine,
        config=FakeConfig(**{"post.chunk_seconds": 1}),
        minutes=False,
        progress=progress,
        cancel=cancel.is_set,
    )

    assert summary["state"] == "interrupted"
    assert summary["interrupted"] is True
    assert len(engine.calls) == 1, "旗标立起后第 2 块不再开跑（两路加起来只跑了这一块）"
    document = read_status(root)
    assert document["state"] == "interrupted"
    assert document["finished_at"], "打断也是终态：收工时刻照写"
    # 缺口注记（d7 判据：读被打断那一场，能看出缺了什么）：
    assert "跑到第 1/3 块" in (document["error"] or ""), document["error"]
    assert "loopback" in (document["error"] or ""), "没开跑的那一路也要点名"
    # 逐路那一格：被打断的那一路 reason = interrupted、缺的区间在 missing_spans；
    # 没轮到的那一路现开一格，同一套键。
    mic_cell = document["artifacts"]["mic"]
    loopback_cell = document["artifacts"]["loopback"]
    assert mic_cell["reason"] == "interrupted"
    assert mic_cell["partial"] is True and len(mic_cell["missing_spans"]) == 2
    assert loopback_cell["reason"] == "interrupted"
    assert loopback_cell["state"] == "interrupted"
    # 顶层 partial 也要立起来（打断场显然缺了区间——落假与缺口注记自相矛盾）
    assert document["partial"] is True
    # 半份材料 = 逐块记录（mic 已跑的那块的字在盘上）；正式稿一份都不落，
    # 报告照落（它也是缺口读数：逐路表带着 interrupted 那一格与缺失区间）。
    header, rows = read_progress(ledger_path(root))
    assert header is not None
    assert [row["source"] for row in rows] == ["mic"]
    assert not (root / "meeting_offline_transcript_mic.md").exists()
    assert not (root / "meeting_offline_transcript_loopback.md").exists()
    report = (root / "meeting_offline_report.md").read_text(encoding="utf-8")
    assert "转写不完整" in report
    assert "mic 缺 2 段" in report


def test_run_session_honors_the_cancel_flag_between_sources_on_a_single_pass(
    tmp_path,
) -> None:
    """单趟：mic 那一块完整跑完（块内没有边界），旗标在「下一路开跑前」命中。

    单趟与分块共主循环、不开特例（决策 d2）：单趟场上「跑到一半」只有整场开始前那一个
    块边界；另一头的边界是逐路之间 —— 跑完的那一路照常落全份产物，没开跑的那一路如实
    记打断。
    """
    root = synth_session(tmp_path / "250919", seconds=3.0)
    cancel = threading.Event()
    engine = StopAfterFirstEngine(cancel)

    summary = run_session(
        root,
        engine=engine,
        config=FakeConfig(),
        minutes=False,
        cancel=cancel.is_set,
    )

    assert summary["state"] == "interrupted"
    assert len(engine.calls) == 1, "只有 mic 那一块进了引擎（loopback 不再开跑）"
    document = read_status(root)
    assert document["state"] == "interrupted"
    # 跑成的那一路照常落产物、格子照旧 done；没开跑的那一路记 interrupted
    assert document["artifacts"]["mic"]["state"] == "done"
    assert (root / "meeting_offline_transcript_mic.md").exists()
    assert document["artifacts"]["loopback"]["reason"] == "interrupted"
    assert not (root / "meeting_offline_transcript_loopback.md").exists()
    assert "没开跑" in (document["error"] or "")


def test_run_session_with_a_cancel_flag_already_set_runs_nothing(tmp_path) -> None:
    """旗标在起跑前就立着 → 一块都不跑，整场落 `interrupted`（两路都是「没开跑」）。"""
    root = synth_session(tmp_path / "250919", seconds=3.0)
    cancel = threading.Event()
    cancel.set()
    engine = StopAfterFirstEngine(cancel)

    summary = run_session(
        root,
        engine=engine,
        config=FakeConfig(),
        minutes=False,
        cancel=cancel.is_set,
    )

    assert summary["state"] == "interrupted"
    assert engine.calls == [], "旗标已立：一个引擎调用都不该有"
    document = read_status(root)
    assert document["state"] == "interrupted"
    assert document["artifacts"]["mic"]["reason"] == "interrupted"
    assert document["artifacts"]["loopback"]["reason"] == "interrupted"
    assert "mic" in (document["error"] or "") and "loopback" in (document["error"] or "")


def test_an_interrupted_run_never_announces_minutes(tmp_path) -> None:
    """打断的那一趟不通知纪要那一层：半份稿不当完整输入（启动器一次都不被调到）。"""
    root = synth_session(tmp_path / "250919", seconds=3.0)
    cancel = threading.Event()
    cancel.set()
    announced: list[str] = []

    summary = run_session(
        root,
        engine=StopAfterFirstEngine(cancel),
        config=FakeConfig(**{"minutes.hook": "auto"}),
        minutes=True,
        announce_minutes=True,
        minutes_launcher=lambda session_dir, **kwargs: announced.append(str(session_dir)) or {},
        cancel=cancel.is_set,
    )

    assert summary["state"] == "interrupted"
    assert announced == [], "打断那一趟不许拉纪要"
    assert summary["minutes"]["announced"] is False
    assert "被打断" in summary["minutes"]["reason"]


def test_write_interrupted_status_reconciles_a_stale_running_disk(tmp_path) -> None:
    """启动核对的写点：盘上 `running` + 逐块记录 → 补成 `interrupted`（同一形状、缺口照拼）。"""
    from meeting.offline import runner as runner_module
    from meeting.offline.progress import write_progress, ChunkProgress, ledger_path

    root = synth_session(tmp_path / "250919", seconds=3.0)
    artifacts.write_status(
        root / artifacts.STATUS_NAME,
        session_id="250919",
        state="running",
        started_at="2025-09-19T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
    )
    write_progress(
        ledger_path(root),
        session_id="250919",
        sources=["mic", "loopback"],
        rows=[
            ChunkProgress("mic", 1, 0.0, 1.0, "transcribed", "第一块", 2, 0.5).to_row(
                chunks_total=3
            ),
        ],
    )

    written = runner_module.write_interrupted_status(
        root, reason="服务重启时这一趟还在跑（进程已结束），按「被打断」收场"
    )

    assert written is not None and written["state"] == "interrupted"
    assert written["started_at"] == "2025-09-19T20:00:00", "开始时刻照盘上那一版"
    assert written["finished_at"]
    assert "跑过 1/3 块" in (written["error"] or ""), written["error"]
    assert "服务重启" in (written["error"] or "")
    assert written["partial"] is True, "缺口（还差块）立 partial，供人读"
    # 已是终态 / 没有状态文件的目录：什么都不写、如实交回 None
    assert (
        runner_module.write_interrupted_status(root, reason="再来一次")
        is None
    ), "interrupted 已落定：核对不许翻掉终态"
    assert (
        runner_module.write_interrupted_status(tmp_path / "没跑过", reason="x") is None
    )


def test_write_interrupted_status_leaves_a_settled_disk_alone(tmp_path) -> None:
    """盘上是 `done` / `failed` 时核对一个字节都不写（核对只接「停着 running」那一档）。"""
    from meeting.offline import runner as runner_module

    root = synth_session(tmp_path / "250919", seconds=3.0)
    artifacts.write_status(
        root / artifacts.STATUS_NAME,
        session_id="250919",
        state="done",
        started_at="2025-09-19T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
        finished_at="2025-09-19T20:10:00",
    )
    before = (root / artifacts.STATUS_NAME).read_text(encoding="utf-8")

    assert runner_module.write_interrupted_status(root, reason="核对") is None
    assert (root / artifacts.STATUS_NAME).read_text(encoding="utf-8") == before
