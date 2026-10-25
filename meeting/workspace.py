"""工作区注册表与目录判别（工作台收编节点 e2f6166b）。

口径（`docs/design-intent.md` 第 2–5 条，`docs/requirements.md` R18–R21）：注册表
`workspaces.json` 住在配置目录里（不塞进 `config.json`——那边表外键严格拒绝），
按路径去重；列表从登记来，软件不扫任何根目录、不在启动时读任何场次目录
（「最近一场」只从注册表读）；目录名就是这一场的名字，不解析、不改、不建目录；
判别只看会议材料的名目（`session.json` 或 `meeting_audio_*.pcm`），不读内容不核对，
有音频名却没有 `session.json` 的落「疑似会议目录」照收编（第 3 条的兜底）；
注册表记录只增删、不反映目录内容——目录还在不在由读取方现场判（`sessions_dict()`）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.session import SESSION_FILE_NAME, archive_name
from meeting.state import now_iso

_log = logging.getLogger("meeting.workspace")

#: 注册表文件名（住在配置目录里，与 `config.json` 并排）
REGISTRY_FILE_NAME = "workspaces.json"

#: 判别的判据名（只看文件名，不读内容）
MIC_ARCHIVE_PREFIX = "meeting_audio_"
PCM_SUFFIX = ".pcm"


def canonical_dir(value: Any) -> str:
    """把用户给的工作区目录算成**规范的绝对路径**（去重判据就落在这条路径上）。

    相对路径照 `resolve_workspace_dir()` 的做法锚到进程当前目录；`~` 展开；
    不判存在、不建目录。注册表的去重判据在它之上再 `normcase`（见 `_registry_key`）。
    """
    text = "~" if value is None else str(value)
    path = Path(text.strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(os.path.normpath(str(path)))


def meeting_material_names() -> frozenset[str]:
    """会议材料的判据名：`session.json` + 两路归档的**真实文件名**。"""
    return frozenset(
        {SESSION_FILE_NAME, archive_name("mic"), archive_name("loopback")}
    )


#: 会后精细稿的文件名前缀（`meeting_offline_transcript_<source>.md`）。
#: 与 `meeting/offline/artifacts.py` 的转写名同一族：这里**只按名目判**，
#: 不 import 那个包（工作区列表是只读的一瞥，不该把会后那一套的依赖拖进来）。
OFFLINE_TRANSCRIPT_PREFIX = "meeting_offline_transcript_"


def has_offline_transcript(value: Any) -> bool:
    """这一场有没有**会后精细稿**（`meeting_offline_transcript_<source>.md`）。

    R29 那一格的判据（`GET /api/sessions` 每行的 `has_offline`）：只看名目、不读内容、
    不猜 —— 文件在就算有，哪怕它还没被切块。「出纪要」按钮按它如实显示。

    目录不在 / 读不动时答 `False`（读不出来就是不认为有），不抛异常：
    这一格服务的是界面上的一句话，不该把整个列表端点带走。
    """
    path = Path(canonical_dir(value))
    try:
        return any(
            item.is_file() and item.name.startswith(OFFLINE_TRANSCRIPT_PREFIX)
            for item in path.iterdir()
        )
    except OSError:
        return False


def classify_workspace_dir(value: Any) -> dict[str, Any]:
    """把一个目录判成三档：`empty` / `meeting` / `unknown`（外加 `missing` 一档）。

    返回 `{dir, kind, suspected, has_session_json, has_audio_archives}`：

    - `missing`：目录不存在（本服务不建目录，path 给错就走这一档）；
    - `empty`：**空目录** → 当一场新会；
    - `meeting`：有会议材料（`session.json` 或任一 `meeting_audio_*.pcm`）→ 收编；
      `suspected = True` 表示**有音频归档名却没有 `session.json`** 的疑似会议目录
      （兜底档，照收编处理，不当杂物）；
    - `unknown`：非空、没有会议材料 → 弹一句确认，确认之后才登记。

    判别**只看文件名**：读不动目录（无权限等）如实落 `unknown`（`error` 带一句话），
    不替用户猜。
    """
    path = Path(canonical_dir(value))
    has_session = False
    has_audio = False
    error: str | None = None
    try:
        if not path.is_dir():
            return {
                "dir": str(path), "kind": "missing", "suspected": False,
                "has_session_json": False, "has_audio_archives": False, "error": None,
            }
        names = [item.name for item in path.iterdir()]
    except OSError as exc:
        return {
            "dir": str(path), "kind": "unknown", "suspected": False,
            "has_session_json": False, "has_audio_archives": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if SESSION_FILE_NAME in names:
        has_session = True
    has_audio = any(
        name.startswith(MIC_ARCHIVE_PREFIX) and name.endswith(PCM_SUFFIX) for name in names
    )
    if not names:
        kind = "empty"
    elif has_session or has_audio:
        kind = "meeting"
    else:
        kind = "unknown"
    return {
        "dir": str(path),
        "kind": kind,
        "suspected": bool(kind == "meeting" and has_audio and not has_session),
        "has_session_json": has_session,
        "has_audio_archives": has_audio,
        "error": error,
    }


def _registry_key(value: Any) -> str:
    """注册表的**去重/查重判据**：canonical 路径再 `normcase`（Windows 不分大小写，
    `C:\\Foo` 与 `C:\\FOO` 是同一个目录，只记一行）。判据归判据——**登记的键保留
    第一次落笔时 canonical 路径的原文**（展示路径不折大小写）。读写两侧
    （`open` / `remove`）都走这一条，口径一致。
    """
    return os.path.normcase(canonical_dir(value))


def _find_key(meetings: dict[str, Any], norm_key: str) -> str | None:
    """已登记键里找与 `norm_key` 同义的那一条（大小写差异经过 normcase 归并）。"""
    if not isinstance(meetings, dict):
        return None
    for key in meetings:
        if isinstance(key, str) and os.path.normcase(key) == norm_key:
            return key
    return None


class WorkspaceRegistry:
    """`workspaces.json` 的内存视图与原子写（按路径去重的最小增删查）。"""

    def _registry_path(self) -> str:
        return os.path.join(config_module.config_dir(), REGISTRY_FILE_NAME)

    def _read_file(self) -> dict[str, Any]:
        """读注册表；读不出来（缺 / 坏 / 顶层不是对象）一律按空表起并留一行 WARNING。

        这里的手法与 `SessionStore.read_session_json()` 同一档：**异常不冒到调用方**，
        下一笔写入会把空表落回盘上（坏的那份本来就读不回来——旧内容不抢救）。
        """
        target = self._registry_path()
        try:
            raw = Path(target).read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            _log.warning(
                "工作区注册表读不出来（按空表处理，本份文件将在下次写入时被原子替换）：%s",
                target,
            )
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("meetings"), dict):
            _log.warning(
                "工作区注册表形状不对（按空表处理）：%s", target
            )
            return {}
        return data

    def _write_file(self, data: dict[str, Any]) -> str:
        """原子写注册表（同目录临时文件 + `os.replace` + fsync；先例是
        `config.write_config_values()`）。写盘失败抛 `OSError`，由调用方收。"""
        target = self._registry_path()
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".{REGISTRY_FILE_NAME}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
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

    def entries(self) -> list[dict[str, Any]]:
        """读全部条目（形状不对的那几条**原样剔掉**，不崩整个读路径）。

        每条只保三键：`{dir, added_at, last_opened_at}`（多余键剔除——形状坏了的条目
        不许顺着读进去）。
        """
        data = self._read_file()
        meetings = data.get("meetings")
        if not isinstance(meetings, dict):  # 已被 _read_file 挡过，这里只兜形状
            return []
        entries: list[dict[str, Any]] = []
        for dir_key, value in meetings.items():
            if not isinstance(value, dict):
                continue
            added, opened = value.get("added_at"), value.get("last_opened_at")
            if not isinstance(added, str) or not isinstance(opened, str):
                continue
            entries.append(
                {"dir": dir_key, "added_at": added, "last_opened_at": opened}
            )
        return entries

    def recent(self) -> dict[str, Any] | None:
        """最近一次被打开/切换的那一条（**只从注册表读，不扫任何目录**）。

        次序判据：`last_opened_at` 大者在前；同秒（并列）时按 `added_at`，再按目录
        字典序——两条并列判据只是让「谁算最近」有唯一的答案，语义仍是最近操作的时间戳。
        """
        entries = self.entries()
        if not entries:
            return None
        return max(
            entries,
            key=lambda item: (item["last_opened_at"], item["added_at"], item["dir"]),
        )

    def open(self, value: Any, *, now: str | None = None) -> dict[str, Any]:
        """「打开目录」的一条登记：新路径加一条，老路径只更新 `last_opened_at`。

        返回 `{dir, added_at, last_opened_at, first_time}`；写盘失败抛 `OSError`。
        """
        data = self._read_file()
        meetings = data.setdefault("meetings", {})
        if not isinstance(meetings, dict):  # pragma: no cover - _read_file 保证
            meetings = data["meetings"] = {}
        return_key = canonical_dir(value)
        norm_key = _registry_key(value)
        stamp = now or now_iso()
        existing_key = _find_key(meetings, norm_key)
        dir_key = existing_key if existing_key is not None else return_key
        existing = meetings.get(dir_key)
        prior_added: str | None = (
            existing.get("added_at") if isinstance(existing, dict) else None
        )
        first_time = not isinstance(prior_added, str) or not prior_added
        entry = {
            "added_at": prior_added or stamp,
            "last_opened_at": stamp,
        }
        meetings[dir_key] = entry
        self._write_file(data)
        return {"dir": dir_key, **entry, "first_time": first_time}

    def remove(self, value: Any) -> dict[str, Any]:
        """「删除」= **只把这一条从注册表摘掉**；目录与文件一个字节不碰。

        目录寻址方式：返回 `{dir, removed}`；没登记过这一条就 `removed: False`，
        不算错（重复删除安全）。
        """
        data = self._read_file()
        meetings = data.get("meetings")
        dir_key = canonical_dir(value)
        removed = False
        existing_key = _find_key(meetings, _registry_key(value))
        if isinstance(meetings, dict) and existing_key is not None:
            del meetings[existing_key]
            self._write_file(data)
            removed = True
        return {"dir": dir_key, "removed": removed}


__all__ = [
    "OFFLINE_TRANSCRIPT_PREFIX",
    "REGISTRY_FILE_NAME",
    "WorkspaceRegistry",
    "canonical_dir",
    "classify_workspace_dir",
    "has_offline_transcript",
    "meeting_material_names",
]
