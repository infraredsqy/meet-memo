"""场次元信息读取（自 `meeting/offline/runner` 编排主模块拆出）：这一场叫什么、该跑哪几路。

- `session_id_of()`：场次编号 = 场次目录名（目录名是服务侧生成的身份，本模块不另起规则）；
- `declared_sources()`：这一场该跑哪几路——读 `session.json` 的 `sources[].id`，
  读不到按两路全集（`SOURCE_IDS`）；
- `_read_session_json()`：缺失或读不出来交回空映射，由调用方决定怎么办。

编排主模块（`meeting/offline/runner`）、重放工具（`meeting/offline/replay.py`）与
逐块进度记录的立档入口都从这里取场次元信息；本模块不 import 包内任何其他模块，
处于依赖图的最下游——对本模块的引用只有自上而下一条方向，反向 import 环不存在。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("meeting.offline.runner.session_info")



#: 两路（与 `meeting.backends.base.SOURCE_IDS` 口径一致；不 import 它，
#: 免得 `meeting.backends` 这条链被包进会后路径）
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")


def session_id_of(session_dir: Path | str) -> str:
    """场次编号 = 场次目录名（目录名是服务侧生成的，本模块不另起规则）。"""
    return Path(session_dir).name


def declared_sources(session_dir: Path | str) -> tuple[str, ...]:
    """这一场该跑哪几路：读 `session.json` 的 `sources[].id`，读不到按两路全集。"""
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
