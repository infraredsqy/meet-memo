"""人名表：一场会目录里的「编号 → 人名」记录（节点，R10 第四条 / R30）。

一场会落一份 `meeting_speaker_names.json` 在场次目录根，只收用户确认过的 `(编号, 名字)` 对
（跳过 = 不起名，不落任何状态）。纯记录：不做注册制声纹（R10 首句）、不做「应该有谁」的
校验（`docs/design-intent.md` 第 6 条）；唯一用途是喂给下一次出纪要的提示词
（`meeting/minutes/prompt.py` 的 `{{SPEAKER_NAMES}}`）。改这张表不改任何已生成的
`meeting_final.md`（R13）、不拉起任何离线/纪要流程（R26）。

编号沿用既有字面量（`meeting/offline/artifacts.py` 的对照表）：离线编号 = `${source}:${local}`
（如 `mic:S01`，两路不撞是结构保证）；没跑过离线聚类时身份 = 路本身（`mic` / `loopback`）。
写入侧当场拦形状不对的键。

字段契约：顶层必填 `schema_version` 与 `names`（每项 `key` / `name` 非空字符串）；
版本不认识 → 读方拒收。读侧只做结构解析：读不出时记一行、如实当作「没有确认过的名字」
继续走，不报错、不挡纪要；写侧谨慎：文件在盘上但读不出时改名**拒写**（`set_speaker_name()`）——
读不出还整份覆盖会把用户逐条确认过的名字无声抹掉。

纯标准库；原子写复用 `meeting.atomicio.write_text_atomic()`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from meeting.atomicio import write_text_atomic
from meeting.minutes.chunk import (
    LIVE_TRANSCRIPT_TEMPLATE,
    OFFLINE_TRANSCRIPT_TEMPLATE,
    SOURCE_IDS,
)
from meeting.offline.artifacts import (
    SPEAKERS_NAME_TEMPLATE,
    ArtifactContractError,
    read_speakers_artifact,
)
from meeting.state import SOURCE_LABELS

_log = logging.getLogger("meeting.speaker_names")

#: 人名表的文件名（住**场次目录根**；一份记两路，键里带着路）
NAMES_FILE_NAME = "meeting_speaker_names.json"

#: 人名表的模式版本。**主版本号读方认识才收**：结构变了就抬它，读方据此拒收。
SCHEMA_VERSION = 1

#: 本实现认识的主版本号（读方按它拒收）
KNOWN_SCHEMA_MAJOR = 1


class SpeakerNamesError(ValueError):
    """人名表的读/写不满足字段契约（版本不认识、键的形状不对、条目缺必填键）。

    继承 `ValueError`：读方拿它当「这一份读不出来」的记号如实降级；写方原样交给调用方登记。
    """


def names_path(session_dir: str | Path) -> Path:
    """这一场的人名表落点（场次目录根下那一份）。"""
    return Path(session_dir) / NAMES_FILE_NAME


def qualified_key_shape(key: Any) -> str:
    """键的形状校验（两路不撞是结构保证，写侧当场拦）：返回去掉首尾空白后的键。

    合法形状只有两种：路本身（`mic` / `loopback`），或带路前缀的限定名（`mic:S01`）。
    裸 `S01` 这种键会把两路的「1 号」写成同一个人。
    """
    text = str(key or "").strip()
    prefix, sep, local = text.partition(":")
    if prefix not in SOURCE_IDS or not prefix:
        raise SpeakerNamesError(
            f"人名表的键说不出是哪一路：{key!r}"
            f"（合法形状是 {' / '.join(SOURCE_IDS)} 或 mic:S01 这种带路前缀的限定名）"
        )
    if sep and (not local or ":" in local):
        raise SpeakerNamesError(
            f"人名表的键不是限定的编号形状：{key!r}（前缀后面要恰好是一段编号，如 mic:S01）"
        )
    return text


def build_names_document(entries: Iterable[tuple[str, str] | Mapping[str, Any]]) -> dict[str, Any]:
    """组一份人名表（**纯函数**，不碰盘）：写出去与读回来都以它为准。

    `entries` 每项是 `(key, name)` 对或带这两键的映射——**只收用户确认过的**；
    键的形状在这里过一遍（见 `qualified_key_shape()`），名字收去首尾空白后的非空串。
    """
    rows: list[dict[str, str]] = []
    for item in entries:
        if isinstance(item, Mapping):
            key, name = item.get("key"), item.get("name")
        else:
            key, name = item[0], item[1]
        clean_key = qualified_key_shape(key)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise SpeakerNamesError(
                f"编号 {clean_key!r} 的名字是空的：跳过 = 不起名，空名字不落任何状态"
            )
        rows.append({"key": clean_key, "name": clean_name})
    return {"schema_version": SCHEMA_VERSION, "names": rows}


def parse_names_document(document: Any) -> dict[str, Any]:
    """读一份人名表（dict 或 JSON 文本）：契约不满足即抛 `SpeakerNamesError`。

    三条拒绝面（与对照表那一套同一范式）：顶层不是对象（或 JSON 解不开）、
    `schema_version` 缺失 / 非整数 / 主版本号不认识、`names` 缺失或某项缺
    `key` / `name` 非空字符串。通过之后原样交回整份 dict（不裁剪、不补默认值）。
    """
    if isinstance(document, (str, bytes, bytearray)):
        try:
            document = json.loads(document)
        except (ValueError, TypeError) as exc:
            raise SpeakerNamesError(f"人名表不是合法 JSON：{exc}") from None
    if not isinstance(document, dict):
        raise SpeakerNamesError(f"人名表顶层该是对象，收到 {type(document).__name__}")

    version = document.get("schema_version")
    if version is None:
        raise SpeakerNamesError("人名表缺 schema_version（读方拒绝，不许猜）")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SpeakerNamesError(f"schema_version 该是整数，收到 {version!r}")
    if version != KNOWN_SCHEMA_MAJOR:
        raise SpeakerNamesError(
            f"人名表的主版本号不认识：{version}（本实现只认 {KNOWN_SCHEMA_MAJOR}）"
        )

    names = document.get("names")
    if not isinstance(names, list):
        raise SpeakerNamesError("人名表缺 names（该是列表）")
    for index, item in enumerate(names):
        if not isinstance(item, dict):
            raise SpeakerNamesError(
                f"names[{index}] 该是对象，收到 {type(item).__name__}"
            )
        for key in ("key", "name"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SpeakerNamesError(f"names[{index}] 缺非空字符串的 {key}")
    return document


# 读（只做结构解析；读不出就如实当作「没有」，不挡任何下游）


def read_names_entries(session_dir: str | Path) -> dict[str, str] | None:
    """读回这一场的 `{编号: 名字}`（只读、不抛）。

    文件缺席 → `None`；契约过 → 按落盘次序交回映射（同一个键以最后落笔的那次为准）；
    在但读不出 → 记一行 log、交回 `None`（提示词如实不带这张表，不报错、不挡纪要）。
    写方对这一档反着来：拒写，见 `set_speaker_name()`。
    """
    path = names_path(session_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("人名表读不动：%s（%s: %s）", path, type(exc).__name__, exc)
        return None
    try:
        document = parse_names_document(raw)
    except SpeakerNamesError as exc:
        _log.warning("人名表读不出来（不挡下游）：%s（%s）", path, exc)
        return None
    entries: dict[str, str] = {}
    for item in document["names"]:
        entries[str(item["key"])] = str(item["name"])
    return entries


def set_speaker_name(session_dir: str | Path, key: str, name: str) -> dict[str, str]:
    """把一个编号的名字写进人名表（唯一写方，改名动作走这一条）。

    同键覆盖（改名 = 再起一次名），没有就追加；落盘前把整份文档过一遍读方契约
    （写出去的必须读得回来）。原子写，界面与提示词随时来读都不会读到半份。

    表在盘上但读不出（坏 JSON / 未来主版本）时拒写：读不出还整份覆盖会把旧表里
    用户逐条确认过的名字无声抹掉——读侧宽容只管渲染（缺表不挡纪要，d7），
    写侧不拿这份宽容去覆盖待抢救的数据。抛 `SpeakerNamesError`，由调用方登记。
    """
    clean_key = qualified_key_shape(key)
    clean_name = str(name or "").strip()
    path = names_path(session_dir)
    existing = read_names_entries(session_dir)
    if existing is None and path.exists():
        # 写侧谨慎：读不出就换新文件 = 无声覆盖旧表，拒（读侧宽容不延伸到写侧）。
        raise SpeakerNamesError(
            f"人名表在盘上但读不出来，改名拒写（旧表里确认过的名字留给机主抢救）：{path}"
        )
    entries = existing or {}
    entries[clean_key] = clean_name
    document = build_names_document(entries.items())
    parse_names_document(document)  # 写侧自检：写出去的必须读得回来
    write_text_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    _log.info("人名表落盘：%s（%s → %s）", path, clean_key, clean_name)
    return {"key": clean_key, "name": clean_name}


# 盘上身份集（单场状态端点与提示词共用的那一份读数）


def _read_speaker_summary(artifact: Path) -> list[dict[str, Any]] | None:
    """读一份对照表的编号汇总；读不出（契约不过 / 盘上出岔）记一行、交回 `None`。"""
    try:
        document = read_speakers_artifact(artifact)
    except (ArtifactContractError, OSError) as exc:
        _log.warning(
            "对照表读不出来（这一路按「没跑过聚类」处理）：%s（%s: %s）",
            artifact, type(exc).__name__, exc,
        )
        return None
    summary: list[dict[str, Any]] = []
    for item in document.get("speakers") or []:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        if not identifier:
            continue
        summary.append(
            {
                "id": identifier,
                "segments": int(item.get("segments") or 0),
            }
        )
    return summary


def _road_is_on_disk(session: Path, source: str) -> bool:
    """这一路在盘上有没有材料（两份稿的任一份，或它那份对照表）。"""
    return (
        (session / LIVE_TRANSCRIPT_TEMPLATE.format(source=source)).is_file()
        or (session / OFFLINE_TRANSCRIPT_TEMPLATE.format(source=source)).is_file()
        or (session / SPEAKERS_NAME_TEMPLATE.format(source=source)).is_file()
    )


def identity_rows(session_dir: str | Path) -> list[dict[str, Any]]:
    """这一场盘上的身份集（单场状态端点的 `speakers` 一格、提示词的人名段共用）。

    逐路各答一遍（照 `SOURCE_IDS` 的序）：对照表在且读得出、里面有编号 → 每个编号一行
    （键 `mic:S01` 这种限定名，`segments` 是出现段数）；对照表缺席 / 读不出 / 空表 →
    这一路落成路本身一行（没跑过离线聚类时身份只有这两种，R10 第四条）。

    每行五格：`key` / `source` / `label`（人话标签）/ `name`（没起过名或表读不出时
    `null`）/ `segments`（路级身份如实 `null`）。这一格是给人看的读数：不校验、不核对、
    不改任何结论（`docs/design-intent.md` 第 6 条）。
    """
    session = Path(session_dir)
    names = read_names_entries(session) or {}
    label_of = lambda source: SOURCE_LABELS.get(source, source)  # noqa: E731 - 两路之外如实亮原样
    rows: list[dict[str, Any]] = []
    for source in SOURCE_IDS:
        artifact = session / SPEAKERS_NAME_TEMPLATE.format(source=source)
        summary = _read_speaker_summary(artifact) if artifact.is_file() else None
        if summary:
            for item in summary:
                identifier = str(item["id"])
                rows.append(
                    {
                        "key": identifier,
                        "source": source,
                        "label": label_of(source),
                        "name": names.get(identifier),
                        "segments": int(item["segments"]),
                    }
                )
            continue
        if artifact.is_file() or _road_is_on_disk(session, source):
            rows.append(
                {
                    "key": source,
                    "source": source,
                    "label": label_of(source),
                    "name": names.get(source),
                    "segments": None,
                }
            )
    return rows


__all__ = [
    "KNOWN_SCHEMA_MAJOR",
    "NAMES_FILE_NAME",
    "SCHEMA_VERSION",
    "SOURCE_IDS",
    "SpeakerNamesError",
    "build_names_document",
    "identity_rows",
    "names_path",
    "parse_names_document",
    "qualified_key_shape",
    "read_names_entries",
    "set_speaker_name",
]
