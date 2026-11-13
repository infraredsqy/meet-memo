"""后端解析的唯一入口（设计口径）。

四个解析函数的**名字在本项目正式定死**（跨层契约冻结项，设计照这四个名字调用）：

  - `resolve_capture_backend()` —— `capture.backend`（约定路径
    `meeting.backends.capture:CaptureBackend`）；
  - `resolve_transcriber_backend()` —— `transcriber.backend`（约定路径
    `meeting.backends.transcribe:TranscriberBackend`）；
  - `resolve_post_hook()` —— `post.hook`（约定路径
    `meeting.backends.post:PostProcessHook`）；
  - `resolve_minutes_hook()` —— `minutes.hook`（约定路径
    `meeting.backends.minutes:MinutesHook`； 设计加的第四个，形状与前三个同）。

取值口径照 `config.py` / `base.py` 里已定死的那一套，**不改口径**：
`auto`（默认，按约定路径找）/ `null` / `dev` / `module:Class` 显式写法；取到的是类就
**无参实例化**（后端构造函数不许有必填参数，运行期依赖一律走 `bind()`）；导入失败、
找不到、不可调用、无参实例化失败 → **回退替身 + 一条 warning**。

`BackendResolution` 分开回答两个问题（**收尾记录的唯一依据**）：

  - `has_implementation`（`bool`）—— 这个构建里**有没有**可执行实现：`true` = 解析到了实现
    （含采集替身的 `null` / `dev` 那一类）；`false` = **缺席**（转写后端缺席、会后钩子缺席、
    显式取值解析失败）。喂给 `skipped` 判定。
  - `run_ok`（`null` / `true` / `false`）—— 那个实现**跑起来成不成**：被解析到之前是 `null`；
    调用走完是 `true`；调用过程中抛异常或超时是 `false`。喂给 `failed` 判定。

**判定规则（与设计的判据逐字一致）**：`has_implementation == false` → 该步记 `skipped`；
`has_implementation == true` 且 `run_ok == false` → 该步记 `failed`。
**不许用「调用时抛异常」冒充缺席**——`has_implementation` 只由解析结果回答，
调用阶段的异常只许写进 `run_ok`。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

from meeting import config as config_module
from meeting.backends.null import NullCaptureBackend, NullTranscriberBackend

_log = logging.getLogger("meeting.backends.registry")

#: 三条约定路径（`auto` 按它们找）
DEFAULT_PATHS: dict[str, str] = {
    "capture.backend": "meeting.backends.capture:CaptureBackend",
    "transcriber.backend": "meeting.backends.transcribe:TranscriberBackend",
    "post.hook": "meeting.backends.post:PostProcessHook",
    # 会后纪要那一格（设计）：`meeting post` 的离线尾巴与「直接出纪要」
    # 按钮经解析到的实现拉起，`auto` 按这条路径找
    "minutes.hook": "meeting.backends.minutes:MinutesHook",
}

#: 显式取值 `dev` 指向的采集替身（只给开发与测试用，非产品路径）
DEV_CAPTURE_PATH = "meeting.backends.dev:DevCaptureBackend"

#: 显式取值 `dev` 指向的**转写**替身（设计口径）：
#: 同一条取值 `dev` 在两个后端键上按 **key 分流**——`capture.backend = dev` 落
#: `DEV_CAPTURE_PATH`、`transcriber.backend = dev` 落这一条。分流只做这一件事，
#: 别的地方（`auto` / `null` / `module:Class`）一个字不动。
DEV_TRANSCRIBER_PATH = "meeting.backends.dev:DevTranscriberBackend"


@dataclass
class BackendResolution:
    """一次解析的结果：实现本身 + 两个冻结标记。"""

    key: str
    selector: str
    target: str | None
    implementation: Any
    has_implementation: bool
    run_ok: bool | None = None
    detail: str = ""

    def mark_run(self, ok: bool) -> None:
        """调用走完之后落 `run_ok`（调用抛异常 / 超时记 `false`）。"""
        self.run_ok = bool(ok)

    def skipped(self) -> bool:
        """该步该不该记 `skipped`（**只看解析结论**）。"""
        return not self.has_implementation

    def failed(self) -> bool:
        """该步该不该记 `failed`（有实现、但跑不成）。"""
        return self.has_implementation and self.run_ok is False


def _selector(key: str, explicit: str | None) -> str:
    """这一格的取值字符串（**永远交回一个字符串**，交不出 `None`）。

    显式传进来的那一个优先；没传就看配置：配置里不是非空字符串（JSON `null` 归一出来的
    `None`、空串、别的类型）一律当 `auto`。`minutes.hook` 那一格是例外——配置级 `null`
    在 `resolve_minutes_hook()` 里**提前返回缺席档**，因此走不到这里（见那个函数）。
    """
    if explicit is not None:
        return explicit
    raw = config_module.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return "auto"
    return raw.strip()


def _target_for(key: str, selector: str) -> str | None:
    """取值字符串 → 要导入的 `module:Class`（`null` 交回 `None`，缺席那一档不走这儿）。

    `selector` 只可能是 `_selector()` 交回的那一个字符串，**不会是 `None`**：配置级
    `null` 既会被 `_selector()` 归一成 `auto`，也会被 `resolve_minutes_hook()` 提前拦下。
    """
    if selector == "auto":
        return DEFAULT_PATHS[key]
    if selector == "null":
        return None
    if selector == "dev":
        # `dev` 一档**按 key 分流**：采集替身与转写替身各归各的（设计口径）
        return DEV_TRANSCRIBER_PATH if key == "transcriber.backend" else DEV_CAPTURE_PATH
    return selector


def _import_class(target: str) -> Any:
    """按 `module:Class` 形状解析类；形状不对 / 导入失败 / 没有这个属性一律抛。"""
    if ":" not in target:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise LookupError(f"{module_name} 里没有 {attr}")
    return getattr(module, attr)


def _resolve(
    key: str,
    *,
    null_class: type,
    explicit: str | None = None,
    absent_is_missing: bool = True,
) -> BackendResolution:
    """解析一个后端键；任何失败都**回退替身 + 一条 warning**（服务照常起）。

    `absent_is_missing`：替身算不算「在场」。采集侧的 `null` / `dev` 是**解析得到的实现**
    （`has_implementation = true`，收尾时由它们的归档能力决定 `skipped`）；转写与会后钩子
    没有替身可用，回退即「缺席」（`has_implementation = false`）。
    """
    selector = _selector(key, explicit)
    target = _target_for(key, selector)

    if selector == "null":
        return BackendResolution(
            key=key,
            selector=selector,
            target=None,
            implementation=null_class(),
            has_implementation=bool(absent_is_missing),
            detail="显式取值 null（替身）",
)

    if target is None:  # pragma: no cover - 只有 null 走到这里，上面已返回
        return BackendResolution(
            key=key, selector=selector, target=None, implementation=None,
            has_implementation=False, detail="没有可解析的目标",
)

    try:
        cls = _import_class(target)
    except Exception as exc:
        _log.warning(
            "%s = %r 解析失败（%s: %s），回退替身", key, selector, type(exc).__name__, exc
)
        return BackendResolution(
            key=key,
            selector=selector,
            target=target,
            implementation=null_class(),
            has_implementation=bool(absent_is_missing),
            detail=f"解析失败：{type(exc).__name__}: {exc}",
)

    if not callable(cls):
        _log.warning("%s = %r 解析出的对象不可调用，回退替身", key, selector)
        return BackendResolution(
            key=key,
            selector=selector,
            target=target,
            implementation=null_class(),
            has_implementation=bool(absent_is_missing),
            detail="解析出的对象不可调用",
)

    try:
        instance = cls()  # 类无参实例化：后端构造函数不许有必填参数
    except Exception as exc:
        _log.warning(
            "%s = %r 无参实例化失败（%s: %s），回退替身",
            key, selector, type(exc).__name__, exc,
)
        return BackendResolution(
            key=key,
            selector=selector,
            target=target,
            implementation=null_class(),
            has_implementation=bool(absent_is_missing),
            detail=f"无参实例化失败：{type(exc).__name__}: {exc}",
)

    return BackendResolution(
        key=key,
        selector=selector,
        target=target,
        implementation=instance,
        has_implementation=True,
        detail=f"解析到 {target}",
)


def resolve_capture_backend(explicit: str | None = None) -> BackendResolution:
    """`capture.backend` → 采集实现（`auto` / `null` / `dev` / `module:Class`）。

    `null` 与 `dev` 都是**解析得到的实现**（`has_implementation = true`）；收尾第 1、2 步
    按实现有没有两路归档能力（`finalize` / `transcode`）单独判 `skipped`。
    """
    return _resolve(
        "capture.backend", null_class=NullCaptureBackend, explicit=explicit,
        absent_is_missing=True,
)


def resolve_transcriber_backend(explicit: str | None = None) -> BackendResolution:
    """`transcriber.backend` → 转写实现；**缺席时返回缺席态**（`has_implementation = false`）。

    缺席这一档的缺省：`health` 恒 `normal`、`backlog = 0`、`segment_timeout = 20.0`
    （由 `NullTranscriberBackend` 给出，与设计的缺省一致）。
    """
    return _resolve(
        "transcriber.backend", null_class=NullTranscriberBackend, explicit=explicit,
        absent_is_missing=False,
)


def resolve_post_hook(explicit: str | None = None) -> BackendResolution:
    """`post.hook` → 会后钩子；**缺席 → 收尾第 4 步 `skipped`**。"""
    return _resolve(
        "post.hook", null_class=_AbsentPostHook, explicit=explicit, absent_is_missing=False
)


def resolve_minutes_hook(explicit: str | None = None) -> BackendResolution:
    """`minutes.hook` → 会后纪要钩子；**缺席（`null`）→「直接出纪要」按钮当场 404
    `action_unavailable`，`meeting post` 补纪要那一路安静跳过**。

    与 `resolve_post_hook()` 同一个形状与同一条口径：显式 `null`（字符串）与**配置里写成
    JSON `null`**（校验层归一成 `None`）都落缺席档，`has_implementation = false`。
    `has_implementation` 只由解析结果回答，调用阶段的异常写进 `run_ok`。

    **配置级 `null` 靠下面这段提前返回生效**（不是靠 `_target_for()` 认出 `None`）：
    配置里写 JSON `null` 时 `_selector()` 会把它归一成 `auto`，这里在归一之前先看一眼原始值，
    因此「不接这一层」这个配置一个字都不会落空。显式传进来的取值照旧走 `_resolve()`。
    """
    if explicit is None:
        raw = config_module.get("minutes.hook")
        if raw is None:
            # `minutes.hook = null`：配置里写的是 JSON 的 null（不是字符串 "null"）。
            # 这一档要落缺席类，不能退回 `auto`（否则「不接这一层」这个配置一个字都不生效）。
            return BackendResolution(
                key="minutes.hook",
                selector="null",
                target=None,
                implementation=_AbsentMinutesHook(),
                has_implementation=False,
                detail="配置里是 null（缺席：「直接出纪要」按钮 404，meeting post 补纪要安静跳过）",
)
    return _resolve(
        "minutes.hook",
        null_class=_AbsentMinutesHook,
        explicit=explicit,
        absent_is_missing=False,
)


class _AbsentPostHook:
    """会后钩子缺席时的占位：`launch()` 抛错（**它不会被执行**——缺席记 `skipped`）。"""

    def launch(self, session_dir: str) -> None:  # pragma: no cover - 缺席档不调用
        raise NotImplementedError("会后钩子缺席（缺席记 skipped，不记 failed）")


class _AbsentMinutesHook:
    """纪要钩子缺席时的占位（`minutes.hook = null`）：`launch()` 抛错，**不会被调用**。

    离线那条链在调它之前就问过 `has_implementation`：缺席时安静跳过，一个字节都不写。
    """

    def launch(self, session_dir: str) -> None:  # pragma: no cover - 缺席档不调用
        raise NotImplementedError("纪要钩子缺席（minutes.hook = null：按钮 404，meeting post 补纪要安静跳过）")


def capabilities_of(implementation: Any) -> dict[str, bool]:
    """一个采集实现声明的能力（收尾记录的输入）。

    判据是实现自己声明的能力位 `writes_real_audio`（类属性；真采集后端为 `True`，
    `null` / `dev` 替身显式为 `False`，没声明的按「没这能力」算）：`finalize` 还要求
    方法真的在场（runtime 要调它），`transcode` 只看声明位——转码独立工序吃的是实现
    写下的 PCM 归档文件，不走实现的方法。替身两项为假 → 收尾第 1 步记 `skipped`、
    flush 落定后的转码独立工序不自动起（判据都是采集侧，见 `docs/state.md` 第 1 章）。
    """
    writes_audio = bool(getattr(implementation, "writes_real_audio", False))
    return {
        "finalize": writes_audio and callable(getattr(implementation, "finalize", None)),
        "transcode": writes_audio,
        "dev_inject": callable(getattr(implementation, "inject_fault", None)),
    }


__all__ = [
    "DEV_CAPTURE_PATH",
    "DEV_TRANSCRIBER_PATH",
    "DEFAULT_PATHS",
    "BackendResolution",
    "capabilities_of",
    "resolve_capture_backend",
    "resolve_minutes_hook",
    "resolve_post_hook",
    "resolve_transcriber_backend",
]
