"""造场探针：被强杀在收尾半路的场次能收口。

**造场**：盘上那份 `session.json` 就是「强杀留下的现场」本身，人一个字节不手改产品产物。

**硬约束**：autopilot 节点绝不许把纪要交付给 DSH 或真实跑会后链 —— 两个后端
全是本用例自带的假件：`post.engine` / `minutes.hook` 都指 `module:Class` 假实现（临时目录
经 `PYTHONPATH` 注入子进程），假 minutes 只往**临时工作区**写一个标记文件，不探任何
`minutes.dsh_url`、不发任何网络请求。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action

pytestmark = pytest.mark.slow

#: 假 minutes 钩子拿「在飞」撑多久（秒）：第二发并发要撞上「有一趟在飞」的闸，
#: 第一趟的线程得还活着。取值收在红线内：够两发 HTTP 一前一后即可。
MINUTES_IN_FLIGHT_SECONDS = 1.0

MINUTES_MARKER_TIMEOUT_SECONDS = 3.0

#: 假 minutes 落下的标记文件名（写进**临时工作区**，不碰任何真实路径）。
MINUTES_MARKER_NAME = "probe_minutes_marker.json"

#: 假件模块名与源码（子进程经 `PYTHONPATH` 导入；`post.engine` 与 `minutes.hook` 都指它）。
FAKE_BACKEND_MODULE_NAME = "fake_probe_backends"

FAKE_BACKEND_MODULE_SRC = f'''
"""探针假件：会后离线引擎与纪要钩子（绝不离机、绝不探 DSH、不发任何网络请求）。"""
import json
import time
from pathlib import Path

MARKER_NAME = {MINUTES_MARKER_NAME!r}
IN_FLIGHT_SECONDS = {MINUTES_IN_FLIGHT_SECONDS!r}


class FakeProbeEngine:
    """会后离线引擎的假件：一台都不真拉，`transcribe` 一段都不交回。"""

    def __init__(self, model=None, dtype=None):
        self.device = "cpu"
        self.model = model
        self.dtype = dtype

    def transcribe(self, audio, *args, **kwargs):
        return []


class FakeProbeMinutesHook:
    """纪要钩子的假件：只往目标目录写一个标记文件（等一小会儿再写，撑住「在飞」）。"""

    def claim(self, target, *args, **kwargs):
        return None

    def launch(self, target, *args, **kwargs):
        time.sleep(IN_FLIGHT_SECONDS)
        marker = Path(str(target)) / MARKER_NAME
        marker.write_text(
            json.dumps({{"target": str(target)}}, ensure_ascii=False), encoding="utf-8"
        )
        return {{"state": "done"}}
'''


def _craft_killed_mid_drain_session(place: Path) -> Path:
    """造一场「被强杀在收尾半路」的会：盘上停在 draining + interrupted、结束标记缺失。"""
    workspace = place / "261016-强杀现场"
    workspace.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": workspace.name,
        "dir": str(workspace),
        "date": "2026-10-16",
        "mode": "both",
        "state": "draining",
        "created_at": "2026-10-16T09:00:00+08:00",
        "started_at": "2026-10-16T09:00:00+08:00",
        "ended_at": None,
        "interrupted": True,
        "drain": {
            "steps": [
                {"id": "flush", "status": "running", "detail": "", "error": None},
                {"id": "log", "status": "waiting", "detail": "", "error": None},
                {"id": "post", "status": "waiting", "detail": "", "error": None},
            ]
        },
    }
    (workspace / "session.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return workspace


def _get(base_url: str, path: str, *, timeout: float = 5.0):
    return helpers.local_get(f"{base_url}{path}", timeout=timeout)


def test_a_session_killed_mid_drain_reads_ended_and_post_actions_are_acceptable(
    tmp_path: Path, meeting_env: Any, service_process: Any
) -> None:
    """被强杀在收尾半路的那一场：读数收口为「录音结束」、会后两按钮照常受理。"""
    env = meeting_env

    # 假件模块落临时目录，经 PYTHONPATH 注入子进程（仓库根本身必须留在最前，import meeting 才通）
    fake_dir = tmp_path / "fake-probe-backends"
    fake_dir.mkdir()
    (fake_dir / f"{FAKE_BACKEND_MODULE_NAME}.py").write_text(
        FAKE_BACKEND_MODULE_SRC, encoding="utf-8"
    )
    pythonpath = os.pathsep.join([str(helpers.REPO_ROOT), str(fake_dir)])

    workspace = _craft_killed_mid_drain_session(env.out_root)
    env.write_config(
        {
            "capture.backend": "dev",
            "post.hook": f"{FAKE_BACKEND_MODULE_NAME}:FakeProbeEngine",
            "post.engine": f"{FAKE_BACKEND_MODULE_NAME}:FakeProbeEngine",
            "minutes.hook": f"{FAKE_BACKEND_MODULE_NAME}:FakeProbeMinutesHook",
        }
    )

    proc = service_process(
        home=env.home,
        out_root=env.out_root,
        extra_env=env.env,
        pythonpath=pythonpath,
    )
    base_url = proc.wait_ready(timeout=20.0)

    # ① 登记进工作台（不改写盘上那份现场）
    opened = post_action(base_url, "workspace_open", {"dir": str(workspace)})
    assert opened.status_code == 200, opened.text
    assert opened.json()["workspace"]["kind"] == "meeting"

    listing = _get(base_url, "/api/sessions")
    assert listing.status_code == 200, listing.text
    rows = {row["dir"]: row for row in listing.json()["sessions"]}
    row = rows[str(workspace)]
    assert row["state"] == "ended", row
    assert row["dir_exists"] is True and row["kind"] == "meeting"

    single = _get(base_url, f"/api/sessions/{quote(str(workspace))}")
    assert single.status_code == 200, single.text
    assert single.json()["state"] == "ended"

    offline_receipt = post_action(base_url, "post_offline", {"dir": str(workspace)})
    assert offline_receipt.status_code == 200, offline_receipt.text
    body = offline_receipt.json()
    assert body["ok"] is True
    assert isinstance(body["revision"], int), body
    assert body["post"] == {"kind": "offline", "dir": str(workspace)}, body

    # 等离线那一趟收工（假件引擎 + 场里没有音频，毫秒级）：顶层 post_jobs 退回空数组
    wait_until(
        lambda: _get(base_url, "/api/sessions").json()["post_jobs"] == [],
        timeout=3.0,
    )

    # ⑤「生成纪要」：假件钩子在场 → 200 受理（同一信封）
    minutes_receipt = post_action(base_url, "post_minutes", {"dir": str(workspace)})
    assert minutes_receipt.status_code == 200, minutes_receipt.text
    minutes_body = minutes_receipt.json()
    assert minutes_body["ok"] is True
    assert isinstance(minutes_body["revision"], int), minutes_body
    assert minutes_body["post"] == {
        "kind": "minutes",
        "dir": str(workspace),
        "inputs": "none",  # 现场没有稿子：回执如实说破输入档位（纪要免闸的读数）
    }, minutes_body

    second = post_action(base_url, "post_minutes", {"dir": str(workspace)})
    assert second.status_code == 409, second.text
    refusal = second.json()
    assert refusal["ok"] is False and refusal["code"] == "draining", refusal
    assert refusal["detail"].get("kind") == "minutes", refusal
    assert refusal["detail"].get("dir") == str(workspace), refusal
    assert "落盘" not in refusal["message"] and "收尾" not in refusal["message"], refusal

    # ⑦ 假 minutes 真的跑过（标记文件落在临时工作区）：全程没碰任何真实端点
    marker = workspace / MINUTES_MARKER_NAME
    wait_until(lambda: marker.exists(), timeout=MINUTES_MARKER_TIMEOUT_SECONDS)
    assert json.loads(marker.read_text(encoding="utf-8"))["target"] == str(workspace)
