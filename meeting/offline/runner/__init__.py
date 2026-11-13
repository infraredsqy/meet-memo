"""会后离线转写主链的**编排**：一次 `run_session(session_dir)` 走完读场次元信息 → 逐路取音频
→ 逐块调引擎 → 逐路编号 → 落三份产物 → 状态文件从 `running` 落成终态。

本模块是 `meeting.offline.runner` 包的主模块：编排（`run_session` 一族）住这里，
对外的公共面也由本模块聚合；杂活各归其家——

- PCM 字节读取/换算与数字静音门：包内 `runner/pcm.py`；
- 场次元信息读取（`session_id_of` / `declared_sources`）：包内 `runner/session_info.py`；
- 引擎解析构造（`resolve_engine` / `build_engine`）：`meeting/offline/engine.py`；
- 状态文件行形状与逐块结论词表：`meeting/offline/artifacts.py`；
- 尾巴上怎么拉起纪要：`meeting/offline/minutes_tail.py`（本模块只留一处调用点）。

关键口径（改之前先读完）：

- **两路绝不先混音再跑**（R10）：块内标签带 `cNN:` 命名空间，块间不做映射、不比相似度、
  不合并——合并错是把两个人并成一个编号（不可接受的错），不合并只是把一个人算成两个。
- **块失败必须露出来**：缺的块进 `partial` / `missing_spans` / 转写首行注释，
  静默 `done` 加一段悄悄消失的音频是禁止的（design-intent 第 11 条）。
- **数字静音门**的判据与满量程换算住 `runner/pcm.py`：峰值低于半格（`SILENT_PEAK_FLOOR`）
  且长度够 `MIN_GATE_SAMPLES` 的块不送引擎——引擎在样本全零的块上会编出整段假正文
  （`250906` 实测 69% 假字）；判不了（解不开、读不全）照常送引擎并记 `gate = "unjudged"`。
- 惰性：`torch` / `transformers` / `av` 不在顶层 import（`tests/test_post_hook.py` 盯着）。
- 尾巴上的纪要钩子在产物终态**之后**最后通知；`announce_minutes` 缺省假（项目与 DSH 隔离，
  只有 `meeting post` 人工入口传 True），钩子炸了不影响离线产物。
- 时长口径唯一：PCM = 字节数 / 2 / 16000（16k 单声道 int16），MP3 = 解码后样本数 / 16000。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from meeting import config as config_module
from meeting.offline import artifacts, minutes_tail
from meeting.offline.artifacts import (
    REASON_ALL_CHUNKS_FAILED,
    REASON_DIGITAL_SILENCE,
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_ENGINE_LOAD_ERROR,
    REASON_GATE_UNJUDGED,
    REASON_INTERRUPTED,
    REASON_NO_AUDIO,
    REASON_NO_SEGMENTS,
    REASON_NOT_RUN,
    REASON_TRANSCRIBED,
    REASON_ZERO_SPAN_SEGMENTS,
    _failed_source_row,
    _status_artifact_row,
)
from meeting.offline.audio import PCM_SUFFIX, PCM_SAMPLE_WIDTH, SAMPLE_RATE as PCM_SAMPLE_RATE
from meeting.offline.engine import (
    DEFAULT_ENGINE_TARGET,
    OfflineEngine,
    OfflineSegment,
    build_engine,
    engine_descriptor,
    release_engine,
    resolve_device_of,
    resolve_engine,
)
from meeting.offline.minutes_tail import MINUTES_GATE_MESSAGE
from meeting.offline.progress import ChunkProgress, OfflineProgressLedger
from meeting.offline.runner.pcm import (
    GATE_METHOD_DECODE_AT_PLAN,
    GATE_METHOD_DECODE_NOW,
    GATE_METHOD_PCM,
    GATE_METHOD_WAVEFORM,
    GATE_NOT_SILENT,
    GATE_SILENT,
    GATE_UNJUDGED,
    MIN_GATE_SAMPLES,
    SILENT_PEAK_FLOOR,
    MeasureResult,
    _PCM_SCAN_BLOCK_BYTES,
    _audio_paths,
    _decode_samples,
    _pcm_sample_count,
    full_scale_counts,
    is_digital_silence,
    measure_span,
    pcm_sample_chunk_counts,
    samples_to_count_floats,
    write_chunk_wav,
)
from meeting.offline.runner.session_info import SOURCE_IDS, declared_sources, session_id_of
from meeting.offline.speakers import number_speakers
from meeting.session import archive_name, segment_archive_paths

_log = logging.getLogger("meeting.offline.runner")


#: 归档 PCM 的形状（16k 采样率 / 样本宽 / 后缀）：唯一出处住 `meeting/offline/audio.py`
#: （`meeting/capture/archive.py` 的同一档），这里按本模块惯用名接进来。
# PCM_SAMPLE_RATE / PCM_SAMPLE_WIDTH / PCM_SUFFIX 由上面的 import 提供

#: 空 PCM 的判据：一个 int16 样本都装不下（与 `capture.archive.EMPTY_PCM_BYTES` 同档）
EMPTY_PCM_BYTES = PCM_SAMPLE_WIDTH

#: 缺省块长（秒），与 `config.DEFAULT_VALUES` 同值；这里留一份让拿不到配置对象时
#: （`post.hook` 后台线程会走到）也有实测安全档。取值理由见 `config.py`。
DEFAULT_CHUNK_SECONDS: float = 450.0

#: `post.chunk_seconds` 显式关掉档：整段一次跑（R9）
CHUNK_SECONDS_OFF: float = 0.0



#: 块内标签的命名空间模板：第 `NN` 块的 `Sxx` 变成 `cNN:Sxx`
CHUNK_LABEL_TEMPLATE = "c{index:02d}:{label}"


#: 分块时临时 wav 住在场次目录下的这个子目录里，跑完一块不留
CHUNK_DIR_NAME = "_offline_chunks"


def chunk_label(index: int, label: str) -> str:
    """块内命名空间（`S01` → `c01:S01`）：不同块的 `S01` 在 `number_speakers()` 眼里
    是不同键，块间编号因此绝不合并（R10）。"""
    return CHUNK_LABEL_TEMPLATE.format(index=int(index), label=str(label))


@dataclass(slots=True)
class ChunkStep:
    """一块音频在原录音时间轴上的位置（`offset` + `seconds` 就是它覆盖的那一段）。"""

    index: int
    offset: float
    seconds: float

    @property
    def span(self) -> tuple[float, float]:
        return (round(float(self.offset), 3), round(float(self.offset + self.seconds), 3))


@dataclass(slots=True)
class ChunkRun:
    """一块跑完的如实登记（成功、跳过、失败各一条），回答「这一段为什么没有字」：
    读方按 `reason` 词档分档、按 `gate` 看门判过没有。`reason` 是构造时显式给的
    `REASON_*` 词；`error` 只负责人话，机器读 `reason`，不从错误消息原文反推。
    """

    index: int
    offset: float
    seconds: float
    segments: int = 0
    wall_seconds: float = 0.0
    error: str | None = None
    reason: str = ""
    #: 这一块是数字静音、跳过了引擎调用
    silent: bool = False
    #: 门结论：`silent` / `not_silent` / `unjudged`
    gate: str = GATE_NOT_SILENT
    #: 门凭什么判的（`GATE_METHOD_*`；判不了的那一档为空）
    gate_method: str | None = None
    #: 门判不了时的一句话（`gate == "unjudged"` 时才有）
    gate_detail: str | None = None
    #: 交回段里 `end <= start` 的条数（0 = 没有）
    zero_span_segments: int = 0

    @property
    def span(self) -> tuple[float, float]:
        return (round(float(self.offset), 3), round(float(self.offset + self.seconds), 3))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset_seconds": round(float(self.offset), 3),
            "seconds": round(float(self.seconds), 3),
            "span": list(self.span),
            "segments": int(self.segments),
            "wall_seconds": round(float(self.wall_seconds), 3),
            "error": self.error,
            "silent": bool(self.silent),
            "reason": self.reason,
            "gate": self.gate,
            "gate_method": self.gate_method,
            "gate_detail": self.gate_detail,
            "zero_span_segments": int(self.zero_span_segments),
        }


@dataclass(slots=True)
class ChunkPlan:
    """一路音频的切法（纯描述，不碰盘、不调引擎）。

    `chunks` 只有一条时就是单趟：引擎拿原始音频路径、标签不带命名空间。
    """

    total_seconds: float
    chunk_seconds: float
    chunks: list[ChunkStep]
    namespace: bool

    @property
    def single_pass(self) -> bool:
        return len(self.chunks) <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_seconds": round(float(self.chunk_seconds), 3),
            "namespace": bool(self.namespace),
            "chunk_count": len(self.chunks),
            "chunks": [
                {
                    "index": step.index,
                    "offset_seconds": round(float(step.offset), 3),
                    "seconds": round(float(step.seconds), 3),
                }
                for step in self.chunks
            ],
        }


def read_chunk_seconds(config: Any = None) -> float:
    """`post.chunk_seconds` 的生效值（秒）；拿不到配置或值非法时退回默认档（**不抛**）。
    `0` = 显式关掉分块（整段一次跑），`> 0` = 每块最多这么长。"""
    getter = getattr(config, "get", None)
    if callable(getter):
        raw = getter("post.chunk_seconds")
    else:
        try:
            raw = config_module.get("post.chunk_seconds")
        except Exception:  # pragma: no cover - 配置层缺席时退回默认档
            raw = None
    if raw is None:
        try:
            raw = config_module.default_for("post.chunk_seconds")
        except Exception:  # pragma: no cover - 键表里没有这一格时退回本模块的默认档
            raw = DEFAULT_CHUNK_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_SECONDS
    if value < 0:
        return DEFAULT_CHUNK_SECONDS
    return value


def plan_chunks(total_seconds: float, chunk_seconds: float) -> ChunkPlan:
    """把「总时长 + 块长」算成切法：**连续、不重叠、从头开始**。块长 `<= 0` 或总时长
    不超过块长时交回单块——单趟与分块共用同一条主循环，产物只在「标签带不带命名空间」
    分叉。切点是固定间隔；切点附近找静默中点那条路未实现、未测，不假装做了（设计报告 10.2）。
    """
    total = max(0.0, float(total_seconds))
    step_seconds = float(chunk_seconds)
    if step_seconds <= 0 or total <= step_seconds:
        return ChunkPlan(
            total_seconds=total,
            chunk_seconds=step_seconds,
            chunks=[ChunkStep(index=1, offset=0.0, seconds=round(total, 3))],
            namespace=False,
        )
    steps: list[ChunkStep] = []
    offset = 0.0
    index = 1
    while offset < total:
        length = min(step_seconds, total - offset)
        steps.append(
            ChunkStep(index=index, offset=round(offset, 3), seconds=round(length, 3))
        )
        offset += step_seconds
        index += 1
    return ChunkPlan(
        total_seconds=total,
        chunk_seconds=step_seconds,
        chunks=steps,
        namespace=True,
    )




def clear_chunk_dir(session_dir: Path | str) -> None:
    """清掉分块用的临时目录（上一次崩在半路留下的块也一并扫掉）。"""
    target = Path(session_dir) / CHUNK_DIR_NAME
    if not target.exists():
        return
    for item in target.iterdir():
        try:
            if item.is_file():
                item.unlink()
        except OSError:  # pragma: no cover - 清不掉不挡住这一趟，交给系统清理临时目录
            _log.warning("分块临时文件清不掉：%s", item)
    try:
        target.rmdir()
    except OSError:  # pragma: no cover - 目录里还有别的东西时留着，不影响结果
        pass


def chunk_file(session_dir: Path | str, index: int) -> Path:
    """第 `index` 块的临时 wav 路径。"""
    return Path(session_dir) / CHUNK_DIR_NAME / f"chunk_{int(index):02d}.wav"










def audio_path_for(session_dir: Path | str, source: str) -> Path:
    """一路**段 1**的音频路径（`meeting/session.py` 的 `archive_name()` 同形）；
    消费入口是 `resolve_audio()`，这一格给「就要看第一段在哪」的读方与日志。"""
    return Path(session_dir) / archive_name(source, 1)


def resolve_audio(
    session_dir: Path | str, source: str
) -> tuple[list[str] | None, str | None]:
    """挑一路的音频：交回 `(按序拼接的段路径表, 说明)`，挑不出来路径为 `None`。
    段 PCM 在场且总字节 `>= 2` 用 PCM；PCM 缺席或全 0 字节而段 MP3 在用 MP3；
    两档都没有交回 `(None, "no_audio…"/"empty_pcm…")`。段拼起来 = 连续时间轴。
    """
    pcm_paths = segment_archive_paths(session_dir, source)

    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = sum(_size(path) for path in pcm_paths)
    if pcm_paths and total >= EMPTY_PCM_BYTES:
        return [str(path) for path in pcm_paths], None
    mp3_paths = [
        path
        for path in segment_archive_paths(session_dir, source, suffix=".mp3")
        if _size(path) > 0
    ]
    if mp3_paths:
        # 「段 PCM 在但为 0 字节」与「段 PCM 全部缺席」分开表述：缺席的文件不能说成空文件。
        if pcm_paths:
            return (
                [str(path) for path in mp3_paths],
                f"归档 PCM 共 {total} 字节，改用 MP3",
            )
        return [str(path) for path in mp3_paths], "归档 PCM 缺席，改用 MP3"
    if pcm_paths:
        return (
            None,
            f"{REASON_EMPTY_PCM}：归档 PCM 共 {total} 字节、同目录没有可用的 MP3",
        )
    first_mp3 = audio_path_for(session_dir, source).with_suffix(".mp3").name
    return (
        None,
        f"{REASON_NO_AUDIO}：{archive_name(source, 1)} 与 {first_mp3} 都不在场",
    )


def _audio_file_label(paths: "Sequence[str]") -> str:
    """产物 `audio.file` 格式：恰好一段写文件名，多段写「第一段名等 N 个段文件」
    （段清单的权威在 `session.json` 的 `segments` 与盘上的段文件，这一格是给人看的注脚）。"""
    names = [Path(path).name for path in paths]
    if len(names) == 1:
        return names[0]
    return f"{names[0]} 等 {len(names)} 个段文件"




def run_session(
    session_dir: str | Path,
    *,
    engine: OfflineEngine | None = None,
    config: Any = None,
    minutes_launcher: Any = None,
    force: bool = False,
    minutes: bool = True,
    announce_minutes: bool = False,
    progress: OfflineProgressLedger | None = None,
    cancel: Any = None,
) -> dict:
    """对一场会跑完会后离线转写，落三份产物 + 状态文件，返回摘要 dict。

    - `engine` / `config` / `progress` 可注入（用例塞假件）；缺省按 `post.engine` /
      `post.model` / `post.dtype` 现造一台、读进程当前生效值。
    - `cancel` 是可注入取消旗标（callable，返回真 = 请求停），在**逐块主循环顶**检查：
      当前块跑完、下一块开跑前收场（正在跑的块不掐），命中后由本函数把状态写成
      `interrupted`（缺口注记进 `error`，只供人读、不作判据——design-intent 第 6 条），
      纪要层不通知（半份稿不当完整输入）。
    - `announce_minutes` 缺省 `False`：尾巴上安静跳过（钩子零启动、盘上零纪要状态文件）。
      只有 `meeting post` 人工入口传 `True`，其余调用一律不碰纪要层（与 DSH 隔离）。
      `minutes = False` 是「生成会后精细稿」按钮（`post_offline`）那一档，只出精细稿
      不通知纪要（R26 / design-intent 第 10 条）；`minutes_launcher` 非空时替掉按
      `minutes.hook` 解析的启动器，`force = True` 跳过「已完成」的闸。
    - **一路的失败不带走整场**：顶层 `state` 三档 `failed`（每路都没交回）/ `partial`
      （至少一路整条没交回）/ `done` 只看这一格就分得开；`artifacts.<source>` 每一路都有
      同形一格（含失败路），没跑成的那一路一份产物文件都不落。被打断是第四档
      `interrupted`。引擎加载失败单独记 `engine_load_error`；整路数字静音时模型一次
      都不加载（design-intent 第 21 条），照旧落 `done` + `silent: true`。
    - `running` 之后逃出的任何异常（连 `BaseException`）都由 `_write_failed_status()`
      落一版 `failed` 再照旧上抛——状态文件不会永远停在 `running`。
    - 没声跳过不影响终态（`state` 仍 `done`、`partial` 仍 `false`），区间进 `silent` /
      `silent_spans`。纪要钩子在三份产物与终态都落完**之后**最后调一次，炸了只记 log。
    - `progress` 是逐块进度记录的注入点：给一份就每块跑完往 `meeting_offline_progress.jsonl`
      补一行；正式稿的写时机与它无关（整路跑完一次写）。
    """
    directory = Path(session_dir) if session_dir else None
    if directory is None or not str(directory):
        raise ValueError("run_session 需要场次目录（收到空值）")
    directory.mkdir(parents=True, exist_ok=True)

    session_id = session_id_of(directory)
    started_at = _now_iso()
    sources = declared_sources(directory)
    status_path = directory / artifacts.STATUS_NAME
    if progress is not None:
        progress.start(started_at=started_at)

    metrics: list[artifacts.SourceMetrics] = []
    per_source: dict[str, dict[str, Any]] = {}
    begun = time.monotonic()
    report_path: Path | None = None
    descriptor: dict[str, Any] = {}
    interrupted = False

    try:
        # 先按「引擎还不存在」备一版描述符：它自己万一也抛，兜底状态文件与报告还有内容可读。
        descriptor = engine_descriptor(config, None)
        engine_error: str | None = None
        if engine is None:
            try:
                engine = build_engine(config)
            except Exception as exc:  # 引擎造不出来：整场如实失败，异常不外传
                engine_error = f"{type(exc).__name__}: {exc}"
                _log.error("会后离线引擎解析失败：%s", engine_error)

        descriptor = engine_descriptor(config, engine)

        # 先落一版 `running` 且落点靠前：后面从哪一步抛出来，兜底都能把 `running` 翻成 `failed`。
        artifacts.write_status(
            status_path,
            session_id=session_id,
            state="running",
            started_at=started_at,
            sources=list(sources),
            artifacts={},
        )

        interrupted = False
        for source in sources:
            # 逐路边界也是块边界：单趟场上「下一块开跑前停」在这里兑现成「下一路开跑前停」。
            if cancel is not None and cancel():
                interrupted = True
                _log.info("会后离线：取消旗标已立，%s 这一路不再开跑", source)
                break
            metric = _run_one_source(
                directory,
                source,
                engine=engine,
                engine_error=engine_error,
                session_id=session_id,
                descriptor=descriptor,
                config=config,
                per_source=per_source,
                progress=progress,
                cancel=cancel,
            )
            metrics.append(metric)
            if metric.reasons.get(REASON_INTERRUPTED):
                interrupted = True
                break
        total_wall = time.monotonic() - begun

        report_path = artifacts.write_report(
            directory / artifacts.REPORT_NAME,
            session_id=session_id,
            session_dir=directory,
            engine=descriptor,
            metrics=metrics,
            total_wall_seconds=total_wall,
        )
    except BaseException as exc:
        # 逃出的异常都在盘上落一版 failed（否则状态文件永远停在 running），
        # 原异常照旧上抛——调用方（meeting post、线程体）的既有契约一个字不改。
        _write_failed_status(
            status_path,
            session_id=session_id,
            started_at=started_at,
            sources=sources,
            per_source=per_source,
            failure=exc,
        )
        raise
    finally:
        # 进度记录收在终态（`finish()` 内部兜住落盘失败），原异常照旧上抛。
        if progress is not None:
            progress.finish(finished_at=_now_iso())
        # 引擎引用随返回释放，但 PyTorch 缓存分配器不会自动把预留显存还给驱动；服务进程
        # 常驻，须显式 release（design-intent 第 21 条；会中 release_gpu() 的先例）。
        if engine is not None:
            try:
                release_engine(engine)
            except Exception:  # pragma: no cover - 释放失败不影响这一趟的结论
                _log.warning("会后离线：引擎显存释放那一步出意外（不影响这一趟）", exc_info=True)

    failed = [item for item in metrics if item.state != "done" and not item.reasons.get(REASON_INTERRUPTED)]
    # 顶层三档：每路都没交回 failed / 至少一路整条没交回 partial / 全交回 done。
    # 没声跳过那一路 state 仍 done（什么都没出错）；打断档在这三档之前。
    if interrupted:
        state = "interrupted"
    elif metrics and len(failed) == len(metrics):
        state = "failed"
    elif failed:
        state = "partial"
    else:
        state = "done"
    error = None
    if interrupted:
        # 缺口注记（d7，只供人读）：任何代码都不许拿它当判据。
        gap_notes = [
            f"{item.source}：{item.error}"
            for item in metrics
            if item.reasons.get(REASON_INTERRUPTED)
        ]
        never_ran = [str(source) for source in sources if source not in per_source]
        if never_ran:
            gap_notes.append(
                "没开跑：" + "、".join(never_ran) + "（逐块记录里没有它们的行）"
            )
        error = "；".join(gap_notes) or "interrupted：取消旗标在块边界命中，这一趟收场"
        # 没跑到的那几路也要有格子（同 `_write_failed_status()` 的兜法），读方按同一套键读。
        for source in sources:
            per_source.setdefault(
                source,
                _failed_source_row(
                    artifacts.SourceMetrics(
                        source=str(source),
                        state="interrupted",
                        reasons={REASON_INTERRUPTED: 1},
                    )
                ),
            )
    elif failed:
        error = "；".join(f"{item.source}：{item.error or item.state}" for item in failed)
    # 缺了块（某块没成）的整场仍落 done，但 partial 立起来：一份少了 450 秒却报 done
    # 的转写，下游读不出它少了东西。打断的那一路也进这一格——它缺的是没跑到的块。
    degraded = [
        item for item in metrics
        if item.state in ("done", "interrupted") and item.partial
    ]
    partial = bool(degraded)
    if degraded and not interrupted:
        note = "；".join(
            f"{item.source}：{_missing_spans_note(item.missing_spans)}"
            f"（原因：{'/'.join(sorted(_failure_reasons(item))) or REASON_ENGINE_ERROR}，"
            "转写缺这一段）"
            for item in degraded
        )
        error = f"{error}；{note}" if error else note
    # 没声跳过不进 error、不立 partial：这一档什么都没出错，只写在自己的三格里。
    silent_spans: list[tuple[float, float]] = [
        span for item in metrics for span in item.silent_spans
    ]
    # 逐路那份进 artifacts.<source>，整场按路聚合进顶层 gate。
    gate = _aggregate_gate(metrics)
    report_name = None if report_path is None else report_path.name
    artifacts_rows = {
        source: _status_artifact_row(row, report=report_name)
        for source, row in per_source.items()
    }
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state=state,
        started_at=started_at,
        sources=list(sources),
        artifacts=artifacts_rows,
        finished_at=_now_iso(),
        error=error,
        partial=partial,
        silent=bool(silent_spans),
        silent_spans=silent_spans,
        gate=gate,
    )

    if interrupted:
        # 打断趟不通知纪要：稿子不完整，把「缺一段」偷偷传给下游是禁止的。
        minutes_summary: dict[str, Any] = {
            "hook": "skipped",
            "announced": False,
            "reason": "这一趟被打断：稿子不完整，纪要那一层不通知",
        }
    elif not minutes:
        minutes_summary = {
            "hook": "skipped",
            "announced": False,
            "reason": "这一趟只要会后精细稿",
        }
    elif not announce_minutes:
        # 隔离闸：非人工发起安静跳过，`_announce_minutes()` 一次都不被调到。
        _log.info("%s（场次目录 %s）", MINUTES_GATE_MESSAGE, directory)
        minutes_summary = {"hook": "skipped", "announced": False, "reason": MINUTES_GATE_MESSAGE}
    else:
        minutes_summary = minutes_tail._announce_minutes(
            directory, launcher=minutes_launcher, force=force, announce_minutes=True
        )
    summary = {
        "session_id": session_id,
        "session_dir": str(directory),
        "state": state,
        "interrupted": interrupted,
        "partial": partial,
        "silent": bool(silent_spans),
        "silent_spans": [[round(float(a), 3), round(float(b), 3)] for a, b in silent_spans],
        "gate": gate,
        "sources": list(sources),
        "error": error,
        "total_wall_seconds": round(total_wall, 3),
        "engine": descriptor,
        "report": report_name,
        "status": status_path.name,
        "transcripts": per_source,
        "artifacts": artifacts_rows,
        "minutes": minutes_summary,
    }
    _log.info(
        "会后离线转写结束：%s（%s，%d 路，%.2fs）",
        session_id, state, len(sources), total_wall,
    )
    return summary


def write_interrupted_status(
    session_dir: str | Path,
    *,
    reason: str,
) -> dict[str, Any] | None:
    """把盘上停在 `running` 的那版离线状态补成 `interrupted`（**服务启动核对**用，
     的 d5）：从状态文件 + 逐块记录拼出同一形状的终态，缺口注记拼进 `error`。
    盘上不是 `running` 时**什么都不写**、交回 `None`——核对不许翻掉已落定的终态。
    """
    directory = Path(session_dir)
    status_path = directory / artifacts.STATUS_NAME
    try:
        existing = artifacts.read_status(status_path)
    except artifacts.ArtifactContractError:
        return None
    if str(existing.get("state") or "") != "running":
        return None
    from meeting.offline import progress as progress_module

    session_id = str(existing.get("session_id") or directory.name)
    sources = [str(item) for item in existing.get("sources") or []]
    snapshot = progress_module.snapshot_of(
        directory, sources=sources, session_id=session_id
    )
    per_road = []
    missing_any = False
    for road in snapshot["sources"]:
        total = road["total"]
        per_road.append(
            f"{road['source']} 跑过 {road['done']}/{total if total is not None else '?'} 块"
        )
        if road["remaining"] not in (None, 0):
            missing_any = True
    gap = "；".join(per_road) if per_road else "逐块记录读不出（这一趟可能一块都没落）"
    note = (
        f"{REASON_INTERRUPTED}：{reason}；{gap}；"
        "正式稿与对照表都还没有（已跑块的文字在逐块记录里），重按按钮可从头再跑"
    )
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state="interrupted",
        started_at=str(existing.get("started_at") or _now_iso()),
        sources=sources,
        artifacts=dict(existing.get("artifacts") or {}),
        finished_at=_now_iso(),
        error=note,
        partial=missing_any,
        silent=bool(existing.get("silent")),
        silent_spans=list(existing.get("silent_spans") or []) or None,
        gate=dict(existing.get("gate") or {}) or None,
    )
    return artifacts.read_status(status_path)


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
    progress: OfflineProgressLedger | None = None,
    cancel: Any = None,
) -> artifacts.SourceMetrics:
    """跑一路：取音频 → 算切法 → 逐块调引擎 → 编号 → 落两份按路产物 → 交回度量。

    本函数**不抛**：任何失败记进度量（`state = "failed"` + `error`），另一路照常跑；
    返回时 `per_source` 里跑成与没跑成都有那一格（没跑成是 `_failed_source_row()` 的简版）。
    被打断那一路也有一格（`state = "interrupted"`），但正式稿与对照表不写——那两份是
    「整路跑完」才写的产物，半份稿缺哪几段的说明只有完整跑完才写得周全。
    分块一块失败落 `partial`、全部失败落 `failed`（`all_chunks_failed`）但两块产物照落；
    单趟失败不落产物。引擎加载失败（`engine_load_error`）一块都没交给引擎、产物不落；
    整路数字静音时这一档不现形（模型不加载，终态 `done` + `silent: true`）。
    没声跳过的块不算失败不算缺段：`state` 仍 `done`、`partial` 仍 `false`。
    """
    metric = artifacts.SourceMetrics(source=source, state="running")
    started = time.monotonic()
    paths, note = resolve_audio(directory, source)
    segments: list[OfflineSegment] = []
    wrote_artifacts = False
    stopped = False

    if paths is None:
        metric.state = "failed"
        metric.error = note
        metric.reasons[
            REASON_NO_AUDIO if REASON_NO_AUDIO in (note or "") else REASON_EMPTY_PCM
        ] = 1
        _log.warning("会后离线：%s 这一路没有可用音频（%s）", source, note)
    elif engine is None:
        # 引擎没造出来同样是「加载」档，不是「跑块抛错」。
        metric.state = "failed"
        metric.error = f"{REASON_ENGINE_LOAD_ERROR}：引擎不可用（{engine_error}）"
        metric.reasons[REASON_ENGINE_LOAD_ERROR] = 1
    else:
        if note:
            _log.info("会后离线：%s 的音频取 %s（%s）", source, paths[0], note)
        # 切法算不出来时一块都没跑过，与逐块失败分开记。
        try:
            metric.audio_file = _audio_file_label(paths)
            plan, waveform, decode_attempted = _plan_for_source(
                directory, source, paths, config=config
            )
            metric.audio_seconds = plan.total_seconds
            if progress is not None:
                # 分母来自切块计划，不因某一块失败或跳过而增减。
                progress.start_source(source, len(plan.chunks))
            segments, runs, load_failure, stopped = _execute_chunks(
                directory,
                source,
                paths,
                engine=engine,
                plan=plan,
                waveform=waveform,
                decode_attempted=decode_attempted,
                progress=progress,
                cancel=cancel,
            )
        except Exception as exc:  # 切法/解码抛错：这一路失败，另一路照跑
            metric.state = "failed"
            metric.audio_file = _audio_file_label(paths)
            metric.error = f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}"
            metric.reasons[REASON_ENGINE_ERROR] = 1
            if progress is not None and not progress.has_source(source):
                # 切法还没算出来也要在记录里留一行，否则「一块没跑」与「没跑成」分不开。
                progress.record_chunk(
                    ChunkProgress(
                        source=source,
                        index=1,
                        offset_seconds=0.0,
                        seconds=round(float(metric.audio_seconds), 3),
                        state="failed",
                        error=metric.error,
                    )
                )
            _log.exception("会后离线：%s 这一路引擎抛错（如实登记，继续跑另一路）", source)
        else:
            if stopped:
                # 块边界命中取消旗标：带着已跑块就地收场，未跑块进缺失区间（只供人读）。
                metric.reasons[REASON_INTERRUPTED] = 1
                metric.state = "interrupted"
                _record_chunk_runs(metric, runs)
                ran_indexes = {run.index for run in runs}
                for step in plan.chunks:
                    if step.index in ran_indexes:
                        continue
                    metric.missing_spans.append(
                        (float(step.offset), float(step.offset) + float(step.seconds))
                    )
                metric.partial = bool(metric.missing_spans)
                metric.error = (
                    f"{REASON_INTERRUPTED}：跑到第 {len(runs)}/{len(plan.chunks)} 块被停下"
                    f"（前 {len(runs)} 块的文字在逐块记录里；这一路的正式稿与对照表没有落）"
                )
                _log.warning(
                    "会后离线：%s 被打断：已跑 %d/%d 块，下一块开跑前收场",
                    source, len(runs), len(plan.chunks),
                )
            elif load_failure is not None:
                # 引擎加载档：一块都没跑，按 engine_load_error 记，不折成笼统的 engine_error。
                metric.state = "failed"
                metric.error = _load_failure_note(engine, load_failure)
                metric.reasons[REASON_ENGINE_LOAD_ERROR] = 1
            else:
                _record_chunk_runs(metric, runs)
                metric.gate = _chunk_gate(runs)
                # 零时长段逐路数一遍（= 逐块之和）：只数不改，见 `ChunkRun.zero_span_segments`。
                metric.zero_span_segments = sum(run.zero_span_segments for run in runs)
                # 跑成与没声跳过都算「留下了有用的东西」，一块都没剩下才 failed。
                ran = any(run.error is None for run in runs)
                if not ran:
                    metric.state = "failed"
                    metric.error = (
                        f"{REASON_ALL_CHUNKS_FAILED}：{_chunk_error_note(runs)}"
                    )
                    _log.error(
                        "会后离线：%s 这一路的 %d 块一块都没跑成（%s）",
                        source, len(runs), metric.error,
                    )
                # 分块且一块都没跑成时两块产物照落：不留的话盘上同名文件是**上一趟**的旧表，
                # 读方会当成这一趟的结果（`250906` 两路跑进同一目录撞过）。
                # 单趟失败不落产物（没有「哪几块没成」可说）；没声那一档照落
                # （0 段转写 + 注释是正常结果，与「引擎没吐东西」是两件事）。
                wrote_artifacts = ran or bool(metric.silent_spans) or len(runs) > 1

    if wrote_artifacts:
        if metric.state == "running":
            # 上面那支已把终态定成 failed，不许盖成 done：「14 块全没成」与「没人说话」分得开。
            metric.state = "done"
        _write_source_artifacts(
            directory,
            source,
            session_id=session_id,
            descriptor=descriptor,
            segments=segments,
            metric=metric,
            per_source=per_source,
        )
    else:
        # 一块产物都没落的那一路也在状态文件里留同形一格（少的是产物名与读数），
        # 产物文件照旧一个不落（不留空文件充数）。
        per_source[source] = _failed_source_row(metric)

    metric.wall_seconds = time.monotonic() - started
    if metric.audio_seconds > 0:
        metric.rtf = metric.wall_seconds / metric.audio_seconds
    return metric


def _plan_for_source(
    directory: Path, source: str, paths: "str | Sequence[str]", *, config: Any = None
) -> tuple[ChunkPlan, Any, bool]:
    """算这一路的切法：归档 PCM 的时长按各段字节数之和（不读内容，几百 MB 不为先量
    一遍读一遍）；别的容器解一遍取样本数，解出的波形一并交回——单趟过门与分块切块
    共用同一次解码，同一个文件不为不同理由解两遍。第三个交回值 = 试过解码没有：
    试过而没成就不再试第二遍（同一句话不打两遍日志）。量长度失败**退回单趟**（时长 0）：
    解不出是引擎该报的错，在这里替它下结论会让「引擎交回什么」变成「量长度成没成」。
    """
    del directory, source  # 切法只认路径与块长，两路之间没有别的差别
    audio_paths = _audio_paths(paths)
    chunk_seconds = read_chunk_seconds(config)
    if audio_paths[0].suffix.lower() == PCM_SUFFIX:
        # 归档 PCM 不解波形：门按字节扫（比解一遍便宜），切块按字节定位读
        try:
            total = _pcm_sample_count(audio_paths) / PCM_SAMPLE_RATE
        except OSError:
            _log.warning(
                "会后离线：%s 的时长量不出来（退回整段一次跑，交给引擎如实报错）",
                paths, exc_info=True,
            )
            return plan_chunks(0.0, chunk_seconds), None, False
        return plan_chunks(total, chunk_seconds), None, False
    try:
        waveform = _decode_samples(audio_paths)
        total = float(waveform.size) / PCM_SAMPLE_RATE
    except Exception:
        _log.warning(
            "会后离线：%s 的时长量不出来（退回整段一次跑，交给引擎如实报错）",
            paths, exc_info=True,
        )
        return plan_chunks(0.0, chunk_seconds), None, True
    return plan_chunks(total, chunk_seconds), waveform, True


def _execute_chunks(
    directory: Path,
    source: str,
    paths: "str | Sequence[str]",
    *,
    engine: Any,
    plan: ChunkPlan,
    waveform: Any = None,
    decode_attempted: bool = False,
    progress: OfflineProgressLedger | None = None,
    cancel: Any = None,
) -> tuple[list[OfflineSegment], list[ChunkRun], str | None, bool]:
    """按切法逐块调引擎，交回 `(拼回整场时间轴的分段, 逐块登记, 加载失败原话, 被打断没有)`。

    第四个值是打断档：`cancel` 在**逐块主循环顶**检查，当前块跑完、下一块
    开跑前收场——引擎是同步推理，外部掐不动正在跑的 CUDA 调用，块级 `except` 还会把
    异常吞成块失败，用异常表达取消行不通。单趟与分块共用主循环，打断不开特例。

    单趟（块数为 1）不切块：恰好一段喂原始路径（产物与加 `post.chunk_seconds` 之前逐字
    一致），多于一段按序拼成临时 wav。解好的波形喂那份 wav，不再解原文件。每一块都先过
    数字静音门（`measure_span()`）；门判不了照常送引擎，并把「判不了」记进 `ChunkRun`。
    任何一块抛错就地收下继续跑：一块坏不废已跑的几十分钟，但必须露在产物里。
    跑完清临时块目录；产物留的是逐块登记，音频本身可重切。

    第三个交回值是引擎加载档：探测**推迟到第一块真要送引擎那一刻**——整路数字静音时
    探都不探，模型一次都不加载（design-intent 第 21 条）。
    """
    audio_paths = _audio_paths(paths)
    steps = plan.chunks
    materialize = len(steps) > 1
    series_concat = not materialize and len(audio_paths) > 1
    if materialize or series_concat:
        clear_chunk_dir(directory)
    # 非 PCM 音频现解一次（分块切片、单趟过门共用）；解不开不算这一路失败，
    # 门记「判不了」后照常送原文件，由引擎给出权威说法。
    if waveform is None and not decode_attempted and audio_paths[0].suffix.lower() != PCM_SUFFIX:
        try:
            waveform = _decode_samples(audio_paths)
        except Exception:
            _log.warning(
                "会后离线：%s 解不出波形，门在这一段上判不了（照常送引擎）",
                paths, exc_info=True,
            )
    waveform_length = None if waveform is None else len(waveform)

    # 加载档单独收，但等第一块真要送引擎才探：探早了会把整路数字静音的场判成加载失败
    # （静音本身什么都没出错），还白把几 GB 权重拉进显存。探一次，结论全程复用。
    load_probed = False
    load_failure: str | None = None

    merged: list[OfflineSegment] = []
    runs: list[ChunkRun] = []
    stopped = False
    try:
        for step in steps:
            if cancel is not None and cancel():
                stopped = True
                _log.info(
                    "会后离线：%s 命中取消旗标，第 %d 块不再开跑（已跑 %d/%d 块）",
                    source, step.index, len(runs), len(steps),
                )
                break
            try:
                target = (
                    _materialize_chunk(directory, audio_paths, step, waveform)
                    if materialize
                    else (
                        str(audio_paths[0])
                        if len(audio_paths) == 1
                        else _materialize_series(directory, audio_paths, waveform)
                    )
                )
                # 单趟与分块走同一个门（C1）
                measured = measure_span(
                    audio_paths,
                    offset=step.offset,
                    seconds=step.seconds,
                    waveform=waveform,
                    waveform_length=waveform_length,
                    decode_attempted=decode_attempted,
                )
                if measured.judged is False:
                    # 「判不了」也要露在产物里；已解好波形就喂那份 wav（解码已花掉），
                    # 否则喂原路径由引擎如实报错。
                    if not materialize and waveform is not None:
                        target = str(write_chunk_wav(chunk_file(directory, 1), waveform))
                    _log.warning(
                        "会后离线：%s 第 %d 块（%.0fs 起）的静音与否判不了（%s），照常送引擎",
                        source, step.index, step.offset, measured.detail,
                    )
                gate_verdict = (
                    GATE_UNJUDGED if measured.silent is None
                    else (GATE_SILENT if measured.silent else GATE_NOT_SILENT)
                )
            except Exception as exc:
                run = ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    reason=REASON_ENGINE_ERROR,
                    error=f"{REASON_ENGINE_ERROR}：切块失败 {type(exc).__name__}: {exc}",
                )
                runs.append(run)
                _note_progress(progress, source, run, spans=[])
                _log.exception("会后离线：%s 第 %d 块切不出来", source, step.index)
                continue

            if measured.silent:
                # 这一段没声：引擎调用一次都不发生，记「跳过」不记「失败」。
                run = ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    reason=REASON_DIGITAL_SILENCE,
                    silent=True,
                    gate=gate_verdict,
                    gate_method=measured.method,
                )
                runs.append(run)
                _note_progress(progress, source, run, spans=[])
                _log.info(
                    "会后离线：%s 第 %d 块（%.0fs 起）是数字静音，跳过引擎调用",
                    source, step.index, step.offset,
                )
                continue

            # 第一块真要送引擎了才探这一次加载；失手按加载档收场。
            if not load_probed:
                load_probed = True
                load_failure = _ensure_engine_loaded(engine)
                if load_failure is not None:
                    # 在逐块记录上留一行：否则读方分不开「一块没跑」与「没跑成」；
                    # 记录里的 total 由切块计划定，不因某一块失败而增减（contract 4.9）。
                    run = ChunkRun(
                        index=step.index,
                        offset=step.offset,
                        seconds=step.seconds,
                        reason=REASON_ENGINE_LOAD_ERROR,
                        error=_load_failure_note(engine, load_failure),
                    )
                    _note_progress(progress, source, run, spans=[])
                    _log.error(
                        "会后离线：%s 这一路引擎没加载起来（%s）", source, load_failure
                    )
                    return [], [], load_failure, False

            begun = time.monotonic()
            try:
                produced = list(engine.transcribe(target))
            except Exception as exc:  # 这一块失败：登记，下一块照跑
                run = ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    wall_seconds=time.monotonic() - begun,
                    reason=REASON_ENGINE_ERROR,
                    error=f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}",
                    gate=gate_verdict,
                    gate_method=measured.method,
                    gate_detail=measured.detail if measured.silent is None else None,
                )
                runs.append(run)
                _note_progress(progress, source, run, spans=[])
                _log.warning(
                    "会后离线：%s 第 %d 块（%.0fs 起）引擎抛错，如实登记后继续",
                    source, step.index, step.offset,
                )
                continue

            wall = time.monotonic() - begun
            if not produced:
                run = ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    wall_seconds=wall,
                    reason=REASON_NO_SEGMENTS,
                    error=f"{REASON_NO_SEGMENTS}：引擎交回 0 段",
                    gate=gate_verdict,
                    gate_method=measured.method,
                    gate_detail=measured.detail if measured.silent is None else None,
                )
                runs.append(run)
                _note_progress(progress, source, run, spans=[])
                _log.warning(
                    "会后离线：%s 第 %d 块（%.0fs 起）一段都没交回，如实登记后继续",
                    source, step.index, step.offset,
                )
                continue

            # 零时长段逐块数一遍：整路一格是这些数之和，读方看得出是哪一块出的形状。
            zero_span = sum(
                1 for segment in produced
                if float(segment.end) <= float(segment.start)
            )
            run = ChunkRun(
                index=step.index,
                offset=step.offset,
                seconds=step.seconds,
                segments=len(produced),
                wall_seconds=wall,
                reason=(
                    # 门判不了且跑成 → gate_unjudged；其次零时长段；否则 transcribed
                    REASON_GATE_UNJUDGED if gate_verdict == GATE_UNJUDGED
                    else (REASON_ZERO_SPAN_SEGMENTS if zero_span else REASON_TRANSCRIBED)
                ),
                gate=gate_verdict,
                gate_method=measured.method,
                gate_detail=measured.detail if measured.silent is None else None,
                zero_span_segments=zero_span,
            )
            runs.append(run)
            shifted = [
                _shift_segment(segment, step, namespaced=plan.namespace)
                for segment in produced
            ]
            # 这一块的文字与落点当场落进进度记录：块一跑完字就在盘上，正式稿照旧整路一次写。
            _note_progress(progress, source, run, spans=shifted)
            merged.extend(shifted)
    finally:
        if materialize or series_concat:
            clear_chunk_dir(directory)
    return merged, runs, None, stopped


def _materialize_chunk(
    directory: Path, audio_paths: "Sequence[Path]", step: ChunkStep, waveform: Any
) -> str:
    """把第 `step` 块的音频写成临时 wav。PCM 走**按字节定位**（450 秒块占 14 MB，与
    整段多长无关），别的容器用已解好的波形切片。读完短于标称块长（收尾被截断、采集侧
    少写尾巴）时按实际样本数写：不补零、不报错，秒数由实际样本数说话。
    """
    target = chunk_file(directory, step.index)
    if waveform is not None:
        import numpy as np

        start = int(round(float(step.offset) * PCM_SAMPLE_RATE))
        end = int(round((float(step.offset) + float(step.seconds)) * PCM_SAMPLE_RATE))
        piece = np.asarray(waveform)[start:end]
        return str(write_chunk_wav(target, piece))

    if audio_paths[0].suffix.lower() != PCM_SUFFIX:
        # 非 PCM 又没有波形（量长度那一步没解出来）：按字节读会把容器字节当 int16 计数
        # 误读成噪声写进 wav——如实抛，调用方按块失败登记，坏容器不出手。
        raise ValueError(
            f"{audio_paths[0].name} 解不出波形，切不出可用的块"
            "（非 PCM 的块只认已解好的波形）"
        )
    samples = pcm_sample_chunk_counts(
        audio_paths, offset=step.offset, seconds=step.seconds
    ).astype("<i2")
    # PCM 读出的已是 int16 计数，换算回 [-1, 1] 走 write_chunk_wav 的 ×32767
    # （与 32768 差在最低位上，无损可逆）。
    return str(write_chunk_wav(target, samples.astype("float32") / 32768.0))


def _materialize_series(directory: Path, audio_paths: "Sequence[Path]", waveform: Any) -> str:
    """单趟跑且**多于一段**时，把各段按序拼成一份临时 wav（引擎的入参必须是一条轴）。

    有已解好的波形就按波形写（与 `_materialize_chunk()` 的波形分支同一条）：MP3 等
    容器的裸字节不是 int16 PCM，按字节拼会拼出「头说 PCM、数据区是容器字节」的坏容器
    送进引擎。归档 PCM 不解波形，保持流式按字节拼：逐块跨段读、读多少写多少，内存里
    永远只有一个块（两小时的 PCM 也不整段进内存）。非 PCM 又解不出波形时如实抛——
    调用方按块失败登记，坏容器不出手（offline-mp3-fallback-series-corrupt-input）。
    临时文件落在分块目录里，跑完由 `_execute_chunks()` 的收尾一并扫掉。
    """
    target = chunk_file(directory, 1)
    if waveform is not None:
        return str(write_chunk_wav(target, waveform))
    if audio_paths[0].suffix.lower() != PCM_SUFFIX:
        raise ValueError(
            "多段非 PCM 音频解不出波形，拼不成一条可用的时间轴"
            f"（{audio_paths[0].name} 等 {len(audio_paths)} 个段文件）"
        )
    import wave

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(PCM_SAMPLE_WIDTH)
        handle.setframerate(PCM_SAMPLE_RATE)
        for path in audio_paths:
            with open(path, "rb") as src:
                while True:
                    block = src.read(_PCM_SCAN_BLOCK_BYTES)
                    if not block:
                        break
                    if len(block) % PCM_SAMPLE_WIDTH:
                        # 残缺的尾巴（不到一个样本）进不了 wav：按 int16 对齐截掉
                        block = block[: len(block) - (len(block) % PCM_SAMPLE_WIDTH)]
                    if block:
                        handle.writeframes(block)
    return str(target)


def _shift_segment(segment: OfflineSegment, step: ChunkStep, *, namespaced: bool) -> OfflineSegment:
    """把块内分段搬到**原录音时间轴**（start/end 加块偏移；末块 end 可能略超块末——引擎
    偶尔把结束时间戳写到块末之外，照实平移、不夹），标签按需加 `cNN:` 前缀不撞名。
    """
    return OfflineSegment(
        start=round(float(segment.start) + float(step.offset), 3),
        end=round(float(segment.end) + float(step.offset), 3),
        speaker=(
            chunk_label(step.index, segment.speaker) if namespaced else str(segment.speaker)
        ),
        text=segment.text,
    )


def _entry_state(run: ChunkRun) -> str:
    """这一块在**进度记录**上落哪一档（四档，见 `progress.CHUNK_STATES`）；逐块登记更细，
    这格只让人一眼看出跑成了、跳过了、还是没成。
    """
    if run.silent:
        return "silent"
    if run.error is not None:
        return "failed"
    if run.gate == GATE_UNJUDGED:
        return "unjudged"
    return "transcribed"


def _note_progress(
    progress: OfflineProgressLedger | None,
    source: str,
    run: ChunkRun,
    *,
    spans: Sequence[OfflineSegment],
) -> None:
    """把这一块落进进度记录（`progress=None` 什么都不做）：这一块的文字 + 落点。
    说话人标签是 `cNN:Sxx`，与正式稿的编号（整路内按首次出现）不是同一个东西。
    落盘失败不带走这一趟（`record_chunk()` 内部兜写盘异常，这里再兜调用方意外）。
    """
    if progress is None:
        return
    text = "\n".join(
        artifacts.format_transcript_line(span.start, span.speaker, span.text)
        for span in spans
    )
    try:
        progress.record_chunk(
            ChunkProgress(
                source=source,
                index=run.index,
                offset_seconds=run.offset,
                seconds=run.seconds,
                state=_entry_state(run),
                text=text,
                segments=int(run.segments),
                wall_seconds=run.wall_seconds,
                error=run.error,
            )
        )
    except Exception:  # pragma: no cover - 进度记录是附注，出意外也不许带走这一趟
        _log.warning(
            "会后离线：%s 第 %d 块的进度没记上（不影响这一趟）",
            source, run.index, exc_info=True,
        )


def _record_chunk_runs(metric: artifacts.SourceMetrics, runs: Sequence[ChunkRun]) -> None:
    """逐块登记写进度量：失败块进 `missing_spans`/`reasons` 并立 `partial`（按块覆盖的
    区间记，不按引擎吐的句子——OOM 时没有段落能说明它覆盖了哪里）。没声块进
    `silent_spans`、门判不了块进 `unjudged_spans`，两批都不进缺失区间、不立 partial
    （什么都没出错），与缺失区间不相交。
    """
    metric.chunks = [run.to_dict() for run in runs]
    for run in runs:
        if run.silent:
            metric.silent_spans.append(run.span)
            metric.reasons[REASON_DIGITAL_SILENCE] = (
                metric.reasons.get(REASON_DIGITAL_SILENCE, 0) + 1
            )
            continue
        if run.gate == GATE_UNJUDGED:
            metric.unjudged_spans.append(run.span)
            metric.reasons[REASON_GATE_UNJUDGED] = (
                metric.reasons.get(REASON_GATE_UNJUDGED, 0) + 1
            )
        if run.error is None:
            continue
        metric.missing_spans.append(run.span)
        metric.reasons[run.reason] = metric.reasons.get(run.reason, 0) + 1
    metric.partial = bool(metric.missing_spans)


def _chunk_gate(runs: Sequence[ChunkRun]) -> dict[str, Any]:
    """这一路的门做成没做成判断（逐路 `gate` 格）：`applied` = 有没有一段没判成；
    `detail` 按这一趟实际发生的事写（判过几块、跳过几块、判不了几块）。
    """
    judged = [run for run in runs if run.gate != GATE_UNJUDGED]
    unjudged = [run for run in runs if run.gate == GATE_UNJUDGED]
    skipped = [run for run in runs if run.silent]
    if not runs:
        # 空 runs 的缺省形状唯一出处（artifacts.default_source_gate()），不在这里手写
        return artifacts.default_source_gate()
    methods = sorted({run.gate_method for run in judged if run.gate_method})
    if unjudged and skipped:
        detail = (
            f"{len(unjudged)} 块的静音与否判不了，照常送了引擎；"
            f"{len(judged)} 块判过，其中 {len(skipped)} 块是数字静音（跳过了引擎）"
        )
    elif unjudged:
        detail = f"{len(unjudged)} 块的静音与否判不了，照常送了引擎"
    elif skipped:
        detail = f"{len(judged)} 块判过，其中 {len(skipped)} 块是数字静音（跳过了引擎）"
    else:
        detail = f"{len(judged)} 块判过，没有一块是数字静音（一块都没跳过引擎）"
    return {
        "applied": not unjudged,
        "method": "/".join(methods) if methods else None,
        "spans_total": len(runs),
        "spans_judged": len(judged),
        "spans_unjudged": len(unjudged),
        "unjudged_spans": [list(run.span) for run in unjudged],
        "unjudged_reason": unjudged[0].gate_detail if unjudged else None,
        "detail": detail,
    }


def _aggregate_gate(metrics: Sequence[artifacts.SourceMetrics]) -> dict[str, Any]:
    """整场的门做成没做成判断（状态文件顶层 `gate` 格）；`applied` 假时逐路列出
    哪些路的哪几段判不了。
    """
    unjudged = {
        item.source: list(item.gate.get("unjudged_spans", []))
        for item in metrics
        if item.gate.get("spans_unjudged")
    }
    judged = sum(int(item.gate.get("spans_judged", 0)) for item in metrics)
    total = sum(int(item.gate.get("spans_total", 0)) for item in metrics)
    return {
        "applied": all(bool(item.gate.get("applied")) for item in metrics) if metrics else False,
        "spans_total": total,
        "spans_judged": judged,
        "spans_unjudged": total - judged,
        "unjudged_spans": unjudged,
    }


def _chunk_error_note(runs: Sequence[ChunkRun]) -> str:
    """「哪些块没跑成、为什么」一句话（每块跟自己的原因）；一块都没失败（全是静音跳过）
    时如实说明这一档不是失败。
    """
    failed = [run for run in runs if run.error is not None]
    if not failed:
        return "没有一块失败（那几块是数字静音，跳过了引擎调用）"
    detail = "、".join(
        f"第 {run.index} 块（{run.span[0]:.1f}–{run.span[1]:.1f}s）没跑成：{run.error}"
        for run in failed
    )
    return f"{len(failed)}/{len(runs)} 块没跑成：{detail}"


def _missing_spans_note(spans: Sequence[Sequence[float]]) -> str:
    """把缺失区间列成一句话：`0.0–450.0s、900.0–1350.0s`（秒级与报告/状态文件一致）。"""
    listed = "、".join(
        f"{float(item[0]):.1f}–{float(item[1]):.1f}s" for item in spans
    )
    return f"缺 {len(spans)} 段：{listed}"


def _failure_reasons(metric: artifacts.SourceMetrics) -> set[str]:
    """这一路缺区间的原因词档（读逐块的 `reason` 格），让人分得清「显存不够」与
    「一段都没交回」。
    """
    reasons: set[str] = set()
    for run in metric.chunks:
        if not run.get("error"):
            continue
        word = str(run.get("reason") or "")
        if word:
            reasons.add(word)
    return reasons


def _write_failed_status(
    status_path: Path,
    *,
    session_id: str,
    started_at: str,
    sources: Sequence[str],
    per_source: dict[str, dict[str, Any]],
    failure: BaseException,
) -> None:
    """兜底状态文件：从这一趟逃出的任何异常都落一版 `failed`。`running` 落得早，
    三种情况都兜得住：抛在 `running` 之后（翻成 failed，已记条目原样留）、抛在之前
    （连 running 一起补——盘上不出现「压根没有状态文件」档）、跑过几路（产物名照旧带在
    `artifacts` 里）。没跑到的路 `reason = REASON_NOT_RUN`（reasons 空、error null），
    不兜成 `engine_error` 才分得开「没跑」与「跑了没成」。写盘再失败只记 log，
    原异常由调用方照旧上抛。
    """
    rows = dict(per_source)
    for source in sources:
        rows.setdefault(
            source,
            _failed_source_row(
                artifacts.SourceMetrics(
                    source=source, state="failed", reasons={REASON_NOT_RUN: 1}
                )
            ),
        )
    note = f"{type(failure).__name__}: {failure}"
    try:
        artifacts.write_status(
            status_path,
            session_id=session_id,
            state="failed",
            started_at=started_at,
            sources=list(sources),
            artifacts={
                source: _status_artifact_row(row, report=None)
                for source, row in rows.items()
            },
            finished_at=_now_iso(),
            error=f"{REASON_ENGINE_ERROR}：这一趟从半路抛出来了（{note}）",
        )
    except Exception:  # pragma: no cover - 写盘再失败时只留 log，原异常照旧上抛
        _log.exception("会后离线：兜底那一版 failed 没写下去（%s）", status_path)
    else:
        _log.error("会后离线：这一趟抛出来了，状态文件已如实落成 failed（%s）", note)




def _ensure_engine_loaded(engine: Any) -> str | None:
    """探一次引擎加载，交回 `None`（好了）或失败原话：真引擎读 `load_error`，假件探
    `_load()`（两样都没有则当无加载档，交给逐块既有路径）。这一探会把模型真的拉起来，
    调用点只在第一块真要送引擎时（整路静音一次都不该调，design-intent 第 21 条）。
    """
    read = getattr(engine, "load_error", None)
    if read is not None:
        return str(read)
    probe = getattr(engine, "_load", None)
    if callable(probe):
        try:
            probe()
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
    return None


def _load_failure_note(engine: Any, failure: str | None = None) -> str:
    """加载失败那句话：`engine_load_error：<原话>`（词档与逐块 engine_error 分开）。"""
    detail = failure if failure is not None else _ensure_engine_loaded(engine)
    return f"{REASON_ENGINE_LOAD_ERROR}：{detail or '引擎没能加载起来'}"


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
    """落这一路的两份产物（转写 + 对照表），并把文件名记进度量与摘要。

    编号来自 `meeting/offline/speakers.py`（本路内按首次出现顺序，`source` 进限定名）；
    分块分段已带 `cNN:` 命名空间（见 `_shift_segment`），天然不撞名。`metric.partial`
    时转写第一行是缺失注释行（下游与程序都在第一行看到），没声区间另起**一条**注释行
    （两份注释各说一件事）。每一块都没跑成也走这里：空表 + 对照表的 `state`/`error`
    说清「这一趟失败」，`speakers` 的 0 类 0 段不会被当成正常但没人说话。
    """
    spans = number_speakers(source, segments)
    summary = artifacts.speaker_rows(
        spans, raw_labels=[segment.speaker for segment in segments]
    )
    transcript_name = artifacts.TRANSCRIPT_NAME_TEMPLATE.format(source=source)
    speakers_name = artifacts.SPEAKERS_NAME_TEMPLATE.format(source=source)

    lines = [
        artifacts.format_transcript_line(span.start, span.local, span.text)
        for span in spans
    ]
    # 零时长段只数不改：解析层对「漏写结束标签」的正常形状，实测假正文每段都是零时长
    # （有用的异常信号），正文一个字不动。
    zero_span = sum(1 for span in spans if float(span.end) <= float(span.start))
    if metric.partial:
        lines.insert(0, artifacts.incomplete_note(metric.missing_spans))
    if metric.silent_spans:
        lines.insert(0, artifacts.silent_note(metric.silent_spans))
    artifacts.write_transcript(directory / transcript_name, lines)
    document = artifacts.build_speakers_document(
        session_id=session_id,
        source=source,
        engine=descriptor,
        audio={
            "file": metric.audio_file,
            "seconds": round(metric.audio_seconds, 3),
            "chunks": len(metric.chunks) if metric.chunks else 1,
            "missing_spans": [list(item) for item in metric.missing_spans],
            "partial": bool(metric.partial),
            # 没声区间与零时长条数分开记：读方分得开「没音频」「漏结束标签」「丢了」
            "silent": bool(metric.silent_spans),
            "silent_spans": [list(item) for item in metric.silent_spans],
            "zero_span_segments": zero_span,
            # 门做成没做成判断：applied=false 时与健康运行不同形
            "gate": dict(metric.gate),
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
        chunks=[artifacts.chunk_row(run) for run in metric.chunks],
        state=metric.state,
        error=metric.error,
    )
    artifacts.write_speakers(directory / speakers_name, document)

    metric.segments = len(spans)
    metric.speakers = len(summary)
    metric.seconds = sum(float(row["seconds"]) for row in summary)
    metric.zero_span_segments = zero_span
    per_source[source] = {
        "transcript": transcript_name,
        "speakers": speakers_name,
        "segments": len(spans),
        "speaker_count": len(summary),
        "audio_seconds": round(metric.audio_seconds, 3),
        # 终态进这一格：读方不用回头再看整场 state 就知道这一路是空表
        "state": metric.state,
        "partial": bool(metric.partial),
        "missing_spans": [list(item) for item in metric.missing_spans],
        "silent": bool(metric.silent_spans),
        "silent_spans": [list(item) for item in metric.silent_spans],
        "gate": dict(metric.gate),
    }


def _now_iso() -> str:
    """现在的 ISO 时刻：naive 本地时间、秒精度。与 `meeting/state.py` 的 aware 版不同形：
    单机单时区的工具与读方都在本机，沿用现状、不 import 它。
    """
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "CHUNK_DIR_NAME",
    "CHUNK_LABEL_TEMPLATE",
    "CHUNK_SECONDS_OFF",
    "ChunkPlan",
    "ChunkRun",
    "ChunkStep",
    "DEFAULT_CHUNK_SECONDS",
    "DEFAULT_ENGINE_TARGET",
    "EMPTY_PCM_BYTES",
    "GATE_METHOD_DECODE_AT_PLAN",
    "GATE_METHOD_DECODE_NOW",
    "GATE_METHOD_PCM",
    "GATE_METHOD_WAVEFORM",
    "GATE_NOT_SILENT",
    "GATE_SILENT",
    "GATE_UNJUDGED",
    "MIN_GATE_SAMPLES",
    "MeasureResult",
    "PCM_SAMPLE_RATE",
    "PCM_SAMPLE_WIDTH",
    "REASON_ALL_CHUNKS_FAILED",
    "REASON_DIGITAL_SILENCE",
    "REASON_EMPTY_PCM",
    "REASON_ENGINE_ERROR",
    "REASON_ENGINE_LOAD_ERROR",
    "REASON_GATE_UNJUDGED",
    "REASON_NO_AUDIO",
    "REASON_NO_SEGMENTS",
    "REASON_NOT_RUN",
    "REASON_TRANSCRIBED",
    "REASON_ZERO_SPAN_SEGMENTS",
    "SILENT_PEAK_FLOOR",
    "SOURCE_IDS",
    "audio_path_for",
    "build_engine",
    "chunk_file",
    "chunk_label",
    "clear_chunk_dir",
    "declared_sources",
    "engine_descriptor",
    "full_scale_counts",
    "is_digital_silence",
    "measure_span",
    "pcm_sample_chunk_counts",
    "plan_chunks",
    "read_chunk_seconds",
    "resolve_audio",
    "resolve_device_of",
    "resolve_engine",
    "run_session",
    "samples_to_count_floats",
    "session_id_of",
    "write_chunk_wav",
]
