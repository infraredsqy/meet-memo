"""会后那一趟的「在飞」记录（「打断会后」节点 的 d5）。

治的是 spec 点名的那条病：**「杀了进程就永远停在在跑」**。`post_job` 是内存里按
`(种类, 场次)` 分格的事实（`runtime.post_jobs_state()`；多槽后 offline 与 minutes
可同时在飞），服务一死它就没了；盘上只剩那份 `running`。本模块给服务侧补一格
**跨进程事实**：起会后时按 `(kind, dir)` 落一条 `{dir, kind, started_at}`，服务启动时
逐条读它—— offline 档且盘上状态仍 `running` → 核对成 `interrupted`；minutes 档只清
记录 + log（纪要那一层有自己的钩子兜底，盘上 running 由它自己收）。

落点在**配置目录**（`config_dir()`，与 `workspaces.json` 并排）：那是「软件自己记的
元信息住配置目录」的既有格局 —— 注册表（`meeting/workspace.py`）就在那里，`config.json`
管配置、注册表管清单，这里管「哪一趟在飞」，三者各管各的。形状与 `post_job` 的读数格
同形（`{kind, dir, started_at}`），文件是**条目数组**、读写都是**整文件原子替换**
（同目录临时文件 + `os.replace` + fsync，先例是注册表的 `_write_file()`）。

边界两条：

  - **记录按 `(种类, 场次)` 一格一条**：多槽世界里几趟会后可以同时在飞，落新记录只
    覆盖自己那一槽、收工只摘调用方那一格（`clear`）；整份清空只归启动核对
    （`clear_all`，它已把每一条处置过）；
  - **`meeting post`（CLI）不读也不写**：它是独立进程，记录归服务自己（`docs/state.md`
    「重启自洽」那条「启动序列不碰任何场次目录」的口径，定向例外只给「记录里点名的
    那几场」，不扫任何目录）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from meeting import config as config_module
from meeting.state import now_iso

_log = logging.getLogger("meeting.service.postjobs")

#: 在飞记录文件名（住在配置目录里，与 `workspaces.json` 并排）
POST_JOBS_FILE_NAME = "post_jobs.json"


def path() -> str:
    """在飞记录的落点：`<config_dir>/post_jobs.json`。"""
    return os.path.join(config_module.config_dir(), POST_JOBS_FILE_NAME)


#: 同一进程里几趟会后可以同时起 / 同时收：record 与 clear 的整文件替换串行在
#: 这把进程内的锁后面（Windows 上两趟同时 `os.replace` 同一目标会互相踩）。
_LOCK = threading.Lock()


def _tmp_path(parent: str) -> str:
    """临时文件名（**每次调用唯一**）：多槽登记之后可以有几趟会后同时收工，两趟的
    原子替换撞同一个临时名会把对方半路按下去（Windows 上 `os.replace` 直接
    `PermissionError`）—— 临时名带上线程 id，各写各的。"""
    return os.path.join(
        parent, f".{POST_JOBS_FILE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    )


def _replace_document(
    build: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> str:
    """三个写入口共用的原子替换脚手架：建目录 → 独占临时名 → 锁内读-改-写 → 收残骸。

    `build` 拿到**锁内**读出的现有记录、给出要落盘的下一版——读-并-写整串在这把
    进程内的锁后面完成（Windows 上两趟同时 `os.replace` 同一目标会互相踩）。临时名
    每次调用唯一（`_tmp_path()`），finally 把没用上的残骸收走。写盘失败抛 `OSError`。
    """
    target = path()
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    tmp = _tmp_path(parent)
    try:
        with _LOCK:
            _write_document(build(load()), tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - 清理失败不影响结果
                pass
    return target


def record(kind: str, directory: str, *, now: str | None = None) -> str:
    """按 `(种类, 场次)` 落 / 改一条「在飞」记录：同槽覆盖、异槽互不挤占。

    多槽世界（offline 与 minutes 可同时在飞、跨场次也互不挤占）里记录**一格一条**，
    落新记录只覆盖 `(kind, dir)` 相同的那一格，别的槽原样保留 —— 先起的那趟在飞记录
    不许被后起的顶掉。
    """
    entry = {
        "kind": str(kind),
        "dir": str(directory),
        "started_at": now or now_iso(),
    }
    return _replace_document(
        lambda items: [
            item
            for item in items
            if not (item.get("kind") == entry["kind"] and item.get("dir") == entry["dir"])
        ]
        + [entry]
    )


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


def _write_document(document: list[dict[str, Any]], tmp: str, target: str) -> None:
    """整文件原子替换（同目录临时文件 + `os.replace` + fsync，先例是注册表）。"""
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def clear(kind: str, directory: str) -> str:
    """只摘 `(种类, 场次)` 这一格的记录（异槽原样保留），写盘原子替换。

    一趟收工只清自己的那一条：另一趟还在飞时把整份清空，等于把别人的安全网拆了
    （进程此刻被杀，那一趟的盘上 `running` 就没人补 `interrupted`）。
    """
    return _replace_document(
        lambda items: [
            item
            for item in items
            if not (item.get("kind") == str(kind) and item.get("dir") == str(directory))
        ]
    )


def clear_all() -> str:
    """清空整份记录（启动核对专用：它已把每一条处置过一遍才清）。写盘失败抛 `OSError`。"""
    return _replace_document(lambda _items: [])


__all__ = ["POST_JOBS_FILE_NAME", "clear", "clear_all", "load", "path", "record"]
