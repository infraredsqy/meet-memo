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
  - `settings_update` 的 `applied`（`asr.*` 五个点号键名）与 `pending_restart`（三分表里其余
    16 个键，含 `alarm.sound`）分类、**写盘拓扑**（只写已有键 + 本次改动的键）、
    成功体形状（只多 `applied` / `pending_restart`，成功体里没有 `message`）、
    在 `idle` 与 `ended` 的两种终态照样 200、`invalid_setting`(400)（超时 ≤ 段长、表外键名、
    `devices.*`）、原子写、`persist_failed`(500) 与**校验在写盘之前**；
  - `alarm.sound` 的行为判据（应答里 `applied` 为空、进 `pending_restart`、主 log 能看到
    「本场出声开关仍取启动值」、紧接着的快照里仍是启动值）；
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
from meeting.service.runtime import ACTION_CODES, ACTION_NAMES

#: 用例里的显式超时（不留裸秒数）
ACTION_POLL_TIMEOUT_SECONDS = 15.0
#: 轮询间隔
ACTION_POLL_INTERVAL_SECONDS = 0.05

#: 三分表：可热读的五键
APPLIED_KEYS = {
    "asr.max_segment_seconds",
    "asr.timeout_seconds",
    "asr.max_new_tokens",
    "asr.repeat_max_unit",
    "asr.repeat_min_coverage",
}
#: 三分表：其余 18 键（含 `alarm.sound` 与两个设备键位）
PENDING_KEYS = {
    "web.port", "web.dist_dir", "output.root", "log.level", "log.dir",
    "log.max_bytes", "log.backup_count", "alert.silence_dbfs", "alert.silence_seconds",
    "alert.recover_seconds", "alert.alarm_suppress_seconds", "alarm.sound",
    "heartbeat.interval_seconds", "capture.backend", "transcriber.backend", "post.hook",
    "devices.mic", "devices.loopback",
}


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """起一个进程内已启动的运行时；`yield (client, runtime, home, out_root)`。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"output.root": str(out_root), "capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        yield client, client.app.state.runtime, home, out_root


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None:
        body["args"] = args
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
    """动作名清单与 `code` 表逐字冻结（`reprobe` 是 `dca4539b` 加的那一名，其余不动）。"""
    assert ACTION_NAMES == (
        "start", "stop", "mute_session", "settings_update", "reprobe", "dev_inject",
    )
    assert set(ACTION_CODES) == {
        "already_recording", "draining", "capture_unavailable", "not_recording",
        "invalid_setting", "persist_failed", "action_unavailable",
    }


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
        assert [item for item in out_root.iterdir() if item.is_dir()] == []


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
        assert len([item for item in Path(detail["dir"]).parent.iterdir() if item.is_dir()]) == 1


def test_start_unavailable_on_the_default_null_backend_leaves_nothing_on_disk(tmp_path):
    """显式配 `null` 替身：`capture_unavailable`(503) 带人话，**盘上不留任何场次目录**。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, _home, out_root):
        response = post(client, "start")
        assert response.status_code == 503
        payload = response.json()
        assert payload["code"] == "capture_unavailable"
        assert payload["message"]
        assert snapshot_of(client)["top_state"] == "idle"
        assert snapshot_of(client)["session"] is None
        assert list(out_root.iterdir()) == []


def test_start_is_rejected_while_draining(tmp_path):
    """收尾未落定（失败停留档）→ `start` 回 `draining`(409)，`detail` 三件套齐。"""
    with live(tmp_path) as (client, runtime, _home, _out):
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)

        class BoomHook:
            def launch(self, target: str) -> None:
                raise RuntimeError("假会后钩子：按测试要求抛错")

        runtime.post_hook = BoomHook()
        runtime.post_resolution.implementation = runtime.post_hook
        runtime.post_resolution.has_implementation = True

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
        assert [item["id"] for item in detail["failed_steps"]] == ["post"]
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
        assert snapshot_of(client)["alerts"] == {"critical": None, "hints": []}


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
        assert track.event_started_ts is not None
        track.event_started_ts -= runtime.judge.thresholds.alarm_suppress_seconds * 2
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
        assert track.event_started_ts is not None
        track.event_started_ts -= runtime.judge.thresholds.alarm_suppress_seconds * 2
        runtime.commit()
        assert snapshot_of(client)["alerts"]["critical"]["muted"] is False


# ---------------------------------------------------------------------------
# settings_update
# ---------------------------------------------------------------------------


def test_settings_update_splits_applied_and_pending_restart(tmp_path):
    """`applied` = 热读五键；`pending_restart` = 三分表里其余 16 键（含 `alarm.sound`）。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        response = post(
            client,
            "settings_update",
            {
                "asr.timeout_seconds": 30,
                "asr.max_segment_seconds": 10,
                "alarm.sound": False,
                "alert.silence_seconds": 200,
                "output.root": str(tmp_path / "another-out"),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"ok", "revision", "applied", "pending_restart"}
        assert "message" not in body, "成功体里没有 message 字段"
        assert sorted(body["applied"]) == [
            "asr.max_segment_seconds", "asr.timeout_seconds",
        ]
        assert sorted(body["pending_restart"]) == [
            "alarm.sound", "alert.silence_seconds", "output.root",
        ]


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


def test_settings_update_is_available_in_every_top_state(tmp_path):
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

        # ended · phase == "draining"（失败停留档）
        assert post(client, "start").status_code == 200

        class BoomHook:
            def launch(self, target: str) -> None:
                raise RuntimeError("假会后钩子：按测试要求抛错")

        runtime.post_hook = BoomHook()
        runtime.post_resolution.implementation = runtime.post_hook
        runtime.post_resolution.has_implementation = True
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


def test_settings_update_logs_the_alarm_sound_pending_restart_line(tmp_path):
    """`alarm.sound` 的行为判据：进 `pending_restart`，且主 log 有「本场出声开关仍取启动值」。

    「写盘成功」与「快照立刻反映」是两件不同的事：`alarm.sound` 归 `pending_restart`，
    本次只落盘，紧接着的 `GET /api/state` 里 `settings.alarm_sound` 仍是**启动值** ——
    这条口径在此显式钉住（评审点名的可观察判据）。
    """
    with live(tmp_path) as (client, _runtime, home, _out):
        before = snapshot_of(client)["settings"]["alarm_sound"]
        response = post(client, "settings_update", {"alarm.sound": not before})
        assert response.status_code == 200
        body = response.json()
        assert body["applied"] == [], "`alarm.sound` 一处都不进 applied"
        assert body["pending_restart"] == ["alarm.sound"]
        # 写盘确实成功了（配置点号扁平键名落进 config.json）
        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["alarm.sound"] == (not before)
        # 运行期出声开关仍取启动时的生效值：紧接着问一次快照，读到的还是旧值
        assert snapshot_of(client)["settings"]["alarm_sound"] is before

        log_text = helpers.read_text(helpers.service_log_path_of(home))
        assert "本场出声开关仍取启动值" in log_text


def test_settings_snapshot_shape_stays_frozen(tmp_path):
    """快照 `settings`：四个子键齐、`asr` 五子键齐且取生效值、`devices` 两个子键。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        settings = snapshot_of(client)["settings"]
        assert set(settings) == {
            "alarm_sound", "heartbeat_interval_seconds", "asr", "devices",
        }
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


# ---------------------------------------------------------------------------
# dev_inject
# ---------------------------------------------------------------------------


def test_dev_inject_requires_an_active_session(tmp_path):
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

        # ③ ended · phase == "draining"（失败停留档）
        assert post(client, "start").status_code == 200

        class BoomHook:
            def launch(self, target: str) -> None:
                raise RuntimeError("假会后钩子：按测试要求抛错")

        runtime.post_hook = BoomHook()
        runtime.post_resolution.implementation = runtime.post_hook
        runtime.post_resolution.has_implementation = True
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
