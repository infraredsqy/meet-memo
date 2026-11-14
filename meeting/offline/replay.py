"""重放验证工具：拿归档录音全速重跑一遍会后离线链路。

`meeting replay <场次目录>` 背后那一层：把已有场次的素材复制/截断进工具自己的临时落地，
在拷贝上跑**与生产同一条** `run_session()`，打印实时率、逐块耗时与失败原因分布。
只做验证，链上代码一个字不动。三条边界：

1. **素材只读**：链必写它被指到的目录，唯一干净落法是工具侧把素材复制/截断进自己的
   临时目录再对拷贝跑链——PCM 按字节截断；MP3 用现成 `decode_audio()`
   解开切前 N 秒写成裸 s16le 段 PCM（链的段 PCM 优先逻辑零改动吃到）。旧素材
   （之前无段号命名）退认旧名、写进拷贝落成现行命名（段 001）。
2. **绝不拉纪要**：直调 `run_session(..., minutes=False)`，不碰 DSH。
3. **读数只给人看**：实时率、墙钟、块计数都是附注，不带校验含义；
   「两遍同量级」的判尺由跑工具的人自己比，本模块不裁断。

音频时长权威口径与链上同一条：**实际截到盘上的音频时长**——
拷贝完成后按字节数现量，不读旧产物、不读 `session.json`。
"""

from __future__ import annotations

import math
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting.offline import runner as offline_runner
from meeting.offline.runner.session_info import declared_sources, session_id_of
from meeting.offline.audio import PCM_SAMPLE_WIDTH, PCM_SUFFIX, SAMPLE_RATE, decode_audio
from meeting.offline.progress import (
    CHUNK_STATES,
    OfflineProgressLedger,
    counts_by_state,
    ledger_path,
    read_progress,
)
from meeting.session import archive_segment_number

#: 重放拷贝的默认落点前缀（系统临时目录下面；每个重放一个新目录）
WORK_DIR_PREFIX = "meeting-replay-"

#: 复制 PCM 时每次读写的块大小（1 MiB；截断复制不整读大文件）
_COPY_BLOCK_BYTES = 1 << 20


def cap_to_seconds(
    *,
    seconds: float | None = None,
    chunks: int | None = None,
    chunk_seconds: float | None = None,
) -> float | None:
    """把两种上限表达换算成**秒上限**；两种都没给交回 `None`（= 整场跑完）。

    - `seconds`：正数（秒）；非正数、非有限数、认不出都拒绝；
    - `chunks`：正整数（块数）× `chunk_seconds` 换算成秒；`chunk_seconds <= 0` 时
      如实拒绝（那格 `0 = 不分块`，块语义塌缩，不硬猜）；
    - 两个都给：拒绝（上限表达不歧义）。
    """
    if seconds is not None and chunks is not None:
        raise ValueError("上限只给一种：--seconds 与 --chunks 别同时给")
    if seconds is not None:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            raise ValueError(f"--seconds 要一个正数，收到 {seconds!r}") from None
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"--seconds 要一个正数，收到 {seconds!r}")
        return value
    if chunks is not None:
        try:
            count = int(chunks)
        except (TypeError, ValueError):
            raise ValueError(f"--chunks 要一个正整数，收到 {chunks!r}") from None
        if count <= 0 or count != chunks:
            raise ValueError(f"--chunks 要一个正整数，收到 {chunks!r}")
        if chunk_seconds is None or chunk_seconds <= 0:
            raise ValueError(
                "按块上限换算不出秒：生效的 post.chunk_seconds 是 "
                f"{chunk_seconds!r}（0 = 不分块，块语义塌缩）；请改用 --seconds"
            )
        return float(count) * float(chunk_seconds)
    return None


@dataclass
class StagedSource:
    """一路截断后的备料读数（截到了几秒、写了哪些段文件）。"""

    source: str
    seconds: float = 0.0
    files: list[str] = field(default_factory=list)
    #: 原目录里这一路为什么没有素材（照链上 `resolve_audio()` 的说法原样带过来）
    note: str | None = None


@dataclass
class StagedMaterial:
    """一份备料结果：拷贝落点 + 逐路截后秒数（盘上现量）。"""

    work_dir: Path
    sources: list[StagedSource] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return round(sum(item.seconds for item in self.sources), 3)

    def seconds_of(self, source: str) -> float:
        for item in self.sources:
            if item.source == source:
                return item.seconds
        return 0.0


def _budget_samples(cap_seconds: float | None) -> int | None:
    """秒上限换算成样本预算（向上取整）；无上限交回 `None`。"""
    if cap_seconds is None:
        return None
    samples = int(math.ceil(float(cap_seconds) * SAMPLE_RATE))
    if samples < 1:
        raise ValueError(f"上限太小，连一个样本都装不下：{cap_seconds} 秒")
    return samples


def _copy_pcm_head(source_path: Path, target_path: Path, budget_bytes: int | None) -> int:
    """把一段归档 PCM 的**前 `budget_bytes` 字节**复制到落点，交回实写字节数。

    归档 PCM 是无头裸流（16k 单声道 s16le），字节级截断就是合法的时间截断；
    实写字节数对齐到整样本（偶数字节）。
    """
    take: int | None = budget_bytes
    size = source_path.stat().st_size
    if take is None or take > size:
        take = size
    take -= take % PCM_SAMPLE_WIDTH
    written = 0
    with open(source_path, "rb") as fin, open(target_path, "wb") as fout:
        while written < take:
            block = fin.read(min(_COPY_BLOCK_BYTES, take - written))
            if not block:
                break
            fout.write(block)
            written += len(block)
    return written


def _write_waveform_head(source_path: Path, target_path: Path, budget_samples: int) -> int:
    """把一段非 PCM 容器（mp3 等）解开后切前 `budget_samples` 个样本写成段 PCM。

    解码用链上现成的 `decode_audio()`（16k 单声道 float32），波形→int16 的换算走
    `runner.full_scale_counts()`（满量程换算的唯一出处；直接 astype 会截成静音）。
    交回实写样本数。
    """
    waveform = decode_audio(source_path)
    head = waveform[:budget_samples]
    if head.size == 0:
        return 0
    pcm = offline_runner.full_scale_counts(head).astype("<i2")
    target_path.write_bytes(pcm.tobytes())
    return int(head.size)


def _segment_number(path: Path, source: str, suffix: str) -> int:
    """段文件名里的序号（`meeting_audio_<source>_<三位段号>`）；认不出时按顺序补位。"""
    stem = path.name[: -len(suffix)] if path.name.lower().endswith(suffix) else path.name
    number = archive_segment_number(stem, source)
    return number if number is not None else 1


def _legacy_audio_paths(source_dir: Path, source: str) -> list[Path]:
    """旧素材的音频文件（**分段归档落地之前**的命名：`meeting_audio_<source>.<扩展名>`）。

    现行链按 `meeting_audio_<source>_<三位段号>` 枚举段（`meeting/session.py` 的
    `segment_archive_paths()`），归档分节点之前的旧录音没有段号（`meeting_audio_mic.mp3`
    这种名字）。重放工具的
    素材面照链上同一条判据（`resolve_audio()`）找不到时，退一步认旧名——**拷贝进
    临时落地时写成现行命名（段 001）**，链对那份拷贝照旧零改动地吃。
    """
    found: list[Path] = []
    for suffix in (PCM_SUFFIX, ".mp3"):
        candidate = source_dir / f"meeting_audio_{source}{suffix}"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                found.append(candidate)
        except OSError:  # pragma: no cover - 素材盘抽风时当作没有这份旧素材
            continue
    return found


def _stage_one_source(
    source_dir: Path,
    work_dir: Path,
    source: str,
    budget_samples: int | None,
) -> StagedSource:
    """备料一路：按链上 `resolve_audio()` 的同一条判据找素材，截断复制成段 PCM。"""
    staged = StagedSource(source=source)
    paths, note = offline_runner.resolve_audio(source_dir, source)
    legacy_note: str | None = None
    if paths is None:
        legacy = _legacy_audio_paths(source_dir, source)
        if legacy:
            paths = [str(path) for path in legacy]
            legacy_note = f"旧素材命名（无段号）：{legacy[0].name}"
            note = None
        else:
            # 素材本来就缺/空：拷贝目录里什么都不写，链对这份拷贝会给出同一条失败原因——
            # 读数照实反映这一路，不替链下结论。
            staged.note = note
            return staged
    if legacy_note:
        staged.note = legacy_note
    remaining = budget_samples
    for path in paths:
        audio_path = Path(path)
        suffix = audio_path.suffix.lower()
        number = _segment_number(audio_path, source, suffix)
        target = work_dir / f"meeting_audio_{source}_{number:03d}.pcm"
        if suffix == PCM_SUFFIX:
            budget_bytes = None if remaining is None else remaining * PCM_SAMPLE_WIDTH
            written_bytes = _copy_pcm_head(audio_path, target, budget_bytes)
            written_samples = written_bytes // PCM_SAMPLE_WIDTH
        else:
            if remaining is None:
                written_samples = _write_waveform_head(
                    audio_path, target, 1 << 62
                )
            else:
                written_samples = _write_waveform_head(audio_path, target, remaining)
        if written_samples <= 0:
            # 这一段一个样本都没分到（预算耗尽）：不落空文件充数
            target.unlink(missing_ok=True)
            continue
        if remaining is not None:
            remaining -= written_samples
        staged.files.append(target.name)
    staged.seconds = _measure_source(work_dir, source)
    return staged


def _measure_source(work_dir: Path, source: str) -> float:
    """拷贝里这一路的音频时长（盘上现量）：各段 PCM 字节数之和 ÷ 2 ÷ 16000。

    与链上量长度的口径一致（`runner._pcm_sample_count()` / `_plan_for_source()`）：
    拷贝里写的全是段 PCM，字节数就是样本数。
    """
    paths = offline_runner.segment_archive_paths(work_dir, source)
    total_bytes = 0
    for path in paths:
        try:
            total_bytes += path.stat().st_size
        except OSError:  # pragma: no cover - 拷贝刚写完就被读不到，不替链猜
            continue
    return round(total_bytes // PCM_SAMPLE_WIDTH / SAMPLE_RATE, 3)


def stage_material(
    source_dir: Path | str,
    work_dir: Path | str,
    cap_seconds: float | None = None,
) -> StagedMaterial:
    """把场次素材复制/截断进 `work_dir`（只带 `session.json` 与各路音频，旧产物不带）。"""
    source = Path(source_dir)
    target = Path(work_dir)
    target.mkdir(parents=True, exist_ok=True)
    session_json = source / "session.json"
    if session_json.exists():
        # 原样复制（元信息不校验、不重写）
        (target / "session.json").write_bytes(session_json.read_bytes())
    budget = _budget_samples(cap_seconds)
    staged = StagedMaterial(work_dir=target)
    for name in declared_sources(source):
        staged.sources.append(_stage_one_source(source, target, name, budget))
    return staged


def default_work_dir(source_dir: Path | str) -> Path:
    """缺省落点：系统临时目录下面、带场次名的一份新目录（每跑一次新开一个）。"""
    root = Path(tempfile.mkdtemp(prefix=WORK_DIR_PREFIX))
    return root / Path(source_dir).name


def run_replay(
    session_dir: Path | str,
    *,
    seconds: float | None = None,
    chunks: int | None = None,
    work_dir: Path | str | None = None,
    engine: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    """对一份已有场次跑一遍重放，交回可读读数（`meeting replay` 与用例共用）。

    备料（截断复制进 `work_dir`，缺省 `default_work_dir()`）→ 立逐块进度记录 →
    直调 `run_session(work_dir, ..., minutes=False)` → 从盘上进度记录读回逐块行 →
    拼读数（实时率 = 墙钟 ÷ 盘上实测音频时长）。`engine` / `config` 照 `run_session()`
    口径透传（用例注入假件用）。
    """
    source = Path(session_dir)
    if not source.is_dir():
        raise ValueError(f"场次目录不存在（或者不是目录）：{source}")
    cap_seconds = cap_to_seconds(
        seconds=seconds,
        chunks=chunks,
        chunk_seconds=(
            offline_runner.read_chunk_seconds(config) if chunks is not None else None
        ),
    )
    target = Path(work_dir) if work_dir is not None else default_work_dir(source)
    staged = stage_material(source, target, cap_seconds)

    sources = declared_sources(source)
    ledger = OfflineProgressLedger(
        target,
        session_id=session_id_of(source),
        sources=sources,
    )
    summary = offline_runner.run_session(
        target,
        engine=engine,
        config=config,
        progress=ledger,
        minutes=False,
    )
    _header, rows = read_progress(ledger_path(target))
    return _build_reading(
        source_dir=source,
        work_dir=target,
        cap_seconds=cap_seconds,
        staged=staged,
        summary=summary,
        rows=rows,
    )


def _build_reading(
    *,
    source_dir: Path,
    work_dir: Path,
    cap_seconds: float | None,
    staged: StagedMaterial,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """把一次重放的结果拼成读数 dict（打印交给 `format_reading()`）。"""
    total_wall = float(summary.get("total_wall_seconds") or 0.0)
    measured = staged.total_seconds
    counts: dict[str, int] = counts_by_state(rows)
    failure_reasons: dict[str, int] = dict(
        Counter(
            str(row.get("error"))
            for row in rows
            if row.get("state") == "failed" and row.get("error")
        )
    )
    per_source: dict[str, dict[str, Any]] = {}
    for item in staged.sources:
        mine = [row for row in rows if str(row.get("source") or "") == item.source]
        wall = round(sum(float(row.get("wall_seconds") or 0.0) for row in mine), 3)
        row_info: dict[str, Any] = {
            "seconds": item.seconds,
            "wall_seconds": wall,
            "chunks": len(mine),
            "note": item.note,
        }
        if item.seconds > 0:
            row_info["rtf"] = round(wall / item.seconds, 4)
        per_source[item.source] = row_info
    reading: dict[str, Any] = {
        "source_dir": str(source_dir),
        "work_dir": str(work_dir),
        "cap_seconds": cap_seconds,
        "measured_seconds": measured,
        "total_wall_seconds": round(total_wall, 3),
        "counts": counts,
        "failure_reasons": failure_reasons,
        "chunks": rows,
        "staged": staged,
        "summary": summary,
        "per_source": per_source,
    }
    reading["rtf"] = (
        round(total_wall / measured, 4) if measured > 0 else None
    )
    return reading


def format_reading(reading: dict[str, Any]) -> str:
    """把读数 dict 排成给人看的几行（**实时率 / 每块耗时 / 失败原因分布**三样都在）。"""
    staged: StagedMaterial = reading["staged"]
    summary: dict[str, Any] = reading.get("summary") or {}
    lines: list[str] = []
    lines.append(f"重放素材：{reading.get('source_dir')}")
    lines.append(f"拷贝落点：{reading.get('work_dir')}（原目录只读，没有动它）")
    if reading.get("cap_seconds") is None:
        lines.append("上限：整场（未截断）")
    else:
        lines.append(f"上限：{reading['cap_seconds']} 秒；截后实测：{reading['measured_seconds']} 秒")
    per_source: dict[str, dict[str, Any]] = reading.get("per_source") or {}
    labeled = "；".join(
        f"{name} {info.get('seconds')} s" for name, info in per_source.items()
    )
    lines.append(f"逐路音频（盘上实测）：{labeled}" if labeled else "逐路音频（盘上实测）：（无）")
    rtf = reading.get("rtf")
    if rtf is None:
        lines.append("实时率（整场）：量不出来（截后音频为 0 秒）")
    else:
        lines.append(
            f"实时率（整场）：{rtf}x"
            f"（墙钟 {reading.get('total_wall_seconds')} s / 音频 {reading['measured_seconds']} s）"
        )
    source_rtf = "；".join(
        f"{name} {info['rtf']}x"
        for name, info in per_source.items()
        if info.get("rtf") is not None
    )
    if source_rtf:
        lines.append(f"逐路实时率：{source_rtf}")
    rows: list[dict[str, Any]] = reading.get("chunks") or []
    if rows:
        lines.append(f"每块耗时（{len(rows)} 块）：")
        for row in rows:
            note = f"，{row['error']}" if row.get("error") else ""
            lines.append(
                f"  - {row.get('source')} 第 {row.get('chunk')}/{row.get('chunks_total')} 块："
                f"{row.get('wall_seconds')} s（{row.get('seconds')} s，{row.get('state')}{note}）"
            )
    else:
        lines.append("每块耗时：（进度记录上没有逐块行——整场一块都没跑成）")
    counts: dict[str, int] = reading.get("counts") or {}
    lines.append(
        "失败原因分布："
        + " / ".join(f"{name} {counts.get(name, 0)}" for name in CHUNK_STATES)
    )
    failure_reasons: dict[str, int] = reading.get("failure_reasons") or {}
    for reason, count in failure_reasons.items():
        lines.append(f"  - {reason}（{count} 块）")
    artifacts_rows: dict[str, Any] = summary.get("artifacts") or {}
    for source_name, row in artifacts_rows.items():
        state = str((row or {}).get("state") or "")
        reason = (row or {}).get("reason") or (row or {}).get("error")
        suffix = f"（{reason}）" if reason else ""
        lines.append(f"逐路结论：{source_name} {state}{suffix}")
    lines.append(f"结论：{summary.get('state')}")
    return "\n".join(lines)


__all__ = [
    "StagedMaterial",
    "StagedSource",
    "WORK_DIR_PREFIX",
    "cap_to_seconds",
    "default_work_dir",
    "format_reading",
    "run_replay",
    "stage_material",
]
