
from __future__ import annotations

import json
import threading
from pathlib import Path

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

def read_status(source: Path) -> dict:
    return artifacts.read_status(source / artifacts.STATUS_NAME)


def verdict_of(document: dict) -> tuple[str, list[str]]:
    """**读方的判据**（本节点给读方立的那一条，用例按它核）：只看状态文件回答两件事。

    「这一场跑成什么样」与「缺的是哪几路」都只读 `state` 与 `artifacts.<source>.state`
    这两档，不去解析顶层 `error` 那句拼接串 —— 拼接串是给人看的，判据落在这两档上。
    三种结论：

      - `("failed", [...])`：整场 `failed`（每一路都没成），方括号里是没成的路；
      - `("partial", [...])`：整场 `done`，但方括号里那几路没成（**这一档必须与
        「两路都成」分得开**）；
      - `("done", [])`：两路（或这一场声明的每一路）都成。
    """
    sources = [str(name) for name in document.get("sources") or []]
    per_source = document.get("artifacts") or {}
    missing = [
        source
        for source in sources
        if (per_source.get(source) or {}).get("state") != "done"
    ]
    if document.get("state") == "failed":
        return "failed", missing
    return ("partial" if missing else "done"), missing


def gate_headline(root: Path) -> str:
    """运行报告开头那一行「数字静音门：…」（那一行是读报告的人第一眼看到的东西）。"""
    for line in (root / artifacts.REPORT_NAME).read_text(encoding="utf-8").splitlines():
        if line.startswith("- 数字静音门："):
            return line
    raise AssertionError("报告里没有「数字静音门」那一行")


def report_row(root: Path, source: str) -> list[str]:
    """运行报告「逐路」表里这一路那一行（末一格是终态）。

    列序在 `artifacts.write_report()` 里定死：
    `| 路 | 音频 | 秒数 | 块数 | 段数 | 类数 | 类内秒数 | 墙钟 | 实时率 | 状态 |`
    """
    for line in (root / artifacts.REPORT_NAME).read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 10 and cells[0] == source:
            return cells
    raise AssertionError(f"报告里没有 {source} 那一行")


