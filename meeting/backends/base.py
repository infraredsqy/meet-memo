"""三个后端协议与数据结构 —— 下游往这儿插（phase1 §3.9，方案 §7.1 / §7.2 / §7.3）。

本模块**只放协议与数据结构**，实现全在下游自己的模块里。

约定导入路径（`auto` 按它们找）：

    meeting.backends.capture:CaptureBackend      # dca4539b 实现
    meeting.backends.transcribe:TranscriberBackend  # 9e5db89c 实现
    meeting.backends.post:PostProcessHook        # 65dd3e4c 实现

注入与构造规则（本节点定，下游照这个接，落地时不改本节点一行代码）：
  - `registry` 读 `config.capture.backend`：`auto`（默认）按上面那条约定路径找，
    也可以是 `null` / `dev` / `module:Class` 这样的显式值。
  - 取到的是**类**就**无参实例化**（后端构造函数不许有必填参数，运行期依赖一律走 `bind()`）；
    导入失败或找不到 → 回退 `NullCaptureBackend` 并写一条 warning。
  - `emit(fact)` / `line_sink(line)` **可以从任何线程调**：runtime 在 `bind()` 里包一层
    `loop.call_soon_threadsafe(...)`，后端不必知道事件循环在哪，也不许自己碰状态。
  - `LevelFrame.ts` **由后端给**：它是采集线程看到那一刻的时间，runtime 只拿它做窗口比较。

**导入顺序契约（C10，模型节点落地时仍然成立）**：先 `import transformers`，再
`import speechbrain`；反过来会让 speechbrain 抢在 transformers 之前完成自己的
依赖探测，出现难以定位的版本不匹配。本 Phase 不实现模型层，只把这条记在这里，
别等装了模型才想起来。

**门限只有一处**（§3.12 第 2 条）：静默秒数由 `alerts.py` 按 `alert.silence_dbfs` 逐帧累计
`LevelFrame` 的**帧时间戳**（不拿墙钟做差，慢机器上不虚报）；后端**不报**「静默了多少秒」，
协议里也没有这个字段 —— 后端只报原始事实，不判告警、不带阈值语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 取值域（逐字冻结）
# ---------------------------------------------------------------------------

#: 设备就绪三态（空闲态直接发给界面）
SOURCE_STATUS = ("ready", "probe_failed", "unprobed")

#: `SourceProbe.status ∈ SOURCE_STATUS` 的口径：
#:   ready        —— 能打开
#:   probe_failed —— 能打开但没信号
#:   unprobed     —— 未探测（`null` 替身如实报出的正是这一态）
SourceStatus = Literal["ready", "probe_failed", "unprobed"]

#: 两路（mic = 本地、loopback = 远端）
SOURCE_IDS = ("mic", "loopback")

#: 转写健康度
TRANSCRIPT_HEALTH = ("normal", "behind", "stopped")

#: 采集帧间隔的协议默认值（秒）。后端在 `bind()` 里记下自己的帧间隔；
#: 声明位是实例属性 `frame_interval`，读不到时回退这个常量。
DEFAULT_FRAME_INTERVAL_SECONDS = 0.5


# ---------------------------------------------------------------------------
# 事实（后端 → 服务，单向）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceProbe:
    """`probe()` 的一条结果：两路各一条。"""

    source: str
    status: str
    device_name: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class LevelFrame:
    """一条电平事实：**每 500ms 一帧、一根柱 = 2 秒**。

    `ts` 由后端给（采集线程看到那一刻的时间），runtime 只拿它做窗口比较。
    后端只报原始事实，不判告警。
    """

    source: str
    ts: float
    dbfs: float
    peak: float
    bar: float


@dataclass(frozen=True)
class SourceFacts:
    """一路的流存活事实。"""

    source: str
    stream_alive: bool


@dataclass(frozen=True)
class SourceLost:
    """一路的流中断事实。"""

    source: str
    reason: str = ""


@dataclass(frozen=True)
class TranscriptLine:
    """一行转写事实。"""

    ts: float
    source: str
    text: str
    kind: str = "line"  # line | gap


# ---------------------------------------------------------------------------
# 三个协议
# ---------------------------------------------------------------------------


@runtime_checkable
class CaptureBackend(Protocol):
    """采集后端（`dca4539b` 实现，约定路径 `meeting.backends.capture:CaptureBackend`）。"""

    #: 帧间隔（秒）：后端在 `bind()` 里记下自己的帧间隔，缺省 `0.5s`。
    frame_interval: float

    def bind(self, emit: Callable[[Any], None], *, config: Any) -> None:
        """runtime 在 `probe()` 之前调一次。

        `emit` 是**事实出口**（可从任何线程调），`config` 是只读配置视图。
        """

    def probe(self) -> list[SourceProbe]:
        """枚举并试开设备，**两路各一条**（`status ∈ ready / probe_failed / unprobed`）。"""

    def start(self, mode: str, session_dir: str) -> None:
        """按开场前定下的 `mode` 起采集，开始往自己的归档里写。

        两路各自独立，一路起不来别拖死另一路 —— 起不来的那路如实抛错，
        runtime 把 `capture_unavailable` 与原因转给界面。
        """

    def finalize(self) -> None:
        """收尾的落盘（可抛错，runtime 如实记账）。"""

    def transcode(self) -> None:
        """收尾的转码（可抛错，runtime 如实记账）。"""

    def stop(self) -> None:
        """停采集。"""


@runtime_checkable
class TranscriberBackend(Protocol):
    """转写后端（`9e5db89c` 实现，约定路径 `meeting.backends.transcribe:TranscriberBackend`）。

    方向只有一条：**runtime 把 sink 交给后端，后端往里送**；后端不许反过来读 runtime 的状态。
    """

    def bind(self, line_sink: Callable[[TranscriptLine], None], *, config: Any) -> None:
        """runtime 在 `start()` 之前调一次。

        `line_sink` 由 runtime 提供、**可从任何线程调**（内部走 `call_soon_threadsafe`），
        后端每出一行就调它一次。
        """

    def start(self, mode: str, session_dir: str) -> None:
        """起转写。起步失败**不阻塞录音**（转写坏了可以重跑，R1 的立场）。"""

    def stop(self) -> None:
        """停转写。"""

    def health(self) -> str:
        """`normal` / `behind` / `stopped`（直接进 `transcript.health`）。"""

    def backlog_depth(self) -> int:
        """积压段数（供自适应预算与提示级；数值本身只进 log 与 `/debug`）。"""

    def segment_timeout(self) -> float:
        """单段超时（秒，供自适应预算与提示级）。"""


@runtime_checkable
class PostProcessHook(Protocol):
    """会后流程钩子（`65dd3e4c` 实现，约定路径 `meeting.backends.post:PostProcessHook`）。"""

    def launch(self, session_dir: str) -> None:
        """收尾第 4 步调它，**返回即算启动成功**（不等跑完）。缺席 → 该步 `skipped`。"""


# ---------------------------------------------------------------------------
# 只读配置视图（`bind()` 的 `config` 参数）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadOnlyConfig:
    """交给后端的只读配置视图：`get(key)` 点号路径取值，`as_dict()` 拿整份生效值。"""

    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


__all__ = [
    "DEFAULT_FRAME_INTERVAL_SECONDS",
    "SOURCE_IDS",
    "SOURCE_STATUS",
    "TRANSCRIPT_HEALTH",
    "CaptureBackend",
    "LevelFrame",
    "PostProcessHook",
    "ReadOnlyConfig",
    "SourceFacts",
    "SourceLost",
    "SourceProbe",
    "SourceStatus",
    "TranscriptLine",
    "TranscriberBackend",
]
