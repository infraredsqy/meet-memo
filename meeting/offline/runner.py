"""把会后离线这条链串起来（`7c6952e5` phase2）。

一次 `run_session(session_dir)` 走完这些事：

    读场次元信息 → 逐路取音频 → 逐路调引擎 → 逐路编号（`S01`、`S02`…）
    → 落三份产物（每路各一份 + 一份运行报告）→ 状态文件从 `running` 落成终态

**两路之间没有共享状态**：每一路的音频、分段编号、产物文件、度量各归各的，
两路的编号因此不会互相看见，「这条路的一号等于那条路的一号」在数据结构上表达不出来
（限定名 `mic:S01` / `loopback:S01` 里始终带着 `source`）。

## 音频来源

一路的音频按两档取，先到先用：

  1. 归档 PCM `meeting_audio_<source>.pcm`（采集侧边采边写的裸流）；
  2. 归档 MP3 `meeting_audio_<source>.mp3`（收尾转码的产物）。

PCM 是 0 字节而 MP3 在时走第二档。**两档都没有的那一路如实记成失败**（报告与状态文件里
都留痕），另一路照常跑完 —— 整场不许因为一路缺音频就崩掉。

**两路分别调用引擎，绝不先混音再跑**：混了就没法「各自编号、互不相认」（R10）。

## 惰性

`torch` / `transformers` / `av` 都不在本模块顶层 import：它们要么在 `meeting/offline/audio.py`
的函数体内，要么在 `MossOfflineEngine.transcribe()` 里。`import meeting.offline.runner`
因此只拉标准库与包内模块（由 `tests/test_post_hook.py` 在全新解释器里盯着）。

音频时长的换算只有一处口径：PCM = 字节数 / 2 / 16000（16k 单声道 int16），
MP3 = 解码后样本数 / 16000（`decode_audio()` 给出的就是 16k 单声道波形）。
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Sequence

from meeting import config as config_module
from meeting.offline import artifacts
from meeting.offline.engine import OfflineEngine, OfflineSegment
from meeting.offline.speakers import number_speakers

_log = logging.getLogger("meeting.offline.runner")

#: 两路（与 `meeting.backends.base.SOURCE_IDS` 的口径一致；这里不 import 它，
#: 免得 `meeting.backends` 这条链被包进会后路径）
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")

#: 归档 PCM 的形状（`meeting/capture/archive.py` 的同一档）
PCM_SAMPLE_RATE = 16000
PCM_SAMPLE_WIDTH = 2

#: 空 PCM 的判据：一个 int16 样本都装不下（与 `capture.archive.EMPTY_PCM_BYTES` 同档）
EMPTY_PCM_BYTES = PCM_SAMPLE_WIDTH

#: 每路交给引擎的音频上限（秒）。`None` = 整段一次跑完（R9 就是「拿录音整段跑」；
#: 超过模型原生 90 分钟时如实记录它跑成什么样，见 spec 的「超过模型原生能力」那一节）。
DEFAULT_MAX_AUDIO_SECONDS: float | None = None

#: `post.engine` 的缺省落点（`auto` → 本节点的引擎实现，与 `post.hook` 的 `auto` 同形）
DEFAULT_ENGINE_TARGET = "meeting.offline.engine:MossOfflineEngine"

#: 取音频时按这个顺序找（先归档 PCM，再退回 MP3）
_AUDIO_SUFFIXES: tuple[str, ...] = (".pcm", ".mp3")

#: 失败原因的词表（报告与状态文件里都用这几个名字，下游按它分档）
REASON_NO_AUDIO = "no_audio"
REASON_EMPTY_PCM = "empty_pcm"
REASON_ENGINE_ERROR = "engine_error"
REASON_NO_SEGMENTS = "no_segments"


# ---------------------------------------------------------------------------
# 场次元信息
# ---------------------------------------------------------------------------


def session_id_of(session_dir: Path | str) -> str:
    """场次编号 = 场次目录名（`250919` 这种；目录名是服务侧生成的，本模块不另起规则）。"""
    return Path(session_dir).name


def declared_sources(session_dir: Path | str) -> tuple[str, ...]:
    """这一场该跑哪几路：优先读 `session.json` 的 `sources`，读不到就按两路全集。

    `session.json` 的 `sources` 每项带一个 `id`（两路各自一条），本模块按顺序取它。
    """
    raw = _read_session_json(session_dir)
    declared = raw.get("sources")
    if isinstance(declared, list):
        names = [
            str(item.get("id"))
            for item in declared
            if isinstance(item, dict) and item.get("id")
        ]
        if names:
            return tuple(names)
    return SOURCE_IDS


def _read_session_json(session_dir: Path | str) -> dict[str, Any]:
    """读场次目录里的 `session.json`（缺失或读不出来时交回空映射，由调用方决定怎么办）。"""
    import json

    path = Path(session_dir) / "session.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _log.warning("场次元信息读不出来：%s（按默认口径继续）", path)
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# 逐路取音频
# ---------------------------------------------------------------------------


def audio_path_for(session_dir: Path | str, source: str) -> Path:
    """一路音频的首选路径（归档的裸 PCM；`meeting/session.py` 的 `archive_name()` 同形）。"""
    return Path(session_dir) / f"meeting_audio_{source}.pcm"


def _read_pcm(session_dir: Path | str, source: str, max_seconds: float | None) -> bytes:
    """读一路的归档 PCM 字节（`max_seconds` 给定时按 16k/单声道/int16 截到那个秒数）。"""
    with open(audio_path_for(session_dir, source), "rb") as handle:
        if max_seconds is None:
            return handle.read()
        return handle.read(int(float(max_seconds) * PCM_SAMPLE_RATE) * PCM_SAMPLE_WIDTH)


def resolve_audio(
    session_dir: Path | str, source: str
) -> tuple[str | None, str | None]:
    """挑一路的音频：交回 `(路径, 说明)`；挑不出来时 `路径` 为 `None`、`说明` 里是原因。

    判据按顺序走：

      - 归档 PCM 字节数 `>= 2`（装得下至少一个样本）→ 用它；
      - PCM 缺席或 0 字节，但同名 `.mp3` 在 → 用 MP3（收尾转码的产物），说明里记一笔；
      - 两档都挑不出来 → `(None, "no_audio…")`。
    """
    pcm = audio_path_for(session_dir, source)
    if pcm.exists():
        size = pcm.stat().st_size
        if size >= EMPTY_PCM_BYTES:
            return str(pcm), None
        mp3 = pcm.with_suffix(".mp3")
        if mp3.exists() and mp3.stat().st_size > 0:
            return str(mp3), f"归档 PCM 是 {size} 字节，改用 MP3"
        return None, f"{REASON_EMPTY_PCM}：归档 PCM 是 {size} 字节、同目录没有可用的 MP3"
    mp3 = pcm.with_suffix(".mp3")
    if mp3.exists() and mp3.stat().st_size > 0:
        return str(mp3), "归档 PCM 缺席，改用 MP3"
    return None, f"{REASON_NO_AUDIO}：{pcm.name} 与 {mp3.name} 都不在场"


# ---------------------------------------------------------------------------
# 引擎解析与构造
# ---------------------------------------------------------------------------


def resolve_engine(selector: str | None = None) -> tuple[Any, str]:
    """按 `post.engine` 解析引擎类，交回 `(类, 说明)`；解析不出来即抛。

    取值口径与 `capture.backend` / `transcriber.backend` / `post.hook` 那三个键同形
    （`config.BACKEND_SELECTORS`）：

      - `auto`（缺省）→ `meeting.offline.engine:MossOfflineEngine`；
      - `null` → 抛：会后离线这一层没有「替身引擎」，显式关掉它的写法是 `post.hook = null`；
      - `module:Class` → 按它导入。

    **与会后钩子分开解析**：`post.engine` 换掉时 `post.hook` 那一层一个字不动
    （spec 的「换引擎时这一层不动」）。
    """
    if selector is None:
        raw = config_module.get("post.engine")
        selector = raw.strip() if isinstance(raw, str) and raw.strip() else "auto"
    selector = str(selector).strip()
    if selector == "null":
        raise LookupError(
            "post.engine = null：会后离线这一层没有替身引擎（关掉整条链请把 post.hook 取 null）"
        )
    target = DEFAULT_ENGINE_TARGET if selector == "auto" else selector
    if ":" not in target:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise LookupError(f"{module_name} 里没有 {attr}")
    return getattr(module, attr), f"post.engine = {selector} → {target}"


def _engine_options(config: Any = None) -> dict[str, str]:
    """模型 / 档位 / 引擎选择三项：优先读传进来的那份配置，没有就读 `config.get()` 的生效值。"""
    getter = getattr(config, "get", None)

    def value(key: str) -> Any:
        if callable(getter):
            return getter(key)
        return config_module.get(key)

    model = value("post.model")
    dtype = value("post.dtype")
    selector = value("post.engine")
    return {
        "model": str(model) if model else config_module.default_for("post.model"),
        "dtype": str(dtype) if dtype else config_module.default_for("post.dtype"),
        "selector": str(selector).strip() if selector else "auto",
    }


def build_engine(config: Any = None) -> Any:
    """造一台引擎（入参可换实现：换引擎时这一层下面的一切都不动）。"""
    options = _engine_options(config)
    cls, detail = resolve_engine(options["selector"])
    _log.info("会后离线引擎：%s（%s，dtype=%s）", detail, options["model"], options["dtype"])
    return cls(model=options["model"], dtype=options["dtype"])


def engine_descriptor(config: Any = None) -> dict[str, Any]:
    """写进产物 `engine` 那一格的四项（`selector` 也留着：换引擎时可追溯）。"""
    options = _engine_options(config)
    return {
        "model": options["model"],
        "dtype": options["dtype"],
        "device": "auto",
        "selector": options["selector"],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_session(
    session_dir: str | Path,
    *,
    engine: OfflineEngine | None = None,
    config: Any = None,
) -> dict:
    """对一场会跑完会后离线转写，落三份产物 + 状态文件，返回摘要 dict。

    `engine` 入参可注入（用例靠它塞假引擎）；不给就按 `post.engine` / `post.model` /
    `post.dtype` 现造一台。`config` 是可选的那一份配置对象（`meeting.config.Config`），
    缺省读进程当前生效值。

    **一路的失败不带走整场**：那一路的失败原因落进报告与状态文件，另一路照常跑。
    全部路都失败时整场落 `failed`，否则落 `done`。
    """
    directory = Path(session_dir) if session_dir else None
    if directory is None or not str(directory):
        raise ValueError("run_session 需要场次目录（收到空值）")
    directory.mkdir(parents=True, exist_ok=True)

    session_id = session_id_of(directory)
    started_at = _now_iso()
    sources = declared_sources(directory)
    descriptor = engine_descriptor(config)
    status_path = directory / artifacts.STATUS_NAME

    # 状态文件先落一版 `running`：钩子那边「返回即算启动成功」，界面上这一格当场可读。
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state="running",
        started_at=started_at,
        sources=list(sources),
        artifacts={},
    )

    engine_error: str | None = None
    if engine is None:
        try:
            engine = build_engine(config)
        except Exception as exc:  # 引擎造不出来：整场如实失败，异常不外传
            engine_error = f"{type(exc).__name__}: {exc}"
            _log.error("会后离线引擎解析失败：%s", engine_error)

    begun = time.monotonic()
    metrics: list[artifacts.SourceMetrics] = []
    per_source: dict[str, dict[str, Any]] = {}
    for source in sources:
        metrics.append(
            _run_one_source(
                directory,
                source,
                engine=engine,
                engine_error=engine_error,
                session_id=session_id,
                descriptor=descriptor,
                config=config,
                per_source=per_source,
            )
        )
    total_wall = time.monotonic() - begun

    report_path = artifacts.write_report(
        directory / artifacts.REPORT_NAME,
        session_id=session_id,
        session_dir=directory,
        engine=descriptor,
        metrics=metrics,
        total_wall_seconds=total_wall,
    )

    failed = [item for item in metrics if item.state != "done"]
    state = "failed" if metrics and len(failed) == len(metrics) else "done"
    error = None
    if failed:
        error = "；".join(f"{item.source}：{item.error or item.state}" for item in failed)
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state=state,
        started_at=started_at,
        sources=list(sources),
        artifacts={
            source: {
                "transcript": row["transcript"],
                "speakers": row["speakers"],
                "report": report_path.name,
            }
            for source, row in per_source.items()
        },
        finished_at=_now_iso(),
        error=error,
    )

    summary = {
        "session_id": session_id,
        "session_dir": str(directory),
        "state": state,
        "sources": list(sources),
        "error": error,
        "total_wall_seconds": round(total_wall, 3),
        "engine": descriptor,
        "report": report_path.name,
        "status": status_path.name,
        "transcripts": per_source,
    }
    _log.info(
        "会后离线转写结束：%s（%s，%d 路，%.2fs）",
        session_id, state, len(sources), total_wall,
    )
    return summary


def _run_one_source(
    directory: Path,
    source: str,
    *,
    engine: Any,
    engine_error: str | None,
    session_id: str,
    descriptor: dict[str, Any],
    config: Any,
    per_source: dict[str, dict[str, Any]],
) -> artifacts.SourceMetrics:
    """跑一路：取音频 → 调引擎 → 编号 → 落两份按路的产物 → 交回度量。

    本函数**不抛**：这一路的任何失败都记进度量（`state = "failed"` + `error`），
    让另一路照常跑完。返回时 `per_source` 里已经补上这一路的产物名。
    """
    metric = artifacts.SourceMetrics(source=source, state="running")
    started = time.monotonic()
    path, note = resolve_audio(directory, source)
    segments: list[OfflineSegment] = []

    if path is None:
        metric.state = "failed"
        metric.error = note
        metric.reasons[
            REASON_NO_AUDIO if REASON_NO_AUDIO in (note or "") else REASON_EMPTY_PCM
        ] = 1
        _log.warning("会后离线：%s 这一路没有可用音频（%s）", source, note)
    elif engine is None:
        metric.state = "failed"
        metric.error = f"{REASON_ENGINE_ERROR}：引擎不可用（{engine_error}）"
        metric.reasons[REASON_ENGINE_ERROR] = 1
    else:
        if note:
            _log.info("会后离线：%s 的音频取 %s（%s）", source, path, note)
        try:
            metric.audio_file = Path(path).name
            segments = list(engine.transcribe(path))
            metric.audio_seconds = _audio_seconds(directory, source, path, segments)
        except Exception as exc:  # 引擎抛错：这一路失败，另一路照跑
            metric.state = "failed"
            metric.audio_file = Path(path).name
            metric.error = f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}"
            metric.reasons[REASON_ENGINE_ERROR] = 1
            _log.exception("会后离线：%s 这一路引擎抛错（如实记账，继续跑另一路）", source)
        else:
            if not segments:
                metric.state = "failed"
                metric.error = f"{REASON_NO_SEGMENTS}：引擎交回 0 段"
                metric.reasons[REASON_NO_SEGMENTS] = 1

    if metric.state != "failed":
        _write_source_artifacts(
            directory,
            source,
            session_id=session_id,
            descriptor=descriptor,
            segments=segments,
            metric=metric,
            per_source=per_source,
        )
        metric.state = "done"

    metric.wall_seconds = time.monotonic() - started
    if metric.audio_seconds > 0:
        metric.rtf = metric.wall_seconds / metric.audio_seconds
    return metric


def _write_source_artifacts(
    directory: Path,
    source: str,
    *,
    session_id: str,
    descriptor: dict[str, Any],
    segments: Sequence[OfflineSegment],
    metric: artifacts.SourceMetrics,
    per_source: dict[str, dict[str, Any]],
) -> None:
    """落这一路的两份按路产物（转写 + 对照表），并把文件名记进度量与摘要。

    编号规则来自 `meeting/offline/speakers.py`：**本路内按首次出现顺序**，`source`
    原样进限定名。
    """
    spans = number_speakers(source, segments)
    summary = artifacts.speaker_rows(
        spans, raw_labels=[segment.speaker for segment in segments]
    )
    transcript_name = artifacts.TRANSCRIPT_NAME_TEMPLATE.format(source=source)
    speakers_name = artifacts.SPEAKERS_NAME_TEMPLATE.format(source=source)

    artifacts.write_transcript(
        directory / transcript_name,
        (
            artifacts.format_transcript_line(span.start, span.local, span.text)
            for span in spans
        ),
    )
    document = artifacts.build_speakers_document(
        session_id=session_id,
        source=source,
        engine=descriptor,
        audio={
            "file": metric.audio_file,
            "seconds": round(metric.audio_seconds, 3),
        },
        segments=[
            {
                "start": span.start,
                "end": span.end,
                "local": span.local,
                "text": span.text,
            }
            for span in spans
        ],
        speakers=[
            {
                "local": row["local"],
                "label_raw": row["label_raw"],
                "segments": row["segments"],
                "seconds": row["seconds"],
            }
            for row in summary
        ],
    )
    artifacts.write_speakers(directory / speakers_name, document)

    metric.segments = len(spans)
    metric.speakers = len(summary)
    metric.seconds = sum(float(row["seconds"]) for row in summary)
    per_source[source] = {
        "transcript": transcript_name,
        "speakers": speakers_name,
        "segments": len(spans),
        "speaker_count": len(summary),
        "audio_seconds": round(metric.audio_seconds, 3),
    }


def _audio_seconds(
    directory: Path, source: str, path: str, segments: Sequence[OfflineSegment]
) -> float:
    """这一路的音频时长（秒）。

    PCM 走「字节数 / 2 / 16000」这条换算；MP3 等别的容器走解码后的样本数 ——
    解码那一步由引擎做过一次（它把同一份音频喂给了模型），这里只解一次算长度，
    拿不到就退回段尾的最大值（**如实退让**，不臆造时长）。
    """
    if Path(path).suffix.lower() == ".pcm":
        return os.path.getsize(path) / PCM_SAMPLE_WIDTH / PCM_SAMPLE_RATE
    try:
        from meeting.offline.audio import decode_audio

        return float(decode_audio(path).size) / PCM_SAMPLE_RATE
    except Exception:  # pragma: no cover - 解码失败只影响时长这一格，不影响产物
        _log.warning("会上音频的时长读不出来：%s（退回段尾最大值）", path, exc_info=True)
        return max((float(item.end) for item in segments), default=0.0)


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/state.py` 的 `now_iso()` 同形，这里不 import 它）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_ENGINE_TARGET",
    "DEFAULT_MAX_AUDIO_SECONDS",
    "EMPTY_PCM_BYTES",
    "PCM_SAMPLE_RATE",
    "PCM_SAMPLE_WIDTH",
    "REASON_EMPTY_PCM",
    "REASON_ENGINE_ERROR",
    "REASON_NO_AUDIO",
    "REASON_NO_SEGMENTS",
    "SOURCE_IDS",
    "audio_path_for",
    "build_engine",
    "declared_sources",
    "engine_descriptor",
    "resolve_audio",
    "resolve_engine",
    "run_session",
    "session_id_of",
]
