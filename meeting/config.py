"""配置层（phase1 §3.11，B11 的正确边界）。

位置：`~/.config/meeting/config.json`
  - `config.json` 是**点号扁平键名的单层映射**（例如 `{"web.port": 8765}`），
    嵌套形状不合法。
  - 不迁移 V1 的 `~/.config/meeting_notes/`，也不做任何自动改写。

优先级：**环境变量 > `config.json` > 代码默认值**（沿用 V1 契约，逐字照搬 B11）。

热读范围：**只有 `asr.*` 那五个键**是每段重新读（改了立刻生效）；
其余键（`web.*` / `output.root` / `log.*` / `alert.*` / `alarm.*` / `heartbeat.*` /
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

#: 五个热读键（每段重新读，改了立刻生效）
HOT_KEYS: tuple[str, ...] = (
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
    # 两路各自用哪个设备（`dca4539b` 定名）：`None` = 按默认策略挑
    # （mic 挡掉抓取型虚拟端点后按 host API 排序试开；loopback 优先 `CABLE Input` 且不含 16ch）
    "devices.mic": None,
    "devices.loopback": None,
    "asr.max_segment_seconds": 10,
    "asr.timeout_seconds": 20,
    "asr.max_new_tokens": 400,
    "asr.repeat_max_unit": 40,
    "asr.repeat_min_coverage": 0.5,
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
}

#: 浮点范围
FLOAT_LIMITS: dict[str, tuple[float, float]] = {
    "alert.silence_dbfs": (-200.0, 100.0),
    "asr.repeat_min_coverage": (0.0, 1.0),
}

#: 字符串键的合法取值说明（`None` = 任意非空字符串）
STRING_LIMITS: dict[str, tuple[str, ...] | None] = {
    "web.dist_dir": None,
    "output.root": None,
    "log.dir": None,
    "log.level": None,
    "capture.backend": BACKEND_SELECTORS,
    "transcriber.backend": BACKEND_SELECTORS,
    "post.hook": BACKEND_SELECTORS,
    # 两个设备键位（`dca4539b` 定名）：任意非空字符串（精确名，MME 端点名可前缀兜）
    "devices.mic": None,
    "devices.loopback": None,
}

#: 可空键：**设备名没配就是 `None`**（空值形状两端一致 —— 快照 `settings.devices` 的子键在
#: 没配时是 `null`，配置文件里也写 `null`；`validate_value()` 把空字符串与纯空白串一并归一成
#: `None`，省得「空串」与「没配」在快照里长出两种形状）
NULLABLE_KEYS: tuple[str, ...] = ("devices.mic", "devices.loopback")

#: 环境变量映射（方案明确列出的五个，一个不多一个不少）
ENV_KEYS: dict[str, str] = {
    "MEETING_PORT": "web.port",
    "MEETING_OUT_ROOT": "output.root",
    "MEETING_LOG_DIR": "log.dir",
    "MEETING_CAPTURE": "capture.backend",
}

#: 关掉浏览器打开的开关（值为 `1` 时生效）
ENV_NO_BROWSER = "MEETING_NO_BROWSER"

# ---------------------------------------------------------------------------
# `settings_update` 的键态三分表（phase2 §3.5.5）
#
# **只有下面逐条列出的字面键名合法，不按前缀归组、也没有任何前缀白名单**；
# 表外的任何键名一律 `invalid_setting`(400)。
# 23 个名字逐一取自上面的键全集（phase1 定死的顶层键 + `asr.*` 五个子键 +
# `dca4539b` 定名的 `devices.mic` / `devices.loopback` 两个设备键位）。
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
ASR_SETTING_KEYS: tuple[str, ...] = tuple(key.split(".", 1)[1] for key in HOT_KEYS)

#: 快照 `settings.devices` 的子键 ↔ 配置键（`dca4539b` 定名，口径与 `ASR_SETTING_KEYS` 同）：
#: `devices.mic` / `devices.loopback` 两个点号键位，快照里收成 `settings.devices.mic` /
#: `settings.devices.loopback` 两个子键。取值的空值形状两端一致：没配即 `null`。
DEVICE_SETTING_KEYS: tuple[str, ...] = tuple(
    key.split(".", 1)[1] for key in NULLABLE_KEYS
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
    if key in STRING_LIMITS:
        allowed = STRING_LIMITS[key]
        text = (
            "非空字符串"
            if allowed is None
            else "字符串，取值 " + " / ".join(allowed) + "（或 module:Class 这种显式写法）"
        )
        if key in NULLABLE_KEYS:
            return f"{text}，或 null（null = 没配，按默认策略挑）"
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

    if key in NULLABLE_KEYS:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigValueError(key, value, _legal_text(key), "收到的不是字符串")
        text = value.strip()
        return text or None

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
        """返回 `asr.*` 五个键（供 log 里「哪些键是热读的」那一行）。"""
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
    """`asr.*` 五个热读键。"""
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
    "DEVICE_SETTING_KEYS",
    "ENV_KEYS",
    "ENV_NO_BROWSER",
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
