"""`tests/test_devices_endpoint.py` —— 设置抽屉节点的服务面（快档）。

覆盖三块，全部**不碰真实音频硬件**（设备枚举一律走假件，`tests/conftest.py` 的
`_no_real_audio_capture` 那道看门照旧在岗）：

  1. `GET /api/devices`（新只读端点）：两路各自的候选端点表、每项六格、
     **纯读**（不涨 `revision`、不广播）、任何状态下都 200、枚举失败如实 503；
  2. 三键写后即时可见（`HOT_KEYS` 收编的那四项里的三个非 ASR 键）：
     `settings_update` 确定后无需重启 —— 快照 `settings.devices` /
     `heartbeat_interval_seconds` / `summary_default_window` 当场换新值；
  3. `reprobe` 按新设备值挑：写盘成功后内存生效值已换，`rebind_and_probe()` bind 给
     后端的那份配置视图读到的就是新值（备料侦察风险 ① 的落地判据）。
"""

from __future__ import annotations

import json
from pathlib import Path

from meeting import config as config_module
from meeting.capture import sources as sources_module
from meeting.capture.devices import DeviceEndpoint
from tests import _helpers as helpers
from tests._app_drivers import live, post, snapshot_of

#: 假端点表（形状照 `tests/test_capture_probe.py` 的本机实测样子，名字自带「假-」前缀）
FAKE_INPUTS = (
    DeviceEndpoint("假-Focusrite Analogue 1 + 2", "MME", "input", index=0, channels=4,
                   is_default=True),
    DeviceEndpoint("假-CABLE-Output (VB-Audio Virtual Cable)", "MME", "input", index=2,
                   channels=16),
)
FAKE_RENDERS = (
    DeviceEndpoint("假-扬声器 (Focusrite USB Audio)", "", "render", index=0),
    DeviceEndpoint("假-CABLE Input (VB-Audio Virtual Cable)", "", "render", index=1),
)

#: 每一项端点行的六格（`DeviceEndpoint` 原样序列化）
ENDPOINT_ROW_KEYS = {"name", "host_api", "role", "index", "channels", "is_default"}


def _install_fake_endpoints(monkeypatch) -> None:
    """把 `sources.py` 的两个枚举函数换成假件（`devices_listing()` 调用的是当下那份）。"""
    monkeypatch.setattr(sources_module, "list_input_endpoints", lambda: list(FAKE_INPUTS))
    monkeypatch.setattr(sources_module, "list_render_endpoints", lambda: list(FAKE_RENDERS))


# ---------------------------------------------------------------------------
# GET /api/devices
# ---------------------------------------------------------------------------


def test_devices_listing_shape_and_purity(tmp_path, monkeypatch):
    """两路各一份清单、每项六格；纯读不涨 revision；录音态照 200。"""
    _install_fake_endpoints(monkeypatch)
    with live(tmp_path) as (client, runtime, _home, _out):
        before = snapshot_of(client)
        response = client.get("/api/devices")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"mic", "loopback"}
        assert [item["name"] for item in body["mic"]] == [
            "假-Focusrite Analogue 1 + 2", "假-CABLE-Output (VB-Audio Virtual Cable)",
        ]
        assert [item["name"] for item in body["loopback"]] == [
            "假-扬声器 (Focusrite USB Audio)", "假-CABLE Input (VB-Audio Virtual Cable)",
        ]
        for rows in body.values():
            for item in rows:
                assert set(item) == ENDPOINT_ROW_KEYS
        assert body["mic"][0]["is_default"] is True, "系统默认输入端点带着 is_default 标记"
        assert body["mic"][0]["role"] == "input"
        assert body["loopback"][0]["role"] == "render"
        # 纯读：revision 原地不动、不广播
        assert int(snapshot_of(client)["revision"]) == int(before["revision"])

        # 录音态照样 200（清单只枚举端点表；换不换得了由界面按第 7 章禁用）
        assert post(client, "start").status_code == 200
        assert client.get("/api/devices").status_code == 200
        del runtime  # live() 给的 runtime 在这一条里只用来证明不用它


def test_devices_listing_maps_enumeration_failure_to_capture_unavailable(tmp_path, monkeypatch):
    """端点表读不出来 → `capture_unavailable`(503)（复用冻结 `code`，不自造）。"""

    def boom():
        raise OSError("端口表读不动（注入）")

    monkeypatch.setattr(sources_module, "list_input_endpoints", boom)
    monkeypatch.setattr(sources_module, "list_render_endpoints", lambda: [])
    with live(tmp_path) as (client, _runtime, _home, _out):
        response = client.get("/api/devices")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["code"] == "capture_unavailable"
        assert set(body["detail"]) == {"error"}


# ---------------------------------------------------------------------------
# 三键写后即时可见（无需重启）
# ---------------------------------------------------------------------------


def test_heartbeat_interval_and_summary_window_apply_at_once(tmp_path):
    """`heartbeat.interval_seconds` 与 `summary.default_window` 写盘那一刻快照就换过去。"""
    with live(tmp_path) as (client, _runtime, home, _out):
        before_revision = int(snapshot_of(client)["revision"])

        response = post(
            client,
            "settings_update",
            {"heartbeat.interval_seconds": 42, "summary.default_window": "30m"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert sorted(body["applied"]) == ["heartbeat.interval_seconds",
                                           "summary.default_window"]
        assert body["pending_restart"] == []

        settings = snapshot_of(client)["settings"]
        assert settings["heartbeat_interval_seconds"] == 42, "心跳周期写完当场可见"
        assert settings["summary_default_window"] == "30m", "总结缺省档写完当场可见"
        assert int(snapshot_of(client)["revision"]) == before_revision + 1, "热读键生效提交一次"

        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["heartbeat.interval_seconds"] == 42
        assert written["summary.default_window"] == "30m"


def test_heartbeat_interval_out_of_range_is_rejected_before_writing(tmp_path):
    """心跳 1–60 之外当场拒绝并说明原因（`invalid_setting`(400)，校验在写盘之前）。"""
    with live(tmp_path) as (client, _runtime, home, _out):
        baseline = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8")) \
            if helpers.config_path_of(home).exists() else {}
        for value in (0, 61, -5):
            response = post(client, "settings_update", {"heartbeat.interval_seconds": value})
            assert response.status_code == 400, response.text
            body = response.json()
            assert body["code"] == "invalid_setting"
            assert body["detail"]["key"] == "heartbeat.interval_seconds"
            assert "1" in str(body["detail"]["legal"]) or body["detail"]["legal"]
        after = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8")) \
            if helpers.config_path_of(home).exists() else {}
        assert after == baseline, "一个非法的都没写进去"
        assert config_module.INT_LIMITS["heartbeat.interval_seconds"] == (1, 60)


# ---------------------------------------------------------------------------
# reprobe 用新设备值（侦察风险 ① 的落地判据）
# ---------------------------------------------------------------------------


def test_reprobe_picks_up_the_newly_written_device_value(tmp_path):
    """先写 `devices.mic` 再 `reprobe`：bind 给后端的那份配置视图读到的就是新值。"""
    seen: list[str | None] = []

    with live(tmp_path, {"capture.backend": "dev"}) as (client, runtime, _home, _out):
        backend = runtime.capture_backend
        real_bind = backend.bind

        def recording_bind(sink, *, config=None):
            value = None
            getter = getattr(config, "get", None)
            if callable(getter):
                value = getter("devices.mic")
            seen.append(value)
            return real_bind(sink, config=config)

        backend.bind = recording_bind
        from meeting.backends.base import SourceProbe

        runtime.capture_backend.probe = lambda: [
            SourceProbe(source="mic", status="ready", device_name="假-换上的麦克风",
                        detail="按新配置挑到的"),
            SourceProbe(source="loopback", status="ready", device_name="假-扬声器",
                        detail="按新配置挑到的"),
        ]

        # 启动探测发生在本用例接管 bind 之前（live() 起服务时已经探过一轮），
        # 因此 `seen` 此刻还空着；这里只认 reprobe 那一次 bind 的读数。

        written_value = "假-Focusrite Analogue 1 + 2"
        response = post(client, "settings_update", {"devices.mic": written_value})
        assert response.status_code == 200, response.text
        assert response.json()["applied"] == ["devices.mic"]

        assert post(client, "reprobe").status_code == 200
        assert seen, "reprobe 该重新 bind 一次"
        assert seen[-1] == written_value, (
            "reprobe 重新 bind 时读到的该是刚写进去的新值（写后即时生效，不等重启）"
        )
        # 结果如实渲染：快照两路的 device_name 是这次探测报的
        sources = snapshot_of(client)["sources"]
        assert sources[0]["device_name"] == "假-换上的麦克风"
