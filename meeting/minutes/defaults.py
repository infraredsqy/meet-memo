"""`minutes.*` 配置键缺省值的包内唯一定义点。

登记正本是 `meeting/config.py` 的 `DEFAULT_VALUES`；但 minutes 包**不许运行依赖
`meeting.config`**——`python -m meeting.minutes.chunk` / `.prompt` 这类手动子命令
不加载配置链（子命令入口的硬口径），因此四个缺省在这里落一份，包内其余模块一律
import 这一份，不再各存同值字面量。与正本的同值由 `tests/test_minutes_config_defaults.py`
逐格钉住。
"""

#: `minutes.chunk_lines` 的缺省（每块行数；取值域的上下界在 `chunk.MIN/MAX_CHUNK_LINES`）
DEFAULT_CHUNK_LINES = 100

#: `minutes.max_segments` 的缺省（每条 source 的段数上限；取值域在 `config.INT_LIMITS`）
DEFAULT_MAX_SEGMENTS = 8

#: `minutes.hook` 的缺省（启动分支：`auto` / `headless` / `web`）
DEFAULT_HOOK = "auto"

#: `minutes.timeout_seconds` 的缺省（四小时：R8 的「会后可以挂到第二天早上」）
DEFAULT_TIMEOUT_SECONDS = 14400.0

__all__ = [
    "DEFAULT_CHUNK_LINES",
    "DEFAULT_HOOK",
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_TIMEOUT_SECONDS",
]
