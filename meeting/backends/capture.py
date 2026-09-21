"""采集后端 —— 设备层与选择面（`dca4539b` phase1）。

约定导入路径就是这个模块的 `CaptureBackend`（`meeting/backends/registry.py` 的
`DEFAULT_PATHS["capture.backend"]`）：`capture.backend` 取默认值 `auto` 时解析到的就是它。
构造函数**不许有必填参数**（registry 无参实例化），运行期依赖一律走 `bind()`。

这一段交出去的是**设备层**：

  - `probe()` 真的去问系统有哪些声卡端点、真的以 16k 试开一遍并读回样本，把结果如实报成
    两路 `ready` / `probe_failed`（三档取值域见 `meeting/backends/base.py` 的 `SOURCE_STATUS`）；
  - 「录哪一路用哪个设备」由 `devices.mic` / `devices.loopback` 两个配置键定，挑选策略在
    `meeting/capture/devices.py`（纯逻辑），真实开设备在 `meeting/capture/sources.py`；
  - 用户在录音前改了配置打 `reprobe`，runtime 再调一次 `probe()` 就重新探测（`probe()` 幂等）。

**采集引擎不在这一段**：`start()` 如实抛 `CaptureUnavailable`（`message` 写明引擎在下一
执行单元接入），`finalize()` / `transcode()` / `stop()` 是安全的空实现。因此这两个收尾
能力此刻都还没有实现体，`registry.capabilities_of()` 会把它们记成 `True`（那是它的口径：
按实现在场与否回答，不看这一段做没做引擎）—— 收尾四步真实记账归下一段。

**没有 `inject_fault`**（有意不加）：`dev_inject` 动作的可用性就是靠这个属性判的，给真后端
加上它会把开发替身的非产品动作在真后端上打开。`capabilities_of()` 因此对这一项报 `False`，
`dev_inject` 在真后端上照旧回 `action_unavailable`(404)。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from meeting.backends.base import DEFAULT_FRAME_INTERVAL_SECONDS, SourceProbe
from meeting.capture import devices as devices_module
from meeting.capture import sources as sources_module

_log = logging.getLogger("meeting.backends.capture")

#: 两路的固定次序（与 `meeting/backends/base.py` 的 `SOURCE_IDS` 一致）
SOURCE_ORDER: tuple[str, ...] = ("mic", "loopback")

#: `start()` 的拒绝文案（这一段不做采集引擎，如实说清而不是假装能录）
CAPTURE_ENGINE_PENDING_REASON = "采集引擎在下一执行单元接入：这一段只交设备层，不会真的录"

try:  # 与 `meeting.backends.null` 同一档异常（`code` 供界面 / 动作端点转成错误信封）
    from meeting.backends.null import CaptureUnavailable
except ImportError:  # pragma: no cover - null.py 一直在，这里只是不让导入路径成为单点
    CaptureUnavailable = RuntimeError  # type: ignore[assignment,misc]


class CaptureBackend:
    """真采集后端（这一段只有设备层）：`probe()` 真探测，`start()` 如实拒绝。"""

    #: 帧间隔的声明位（协议默认 0.5s；采集引擎在下一执行单元接上之后仍是这个节奏）
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
    ) -> None:
        """四个可注入的接缝（缺省都指向 `meeting/capture/sources.py` 的**当下**那一份）。

        缺省写法用 lambda 把查找推到调用那一刻（构造时不抓函数对象）：测试里
        `monkeypatch.setattr(sources_module, "list_input_endpoints", fake)` 因此照样生效
        （`docs/legacy-assets.md` 的坑 C9：`from ... import f` 绑的是函数对象，改不动）。
        """
        self._list_inputs = list_inputs or (lambda: sources_module.list_input_endpoints())
        self._list_renders = list_renders or (lambda: sources_module.list_render_endpoints())
        self._test_open_input = test_open_input or (
            lambda endpoint: sources_module.test_open_input(endpoint)
        )
        self._test_open_loopback = test_open_loopback or (
            lambda endpoint: sources_module.test_open_loopback(endpoint)
        )
        self._emit: Callable[[Any], None] | None = None
        self._config: Any = None
        #: 最近一次 `probe()` 的两条结论（`reprobe` 与 `/debug` 看这份）
        self.last_probes: list[SourceProbe] = []

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
        """起采集 —— 这一段不做采集引擎，**如实拒绝**，不假装能录。"""
        raise CaptureUnavailable(CAPTURE_ENGINE_PENDING_REASON)

    def finalize(self) -> None:
        """安全空实现（收尾落盘归采集引擎那一段）。"""

    def transcode(self) -> None:
        """安全空实现（转码归采集引擎那一段）。"""

    def stop(self) -> None:
        """安全空实现（没有引擎可停）。"""

    # -- 内部 -------------------------------------------------------------

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


__all__ = [
    "CAPTURE_ENGINE_PENDING_REASON",
    "SOURCE_ORDER",
    "CaptureBackend",
    "CaptureUnavailable",
]
