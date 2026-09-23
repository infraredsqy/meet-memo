"""会中总结的配置：端点四件套 + 预算三件套，从只读配置视图读出来。

七个键的键名与默认值登记在 `meeting/config.py` 的四张表里（默认值 / 取值域 / 环境变量映射 /
三分表）；本模块**只读**它们，并做三件事：

  1. **归一化**：`extra_body_json` 那段 JSON 文本翻成 dict（空串 ↔ `None`，两处同解）；
     数字键夹进合法范围（`deadline_seconds` 的下界、`max_attempts` 至少一次尝试）；
     `default_window` 收进四档取值（认不出的档落缺省档，装载时那一层已经拦过一次）。
  2. **判「缺哪几个键」**：`missing` 属性点名缺的那些**配置键全名** —— 动作层拿它说人话
     （「模型端点没配：缺 summary.base_url、summary.api_key」），也让 `run_summary()`
     在**发出任何请求之前**就停下来。
  3. **给缺省档**：`ReadOnlyConfig` / `Config` / 一份裸 dict / `None` 四种来源都收，
     取不到键时用 `meeting/config.py` 的默认值表，因此用例塞一个残缺对象也不会炸。

**端点四件套的取值**（`base_url` / `api_key` / `model` / `extra_body_json`）跟着
`docs/legacy-assets.md` 的「模型端点与 preset 配置」那一条走：语义照搬 V1 的 preset，
落到 V2 是四个**点号扁平键名**（`summary.*`），`extra_body` 那一段以**一个 JSON 字符串键**
承载（V2 配置层装载时明确拒收 dict 值，为这一个键放开那条硬约束不划算）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from meeting import config as config_module

#: 四档窗口取值（与 `meeting/summary/runner.py` 的 `WINDOW_CHOICES` 同一套）
WINDOW_CHOICES: tuple[str, ...] = ("5m", "15m", "30m", "full")

#: 四档窗口的秒数（`full` 是 `None`：整场取缓冲里的全部）
WINDOW_SECONDS: dict[str, float | None] = {
    "5m": 300.0,
    "15m": 900.0,
    "30m": 1800.0,
    "full": None,
}

#: 缺省窗口（设计章 2.3「缺省窗口 = 15 分钟」；与配置键的默认值同值）
DEFAULT_WINDOW = "15m"

#: 端点四件套 + 预算三件套的键名（`missing` 报的就是这一批里的名字）
BASE_URL_KEY = "summary.base_url"
API_KEY_KEY = "summary.api_key"
MODEL_KEY = "summary.model"
EXTRA_BODY_KEY = "summary.extra_body_json"
DEADLINE_KEY = "summary.deadline_seconds"
MAX_ATTEMPTS_KEY = "summary.max_attempts"
DEFAULT_WINDOW_KEY = "summary.default_window"

#: 七个键按这一序读、`missing` 也按这一序报（人读起来是「端点 → 预算 → 缺省档」）
SETTING_KEYS: tuple[str, ...] = (
    BASE_URL_KEY,
    API_KEY_KEY,
    MODEL_KEY,
    EXTRA_BODY_KEY,
    DEADLINE_KEY,
    MAX_ATTEMPTS_KEY,
    DEFAULT_WINDOW_KEY,
)

def _default(key: str) -> Any:
    """某个键的缺省值（平台相关键由 `config.default_for()` 解析）。"""
    try:
        return config_module.default_for(key)
    except KeyError:  # pragma: no cover - 键表由本模块与 config.py 同源维护
        return None


def _lookup(source: Any, key: str) -> Any:
    """从一个配置来源取键值：`get(key)` → `source[key]` → 缺省值表。

    四种来源（`ReadOnlyConfig` / `Config` / 裸 dict / `None`）都走这一条：
    前两种与 dict 都有 `get`，`None` 直接落缺省档。
    """
    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            value = getter(key)
        except Exception:  # pragma: no cover - 来源对象自己炸了就落缺省档
            value = None
        if value is not None:
            return value
        return _default(key)
    return _default(key)


def _as_text(value: Any) -> str:
    """值 → 字符串（`None` 与空白都归成空串：这一档就是「没配」）。"""
    if value is None:
        return ""
    return str(value).strip()


def _as_float(value: Any, fallback: float) -> float:
    """值 → 正浮点；不是数字、或小到没有意义时用兜底档。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number <= 0:
        return fallback
    return number


def _as_attempts(value: Any, fallback: int) -> int:
    """值 → 尝试次数上限（至少一次：一次都不试的话这套编排没有意义）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, number)


def parse_extra_body(text: Any) -> dict[str, Any] | None:
    """`summary.extra_body_json` 的文本 → dict；空串与 `None` 都交回 `None`。

    装载那一层（`config.validate_value()`）已经把非 JSON 对象的值拦下了，这里再兜一道：
    调用方手写的配置文件绕过 `load()` 时，一段读不出来的文本按「没配」处理，
    不让它把整次总结带崩。
    """
    raw = _as_text(text)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_window(value: Any) -> str:
    """窗口取值 → 四档之一（认不出的落缺省档）。"""
    text = _as_text(value)
    return text if text in WINDOW_CHOICES else DEFAULT_WINDOW


def window_seconds_of(window: str) -> float | None:
    """窗口档位 → 秒数（`full` 交回 `None`）。"""
    return WINDOW_SECONDS[normalize_window(window)]


@dataclass(frozen=True)
class SummarySettings:
    """会中总结那一次调用要的全部配置（端点四件套 + 预算三件套）。

    冻结：一次总结跑起来之后，它用的就是构造那一刻的这份快照 —— 中途改配置不影响
    在飞的这一次（与 V1 `_call_api()` 快照四个字段同一个立场）。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    extra_body: dict[str, Any] | None = field(default=None)
    deadline_seconds: float = 25.0
    max_attempts: int = 3
    default_window: str = DEFAULT_WINDOW

    @property
    def missing(self) -> tuple[str, ...]:
        """缺哪些键（**配置键全名**，按 `SETTING_KEYS` 的次序）。

        端点三件套缺一个都跑不起来（没有地址、没有密钥、没有模型名，请求发不出去）；
        `extra_body` 与窗口、预算三件套都有可用的缺省档，它们不在这一档里。

        判据是 `strip()` 之后还有没有内容：只有空白的那几个字段与「没配」同解 ——
        `from_config()` 已经把空白归一过一遍，这里再兜一道手搓实例的调用方。
        """
        gaps: list[str] = []
        if not self.base_url.strip():
            gaps.append(BASE_URL_KEY)
        if not self.api_key.strip():
            gaps.append(API_KEY_KEY)
        if not self.model.strip():
            gaps.append(MODEL_KEY)
        return tuple(gaps)

    @property
    def ready(self) -> bool:
        """端点配齐了没有（`missing` 为空的快捷读法）。"""
        return not self.missing

    @classmethod
    def from_config(cls, config: Any = None) -> "SummarySettings":
        """从一份只读配置视图读七个键，归一化成一份设置。

        `config` 可以是 `meeting.backends.base.ReadOnlyConfig`、`meeting.config.Config`、
        一份裸 dict，或者 `None`（走缺省值表）。
        """
        return cls(
            base_url=_as_text(_lookup(config, BASE_URL_KEY)),
            api_key=_as_text(_lookup(config, API_KEY_KEY)),
            model=_as_text(_lookup(config, MODEL_KEY)),
            extra_body=parse_extra_body(_lookup(config, EXTRA_BODY_KEY)),
            deadline_seconds=_as_float(
                _lookup(config, DEADLINE_KEY), float(_default(DEADLINE_KEY) or 25.0)
            ),
            max_attempts=_as_attempts(
                _lookup(config, MAX_ATTEMPTS_KEY), int(_default(MAX_ATTEMPTS_KEY) or 3)
            ),
            default_window=normalize_window(_lookup(config, DEFAULT_WINDOW_KEY)),
        )


__all__ = [
    "API_KEY_KEY",
    "BASE_URL_KEY",
    "DEADLINE_KEY",
    "DEFAULT_WINDOW",
    "DEFAULT_WINDOW_KEY",
    "EXTRA_BODY_KEY",
    "MAX_ATTEMPTS_KEY",
    "MODEL_KEY",
    "SETTING_KEYS",
    "SummarySettings",
    "WINDOW_CHOICES",
    "WINDOW_SECONDS",
    "normalize_window",
    "parse_extra_body",
    "window_seconds_of",
]
