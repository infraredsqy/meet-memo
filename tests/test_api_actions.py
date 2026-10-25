"""`tests/test_api_actions.py` —— `POST /api/actions` 的信封、动作与 `code` 表（phase2 §5.1 第四行）。

覆盖（逐条对 §5.1 与 §3.6.2 的可用性表）：

  - `start` 成功（建目录 + 写 `session.json` + 进 `recording`）；
    `mode` 缺省 `both`、三个合法取值都能进 `recording`、第四个取值 `invalid_setting`(400)
    且**盘上不留目录**；失败顺序固定：先判态 → 再校验 `args` → 再判后端 → 再建目录；
  - `already_recording`(409，带 `session_id` / `dir` / `elapsed_seconds`)、
    `draining`(409，`detail` 带场次目录 + 失败步 + 场次 log 路径)；
  - `not_recording`(409) 且**重复调用不产生第二次迁移**；
  - `capture_unavailable`(503，显式配 `null` 替身时 `start` 的真实结果，且盘上不留目录)；
  - `mute_session` 立刻改 `muted`、**不动** `alarm.sound`、**下一场 `start` 复位**；
  - `settings_update` 的 `applied`（`asr.*` 五个点号键名 + `alarm.sound` + 设置抽屉那四项）
    与 `pending_restart`（三分表里其余 28 个键）分类、**写盘拓扑**（只写已有键 + 本次改动的键）、
    成功体形状（只多 `applied` / `pending_restart`，成功体里没有 `message`）、
    在 `idle` 与 `ended` 的两种终态照样 200、`invalid_setting`(400)（超时 ≤ 段长、表外键名、
    `devices.*`）、原子写、`persist_failed`(500) 与**校验在写盘之前**；
  - `alarm.sound` 的行为判据（应答里进 `applied`、**不进** `pending_restart`、紧接着的快照
    `settings.alarm_sound` 当场就是新值、`revision` 涨一格、主 log 不再说「仍取启动值」，
    而同一个请求里真正要重启的键照样进 `pending_restart` 并有它自己那一行）；
  - `dev_inject` 在没有正在进行的场次时回 `not_recording`(409)（三处各断言一次，且一条事实
    都不注入）、非 dev 后端 `action_unavailable`(404)、非法 `source` / `fault` 时
    `invalid_setting`(400)；
  - 信封两种 body 形状与 `code` 取值集合；缺 `X-Meeting-Service: 1` 或 `Content-Type` → 403。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting import config as config_module
from meeting.service import runtime as runtime_module
from meeting.service.runtime import ACTION_CODES, ACTION_NAMES

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

#: 用例里的显式超时（不留裸秒数）
ACTION_POLL_TIMEOUT_SECONDS = 15.0
#: 轮询间隔
ACTION_POLL_INTERVAL_SECONDS = 0.05

#: 三分表：可热读的十二键（`asr.*` 五个 + `alarm.sound` + 设置抽屉那四项 + 会中切模型两键）
APPLIED_KEYS = {
    "alarm.sound",
    "asr.max_segment_seconds",
    "asr.timeout_seconds",
    "asr.max_new_tokens",
    "asr.repeat_max_unit",
    "asr.repeat_min_coverage",
    "devices.mic",
    "devices.loopback",
    "heartbeat.interval_seconds",
    "summary.default_window",
    "summary.presets_json",
    "summary.active_preset",
}
#: 三分表：其余 30 键（`web.*` / `log.*` / `alert.*`、五个后端选择键、会后离线那四个 `post.*`、
#: 会后纪要那五个 `minutes.*`，以及会中总结端点与预算那六个 `summary.*`）。
#: 三分表总共 40 个键 = `DEFAULT_VALUES` 的键全集（「输出根」那一格已退场）。
PENDING_KEYS = {
    "web.port", "web.dist_dir", "log.level", "log.dir",
    "log.max_bytes", "log.backup_count", "alert.silence_dbfs", "alert.silence_seconds",
    "alert.recover_seconds", "alert.alarm_suppress_seconds",
    "capture.backend", "transcriber.backend", "post.hook",
    "post.engine", "post.model", "post.dtype", "post.chunk_seconds",
    "minutes.hook", "minutes.chunk_lines", "minutes.max_segments", "minutes.timeout_seconds",
    "minutes.dsh_url",
    "summary.base_url", "summary.api_key", "summary.model", "summary.extra_body_json",
    "summary.deadline_seconds", "summary.max_attempts",
}


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """起一个进程内已启动的运行时；`yield (client, runtime, home, out_root)`。

    `out_root` 沿用旧名，语义变了：它只是**测试层的场次放置根**（给 start 的 `args.dir`
    预建工作区目录用），不再是任何配置键的值 —— 产品没有默认落点。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        yield client, client.app.state.runtime, home, out_root


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None or action == "start":
        place_root = getattr(client, "place_root", None)
        assert place_root is not None, (
            "start 要显式给 dir：live() 给的 client 自带放置根（client.place_root）"
        )
        body["args"] = helpers.start_args(action, args, place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def snapshot_of(client) -> dict:
    return client.get("/api/state").json()


def wait_until(predicate, *, timeout: float = ACTION_POLL_TIMEOUT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(ACTION_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def wait_for_done(client):
    return wait_until(
        lambda: (lambda s: s if s["ended"] and s["ended"]["phase"] == "done" else None)(
            snapshot_of(client)
        ),
        what="收尾停在 done",
    )


# ---------------------------------------------------------------------------
# 信封与 `code` 表
# ---------------------------------------------------------------------------


def test_action_names_and_codes_are_the_frozen_ones():
    """动作名清单与 `code` 表逐字冻结（`reprobe` 是 `dca4539b` 加的那一名、`summarize` 与它那两个
    `code` 是 `1987cb20` phase2 加的那一档，其余不动）。`workspace_open` / `workspace_remove`
    两只是工作台收编节点 `e2f6166b` 加的；`post_offline` / `post_minutes` 是会后两个按钮
    （`05a48b6a`）；`pause` / `resume` / `set_source` 是暂停与会中开关某一路那三个动作
    （`10770b41`）；`speaker_rename` 是人名节点（`91b6752e`）加的那一只；
    `post_stop` 是打断会后那个节点（`b1c6a58e`）加的；`transcode_run` / `transcode_pause` /
    `transcode_resume` 是转码独立工序这一棒加的三只。"""
    assert ACTION_NAMES == (
        "start", "stop", "mute_session", "settings_update", "reprobe", "dev_inject",
        "summarize", "workspace_open", "workspace_remove",
        "post_offline", "post_minutes", "post_stop",
        "transcode_run", "transcode_pause", "transcode_resume",
        "pause", "resume", "set_source",
        "speaker_rename",
    )
    assert set(ACTION_CODES) == {
        "already_recording", "draining", "capture_unavailable", "not_recording",
        "invalid_setting", "persist_failed", "action_unavailable",
        "transcript_unavailable", "summary_failed",
    }


def test_action_registry_is_reconciled_with_action_names():
    """分发的登记表与 `ACTION_NAMES` 一字不差地对上（routes 的启动期核对把两边钉死）。

    从前动作名有两份清单：`ACTION_NAMES` 与 routes 里那条 if/elif 链，靠纪律同步 ——
    加名漏加分支时请求穿过分发链落到函数末尾，什么都不做却回 `ok: true`。现在分发只认
    `_ACTION_HANDLERS` 那张表，且本用例导入 `meeting.service.routes` 那一刻核对就已经
    跑过（对不上，导入当场炸）；这里再把结论摆到明面上钉一次。
    """
    from meeting.service import routes as routes_module

    assert set(routes_module._ACTION_HANDLERS) == set(ACTION_NAMES)
    assert len(routes_module._ACTION_HANDLERS) == len(ACTION_NAMES)


def test_action_registry_reconciliation_rejects_drift(monkeypatch):
    """核对器按字面意思执法：任何一边多一个名、少一个名，都当场炸，报哪个名字漂了。"""
    from meeting.service import routes as routes_module

    original = dict(routes_module._ACTION_HANDLERS)

    missing = {name: handler for name, handler in original.items() if name != "reprobe"}
    monkeypatch.setattr(routes_module, "_ACTION_HANDLERS", missing)
    with pytest.raises(RuntimeError, match="reprobe"):
        routes_module._reconcile_action_registry()

    extra = dict(original)
    extra["phantom_action"] = original["start"]
    monkeypatch.setattr(routes_module, "_ACTION_HANDLERS", extra)
    with pytest.raises(RuntimeError, match="phantom_action"):
        routes_module._reconcile_action_registry()


def test_success_and_failure_envelope_shapes(tmp_path):
    """成功体 `{ok, revision}`（`settings_update` 多两个数组）；失败体带 `code` / `message` / `detail`。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        ok = post(client, "start")
        assert ok.status_code == 200
        body = ok.json()
        assert set(body) == {"ok", "revision"}
        assert body["ok"] is True
        assert isinstance(body["revision"], int)

        failure = post(client, "start")  # 录音中再 start 是确定的拒绝路径
        assert failure.status_code == 409
        payload = failure.json()
        assert set(payload) == {"ok", "code", "message", "detail"}
        assert payload["ok"] is False
        assert payload["code"] in ACTION_CODES
        assert payload["message"]
        assert isinstance(payload["detail"], dict)


def test_middleware_still_forbids_bare_action_requests(tmp_path):
    """缺 `X-Meeting-Service: 1` 或 `Content-Type` → 403（phase1 的中间件口径不变）。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        assert client.post("/api/actions", json={"action": "start"}).status_code == 403
        assert client.post(
            "/api/actions", json={"action": "start"}, headers={"X-Meeting-Service": "0"}
        ).status_code == 403
        assert client.post(
            "/api/actions",
            content=b"action=start",
            headers={"X-Meeting-Service": "1",
                     "Content-Type": "application/x-www-form-urlencoded"},
        ).status_code == 403
        # SSE 路由也落在同一中间件覆盖范围内（`/api/*` 一律 no-store）
        assert client.get("/api/heartbeat").headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_creates_the_session_and_enters_recording(tmp_path):
    """`start` 成功：建目录 + 写 `session.json` + 进 `recording`（`mode` 缺省 `both`）。"""
    with live(tmp_path) as (client, runtime, _home, out_root):
        assert post(client, "start").status_code == 200
        snapshot = snapshot_of(client)
        assert snapshot["top_state"] == "recording"
        assert snapshot["session"]["mode"] == "both"
        assert snapshot["session"]["frozen"] is False
        assert {item["health"] for item in snapshot["sources"]} == {"capturing"}

        session_dir = Path(snapshot["session"]["dir"])
        assert session_dir.parent == out_root
        record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "recording"
        assert record["mode"] == "both"
        assert record["session_id"] == session_dir.name
        assert runtime.snapshot.session.id == session_dir.name


@pytest.mark.parametrize("mode", ["both", "mic", "loopback"])
def test_start_accepts_the_three_legal_modes(tmp_path, mode):
    """三个合法取值各起一次都能进 `recording`，盘上 `mode` 如实落。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start", {"mode": mode}).status_code == 200
        assert snapshot_of(client)["top_state"] == "recording"
        record = json.loads(
            (Path(runtime.snapshot.session.dir) / "session.json").read_text(encoding="utf-8")
        )
        assert record["mode"] == mode


def test_start_rejects_an_illegal_mode_before_creating_anything(tmp_path):
    """第四个取值 → `invalid_setting`(400)，**校验在建目录之前**（盘上不留目录）。"""
    with live(tmp_path) as (client, _runtime, _home, out_root):
        response = post(client, "start", {"mode": "stereo"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_setting"
        assert response.json()["detail"]["key"] == "mode"
        assert snapshot_of(client)["top_state"] == "idle"
        # 盘上没有本场写下的任何东西（没有 session.json、没有其余产物）
        assert not any((item / "session.json").exists() for item in out_root.iterdir() if item.is_dir())


def test_start_returns_already_recording_with_the_three_fields(tmp_path):
    """录音中再 `start` → `already_recording`(409)，`detail` 带三个字段（§9-8 / R17）。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start").status_code == 200
        time.sleep(0.3)
        again = post(client, "start")
        assert again.status_code == 409
        assert again.json()["code"] == "already_recording"
        detail = again.json()["detail"]
        assert set(detail) >= {"session_id", "dir", "elapsed_seconds"}
        assert detail["session_id"] == runtime.snapshot.session.id
        assert detail["dir"] == runtime.snapshot.session.dir
        assert detail["elapsed_seconds"] > 0
        # 拒绝路径不产生第二次迁移
        assert snapshot_of(client)["revision"] == runtime.revision


def test_start_unavailable_on_the_explicit_null_backend_leaves_nothing_on_disk(tmp_path):
    """显式配 `null` 替身：`capture_unavailable`(503) 带人话，**盘上没有 `session.json`**。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, _home, out_root):
        response = post(client, "start")
        assert response.status_code == 503
        payload = response.json()
        assert payload["code"] == "capture_unavailable"
        assert payload["message"]
        assert snapshot_of(client)["top_state"] == "idle"
        assert snapshot_of(client)["session"] is None
        assert not any((item / "session.json").exists() for item in out_root.iterdir() if item.is_dir())


def test_start_is_rejected_while_draining(tmp_path, monkeypatch):
    """收尾未落定（失败停留档）→ `start` 回 `draining`(409)，`detail` 三件套齐。

    失败步由 `log` 那一步演（`write_session_end_marker` 换成抛错的替身）：
    第 4 步 `post` 按「结束录音」之后不自动拉起会后（R26），它每次都停在 `waiting`，
    拿它演不出失败档。
    """
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)

        def boom_end_marker(*args, **kwargs):
            raise RuntimeError("假场次日志接缝：按测试要求抛错")

        monkeypatch.setattr(
            runtime_module.logging_setup, "write_session_end_marker", boom_end_marker
        )

        assert post(client, "stop").status_code == 200
        wait_until(
            lambda: (lambda s: s if (
                s["ended"] and any(step["status"] == "failed" for step in s["ended"]["steps"])
            ) else None)(snapshot_of(client)),
            what="收尾失败档落定",
        )

        rejected = post(client, "start")
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "draining"
        detail = rejected.json()["detail"]
        assert detail["session_id"] == session_dir.name
        assert detail["dir"] == str(session_dir)
        assert [item["id"] for item in detail["failed_steps"]] == ["log"]
        assert detail["log_path"] == runtime.log_path
        assert snapshot_of(client)["top_state"] == "ended"
        # 现场没有被覆盖：盘上还是失败那一版
        record = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        assert record["state"] == "failed"


# ---------------------------------------------------------------------------
# stop / mute_session
# ---------------------------------------------------------------------------


def test_stop_and_mute_are_not_recording_outside_a_session(tmp_path):
    """`stop` / `mute_session` / `dev_inject` 在非录音态一律 `not_recording`(409)。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        for action in ("stop", "mute_session", "dev_inject"):
            response = post(client, action, {"source": "mic", "fault": "silence"})
            assert response.status_code == 409, action
            assert response.json()["code"] == "not_recording"
        # 重复调用不产生第二次迁移
        before = runtime.revision
        for _ in range(3):
            post(client, "stop")
        assert runtime.revision == before


def test_stop_freezes_and_ends_the_session(tmp_path):
    """`stop` 进 `ended·draining`、时长冻结；重复 `stop` 不产生第二次迁移。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start").status_code == 200
        time.sleep(0.3)
        assert post(client, "stop").status_code == 200
        snapshot = snapshot_of(client)
        assert snapshot["top_state"] == "ended"
        assert snapshot["session"]["frozen"] is True

        wait_for_done(client)
        # 收尾完成档下 `stop` / `mute_session` 一律 `not_recording`
        for action in ("stop", "mute_session"):
            response = post(client, action)
            assert response.status_code == 409
            assert response.json()["code"] == "not_recording"


def test_mute_session_flips_muted_and_resets_on_the_next_session(tmp_path):
    """`mute_session` 立刻改 `muted`、不动 `alarm.sound`、**下一场 `start` 复位**。"""
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 1}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0, duration_seconds=60.0
        )
        wait_until(lambda: snapshot_of(client)["top_state"] == "alert", what="进 alert")

        before_sound = snapshot_of(client)["settings"]["alarm_sound"]
        muted = post(client, "mute_session")
        assert muted.status_code == 200
        snapshot = snapshot_of(client)
        assert snapshot["alerts"]["critical"]["muted"] is True
        for hint in snapshot["alerts"]["hints"]:
            assert hint["muted"] is True
        # 持久化开关没被动过
        assert snapshot["settings"]["alarm_sound"] == before_sound

        # 收尾 → 下一场 `start` 复位（会话级静音不带过去）
        assert post(client, "stop").status_code == 200
        wait_for_done(client)
        assert runtime.judge.mute_session is True, "复位发生在下一场 start，不在 stop"

        assert post(client, "start").status_code == 200
        assert runtime.judge.mute_session is False
        # 新一场起来之后这条告警本身必须已经撤掉：`mute_session` 复位带走的是会话级静音，
        # 而**这一场**还没有任何一路出事，所以致命级与提示级都该是空的
        # （提示级只在 `recording` / `alert` 两态可能有条目，这一场刚进 `recording`，
        # 上一场那条「转写停了」的登记已经随离开录音态一起清掉了）。
        snapshot = snapshot_of(client)
        assert snapshot["alerts"]["critical"] is None
        assert snapshot["alerts"]["hints"] == []
        assert snapshot["top_state"] == "recording"


def test_critical_muted_recalculates_at_every_commit(tmp_path):
    """`alerts.critical.muted` 在每次提交快照时按当下重算一遍，随窗口与开关来去。

    三处判据各走一遍：① 会话级静音撤销之后，提交出来的快照仍写着 `True`（这一条事件本身
    还在抑制窗口里，两个条件任一成立就不出声）；② 窗口起点被推旧之后，同一条告警重算回
    `False`；③ 会话级静音撤销 + 窗口过去两件事一起松开，结果同样是 `False`。
    """
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 1}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0, duration_seconds=60.0
        )
        wait_until(lambda: snapshot_of(client)["top_state"] == "alert", what="进 alert")
        assert post(client, "mute_session").status_code == 200
        assert snapshot_of(client)["alerts"]["critical"]["muted"] is True

        # ① 只撤销会话级静音：窗口还在，快照里仍是 True（当下的判定）
        runtime.judge.reset_session_mute()
        runtime.commit()
        assert snapshot_of(client)["alerts"]["critical"]["muted"] is True

        # ② 把这一条事件的窗口起点推旧（事件已经过去很久）→ 重算为 False
        track = runtime.judge._tracks["loopback"]  # noqa: SLF001 - 就为了把窗口推旧
        assert track.last_raise_ts is not None
        track.last_raise_ts -= runtime.judge.thresholds.alarm_suppress_seconds * 2
        runtime.commit()
        assert snapshot_of(client)["alerts"]["critical"]["muted"] is False


def test_session_mute_recalculates_instead_of_stamping_true(tmp_path):
    """静音这条线：开关翻上去快照变 `True`，两个条件都松开之后同一条告警回到 `False`。

    覆盖原来那条「`mute_session()` 手写 `True` 且只升不降」的写法：`muted` 由 `commit()`
    在提交快照时按当下重算，一次动作仍然只提交一次（一个 revision）。
    """
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 1}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0, duration_seconds=60.0
        )
        wait_until(lambda: snapshot_of(client)["top_state"] == "alert", what="进 alert")

        before = snapshot_of(client)["revision"]
        assert post(client, "mute_session").status_code == 200
        muted = snapshot_of(client)
        assert muted["revision"] == before + 1, "一次动作只提交一次"
        assert muted["alerts"]["critical"]["muted"] is True, "静音开关翻上去，快照随之变 True"

        # 会话级静音与抑制窗口这两件事一起松开 → 同一条告警回到会出声
        runtime.judge.reset_session_mute()
        track = runtime.judge._tracks["loopback"]  # noqa: SLF001 - 就为了把窗口推旧
        assert track.last_raise_ts is not None
        track.last_raise_ts -= runtime.judge.thresholds.alarm_suppress_seconds * 2
        runtime.commit()
        assert snapshot_of(client)["alerts"]["critical"]["muted"] is False


# ---------------------------------------------------------------------------
# settings_update
# ---------------------------------------------------------------------------


def test_settings_update_splits_applied_and_pending_restart(tmp_path):
    """`applied` = 热读十二键；`pending_restart` = 本次改的其余键。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        response = post(
            client,
            "settings_update",
            {
                "asr.timeout_seconds": 30,
                "asr.max_segment_seconds": 10,
                "alarm.sound": False,
                "alert.silence_seconds": 200,
                "log.level": "DEBUG",
                "devices.mic": "Analogue 1 + 2 (2- Focusrite US",
                "heartbeat.interval_seconds": 20,
                "summary.default_window": "30m",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"ok", "revision", "applied", "pending_restart"}
        assert "message" not in body, "成功体里没有 message 字段"
        assert sorted(body["applied"]) == [
            "alarm.sound", "asr.max_segment_seconds", "asr.timeout_seconds",
            "devices.mic", "heartbeat.interval_seconds", "summary.default_window",
        ]
        assert sorted(body["pending_restart"]) == [
            "alert.silence_seconds", "log.level",
        ]


def test_settings_key_groups_cover_the_whole_table(tmp_path):
    """两个数组的键名与配置层的三分表逐键一致（40 个键一个不多、一个不少）。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        response = post(
            client,
            "settings_update",
            {key: config_module.default_for(key) for key in sorted(APPLIED_KEYS | PENDING_KEYS)},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["applied"]) == APPLIED_KEYS
        assert set(body["pending_restart"]) == PENDING_KEYS
        # 两个数组不重叠，合起来正好是整张三分表
        assert not (APPLIED_KEYS & PENDING_KEYS)
        assert APPLIED_KEYS | PENDING_KEYS == set(config_module.SETTING_KEY_GROUPS)
        assert set(config_module.APPLIED_SETTING_KEYS) == APPLIED_KEYS
        assert set(config_module.PENDING_RESTART_SETTING_KEYS) == PENDING_KEYS
        # 两个数组按定义就是这一档的补集关系（不是两张各自维护的名单）
        assert set(config_module.APPLIED_SETTING_KEYS) == set(config_module.HOT_KEYS)
        assert set(config_module.PENDING_RESTART_SETTING_KEYS) == (
            set(config_module.DEFAULT_VALUES) - set(config_module.HOT_KEYS)
        )


def test_settings_update_writes_only_existing_plus_changed_keys(tmp_path):
    """写盘拓扑：只写「文件里已有的键 + 本次改动的键」，**不把默认值落盘**。"""
    with live(tmp_path) as (client, _runtime, home, _out):
        config_path = helpers.config_path_of(home)
        helpers.write_config_json(home, {"alert.silence_seconds": 3})
        before = json.loads(config_path.read_text(encoding="utf-8"))
        assert set(before) == {"alert.silence_seconds"}

        assert post(
            client, "settings_update", {"asr.timeout_seconds": 25}
        ).status_code == 200
        after = json.loads(config_path.read_text(encoding="utf-8"))
        assert set(after) == {"alert.silence_seconds", "asr.timeout_seconds"}
        assert after["asr.timeout_seconds"] == 25
        # 默认值一个都没被写进去
        for key in ("web.port", "log.level", "capture.backend", "alarm.sound"):
            assert key not in after

        # 写回后键名集合 = 读回时的键名集合 ∪ 本次改动的键名
        assert post(client, "settings_update", {"log.level": "DEBUG"}).status_code == 200
        final = json.loads(config_path.read_text(encoding="utf-8"))
        assert set(final) == {"alert.silence_seconds", "asr.timeout_seconds", "log.level"}
        # 没有 `.tmp` 残骸
        assert [item.name for item in config_path.parent.iterdir() if item.name.endswith(".tmp")] == []


def test_settings_update_is_available_in_every_top_state(tmp_path, monkeypatch):
    """`settings_update` 在任何 `top_state` 下都可用（含 `idle` 与 `ended` 的两种终态）。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        # idle
        assert post(client, "settings_update", {"alert.silence_seconds": 120}).status_code == 200

        # recording
        assert post(client, "start").status_code == 200
        assert post(client, "settings_update", {"alert.silence_seconds": 121}).status_code == 200

        # ended · phase == "done"
        assert post(client, "stop").status_code == 200
        wait_for_done(client)
        assert snapshot_of(client)["ended"]["phase"] == "done"
        assert post(client, "settings_update", {"alert.silence_seconds": 122}).status_code == 200

        # ended · phase == "draining"（失败停留档；失败步由 `log` 那一步演 —— 第 4 步 `post`
        # 按 R26 停在 `waiting`，拿它演不出失败档）
        assert post(client, "start").status_code == 200

        def boom_end_marker(*args, **kwargs):
            raise RuntimeError("假场次日志接缝：按测试要求抛错")

        monkeypatch.setattr(
            runtime_module.logging_setup, "write_session_end_marker", boom_end_marker
        )
        assert post(client, "stop").status_code == 200
        wait_until(
            lambda: (lambda s: s if (
                s["ended"] and s["ended"]["phase"] == "draining"
                and any(step["status"] == "failed" for step in s["ended"]["steps"])
            ) else None)(snapshot_of(client)),
            what="收尾失败停留档",
        )
        assert post(client, "settings_update", {"alert.silence_seconds": 123}).status_code == 200


def test_settings_update_rejects_illegal_calls_before_writing(tmp_path):
    """`invalid_setting`(400)：超时 ≤ 段长、表外键名、`devices.*` 里没定名的名字；**校验在写盘之前**。"""
    with live(tmp_path) as (client, _runtime, home, _out):
        config_path = helpers.config_path_of(home)
        baseline = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

        cases = [
            ({"asr.timeout_seconds": 5, "asr.max_segment_seconds": 10}, "超时必须大于段长"),
            ({"not.a.key": 1}, "表外的键名"),
            # 定名的只有 `devices.mic` / `devices.loopback` 两个字面键名：前缀不是白名单
            ({"devices.mic_device": "某设备"}, "名字没定名的设备键位落在表外"),
            ({"devices": {"mic": "某设备"}}, "嵌套形状不合法"),
            ({"asr": {"timeout_seconds": 30}}, "嵌套形状不合法"),
        ]
        for args, why in cases:
            response = post(client, "settings_update", args)
            assert response.status_code == 400, f"{why}：{response.text}"
            assert response.json()["code"] == "invalid_setting"

        # 一个非法的都没写进去
        after = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        assert after == baseline


def test_settings_update_reports_persist_failed(tmp_path):
    """写盘失败 → `persist_failed`(500)：造法 = 用**同名目录**顶替配置文件。"""
    with live(tmp_path) as (client, _runtime, home, _out):
        config_path = helpers.config_path_of(home)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            config_path.unlink()
        config_path.mkdir()  # 名字就叫 config.json 的目录 → 写盘必然失败

        response = post(client, "settings_update", {"asr.timeout_seconds": 30})
        assert response.status_code == 500
        assert response.json()["code"] == "persist_failed"
        assert response.json()["message"]


def test_settings_update_applies_the_alarm_sound_switch_at_once(tmp_path):
    """`alarm.sound` 收在热读档：写盘那一刻快照就换过去，`revision` 跟着涨一格。

    「写盘成功」与「快照立刻反映」在这里合成了同一件事 —— 出声开关要在勾选那一刻就改运行期
    行为（`docs/contract.md`）。四条判据逐条咬住：应答分类、快照值、`revision`、
    写盘确实落了盘。
    """
    with live(tmp_path) as (client, _runtime, home, _out):
        before = snapshot_of(client)["settings"]["alarm_sound"]
        before_revision = int(snapshot_of(client)["revision"])

        response = post(client, "settings_update", {"alarm.sound": not before})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == ["alarm.sound"], "`alarm.sound` 归热读那一档"
        assert body["pending_restart"] == [], "它一处都不该进 pending_restart"

        # 写盘确实成功了（配置点号扁平键名落进 config.json）
        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["alarm.sound"] == (not before)

        # 紧接着问一次快照：读到的就是新值（运行期生效值换过去了）
        after = snapshot_of(client)
        assert after["settings"]["alarm_sound"] is (not before), "改了当场生效"
        assert int(after["revision"]) == before_revision + 1, "热读键生效提交一次"

        # 改回来同样当场可见
        assert post(client, "settings_update", {"alarm.sound": before}).status_code == 200
        assert snapshot_of(client)["settings"]["alarm_sound"] is before


def test_settings_update_still_flags_the_keys_that_really_need_a_restart(tmp_path):
    """同一个请求里真正要重启的键照样进 `pending_restart`、照样有它自己那一行 log。

    出声开关那一档改口径之后，那条「仍取启动值」的 log 只该对**真正 pending 的键**说；
    一句都不说会让「哪些键改了要重启」失去可观察判据，说错对象则是一句与事实相反的话。
    """
    with live(tmp_path) as (client, _runtime, home, _out):
        before_sound = snapshot_of(client)["settings"]["alarm_sound"]
        response = post(
            client,
            "settings_update",
            {"alarm.sound": not before_sound, "alert.silence_seconds": 200},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == ["alarm.sound"]
        assert body["pending_restart"] == ["alert.silence_seconds"]
        # 热读那一格当场变，pending 那一格不在快照里露头（它管的是运行期阈值）
        assert snapshot_of(client)["settings"]["alarm_sound"] is (not before_sound)

        log_text = helpers.read_text(helpers.service_log_path_of(home))
        assert "alert.silence_seconds" in log_text
        assert "归 pending_restart" in log_text
        # 那句话不再扣到出声开关头上
        assert "alarm.sound 归 pending_restart" not in log_text
        assert "本场出声开关仍取启动值" not in log_text


def test_settings_update_with_only_pending_keys_writes_without_committing(tmp_path):
    """全是 `pending_restart` 的改动只写盘、不提交：`revision` 原地不动。

    这一条守住热读那一档的**边界** —— 把「有改动就提交」写进去的话，改一个设备名也会推着
    `revision` 走，界面上「`id` 变了就是状态变了」就不再成立。
    """
    with live(tmp_path) as (client, _runtime, home, _out):
        before_revision = int(snapshot_of(client)["revision"])
        response = post(client, "settings_update", {"alert.silence_seconds": 200})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == []
        assert body["pending_restart"] == ["alert.silence_seconds"]
        assert int(snapshot_of(client)["revision"]) == before_revision, "pending 改动不提交"

        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["alert.silence_seconds"] == 200, "写盘照旧成功"


def test_settings_snapshot_shape_stays_frozen(tmp_path):
    """快照 `settings`：七个子键齐、`asr` 五子键齐且取生效值、`devices` 两个子键。

    会中切模型的两格（本节点）：`summary_preset_names` 只给名字（预设内容与密钥
    不进快照），`summary_active_preset` 是当前用哪一套的**生效值**（没配 = 空串）。
    """
    with live(tmp_path) as (client, _runtime, _home, _out):
        settings = snapshot_of(client)["settings"]
        assert set(settings) == {
            "alarm_sound", "heartbeat_interval_seconds", "summary_default_window",
            "summary_preset_names", "summary_active_preset",
            "asr", "devices",
        }
        assert settings["summary_default_window"] == config_module.default_for(
            "summary.default_window"
        )
        assert settings["summary_preset_names"] == []
        assert settings["summary_active_preset"] == ""
        assert set(settings["asr"]) == {
            "max_segment_seconds", "timeout_seconds", "max_new_tokens",
            "repeat_max_unit", "repeat_min_coverage",
        }
        assert settings["asr"]["timeout_seconds"] == config_module.default_for("asr.timeout_seconds")
        # 两个设备键位没配时都是 `null`（`dca4539b` 定名；空值形状两端一致）
        assert settings["devices"] == {
            "mic": config_module.default_for("devices.mic"),
            "loopback": config_module.default_for("devices.loopback"),
        }
        assert settings["devices"] == {"mic": None, "loopback": None}


def test_settings_update_swaps_the_summary_preset_at_once(tmp_path):
    """会中切模型：预设清单与当前指针都收热读档，写盘那一刻快照与运行期一起换。

    四条判据（照 `alarm.sound` 那条的口径）：应答分类（进 `applied`）、快照两格
    当场是新值（名字清单 + 生效指针）、`revision` 涨一格、写盘确实落了盘。
    指向清单外名字的指针在写前校验就被拦下（`invalid_setting`）。
    """
    presets = json.dumps(
        [
            {"name": "主力", "base_url": "http://127.0.0.1:9/v1", "api_key": "k1", "model": "m1"},
            {"name": "备用（必败）", "base_url": "http://127.0.0.1:1/v1", "api_key": "k2", "model": "m2"},
        ],
        ensure_ascii=False,
    )
    with live(tmp_path) as (client, _runtime, home, _out):
        before_revision = int(snapshot_of(client)["revision"])

        # ① 一次写清单 + 指名：两组都进 applied，快照当场可见
        response = post(
            client,
            "settings_update",
            {"summary.presets_json": presets, "summary.active_preset": "主力"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert sorted(body["applied"]) == ["summary.active_preset", "summary.presets_json"]
        assert body["pending_restart"] == []
        after = snapshot_of(client)
        assert after["settings"]["summary_preset_names"] == ["主力", "备用（必败）"]
        assert after["settings"]["summary_active_preset"] == "主力"
        assert int(after["revision"]) == before_revision + 1, "热读键生效提交一次"
        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["summary.presets_json"] == presets
        assert written["summary.active_preset"] == "主力"

        # ② 会中照样切（这正是这条功能的用场）：切到另一套，当场生效
        assert post(client, "start").status_code == 200
        switch = post(client, "settings_update", {"summary.active_preset": "备用（必败）"})
        assert switch.status_code == 200, switch.text
        assert switch.json()["applied"] == ["summary.active_preset"]
        recording = snapshot_of(client)
        assert recording["settings"]["summary_active_preset"] == "备用（必败）"

        # ③ 切回「不选预设」（空串 = 老四键平铺那一档）
        assert post(client, "settings_update", {"summary.active_preset": ""}).status_code == 200
        assert snapshot_of(client)["settings"]["summary_active_preset"] == ""

        # ④ 指向清单外名字：写前校验拦下（不发 invalid 的盘）
        refused = post(client, "settings_update", {"summary.active_preset": "没有的那套"})
        assert refused.status_code == 400
        assert refused.json()["code"] == "invalid_setting"
        assert snapshot_of(client)["settings"]["summary_active_preset"] == ""


# ---------------------------------------------------------------------------
# dev_inject
# ---------------------------------------------------------------------------


def test_dev_inject_requires_an_active_session(tmp_path, monkeypatch):
    """没有正在进行的场次时 `dev_inject` 回 `not_recording`(409)，且**一条事实都不注入**。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        # ① idle
        idle = post(client, "dev_inject", {"source": "mic", "fault": "silence"})
        assert idle.status_code == 409
        assert idle.json()["code"] == "not_recording"
        assert runtime.capture_backend.active_faults() == {}

        # ② ended · phase == "done"
        assert post(client, "start").status_code == 200
        assert post(client, "stop").status_code == 200
        wait_for_done(client)
        done = post(client, "dev_inject", {"source": "mic", "fault": "silence"})
        assert done.status_code == 409
        assert done.json()["code"] == "not_recording"
        assert runtime.capture_backend.active_faults() == {}

        # ③ ended · phase == "draining"（失败停留档；失败步由 `log` 那一步演 —— 第 4 步 `post`
        # 按 R26 停在 `waiting`，拿它演不出失败档）
        assert post(client, "start").status_code == 200

        def boom_end_marker(*args, **kwargs):
            raise RuntimeError("假场次日志接缝：按测试要求抛错")

        monkeypatch.setattr(
            runtime_module.logging_setup, "write_session_end_marker", boom_end_marker
        )
        assert post(client, "stop").status_code == 200
        wait_until(
            lambda: (lambda s: s if (
                s["ended"] and any(step["status"] == "failed" for step in s["ended"]["steps"])
            ) else None)(snapshot_of(client)),
            what="收尾失败停留档",
        )
        draining = post(client, "dev_inject", {"source": "mic", "fault": "silence"})
        assert draining.status_code == 409
        assert draining.json()["code"] == "not_recording"
        assert runtime.capture_backend.active_faults() == {}


def test_dev_inject_is_unavailable_on_a_non_dev_backend(tmp_path):
    """非 dev 采集后端 → `action_unavailable`(404)。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, _home, _out):
        response = post(client, "dev_inject", {"source": "mic", "fault": "silence"})
        assert response.status_code == 404
        assert response.json()["code"] == "action_unavailable"


def test_dev_inject_rejects_illegal_source_and_fault(tmp_path):
    """非法 `source` / `fault` → `invalid_setting`(400)。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start").status_code == 200
        bad_source = post(client, "dev_inject", {"source": "keyboard", "fault": "silence"})
        assert bad_source.status_code == 400
        assert bad_source.json()["code"] == "invalid_setting"

        bad_fault = post(client, "dev_inject", {"source": "mic", "fault": "explode"})
        assert bad_fault.status_code == 400
        assert bad_fault.json()["code"] == "invalid_setting"
        assert runtime.capture_backend.active_faults() == {}

        # 正数校验：`duration_seconds` 必须为正
        bad_duration = post(
            client, "dev_inject",
            {"source": "mic", "fault": "silence", "duration_seconds": 0},
        )
        assert bad_duration.status_code == 400
        assert bad_duration.json()["code"] == "invalid_setting"


def test_dev_inject_works_on_the_dev_backend(tmp_path):
    """dev 后端下 `dev_inject` 可用（到点生效、到点恢复的完整语义在 test_dev_backend 里断言）。"""
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 1}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        ok = post(
            client, "dev_inject",
            {"source": "loopback", "fault": "silence", "after_seconds": 0.0,
             "duration_seconds": 60.0},
        )
        assert ok.status_code == 200, ok.text
        assert runtime.capture_backend.active_faults().get("loopback") == "silence"
        assert snapshot_of(client)["top_state"] in ("recording", "alert")


def test_pause_action_flips_session_paused_and_back(tmp_path):
    """`pause` / `resume` 动作入口的接线（`68927c27` 收编自 test_pause_actions 的快测部分）。

    引擎层的暂停语义（暂停窗逐样本静音、段不切、权威时长不含暂停）由
    `tests/test_capture_engine.py` 的引擎直测覆盖；这里只钉动作入口这一格：
    按下之后快照的 `session.paused` 翻面、顶层停在 `recording`（暂停是第五态之外的一格）。
    """
    with live(tmp_path) as (client, _runtime, _home, _out):
        assert post(client, "start").status_code == 200
        wait_until(lambda: snapshot_of(client)["top_state"] == "recording", what="进入录音")
        assert post(client, "pause").status_code == 200
        paused = snapshot_of(client)
        assert paused["session"]["paused"] is True
        assert paused["top_state"] == "recording"
        assert post(client, "resume").status_code == 200
        assert snapshot_of(client)["session"]["paused"] is False
