"""`tests/test_capture_probe.py` —— 真探测这一段的后端、动作面与配置面。

覆盖三块，全部**不碰真实硬件**（`meeting/capture/sources.py` 的枚举与试开、以及
`meeting/backends/capture.py` 的两个造源函数在这里被换成假件；`CaptureBackend` 的设备层
四个接缝是它自己的构造参数，所以注入的是**生产代码那一份真类**）：

  1. `meeting/backends/capture.py` 的 `probe()`：两路各一条结论、`status` 三档之内、
     `device_name` 是真端点名、试开真的读回样本、重复探测幂等（`reprobe` 走的就是它）；
  2. 采集引擎的接缝（这一段**只验接线**：真起两路、真写归档、收尾真落盘与真转码的用例在
     `tests/test_capture_backend.py`）：本文件把 `sources.py` 的枚举/试开与
     `capture.create_capture_source()` / `create_loopback_source()` 一起换成假件，于是
     `start()` 这条生产路径能整条走通，而设备表与试开结论仍由假件摆布；
  3. 选择面：`capture.backend` 默认的 `auto` 解析到真类（`registry` 的约定路径）、
     快照 `settings.devices` 两个子键、`settings_update` 写 `devices.mic` 落盘、
     `reprobe` 动作在四态下的行为与 `ACTION_NAMES` 的形状。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.backends import capture as capture_module
from meeting.backends.base import SOURCE_IDS, SOURCE_STATUS
from meeting.backends.registry import capabilities_of, resolve_capture_backend
from meeting.capture import devices as devices_module
from meeting.capture import sources as sources_module
from meeting.capture.devices import DeviceEndpoint, OpenAttempt, measure_samples
from tests import _helpers as helpers
from tests._app_drivers import live, post, snapshot_of

# 假端点表与假试开（真机实测的形状：见 meeting/capture/devices.py 的模块注释）

WAVE = [0, 120, -300, 44, 900, -12, 7, -400, 220, 0]
SILENCE = [0] * 120

#: 假端点表：同一块 Focusrite 在 WASAPI / DirectSound / MME
#: 三个 host API 下同名（`_resolve_mic_device` 的排序与试开那段就是为这个形状写的），
#: 外加一条抓取型虚拟端点 `CABLE Output`。MME 那条带 `is_default=True` = 系统默认输入端点；
#: 序号最小的则是 MME #0 的 WAVE_MAPPER。
FAKE_MIC_NAME = "假-Focusrite Analogue 1 + 2"
FAKE_INPUTS: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint(FAKE_MIC_NAME, "MME", "input", index=0, channels=4, is_default=True),
    DeviceEndpoint(FAKE_MIC_NAME, "Windows WASAPI", "input", index=18, channels=2),
    DeviceEndpoint(FAKE_MIC_NAME, "Windows DirectSound", "input", index=8, channels=4),
    DeviceEndpoint("假-CABLE-Output (VB-Audio Virtual Cable)", "MME", "input", index=2,
                   channels=16),
)

FAKE_RENDERS: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint("假-扬声器 (Focusrite USB Audio)", "", "render", index=0),
    DeviceEndpoint("假-CABLE Input (VB-Audio Virtual Cable)", "", "render", index=1),
)


def endpoint_key(endpoint: DeviceEndpoint) -> str:
    """假件的查表键：**名字 + host API**（同名端点在多个 host API 下要分开登记）。"""
    return f"{endpoint.name}@{endpoint.host_api}"


def wave_attempt() -> OpenAttempt:
    return OpenAttempt(opened=True, sample=measure_samples(WAVE))


def silence_attempt() -> OpenAttempt:
    return OpenAttempt(opened=True, sample=measure_samples(SILENCE))


def refuse_attempt(reason: str = "Invalid sample rate [PaErrorCode -9997]") -> OpenAttempt:
    return OpenAttempt(opened=False, error=reason)


class FakeSources:
    """把 `sources.py` 的四个函数换成假件（`CaptureBackend` 的缺省接缝按当下那份查）。"""

    def __init__(
        self,
        *,
        inputs: tuple[DeviceEndpoint, ...] = FAKE_INPUTS,
        renders: tuple[DeviceEndpoint, ...] = FAKE_RENDERS,
        input_attempts: dict[str, OpenAttempt] | None = None,
        loopback_attempts: dict[str, OpenAttempt] | None = None,
        input_default: OpenAttempt | None = None,
        loopback_default: OpenAttempt | None = None,
        input_error: BaseException | None = None,
    ) -> None:
        self.inputs = inputs
        self.renders = renders
        self.input_attempts = dict(input_attempts or {})
        self.loopback_attempts = dict(loopback_attempts or {})
        self.input_default = input_default or wave_attempt()
        self.loopback_default = loopback_default or silence_attempt()
        self.input_error = input_error
        #: 试开过的端点（`endpoint_key()` 形态，能分辨同名端点在不同 host API 下的两次试开）
        self.input_calls: list[str] = []
        self.loopback_calls: list[str] = []
        self.list_calls = 0

    def list_input_endpoints(self) -> list[DeviceEndpoint]:
        self.list_calls += 1
        return list(self.inputs)

    def list_render_endpoints(self) -> list[DeviceEndpoint]:
        return list(self.renders)

    def test_open_input(self, endpoint: DeviceEndpoint) -> OpenAttempt:
        self.input_calls.append(endpoint_key(endpoint))
        if self.input_error is not None:
            raise self.input_error
        return self.input_attempts.get(endpoint_key(endpoint), self.input_default)

    def test_open_loopback(self, endpoint: DeviceEndpoint) -> OpenAttempt:
        self.loopback_calls.append(endpoint_key(endpoint))
        return self.loopback_attempts.get(endpoint_key(endpoint), self.loopback_default)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeSources":
        """把枚举、试开与**两个造源函数**一起换成假件（`start()` 因此也走假源）。

        `capture.create_capture_source()` / `create_loopback_source()` 是采集引擎的注入接缝：
        后端用的是「调用那一刻再取」的模块级查找，所以打在**模块对象**上照样生效。
        """
        from tests._capture_fakes import FakeCaptureSource

        monkeypatch.setattr(sources_module, "list_input_endpoints", self.list_input_endpoints)
        monkeypatch.setattr(sources_module, "list_render_endpoints", self.list_render_endpoints)
        monkeypatch.setattr(sources_module, "test_open_input", self.test_open_input)
        monkeypatch.setattr(sources_module, "test_open_loopback", self.test_open_loopback)
        monkeypatch.setattr(
            capture_module,
            "create_capture_source",
            lambda source, endpoint: FakeCaptureSource(source, chunks=None, pace_seconds=0.05),
        )
        monkeypatch.setattr(
            capture_module,
            "create_loopback_source",
            lambda endpoint: FakeCaptureSource(
                "loopback", chunks=None, pace_seconds=0.05
            ),
        )
        return self


def probe_with(config_values: dict | None = None, **fake_kwargs) -> tuple[list, FakeSources, Any]:
    """造一个真 `CaptureBackend` + 一份只读配置视图，跑一次 `probe()`。

    假件缺省的样子：同一块 Focusrite 在 WASAPI 下开不了（`Invalid
    sample rate`）、在 DirectSound 下能打开却交回全零、在 MME 下有真实波形 —— 于是
    「选中 MME 那条」这件事只有在试开真的读回样本时才成立（光看 host API 名字会挑到
    DirectSound）。loopback 那一侧同理：系统默认 render 端点交回全零（能打开），
    `CABLE Input` 那条打开成功。
    """
    from meeting.backends.base import ReadOnlyConfig

    silent_inputs = {
        f"{FAKE_MIC_NAME}@Windows WASAPI": refuse_attempt(),
        f"{FAKE_MIC_NAME}@Windows DirectSound": silence_attempt(),
        "假-CABLE-Output (VB-Audio Virtual Cable)@MME": silence_attempt(),
    }
    silent_renders = {"假-扬声器 (Focusrite USB Audio)@": silence_attempt()}

    # 调用方只给了兜底结果（例如「一条都开不了」）时，兜底管住全部端点：清掉登记表
    if "input_default" in fake_kwargs and "input_attempts" not in fake_kwargs:
        silent_inputs = {}
    if "loopback_default" in fake_kwargs and "loopback_attempts" not in fake_kwargs:
        silent_renders = {}

    fake_kwargs.setdefault("input_attempts", silent_inputs)
    fake_kwargs.setdefault("input_default", wave_attempt())
    fake_kwargs.setdefault("loopback_attempts", silent_renders)
    fake_kwargs.setdefault("loopback_default", silence_attempt())

    fake = FakeSources(**fake_kwargs)
    backend = capture_module.CaptureBackend(
        list_inputs=fake.list_input_endpoints,
        list_renders=fake.list_render_endpoints,
        test_open_input=fake.test_open_input,
        test_open_loopback=fake.test_open_loopback,
    )
    values = {"devices.mic": None, "devices.loopback": None}
    values.update(config_values or {})
    backend.bind(lambda fact: None, config=ReadOnlyConfig(values))
    return backend.probe(), fake, backend


# ① probe() 的形状与真探测


def test_probe_returns_one_conclusion_per_source():
    """两路各一条，次序固定，`source` 落在取值域里。"""
    probes, _fake, _backend = probe_with()
    assert [probe.source for probe in probes] == list(SOURCE_IDS)


def test_probe_status_stays_inside_the_frozen_domain():
    """`status` 只许 `ready` / `probe_failed` / `unprobed` 三档（真后端只报前两档）。"""
    probes, _fake, _backend = probe_with()
    assert all(probe.status in SOURCE_STATUS for probe in probes)
    assert {probe.status for probe in probes} == {"ready"}


def test_probe_reads_samples_back_to_pick_the_microphone():
    """试开**读回样本**：DirectSound 那条能打开但交回全零，选中的因此是 MME 那条。

    三条同名候选按 host rank 试开：WASAPI 开不了 → DirectSound 开了但没波形 → MME 有波形。
    """
    probes, fake, _backend = probe_with()
    mic = probes[0]
    assert mic.device_name == FAKE_MIC_NAME
    assert fake.input_calls == [
        f"{FAKE_MIC_NAME}@Windows WASAPI",
        f"{FAKE_MIC_NAME}@Windows DirectSound",
        f"{FAKE_MIC_NAME}@MME",
    ]
    # 抓到取型虚拟端点（`CABLE Output`）一条都没被试过
    assert all("CABLE-Output" not in key for key in fake.input_calls)
    assert "有真实波形" in mic.detail
    assert "没有波形" in mic.detail  # DirectSound 那条的留痕
    assert "Invalid sample rate" in mic.detail  # WASAPI 那条的留痕


def test_probe_picks_the_first_opened_endpoint_when_no_waveform_shows_up():
    """一条有波形的都没有（房间安静 / 远端没出声）：取**第一条打开了**的端点，仍报 `ready`。

    三条同名候选中 WASAPI 那条在 16k 开不了、DirectSound 与 MME 都能打开但交回全零 ——
    中选的是排序后第一条**打开了**的 DirectSound。
    """
    probes, fake, _backend = probe_with(
        input_attempts={
            f"{FAKE_MIC_NAME}@Windows WASAPI": refuse_attempt(),
            f"{FAKE_MIC_NAME}@Windows DirectSound": silence_attempt(),
            f"{FAKE_MIC_NAME}@MME": silence_attempt(),
        }
    )
    mic = probes[0]
    assert mic.status == "ready"
    assert mic.device_name == FAKE_MIC_NAME
    assert "取第一条**打开了**的候选" in mic.detail
    # 三条都试过（试开次序照 host rank），选中那条是同名三条里的 DirectSound
    assert fake.input_calls == [
        f"{FAKE_MIC_NAME}@Windows WASAPI",
        f"{FAKE_MIC_NAME}@Windows DirectSound",
        f"{FAKE_MIC_NAME}@MME",
    ]
    assert f"取第一条**打开了**的候选 {FAKE_MIC_NAME}（Windows DirectSound #8）" in mic.detail


def test_probe_reports_the_real_endpoint_name_for_loopback():
    """loopback 那条报的是 render 端点的真名（`CABLE Input` 2ch）。"""
    probes, fake, _backend = probe_with()
    loopback = probes[1]
    assert loopback.status == "ready"
    assert loopback.device_name == "假-CABLE Input (VB-Audio Virtual Cable)"
    assert fake.loopback_calls == ["假-CABLE Input (VB-Audio Virtual Cable)@"]


def test_probe_honours_an_explicitly_configured_device():
    """显式配置的名字落成 `device_name`，同名候选仍按 host rank 逐个试开（V1 的骨架）。"""
    probes, fake, _backend = probe_with({"devices.mic": FAKE_MIC_NAME})
    assert probes[0].device_name == FAKE_MIC_NAME
    assert fake.input_calls == [
        f"{FAKE_MIC_NAME}@Windows WASAPI",
        f"{FAKE_MIC_NAME}@Windows DirectSound",
        f"{FAKE_MIC_NAME}@MME",
    ]


def test_probe_mic_falls_back_while_loopback_reports_probe_failed():
    """全都开不了时两路各走各的：mic 落 `probe_failed`，loopback 同样如实落。

    mic 这一侧就是复审核定的那一条：同名三条候选一条都没打开 → **一个能打开的端点都没有**，
    `device_name` 为 `None`、`detail` 逐条写明试过哪几条、各自为什么没成（**不回落**一个从没
    打开过的候选）。
    """
    probes, _fake, _backend = probe_with(
        input_default=refuse_attempt(),
        loopback_default=refuse_attempt("soundcard 里没有这条端点的 loopback"),
    )
    assert [probe.status for probe in probes] == ["probe_failed", "probe_failed"]
    # mic：同名三条候选全都开不了 → 一个端点都不落，detail 逐条留痕
    assert probes[0].device_name is None
    assert "一条都没打开" in probes[0].detail
    assert "Invalid sample rate" in probes[0].detail
    assert "不回落从没打开过的候选" in probes[0].detail
    # loopback：挑中的 render 端点打不开，如实报出
    assert probes[1].device_name is None
    assert "挑中的 render 端点打不开" in probes[1].detail
    assert "soundcard 里没有这条端点的 loopback" in probes[1].detail


def test_probe_survives_an_enumerating_backend_that_raises():
    """枚举端点时抛错：如实降级成 `probe_failed`，服务照常起。"""
    probes, _fake, _backend = probe_with(
        input_error=OSError("假件：PortAudio 起不来"),
    )
    # 枚举本身没抛，试开抛了 → 这一路如实报 `probe_failed`
    assert probes[0].status == "probe_failed"


def test_probe_is_idempotent_and_uses_the_current_config():
    """重复探测幂等，且每次都取**当下**的配置（`reprobe` 走的正是这条路）。"""
    from meeting.backends.base import ReadOnlyConfig

    fake = FakeSources()
    backend = capture_module.CaptureBackend(
        list_inputs=fake.list_input_endpoints,
        list_renders=fake.list_render_endpoints,
        test_open_input=fake.test_open_input,
        test_open_loopback=fake.test_open_loopback,
    )
    config = {"devices.mic": None, "devices.loopback": None}
    backend.bind(lambda fact: None, config=ReadOnlyConfig(config))
    first = backend.probe()
    second = backend.probe()
    assert [probe.device_name for probe in first] == [probe.device_name for probe in second]

    # 换了配置再探一次：这一路跟着换（同一个实例、同一份 bind 之后的新视图）
    config["devices.mic"] = "假-扬声器 (Focusrite USB Audio)"
    backend.bind(lambda fact: None, config=ReadOnlyConfig(config))
    third = backend.probe()
    # 配了一个输入端点表里没有的名字 → 候选为空 → `probe_failed`（与上面那一版不同）
    assert third[0].status == "probe_failed"
    assert third[0].device_name is None
    assert third[0].detail != second[0].detail
    # loopback 那一侧没被配置改动顶到，结论不变
    assert third[1].device_name == second[1].device_name
    assert backend.last_probes == third


def test_probe_reads_the_loopback_setting_too():
    """`devices.loopback` 也读：配了名字就精确命中那个 render 端点。"""
    probes, fake, _backend = probe_with(
        {"devices.loopback": "假-扬声器 (Focusrite USB Audio)"}
    )
    assert probes[1].device_name == "假-扬声器 (Focusrite USB Audio)"
    assert fake.loopback_calls == ["假-扬声器 (Focusrite USB Audio)@"]


# ② 采集引擎的接线（真起两路 / 真写归档 / 收尾真落盘与真转码见
#    `tests/test_capture_backend.py`；这里只验「挑不出端点就没得采」这一档）


def test_start_refuses_when_no_endpoint_can_be_opened():
    """一条端点都挑不出来：`start()` 抛 `CaptureUnavailable`，`message` 逐路写明缘由。"""
    from meeting.backends.null import CaptureUnavailable

    backend = capture_module.CaptureBackend(
        list_inputs=lambda: [],
        list_renders=lambda: [],
        test_open_input=lambda endpoint: refuse_attempt(),
        test_open_loopback=lambda endpoint: refuse_attempt(),
    )
    with pytest.raises(CaptureUnavailable) as info:
        backend.start("both", "C:/tmp/session")
    assert "采集起不来" in str(info.value)
    assert "mic" in str(info.value) and "loopback" in str(info.value)
    assert info.value.code == "capture_unavailable"
    assert CaptureUnavailable is capture_module.CaptureUnavailable


def test_finalize_and_stop_are_safe_before_any_session():
    """一场都没开就收尾（`start()` 抛错之后那条路）：不抛错、不复用什么会话。"""
    backend = capture_module.CaptureBackend(
        list_inputs=lambda: [],
        list_renders=lambda: [],
        test_open_input=lambda endpoint: refuse_attempt(),
        test_open_loopback=lambda endpoint: refuse_attempt(),
    )
    assert backend.finalize() is None
    assert backend.stop() is None


def test_the_real_backend_has_no_inject_fault():
    """**不许加 `inject_fault`**：`dev_inject` 的可用性就是靠这个属性判的。"""
    probes, _fake, backend = probe_with()
    assert not hasattr(backend, "inject_fault")
    assert capabilities_of(backend)["dev_inject"] is False
    # 另外两项按 registry 的口径回答（实现者在场即有这两项能力的能力位）
    assert capabilities_of(backend)["finalize"] is True
    assert capabilities_of(backend)["transcode"] is True


def test_frame_interval_is_declared():
    """帧间隔有位可读（协议默认 0.5s），采集引擎那一段照这个节奏发帧。"""
    _probes, _fake, backend = probe_with()
    assert backend.frame_interval == 0.5


# ③ 选择面：解析、配置键、动作


def test_auto_resolves_to_the_real_capture_backend():
    """默认 `auto` 走 `registry` 的约定路径，解析到真类（不再回退 null 替身）。"""
    resolution = resolve_capture_backend()
    assert resolution.target == "meeting.backends.capture:CaptureBackend"
    assert type(resolution.implementation) is capture_module.CaptureBackend
    assert resolution.has_implementation is True


def test_settings_devices_shape_and_update_round_trip(tmp_path: Path):
    """快照 `settings.devices` 两个子键；`settings_update` 写 `devices.mic` 落盘并当场生效。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, home, _out):
        settings = snapshot_of(client)["settings"]
        assert set(settings) == {
            "alarm_sound", "heartbeat_interval_seconds", "summary_default_window",
            "summary_preset_names", "summary_active_preset",
            "asr", "devices",
        }
        # 没配时两个子键都是 null（空值形状与配置层的 NULLABLE_KEYS 一致）
        assert settings["devices"] == {"mic": None, "loopback": None}

        before_revision = int(snapshot_of(client)["revision"])
        response = post(client, "settings_update", {"devices.mic": "Analogue 1 + 2 (2- Focusrite US"})
        assert response.status_code == 200, response.text
        body = response.json()
        # 设备键位收在热读档：写盘成功，快照当场换新值、revision 涨一格
        assert body["applied"] == ["devices.mic"]
        assert body["pending_restart"] == []
        assert snapshot_of(client)["settings"]["devices"]["mic"] == (
            "Analogue 1 + 2 (2- Focusrite US"
        )
        assert int(snapshot_of(client)["revision"]) == before_revision + 1

        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["devices.mic"] == "Analogue 1 + 2 (2- Focusrite US"

        # 值非法照样在写盘之前拦下
        bad = post(client, "settings_update", {"devices.mic": 3})
        assert bad.status_code == 400
        assert bad.json()["code"] == "invalid_setting"

        # 清空成 null 也合法（`null` = 没配，按默认策略挑），同样当场可见
        cleared = post(client, "settings_update", {"devices.mic": None})
        assert cleared.status_code == 200, cleared.text
        assert snapshot_of(client)["settings"]["devices"]["mic"] is None
        written = json.loads(helpers.config_path_of(home).read_text(encoding="utf-8"))
        assert written["devices.mic"] is None


def test_devices_keys_land_in_the_applied_group():
    """两个设备键位在三分表里归 `applied`（设置抽屉节点 改的热读档）。"""
    assert config_module.setting_group("devices.mic") == "applied"
    assert config_module.setting_group("devices.loopback") == "applied"
    assert config_module.DEVICE_SETTING_KEYS == ("mic", "loopback")
    assert "devices.mic_device" not in config_module.SETTING_KEY_GROUPS
    assert config_module.setting_group("devices.mic_device") is None


def test_reprobe_action_re_enumerates_and_shares_the_writing_route(tmp_path: Path):
    """`reprobe` 在空闲态可用：重新探测、更新两路、涨一次 revision。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, runtime, home, _out):
        before = snapshot_of(client)
        assert before["top_state"] == "idle"
        assert [item["device_name"] for item in before["sources"]] == [None, None]

        calls: list[str] = []

        def fake_probe():
            calls.append("probe")
            from meeting.backends.base import SourceProbe

            return [
                SourceProbe(source="mic", status="ready", device_name="真麦克风",
                            detail="重新探测挑到的"),
                SourceProbe(source="loopback", status="ready",
                            device_name="CABLE Input (VB-Audio Virtual Cable)",
                            detail="重新探测挑到的"),
            ]

        runtime.capture_backend.probe = fake_probe
        response = post(client, "reprobe")
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["revision"] == before["revision"] + 1
        assert calls == ["probe"]

        after = snapshot_of(client)
        assert [item["device_name"] for item in after["sources"]] == [
            "真麦克风", "CABLE Input (VB-Audio Virtual Cable)",
        ]
        assert all(item["health"] == "ready" for item in after["sources"])
        # 两条健康迁移都落进主 log（`unprobed -> ready`）
        log_text = helpers.read_text(helpers.service_log_path_of(home))
        assert "每一路健康迁移：mic unprobed -> ready" in log_text
        assert "每一路健康迁移：loopback unprobed -> ready" in log_text


def test_reprobe_is_refused_while_a_session_is_running(tmp_path: Path):
    """`recording` / `alert` 态下 `reprobe` 拒绝（会中换设备是明文不做的那一档）。"""
    with live(tmp_path) as (client, _runtime, _home, _out):
        assert post(client, "start").status_code == 200
        recording = snapshot_of(client)
        assert recording["top_state"] == "recording"

        refused = post(client, "reprobe")
        assert refused.status_code == 409
        assert refused.json()["code"] == "already_recording"
        # 被拒绝的请求不涨 revision、也不动两路
        assert snapshot_of(client)["revision"] == recording["revision"]

        # 收尾走完之后（`ended`）重新探测又能用了
        assert post(client, "stop").status_code == 200
        from tests._app_drivers import wait_for_done

        wait_for_done(client)
        allowed = post(client, "reprobe")
        assert allowed.status_code == 200, allowed.text


def test_reprobe_action_is_reachable_through_the_http_endpoint(tmp_path: Path):
    """动作端点认这个新名字（不是只加进 `ACTION_NAMES` 就完事）。"""
    with live(tmp_path, {"capture.backend": "null"}) as (client, _runtime, _home, _out):
        accepted = post(client, "reprobe")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["ok"] is True
        # 动作名清单本身也经端点报出来（不认识的名字照旧 400）
        unknown = post(client, "definitely_not_an_action")
        assert unknown.status_code == 400
        assert "reprobe" in unknown.json()["detail"]["known"]


# ④ 服务侧也走注入：起真服务时把两路探测换成假端点表
#
# **这一段不碰真声卡**（用户明确要求：没有知情同意就不许打开他机器上的输入设备）。
# `startup()` 里的 `probe()` 由**注入的假端点表 + 假试开函数**交回结论，于是「服务真把
# 探测结果落进快照两路」这件事照样验得到，而一个音频流都不会被打开。


def _install_fake_probe(monkeypatch: pytest.MonkeyPatch, fake: FakeSources) -> None:
    """把 `sources.py` 的四个函数换成假件（服务启动时那一次 `probe()` 走的就是它们）。"""
    fake.install(monkeypatch)


def test_service_startup_leaves_unprobed_for_a_source_without_a_probe(tmp_path, monkeypatch):
    """后端只报了 mic 一条：loopback 那一格按 `unprobed` 落（三档里的第三档）。"""
    from meeting.backends.base import SourceProbe

    class MicOnlyBackend:
        """只报 mic 的假后端：用来验 runtime 那一侧「没有探测结果 → `unprobed`」的落法。"""

        frame_interval = 0.5

        def bind(self, emit, *, config=None):
            return None

        def probe(self):
            return [SourceProbe(source="mic", status="ready", device_name="假-单路麦克风")]

        def start(self, mode, session_dir):
            return None

        def finalize(self):
            return None

        def stop(self):
            return None

    with live(tmp_path, {"capture.backend": "null"}) as (client, runtime, _home, _out):
        runtime.capture_backend = MicOnlyBackend()
        runtime.capture_resolution.implementation = runtime.capture_backend
        assert post(client, "reprobe").status_code == 200
        sources = {item["id"]: item for item in snapshot_of(client)["sources"]}
        assert sources["mic"]["health"] == "ready"
        assert sources["mic"]["device_name"] == "假-单路麦克风"
        assert sources["loopback"]["health"] == "unprobed"
        assert sources["loopback"]["device_name"] is None
        assert {sources["mic"]["health"], sources["loopback"]["health"]} <= set(SOURCE_STATUS)


def test_service_startup_lands_the_probe_results_in_the_snapshot(tmp_path, monkeypatch):
    """起真服务（`capture.backend = auto`）：两路快照落的是假端点表交回的真设备名。"""
    _install_fake_probe(monkeypatch, FakeSources())

    with live(tmp_path, {"capture.backend": "auto"}) as (client, runtime, _home, _out):
        snapshot = snapshot_of(client)
        assert snapshot["top_state"] == "idle"
        sources = {item["id"]: item for item in snapshot["sources"]}
        # 假端点表里 MME 那条有波形（DirectSound 那条开了却全是零）—— 选中它就是
        # 「试开真的读了样本」的证据
        assert sources["mic"]["health"] == "ready"
        assert sources["mic"]["device_name"] == FAKE_MIC_NAME
        assert sources["loopback"]["health"] == "ready"
        assert sources["loopback"]["device_name"] == "假-CABLE Input (VB-Audio Virtual Cable)"
        assert sources["mic"]["device_name"] not in ("dev-mic", "null")

        # 两路真设备名同时写进 `session.json`（开场前定下来的那一份）
        from meeting.backends.base import SourceProbe

        assert isinstance(runtime.capture_backend, capture_module.CaptureBackend)
        assert [probe.status for probe in runtime.probes] == ["ready", "ready"]
        assert {probe.source for probe in runtime.probes} == set(SOURCE_IDS)
        assert all(isinstance(probe, SourceProbe) for probe in runtime.probes)

        # 采集引擎已经在场：起一场走的是真路径（假源喂料），归档落在场次目录里
        from meeting.session import archive_name

        started = post(client, "start")
        assert started.status_code == 200, started.text
        session_dir = Path(snapshot_of(client)["session"]["dir"])
        helpers.wait_until(
            lambda: (session_dir / archive_name("mic")).exists()
            and (session_dir / archive_name("mic")).stat().st_size > 0,
            timeout=10.0,
            what="mic 那一份归档长起来",
        )
        assert post(client, "stop").status_code == 200


def test_service_startup_reports_probe_failed_for_both_sources(tmp_path, monkeypatch):
    """端点表里一条候选都没有（枚举失败那一档）：两路 `probe_failed`、`device_name` 为 `null`。

    V1 在「候选为空」这一档是 `log.warning(...); return None`（交给系统默认输入）；
    V2 要如实报出三态，所以这一档落成 `probe_failed` 并把原因写进 `detail`。
    服务照常起，一个音频流都没被打开。
    """
    fake = FakeSources(inputs=(), renders=())
    _install_fake_probe(monkeypatch, fake)

    with live(tmp_path, {"capture.backend": "auto"}) as (client, runtime, _home, _out):
        sources = {item["id"]: item for item in snapshot_of(client)["sources"]}
        assert sources["mic"]["health"] == "probe_failed"
        assert sources["mic"]["device_name"] is None
        assert sources["loopback"]["health"] == "probe_failed"
        assert sources["loopback"]["device_name"] is None
        # 一条端点都没有：一次试开都没发生
        assert fake.input_calls == []
        assert fake.loopback_calls == []
        details = {probe.source: probe.detail for probe in runtime.probes}
        assert "候选为空" in details["mic"]
        assert "一个 render（播放）端点都没有" in details["loopback"]


def test_service_startup_survives_an_enumeration_that_raises(tmp_path, monkeypatch):
    """枚举端点时抛错：两路如实落 `probe_failed`，服务照常起（后端不把服务带崩）。"""
    fake = FakeSources(input_error=OSError("假件：PortAudio 起不来"))
    _install_fake_probe(monkeypatch, fake)

    with live(tmp_path, {"capture.backend": "auto"}) as (client, _runtime, _home, _out):
        sources = {item["id"]: item for item in snapshot_of(client)["sources"]}
        assert sources["mic"]["health"] == "probe_failed"
        assert sources["mic"]["device_name"] is None
        # 试开那一步抛的错被如实收成「这一条没成」，loopback 那一侧照常探到
        assert sources["loopback"]["health"] == "ready"


def test_reprobe_action_re_runs_the_injected_probe(tmp_path, monkeypatch):
    """`reprobe` 真的再探一遍：换一份假端点表，快照两路跟着换。"""
    first = FakeSources(
        inputs=(DeviceEndpoint("假-一号麦克风", "MME", "input", index=1),),
        renders=(DeviceEndpoint("假-一号回环", "", "render", index=0),),
        input_default=wave_attempt(),
        loopback_default=silence_attempt(),
    )
    first.install(monkeypatch)

    with live(tmp_path, {"capture.backend": "auto"}) as (client, _runtime, _home, _out):
        assert [item["device_name"] for item in snapshot_of(client)["sources"]] == [
            "假-一号麦克风", "假-一号回环",
        ]

        second = FakeSources(
            inputs=(DeviceEndpoint("假-二号麦克风", "MME", "input", index=1),),
            renders=(DeviceEndpoint("假-二号回环", "", "render", index=0),),
            input_default=wave_attempt(),
            loopback_default=silence_attempt(),
        )
        second.install(monkeypatch)

        response = post(client, "reprobe")
        assert response.status_code == 200, response.text
        assert [item["device_name"] for item in snapshot_of(client)["sources"]] == [
            "假-二号麦克风", "假-二号回环",
        ]


# ⑤ 纯逻辑那一侧的 import 边界（`devices.py` 不许 import 声卡库）


def test_devices_module_has_no_module_level_audio_imports():
    """`meeting/capture/devices.py` 的 import 表里没有声卡库与 numpy（纯逻辑那一侧）。"""
    import ast
    import pathlib

    source = pathlib.Path(devices_module.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    roots = {name.split(".")[0] for name in imported}
    assert roots.isdisjoint({"sounddevice", "soundcard", "numpy", "pydub"})


# ⑥ `list_input_endpoints()` 给**系统默认输入端点**打标记（C1 的接线口）
#
# 这一节的读数是许可以内的那一档：**只读枚举**（`query_devices` / `query_hostapis` /
# `default.device`），一次流都不开。真库在这里被换成替身，所以本节连真机枚举都不碰。


class _FakeDefault:
    """`sd.default` 的替身（只带 `.device` 这一对读数）。"""

    def __init__(self, value: tuple[int, int] | None) -> None:
        if value is not None:
            self.device = value


class _FakeSoundDevice:
    """`sounddevice` 枚举面的替身：`query_devices()` / `query_hostapis()` / `default.device`。

    表的样子：`#0` 是 MME 的 `Microsoft 声音映射器 - Input`（WAVE_MAPPER，
    跟随系统默认输入设备）、`#1` 是 MME 的 Focusrite（**系统默认输入**）、另有一条
    `max_input_channels=0` 的播放端点（枚举时就该被滤掉）。
    """

    def __init__(
        self,
        *,
        default: tuple[int, int] | None = (1, 6),
        default_input_raises: bool = False,
        kind_input: dict | None = None,
    ) -> None:
        self.devices = [
            {"name": "Microsoft 声音映射器 - Input", "hostapi": 0, "max_input_channels": 2},
            {"name": "Analogue 1 + 2 (2- Focusrite US", "hostapi": 0, "max_input_channels": 4},
            {"name": "扬声器 (2- Focusrite USB Audio)", "hostapi": 1, "max_output_channels": 2},
        ]
        self.hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        self.default = _FakeDefault(default)
        self.default_input_raises = default_input_raises
        self.kind_input = kind_input or {"index": 1, "name": "Analogue 1 + 2 (2- Focusrite US"}

    def query_devices(self, kind: str | None = None):  # noqa: ANN201 - 替身，形状随库
        if kind == "input":
            if self.default_input_raises:
                raise OSError("假件：PortAudio 报不出默认输入")
            return self.kind_input
        return self.devices

    def query_hostapis(self):
        return self.hostapis


def _enumerate_with(fake: _FakeSoundDevice, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sources_module, "_import_sounddevice", lambda: fake)
    return sources_module.list_input_endpoints()


def test_enumeration_marks_the_system_default_input_endpoint(monkeypatch):
    """`is_default` 落在系统默认输入那条上（本机形态：默认输入 `#1`，序号最小的是 `#0`）。"""
    endpoints = _enumerate_with(_FakeSoundDevice(), monkeypatch)
    assert [(item.index, item.is_default) for item in endpoints] == [(0, False), (1, True)]
    assert [item.channels for item in endpoints] == [2, 4]
    # 播放端点（`max_input_channels=0`）在枚举这一步就被滤掉：表里没有它
    assert "扬声器 (2- Focusrite USB Audio)" not in [item.name for item in endpoints]

    # 换一份默认输入读数：标记跟着读数走（读数说哪条，`is_default` 就标在哪条上）
    moved = _FakeSoundDevice(kind_input={"index": 0, "name": "Microsoft 声音映射器 - Input"})
    moved.default = _FakeDefault((0, 6))
    endpoints = _enumerate_with(moved, monkeypatch)
    assert [(item.index, item.is_default) for item in endpoints] == [(0, True), (1, False)]


def test_default_input_falls_back_to_the_query_and_survives_a_missing_one(monkeypatch):
    """`default.device[0]` 是 `-1` 时退到 `query_devices(kind="input")`；两条都读不出来时无人带标记。"""
    unresolved = _FakeSoundDevice(default=(-1, -1))
    endpoints = _enumerate_with(unresolved, monkeypatch)
    assert [(item.index, item.is_default) for item in endpoints] == [(0, False), (1, True)]

    silent = _FakeSoundDevice(default=None, default_input_raises=True)
    monkeypatch.setattr(sources_module, "_import_sounddevice", lambda: silent)
    assert sources_module._default_input_index(silent) is None
    endpoints = sources_module.list_input_endpoints()
    assert [item.is_default for item in endpoints] == [False, False]


def test_mic_without_config_lands_on_the_enumerated_default(monkeypatch):
    """接线口：没配 `devices.mic` 时，挑选落在枚举标出的那条系统默认输入上。

    本机读数就是这一条：默认输入是 MME `#1` Focusrite，报出来的 `device_name` 是 MME 那条
    的截断名（同一块声卡的 WASAPI / DirectSound 条目带着完整名，靠前缀也能兜上，但精确名
    先命中，所以候选集合收在 MME 那一条上）。
    """
    endpoints = _enumerate_with(_FakeSoundDevice(), monkeypatch)
    candidates, _pseudo, matched_prefix = devices_module.mic_candidates(endpoints, None)
    assert [item.name for item in candidates] == ["Analogue 1 + 2 (2- Focusrite US"]
    assert matched_prefix is False


# ⑦ 看门抛的异常穿得过试开那两步（`sources.py` 的两处 `except` 口径）

#: 看门装的桩的记号（`tests/conftest.py` 的 `_forbid_real_capture()` 里那个内层函数名）
GUARD_STUB_QUALNAME_TOKEN = "blocked"
#: 看门的桩定义在哪个模块（身份断言的第二半）
GUARD_STUB_MODULE = "tests.conftest"


def _assert_entry_is_stubbed(target: object, entry: str) -> None:
    """**发真调用之前**断言 `target` 确实是 `tests/conftest.py` 那道看门装的桩。

    这里是 C2 复查的落点：这几条用例的安全性完全押在它们正在验的那道看门上。看门一旦回归
    （夹具没装上桩、或桩被还原成真入口），调用一次 `sounddevice.InputStream(0)` 就会经
    `sources.py` 落到 PortAudio 上真开设备、真读样本。所以判定必须在**调用之前**做完，
    不满足就 fail 在这一句上 —— 调用一次都不发出去。
    """
    assert callable(target), (
        f"{entry} 不是可调用的桩（拿到的是 {target!r}）：拒绝往下发真调用"
    )
    qualname = str(getattr(target, "__qualname__", ""))
    module = str(getattr(target, "__module__", ""))
    assert GUARD_STUB_QUALNAME_TOKEN in qualname, (
        f"{entry} 上没有看门装的桩（__qualname__ = {qualname!r}，"
        f"预期含 {GUARD_STUB_QUALNAME_TOKEN!r}）："
        "往下走这一句就会真的打开设备、真的录 0.5 秒，因此停在这里"
    )
    assert module == GUARD_STUB_MODULE, (
        f"{entry} 上的桩不来自 {GUARD_STUB_MODULE}（__module__ = {module!r}）："
        "看门没装上，停在这里"
    )


@pytest.mark.parametrize(
    ("what", "entry"),
    [
        ("test_open_input", "sounddevice.InputStream"),
        ("test_open_loopback", "soundcard.get_microphone"),
    ],
)
def test_guard_exception_reaches_the_case_through_the_open_steps(
    monkeypatch: pytest.MonkeyPatch, what: str, entry: str
) -> None:
    """看门抛的异常**穿得过** `sources.py` 试开那两步的 `except`，原样炸到用例上。

    这两步收的是「开流失败」（`except Exception`），而看门（`tests/conftest.py` 的
    `RealCaptureForbidden`）**不继承 `Exception`**。于是：

      - 修之前（`except BaseException`）：`AssertionError` 被收成一条
        `OpenAttempt(opened=False, error="AssertionError: 自动化测试不许碰真声卡…")` 交回来，
        用例看到的是「设备开不了」，**看不出这是看门在说话**；
      - 修之后：异常原样往上抛，用例当场看到看门的文案与它点名的那个入口。

    本用例把 `sources.py` 里那两个 `_import_*` 换成**真的** import（`import sounddevice` /
    `import soundcard`）：库此刻已经在 `sys.modules` 里、且入口挂着看门的桩，于是「惰性
    import 拿到的就是那个被打过桩的模块对象」这条也在同一条用例里落地。

    **发调用之前先断言那个入口确实是桩**（`_assert_entry_is_stubbed()`）：这条用例的安全性
    整个押在那道看门上 —— 看门一回归，下面这一句就会经 `sources.py` 真的开
    `InputStream(device=0, samplerate=16000, …)` 并 `read(8000)`（0.5 秒），loopback 那条会
    真的抓 0.5 秒扬声器。断言落在调用之前：桩没装上就红在断言上，**绝不把调用发出去**。
    """
    import importlib

    module_name, entry_name = entry.split(".")
    try:
        module = importlib.import_module(module_name)
    except Exception:  # pragma: no cover - 这台机器上没装这个库
        pytest.skip(f"本机没装 {module_name}：没有可拦的入口")
    assert hasattr(module, entry_name)

    # **调用之前的身份断言**：目标必须是 conftest 那道看门装的桩，否则当场 fail
    _assert_entry_is_stubbed(getattr(module, entry_name), entry)

    # `sources.py` 里两个「惰性 import」的接缝换成真的 import：拿到的就是上面那个模块对象
    # —— 看门的桩此刻正挂在它的 `entry_name` 上。
    monkeypatch.setattr(
        sources_module, "_import_sounddevice", lambda: importlib.import_module("sounddevice")
    )
    monkeypatch.setattr(
        sources_module, "_import_soundcard", lambda: importlib.import_module("soundcard")
    )

    from tests.conftest import REAL_CAPTURE_FORBIDDEN_MESSAGE, RealCaptureForbidden

    call = getattr(sources_module, what)
    with pytest.raises(RealCaptureForbidden) as info:
        call(DeviceEndpoint("假端点", "MME", "input", index=0, channels=1))

    text = str(info.value)
    assert REAL_CAPTURE_FORBIDDEN_MESSAGE in text
    assert entry in text
