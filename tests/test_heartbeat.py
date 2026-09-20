"""`tests/test_heartbeat.py` —— `GET /api/heartbeat` 与帧时钟（phase2 §5.1 第八行；§3.5.4）。

覆盖（逐条对 §5.1 的 ①–⑦）：

  ① `idle` 下 200、`service == "meeting"`、`top_state == "idle"`、`recording_alive is None`、
     `seconds_since_last_tick is None`、`server_time` 合法 ISO、`revision` 与 `GET /api/state` 一致；
  ② **HTTP 通着但录音停了**（§9-6）：dev 后端 `start` → `recording_alive is True` →
     `dev_inject` 注入 `stall` → 超窗口后 `/api/heartbeat` **仍 200**、`recording_alive is False`、
     `seconds_since_last_tick > 2.0`；
  ③ 窗口口径 `max(2.0s, 3 × frame_interval)`：**对窗口计算那组常量与纯函数直接断言**
     （协议默认 `0.5s` 与 dev 的 `0.1s` 两档都算成 2.0 秒），不假装跑真后端；
     改 `heartbeat.interval_seconds` **不影响**判定；
  ④ 判定权在 runtime：`stall` 下后端并没有报「流断了」——`recording_alive=false` 完全由帧时钟算出；
  ⑤ `alert` 态照常给布尔值（`silence` + 测试里显式调小 `alert.silence_seconds`）；
  ⑥ **`SourceFacts` / `SourceLost` 不刷新帧时钟**；
  ⑦ **帧间隔的声明与回退**：`frame_interval` 属性就是声明位，拿不到或取值非正数时按
     `DEFAULT_FRAME_INTERVAL_SECONDS` 算窗口、**服务照常起、`top_state` 不动**。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

import _helpers as helpers
from meeting.backends.base import SourceFacts, SourceLost
from meeting.state import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    HEARTBEAT_MIN_WINDOW_SECONDS,
    HEARTBEAT_WINDOW_FACTOR,
    frame_clock_window,
)

#: 用例的显式等待上限（秒）
HEARTBEAT_TIMEOUT_SECONDS = 15.0
#: 轮询间隔
HEARTBEAT_INTERVAL_SECONDS = 0.05
#: `stall` 的停帧窗口：比帧时钟窗口长，才观测得到「HTTP 通着但录音停了」
HEARTBEAT_STALL_SECONDS = 5.0

HEARTBEAT_FIELDS = {
    "service", "server_time", "revision", "top_state",
    "recording_alive", "seconds_since_last_tick",
}


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """进程内已启动的运行时 + ASGI 客户端。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"output.root": str(out_root), "capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        yield client, client.app.state.runtime, home


def wait_until(predicate, *, timeout: float = HEARTBEAT_TIMEOUT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None:
        body["args"] = args
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


# ---------------------------------------------------------------------------
# ① 空闲态
# ---------------------------------------------------------------------------


def test_idle_heartbeat_fields(tmp_path):
    """① 空闲态：六字段齐、`service` 字面量在、两字段为 `null`、`revision` 与快照一致。"""
    with live(tmp_path) as (client, _runtime, _home):
        response = client.get("/api/heartbeat")
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == HEARTBEAT_FIELDS
        assert payload["service"] == "meeting"
        assert payload["top_state"] == "idle"
        assert payload["recording_alive"] is None
        assert payload["seconds_since_last_tick"] is None
        # `server_time` 是合法 ISO（按服务侧时间戳算）
        parsed = dt.datetime.fromisoformat(payload["server_time"])
        assert parsed.tzinfo is not None
        # 与 `GET /api/state` 的 revision 一致
        assert payload["revision"] == client.get("/api/state").json()["revision"]
        assert response.headers["cache-control"] == "no-store"


def test_heartbeat_stays_200_in_every_state(tmp_path):
    """心跳必须在**任何状态**下都 200 且带 `service` 字面量（端口策略与 `/debug` 都靠它）。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, _home):
        assert client.get("/api/heartbeat").json()["top_state"] == "idle"

    with live(tmp_path / "dev", {"capture.backend": "dev"}) as (client, _runtime, _home):
        assert post(client, "start").status_code == 200
        assert client.get("/api/heartbeat").status_code == 200
        assert post(client, "stop").status_code == 200
        wait_until(
            lambda: (lambda s: s if s["ended"] and s["ended"]["phase"] == "done" else None)(
                client.get("/api/state").json()
            ),
            what="收尾停在 done",
        )
        payload = client.get("/api/heartbeat").json()
        assert payload["service"] == "meeting"
        assert payload["top_state"] == "ended"
        assert payload["recording_alive"] is None, "ended 态不再有采集事实帧在跑"


# ---------------------------------------------------------------------------
# ② 与 ④ HTTP 通着但录音停了
# ---------------------------------------------------------------------------


def test_heartbeat_reports_a_stalled_recorder(tmp_path):
    """② `stall` → 心跳仍 200、`recording_alive is False`、`seconds_since_last_tick > 2.0`。"""
    with live(tmp_path) as (client, runtime, _home):
        assert post(client, "start").status_code == 200
        wait_until(
            lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
            what="recording_alive == True",
        )
        assert runtime.capture_backend.active_faults() == {}, "④ 这一档后端并没有报「流断了」"

        assert post(
            client, "dev_inject",
            {"source": "mic", "fault": "stall", "after_seconds": 0.0,
             "duration_seconds": HEARTBEAT_STALL_SECONDS},
        ).status_code == 200

        stalled = wait_until(
            lambda: (lambda h: h if h["recording_alive"] is False else None)(
                client.get("/api/heartbeat").json()
            ),
            timeout=HEARTBEAT_STALL_SECONDS,
            what="HTTP 通着但录音停了",
        )
        assert stalled["service"] == "meeting"
        assert stalled["seconds_since_last_tick"] > 2.0
        # ④ 判定权在 runtime：后端没有报「流断了」，这个结论完全由帧时钟算出来
        assert runtime.capture_backend.active_faults().get("mic") in ("stall", None)
        assert client.get("/api/state").json()["top_state"] == "recording"


# ---------------------------------------------------------------------------
# ③ 窗口口径
# ---------------------------------------------------------------------------


def test_frame_clock_window_constants_and_function():
    """③ `max(2.0s, 3 × frame_interval)`：两档都算成 2.0 秒（直接断言纯函数与常量）。"""
    assert HEARTBEAT_MIN_WINDOW_SECONDS == 2.0
    assert HEARTBEAT_WINDOW_FACTOR == 3.0
    assert DEFAULT_FRAME_INTERVAL_SECONDS == 0.5
    # 协议默认 0.5s 与 dev 替身 0.1s 两档
    assert frame_clock_window(0.5) == 2.0
    assert frame_clock_window(0.1) == 2.0
    # 更大的帧间隔才把窗口抬起来
    assert frame_clock_window(1.0) == 3.0
    assert frame_clock_window(2.0) == 6.0


def test_heartbeat_interval_setting_does_not_participate(tmp_path):
    """③ 界面的 10 秒心跳周期**不参与**存活窗口的计算（两个心跳各管一头）。"""
    with live(tmp_path, {"heartbeat.interval_seconds": 60}) as (client, runtime, _home):
        assert client.get("/api/state").json()["settings"]["heartbeat_interval_seconds"] == 60
        assert runtime.frame_interval == 0.1, "窗口只看采集后端声明的帧间隔"
        assert frame_clock_window(runtime.frame_interval) == 2.0
        assert frame_clock_window(60) == 180.0, "拿界面心跳周期算会得到完全不同的窗口"
        # 空闲态照样两字段为 null（不受这个设置影响）
        assert client.get("/api/heartbeat").json()["recording_alive"] is None


# ---------------------------------------------------------------------------
# ⑤ alert 态照常给布尔值
# ---------------------------------------------------------------------------


def test_heartbeat_gives_a_boolean_while_in_alert(tmp_path):
    """⑤ `alert` 态照常给布尔值（`silence` + 用例里显式调小阈值）。"""
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 30}) as (
        client, runtime, _home
    ):
        assert post(client, "start").status_code == 200
        wait_until(
            lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
            what="recording_alive == True",
        )
        assert post(
            client, "dev_inject",
            {"source": "loopback", "fault": "silence", "after_seconds": 0.0,
             "duration_seconds": 60.0},
        ).status_code == 200
        alerted = wait_until(
            lambda: (lambda h: h if h["top_state"] == "alert" else None)(
                client.get("/api/heartbeat").json()
            ),
            what="top_state == alert",
        )
        assert alerted["recording_alive"] is True
        assert isinstance(alerted["recording_alive"], bool)
        assert alerted["seconds_since_last_tick"] < 2.0


# ---------------------------------------------------------------------------
# ⑥ SourceFacts / SourceLost 不刷新帧时钟
# ---------------------------------------------------------------------------


def test_source_facts_and_lost_do_not_refresh_the_frame_clock(tmp_path):
    """⑥ 只认 `LevelFrame` 刷新帧时钟：`SourceFacts` 与 `SourceLost` 一概不算。"""
    with live(tmp_path, {"alert.silence_seconds": 600}) as (client, runtime, _home):
        assert post(client, "start").status_code == 200
        wait_until(lambda: runtime.last_frame_ts is not None, what="收到第一帧电平")
        stamp = runtime.last_frame_ts

        runtime.apply_fact(SourceFacts(source="mic", stream_alive=True))
        assert runtime.last_frame_ts == stamp, "SourceFacts 不刷新帧时钟"

        runtime.apply_fact(SourceLost(source="loopback", reason="按测试要求"))
        assert runtime.last_frame_ts == stamp, "SourceLost 不刷新帧时钟"

        # 只有电平帧才刷新
        runtime.apply_fact(_level_frame("mic"))
        assert runtime.last_frame_ts is not None and runtime.last_frame_ts >= stamp


def _level_frame(source: str):
    from meeting.backends.base import LevelFrame

    return LevelFrame(source=source, ts=time.monotonic(), dbfs=-30.0, peak=-20.0, bar=-20.0)


# ---------------------------------------------------------------------------
# ⑦ 帧间隔的声明与回退
# ---------------------------------------------------------------------------


def test_frame_interval_falls_back_when_the_backend_does_not_declare_it(tmp_path):
    """⑦ 不声明帧间隔的替身后端：按 `DEFAULT_FRAME_INTERVAL_SECONDS` 算窗口、服务照常起。"""
    module_dir = tmp_path / "fake-backends"
    module_dir.mkdir()
    (module_dir / "quiet_backends.py").write_text(
        textwrap.dedent(
            '''
            """测试用的采集替身：**只声明 probe()，不声明 frame_interval**。"""

            from meeting.backends.base import SourceProbe


            class QuietCaptureBackend:
                def bind(self, emit, *, config=None):
                    self._emit = emit

                def probe(self):
                    return [
                        SourceProbe(source="mic", status="ready", device_name="quiet-mic"),
                        SourceProbe(source="loopback", status="unprobed"),
                    ]

                def start(self, mode, session_dir):
                    return None

                def finalize(self):
                    return None

                def transcode(self):
                    return None

                def stop(self):
                    return None
            '''
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(module_dir))
    try:
        with live(
            tmp_path,
            {"capture.backend": "quiet_backends:QuietCaptureBackend"},
        ) as (client, runtime, home):
            assert runtime.frame_interval == DEFAULT_FRAME_INTERVAL_SECONDS
            assert frame_clock_window(runtime.frame_interval) == 2.0
            assert client.get("/api/heartbeat").status_code == 200
            # 服务照常起、`top_state` 不动
            assert client.get("/api/state").json()["top_state"] == "idle"
            # 落了一条 warning（回退只影响窗口取值）
            log_text = helpers.read_text(helpers.service_log_path_of(home))
            assert "frame_interval" in log_text
            assert "WARNING" in log_text
            # 还是能正常开一场（回退不阻塞 start）
            assert post(client, "start").status_code == 200
            assert client.get("/api/state").json()["top_state"] == "recording"
    finally:
        sys.path.remove(str(module_dir))


def test_frame_interval_with_a_non_positive_value_falls_back(tmp_path):
    """⑦ 取值非正数（`0` / 负数 / 非数值）同样回退，且不许炸在窗口计算上。"""
    module_dir = tmp_path / "fake-backends-bad"
    module_dir.mkdir()
    (module_dir / "odd_backends.py").write_text(
        textwrap.dedent(
            '''
            """测试用的采集替身：声明的帧间隔全是非正数 / 非数值。"""

            from meeting.backends.base import SourceProbe


            class ZeroIntervalBackend:
                frame_interval = 0

                def bind(self, emit, *, config=None):
                    pass

                def probe(self):
                    return [SourceProbe(source="mic", status="ready"),
                            SourceProbe(source="loopback", status="ready")]

                def start(self, mode, session_dir):
                    return None

                def finalize(self):
                    return None

                def transcode(self):
                    return None

                def stop(self):
                    return None


            class TextIntervalBackend(ZeroIntervalBackend):
                frame_interval = "半秒"
            '''
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(module_dir))
    try:
        for target in ("odd_backends:ZeroIntervalBackend", "odd_backends:TextIntervalBackend"):
            with live(tmp_path / target.split(":")[1], {"capture.backend": target}) as (
                client, runtime, _home
            ):
                assert runtime.frame_interval == DEFAULT_FRAME_INTERVAL_SECONDS
                assert client.get("/api/heartbeat").json()["recording_alive"] is None
                assert client.get("/api/state").json()["top_state"] == "idle"
    finally:
        sys.path.remove(str(module_dir))


def test_missing_frame_interval_attribute_falls_back(tmp_path):
    """⑦ 实例上根本没有 `frame_interval` 属性时同样回退。"""
    module_dir = tmp_path / "fake-backends-none"
    module_dir.mkdir()
    (module_dir / "bare_backends.py").write_text(
        textwrap.dedent(
            '''
            """测试用的采集替身：连 `frame_interval` 这个名字都不出现。"""

            from meeting.backends.base import SourceProbe


            class BareCaptureBackend:
                def bind(self, emit, *, config=None):
                    pass

                def probe(self):
                    return [SourceProbe(source="mic", status="ready"),
                            SourceProbe(source="loopback", status="ready")]

                def start(self, mode, session_dir):
                    return None

                def finalize(self):
                    return None

                def transcode(self):
                    return None

                def stop(self):
                    return None
            '''
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(module_dir))
    try:
        with live(tmp_path, {"capture.backend": "bare_backends:BareCaptureBackend"}) as (
            client, runtime, _home
        ):
            assert runtime.frame_interval == DEFAULT_FRAME_INTERVAL_SECONDS
            assert client.get("/api/heartbeat").json()["recording_alive"] is None
            assert client.get("/api/state").json()["top_state"] == "idle"
    finally:
        sys.path.remove(str(module_dir))


# ---------------------------------------------------------------------------
# 真服务一侧：心跳形状与探测判据
# ---------------------------------------------------------------------------


def test_heartbeat_over_http_carries_the_service_literal(tmp_path, service_process):
    """真服务子进程：六字段齐、`service` 字面量在（端口探针靠它认亲）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out")
    proc.wait_ready()
    payload = httpx.get(f"{proc.base_url}/api/heartbeat", timeout=5.0, trust_env=False).json()
    assert set(payload) == HEARTBEAT_FIELDS
    assert payload["service"] == "meeting"
    assert payload["top_state"] == "idle"
    assert payload["recording_alive"] is None
    # 端口策略的探针替身认亲成功
    from meeting.service import app as service_app

    assert service_app.probe_meeting_service("127.0.0.1", proc.port()) is True
