"""minutes 层配置缺省值的对账用例（只做 import 级比对，不读盘、不起服务）。

`meeting.minutes` 各模块把 `minutes.*` 配置键的缺省值以包内常量**另存一份**：`chunk.py`
的每块行数、`contract.py` 与 `prompt.py` 各一份的分段上限、`runner.py` 的 hook 与墙钟
超时、`dsh_web.py` 的 DSH web 地址。这份用例把它们逐值钉在 `config.DEFAULT_VALUES` 的
同一格上，哪一份走样当场报红；chunk 的 MIN / MAX 两档与 `config.INT_LIMITS` 的边界格
对账。纯 import 比对，**快档**。
"""

from __future__ import annotations

from meeting import config
from meeting.minutes import chunk, contract, dsh_web, prompt, runner  # noqa: F401


def test_chunk_lines_default_and_bounds_match_config() -> None:
    """每块行数：缺省与上下界三格都跟 config 同格同值（取值域一条口径）。"""
    assert chunk.DEFAULT_CHUNK_LINES == config.DEFAULT_VALUES["minutes.chunk_lines"]
    assert chunk.MIN_CHUNK_LINES == config.INT_LIMITS["minutes.chunk_lines"][0]
    assert chunk.MAX_CHUNK_LINES == config.INT_LIMITS["minutes.chunk_lines"][1]


def test_max_segments_default_matches_config() -> None:
    """分段上限：contract 与 prompt 两份副本同格同值。

    模块自己没有 MIN/MAX 常量（越界在 `render_prompt()` 的 ≥1 与 config 取值域把关），
    这里只断 `min < default < max` 的形状（边界格在 `config.INT_LIMITS`）。
    """
    default = config.DEFAULT_VALUES["minutes.max_segments"]
    assert contract.DEFAULT_MAX_SEGMENTS == default
    assert prompt.DEFAULT_MAX_SEGMENTS == default
    low, high = config.INT_LIMITS["minutes.max_segments"]
    assert low < default < high


def test_hook_default_matches_config() -> None:
    """`minutes.hook` 的缺省：runner 的副本同格同值。"""
    assert runner.DEFAULT_HOOK == config.DEFAULT_VALUES["minutes.hook"]


def test_timeout_seconds_default_matches_config() -> None:
    """`minutes.timeout_seconds` 的缺省：`DEFAULT_VALUES` 里是整数格 14400，runner 侧是
    浮点预算 14400.0 —— 数值同格同值。"""
    assert runner.DEFAULT_TIMEOUT_SECONDS == config.DEFAULT_VALUES["minutes.timeout_seconds"]


def test_dsh_url_default_matches_config() -> None:
    """`minutes.dsh_url` 的缺省：dsh_web 的副本同格同值。"""
    assert dsh_web.DEFAULT_DSH_URL == config.DEFAULT_VALUES["minutes.dsh_url"]
