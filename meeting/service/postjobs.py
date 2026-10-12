"""会后那一趟的「在飞」记录（「打断会后」节点 b1c6a58e 的 d5）。

治的是 spec 点名的那条病：**「杀了进程就永远停在在跑」**。`post_job` 是内存里的单槽
事实（`runtime.post_job_state()`），服务一死它就没了；盘上只剩那份 `running`。本模块
给服务侧补一格**跨进程事实**：起会后时落一条 `{dir, kind, started_at}`，服务启动时读它
—— offline 档且盘上状态仍 `running` → 核对成 `interrupted`；minutes 档只清记录 + log
（纪要那一层有自己的钩子兜底，盘上 running 由它自己收）。

落点在**配置目录**（`config_dir()`，与 `workspaces.json` 并排）：那是「软件自己记的
元信息住配置目录」的既有格局 —— 注册表（`meeting/workspace.py`）就在那里，`config.json`
管配置、注册表管清单，这里管「哪一趟在飞」，三者各管各的。形状与 `post_job` 的读数格
同形（`{kind, dir, started_at}`），读写都是**整文件原子替换**（同目录临时文件 +
`os.replace` + fsync，先例是注册表的 `_write_file()`）。

边界两条：

  - **记录文件里至多一条**：服务是单槽的（同时只有一趟会后），落新记录就整文件替换；
  - **`meeting post`（CLI）不读也不写**：它是独立进程，记录归服务自己（`docs/state.md`
    「重启自洽」那条「启动序列不碰任何场次目录」的口径，定向例外只给「记录里点名的
    那一场」，不扫任何目录）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.state import now_iso

_log = logging.getLogger("meeting.service.postjobs")

#: 在飞记录文件名（住在配置目录里，与 `workspaces.json` 并排）
POST_JOBS_FILE_NAME = "post_jobs.json"


def path() -> str:
    """在飞记录的落点：`<config_dir>/post_jobs.json`。"""
    return os.path.join(config_module.config_dir(), POST_JOBS_FILE_NAME)


def record(kind: str, directory: str, *, now: str | None = None) -> str:
    """落一条「在飞」记录（**整文件替换**：服务单槽，至多一条）。写盘失败抛 `OSError`。"""
    target = path()
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    document = [
        {
            "kind": str(kind),
            "dir": str(directory),
            "started_at": now or now_iso(),
        }
    ]
    tmp = os.path.join(parent, f".{POST_JOBS_FILE_NAME}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
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


def load() -> list[dict[str, Any]]:
    """读全部记录（缺 / 坏 / 形状不对一律按空表起，读不出来不阻塞启动）。"""
    try:
        raw = Path(path()).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        _log.warning("在飞记录读不出来（按空表处理）：%s", path())
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        directory = item.get("dir")
        if not isinstance(kind, str) or not isinstance(directory, str):
            continue
        started = item.get("started_at")
        entries.append(
            {
                "kind": kind,
                "dir": directory,
                "started_at": started if isinstance(started, str) else None,
            }
        )
    return entries


def clear() -> str:
    """清空记录（写一份空表，原子替换）。写盘失败抛 `OSError`。"""
    target = path()
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".{POST_JOBS_FILE_NAME}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump([], handle, ensure_ascii=False)
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


__all__ = ["POST_JOBS_FILE_NAME", "clear", "load", "path", "record"]
