"""`GET /api/sessions/{id}` 那只单场状态端点的用例（会后视图的进度区）。

本文件盯住五件事：

  - **跑到一半读得到**：会后的活儿正在飞时打这只端点，读得到「第 N 块 / 共 M 块、
    正在跑哪一路、已经跑了多久」；跑完之后同一只端点照旧给得出读数（改读盘上那份记录）；
  - **两块回答两件事**：`offline.state` 照盘上那份如实答（盘上写 `running` 就答
    `running`，崩在半路那一场因此答 `running`；盘上写 `partial` / `failed` 也照它自己
    那一档答），`offline.in_process` 答「本进程里此刻有没有这一趟活」；
  - **前七格与列表同源**：同一份盘上 `session.json`，`GET /api/sessions` 那一行与
    这一只端点的前七格给同一个值（域外的值两处都如实落 `null`）；
  - **纯读**：读它不涨 `revision`、不广播，快照九键一个字不动；
  - **失败档**：目录不在原地、这一场没在工作台清单里 → 404，回通用失败信封。

**全部用假件**：离线段那一趟在现场把 `runner.build_engine` 换成假引擎（与
`tests/test_post_idempotency.py` 同一处接缝），一次真推理都不发生，也不占显卡。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.offline import artifacts as offline_artifacts
from meeting.offline import progress as offline_progress
from meeting.offline import runner as offline_runner
from meeting.service.app import build_runtime
from meeting.service.routes import api_routes
from tests._offline_fakes import GatedEngine, sparse_pcm, synth_session

#: 等后台那一趟收工的 join 上限（等到了就立刻回，不赌固定时长）
JOB_TIMEOUT_SECONDS = 5.0
CHUNK_SECONDS = 10.0


def _runtime(tmp_path: Path, monkeypatch, **values):
    """起一台真运行时（dev 采集替身），配置里覆盖 `**values`。"""
    home = tmp_path / "home-session-status"
    home.mkdir(parents=True, exist_ok=True)
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
    runtime._resolve_backends()
    # 绑定一个回环端口：读快照那一条（`to_dict()`）只认监听 socket 给回来的端口
    runtime.set_binding("127.0.0.1", 8765)
    return runtime, previous


def _workspace_with_audio(tmp_path: Path, *, seconds: float = 25.0) -> Path:
    """一场已有音频的三块会（块长 10 秒 → 每路 3 块），并且已经跑过一趟。"""
    session = synth_session(tmp_path / "ws-session-status" / "250906")
    for source in ("mic", "loopback"):
        sparse_pcm(session, source, seconds)
    return session


def _wait_job_done(runtime) -> None:
    """等后台那一趟收工：**直接 join 那条线程**（它是这一格的观测量，不必轮询）。

    线程对象由运行时自己拿着（`post_jobs` 的 `(kind, dir)` 那一格）；这里先取它、再等它
    —— 与 `post_jobs_state()` 那条"看线程活没活着"的判据同源。
    """
    threads = [
        job.get("thread")
        for job in runtime.post_jobs.values()
        if isinstance(job, dict) and job.get("thread") is not None
    ]
    for thread in threads:
        thread.join(timeout=JOB_TIMEOUT_SECONDS)
    assert not runtime.post_jobs_state(), "等不到会后的活儿收工"


def _write_session_json(session: Path, document: dict) -> None:
    """把这一场的 `session.json` 换成给定的那一份（按字节写盘，不经过写侧代码）。"""
    (session / "session.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def _ledger_and_status(
    session: Path, *, status_state: str | None, rows: list | None = None
) -> None:
    """按真实形状落一份逐块记录（缺省 mic 一块跑完）与一版状态文件（`None` = 不落状态文件）。

    进度记录的两段文字与块号都是确定值；状态文件那一版由 `status_state` 给
    （`running` 就是「上一次落的那一版 `running` 还在盘上」那一档）。
    """
    offline_progress.write_progress(
        offline_progress.ledger_path(session),
        session_id=session.name,
        sources=["mic", "loopback"],
        rows=rows
        if rows is not None
        else [
            offline_progress.ChunkProgress(
                "mic", 1, 0.0, 10.0, "transcribed",
                "[00:00] **c01:S01**: 第一块的一句话", 1, 3.0,
            ).to_row(chunks_total=3)
        ],
    )
    if status_state is None:
        return
    offline_artifacts.write_status(
        session / offline_artifacts.STATUS_NAME,
        session_id=session.name,
        state=status_state,
        started_at="2025-09-06T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
    )


@pytest.fixture()
def restore_config():
    previous = config_module.current()
    yield
    config_module.set_current(previous)


def test_the_endpoint_is_registered() -> None:
    """路由注册点里那条路径在（`{dir:path}` 是为了收下 URL 编码的绝对路径）。"""
    paths = [route.path for route in api_routes()]
    assert "/api/sessions/{dir:path}" in paths


def test_the_progress_is_readable_while_the_offline_run_is_in_flight(
    tmp_path, monkeypatch, restore_config
) -> None:
    """跑到一半打这只端点：第 N 块 / 共 M 块、当前哪一路、已跑多久都读得到。"""
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    opened = runtime.workspace_open(str(session))
    assert opened["dir"] == str(session)

    gate = threading.Event()
    engine = GatedEngine(gate, block_at=2)  # mic 第 2 块卡住
    monkeypatch.setattr(offline_runner, "build_engine", lambda config=None: engine)
    try:
        runtime.post_offline({"dir": str(session)})
        assert engine.reached.wait(timeout=JOB_TIMEOUT_SECONDS), "引擎没跑到 mic 的第 2 块"

        revision_before = runtime.revision
        body = runtime.session_status_dict(str(session))

        assert body["dir"] == str(session)
        assert body["session_id"] == "250919", "盘上 session.json 那一格原样给回"
        assert body["kind"] == "meeting"
        assert body["state"] == "ended"  # 三档口径：盘上有 session.json（录音已发生过）
        assert body["has_offline"] is False, "跑到一半时正式稿还没落，那一格照旧是假"
        # `post` 那一格是多槽登记里**这一场**的在飞清单（可以同时有 offline 与 minutes）
        assert [item["kind"] for item in body["post"]] == ["offline"], body["post"]
        assert body["post"][0]["running"] is True, "这一趟正在飞"
        assert body["post"][0]["finished_at"] is None, "还没收工，这一格如实为空"
        offline = body["offline"]
        assert offline["state"] == "running"
        assert offline["in_process"] is True, "这一趟正在本进程里跑（与 post.running 同源）"
        assert offline["current_source"] == "mic", offline
        assert offline["elapsed_seconds"] >= 0.0
        assert offline["started_at"], "起跑时刻要读得到"
        assert offline["ledger"] == "meeting_offline_progress.jsonl"
        mic = next(item for item in offline["sources"] if item["source"] == "mic")
        assert (mic["done"], mic["total"], mic["current_chunk"]) == (1, 3, 2)
        assert mic["counts"] == {"transcribed": 1, "silent": 0, "failed": 0, "unjudged": 0}
        assert runtime.revision == revision_before, "读它是纯读：不涨 revision"
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)

    # 跑完之后同一只端点照旧给得出读数（改读盘上那份记录）：两路各 3 块都跑完
    after = runtime.session_status_dict(str(session))
    assert after["has_offline"] is True
    assert after["offline"]["state"] == "done"
    assert after["offline"]["in_process"] is False, "跑完之后这一格翻回假（盘上那份记录照读）"
    per_source = {item["source"]: item for item in after["offline"]["sources"]}
    assert (per_source["mic"]["done"], per_source["mic"]["total"]) == (3, 3)
    assert (per_source["loopback"]["done"], per_source["loopback"]["total"]) == (3, 3)
    assert per_source["mic"]["remaining"] == 0
    assert after["offline"]["finished_at"], "跑完那一刻的墙钟戳要读得到"


def test_the_endpoint_falls_back_to_the_ledger_after_a_process_restart(
    tmp_path, monkeypatch, restore_config
) -> None:
    """服务重启之后（进程里没有现场）读盘上那份记录：块数照旧读得到，已跑时长如实交回 `None`。"""
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    runtime.workspace_open(str(session))
    monkeypatch.setattr(offline_runner, "build_engine", lambda config=None: GatedEngine(
        threading.Event(), block_at=0
    ))
    try:
        # 直接用产品那一段实现跑一趟（不经过线程，模拟"上一次服务进程跑完的那一趟"）
        runtime._run_post_offline(str(session))
        runtime.post_jobs.pop(("offline", str(session)), None)  # 假装本进程没有这一趟的现场
        body = runtime.session_status_dict(str(session))
        offline = body["offline"]
        assert offline["state"] == "done"
        assert offline["current_source"] is None
        assert offline["elapsed_seconds"] is None, "跨进程算不出单调钟的差值，这一格不猜"
        assert offline["started_at"] and offline["finished_at"]
        assert {item["done"] for item in offline["sources"]} == {3}
    finally:
        config_module.set_current(previous)


def test_the_seven_cells_match_the_list_row_when_the_disk_is_out_of_range(
    tmp_path, monkeypatch, restore_config
) -> None:
    """前七格与 `GET /api/sessions` 同一行**同源**：盘上落在域外时两处给同一个值。

    契约 4.9 写着前七格与 4.8 同一行同源、逐格语义见 4.8；这一条把「同一个事实在两处
    给同一个值」钉住：`session.json` 的 `state` 写成取值域之外的 `waiting` 时，三档口径
    照样答 `ended`（盘上有这份记录 = 录音已发生过，盘上 `state` 停在哪一档不进这个口径）、
    `date` 写成数字 `20250906`、`mode` 写成 `null` 时两处都如实答 `null`
    —— 单场那一只不另开一套读法。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _write_session_json(
        session,
        {
            "session_id": "250919",
            "state": "waiting",        # 盘上取值域之外的值（三档口径照答 ended）
            "date": 20250906,          # 契约写的是 <YYYY-MM-DD>，这里是数字
            "mode": None,              # 契约写的是 both|mic|loopback
        },
    )
    opened = runtime.workspace_open(str(session))
    assert opened["dir"] == str(session)
    try:
        row = next(
            item for item in runtime.sessions_dict()["sessions"] if item["dir"] == opened["dir"]
        )
        body = runtime.session_status_dict(str(session))
        for cell in ("state", "date", "mode"):
            assert body[cell] == row[cell], (
                f"{cell} 这一格两处该同值：列表给 {row[cell]!r}、单场给 {body[cell]!r}"
            )
        assert row["state"] == "ended", "盘上有 session.json 一律答 ended（三档口径）"
        assert row["date"] is None, "不是字符串的日期如实 null"
        assert row["mode"] is None
    finally:
        config_module.set_current(previous)


def test_a_stale_running_status_file_is_answered_as_it_is_on_disk(
    tmp_path, monkeypatch, restore_config
) -> None:
    """崩在半路那一场：`offline.state` 照盘上那份答 `running`，另立一格答「本进程里没在跑」。

    盘上停着上一次落的那版 `running`、记录里只有 mic 的第一块，而这一趟谁都没在跑
    （进程重启过、命令行跑完退出了）。读数如实分两层：`state` 就是盘上那个 `running`
    （不在这里替它二选一），`in_process` 答 `false` 把「此刻没有东西在跑」说破 ——
    两块合起来读得出「盘上写着在跑、其实没在跑」。被打断是另一档（落地）：
    那一场照盘答 `interrupted`，与这一档分得开。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _ledger_and_status(session, status_state="running")
    runtime.workspace_open(str(session))
    try:
        body = runtime.session_status_dict(str(session))
        offline = body["offline"]
        assert offline["state"] == "running", "照盘上那份如实答，不做二次解读"
        assert offline["in_process"] is False, "本进程里没有这一趟活"
        assert offline["elapsed_seconds"] is None
        assert offline["current_source"] is None
        per_source = {item["source"]: item for item in offline["sources"]}
        assert (per_source["mic"]["done"], per_source["mic"]["total"]) == (1, 3)
        assert per_source["mic"]["current_chunk"] is None
        assert body["post"] == []
    finally:
        config_module.set_current(previous)


def test_a_failed_status_file_is_answered_as_failed(tmp_path, monkeypatch, restore_config) -> None:
    """盘上落 `failed` 的那一场：`offline.state` 答 `failed`，**不许折成 `done`**。

    折了之后，一趟整个失败的会后与一趟跑成的会后在读数上同形 —— 读方（界面这一格、
    下拉看这一场的人）就被骗了。这一格照盘上那份答，与 `running` / `ledger` 那两档
    同一个姿态：盘上写什么就答什么。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _ledger_and_status(session, status_state="failed")
    runtime.workspace_open(str(session))
    try:
        body = runtime.session_status_dict(str(session))
        assert body["offline"]["state"] == "failed", "盘上那份的终态照它答"
        assert body["offline"]["in_process"] is False
        assert body["post"] == []
    finally:
        config_module.set_current(previous)


def test_a_partial_status_file_is_answered_as_partial(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上落 `partial`（一路整条没交回、另一路跑成了）的那一场：这一格答 `partial`。

    与 `failed` / `running` 那几档同一个姿态：盘上写什么就答什么。折成 `done` 之后，
    一路整条没交回的会后与一趟跑成的会后在读数上同形 —— 读方（界面这一格、下拉看这一场
    的人）就被骗了，而那正是本节点第二条 bug 的痛点。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _ledger_and_status(session, status_state="partial")
    runtime.workspace_open(str(session))
    try:
        body = runtime.session_status_dict(str(session))
        assert body["offline"]["state"] == "partial", "盘上那份的终态照它答"
        assert body["offline"]["in_process"] is False
        assert body["post"] == []
    finally:
        config_module.set_current(previous)


def test_an_interrupted_status_file_is_answered_as_interrupted(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上落 `interrupted`（人停在块边界）的那一场：这一格答 `interrupted`，**不许折成 running**。

    折档行（之前）只认 `done` / `partial` / `failed` 三档，`interrupted` 会掉进
    「别的档一律折 running」的兜底 —— 被人停下的那一场就在读数上永远「在跑」了。加进
    白名单之后它与那三档同一个姿态：盘上写什么就答什么。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _ledger_and_status(session, status_state="interrupted")
    runtime.workspace_open(str(session))
    try:
        body = runtime.session_status_dict(str(session))
        assert body["offline"]["state"] == "interrupted", "盘上那份的终态照它答"
        assert body["offline"]["in_process"] is False, "此刻没有任何东西在跑"
        assert body["post"] == []
    finally:
        config_module.set_current(previous)


def test_the_ledger_without_a_status_file_is_answered_as_a_ledger(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上有记录、没有状态文件：`state` 如实答 `ledger`（成没成没人回答），不冒充 `running`。"""
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    _ledger_and_status(session, status_state=None)
    runtime.workspace_open(str(session))
    try:
        offline = runtime.session_status_dict(str(session))["offline"]
        assert offline["state"] == "ledger"
        assert offline["in_process"] is False
        assert offline["started_at"] is None
        assert offline["finished_at"] is None
        assert {item["done"] for item in offline["sources"]} == {1, 0}
    finally:
        config_module.set_current(previous)


def test_a_directory_that_is_gone_is_a_404(tmp_path, monkeypatch, restore_config) -> None:
    """目录不在原地 → 404（通用失败信封 + 一个机器可读的 `code`）。"""
    from meeting.service.runtime import ActionError

    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    try:
        with pytest.raises(ActionError) as info:
            runtime.session_status_dict(str(tmp_path / "never-existed"))
        assert info.value.status == 404
        assert info.value.code == "session_dir_missing"
        assert info.value.detail["kind"] == "missing"
    finally:
        config_module.set_current(previous)


def test_a_directory_outside_the_workbench_list_is_a_404(
    tmp_path, monkeypatch, restore_config
) -> None:
    """这一场没登记过 → 404 `invalid_setting`（读这一格与列表端点同一套判据）。"""
    from meeting.service.runtime import ActionError

    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    try:
        with pytest.raises(ActionError) as info:
            runtime.session_status_dict(str(session))
        assert info.value.status == 404
        assert info.value.code == "invalid_setting"
        assert info.value.detail["dir"] == str(session)
    finally:
        config_module.set_current(previous)


def test_a_session_that_never_ran_post_reports_an_empty_reading(
    tmp_path, monkeypatch, restore_config
) -> None:
    """一次会后都没跑过的一场：读数如实是"还没跑"，`post` 与块数都不编。"""
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    runtime.workspace_open(str(session))
    try:
        body = runtime.session_status_dict(str(session))
        assert body["post"] == []
        assert body["offline"]["state"] == "idle"
        assert body["offline"]["current_source"] is None
        assert body["offline"]["elapsed_seconds"] is None
        assert [item["done"] for item in body["offline"]["sources"]] == [0, 0]
    finally:
        config_module.set_current(previous)


def test_the_endpoint_leaves_the_snapshot_and_the_list_alone(
    tmp_path, monkeypatch, restore_config
) -> None:
    """快照顶层九键与列表每行十二键各归各位（快照九键是硬门，本节点不碰）。"""
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    runtime.workspace_open(str(session))
    # 引擎一律用假件：产品路径缺省会去造真离线引擎（真加载权重、真占卡），
    # 用例这一层不许走到那一步（见 `tests/test_cli_post_config.py` 的自述事故）。
    monkeypatch.setattr(offline_runner, "build_engine", lambda config=None: GatedEngine(
        threading.Event(), block_at=0
    ))
    try:
        runtime._run_post_offline(str(session))
        runtime.session_status_dict(str(session))
        assert set(runtime.to_dict()) == {
            "revision", "service", "top_state", "session", "sources", "alerts",
            "transcript", "ended", "settings",
        }
        rows = runtime.sessions_dict()["sessions"]
        assert len(rows) == 1
        assert len(rows[0]) == 12, sorted(rows[0])
    finally:
        config_module.set_current(previous)


def test_the_list_carries_the_post_job_cell_at_the_top_level(
    tmp_path, monkeypatch, restore_config
) -> None:
    """`GET /api/sessions` 顶层第三格 `post_jobs`：会后进度界面那格「显卡忙不忙」的读数。

    - 没有活儿在飞时它如实是**空数组**（多槽登记：`(kind, dir)` 一趟一格，可以不止一趟
      在飞）；**每行照旧十二键**（这一格在顶层，不进任何行 —— 快照九键那道硬门原样）；
    - 离线那一趟在本进程里飞着时它带 `{kind, dir, started_at}` 三格（`post_jobs_state()`
      的原文，不另造一份形状）；`kind` 答**这一趟跑的是哪条路**（`offline` / `minutes` /
      `transcode`）；
    - 跑完那一刻线程收工，这一格退回空数组（`post_jobs_state()` 的既有判据）。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch, **{"post.chunk_seconds": CHUNK_SECONDS})
    session = _workspace_with_audio(tmp_path)
    runtime.workspace_open(str(session))

    gate = threading.Event()
    engine = GatedEngine(gate, block_at=1)  # mic 第 1 块卡住
    monkeypatch.setattr(offline_runner, "build_engine", lambda config=None: engine)
    try:
        idle = runtime.sessions_dict()
        assert set(idle) == {"sessions", "recent", "post_jobs"}, sorted(idle)
        assert idle["post_jobs"] == []

        runtime.post_offline({"dir": str(session)})
        assert engine.reached.wait(timeout=JOB_TIMEOUT_SECONDS), "引擎没跑到 mic 的第 1 块"

        revision_before = runtime.revision
        flying = runtime.sessions_dict()
        jobs = flying["post_jobs"]
        assert len(jobs) == 1, jobs
        job = jobs[0]
        assert set(job) == {"kind", "dir", "started_at"}, sorted(job)
        assert job["kind"] == "offline"
        assert job["dir"] == str(session)
        assert job["started_at"], "起跑时刻要读得到"
        for row in flying["sessions"]:
            assert len(row) == 12, sorted(row)

        assert runtime.revision == revision_before, "读它不涨 revision"
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)

    done = runtime.sessions_dict()
    assert done["post_jobs"] == [], "跑完那一刻这一格退回空数组"
    assert {row["dir"]: row for row in done["sessions"]}[str(session)]["state"] == "ended"


# ---------------------------------------------------------------------------
# `minutes` 那一格：纪要那一层的盘上读数（判据读盘，重启后照旧）
# ---------------------------------------------------------------------------


def _minutes_cell(tmp_path, monkeypatch, restore_config, session: Path) -> dict:
    runtime, previous = _runtime(tmp_path, monkeypatch)
    try:
        runtime.workspace_open(str(session))
        return runtime.session_status_dict(str(session))["minutes"]
    finally:
        config_module.set_current(previous)


def test_the_minutes_cell_is_idle_without_a_status_file(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上没有那份状态文件 → 如实 `idle`（纪要那一格是给人看的读数，读不出就不认为有）。"""
    session = _workspace_with_audio(tmp_path)
    cell = _minutes_cell(tmp_path, monkeypatch, restore_config, session)
    assert cell == {"state": "idle", "started_at": None, "finished_at": None, "error": None}


def test_the_minutes_cell_reads_the_disk_states(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上那几档照它自己那一档答：`running` 的空串收尾戳折成 `null`，`failed` 带原因。"""
    from meeting.minutes import status as minutes_status

    session = _workspace_with_audio(tmp_path)
    minutes_status.write_session_status(
        session,
        state=minutes_status.STATE_RUNNING,
        mode="none",
        branch="headless",
        started_at="2025-09-06T21:00:00",
        finished_at="",
    )
    cell = _minutes_cell(tmp_path, monkeypatch, restore_config, session)
    assert cell["state"] == "running"
    assert cell["started_at"] == "2025-09-06T21:00:00"
    assert cell["finished_at"] is None, "在飞那一版的空串收尾戳折成 null，不摆没有数的空格"
    assert cell["error"] is None

    minutes_status.write_session_status(
        session,
        state=minutes_status.STATE_FAILED,
        mode="live_only",
        branch="headless",
        started_at="2025-09-06T21:00:00",
        finished_at="2025-09-06T21:05:00",
        error="DSH 没应答",
    )
    cell = _minutes_cell(tmp_path, monkeypatch, restore_config, session)
    assert cell == {
        "state": "failed",
        "started_at": "2025-09-06T21:00:00",
        "finished_at": "2025-09-06T21:05:00",
        "error": "DSH 没应答",
    }


def test_the_minutes_cell_survives_a_corrupt_status_file(
    tmp_path, monkeypatch, restore_config
) -> None:
    """状态文件坏了 → 如实 `idle`，不挡这只端点（读不出就是不认为有，第 6 条）。"""
    session = _workspace_with_audio(tmp_path)
    (session / "meeting_minutes_status.json").write_text("{oops", encoding="utf-8")
    cell = _minutes_cell(tmp_path, monkeypatch, restore_config, session)
    assert cell == {"state": "idle", "started_at": None, "finished_at": None, "error": None}
