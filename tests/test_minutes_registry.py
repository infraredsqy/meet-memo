"""`meeting/backends/registry.py` 的解析口径用例（修复轮 M1）。

修复轮复审实测出的一件事实：`_target_for()` 里那条 `selector is None → None` 分支**触发不到**。
四条公开入口都先经过 `_selector()`，而它把配置里的 `None`（JSON `null` 归一出来的那一个）
归一成 `"auto"`；真正让 `minutes.hook = null` 生效的是 `resolve_minutes_hook()` 里那段
**提前返回缺席档**。这一段把那条因果钉在用例里，免得后来人再照着一句错话去理解这段代码。

本文件只碰解析层：不写盘、不起进程、不加载真配置（`config.get` 是 `monkeypatch` 过的）。
"""

from __future__ import annotations

from typing import Any

import pytest

from meeting import config as config_module
from meeting.backends import registry
from meeting.backends.registry import (
    DEFAULT_PATHS,
    resolve_capture_backend,
    resolve_minutes_hook,
    resolve_post_hook,
    resolve_transcriber_backend,
)

#: 四条公开入口（解析层的全部出口）
ENTRIES = {
    "capture.backend": resolve_capture_backend,
    "transcriber.backend": resolve_transcriber_backend,
    "post.hook": resolve_post_hook,
    "minutes.hook": resolve_minutes_hook,
}


def _config_value(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    """把配置层的某一格设成 `value`（只拦 `config.get`，别的一概走原路）。"""
    monkeypatch.setattr(config_module, "get", lambda key, default=None: value)


# ---------------------------------------------------------------------------
# 一、`_selector()` 交回的是字符串：配置里的 `None` 在它这里就归一成 `auto`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_none_from_the_config_layer_never_reaches_target_for(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置里是 JSON `null`（`None`）时，`_selector()` 交回 `"auto"`，`_target_for()` 拿不到 `None`。"""
    _config_value(monkeypatch, None)

    assert registry._selector(key, None) == "auto"


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_an_empty_string_from_the_config_layer_also_lands_on_auto(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空串与空白串同档（「没配」），也归一成 `auto`。"""
    _config_value(monkeypatch, "   ")

    assert registry._selector(key, None) == "auto"


def test_the_explicit_argument_wins_over_the_config_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式传进来的取值优先（`module:Class` 与 `dev` 都不会被配置层改写）。"""
    _config_value(monkeypatch, "auto")

    assert registry._selector("post.hook", "module:Class") == "module:Class"
    assert registry._selector("capture.backend", "dev") == "dev"


# ---------------------------------------------------------------------------
# 二、`minutes.hook = null` 真正生效的那条路：`resolve_minutes_hook()` 的提前返回
# ---------------------------------------------------------------------------


def test_the_config_level_null_is_answered_before_any_target_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**M1 的因果**：配置级 `null` → 落缺席档，且 `target` 是 `None`（一次 `DEFAULT_PATHS` 查询都没做）。

    这一档要是在 `_target_for()` 里靠「认出 `None`」来处理，`minutes.hook = null` 就得
    先穿过 `_selector()` 的归一（那里的答案是 `auto`）——那样「关掉自动跑」会静默失效。
    """
    _config_value(monkeypatch, None)
    looked_up: list[str] = []
    monkeypatch.setattr(
        registry, "DEFAULT_PATHS", _RecordingPaths(looked_up)
    )

    resolution = resolve_minutes_hook()

    assert resolution.selector == "null"
    assert resolution.target is None
    assert resolution.has_implementation is False
    assert resolution.skipped() is True
    assert type(resolution.implementation).__name__ == "_AbsentMinutesHook"
    assert "配置里是 null" in resolution.detail
    assert looked_up == [], "这一档不该去 DEFAULT_PATHS 上找一个键"


class _RecordingPaths(dict):
    """记下被查过哪些键的 `DEFAULT_PATHS` 替身。"""

    def __init__(self, seen: list[str]) -> None:
        super().__init__(DEFAULT_PATHS)
        self._seen = seen

    def __getitem__(self, key: str) -> str:
        self._seen.append(key)
        return super().__getitem__(key)


def test_the_default_config_resolves_the_minutes_hook_to_the_real_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认取值（`auto`）照旧解析到真实现：`minutes.hook = null` 那一档没有把默认路堵掉。"""
    _config_value(monkeypatch, "auto")

    resolution = resolve_minutes_hook()

    assert resolution.selector == "auto"
    assert resolution.target == DEFAULT_PATHS["minutes.hook"]
    assert resolution.has_implementation is True
