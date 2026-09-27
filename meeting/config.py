"""配置层（phase1 §3.11，B11 的正确边界）。

位置：`~/.config/meeting/config.json`
  - `config.json` 是**点号扁平键名的单层映射**（例如 `{"web.port": 8765}`），
    嵌套形状不合法。
  - 不迁移 V1 的 `~/.config/meeting_notes/`，也不做任何自动改写。

优先级：**环境变量 > `config.json` > 代码默认值**（沿用 V1 契约，逐字照搬 B11）。
环境变量那一层是 `ENV_KEYS` 里的五个加 `MEETING_SUMMARY_API_KEY`（会中总结的密钥，
`1987cb20` phase1 定名）。

热读范围：**`asr.*` 那五个键每段重新读，`alarm.sound` 写盘那一刻就生效** —— 这六个合起来
就是 `HOT_KEYS`（出声开关要在勾选那一刻起作用）；
其余键（`web.*` / `output.root` / `log.*` / `alert.*` / `heartbeat.*` /
`capture.*` / `transcriber.*` / `post.*` / `devices.*`）都是**启动时读一次，改了要重启**。
**环境变量一律在 import / 启动时求值**，改了不会热生效（B11 的坑）。

两个前缀说的两件事：
  - `alert.*` 是**判定阈值**（静默多久算告警、多久算恢复、多久内不重复出声）；
  - `alarm.*` 只管出声（`alarm.sound` 就是那个持久化开关）。

两个「心跳」不许串线（§3.11 / §6.2 第 4 条）：
  - `heartbeat.interval_seconds` 是**界面**发心跳的周期（默认 10 秒、可配 1–60 秒），管失联横幅；
  - 服务侧的采集帧心跳窗口是 `max(2.0s, 3 × frame_interval)` = 2.0 秒，管 `recording_alive`。

`config.json` 的两种坏情形：
  - 文件**不存在** → 用默认值照常起（服务在缺失配置下的正常运行）；
  - 文件**存在但读不出来或不是合法 JSON** → 抛 `ConfigCorruptError`，
    由 `cli.main()` 映射成 `EXIT_STARTUP_UNMET`（文件级损坏，连键都解析不出）；
  - 文件**能解析但某个键的值非法** → 抛 `ConfigValueError`，
    由 `cli.main()` 映射成 `EXIT_REJECTED`（值级非法），报错含键名 / 收到的值 / 合法范围，
    并且**在写生效值清单之前**拦下。
两种坏情形都不许拿默认值悄悄糊过去。

**`web.host` 这类键永远不许出现**：只监听 `127.0.0.1` 是安全边界，不能被配置项改掉。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 异常（本模块不定义退出码常量：由 cli.main() 映射成 §3.4 的常量后 sys.exit）
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """配置层错误基类。"""


class ConfigCorruptError(ConfigError):
    """`config.json` 读不出来或不是合法 JSON（文件级损坏）。

    对应退出码档位 `EXIT_STARTUP_UNMET`。`path` 与 `detail` 供终端与 bootstrap 日志
    写清「文件路径与出错的行」。
    """

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"配置文件损坏：{path}（{detail}）")


class ConfigValueError(ConfigError):
    """某个键的值非法（类型 / 范围 / 键间规则）。

    对应退出码档位 `EXIT_REJECTED`。`key` / `value` / `legal` 三样齐，
    供终端与 bootstrap 日志写清「键名、收到的值、合法范围」。
    """

    def __init__(self, key: str, value: Any, legal: str, detail: str | None = None) -> None:
        self.key = key
        self.value = value
        self.legal = legal
        self.detail = detail
        message = f"配置值非法：{key} = {value!r}（合法范围：{legal}）"
        if detail:
            message = f"{message}：{detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 默认值表（§3.11，本 Phase 定死；键名与默认值都是跨 Phase 冻结项）
# ---------------------------------------------------------------------------

CONFIG_DIR_NAME = (".config", "meeting")
CONFIG_FILE_NAME = "config.json"
SERVICE_LOG_FILE_NAME = "meeting.log"

#: 热读键（写盘那一刻就生效，不必重启）
#:
#: 顺序有意把 `alarm.sound` 排在 `asr.*` 前面：`ASR_SETTING_KEYS` 是从这张表里**按
#: `asr.` 前缀筛**出来的（见那一格），排在前面不参与快照 `settings.asr` 的子键次序。
HOT_KEYS: tuple[str, ...] = (
    "alarm.sound",
    "asr.max_segment_seconds",
    "asr.timeout_seconds",
    "asr.max_new_tokens",
    "asr.repeat_max_unit",
    "asr.repeat_min_coverage",
)

#: `asr.*` 五个默认值沿用 `docs/legacy-assets.md` 的 B1（V1 `mnotes_core/config.py` 实测值），
#: 待 `9e5db89c` 落地时复核；**复核只许改值不许改口径**。
DEFAULT_VALUES: dict[str, Any] = {
    "web.port": 8765,
    "web.dist_dir": "web/dist",
    "output.root": None,  # 平台相关，见 default_output_root()
    "log.level": "INFO",
    "log.dir": None,  # 家目录相关，见 default_log_dir()
    "log.max_bytes": 5 * 1024 * 1024,  # 5 MiB
    "log.backup_count": 3,
    "alert.silence_seconds": 180,
    "alert.recover_seconds": 10,
    "alert.alarm_suppress_seconds": 60,
    "alert.silence_dbfs": -60,
    "alarm.sound": True,
    "heartbeat.interval_seconds": 10,
    "capture.backend": "auto",
    "transcriber.backend": "auto",
    "post.hook": "auto",
    # 会后离线转写这一层的三个开关（`7c6952e5` 定名，与 `capture.backend` 同形：
    # 「接口留在这里，值归选型节点」）。缺省值取**本机实测跑通**的那一档：
    # 模型走 MOSSTD 的 HF 仓库 id（本机已有本地快照），档位走 bf16。
    "post.engine": "auto",
    "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
    "post.dtype": "bfloat16",
    # 每块音频的长度上限（秒），`0` = 显式关掉分块、整段一次跑。
    #
    # **本键的默认值取 450**，这是对 spec「退回显式分块（默认关闭）」的一处有意偏离：
    # 那句话写在显卡天花板被测出来之前。spec 的「坑与注意」里写着「3060 6GB 这个量级
    # 要提前量清楚显存」，本机实测（16 GB 卡，`phase3` 报告第二节与 10.1）是
    # 600 s → 11.6 GiB 峰值（还在卡内）、900 s → 22.9 GiB（已越界换页，墙钟劣化 5–12 倍）、
    # 1500 s → `OutOfMemoryError`；而本次验收那条 99 分 03 秒的素材是 5943 s，
    # 整段一次跑必然死在 OOM 上。按「默认关闭」发出去，等于每一场真实会议都失败，
    # R9 要的那条权威转写一条都出不来。450 s 是实测的安全档（峰值 7.5 GiB、0.10–0.15×），
    # 因此默认取它；短于 450 s 的音频仍然是「整段一次性跑」——块数为 1 时本模块不切块。
    "post.chunk_seconds": 450,
    # 两路各自用哪个设备（`dca4539b` 定名）：`None` = 按默认策略挑
    # （mic 挡掉抓取型虚拟端点后按 host API 排序试开；loopback 优先 `CABLE Input` 且不含 16ch）
    "devices.mic": None,
    "devices.loopback": None,
    "asr.max_segment_seconds": 10,
    "asr.timeout_seconds": 20,
    "asr.max_new_tokens": 400,
    "asr.repeat_max_unit": 40,
    "asr.repeat_min_coverage": 0.5,
    # 会后纪要生成这一层的四个开关（`41465462` phase2 定名，与 `post.*` 同形：
    # 「接口留在这里，值归选型节点」）：
    #   - `minutes.hook`：会后是否自动跑纪要生成。`auto` = 跑（跟离线那条链的尾巴走）；
    #     `null` = 不自动跑（手动 `meeting post` 那条路另算）；与 `post.hook` 同一套取值口径。
    #   - `minutes.chunk_lines`：每块行数。默认 100 与 V1 那套切块口径一致（`00_index.md` 的
    #     `chunk_size`），取值域与 `chunk.MIN_CHUNK_LINES` / `MAX_CHUNK_LINES` 逐字相同。
    #   - `minutes.max_segments`：每条 source 的分段上限（契约校验与提示词都用它）；
    #     取值域与 `contract.DEFAULT_MAX_SEGMENTS` 那一档一致。
    #   - `minutes.timeout_seconds`：一次纪要生成的墙钟上限。默认四小时 —— R8 的「会后时间
    #     宽裕换质量」意味着这一趟可以挂到第二天早上，但它仍然要有一个上限。
    #   - `minutes.dsh_url`：DSH **web 服务端**的地址（不是 `dsh` 可执行文件那一头）。
    #     它在跑时这一场走「交付」那条路：经 `/api` 在**场次目录对应的那个工作区**里建会话、
    #     送入提示词全文，终态记 `delivered`（投递 accepted 即算交付完成）；没在跑才落回
    #     本进程自己起的 headless 子进程（等它退出并按四条判据校验产物）。默认值就是
    #     `dsh web` 的默认监听地址。
    "minutes.hook": "auto",
    "minutes.chunk_lines": 100,
    "minutes.max_segments": 8,
    "minutes.timeout_seconds": 14400,
    "minutes.dsh_url": "http://127.0.0.1:3080",
    # 会中总结这一层的七个键（`1987cb20` phase1 定名，与 `post.*` / `minutes.*` 同形：
    # 「接口留在这里，值归选型节点」）。四件套 + 预算三件套：
    #   - `summary.base_url` / `summary.model`：端点地址与模型名。默认值取本机验收时
    #     实测跑通的那一对（DeepSeek 的 OpenAI 兼容端点与 `deepseek-chat`），
    #     换一家改这两个键即可 —— 这一层说的是 OpenAI 兼容协议，与厂商无关。
    #   - `summary.api_key`：**可空键**（进 `NULLABLE_KEYS`）。默认 `None` = 没配，
    #     `SummarySettings.missing` 会点名它，动作层据此说「缺哪几个键」。
    #     密钥不进仓库：配置文件里写它、或者用环境变量 `MEETING_SUMMARY_API_KEY` 压住它。
    #   - `summary.extra_body_json`：厂商特有字段（V1 preset 的 `extra_body`）以**一个
    #     JSON 字符串键**承载，空串 = 不并。V2 配置层是点号扁平键名的单层映射、
    #     装载时明确拒收 dict 值，为这一个键放开那条硬约束不值得。
    #   - `summary.deadline_seconds`：一次总结的**墙钟 deadline**（默认 25 秒）。
    #     它换掉的是 V1「3 次 × 60 秒硬编码超时」那套最坏 180 秒的账。
    #   - `summary.max_attempts`：尝试次数上限（默认 3）。它是**上限**：真正的刹车是 deadline。
    #   - `summary.default_window`：`args.window` 省略时用的那一档（四档取值，默认 `15m`）。
    "summary.base_url": "https://api.deepseek.com/v1",
    "summary.api_key": None,
    "summary.model": "deepseek-chat",
    "summary.extra_body_json": "",
    "summary.deadline_seconds": 25.0,
    "summary.max_attempts": 3,
    "summary.default_window": "15m",
}

#: 后端选取键的取值口径（§3.11 / 冻结项 6）
BACKEND_SELECTORS = ("auto", "null", "dev")

#: 整数范围
#:
#: `web.port` 的下界取 `0` 是**契约给的特例**（§3.6）：`0` 让系统挑一个空闲端口，
#: 给测试与无头场景用；**默认值仍然是 `8765`**（`DEFAULT_VALUES["web.port"]`），
#: 并且**端口被占时照旧拒绝启动**（`bind_http_socket()` 抛 `PortUnavailableError` →
#: `EXIT_REJECTED`）—— `0` 这一档与「被占仍拒绝启动」是两件事，别把 `0` 读成
#: 「允许随机跳端口」。选中的真实端口由监听 socket 的 `getsockname()` 读回并落 log。
INT_LIMITS: dict[str, tuple[int, int]] = {
    "web.port": (0, 65535),
    "log.max_bytes": (1, 2**53),
    "log.backup_count": (0, 100),
    "alert.silence_seconds": (1, 86400),
    "alert.recover_seconds": (0, 86400),
    "alert.alarm_suppress_seconds": (0, 86400),
    "heartbeat.interval_seconds": (1, 60),
    "asr.max_segment_seconds": (1, 86400),
    "asr.timeout_seconds": (1, 86400),
    "asr.max_new_tokens": (1, 2**31),
    "asr.repeat_max_unit": (1, 2**31),
    # 会后离线的分块长度：下界取 `0` 是**契约给的特例**（`0` = 显式关掉分块、
    # 整段一次跑；与 `web.port = 0` 那种「一个特殊取值合法、默认值另算」的口径同形）。
    # 上界取一天：比这更长的会议按段归档本来就是另一件事。
    # 默认值取 450 的理由写在上面 `DEFAULT_VALUES` 里那一格上面。
    "post.chunk_seconds": (0, 86400),
    # 会后纪要这一层的三个整数键（`41465462` phase2）：取值域与 `meeting/minutes/` 里
    # 那几个常量逐字相同（`chunk.MIN_CHUNK_LINES` / `MAX_CHUNK_LINES`、
    # `contract.DEFAULT_MAX_SEGMENTS` 那一档、一次运行的墙钟上限）。
    "minutes.chunk_lines": (10, 1000),
    "minutes.max_segments": (1, 20),
    "minutes.timeout_seconds": (60, 86400),
    # 会中总结这一层的两个整数键（`1987cb20` phase1）：尝试次数上限与缺省窗口无关的
    # 那一格在这里只有前者。上界取 10 是「再多的重试也只是把同一个错误重放一遍」的
    # 直接读数 —— 真正的刹车是 `summary.deadline_seconds` 那条墙钟 deadline。
    "summary.max_attempts": (1, 10),
}

#: 浮点范围
FLOAT_LIMITS: dict[str, tuple[float, float]] = {
    "alert.silence_dbfs": (-200.0, 100.0),
    "asr.repeat_min_coverage": (0.0, 1.0),
    # 一次会中总结的墙钟 deadline（秒）：下界 1 秒（比它还短的预算拿不到任何应答），
    # 上界一个小时（再长就不再是「会中随叫随到」那件事了）。
    "summary.deadline_seconds": (1.0, 3600.0),
}

#: 会后离线引擎的精度档位（`post.dtype`）：取值逐个对应
#: `meeting/offline/engine.py` 的 `DTYPE_NAMES`（那边把档位名翻成 `torch.dtype`，
#: 认不出的名字当场报错）。两处口径一致：这里放行的名字那边都认得。
POST_DTYPE_VALUES = ("bfloat16", "bf16", "float16", "fp16", "float32", "fp32")

#: 字符串键的合法取值说明（`None` = 任意非空字符串）
STRING_LIMITS: dict[str, tuple[str, ...] | None] = {
    "web.dist_dir": None,
    "output.root": None,
    "log.dir": None,
    "log.level": None,
    "capture.backend": BACKEND_SELECTORS,
    "transcriber.backend": BACKEND_SELECTORS,
    "post.hook": BACKEND_SELECTORS,
    # 会后纪要的钩子（`41465462` phase2）：与 `post.hook` 同一套取值口径
    # （`auto` / `null` / `dev` / `module:Class`）。它是**可空键**（见 `NULLABLE_KEYS`）：
    # 配置文件里写 JSON 的 `null` 就是「不自动跑」，与写字符串 `"null"` 同解。
    "minutes.hook": BACKEND_SELECTORS,
    # 会后纪要那一层的 web 分支（`41465462` 的后续增量）：DSH web 服务端的地址。
    # 任意非空字符串（与 `summary.base_url` 同一条口径：本层不校验 URL 形状，
    # 探针探不通就落回 headless 分支，不留半条路）。
    "minutes.dsh_url": None,
    # 会后离线这一层（`7c6952e5`）：
    #   - `post.engine` 与三个后端选择键同形（`auto` / `null` / `module:Class`）；
    #   - `post.model` 收 HF 仓库 id 或本地快照绝对路径（`None` = 任意非空字符串）；
    #   - `post.dtype` 只认引擎那边认得出来的档位名（写错档位在启动时就拦下）。
    "post.engine": BACKEND_SELECTORS,
    "post.model": None,
    "post.dtype": POST_DTYPE_VALUES,
    # 两个设备键位（`dca4539b` 定名）：任意非空字符串（精确名，MME 端点名可前缀兜）
    "devices.mic": None,
    "devices.loopback": None,
    # 会中总结这一层（`1987cb20` phase1）：
    #   - `summary.base_url` / `summary.model`：任意非空字符串（地址与模型名，换厂商改这里）；
    #   - `summary.default_window`：**四档取值**（与设计章 2.3 的四个按钮逐字对应）。
    #     取值不在这一档里的（比如 `7m`）装载时就拦下。
    # `summary.api_key` 不在这里：它是**可空键**（见 `NULLABLE_KEYS`）。
    # `summary.extra_body_json` 也不在这里：它要的是「一段合法 JSON 对象的文本」，
    # 由 `validate_value()` 里那一支单独管。
    "summary.base_url": None,
    "summary.model": None,
    "summary.default_window": ("5m", "15m", "30m", "full"),
}

#: 可空键：**设备名没配就是 `None`**（空值形状两端一致 —— 快照 `settings.devices` 的子键在
#: 没配时是 `null`，配置文件里也写 `null`；`validate_value()` 把空字符串与纯空白串一并归一成
#: `None`，省得「空串」与「没配」在快照里长出两种形状）。
#:
#: `summary.api_key`（`1987cb20` phase1）也归这一档，理由与设备名同形：**没有密钥就是没配**。
#: 它在 `SummarySettings.missing` 里点名，动作层据此说「缺 summary.api_key」；
#: 归一成空串会让「没配」这件事在读方那边长出第二种形状。它进 `ENV_KEYS`
#: （`MEETING_SUMMARY_API_KEY`），是这一档里唯一一个走环境变量的键。
NULLABLE_KEYS: tuple[str, ...] = ("devices.mic", "devices.loopback", "summary.api_key")

#: 「`null` 是**一个取值**、原样留着」的可空键（`41465462` phase2 加的 `minutes.hook`）：
#: 配置文件里写 JSON 的 `null` 就是「不自动跑会后纪要」，它与两个设备键的「没配」不同 ——
#: 那一档要**原样收下 `None`**（归一成空串或默认值都会把「关掉」这件事吃掉），
#: 解析层（`registry.resolve_minutes_hook()`）看到它是 `None` 就落缺席档。
NULL_SENTINEL_KEYS: tuple[str, ...] = ("minutes.hook",)

#: 全部可写 `null` 的键（设备名那一档 + 上面那一个取值档）
ALL_NULLABLE_KEYS: tuple[str, ...] = (*NULLABLE_KEYS, *NULL_SENTINEL_KEYS)

#: 环境变量映射：契约 §3.11 冻结的五个，外加会中总结的密钥那一格
#: （`1987cb20` phase1 定名）。
#:
#: `MEETING_SUMMARY_API_KEY` 落在这一张表里，走的是与另外五个**同一条**通路：装载时求值、
#: 优先级最高（环境变量 > `config.json` > 代码默认值）、`load()` 的一条 warning 会说它
#: 压住了文件里的值。它是密钥，所以**唯一的用法就是环境变量**：探针与验收脚本只读
#: 调用者已经设好的这个环境变量，自己不写、不打印、不进证据。
#:
#: 它同时也进了测试层的剔除清单（`tests/_helpers.py` 的 `SERVICE_ENV_NAMES` 由本表派生）：
#: 于是机主宿主环境里的这个变量不会悄悄漏进任何子进程用例。
ENV_KEYS: dict[str, str] = {
    "MEETING_PORT": "web.port",
    "MEETING_OUT_ROOT": "output.root",
    "MEETING_LOG_DIR": "log.dir",
    "MEETING_CAPTURE": "capture.backend",
    "MEETING_SUMMARY_API_KEY": "summary.api_key",
}

#: 会中总结的密钥那一格的环境变量名（`ENV_KEYS` 里那一条的名字，供探针与文档引用）
ENV_SUMMARY_API_KEY = "MEETING_SUMMARY_API_KEY"

#: 关掉浏览器打开的开关（值为 `1` 时生效）
ENV_NO_BROWSER = "MEETING_NO_BROWSER"

# ---------------------------------------------------------------------------
# `settings_update` 的键态三分表（phase2 §3.5.5）
#
# **只有下面逐条列出的字面键名合法，不按前缀归组、也没有任何前缀白名单**；
# 表外的任何键名一律 `invalid_setting`(400)。
# 27 个名字逐一取自上面的键全集（phase1 定死的顶层键 + `asr.*` 五个子键 +
# `dca4539b` 定名的 `devices.mic` / `devices.loopback` 两个设备键位 +
# `7c6952e5` 定名的 `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds`
# 四个会后离线开关）。
# 设备键位是**点号字面键名**（`devices.mic`），快照里才收成 `settings.devices.mic` 这种子键；
# 按前缀归组或写成 `devices.{...}` 的嵌套形状都不合法（与 `asr.*` 同一套口径）。
# ---------------------------------------------------------------------------

#: 可热读的键（每段重新读）→ 落 `applied`
APPLIED_SETTING_KEYS: tuple[str, ...] = tuple(HOT_KEYS)

#: 其余键（写盘成功但要重启才生效）→ 落 `pending_restart`
#:
#: `devices.mic` / `devices.loopback` 归这一档：写盘成功即落盘，运行期仍取启动时的生效值
#: （设备在开场前定下来，改配置要重启才在 `probe()` 里生效）。
PENDING_RESTART_SETTING_KEYS: tuple[str, ...] = tuple(
    key for key in DEFAULT_VALUES if key not in HOT_KEYS
)

#: 三分表本体：点号字面键名 → 归类（`applied` / `pending_restart`）
SETTING_KEY_GROUPS: dict[str, str] = {
    **{key: "applied" for key in APPLIED_SETTING_KEYS},
    **{key: "pending_restart" for key in PENDING_RESTART_SETTING_KEYS},
}


def setting_group(key: str) -> str | None:
    """某个字面键名在三分表里的归类；表外一律 `None`（调用方转 `invalid_setting`(400)）。"""
    return SETTING_KEY_GROUPS.get(key)


#: 快照 `settings.asr` 的五个子键 ↔ 配置键（**逐字同名**，只是去掉了 `asr.` 前缀）。
#: 嵌套写法只出现在状态 JSON 里，**不进请求体、不进应答体、不进 `config.json`**
#: （phase2 §3.5.5：请求 / 应答 / 配置文件 / 读取层四处一律用同一套点号扁平键名）。
#: 从 `HOT_KEYS` 里**按 `asr.` 前缀筛**：热读键那一档后来收了 `alarm.sound`，直接对整张表
#: 去前缀会让快照里长出一个 `settings.asr.sound`。
ASR_SETTING_KEYS: tuple[str, ...] = tuple(
    key.split(".", 1)[1] for key in HOT_KEYS if key.startswith("asr.")
)

#: 两个设备键位（`dca4539b` 定名）—— `DEVICE_SETTING_KEYS` 与快照那一格都按它筛，
#: 因此后来收进 `NULLABLE_KEYS` 的 `summary.api_key`（也是「没配即 `null`」那一档）
#: **不会**在快照里长出一个 `settings.devices.api_key`。
DEVICE_KEYS: tuple[str, ...] = ("devices.mic", "devices.loopback")

#: 快照 `settings.devices` 的子键 ↔ 配置键（`dca4539b` 定名，口径与 `ASR_SETTING_KEYS` 同）：
#: `devices.mic` / `devices.loopback` 两个点号键位，快照里收成 `settings.devices.mic` /
#: `settings.devices.loopback` 两个子键。取值的空值形状两端一致：没配即 `null`。
DEVICE_SETTING_KEYS: tuple[str, ...] = tuple(
    key.split(".", 1)[1] for key in DEVICE_KEYS
)


def is_setting_key(key: str) -> bool:
    return key in SETTING_KEY_GROUPS


def validate_setting_changes(
    changes: dict[str, Any], *, current: "Config | None" = None
) -> dict[str, Any]:
    """`settings_update` 的写前校验：**表外键名与非法值一律在这里拦下**。

    至少落地方案点名的「**超时必须大于段长**」（`asr.timeout_seconds >
    asr.max_segment_seconds`）：这次没改到的那一半取生效配置里的当前值，
    违规时 `ConfigValueError` 的 `key` 指向 `asr.timeout_seconds`。
    """
    if not isinstance(changes, dict) or not changes:
        raise ConfigValueError(
            "settings_update", changes, "「点号扁平键名 → 值」的非空映射",
            "请求体 args 是「配置点号键名 → 值」的扁平映射，只有这一层、不按键前缀分组",
        )

    applied: dict[str, Any] = {}
    for key, value in changes.items():
        if not isinstance(key, str) or not is_setting_key(key):
            raise ConfigValueError(
                key if isinstance(key, str) else repr(key),
                value,
                "本服务定义过的键名",
                "这个键名不在三分表里（表里没有这个字面键名）",
            )
        if isinstance(value, dict):
            raise ConfigValueError(
                key, value, "标量值",
                "嵌套形状不合法：args 是「配置点号键名 → 值」的扁平映射",
            )
        applied[key] = validate_value(key, value)

    def effective(key: str) -> Any:
        if current is not None:
            return current.get(key)
        return get(key)

    segment = applied.get("asr.max_segment_seconds", effective("asr.max_segment_seconds"))
    timeout = applied.get("asr.timeout_seconds", effective("asr.timeout_seconds"))
    if float(timeout) <= float(segment):
        raise ConfigValueError(
            "asr.timeout_seconds",
            timeout,
            f"必须大于 asr.max_segment_seconds（当前 {segment}）",
            "键间规则不成立（超时必须大于段长）",
        )
    return applied


def split_setting_changes(changes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把一次写盘的键名按三分表切成 `(applied, pending_restart)` 两个数组。

    `applied` 的判据只有一条：**该键在三分表里归 `applied`，且本次写盘成功**。
    「被环境变量压着」这件事不改分类（它只进主 log 的一条 warning）。
    """
    applied: list[str] = []
    pending: list[str] = []
    for key in changes:
        group = setting_group(key)
        if group == "applied":
            applied.append(key)
        elif group == "pending_restart":
            pending.append(key)
    return applied, pending


def env_name_for(key: str) -> str | None:
    """压住某个键的环境变量名（没有就 `None`）；只用于主 log 的那条 warning。"""
    for env_name, mapped in ENV_KEYS.items():
        if mapped == key:
            return env_name
    return None


def write_config_values(
    changes: dict[str, Any], *, path: str | None = None
) -> str:
    """`settings_update` 的原子写入口：**只写用户配置文件里已有的键 + 本次改动的键**。

    落地读法（phase2 §3.5.5 的写盘拓扑）：先读回现有 `config.json`（文件不存在时按空对象起）
    → 叠加本次改动的键 → 原子写回（同目录临时文件 + `os.replace` + flush）。
    写回后文件里的键名集合 = 读回时的键名集合 ∪ 本次改动的键名（**不把代码默认值落盘**）。

    写盘失败抛 `OSError`（调用方映射成 `persist_failed`(500)）；**内存里的生效值不许留在半途**。
    """
    target = config_path() if path is None else path
    current = _read_config_file(target)  # 损坏 / 读不出来 → ConfigCorruptError（由调用方处理）
    merged = dict(current)
    merged.update(changes)

    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{CONFIG_FILE_NAME}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - 清理失败不影响结果
                pass
    return target


# ---------------------------------------------------------------------------
# 家目录解析（测试的配置隔离靠重定向 HOME / USERPROFILE，不靠环境变量换路径）
# ---------------------------------------------------------------------------


def home_dir() -> str:
    """当前用户主目录：先看 `HOME`，再看 `USERPROFILE`。

    同时尊重已显式设置的 `HOME` 与 `USERPROFILE`（两者都指到临时目录时结果相同）。
    """
    home = os.environ.get("HOME")
    if home:
        return home
    profile = os.environ.get("USERPROFILE")
    if profile:
        return profile
    return os.path.expanduser("~")


def config_dir() -> str:
    """配置与日志的根目录：`~/.config/meeting`。"""
    return os.path.join(home_dir(), *CONFIG_DIR_NAME)


def config_path() -> str:
    """配置文件路径：`~/.config/meeting/config.json`（由家目录推出，不靠环境变量）。"""
    return os.path.join(config_dir(), CONFIG_FILE_NAME)


def default_log_dir() -> str:
    """`log.dir` 的默认值：`~/.config/meeting/logs`。"""
    return os.path.join(config_dir(), "logs")


def default_output_root() -> str:
    """`output.root` 的默认值：`%USERPROFILE%\\Documents\\meeting\\out`。

    Windows 上用 `USERPROFILE` 展开；其它平台退化为用户主目录下的 `Documents/meeting/out`。
    """
    return os.path.join(home_dir(), "Documents", "meeting", "out")


def default_for(key: str) -> Any:
    """取某个键的默认值（平台相关键在这里解析成具体值）。"""
    if key == "output.root":
        return default_output_root()
    if key == "log.dir":
        return default_log_dir()
    return DEFAULT_VALUES[key]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _legal_text(key: str) -> str:
    if key in INT_LIMITS:
        low, high = INT_LIMITS[key]
        return f"整数，{low}–{high}"
    if key in FLOAT_LIMITS:
        low, high = FLOAT_LIMITS[key]
        return f"数字，{low}–{high}"
    if key == "summary.extra_body_json":
        return "空字符串，或一段 JSON 对象的文本（例如 {\"thinking\": {\"type\": \"disabled\"}}）"
    if key in STRING_LIMITS:
        allowed = STRING_LIMITS[key]
        text = (
            "非空字符串"
            if allowed is None
            else "字符串，取值 " + " / ".join(allowed) + "（或 module:Class 这种显式写法）"
        )
        if key in NULLABLE_KEYS:
            return f"{text}，或 null（null = 没配，按默认策略挑）"
        if key in NULL_SENTINEL_KEYS:
            return f"{text}，或 null（null = 显式关掉这一步）"
        return text
    if key == "alarm.sound":
        return "布尔值 true / false"
    raise KeyError(key)


def validate_value(key: str, value: Any) -> Any:
    """校验并规范化一个键的值；非法即抛 `ConfigValueError`。

    类型不对、超出范围、或违反键间规则都在这里拦下 —— 报错含键名 / 收到的值 / 合法范围。
    可空键（`NULLABLE_KEYS`，设备名那两个）收 `None` 并把空字符串一并归一成 `None`：
    快照 `settings.devices` 的子键因此只有「设备名」与 `null` 两种形状。
    """
    if key not in DEFAULT_VALUES:
        raise ConfigValueError(key, value, "本服务定义过的键名", "这个键名不在配置键表里")

    if key == "alarm.sound":
        if not isinstance(value, bool):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是布尔值")
        return value

    if key == "summary.extra_body_json":
        # 厂商特有字段（V1 preset 的 `extra_body`）以**一个 JSON 字符串键**承载：
        # 空串 = 不并；非空时必须是一段**JSON 对象的文本**。数组、数字、`"abc"` 这种
        # 能解析但不是对象的，与压根解析不了的，都在装载时拦下 —— 让一个读不出来的值
        # 走到请求那一层，等于把一次总结交给一段语法错误的文本。
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串")
        if not value.strip():
            return ""
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigValueError(
                key, value, _legal_text(key), f"这段文本不是合法 JSON：{exc.msg}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigValueError(
                key, value, _legal_text(key), "这段 JSON 的顶层不是对象"
            )
        return value

    if key in NULLABLE_KEYS:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串")
        text = value.strip()
        return text or None

    if key in NULL_SENTINEL_KEYS:
        # `null` 在这里是一个**取值**（关掉这一步）：原样收下 `None`，与两个设备键的「没配」
        # 各走各的一支 —— 归一成空串会让「关掉」这件事在读方那边消失。
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串或 null")
        if not value.strip():
            raise ConfigValueError(key, value, _legal_text(key), "空字符串不合法（要关掉就写 null）")
        allowed = STRING_LIMITS[key]
        if allowed is not None and value not in allowed and ":" not in value:
            raise ConfigValueError(key, value, _legal_text(key), "取值不在允许集合里")
        return value

    if key in INT_LIMITS:
        # bool 是 int 的子类，别让 True 混成 1
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是整数")
        low, high = INT_LIMITS[key]
        if not (low <= value <= high):
            raise ConfigValueError(key, value, _legal_text(key), "超出范围")
        # `web.port = 0` 是测试 / 无头场景的特例（系统挑空闲端口），合法范围照 INT_LIMITS；
        # 默认值仍是 8765，端口被占仍然拒绝启动（见 INT_LIMITS 上面的说明）。
        return value

    if key in FLOAT_LIMITS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是数字")
        low, high = FLOAT_LIMITS[key]
        if not (low <= float(value) <= high):
            raise ConfigValueError(key, value, _legal_text(key), "超出范围")
        return float(value)

    if key in STRING_LIMITS:
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串")
        if not value.strip():
            raise ConfigValueError(key, value, _legal_text(key), "空字符串不合法")
        allowed = STRING_LIMITS[key]
        if allowed is not None and value not in allowed and ":" not in value:
            raise ConfigValueError(key, value, _legal_text(key), "取值不在允许集合里")
        return value

    raise KeyError(key)  # pragma: no cover - DEFAULT_VALUES 全覆盖


def _validate_cross_rules(applied: dict[str, Any]) -> None:
    """键间规则（D6 / 方案 §5.3 的 `invalid_setting` 判据）。

    `asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。
    """
    segment = applied["asr.max_segment_seconds"]
    timeout = applied["asr.timeout_seconds"]
    if timeout <= segment:
        raise ConfigValueError(
            "asr.timeout_seconds",
            timeout,
            f"必须大于 asr.max_segment_seconds（当前 {segment}）",
            "键间规则不成立",
        )


def _read_config_file(path: str) -> dict[str, Any]:
    """读 `config.json`；文件不存在返回空映射，损坏抛 `ConfigCorruptError`。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        # 例如同名目录顶替配置文件（Windows 上是 PermissionError）
        raise ConfigCorruptError(path, f"{type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigCorruptError(path, f"JSON 解析失败于第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigCorruptError(path, "顶层不是 JSON 对象（本文件是点号扁平键名的单层映射）")
    return data


def _flatten_message(key: str, value: Any) -> str:
    return f"{key} = {value!r}"


# ---------------------------------------------------------------------------
# 生效配置对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """一次 `load()` 的产物：生效值 + 来源信息。"""

    applied: dict[str, Any]
    sources: dict[str, str]
    path: str

    def get(self, key: str, default: Any = None) -> Any:
        """点号路径取值。"""
        if key in self.applied:
            return self.applied[key]
        if default is not None:
            return default
        return default_for(key)

    def as_dict(self) -> dict[str, Any]:
        """生效值（供 log 与 `/debug`）。"""
        return dict(self.applied)

    def hot_keys(self) -> list[str]:
        """返回热读键（供 log 里「哪些键是热读的」那一行）。

        眼下六个：`alarm.sound` 与 `asr.*` 那五个（`HOT_KEYS` 那张表就是清单本身）。
        """
        return list(HOT_KEYS)

    def config_path(self) -> str:
        """本次读取用的配置文件路径。"""
        return self.path

    def source_of(self, key: str) -> str:
        return self.sources.get(key, "默认")

    def no_browser(self) -> bool:
        """`MEETING_NO_BROWSER=1` 关掉浏览器打开（供测试与无头场景）。"""
        return os.environ.get(ENV_NO_BROWSER, "").strip() == "1"


# ---------------------------------------------------------------------------
# 读取入口
# ---------------------------------------------------------------------------


def load(
    *,
    environ: dict[str, str] | None = None,
    path: str | None = None,
) -> Config:
    """启动时读一次配置：环境变量 > `config.json` > 默认值。

    `environ` / `path` 只服务测试注入；默认分别取 `os.environ` 与 `config_path()`。
    """
    env = os.environ if environ is None else environ
    config_file = config_path() if path is None else path

    file_values = _read_config_file(config_file)

    applied: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for key, raw in file_values.items():
        if key not in DEFAULT_VALUES:
            raise ConfigValueError(
                key, raw, "本服务定义过的键名",
                "这个键名不在配置键表里（键名拼错或写了多余的键）",
            )

    for key in DEFAULT_VALUES:
        value = default_for(key)
        sources[key] = "默认"

        if key in file_values:
            raw = file_values[key]
            if isinstance(raw, dict):
                raise ConfigValueError(
                    key, raw, _legal_text(key),
                    "嵌套形状不合法：config.json 是点号扁平键名的单层映射",
                )
            value = validate_value(key, raw)
            sources[key] = "config.json"

        applied[key] = value

    # 环境变量层（一律启动时求值，改了不会热生效）
    for env_name, key in ENV_KEYS.items():
        if env_name not in env:
            continue
        raw_text = env[env_name]
        if key in INT_LIMITS:
            try:
                raw: Any = int(raw_text)
            except (TypeError, ValueError):
                raise ConfigValueError(
                    key, raw_text, _legal_text(key),
                    f"环境变量 {env_name} 的值不是整数",
                ) from None
        else:
            raw = raw_text
        applied[key] = validate_value(key, raw)
        sources[key] = f"环境变量 {env_name}"

    _validate_cross_rules(applied)

    return Config(applied=applied, sources=sources, path=config_file)


def format_applied(config: Config) -> str:
    """把生效值清单写成一行行 `key = value`（供 log 的「配置来源与生效值」）。"""
    lines = []
    for key in DEFAULT_VALUES:
        lines.append(f"  {_flatten_message(key, config.applied[key])}  [{config.source_of(key)}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级状态（`get()` 的便捷入口）
# ---------------------------------------------------------------------------

_LOADED: Config | None = None


def get(key: str, default: Any = None) -> Any:
    """点号路径取值（未 `load()` 过时退回默认值表）。"""
    if _LOADED is None:
        return default_for(key) if default is None else default
    return _LOADED.get(key, default)


def current() -> Config | None:
    return _LOADED


def set_current(config: Config | None) -> None:
    """测试与 `cli.py` 的注入点。"""
    global _LOADED
    _LOADED = config


def as_dict() -> dict[str, Any]:
    """生效值（供 log 与 `/debug`）。"""
    if _LOADED is None:
        return {key: default_for(key) for key in DEFAULT_VALUES}
    return _LOADED.as_dict()


def hot_keys() -> list[str]:
    """热读键六个：`alarm.sound` 与 `asr.*` 那五个（清单就是 `HOT_KEYS`）。"""
    return list(HOT_KEYS)


__all__ = [
    "APPLIED_SETTING_KEYS",
    "ASR_SETTING_KEYS",
    "BACKEND_SELECTORS",
    "CONFIG_FILE_NAME",
    "Config",
    "ConfigCorruptError",
    "ConfigError",
    "ConfigValueError",
    "DEFAULT_VALUES",
    "DEVICE_KEYS",
    "DEVICE_SETTING_KEYS",
    "ENV_KEYS",
    "ENV_NO_BROWSER",
    "ENV_SUMMARY_API_KEY",
    "HOT_KEYS",
    "NULLABLE_KEYS",
    "PENDING_RESTART_SETTING_KEYS",
    "SETTING_KEY_GROUPS",
    "as_dict",
    "config_dir",
    "config_path",
    "current",
    "default_for",
    "default_log_dir",
    "default_output_root",
    "env_name_for",
    "format_applied",
    "get",
    "home_dir",
    "hot_keys",
    "is_setting_key",
    "load",
    "set_current",
    "setting_group",
    "split_setting_changes",
    "validate_setting_changes",
    "validate_value",
    "write_config_values",
]
