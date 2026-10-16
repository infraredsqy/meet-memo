"""转码独立工序 —— 逐段「PCM → MP3」的补齐与**完成判据**（全仓唯一一份）。

## 它是什么

一场会收尾落盘（flush）之后，各路各段的裸 PCM 归档要逐段转成 64k MP3。这道工序**不再
是收尾序列里的一步**：它由 flush 落定自动在后台起、也可由「补跑」动作或命令行随时拉起；
可暂停、可继续、可补跑，进度与凭据都落在这一个模块管的几张盘上文件里。同一段判据服务
三处入口（服务后台工序 / 界面补跑动作 / `meeting post` 的归档补齐），**判据只住这一处**
——「这一段写没写完」谁说了算，别处不许抄第二份。

## 完成判据：凭据，不是产物文件

「这段写完了」的**唯一凭据**是场次目录里的那份逐段凭据文件
（`meeting_transcode_<source>_<NNN>.json`，临时文件 + `os.replace` 原子写）。凭据记下
这一段的源 PCM 字节数与时长、产物 MP3 的字节数；补跑 / 重启核对时凭据**缺失或对不上
账**（源 PCM 尺寸变了、产物缺了、凭据写的是空段而盘上有字节）→ 那一段**重做**。
「产物文件在那儿」**永远不构成完成**：被强杀留下的半截 MP3 没有凭据，下一次一定被认出来
重做，不会被当成成品跳过。

逐段的转码实现仍然只有原来那一份（`meeting/capture/archive.py` 的 `convert_to_mp3` +
`mp3_path_for`，转完保留 PCM）；本模块只管「该不该转、转完怎么记账」。MP3 先写到临时
名字再原子替换：半截 MP3 永远不会坐在最终路径上，读方（离线链的解码、验收探针）要么
看到旧成品、要么看到新成品，看不到半个。

## 进度与状态（照离线层 `meeting_offline_status.json` / `meeting_offline_progress.jsonl`
的先例设计，命名与格式同族）

| 文件 | 内容 | 读者 |
|---|---|---|
| `meeting_transcode_status.json` | 工序状态：`running` / `paused` / `done` / `partial` / `failed`，外加逐路的 done / total / current_segment 与逐段行 | 下游程序 + `GET /api/sessions/{id}` 的 `transcode` 格 |
| `meeting_transcode_progress.jsonl` | 逐段进度记录：每转完一段补一行（哪一路、第几段、几秒、耗时、结果档） | 人 + 读方 |
| `meeting_transcode_<source>_<NNN>.json` | **逐段凭据**（完成判据本体，见上） | 本模块的核对 |

**重启 / 补跑的存活**（判据读盘、不依赖进程内存）：凭据与状态文件都在场次目录里，服务
重启后读数照旧；盘上停着一版 `running` 而本进程没有这道工序在飞，读方按 `in_process`
分「真的在跑」与「上次被杀停在半路」——与离线层 `offline.state` / `in_process` 两格
同一套读法。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from meeting.backends.base import SOURCE_IDS

_log = logging.getLogger("meeting.capture.transcode")

#: 工序状态文件名（照 `meeting_offline_status.json` 的先例）
STATUS_NAME = "meeting_transcode_status.json"

#: 逐段进度记录名（照 `meeting_offline_progress.jsonl` 的先例）
PROGRESS_NAME = "meeting_transcode_progress.jsonl"

#: 逐段凭据的文件名前缀（`meeting_transcode_<source>_<NNN>.json`）
CREDENTIAL_PREFIX = "meeting_transcode_"

#: 状态 / 凭据 / 记录三层各自的 `kind` 字面量（读方据此认文件）
STATUS_KIND = "transcode"
CREDENTIAL_KIND = "transcode_segment"
LEDGER_KIND = "transcode-ledger"

#: 模式版本（读方不认识就拒收，不猜）
SCHEMA_VERSION = 1

#: 空 PCM 的判据（与 `capture.archive.EMPTY_PCM_BYTES` 同一条：装不下一个 int16 样本）
EMPTY_PCM_BYTES = 2

#: PCM 的形状（16k / 单声道 / int16 → 每秒 32000 字节；与 `capture.archive` 同一条口径）
PCM_BYTES_PER_SECOND = 32000

#: 逐段结果档：`transcoded`（转出了 MP3）/ `skipped`（空段，无产物可产）/ `failed`（没转成）
ROW_TRANSCODED = "transcoded"
ROW_SKIPPED = "skipped"
ROW_FAILED = "failed"
ROW_STATES: tuple[str, ...] = (ROW_TRANSCODED, ROW_SKIPPED, ROW_FAILED)

#: 工序状态档：`partial` = 至少一段 failed、也至少一段成了；全部失败才落 `failed`
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_DONE = "done"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATUS_STATES: tuple[str, ...] = (STATE_RUNNING, STATE_PAUSED, STATE_DONE, STATE_PARTIAL, STATE_FAILED)


#: 暂停旗标的轮询间隔（秒）：后台工序在段边界等旗标摘下，等的是事件、轮询只是它的腿
PAUSE_POLL_SECONDS = 0.05


def now_iso() -> str:
    """现在的 ISO 时刻（本地时区、秒精度；与离线层 `_now_iso()` 同形）。"""
    return datetime.now().isoformat(timespec="seconds")


def credential_name(source: str, segment: int) -> str:
    """这一段凭据的文件名：`meeting_transcode_<source>_<NNN>.json`（段号三位零填充）。"""
    return f"{CREDENTIAL_PREFIX}{source}_{int(segment):03d}.json"


def credential_path(session_dir: Path | str, source: str, segment: int) -> Path:
    """这一段凭据的落点（场次目录根）。"""
    return Path(session_dir) / credential_name(source, segment)


def status_path(session_dir: Path | str) -> Path:
    """工序状态文件的落点（场次目录根）。"""
    return Path(session_dir) / STATUS_NAME


def progress_path(session_dir: Path | str) -> Path:
    """逐段进度记录的落点（场次目录根）。"""
    return Path(session_dir) / PROGRESS_NAME


def default_converter() -> Callable[..., bool]:
    """真转码那一个函数（**函数体内 import**：转码实现只有 `capture.archive` 那一份）。"""
    from meeting.capture.archive import convert_to_mp3

    return convert_to_mp3


@dataclass(slots=True)
class TranscodeSegment:
    """一段待转/已转的归档（哪一路、第几段、PCM 与 MP3 的落点）。"""

    source: str
    segment: int
    pcm: Path
    mp3: Path


def declared_sources(session_dir: Path | str) -> tuple[str, ...]:
    """这一场该转哪几路：优先读 `session.json` 的 `sources`，读不到按两路全集。"""
    path = Path(session_dir) / "session.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = None
    if isinstance(raw, dict):
        declared = raw.get("sources")
        if isinstance(declared, list):
            names = [
                str(item.get("id"))
                for item in declared
                if isinstance(item, dict) and item.get("id")
            ]
            if names:
                return tuple(names)
    return tuple(SOURCE_IDS)


def plan_segments(session_dir: Path | str, sources: Sequence[str] | None = None) -> list[TranscodeSegment]:
    """这一场的逐段计划：各路盘上实有的段 PCM（序号升序），每段配同名的 MP3 落点。

    段列表来自 `meeting/session.py` 的 `segment_archive_paths()`（与离线链取音频同一条
    枚举判据）：一段 = 一次录制，目录里没有这一路的段文件时这一路就没有可转的东西。
    """
    from meeting.capture.archive import mp3_path_for
    from meeting.session import archive_segment_number, segment_archive_paths

    directory = Path(session_dir)
    names = tuple(sources) if sources is not None else declared_sources(directory)
    plan: list[TranscodeSegment] = []
    for source in names:
        for pcm_path in segment_archive_paths(directory, source):
            number = archive_segment_number(pcm_path.stem, str(source))
            plan.append(
                TranscodeSegment(
                    source=str(source),
                    segment=1 if number is None else int(number),
                    pcm=pcm_path,
                    mp3=mp3_path_for(pcm_path),
                )
            )
    return plan


def _pcm_size(pcm_path: Path) -> int:
    try:
        return pcm_path.stat().st_size
    except OSError:
        return 0


def _mp3_size(mp3_path: Path) -> int:
    try:
        return mp3_path.stat().st_size
    except OSError:
        return 0


def read_credential(path: Path | str) -> dict[str, Any] | None:
    """读一份逐段凭据；缺失 / 坏 JSON / `kind` 不对一律交回 `None`（= 没有凭据）。"""
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != CREDENTIAL_KIND:
        return None
    return document


def credential_matches(
    entry: TranscodeSegment, credential: dict[str, Any] | None
) -> bool:
    """凭据认不认账：段身份对得上、源 PCM 尺寸没变、产物在（空段凭据则要求盘上仍空）。

    这一把尺就是「这一段写完了」的**全部判据**（服务后台工序 / 界面补跑 / 命令行补齐
    共用）：

      - 凭据缺失、坏档、`source` / `segment` / `pcm` 名对不上 → 不认账（重做）；
      - 凭据记的 `pcm_bytes` 与**当下**源 PCM 尺寸不相等 → 不认账（源变了，旧账作废）；
      - 凭据记 `produced: true` 而产物 MP3 缺席或字节数对不上 → 不认账（半截产物重做）；
      - 凭据记 `produced: false`（空段）而源 PCM 现在有了字节 → 不认账（重转）。
    """
    if not isinstance(credential, dict):
        return False
    if credential.get("source") != entry.source or credential.get("segment") != entry.segment:
        return False
    if credential.get("pcm") != entry.pcm.name:
        return False
    size = _pcm_size(entry.pcm)
    if credential.get("pcm_bytes") != size:
        return False
    produced = bool(credential.get("produced"))
    if produced:
        if credential.get("mp3") != entry.mp3.name:
            return False
        return _mp3_size(entry.mp3) == int(credential.get("mp3_bytes") or 0)
    return size < EMPTY_PCM_BYTES


def segment_done(entry: TranscodeSegment) -> bool:
    """这一段写完了吗：**凭据在且认账**（产物文件在不在不当判据）。"""
    return credential_matches(entry, read_credential(credential_path(entry.pcm.parent, entry.source, entry.segment)))


# ---------------------------------------------------------------------------
# 原子写（临时文件 + os.replace；凭据 / 状态 / 记录三层共用同一份写法）
# ---------------------------------------------------------------------------


def _write_atomic(path: Path | str, text: str) -> Path:
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - 清理失败不影响结果
                pass
    return target


def write_credential(entry: TranscodeSegment, document: dict[str, Any]) -> Path:
    """落一份逐段凭据（**原子写**：这一段「写完了」的事实以此为准）。

    `source` / `segment` / `pcm` 三格身份由这里统一带上（调用方只给产物账目），核对
    侧 `credential_matches()` 按同一套键读。
    """
    payload = {
        "kind": CREDENTIAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "source": entry.source,
        "segment": entry.segment,
        "pcm": entry.pcm.name,
        **document,
    }
    return _write_atomic(
        credential_path(entry.pcm.parent, entry.source, entry.segment),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


# ---------------------------------------------------------------------------
# 状态与进度记录（照离线层先例：状态整份原子替换；记录一行一段、整份重写原子替换）
# ---------------------------------------------------------------------------


def build_status(
    *,
    session_id: str,
    state: str,
    sources: Sequence[dict[str, Any]],
    started_at: str,
    finished_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """组一份工序状态（纯函数；写出去与读回来都以它为准）。"""
    if state not in STATUS_STATES:
        raise ValueError(f"转码工序状态只认 {STATUS_STATES}，收到 {state!r}")
    return {
        "kind": STATUS_KIND,
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "state": state,
        "started_at": str(started_at),
        "finished_at": None if finished_at is None else str(finished_at),
        "error": None if error is None else str(error),
        "sources": [dict(item) for item in sources],
    }


def read_status(session_dir: Path | str) -> dict[str, Any] | None:
    """读工序状态文件；缺失 / 坏档 / `kind` 不对 / 状态不在取值域里 → `None`（不猜）。"""
    try:
        document = json.loads(status_path(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != STATUS_KIND:
        return None
    if document.get("state") not in STATUS_STATES:
        return None
    return document


def write_status(session_dir: Path | str, document: dict[str, Any]) -> Path:
    """落一份工序状态（原子替换；半份状态比晚一点落地坏得多）。"""
    return _write_atomic(
        status_path(session_dir), json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def write_progress(
    session_dir: Path | str,
    *,
    session_id: str,
    sources: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> Path:
    """整份重写逐段进度记录（表头 + 逐段那些行，原子替换；JSON Lines）。"""
    header = {
        "kind": LEDGER_KIND,
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "sources": [str(item) for item in sources],
    }
    lines = [json.dumps(header, ensure_ascii=False)]
    for row in rows:
        lines.append(json.dumps(dict(row), ensure_ascii=False))
    return _write_atomic(progress_path(session_dir), "\n".join(lines) + "\n")


def read_progress(session_dir: Path | str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """读回逐段进度记录：`(表头, 逐段那些行)`；读不出来如实交回 `(None, [])`。"""
    try:
        raw = progress_path(session_dir).read_text(encoding="utf-8")
    except OSError:
        return None, []
    header: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        if number == 0 and item.get("kind") == LEDGER_KIND:
            header = item
            continue
        if "source" in item and "segment" in item:
            rows.append(item)
    return header, rows


# ---------------------------------------------------------------------------
# 主流程：逐段「没凭据的转、有凭据的跳、空段记账、暂停让路」
# ---------------------------------------------------------------------------


def ensure_segment(
    entry: TranscodeSegment, apply_conversion: Callable[..., bool]
) -> dict[str, Any]:
    """把一段归档补齐到「凭据认账」的状态，交回这一段的记账行（**唯一的逐段实现**）。

    四条路（判据全部落在凭据上，见模块头）：

      - **凭据在且认账** → 跳过（不重转；`reused: true`，补跑的「接着做」落在这一条）；
      - **空段**（PCM 装不下一个样本）→ 不产 MP3，落一份 `produced: false` 的凭据；
      - **没凭据 / 不认账** → 转码（MP3 先写临时名字、`os.replace` 原子落位）+ 落凭据；
      - **转码抛错** → `state: failed`，临时产物收走，**PCM 一个字节都不动**。

    返回行字段：`{source, segment, state, mp3, seconds, reused, error}`。
    """
    row: dict[str, Any] = {
        "source": entry.source,
        "segment": entry.segment,
        "pcm": entry.pcm.name,
        "mp3": None,
        "state": ROW_TRANSCODED,
        "seconds": None,
        "reused": False,
        "error": None,
    }
    credential = read_credential(credential_path(entry.pcm.parent, entry.source, entry.segment))
    if credential_matches(entry, credential):
        # 凭据认账：这一段早已写完，不重转（补跑的「接着做」就落在这一条上）
        row["state"] = ROW_SKIPPED if not bool(credential.get("produced")) else ROW_TRANSCODED
        row["mp3"] = entry.mp3.name if bool(credential.get("produced")) else None
        row["seconds"] = credential.get("pcm_seconds")
        row["reused"] = True
        return row
    size = _pcm_size(entry.pcm)
    if size < EMPTY_PCM_BYTES:
        # 空段：没有音频可转，落一份「这段核对过、无需产物」的凭据
        write_credential(
            entry,
            {
                "produced": False,
                "pcm_bytes": size,
                "pcm_seconds": 0.0,
                "mp3": None,
                "mp3_bytes": 0,
                "finished_at": now_iso(),
            },
        )
        row["state"] = ROW_SKIPPED
        row["seconds"] = 0.0
        return row
    tmp_mp3 = entry.mp3.with_name(entry.mp3.name + ".tmp")
    try:
        produced = bool(apply_conversion(entry.pcm, tmp_mp3))
    except Exception as exc:
        try:
            tmp_mp3.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 收不走不挡记账
            pass
        row["state"] = ROW_FAILED
        row["error"] = f"{type(exc).__name__}: {exc}"
        _log.error("转码工序：%s 第 %s 段没转成（%s）", entry.source, entry.segment, row["error"])
        return row
    try:
        if produced and tmp_mp3.is_file():
            # 原子落位：半截 MP3 永远不会坐在最终路径上（读方要么见旧成品、要么见新成品；
            # 落位之后凭据才落 —— 「写完了」以凭据为准，半截产物一定被认出来重做）
            os.replace(tmp_mp3, entry.mp3)
        elif produced:
            row["state"] = ROW_FAILED
            row["error"] = "转码器报成功，但 MP3 没落盘"
            return row
        else:
            tmp_mp3.unlink(missing_ok=True)
    except OSError as exc:
        row["state"] = ROW_FAILED
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    mp3_bytes = _mp3_size(entry.mp3)
    write_credential(
        entry,
        {
            "produced": bool(produced),
            "pcm_bytes": size,
            "pcm_seconds": round(size / PCM_BYTES_PER_SECOND, 3),
            "mp3": entry.mp3.name if produced else None,
            "mp3_bytes": mp3_bytes if produced else 0,
            "finished_at": now_iso(),
        },
    )
    row["mp3"] = entry.mp3.name if produced else None
    row["seconds"] = round(size / PCM_BYTES_PER_SECOND, 3)
    return row


def run_transcode(
    session_dir: Path | str,
    *,
    converter: Callable[..., bool] | None = None,
    sources: Sequence[str] | None = None,
    pause: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """把这一场没转完的段接着做完，交回摘要 dict。

    逐段四条路（判据全部落在凭据上，见模块头）：

      - **凭据在且认账** → 跳过（不重转；记一行 `reused` 的账）；
      - **空段**（PCM 装不下一个样本）→ 不产 MP3，落一份 `produced: false` 的凭据；
      - **没凭据 / 不认账** → 转码（MP3 先写临时名字、`os.replace` 原子落位）+ 落凭据；
      - **转码抛错** → 该段记 `failed`，临时产物收走，**PCM 一个字节都不动**，别的段照转。

    `pause` 是**可注入的暂停旗标**（callable，返回真 = 请求暂停）：在**每段开跑前**检查，
    当前段照完、下一段不开 —— 命中后工序状态落 `paused`，已转完的段凭据都在；**旗标摘下
    （callable 回假）之后同一条工序在段边界接着往下做**（继续就是摘旗），本函数只在全部
    段做完（或失败）之后才交回执。`None` = 不暂停（命令行补齐那一档）。

    工序状态与逐段记录随做随写：开跑落 `running`、进暂停落 `paused`、每段落一行账、收工
    按终档落（`done` / `partial` / `failed`；盘上停着 `paused` = 工序还活着、在段边界等旗标，
    进程死了那一版原地不动，重启后由补跑 / 继续接着做）。
    """
    apply_conversion = converter if converter is not None else default_converter()
    directory = Path(session_dir)
    names = tuple(sources) if sources is not None else declared_sources(directory)
    session_id = directory.name
    started_at = now_iso()

    plan = plan_segments(directory, names)
    totals = {name: sum(1 for item in plan if item.source == name) for name in names}

    def _source_cell(source: str, rows: list[dict[str, Any]], current: int | None) -> dict[str, Any]:
        mine = [row for row in rows if str(row.get("source")) == source]
        done = sum(1 for row in mine if str(row.get("state")) in (ROW_TRANSCODED, ROW_SKIPPED))
        total = int(totals.get(source, 0))
        return {
            "source": str(source),
            "total": total,
            "done": done,
            "remaining": max(0, total - done),
            "current_segment": current,
            "segments": [
                {
                    "segment": int(row.get("segment") or 0),
                    "state": str(row.get("state")),
                    "seconds": row.get("seconds"),
                    "mp3": row.get("mp3"),
                }
                for row in mine
            ],
        }

    def _persist(state: str, rows: list[dict[str, Any]], current: dict[str, int | None], *, error: str | None = None, finished: bool = False) -> None:
        write_status(
            directory,
            build_status(
                session_id=session_id,
                state=state,
                sources=[_source_cell(name, rows, current.get(name)) for name in names],
                started_at=started_at,
                finished_at=now_iso() if finished else None,
                error=error,
            ),
        )
        write_progress(directory, session_id=session_id, sources=list(names), rows=rows)

    _persist(STATE_RUNNING, [], {name: None for name in names})
    _log.info("转码工序开跑：%s（%s 路计划 %s 段）", directory, len(names), len(plan))

    rows: list[dict[str, Any]] = []
    current: dict[str, int | None] = {name: None for name in names}
    paused_seen = False
    resumed_pending = False
    for entry in plan:
        # 暂停在段边界收场：当前段照完，下一段开跑前等旗标摘下（继续 = 摘旗，同一条
        # 工序接着前进；进程死了盘上停着 paused，重启后补跑 / 继续从凭据接着做）
        while pause is not None and pause():
            if not paused_seen:
                paused_seen = True
                _log.info("转码工序：暂停旗标已立，%s 第 %s 段开跑前等旗标摘下", entry.source, entry.segment)
                _persist(STATE_PAUSED, rows, dict(current))
            resumed_pending = True
            time.sleep(PAUSE_POLL_SECONDS)
        if resumed_pending:
            # 旗标刚摘下：状态翻回 running，从这一段起接着前进（每次续跑只写这一笔）
            resumed_pending = False
            _log.info("转码工序：暂停旗标已摘，%s 第 %s 段起接着前进", entry.source, entry.segment)
            _persist(STATE_RUNNING, rows, dict(current))
        current[entry.source] = entry.segment
        started = time.monotonic()
        row = ensure_segment(entry, apply_conversion)
        row["segments_total"] = int(totals.get(entry.source, 0))
        row["wall_seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
        _persist(STATE_RUNNING, rows, dict(current))

    failed = [row for row in rows if row["state"] == ROW_FAILED]
    if failed and len(failed) == len(rows):
        state = STATE_FAILED
    elif failed:
        state = STATE_PARTIAL
    else:
        state = STATE_DONE
    error = (
        f"{len(failed)} 段没转成：" + "；".join(
            f"{row['source']} 第 {row['segment']} 段（{row['error']}）" for row in failed
        )
        if failed
        else None
    )
    _persist(state, rows, {name: None for name in names}, error=error, finished=True)
    _log.info("转码工序收工：%s（state=%s，%s 段，失败 %s）", directory, state, len(rows), len(failed))
    return {
        "session_id": session_id,
        "state": state,
        "sources": [name for name in names],
        "segments_total": len(plan),
        "segments_done": sum(1 for row in rows if row["state"] in (ROW_TRANSCODED, ROW_SKIPPED)),
        "segments_failed": len(failed),
        "paused": paused_seen,
        "rows": rows,
    }


__all__ = [
    "CREDENTIAL_KIND",
    "CREDENTIAL_PREFIX",
    "EMPTY_PCM_BYTES",
    "LEDGER_KIND",
    "PCM_BYTES_PER_SECOND",
    "PROGRESS_NAME",
    "ROW_FAILED",
    "ROW_SKIPPED",
    "ROW_STATES",
    "ROW_TRANSCODED",
    "SCHEMA_VERSION",
    "STATUS_KIND",
    "STATUS_NAME",
    "STATUS_STATES",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_PAUSED",
    "STATE_PARTIAL",
    "STATE_RUNNING",
    "TranscodeSegment",
    "build_status",
    "credential_matches",
    "credential_name",
    "credential_path",
    "ensure_segment",
    "declared_sources",
    "default_converter",
    "plan_segments",
    "progress_path",
    "read_credential",
    "read_progress",
    "read_status",
    "run_transcode",
    "segment_done",
    "status_path",
    "write_credential",
    "write_progress",
    "write_status",
]
