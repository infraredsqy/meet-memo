"""`minutes.*` 配置键缺省值的包内唯一定义点。

登记正本是 `meeting/config.py` 的 `DEFAULT_VALUES`；但 minutes 包**不许运行依赖
`meeting.config`**——`python -m meeting.minutes.chunk` / `.prompt` 这类手动子命令
不加载配置链，因此四个缺省在这里落一份，包内其余模块一律 import 这一份，不再各存
同值字面量。
"""

DEFAULT_CHUNK_LINES = 100

DEFAULT_MAX_SEGMENTS = 8

DEFAULT_HOOK = "auto"

DEFAULT_TIMEOUT_SECONDS = 14400.0

__all__ = [
    "DEFAULT_CHUNK_LINES",
    "DEFAULT_HOOK",
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_TIMEOUT_SECONDS",
]
