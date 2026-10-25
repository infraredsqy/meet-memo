"""配置层（phase1 §3.11，B11 的正确边界）。

`~/.config/meeting/config.json`：点号扁平键名的单层映射（嵌套形状不合法），
不迁移 V1 的 `~/.config/meeting_notes/`。优先级：环境变量 > `config.json` > 代码默认值（B11）。
热读键见 `HOT_KEYS`（写盘即生效），其余键启动时读一次；环境变量一律在 import / 启动时求值
（B11 的坑）。`alert.*` 是判定阈值，`alarm.*` 只管出声；`heartbeat.interval_seconds` 是界面
心跳（管失联横幅），与服务侧采集帧心跳（`max(2.0s, 3 × frame_interval)`，管 `recording_alive`）
不许串线。文件不存在 → 用默认值照常起；读不出来 / 不是 JSON → `ConfigCorruptError`
（`cli.main()` 映射 `EXIT_STARTUP_UNMET`）；键值非法 → `ConfigValueError`（`EXIT_REJECTED`，
在写生效值清单之前拦下）——都不许拿默认值糊过去。`web.host` 这类键永远不合法：
只监听 `127.0.0.1` 是安全边界，不能被配置改掉。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

# 异常（退出码常量由 cli.main() 映射成 §3.4 的常量后 sys.exit，本模块不定义）


class ConfigError(Exception):
    """配置层错误基类。"""


class ConfigCorruptError(ConfigError):
    """`config.json` 读不出来或不是合法 JSON（文件级损坏），对应 `EXIT_STARTUP_UNMET`。"""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"配置文件损坏：{path}（{detail}）")


class ConfigValueError(ConfigError):
    """某个键的值非法（类型 / 范围 / 键间规则），对应 `EXIT_REJECTED`。

    `key` / `value` / `legal` 供终端与 bootstrap 日志写清键名、收到的值、合法范围。
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


# 默认值表（§3.11，键名与默认值都是跨 Phase 冻结项）

CONFIG_DIR_NAME = (".config", "meeting")
CONFIG_FILE_NAME = "config.json"
SERVICE_LOG_FILE_NAME = "meeting.log"

#: 热读键：写盘那一刻就生效。`alarm.sound` 排在 `asr.*` 前面，因为 `ASR_SETTING_KEYS`
#: 是按 `asr.` 前缀从这张表筛出来的，排在前面不参与快照 `settings.asr` 的子键次序。
HOT_KEYS: tuple[str, ...] = (
    "alarm.sound",
    "asr.max_segment_seconds",
    "asr.timeout_seconds",
    "asr.max_new_tokens",
    "asr.repeat_max_unit",
    "asr.repeat_min_coverage",
    "devices.mic",
    "devices.loopback",
    "heartbeat.interval_seconds",
    "summary.default_window",
    "summary.presets_json",
    "summary.active_preset",
)

#: `asr.*` 五个默认值沿用 `docs/v1-lineage.md` 的 B1（V1 实测值），复核只许改值不许改口径。
DEFAULT_VALUES: dict[str, Any] = {
    "web.port": 8765,
    "web.dist_dir": "web/dist",
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
    # 会后离线转写（`7c6952e5` 定名，与 `capture.backend` 同形：接口在这里，值归选型节点）。
    # 模型默认 MOSSTD 的 HF 仓库 id（本机已有本地快照），档位取本机实测跑通的 bf16。
    "post.engine": "auto",
    "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
    "post.dtype": "bfloat16",
    # 每块音频长度上限（秒），`0` = 显式关掉分块。默认 450 是对 spec「默认关闭」的有意偏离：
    # 本机实测 600 s → 11.6 GiB 峰值、900 s → 22.9 GiB（越界换页，墙钟劣化 5–12 倍）、
    # 1500 s → OOM（16 GB 卡，phase3 报告 10.1），而验收素材 5943 s 整段必死；450 s 实测
    # 峰值 7.5 GiB 是安全档。块数为 1 时不切块（短于 450 s 仍整段跑）。
    "post.chunk_seconds": 450,
    # 两路各自用哪个设备（`dca4539b` 定名）：`None` = 按默认策略挑（见 capture/devices.py）
    "devices.mic": None,
    "devices.loopback": None,
    "asr.max_segment_seconds": 10,
    "asr.timeout_seconds": 20,
    "asr.max_new_tokens": 400,
    "asr.repeat_max_unit": 40,
    "asr.repeat_min_coverage": 0.5,
    # 会后纪要生成（`41465462` phase2 定名，与 `post.*` 同形）：
    #   - `minutes.hook`：`auto` = 跟离线链尾巴自动跑；JSON `null` = 不自动跑。
    #   - `minutes.chunk_lines`：每块行数，与 V1 切块口径一致（取值域同 chunk.MIN/MAX）。
    #   - `minutes.max_segments`：每条 source 的分段上限（取值域同 contract.DEFAULT_MAX_SEGMENTS）。
    #   - `minutes.timeout_seconds`：一次纪要生成的墙钟上限（R8：会后换质量，但要有上限）。
    #   - `minutes.dsh_url`：DSH web 服务端地址（不是 `dsh` 可执行文件）。在跑时经 `/api`
    #     在场次工作区建会话交付（终态 `delivered`），没在跑才落回本进程 headless 子进程。
    "minutes.hook": "auto",
    "minutes.chunk_lines": 100,
    "minutes.max_segments": 8,
    "minutes.timeout_seconds": 14400,
    "minutes.dsh_url": "http://127.0.0.1:3080",
    # 会中总结（`1987cb20` phase1 定名，与 `post.*` / `minutes.*` 同形）：
    #   - `base_url` / `model`：OpenAI 兼容协议，默认本机实测跑通的 DeepSeek 端点，换厂商改这两个键。
    #   - `api_key`：可空键，密钥不进仓库——写配置文件或用 `MEETING_SUMMARY_API_KEY` 压住。
    #   - `extra_body_json`：厂商特有字段以一个 JSON 字符串键承载（配置层拒收 dict 值，
    #     为这一个键放开硬约束不值得），空串 = 不并。
    #   - `deadline_seconds`：墙钟 deadline，换掉 V1「3 × 60 秒硬编码超时」最坏 180 秒的算法。
    #   - `max_attempts`：尝试次数上限，真正的刹车是 deadline。
    #   - `default_window`：`args.window` 省略时的缺省档（四档取值）。
    "summary.base_url": "https://api.deepseek.com/v1",
    "summary.api_key": None,
    "summary.model": "deepseek-chat",
    "summary.extra_body_json": "",
    # 会中切模型两键（与四件套同组）：`presets_json` 全部预设装在一个 JSON 字符串键里
    # （同 `extra_body_json` 的先例），空串 = 没配（回落平铺四键），非空必须是 JSON 数组文本：
    # 每套 `name` 必填（非空白、不重名，指针按它挑），其余四格可省。**预设里的密钥不进快照、
    # 不在界面回显**。`active_preset` 当前用哪套；名字不在清单里时读的一侧回落平铺键。
    "summary.presets_json": "",
    "summary.active_preset": "",
    "summary.deadline_seconds": 25.0,
    "summary.max_attempts": 3,
    "summary.default_window": "15m",
}

#: 后端选取键的取值口径（§3.11 / 冻结项 6）
BACKEND_SELECTORS = ("auto", "null", "dev")

#: `web.port` 下界 `0` 是契约特例（§3.6）：让系统挑空闲端口（测试 / 无头场景），
#: 默认值仍是 8765，且端口被占照旧拒绝启动——`0` 不是「允许随机跳端口」。
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
    # `0` = 关掉分块（与 `web.port = 0` 的「特殊取值合法、默认值另算」同形）；上界一天。
    "post.chunk_seconds": (0, 86400),
    # 取值域与 `meeting/minutes/` 里的常量逐字相同
    "minutes.chunk_lines": (10, 1000),
    "minutes.max_segments": (1, 20),
    "minutes.timeout_seconds": (60, 86400),
    # 上界 10：再多的重试只是重放同一个错误，真正的刹车是 deadline。
    "summary.max_attempts": (1, 10),
}

#: 浮点范围
FLOAT_LIMITS: dict[str, tuple[float, float]] = {
    "alert.silence_dbfs": (-200.0, 100.0),
    "asr.repeat_min_coverage": (0.0, 1.0),
    # 一次总结的墙钟 deadline（秒）：下界 1 秒（更短拿不到任何应答），上界一小时。
    "summary.deadline_seconds": (1.0, 3600.0),
}

#: `post.dtype` 取值逐个对应 `meeting/offline/engine.py` 的 `DTYPE_NAMES`，两处口径一致。
POST_DTYPE_VALUES = ("bfloat16", "bf16", "float16", "fp16", "float32", "fp32")

#: 字符串键的合法取值（`None` = 任意非空字符串；本层不校验 URL 形状）
STRING_LIMITS: dict[str, tuple[str, ...] | None] = {
    "web.dist_dir": None,
    "log.dir": None,
    "log.level": None,
    "capture.backend": BACKEND_SELECTORS,
    "transcriber.backend": BACKEND_SELECTORS,
    "post.hook": BACKEND_SELECTORS,
    "minutes.hook": BACKEND_SELECTORS,  # 与 post.hook 同口径；可空键（null = 不自动跑）
    "minutes.dsh_url": None,
    "post.engine": BACKEND_SELECTORS,  # auto / null / module:Class
    "post.model": None,  # HF 仓库 id 或本地快照绝对路径
    "post.dtype": POST_DTYPE_VALUES,
    "devices.mic": None,  # 精确设备名，MME 端点名可前缀兜
    "devices.loopback": None,
    "summary.base_url": None,
    "summary.model": None,
    # 四档取值（设计章 2.3 的四个按钮），取值不在档内的装载时拦下
    "summary.default_window": ("5m", "15m", "30m", "full"),
}

#: 可空键：没配就是 `None`，空值形状两端一致（快照子键与配置文件都写 `null`；
#: `validate_value()` 把空串归一成 `None`）。`summary.api_key` 同理——
#: 「没配」不归一成空串，归一会让读方长出第二种形状。
NULLABLE_KEYS: tuple[str, ...] = ("devices.mic", "devices.loopback", "summary.api_key")

#: `null` 是**一个取值**（原样留 `None`）的可空键：`minutes.hook` 写 JSON `null` = 关掉
#: 会后纪要，归一成空串或默认值都会把「关掉」吃掉。
NULL_SENTINEL_KEYS: tuple[str, ...] = ("minutes.hook",)

#: 全部可写 `null` 的键
ALL_NULLABLE_KEYS: tuple[str, ...] = (*NULLABLE_KEYS, *NULL_SENTINEL_KEYS)

#: 契约 §3.11 冻结的映射。`MEETING_SUMMARY_API_KEY` 走同一条通路：装载时求值、
#: 优先级最高；是密钥，唯一用法就是环境变量（不写盘、不打印、不进证据）。
#: `tests/_helpers.py` 的 `SERVICE_ENV_NAMES` 由本表派生，机主环境变量不会漏进子进程用例。
ENV_KEYS: dict[str, str] = {
    "MEETING_PORT": "web.port",
    "MEETING_LOG_DIR": "log.dir",
    "MEETING_CAPTURE": "capture.backend",
    "MEETING_SUMMARY_API_KEY": "summary.api_key",
}

#: 会中总结密钥的环境变量名（供探针与文档引用）
ENV_SUMMARY_API_KEY = "MEETING_SUMMARY_API_KEY"

#: 值为 `1` 时关掉浏览器打开（测试与无头场景）
ENV_NO_BROWSER = "MEETING_NO_BROWSER"

# `settings_update` 的键态三分表（phase2 §3.5.5）：只有逐条列出的字面键名合法，
# 不按前缀归组、没有前缀白名单，表外一律 `invalid_setting`(400)。设备键位是点号
# 字面键名，快照里才收成子键；嵌套形状不合法。

#: 可热读的键 → 落 `applied`
APPLIED_SETTING_KEYS: tuple[str, ...] = tuple(HOT_KEYS)

#: 其余键（写盘成功但要重启才生效）→ 落 `pending_restart`
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


#: 快照 `settings.asr` 的子键 ↔ 配置键（逐字同名，去掉 `asr.` 前缀）。嵌套写法只出现在
#: 状态 JSON 里，不进请求体、应答体、`config.json`。按 `asr.` 前缀从 `HOT_KEYS` 筛
#: （热读档后来收了 `alarm.sound`，直接对整张表去前缀会长出 `settings.asr.sound`）。
ASR_SETTING_KEYS: tuple[str, ...] = tuple(
    key.split(".", 1)[1] for key in HOT_KEYS if key.startswith("asr.")
)

#: 两个设备键位——`DEVICE_SETTING_KEYS` 与快照都按它筛，因此同为「没配即 null」档的
#: `summary.api_key` 不会在快照里长出 `settings.devices.api_key`。
DEVICE_KEYS: tuple[str, ...] = ("devices.mic", "devices.loopback")

#: 快照 `settings.devices` 的子键 ↔ 配置键（口径与 `ASR_SETTING_KEYS` 同）
DEVICE_SETTING_KEYS: tuple[str, ...] = tuple(
    key.split(".", 1)[1] for key in DEVICE_KEYS
)


def is_setting_key(key: str) -> bool:
    return key in SETTING_KEY_GROUPS


def summary_presets_of(text: Any) -> list[dict[str, str]]:
    """`summary.presets_json` 文本 → 归一化预设清单（宽进：读的一侧不炸）。

    文本空白、解析失败、顶层非数组、`name` 缺失或重名都按「没配」收——
    手写配置绕过 `validate_value()` 严格校验时这里兜底。空清单 = 无可用预设。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    presets: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean = name.strip()
        if clean in seen:  # 重名的那一套没法被指针单独挑中，丢掉后面那套
            continue
        seen.add(clean)
        fields: dict[str, str] = {"name": clean}
        for field_name in ("base_url", "api_key", "model", "extra_body_json"):
            value = item.get(field_name, "")
            fields[field_name] = value if isinstance(value, str) else ""
        presets.append(fields)
    return presets


def validate_setting_changes(
    changes: dict[str, Any], *, current: "Config | None" = None
) -> dict[str, Any]:
    """`settings_update` 的写前校验：表外键名与非法值一律在这里拦下。

    键间规则「超时必须大于段长」：没改到的那一半取生效配置里的当前值。
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

    # 键间规则：active_preset 指向的名字必须在预设清单里。只拦 settings_update 的写前
    # 校验，刻意不在装载时拦——手删一套预设后服务照常起。
    presets_text = applied.get("summary.presets_json", effective("summary.presets_json"))
    active = applied.get("summary.active_preset", effective("summary.active_preset"))
    active_name = active.strip() if isinstance(active, str) else ""
    if active_name:
        names = [item["name"] for item in summary_presets_of(presets_text)]
        if active_name not in names:
            raise ConfigValueError(
                "summary.active_preset",
                active,
                "空字符串（不选预设），或某套预设的 name",
                (
                    f"预设清单里没有叫 {active_name!r} 的那一套"
                    + (f"（清单里有的：{'、'.join(names)}）" if names else "（预设清单是空的）")
                ),
            )
    return applied


def split_setting_changes(changes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """按三分表切成 `(applied, pending_restart)`；判据只有「归 applied 且本次写盘成功」。

    「被环境变量压着」不改分类（只进主 log 的一条 warning）。
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
    """`settings_update` 的原子写入口：只写用户配置文件里已有的键 + 本次改动的键。

    先读回现有 `config.json` → 叠加改动 → 原子写回；不把代码默认值落盘。
    写盘失败抛 `OSError`（调用方映射成 `persist_failed`(500)），内存生效值不许留在半途。
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


# 家目录解析（测试的配置隔离靠重定向 HOME / USERPROFILE）


def home_dir() -> str:
    """当前用户主目录：先看 `HOME`，再看 `USERPROFILE`。"""
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
    """配置文件路径：`~/.config/meeting/config.json`（由家目录推出）。"""
    return os.path.join(config_dir(), CONFIG_FILE_NAME)


def default_log_dir() -> str:
    """`log.dir` 的默认值：`~/.config/meeting/logs`。"""
    return os.path.join(config_dir(), "logs")


def default_for(key: str) -> Any:
    """取某个键的默认值（平台相关键在这里解析成具体值）。"""
    if key == "log.dir":
        return default_log_dir()
    return DEFAULT_VALUES[key]


# 校验


def _legal_text(key: str) -> str:
    if key in INT_LIMITS:
        low, high = INT_LIMITS[key]
        return f"整数，{low}–{high}"
    if key in FLOAT_LIMITS:
        low, high = FLOAT_LIMITS[key]
        return f"数字，{low}–{high}"
    if key == "summary.presets_json":
        return (
            "空字符串，或一段 JSON 数组的文本：每套一个对象，"
            'name 必填（非空白、不重名），base_url / api_key / model / extra_body_json 可省'
        )
    if key == "summary.active_preset":
        return "空字符串（不选预设，用平铺四键），或某套预设的 name"
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
    """校验并规范化一个键的值；非法即抛 `ConfigValueError`（报错含键名 / 值 / 合法范围）。

    可空键收 `None` 并把空字符串归一成 `None`，快照子键因此只有「设备名」与 `null` 两种形状。
    """
    if key not in DEFAULT_VALUES:
        raise ConfigValueError(key, value, "本服务定义过的键名", "这个键名不在配置键表里")

    if key == "alarm.sound":
        if not isinstance(value, bool):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是布尔值")
        return value

    if key == "summary.presets_json":
        # 非空必须是 JSON 数组文本，每套 name 必填且不重名。写错的预设当场拦下——
        # 让一套读不出来的预设走到总结层，等于把一次总结交给一段语法错误的文本。
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
        if not isinstance(parsed, list):
            raise ConfigValueError(
                key, value, _legal_text(key), "这段 JSON 的顶层不是数组"
            )
        seen_names: set[str] = set()
        for index, item in enumerate(parsed, start=1):
            where = f"第 {index} 套预设"
            if not isinstance(item, dict):
                raise ConfigValueError(
                    key, value, _legal_text(key),
                    f"{where}不是对象（每套预设该是带 name 等五格的对象）",
                )
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ConfigValueError(
                    key, value, _legal_text(key),
                    f"{where}缺一个非空白的 name（界面上当前用哪一套按名字挑）",
                )
            clean = name.strip()
            if clean in seen_names:
                raise ConfigValueError(
                    key, value, _legal_text(key),
                    f"{where}的 name 与前面某套重复（{clean!r}）：重名的话指针没法按名字挑",
                )
            seen_names.add(clean)
            for field_name in ("base_url", "api_key", "model", "extra_body_json"):
                field_value = item.get(field_name, "")
                if field_value is None:
                    continue  # 显式 null 与省略同解（= 没配）
                if not isinstance(field_value, str):
                    raise ConfigValueError(
                        key, value, _legal_text(key),
                        f"{where}（{clean}）的 {field_name} 不是字符串",
                    )
                if field_name == "extra_body_json" and field_value.strip():
                    try:
                        extra = json.loads(field_value)
                    except json.JSONDecodeError as exc:
                        raise ConfigValueError(
                            key, value, _legal_text(key),
                            f"{where}（{clean}）的 extra_body_json 不是合法 JSON：{exc.msg}",
                        ) from exc
                    if not isinstance(extra, dict):
                        raise ConfigValueError(
                            key, value, _legal_text(key),
                            f"{where}（{clean}）的 extra_body_json 顶层不是对象",
                        )
            unknown = set(item) - {"name", "base_url", "api_key", "model", "extra_body_json"}
            if unknown:
                raise ConfigValueError(
                    key, value, _legal_text(key),
                    f"{where}（{clean}）带认不出的格：{'、'.join(sorted(str(u) for u in unknown))}"
                    "（每套只认 name / base_url / api_key / model / extra_body_json 这五格）",
                )
        return value

    if key == "summary.active_preset":
        # 空串 = 不选预设（平铺四键，存量配置零迁移）；「名字在不在清单里」归
        # validate_setting_changes（见那里的注释）。
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串")
        return value.strip()

    if key == "summary.extra_body_json":
        # 空串 = 不并；能解析但不是对象的（数组 / 数字 / "abc"）与解析不了的都在装载时拦下。
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
        # `null` 在这里是一个取值（关掉这一步）：原样收下 None，与设备键的「没配」各走各的。
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
    """键间规则（D6 / 方案 §5.3）：`asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。"""
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


# 生效配置对象


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
        """热读键清单（就是 `HOT_KEYS`）。"""
        return list(HOT_KEYS)

    def config_path(self) -> str:
        """本次读取用的配置文件路径。"""
        return self.path

    def source_of(self, key: str) -> str:
        return self.sources.get(key, "默认")

    def no_browser(self) -> bool:
        """`MEETING_NO_BROWSER=1` 关掉浏览器打开（测试与无头场景）。"""
        return os.environ.get(ENV_NO_BROWSER, "").strip() == "1"


# 读取入口

#: 已退场键的字面键名：存量 config.json 里读到即忽略（见 `load()`）；
#: 除此之外的未知键仍然严格拒绝。
_RETIRED_KEYS = frozenset({"output.root"})


def load(
    *,
    environ: dict[str, str] | None = None,
    path: str | None = None,
) -> Config:
    """启动时读一次配置：环境变量 > `config.json` > 默认值。

    `environ` / `path` 只服务测试注入。
    """
    env = os.environ if environ is None else environ
    config_file = config_path() if path is None else path

    file_values = _read_config_file(config_file)

    # 退场键读到即忽略 + 落一条 log，不抛 ConfigValueError；拼错键名的严格拒绝保留。
    for retired in _RETIRED_KEYS:
        if retired in file_values:
            logging.getLogger("meeting.config").warning(
                "config.json 里的已退场键 %r 已被忽略（该键已不再生效，可从配置文件删掉）",
                retired,
            )
            file_values = {k: v for k, v in file_values.items() if k != retired}

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


# 模块级状态（`get()` 的便捷入口）

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
    """热读键清单（就是 `HOT_KEYS`）。"""
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
    "summary_presets_of",
    "validate_setting_changes",
    "validate_value",
    "write_config_values",
]
