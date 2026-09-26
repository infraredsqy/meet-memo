"""`tests/test_restart_consistency.py` —— 重启自洽闭环（phase2 §5.1 第五行；§3.2.3）。

覆盖：

  - 造一个 `state: "recording"` / `"draining"` / `interrupted: true` 的场次目录 → 起服务 →
    `top_state == "idle"` 且 `notice.kind == "restart_unfinished"`、文案含音频路径与 log 路径；
  - 该记录被改写为 `state: "draining"` + `interrupted: true`，**原 `steps` 原样保留、不伪造
    终态**，场次目录里其余文件逐字不变、**不新建任何目录**（§2.3 第 7 条的同源口径）；
  - **这一场未收尾的场次要拦住新场次**：扫描命中并改写之后，新的 `start` 回 `draining`(409)，
    `detail` 带场次目录 + 失败步 + 场次 log 路径，且**没有覆盖现场**；
  - 再起一次读到**同一副面孔**；**绝不自动续录、绝不出现「录音中」**；
  - `state: "done"` / `"failed"` 的场次**不**触发 `notice`、**不**被改写；
  - 输出根不存在时扫出「没有未收尾场次」且**不趁机建目录**；
  - 服务级主 log 里那行含场次目录与 `state` / `interrupted` 的 `WARNING` 照旧出现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import _helpers as helpers

#: 显式等待上限（秒）
RESTART_POLL_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# 现场造法
# ---------------------------------------------------------------------------


def session_record(
    *,
    session_id: str = "250906",
    state: str = "recording",
    interrupted: bool = False,
    ended_at: str | None = None,
    step_statuses: dict | None = None,
) -> dict:
    """一份字段齐全的 `session.json`（两路、四步、`service` 两键都在）。"""
    statuses = step_statuses or {}
    steps = [
        {
            "id": step_id,
            "status": statuses.get(step_id, "waiting"),
            "detail": f"（{step_id} 这一趟的说明）" if step_id in statuses else "",
            "error": None,
        }
        for step_id in ("flush", "transcode", "log", "post")
    ]
    return {
        "session_id": session_id,
        "dir": "",
        "mode": "both",
        "state": state,
        "created_at": "2025-09-06T10:00:00+08:00",
        "started_at": "2025-09-06T10:00:00+08:00",
        "ended_at": ended_at,
        "sources": [
            {"id": "mic", "device": "某设备", "archive": "meeting_audio_mic.pcm"},
            {"id": "loopback", "device": None, "archive": "meeting_audio_loopback.pcm"},
        ],
        "drain": {"steps": steps},
        "interrupted": interrupted,
        "service": {"pid": 4242, "started_at": "2025-09-06T10:00:00+08:00"},
    }


def make_session_dir(out_root: Path, session_id: str = "250906", **kwargs) -> Path:
    """在输出根下造一场：`session.json` + 一份音频归档 + 一份场次 log（后两个不该被动）。"""
    session_dir = out_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    record = session_record(session_id=session_id, **kwargs)
    record["dir"] = str(session_dir)
    (session_dir / "session.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (session_dir / "meeting_audio_mic.pcm").write_bytes(b"\x01" * 24)
    (session_dir / "meeting.log").write_text("场次 log 的一行\n", encoding="utf-8")
    return session_dir


def read_record(session_dir: Path) -> dict:
    return json.loads((session_dir / "session.json").read_text(encoding="utf-8"))


def start_service(service_process, home: Path, out_root: Path):
    proc = service_process(home=home, capture="dev", out_root=out_root)
    proc.wait_ready()
    return proc


def post(proc, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None:
        body["args"] = args
    return helpers.local_post(
        f"{proc.base_url}/api/actions",
        json=body,
        headers={"Content-Type": "application/json", "X-Meeting-Service": "1"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# 命中：挂 notice + 改写现场
# ---------------------------------------------------------------------------


def test_unfinished_session_surfaces_as_notice_without_resuming(tmp_path, service_process):
    """未收尾场次 → 空闲态 + `notice.restart_unfinished`；现场被改写、**绝不自动续录**。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = make_session_dir(out_root, state="recording", interrupted=False)
    before_files = sorted(item.name for item in session_dir.iterdir())

    proc = start_service(service_process, home, out_root)
    snapshot = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()

    # 空闲态 + 提示（绝不把状态恢复成「录音中」）
    assert snapshot["top_state"] == "idle"
    assert snapshot["session"] is None
    assert snapshot["notice"] is not None
    assert snapshot["notice"]["kind"] == "restart_unfinished"
    assert snapshot["notice"]["session_dir"] == str(session_dir)
    assert snapshot["notice"]["log_path"]
    assert str(session_dir) in snapshot["notice"]["text"]
    assert snapshot["notice"]["log_path"] in snapshot["notice"]["text"]

    # 该记录被改写成「收尾中 + 已打断」，原 `steps` 原样保留、不伪造终态
    rewritten = read_record(session_dir)
    assert rewritten["state"] == "draining"
    assert rewritten["interrupted"] is True
    assert rewritten["ended_at"] is None
    assert rewritten["drain"]["steps"] == session_record()["drain"]["steps"]
    assert set(rewritten) == set(session_record())

    # 场次目录里其余文件逐字不变、不新建任何目录
    assert sorted(item.name for item in session_dir.iterdir()) == before_files
    assert (session_dir / "meeting_audio_mic.pcm").read_bytes() == b"\x01" * 24
    assert (session_dir / "meeting.log").read_text(encoding="utf-8") == "场次 log 的一行\n"
    assert sorted(item.name for item in out_root.iterdir()) == ["250906"]

    # 服务级主 log 里那行含场次目录与 state / interrupted 的 WARNING 照旧出现
    log_text = proc.log_text()
    warnings = [
        line for line in log_text.splitlines() if "WARNING" in line and "启动扫描" in line
    ]
    assert any("250906" in line and "recording" in line for line in warnings), warnings
    # 没有场次开始行（绝不自动续录）
    assert "场次开始：" not in log_text


def test_unfinished_session_blocks_the_next_start_without_overwriting_it(tmp_path, service_process):
    """扫描命中并改写之后，新的 `start` 回 `draining`(409)，且**没有覆盖现场**。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = make_session_dir(
        out_root, state="draining", step_statuses={"flush": "done", "post": "failed"}
    )

    proc = start_service(service_process, home, out_root)
    # 扫描改写之后的那一版（改写只会发生一次）
    helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0)
    rewritten = read_record(session_dir)
    assert rewritten["state"] == "draining"
    assert rewritten["interrupted"] is True

    response = post(proc, "start")
    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["code"] == "draining"
    detail = payload["detail"]
    assert detail["dir"] == str(session_dir)
    assert detail["session_id"] == "250906"
    assert [item["id"] for item in detail["failed_steps"]] == ["post"]
    assert detail["log_path"]

    # 现场没有被覆盖：还是刚改写出来的那一版
    assert read_record(session_dir) == rewritten
    assert sorted(item.name for item in out_root.iterdir()) == ["250906"]
    # 顶态仍是 idle（拦住的是「再开一场」这个动作）
    assert helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()["top_state"] == "idle"


def test_second_restart_reads_the_same_face(tmp_path, service_process):
    """再重启一次读到**同一副面孔**（不会因为「刚才还写着 recording」而反复改口）。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = make_session_dir(out_root, state="recording")

    first = start_service(service_process, home, out_root)
    first_snapshot = helpers.local_get(f"{first.base_url}/api/state", timeout=5.0).json()
    first_record = read_record(session_dir)
    first.terminate()

    second = start_service(service_process, home, out_root)
    second_snapshot = helpers.local_get(f"{second.base_url}/api/state", timeout=5.0).json()
    second_record = read_record(session_dir)

    assert second_snapshot["top_state"] == "idle"
    assert second_snapshot["notice"]["kind"] == "restart_unfinished"
    assert second_snapshot["notice"]["session_dir"] == str(session_dir)
    assert second_record == first_record, "第二次读到的还是同一副面孔"
    assert second_record["state"] == "draining"
    assert second_record["interrupted"] is True
    assert first_snapshot["notice"]["session_dir"] == second_snapshot["notice"]["session_dir"]


def test_interrupted_true_alone_counts_as_unfinished(tmp_path, service_process):
    """判据只有一条并集：`state ∈ {recording, draining}` **或** `interrupted: true`。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = make_session_dir(out_root, state="done", interrupted=True, ended_at="t")

    proc = start_service(service_process, home, out_root)
    snapshot = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    assert snapshot["top_state"] == "idle"
    assert snapshot["notice"]["kind"] == "restart_unfinished"
    rewritten = read_record(session_dir)
    assert rewritten["state"] == "draining"
    assert rewritten["interrupted"] is True
    assert rewritten["drain"]["steps"] == session_record()["drain"]["steps"]


# ---------------------------------------------------------------------------
# 不命中：已收尾的场次
# ---------------------------------------------------------------------------


def test_finished_sessions_are_left_alone(tmp_path, service_process):
    """`state: "done"` / `"failed"` 的场次**不**触发 `notice`、**不**被改写。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    done_dir = make_session_dir(out_root, session_id="260901", state="done", ended_at="t1")
    make_session_dir(out_root, session_id="260902", state="failed", ended_at="t2")
    older_done = read_record(done_dir)
    failed_dir = out_root / "260902"
    failed_before = read_record(failed_dir)

    proc = start_service(service_process, home, out_root)
    snapshot = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    # 最新一场是 `failed`：不挂 notice、不改写
    assert snapshot["top_state"] == "idle"
    assert snapshot["notice"] is None
    assert read_record(failed_dir) == failed_before
    assert read_record(done_dir) == older_done

    log_text = proc.log_text()
    scan_warnings = [
        line for line in log_text.splitlines()
        if "WARNING" in line and "启动扫描" in line
    ]
    assert scan_warnings == [], f"已收尾的场次不该落启动扫描警告：{scan_warnings}"


def test_missing_out_root_is_scanned_as_nothing_and_not_created(tmp_path, service_process):
    """输出根不存在 → 扫出「没有未收尾场次」，且**不趁机建目录**。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "not-there-yet"
    assert not out_root.exists()

    proc = start_service(service_process, home, out_root)
    snapshot = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    assert snapshot["top_state"] == "idle"
    assert snapshot["notice"] is None
    assert not out_root.exists(), "扫描不该顺手建输出根"

    log_text = proc.log_text()
    assert not [line for line in log_text.splitlines() if "启动扫描" in line]


def test_readable_but_broken_record_counts_as_a_miss(tmp_path, service_process):
    """文件存在却读不出来 → 按未命中处理、恰一行「读不出来」WARNING，服务照常起。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = out_root / "250906"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_bytes(b'{"state": "recording", "\xff\xfe": 1}')

    proc = start_service(service_process, home, out_root)
    snapshot = helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()
    assert snapshot["top_state"] == "idle"
    assert snapshot["notice"] is None
    warnings = [
        line for line in proc.log_text().splitlines()
        if "WARNING" in line and "读不出来" in line
    ]
    assert len(warnings) == 1, warnings
    # 那份文件原样不动
    assert (session_dir / "session.json").read_bytes() == b'{"state": "recording", "\xff\xfe": 1}'
