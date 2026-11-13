"""启动核对（的 d5）：在飞记录 + 盘上 `running` → 补成 `interrupted`。

治的是 spec 点名的那条病：「杀了进程就永远停在在跑」。`post_jobs` 是内存事实（按 `(种类, 场次)` 多槽）、进程一死
就没；本节点给服务侧补一格跨进程事实（配置目录的 `post_jobs.json`），服务启动时按它
把「盘上停着 running」的那一场 offline 会后核对成 `interrupted`。

全部用例走**真 `startup()`**（dev 采集替身、`HOME` 指临时目录）：核对是启动序列的一步，
这一层要钉的就是「启动那一刻发生了什么」。不扫目录：范围只有记录里点名的那一场。
"""

from __future__ import annotations

import json
from pathlib import Path

from meeting import config as config_module
from meeting.service import postjobs
from meeting.service.app import build_runtime

from meeting.offline import artifacts


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home-post-reconcile"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _runtime(tmp_path: Path, monkeypatch, **values):
    """起一台真运行时（`HOME` 指临时目录，启动序列跑到核对那一步）。"""
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()
    applied = {
        **config_module.DEFAULT_VALUES,
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        **values,
    }
    runtime = build_runtime(
        config_module.Config(applied=applied, sources={key: "默认" for key in applied}, path="")
    )
    return runtime, previous


def _session_dir(tmp_path: Path, *, state: str, session_id: str = "250919") -> Path:
    """一个带 `session.json` 与指定档状态文件的场次目录（核对的靶子）。"""
    root = tmp_path / "sessions" / session_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "dir": str(root),
                "state": "done",
                "mode": "both",
                "sources": [],
                "segments": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if state != "none":
        artifacts.write_status(
            root / artifacts.STATUS_NAME,
            session_id=session_id,
            state=state,
            started_at="2025-09-19T20:00:00",
            sources=["mic", "loopback"],
            artifacts={},
        )
    return root


def test_startup_reconciles_an_interrupted_offline_job(tmp_path, monkeypatch) -> None:
    """记录在 + 盘上 `running` → 补成 `interrupted`（缺口注记进 error），记录清空。"""
    target = _session_dir(tmp_path, state="running")

    # 先起运行时（`HOME` 指临时目录）再落记录：记录跟着被测的那台服务走
    runtime, previous = _runtime(tmp_path, monkeypatch)
    postjobs.record("offline", str(target), now="2025-09-19T20:00:05")
    try:
        import asyncio

        asyncio.run(runtime.startup())
    finally:
        config_module.set_current(previous)

    document = artifacts.read_status(target / artifacts.STATUS_NAME)
    assert document["state"] == "interrupted"
    assert document["started_at"] == "2025-09-19T20:00:00", "开始时刻照盘上那一版"
    assert document["finished_at"]
    assert "服务重启" in (document["error"] or ""), document["error"]
    assert postjobs.load() == [], "核对完记录清空"


def test_startup_only_clears_the_record_when_disk_is_not_running(
    tmp_path, monkeypatch
) -> None:
    """那趟其实跑完了（盘上 `done`）→ 只清记录，状态文件一个字节不动。"""
    target = _session_dir(tmp_path, state="done")
    before = (target / artifacts.STATUS_NAME).read_text(encoding="utf-8")

    runtime, previous = _runtime(tmp_path, monkeypatch)
    postjobs.record("offline", str(target), now="2025-09-19T20:00:05")
    try:
        import asyncio

        asyncio.run(runtime.startup())
    finally:
        config_module.set_current(previous)

    assert (target / artifacts.STATUS_NAME).read_text(encoding="utf-8") == before
    assert postjobs.load() == []


def test_startup_only_clears_minutes_records(tmp_path, monkeypatch) -> None:
    """minutes 档的记录只清 + log：纪要那一层有自己的钩子兜底，这里不碰那一场。"""
    target = _session_dir(tmp_path, state="running")
    before = (target / artifacts.STATUS_NAME).read_text(encoding="utf-8")

    runtime, previous = _runtime(tmp_path, monkeypatch)
    postjobs.record("minutes", str(target), now="2025-09-19T20:00:05")
    try:
        import asyncio

        asyncio.run(runtime.startup())
    finally:
        config_module.set_current(previous)

    assert (target / artifacts.STATUS_NAME).read_text(encoding="utf-8") == before
    assert postjobs.load() == []


def test_startup_without_records_touches_nothing(tmp_path, monkeypatch) -> None:
    """没有记录 → 启动照旧，配置目录里连记录文件都可以不出现。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    try:
        import asyncio

        asyncio.run(runtime.startup())
    finally:
        config_module.set_current(previous)

    assert postjobs.load() == []
    assert not Path(postjobs.path()).exists()


def test_a_corrupt_record_file_is_read_as_empty(tmp_path, monkeypatch) -> None:
    """记录文件坏了（半个 JSON）：按空表起、不阻塞启动 —— 核对不许把启动带崩。"""
    home = _home(tmp_path)
    (Path(home) / "post_jobs.json").write_text('{"kind": "off', encoding="utf-8")

    runtime, previous = _runtime(tmp_path, monkeypatch)
    try:
        import asyncio

        asyncio.run(runtime.startup())
    finally:
        config_module.set_current(previous)

    assert postjobs.load() == []


def test_two_in_flight_jobs_keep_both_records_and_clear_takes_one_slot(
    tmp_path, monkeypatch
) -> None:
    """多槽：offline(A) 在飞时再起 minutes(A)（免闸后的常规路径）与 offline(B)，
    三条记录同时在盘；`clear` 只摘调用方那一格 —— 先收工的那趟不把还在飞的另一趟
    记录一并清掉（清掉了，「杀了进程就永远停在在跑」就在并发场景复活）。"""
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    target_a = str(_session_dir(tmp_path, state="done", session_id="250919"))
    target_b = str(_session_dir(tmp_path, state="done", session_id="260920"))

    postjobs.record("offline", target_a, now="2025-09-19T20:00:05")
    postjobs.record("minutes", target_a, now="2025-09-19T20:00:06")
    postjobs.record("offline", target_b, now="2025-09-19T20:00:07")
    assert [(item["kind"], item["dir"]) for item in postjobs.load()] == [
        ("offline", target_a),
        ("minutes", target_a),
        ("offline", target_b),
    ]

    # 同槽再落一次是改（覆盖 started_at），不是添第二条
    postjobs.record("offline", target_a, now="2025-09-19T20:01:00")
    same_slot = [
        item for item in postjobs.load() if item["kind"] == "offline" and item["dir"] == target_a
    ]
    assert len(same_slot) == 1
    assert same_slot[0]["started_at"] == "2025-09-19T20:01:00"

    # 先收工的那趟只摘自己那一格：异槽（含还在飞的另一场）原样保留
    postjobs.clear("minutes", target_a)
    slots = [(item["kind"], item["dir"]) for item in postjobs.load()]
    assert ("minutes", target_a) not in slots
    assert ("offline", target_a) in slots
    assert ("offline", target_b) in slots

    # 整份清空只归启动核对（它已把每一条处置过）
    postjobs.clear_all()
    assert postjobs.load() == []


def test_an_old_single_entry_file_is_read_as_is(tmp_path, monkeypatch) -> None:
    """旧单条文件（形状就是条目数组）原样读进来，不做迁移仪式。"""
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    target = str(_session_dir(tmp_path, state="done"))
    record_file = Path(home) / ".config" / "meeting" / "post_jobs.json"
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(
        json.dumps(
            [{"kind": "offline", "dir": target, "started_at": "2025-09-19T20:00:05"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert postjobs.load() == [
        {"kind": "offline", "dir": target, "started_at": "2025-09-19T20:00:05"}
    ]
