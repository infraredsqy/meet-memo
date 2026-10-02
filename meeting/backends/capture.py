"""采集后端 —— 设备层 + 采集引擎（`dca4539b`）。

约定导入路径就是这个模块的 `CaptureBackend`（`meeting/backends/registry.py` 的
`DEFAULT_PATHS["capture.backend"]`）：`capture.backend` 取默认值 `auto` 时解析到的就是它。
构造函数**不许有必填参数**（registry 无参实例化），运行期依赖一律走 `bind()`。

  - `probe()` 真的去问系统有哪些声卡端点、真的以 16k 试开一遍并读回样本，把结果如实报成
    两路 `ready` / `probe_failed`（三档取值域见 `meeting/backends/base.py` 的 `SOURCE_STATUS`）；
  - 「录哪一路用哪个设备」由 `devices.mic` / `devices.loopback` 两个配置键定，挑选策略在
    `meeting/capture/devices.py`（纯逻辑），真实开设备在 `meeting/capture/sources.py`；
  - 用户在录音前改了配置打 `reprobe`，runtime 再调一次 `probe()` 就重新探测（`probe()` 幂等）；
  - `start()` 起**两路各自独立**的采集（读侧在 `sources.py`、写侧与电平刻度在
    `meeting/capture/engine.py`），归档写进 `start()` 收到的 `session_dir`；
  - `finalize()` 停流 + join + 关文件 + `fsync`，`transcode()` 把各段 PCM 转成 MP3
    （收尾第 1、2 步），`stop()` 幂等收场（进程关停时调）；
  - `pause()` / `resume()` / `deactivate_source()` / `activate_source()` 是暂停与
    「用户关一路 / 重新打开一路」的服务层出口（写静音、不录内容；告警语义不动），
    动作面入口是 `pause` / `resume` / `set_source` 三个动作（`meeting/service/routes.py`）。

**`start()` 的抛错口径**（§5 的裁定）：单路起不来**不抛错** —— 只给那一路发一条
`SourceLost`（`reason` 写人话），另一路照跑；**本场该采的路全部起不来**才抛
`CaptureUnavailable`。`mode=mic` 时只有 mic 起不来才算全败。`start()` 抛错之前自己拉起来的
线程要自己收干净（runtime 的 `_abort_session()` 那条路**不调** `stop()`）。

**没有 `inject_fault`**（有意不加）：`dev_inject` 动作的可用性就是靠这个属性判的，给真后端
加上它会把开发替身的非产品动作在真后端上打开。`capabilities_of()` 因此对这一项报 `False`，
`dev_inject` 在真后端上照旧回 `action_unavailable`(404)。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from meeting.backends.base import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    SOURCE_IDS,
    SourceLost,
    SourceProbe,
)
from meeting.capture import devices as devices_module
from meeting.capture import sources as sources_module
from meeting.capture import engine as engine_module

_log = logging.getLogger("meeting.backends.capture")

#: 采集合法的开场模式（`start(mode, ...)` 的取值域，与动作面的校验同源）
CAPTURE_MODES: tuple[str, ...] = engine_module.CAPTURE_MODES

try:  # 与 `meeting.backends.null` 同一档异常（`code` 供界面 / 动作端点转成错误信封）
    from meeting.backends.null import CaptureUnavailable
except ImportError:  # pragma: no cover - null.py 一直在，这里只是不让导入路径成为单点
    CaptureUnavailable = RuntimeError  # type: ignore[assignment,misc]


class CaptureBackend:
    """真采集后端：`probe()` 真探测，`start()` 真起两路，收尾真落盘与真转码。"""

    #: 帧间隔的声明位（协议默认 0.5s：服务侧的心跳窗口是 `max(2.0s, 3 × frame_interval)`）
    frame_interval: float = DEFAULT_FRAME_INTERVAL_SECONDS

    def __init__(
        self,
        *,
        list_inputs: Callable[[], list[devices_module.DeviceEndpoint]] | None = None,
        list_renders: Callable[[], list[devices_module.DeviceEndpoint]] | None = None,
        test_open_input: Callable[[devices_module.DeviceEndpoint], devices_module.OpenAttempt]
        | None = None,
        test_open_loopback: Callable[
            [devices_module.DeviceEndpoint], devices_module.OpenAttempt
        ]
        | None = None,
        source_factory: Callable[[str, devices_module.DeviceEndpoint], Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        """六个可注入的接缝（缺省都指向 `meeting/capture/sources.py` 与 `meeting/capture/engine.py`
        的**当下**那一份）。

        缺省写法用 lambda 把查找推到调用那一刻（构造时不抓函数对象）：测试里
        `monkeypatch.setattr(capture_module, "create_capture_source", fake)` 与
        `monkeypatch.setattr(sources_module, "list_input_endpoints", fake)` 因此照样生效
        （`docs/v1-lineage.md` 的坑 C9：`from ... import f` 绑的是函数对象，改不动）。

        后两个接缝是采集引擎的注入点：`source_factory(source, endpoint)` 造一路采集源
        （缺省走本模块的 `create_capture_source()` / `create_loopback_source()`），
        `session_factory(**kwargs)` 造会话引擎（缺省 `engine.CaptureSession`）。**假源注入
        是这一段的验收手段**：测试因此验的是生产代码这条路径（真读线程、真写线程、真电平
        刻度、真收尾落盘），换掉的只有最外面那一层声卡库。
        """
        self._list_inputs = list_inputs or (lambda: sources_module.list_input_endpoints())
        self._list_renders = list_renders or (lambda: sources_module.list_render_endpoints())
        self._test_open_input = test_open_input or (
            lambda endpoint: sources_module.test_open_input(endpoint)
        )
        self._test_open_loopback = test_open_loopback or (
            lambda endpoint: sources_module.test_open_loopback(endpoint)
        )
        self._source_factory = source_factory or _default_source_factory
        self._session_factory = session_factory or engine_module.CaptureSession
        self._emit: Callable[[Any], None] | None = None
        self._config: Any = None
        #: 最近一次 `probe()` 的两条结论（`reprobe` 与 `/debug` 看这份）
        self.last_probes: list[SourceProbe] = []
        #: 正在跑的这一场（没有会话时为 `None`）
        self.session: Any = None

    # -- 协议 -------------------------------------------------------------

    def bind(self, emit: Callable[[Any], None], *, config: Any = None) -> None:
        """记下事实出口与只读配置视图（runtime 在 `probe()` 之前调一次）。"""
        self._emit = emit
        self._config = config

    def probe(self) -> list[SourceProbe]:
        """枚举端点、按配置与策略挑选、试开并**读回样本**，两路各一条结论。

        幂等：重复调用就是重新探测一遍（`reprobe` 动作走的就是这条路），每次都用**当下**
        的配置与**当下**的系统端点表。
        """
        mic_config = self._setting("devices.mic")
        loopback_config = self._setting("devices.loopback")

        mic_outcome = self._safe_select(
            source="mic",
            select=lambda: devices_module.select_microphone(
                self._safe_list(self._list_inputs, "输入端点"), mic_config, self._test_open_input
            ),
        )
        loopback_outcome = self._safe_select(
            source="loopback",
            select=lambda: devices_module.select_loopback(
                self._safe_list(self._list_renders, "render 端点"),
                loopback_config,
                self._test_open_loopback,
            ),
        )

        probes = [
            SourceProbe(
                source="mic",
                status=mic_outcome.status,
                device_name=mic_outcome.device_name(),
                detail=mic_outcome.detail,
            ),
            SourceProbe(
                source="loopback",
                status=loopback_outcome.status,
                device_name=loopback_outcome.device_name(),
                detail=loopback_outcome.detail,
            ),
        ]
        self.last_probes = list(probes)
        for probe in probes:
            _log.info(
                "设备探测：%s -> status=%s device=%s（%s）",
                probe.source, probe.status, probe.device_name, probe.detail,
            )
        return probes

    def start(self, mode: str, session_dir: str) -> None:
        """起采集：按 `mode` 起该采的路，归档写进 `session_dir`。

        抛错口径（§5 的裁定）：**单路起不来不抛错**，只给那一路发一条 `SourceLost`
        （`reason` 写人话），另一路照跑；本场该采的路**全部**起不来才抛 `CaptureUnavailable`。
        `mode=mic` 时只有 mic 起不来才算全败。
        """
        requested = engine_module.sources_for_mode(mode)
        self._stop_session()
        session = self._session_factory(
            mode=mode,
            session_dir=session_dir,
            sources={},
            emit=self._emit_fact,
            frame_interval=self.frame_interval,
            requested=requested,
        )
        self.session = session

        selected: dict[str, devices_module.DeviceEndpoint] = {}
        failures: dict[str, str] = {}
        for source in requested:
            try:
                selected[source] = self._select_endpoint(source)
            except Exception as exc:  # noqa: BLE001 - 单路挑不出来不拖死另一路
                failures[source] = self._failure_text(exc)
                _log.warning("采集设备挑不出来：%s（%s）", source, failures[source])

        opened: dict[str, str] = {}
        for source in requested:
            endpoint = selected.get(source)
            if endpoint is None:
                continue
            try:
                candidate = self._source_factory(source, endpoint)
                session.begin_source(source, candidate)
                candidate.start()
            except Exception as exc:  # noqa: BLE001 - 打开失败的那一路如实报 SourceLost
                failures[source] = self._failure_text(exc)
                _log.warning(
                    "采集源打不开：%s -> %s（%s）", source, endpoint.label(), failures[source]
                )
                continue
            except BaseException:
                # `RealCaptureForbidden`（`tests/conftest.py` 的看门，`BaseException` 直系类）
                # 必须炸到用例上：这条路意味着「有人真的去开声卡了」，在这里收成业务失败
                # 就把看门废了。收干净已经起来的东西再上抛（runtime 的 abort 路不调 `stop()`）。
                # 这一支写在 `except Exception` **之后**：先收业务失败，剩下的才轮到看门。
                session.stop()
                self.session = None
                raise
            opened[source] = endpoint.label()

        if not opened:
            session.stop()
            self.session = None
            raise CaptureUnavailable(self._unavailable_text(requested, failures))

        session.start()
        # 起不来的那一路：如实报一条 `SourceLost`（另一路已经在采，这里**不抛错**）
        for source in requested:
            if source not in opened:
                session.mark_lost(source, failures[source])
        _log.info(
            "采集已起：mode=%s session=%s 在采 %s%s",
            mode,
            session_dir,
            "、".join(f"{source}[{label}]" for source, label in opened.items()),
            ("；起不来：" + "、".join(f"{source}（{reason}）" for source, reason in failures.items()))
            if failures
            else "",
        )

    def finalize(self) -> None:
        """收尾第 1 步：停流 + join + 关文件 + `fsync`。"""
        session = self.session
        if session is None:  # pragma: no cover - 收尾只在采集能力在场时被调
            return
        session.finalize()

    def transcode(self) -> None:
        """收尾第 2 步：两路 PCM → MP3（转不动就原样抛错，PCM 留着，该步记 `failed`）。"""
        session = self.session
        if session is None:  # pragma: no cover - 同上
            return
        session.transcode()

    def stop(self) -> None:
        """停采集（**幂等**）：没落过盘就先 `finalize()`，再清掉这一场。"""
        self._stop_session()

    # -- 暂停 / 关一路（引擎能力的服务层出口；动作面入口归暂停/切源节点） -------

    def pause(self) -> None:
        """暂停本场采集：所有路写数字静音、不录内容（`docs/design-intent.md` 第 8 条）。

        没有在录的会话时什么都不做（幂等；动作面怎么暴露这个能力是下游节点的事，
        `ACTION_NAMES` 与顶层四态本节点都不动）。
        """
        session = self.session
        if session is None:
            return
        session.pause()

    def resume(self) -> None:
        """继续本场采集（幂等；没有在录的会话时什么都不做）。"""
        session = self.session
        if session is None:
            return
        session.resume()

    def deactivate_source(self, source: str) -> None:
        """用户主动关掉某一路：那一路写静音，**不报 `SourceLost`、不进告警**（幂等）。"""
        session = self.session
        if session is None:
            return
        session.deactivate_source(source)

    def activate_source(self, source: str) -> None:
        """用户重新打开某一路：重新挑设备、重开流，交回引擎的读线程。

        **失败原样上抛**（调用方转成 `capture_unavailable`）：设备又被别的程序占着这一档
        必须让按按钮的人看得见 —— 悄悄不打开等于「看起来在录、其实没录」
        （`docs/design-intent.md` 第 1 条）。上抛之前把刚造出来的那一份源收干净；
        引擎那一侧此刻还没清「用户已关」的标志，那一路照旧如实停在写静音。
        """
        session = self.session
        if session is None:
            return
        endpoint = self._select_endpoint(source)
        candidate = self._source_factory(source, endpoint)
        try:
            candidate.start()
        except BaseException:
            self._safe_stop(candidate)
            raise
        session.activate_source(source, candidate)
        _log.info("用户重新打开 %s 这一路：设备 %s", source, endpoint.label())

    # -- 内部 -------------------------------------------------------------

    def _stop_session(self) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        try:
            session.stop()
        except Exception:  # noqa: BLE001 - 收场失败不掩盖别的结论
            _log.warning("停采集时抛错（如实记账，不静默）", exc_info=True)

    def _safe_stop(self, source_impl: Any) -> None:
        """收掉一份刚造出来的源（重开失败那条路用；停不掉也不掩盖真正的原因）。"""
        stop = getattr(source_impl, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:  # noqa: BLE001 - 见上
            _log.warning("收掉重开失败的那一份源时抛错（如实记账）", exc_info=True)

    def _select_endpoint(self, source: str) -> devices_module.DeviceEndpoint:
        """按当下的配置与端点表挑这一路的设备（挑不出来抛错，由调用方如实记账）。"""
        configured = self._setting(f"devices.{source}")
        if source == "mic":
            outcome = devices_module.select_microphone(
                self._safe_list(self._list_inputs, "输入端点"), configured, self._test_open_input
            )
        else:
            outcome = devices_module.select_loopback(
                self._safe_list(self._list_renders, "render 端点"),
                configured,
                self._test_open_loopback,
            )
        if outcome.endpoint is None:
            raise RuntimeError(outcome.detail or f"{source} 这一路挑不出能打开的端点")
        return outcome.endpoint

    def _emit_fact(self, fact: Any) -> None:
        """把一条事实交给服务侧（`emit` 由 runtime 在 `bind()` 里给，可从任何线程调）。"""
        emit = self._emit
        if emit is None:  # pragma: no cover - runtime 一定先 bind
            _log.warning("采集还没有接上事实出口（emit 为 None），这一条事实只能留在本地")
            return
        emit(fact)

    def _failure_text(self, exc: BaseException) -> str:
        """一路起不来的一句话原因（`SourceLost.reason` 与 `CaptureUnavailable` 都用它）。"""
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        return f"{exc.__class__.__name__}: {message}"

    def _unavailable_text(self, requested: tuple[str, ...], failures: dict[str, str]) -> str:
        """本场该采的路全起不来时的说明（逐路写清为什么）。"""
        detail = "；".join(f"{source}：{failures.get(source, '没有可用的端点')}" for source in requested)
        return f"采集起不来（本场该采 {'、'.join(requested)}）：{detail}"

    def _setting(self, key: str) -> str | None:
        """读一个配置键（`bind()` 收到的是 `ReadOnlyConfig`，读不到就按没配处理）。"""
        config = self._config
        if config is None:
            return None
        value: Any = None
        getter = getattr(config, "get", None)
        if callable(getter):
            value = getter(key)
        elif isinstance(config, dict):
            value = config.get(key)
        if not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    def _safe_list(
        self,
        lister: Callable[[], list[devices_module.DeviceEndpoint]],
        what: str,
    ) -> list[devices_module.DeviceEndpoint]:
        """枚举端点；枚举本身失败时**如实降级成空表**（挑不出设备即 `probe_failed`）。"""
        try:
            return list(lister() or [])
        except Exception as exc:  # noqa: BLE001 - 枚举失败不该带崩探测
            _log.warning("设备探测：%s 枚举失败（%s: %s）", what, type(exc).__name__, exc)
            return []

    def _safe_select(
        self, *, source: str, select: Callable[[], devices_module.SelectionOutcome]
    ) -> devices_module.SelectionOutcome:
        """跑一遍挑选；挑不出结论时收成一条 `probe_failed`（细节写清哪一步出的错）。"""
        try:
            return select()
        except Exception as exc:  # noqa: BLE001 - 后端不许因为探测失败把服务带崩
            _log.warning("设备探测：%s 挑选失败（%s: %s）", source, type(exc).__name__, exc)
            return devices_module.SelectionOutcome(
                status=devices_module.PROBE_FAILED,
                endpoint=None,
                detail=f"挑选这一路设备时出错：{type(exc).__name__}: {exc}",
            )


def _default_source_factory(source: str, endpoint: devices_module.DeviceEndpoint) -> Any:
    """缺省的采集源工厂：**调用那一刻**再去 `create_capture_source()` / `create_loopback_source()`
    里取（与上面四个接缝同一手法：测试换掉那两个模块级函数就能挂上假源）。"""
    if source == "mic":
        return create_capture_source(source, endpoint)
    return create_loopback_source(endpoint)


def create_capture_source(source: str, endpoint: devices_module.DeviceEndpoint) -> Any:
    """造一路采集源（照 V1 的两条路：mic 走 `sounddevice`、loopback 走 `soundcard`）。

    这一层是**测试注入假源的接缝**：`meeting/capture/devices.py` 挑出端点之后，真实开设备
    的那一步就落在它身上。测试把那两个函数换成假件即可（服务启动那一次 `probe()` 用同一套
    手法，见 `tests/test_capture_probe.py` 的 `FakeSources.install()`）。
    """
    return sources_module.SounddeviceMicSource(endpoint)


def create_loopback_source(endpoint: devices_module.DeviceEndpoint) -> Any:
    """造 loopback 那一路的采集源（`soundcard` 的 recorder + 专用线程）。"""
    return sources_module.SoundcardLoopbackSource(endpoint)


__all__ = [
    "CAPTURE_MODES",
    "CaptureBackend",
    "CaptureUnavailable",
    "create_capture_source",
    "create_loopback_source",
]
