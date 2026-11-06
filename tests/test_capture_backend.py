"""`tests/test_capture_backend.py` —— 真采集后端的起停与收尾（`meeting/backends/capture.py`）。

**假源注入**（§5 的验收手段）：把 `capture.create_capture_source()` /
`create_loopback_source()` 换成假源，于是 `CaptureBackend.start()` 这条**生产代码路径**
被整条走通（挑端点 → 造源 → 起会话引擎 → 两路归档 → 收尾落盘 → 转码），换掉的只有最外面
那一层声卡库。一条真机调用都没有。

三块：

  1. `start()` 的抛错口径：单路起不来**不抛错**（只给那一路发 `SourceLost`）、本场该采的路
     全起不来才抛 `CaptureUnavailable`、`mode=mic` 时只有 mic 算数；
  2. 会话与收尾：归档写进 `session_dir`、`finalize()` 落盘、`transcode()` 出 MP3、`stop()` 幂等；
  3. 端到端（进程内真 `ServiceRuntime`）：`capture.backend = auto` 开一场 → 停 → 收尾 `flush`
     记 `done`、flush 落定自动起的转码独立工序转出两个 `.mp3`（状态文件落 `done`）。
"""

from __future__ import annotations

import time
import pytest

from pathlib import Path


from meeting.backends import capture as capture_module
from meeting.backends.base import ReadOnlyConfig, SourceLost
from meeting.backends.registry import capabilities_of
from meeting.capture import sources as sources_module
from meeting.capture.devices import DeviceEndpoint, OpenAttempt
from meeting.session import archive_name
from tests.test_api_actions import live, post, snapshot_of, wait_until
from tests.test_capture_engine import CHUNK_BYTES, FakeCaptureSource

#: 慢档标记（68927c27 收尾轮）：真流式/真后端的收尾验收，不进每次迭代圈。
pytestmark = pytest.mark.slow

#: 假端点表（形状照本机实测：同名端点在不同 host API 下并存）
FAKE_MIC = DeviceEndpoint("假-麦克风 (Focusrite)", "MME", "input", index=1, is_default=True)
FAKE_RENDER = DeviceEndpoint("假-扬声器 (Focusrite USB Audio)", "", "render", index=0)

#: 进程内用例的轮询超时（收尾三步跑在收尾线程池里）
BACKEND_POLL_TIMEOUT_SECONDS = 15.0


def wave_attempt() -> OpenAttempt:
    """能打开、也有波形的试开结果（本机实测的 MME 那条）。"""
    from meeting.capture.devices import measure_samples

    return OpenAttempt(
        opened=True, sample=measure_samples([0, 120, -300, 44, 900, -12, 7, -400, 220, 0])
    )


class SourceFactory:
    """假源工厂：按路造 `FakeCaptureSource`，并记下造了什么、`start()` 抛不抛。"""

    #: 端到端用例的块节奏（秒）：一个块 0.1 秒，与真采集同速（假源不设它就会把盘写满）
    PACE_SECONDS = 0.1

    def __init__(
        self,
        *,
        fail_start: tuple[str, ...] = (),
        chunks: int | None = None,
        error_at: dict[str, int] | None = None,
        pace_seconds: float = 0.0,
    ) -> None:
        self.fail_start = tuple(fail_start)
        self.chunks = chunks
        self.error_at = dict(error_at or {})
        self.pace_seconds = pace_seconds
        self.created: dict[str, FakeCaptureSource] = {}
        self.endpoints: dict[str, DeviceEndpoint] = {}
        self.starts: list[str] = []

    def __call__(self, source: str, endpoint: DeviceEndpoint) -> FakeCaptureSource:
        impl = _FailingStart(source) if source in self.fail_start else FakeCaptureSource(
            source,
            chunks=self.chunks,
            error_at=self.error_at.get(source),
            pace_seconds=self.pace_seconds,
        )
        self.created[source] = impl
        self.endpoints[source] = endpoint
        return impl


class _FailingStart(FakeCaptureSource):
    """`start()` 就抛错的假源（这一路本场起不来）。"""

    def start(self) -> None:  # type: ignore[override]
        raise RuntimeError(f"假件：{self.name} 这一路的设备打不开")


def install_fake_probe(monkeypatch: pytest.MonkeyPatch, factory: SourceFactory) -> None:
    """把**端点枚举 + 试开 + 造源**三处都换成假件，一个真声卡入口都不碰。

    三处都要换（`docs/v1-lineage.md` 的坑 C9 那一手：**函数内查找**）：

      - `sources.list_input_endpoints` / `list_render_endpoints` → 假端点表；
      - `sources.test_open_input` / `test_open_loopback` → 能打开、有波形的假结果；
      - `capture.create_capture_source` / `create_loopback_source` → 本用例的假源工厂。

    后端那四个接缝的缺省写法是 `lambda: sources_module.<f>()`（调用那一刻再取），所以
    `monkeypatch.setattr` 打在**模块对象**上照样生效 —— 后端构造函数里一个函数对象都没抓。
    """
    monkeypatch.setattr(sources_module, "list_input_endpoints", lambda: [FAKE_MIC])
    monkeypatch.setattr(sources_module, "list_render_endpoints", lambda: [FAKE_RENDER])
    monkeypatch.setattr(sources_module, "test_open_input", lambda endpoint: wave_attempt())
    monkeypatch.setattr(sources_module, "test_open_loopback", lambda endpoint: wave_attempt())
    monkeypatch.setattr(
        capture_module,
        "create_capture_source",
        lambda source, endpoint: factory(source, endpoint),
    )
    monkeypatch.setattr(
        capture_module, "create_loopback_source", lambda endpoint: factory("loopback", endpoint)
    )


def make_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    factory: SourceFactory,
    devices: dict[str, str | None] | None = None,
    no_endpoints: tuple[str, ...] = (),
) -> capture_module.CaptureBackend:
    """造一个真 `CaptureBackend` + 假端点表 + 假源工厂，并 `bind()` 上事实出口。

    `no_endpoints` 里的那几路**端点表为空**（挑不出设备那一档）。
    """
    values = {"devices.mic": None, "devices.loopback": None}
    values.update(devices or {})

    inputs = () if "mic" in no_endpoints else (FAKE_MIC,)
    renders = () if "loopback" in no_endpoints else (FAKE_RENDER,)

    monkeypatch.setattr(sources_module, "test_open_input", lambda endpoint: wave_attempt())
    monkeypatch.setattr(sources_module, "test_open_loopback", lambda endpoint: wave_attempt())
    monkeypatch.setattr(
        capture_module,
        "create_capture_source",
        lambda source, endpoint: factory(source, endpoint),
    )
    monkeypatch.setattr(
        capture_module, "create_loopback_source", lambda endpoint: factory("loopback", endpoint)
    )
    backend = capture_module.CaptureBackend(
        list_inputs=lambda: list(inputs),
        list_renders=lambda: list(renders),
    )
    backend.bind(lambda fact: None, config=ReadOnlyConfig(values))
    return backend


# ---------------------------------------------------------------------------
# ① 起采集：抛错口径
# ---------------------------------------------------------------------------


def test_start_brings_up_both_routes_and_writes_both_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """两路都起得来：归档写进 `session_dir`，字节数按 3200 一 块 对得上。"""
    factory = SourceFactory(chunks=4)
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("both", str(session_dir))
    try:
        assert set(factory.created) == {"mic", "loopback"}
        assert factory.endpoints["mic"] is FAKE_MIC
        assert factory.endpoints["loopback"] is FAKE_RENDER
        wait_until(
            lambda: backend.session.bytes_written("mic") >= 4 * CHUNK_BYTES,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="mic 那一路的归档长起来",
        )
        assert (session_dir / archive_name("mic")).stat().st_size >= 4 * CHUNK_BYTES
    finally:
        backend.stop()


def test_one_dead_route_does_not_stop_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """单路起不来**不抛错**：那一路收到一条 `SourceLost`，另一路照采。"""
    facts: list[object] = []
    factory = SourceFactory(fail_start=("loopback",), chunks=4)
    backend = make_backend(monkeypatch, factory=factory)
    backend.bind(facts.append, config=ReadOnlyConfig({"devices.mic": None}))
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("both", str(session_dir))  # 不抛
    try:
        lost = [fact for fact in facts if isinstance(fact, SourceLost)]
        assert [fact.source for fact in lost] == ["loopback"]
        assert "设备打不开" in lost[0].reason
        wait_until(
            lambda: backend.session.bytes_written("mic") >= 4 * CHUNK_BYTES,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="另一路的归档照样长起来",
        )
    finally:
        backend.stop()


def test_start_raises_when_every_requested_route_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """本场该采的路全起不来 → 抛 `CaptureUnavailable`，两路的缘由都在 `message` 里。"""
    factory = SourceFactory(fail_start=("mic", "loopback"))
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    with pytest.raises(capture_module.CaptureUnavailable) as info:
        backend.start("both", str(session_dir))
    assert info.value.code == "capture_unavailable"
    assert "采集起不来" in str(info.value)
    assert "mic" in str(info.value) and "loopback" in str(info.value)
    assert backend.session is None


def test_start_raises_when_the_device_table_has_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """一条端点都挑不出来（空表）→ 抛 `CaptureUnavailable`，不留下会话。"""
    factory = SourceFactory()
    backend = make_backend(monkeypatch, factory=factory, no_endpoints=("mic", "loopback"))
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    with pytest.raises(capture_module.CaptureUnavailable) as info:
        backend.start("both", str(session_dir))
    # 两路各自为什么挑不出端点都写在 `message` 里（mic 是「候选为空」那一档）
    assert "候选为空" in str(info.value)
    assert "一个 render（播放）端点都没有" in str(info.value)
    # 一条源都没有被造出来（连开设备那一步都没走到）
    assert factory.created == {}
    assert backend.session is None


def test_mic_mode_only_counts_the_mic_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`mode=mic`：只有 mic 起不来才算全败（loopback 那一路本场不采，轮不到它说话）。"""
    factory = SourceFactory(fail_start=("loopback",))
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("mic", str(session_dir))
    try:
        assert set(factory.created) == {"mic"}
    finally:
        backend.stop()

    # 反过来：mic 起不来就是全败（本场只采这一路）
    factory2 = SourceFactory(fail_start=("mic",))
    backend2 = make_backend(monkeypatch, factory=factory2)
    with pytest.raises(capture_module.CaptureUnavailable):
        backend2.start("mic", str(session_dir))


def test_mic_mode_does_not_read_the_loopback_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`mode=mic` 时不读 loopback：那一路的源连造都不造，归档**整段时间写静音**。

    「某一路没开」也是「那一路写静音」（`docs/design-intent.md` 第 9 条）：不再是 0 字节
    文件——两路时间轴等长，落盘的全是数字零，内容量一格不动。
    """
    factory = SourceFactory(chunks=3)
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("mic", str(session_dir))
    session = backend.session
    try:
        wait_until(
            lambda: session.bytes_written("mic") >= 3 * CHUNK_BYTES,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="mic 那一路的归档长起来",
        )
        wait_until(
            lambda: session.silence_bytes("loopback") >= 2 * CHUNK_BYTES,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="loopback 那一路的静音写起来",
        )
    finally:
        backend.stop()
    loop_file = session_dir / archive_name("loopback")
    assert loop_file.exists()
    assert loop_file.stat().st_size == session.bytes_written("loopback") > 0
    assert session.content_bytes("loopback") == 0
    assert loop_file.read_bytes() == bytes(loop_file.stat().st_size)  # 逐样本为零


# ---------------------------------------------------------------------------
# ② 会话与收尾
# ---------------------------------------------------------------------------


def test_finalize_flushes_both_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`finalize()` 把两路归档落盘：之后 `stop()` 不会再多写一个字节。"""
    factory = SourceFactory(chunks=4)
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("both", str(session_dir))
    wait_until(
        lambda: backend.session.bytes_written("mic") >= 4 * CHUNK_BYTES,
        timeout=BACKEND_POLL_TIMEOUT_SECONDS,
        what="mic 那一路的归档长起来",
    )
    backend.finalize()
    sizes = {source: (session_dir / archive_name(source)).stat().st_size for source in ("mic", "loopback")}
    assert sizes["mic"] > 0
    backend.finalize()  # 幂等
    backend.stop()
    assert {
        source: (session_dir / archive_name(source)).stat().st_size for source in ("mic", "loopback")
    } == sizes


def test_stop_is_idempotent_and_clears_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`stop()` 幂等：连调三次不抛错，会话引用被清掉。"""
    factory = SourceFactory(chunks=3)
    backend = make_backend(monkeypatch, factory=factory)
    session_dir = tmp_path / "250906"
    session_dir.mkdir()

    backend.start("both", str(session_dir))
    assert backend.session is not None
    backend.stop()
    backend.stop()
    backend.stop()
    assert backend.session is None
    assert factory.created["mic"].stopped is True


def test_a_second_start_replaces_the_previous_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """再 `start()` 一场：上一场先收干净（旧会话的线程不留在后台）。"""
    factory = SourceFactory(chunks=3)
    backend = make_backend(monkeypatch, factory=factory)
    first_dir = tmp_path / "250906"
    second_dir = tmp_path / "260907"
    first_dir.mkdir()
    second_dir.mkdir()

    backend.start("both", str(first_dir))
    old_session = backend.session
    backend.start("both", str(second_dir))
    try:
        assert backend.session is not old_session
        assert old_session.workers["mic"]._threads == []
        assert (second_dir / archive_name("mic")).exists()
    finally:
        backend.stop()


def test_the_real_backend_still_has_no_inject_fault(monkeypatch: pytest.MonkeyPatch):
    """`dev_inject` 在真后端上照旧不可用；两项收尾能力如实为真。"""
    backend = make_backend(monkeypatch, factory=SourceFactory())
    assert not hasattr(backend, "inject_fault")
    assert capabilities_of(backend) == {
        "finalize": True,
        "transcode": True,
        "dev_inject": False,
    }


# ---------------------------------------------------------------------------
# ③ 端到端：flush 记 done、转码独立工序转出两份 MP3
# ---------------------------------------------------------------------------


def test_end_to_end_session_drains_with_two_mp3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """进程内真运行时 + `auto` 真后端 + 假源：开一场 → 停 → 收尾 `flush` 跑真实现，
    转码独立工序（flush 落定自动起的那一道）转出两个 `.mp3`。

    `flush` 这一档记 **`done`**（不再是 `skipped`）：真后端落地之后
    `registry.capabilities_of()` 对 `finalize` 报 `True`，收尾第 1 步因此真的调它。

    转码已不在收尾序列里：它是这一场的**独立后台工序**（`meeting/capture/transcode.py`），
    flush 落定那一刻自动在后台起（判据同样是采集侧的 `capture_capabilities["transcode"]`，
    转写后端在不在场与它无关）。本场的采集后端是真的（`auto`），两个 `.mp3` 由**真代码**
    经这道工序转出来（不是替身记的一笔）；它的完成以逐段凭据与状态文件落 `done` 为准。
    """
    factory = SourceFactory(chunks=None, pace_seconds=SourceFactory.PACE_SECONDS)
    install_fake_probe(monkeypatch, factory)

    with live(tmp_path, {"capture.backend": "auto"}) as (client, _runtime, _home, out_root):
        started = post(client, "start", {"mode": "both"})
        assert started.status_code == 200, started.text

        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        session_dir = Path(snapshot_of(client)["session"]["dir"])
        assert {item["device_name"] for item in snapshot_of(client)["sources"]} <= {
            FAKE_MIC.name,
            FAKE_RENDER.name,
            None,
        }

        # 让归档真的长出一些字节再收尾
        wait_until(
            lambda: (session_dir / archive_name("mic")).exists()
            and (session_dir / archive_name("mic")).stat().st_size > 0,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="mic 归档开始长字节",
        )

        assert post(client, "stop").status_code == 200
        done = wait_until(
            lambda: (
                lambda snap: snap
                if snap["ended"] and snap["ended"]["phase"] == "done"
                else None
            )(snapshot_of(client)),
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="收尾停在 done",
        )

        steps = {step["id"]: step for step in done["ended"]["steps"]}
        assert steps["flush"]["status"] == "done"
        assert steps["log"]["status"] == "done"
        assert steps["post"]["status"] == "waiting"
        assert set(steps) == {"flush", "log", "post"}, sorted(steps)

        # 转码独立工序在 flush 落定后自动起：等它凭据落齐、状态落 done，MP3 才算「写完了」
        from meeting.capture import transcode as transcode_module

        document = wait_until(
            lambda: (
                doc
                if (doc := transcode_module.read_status(session_dir)) is not None
                and doc.get("state") == "done"
                else None
            ),
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="转码独立工序落 done",
        )
        assert document["sources"][0]["done"] == 1 and document["sources"][1]["done"] == 1, document
        assert transcode_module.segment_done(
            transcode_module.plan_segments(session_dir, ("mic",))[0]
        )
        assert transcode_module.segment_done(
            transcode_module.plan_segments(session_dir, ("loopback",))[0]
        )

        assert (session_dir / "meeting_audio_mic_001.mp3").exists()
        assert (session_dir / "meeting_audio_loopback_001.mp3").exists()
        # 两份 PCM 原样留着（`session.json` 声明的是它们）
        assert (session_dir / archive_name("mic")).exists()
        assert (session_dir / archive_name("loopback")).exists()
        # 归档落在输出根下（没有落到别处）
        assert str(session_dir).startswith(str(out_root))


def test_end_to_end_finalize_step_records_failure_when_a_route_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """一路在运行期死掉：会话照常收尾、`flush` 仍记 `done`，两路归档都留在场面上。"""
    factory = SourceFactory(
        chunks=None, error_at={"loopback": 3}, pace_seconds=SourceFactory.PACE_SECONDS
    )
    install_fake_probe(monkeypatch, factory)

    with live(tmp_path, {"capture.backend": "auto"}) as (client, _runtime, _home, _out):
        assert post(client, "start", {"mode": "both"}).status_code == 200
        session_dir = Path(snapshot_of(client)["session"]["dir"])

        # 等到那一路真的读到第 3 块（随后它自己炸掉 → 一条 SourceLost → 顶层进 alert）
        wait_until(
            lambda: factory.created["loopback"].fed >= 3,
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="loopback 那一路读到第 3 块",
        )
        time.sleep(0.1)

        assert post(client, "stop").status_code == 200
        done = wait_until(
            lambda: (
                lambda snap: snap
                if snap["ended"] and snap["ended"]["phase"] in ("done", "failed")
                else None
            )(snapshot_of(client)),
            timeout=BACKEND_POLL_TIMEOUT_SECONDS,
            what="收尾落定",
        )
        steps = {step["id"]: step for step in done["ended"]["steps"]}
        assert steps["flush"]["status"] == "done"
        # 死掉那一路留下的是它读到的那三块；另一路照常继续、字节数更多
        assert (session_dir / archive_name("loopback")).stat().st_size == 3 * CHUNK_BYTES
        assert (session_dir / archive_name("mic")).stat().st_size > 3 * CHUNK_BYTES
