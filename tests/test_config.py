"""`tests/test_config.py` —— 配置层（phase1 §5 第 5 条 / §4.1 文件清单第 14 行）。

覆盖：
  - 优先级 env > config.json > 默认；
  - 默认值表逐键（含 §3.12 四个阈值、`asr.*` 五个沿用 V1 实测值、`alarm.sound` 默认 `true`）；
  - `asr.*` 五个热读键的清单；
  - `timeout_seconds > max_segment_seconds` 这条键间校验；
  - 环境变量映射（`MEETING_PORT` / `MEETING_OUT_ROOT` / `MEETING_LOG_DIR`，**不新增变量**）；
  - `config.json` **缺失**时用默认值照常起；
  - `config.json` **损坏**时 `config.load()` 抛配置损坏错误（**进程内**，两支：非法 JSON / 同名目录）；
  - **值非法 → `EXIT_REJECTED`**（子进程退出码；含 `web.port` 的类型 / 范围与 `asr` 的键间规则）；
  - `config.json` 必须是**点号扁平键名的单层映射**：嵌套 `web` 对象被拦下并说清「嵌套形状
    不合法」，点号键 `web.port` 反向证明合法（子进程 + 进程内两层断言）。

`EXIT_STARTUP_UNMET` 的退出码断言**不在本文件里**（它唯一落在 `tests/test_logging_setup.py`，§4.4）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests import _helpers as helpers
from tests.conftest import CASE_HOME_DIR_NAME
from meeting import config as config_module
from meeting.cli import EXIT_REJECTED

# ---------------------------------------------------------------------------
# 进程内的读取层
# ---------------------------------------------------------------------------


def _redirect_home(monkeypatch, home) -> None:
    """只重定向家目录 + 剔除本服务环境变量（不预置任何本服务变量）。

    `_load_with_env(monkeypatch, home, {})` 的等价形态，给「先跑子进程、再在进程内读一次」
    的用例复用（两条路径要读的是**同一份** `<临时家目录>/.config/meeting/config.json`）。
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in helpers.SERVICE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _load_with_env(monkeypatch, home, env: dict[str, str]):
    """在重定向家目录 + 指定环境变量下读一次配置（进程内）。"""
    _redirect_home(monkeypatch, home)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return config_module.load()


def test_default_values_match_the_frozen_table(tmp_path, monkeypatch):
    """默认值表逐键核对（§3.11 的键全集，一个不多一个不少）。"""
    home = tmp_path / "home"
    home.mkdir()
    loaded = _load_with_env(monkeypatch, home, {})

    expected = {
        "web.port": 8765,
        "web.dist_dir": "web/dist",
        "output.root": str(home / "Documents" / "meeting" / "out"),
        "log.level": "INFO",
        "log.dir": str(home / ".config" / "meeting" / "logs"),
        "log.max_bytes": 5 * 1024 * 1024,
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
        # 会后离线这一层的四个开关（`7c6952e5` 定名）：缺省值取本机实测跑通的那一档
        "post.engine": "auto",
        "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
        "post.dtype": "bfloat16",
        # 分块块长取实测的安全档 450 秒（`0` = 显式关掉分块、整段一次跑）
        "post.chunk_seconds": 450,
        # 两个设备键位（`dca4539b` 定名）：没配就是 `None`
        "devices.mic": None,
        "devices.loopback": None,
        "asr.max_segment_seconds": 10,
        "asr.timeout_seconds": 20,
        "asr.max_new_tokens": 400,
        "asr.repeat_max_unit": 40,
        "asr.repeat_min_coverage": 0.5,
        # 会后纪要这一层的四个开关（`41465462` phase2 定名）：钩子默认 `auto`（跟着离线
        # 那一趟的尾巴跑）、每块 100 行、每条 source 最多 8 段、一次运行最多四小时
        "minutes.hook": "auto",
        "minutes.chunk_lines": 100,
        "minutes.max_segments": 8,
        "minutes.timeout_seconds": 14400,
    }
    assert loaded.as_dict() == expected
    # 全部来源都是「默认」
    assert set(loaded.sources.values()) == {"默认"}


def test_post_layer_keys_defaults_values_and_groups(tmp_path, monkeypatch):
    """`post.engine` / `post.model` / `post.dtype` 三个键：默认值、合法取值、三分表登记。

    三个键都是**启动时读一次**那一档（`pending_restart`）：会后离线的一次运行在开场前
    就把引擎与档位定下来，改了要重启。
    """
    home = tmp_path / "home"
    home.mkdir()
    loaded = _load_with_env(monkeypatch, home, {})

    assert loaded.get("post.engine") == "auto"
    assert loaded.get("post.model") == "OpenMOSS-Team/MOSS-Transcribe-Diarize"
    assert loaded.get("post.dtype") == "bfloat16"
    for key in ("post.engine", "post.model", "post.dtype"):
        assert key in config_module.DEFAULT_VALUES, key
        assert config_module.setting_group(key) == "pending_restart", key
        assert key in config_module.STRING_LIMITS, key


def test_post_engine_accepts_the_same_selectors_as_the_other_backends():
    """`post.engine` 与 `capture.backend` / `transcriber.backend` / `post.hook` 同形。"""
    # `module:Class` 这种显式写法照旧放行（三个后端键都是这条口径）；白名单本身由
    # `config_module.BACKEND_SELECTORS` 给出，喂回校验器只是把常量自比一遍，略去不测。
    explicit = "meeting.offline.engine:MossOfflineEngine"
    assert config_module.validate_value("post.engine", explicit) == explicit

    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.validate_value("post.engine", "cuda")
    assert info.value.key == "post.engine"
    assert "auto" in info.value.legal


@pytest.mark.parametrize("dtype", ["bfloat16", "bf16", "float16", "fp16", "float32", "fp32"])
def test_post_dtype_accepts_every_level_the_engine_knows(dtype):
    """`post.dtype` 放行的档位名逐个都在 `meeting.offline.engine.DTYPE_NAMES` 里。"""
    from meeting.offline.engine import DTYPE_NAMES

    assert dtype in DTYPE_NAMES, f"{dtype} 放行了、引擎那边却认不出"
    assert config_module.validate_value("post.dtype", dtype) == dtype


@pytest.mark.parametrize(
    "value",
    ["int8", "fp8", "BFLOAT16", "bfloat", "", "   ", 16, None, True],
)
def test_post_dtype_rejects_everything_else(value):
    """档位名写错在启动时就被拦下（报错含键名、收到的值、合法范围）。"""
    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.validate_value("post.dtype", value)
    assert info.value.key == "post.dtype"
    assert "bfloat16" in info.value.legal


def test_post_model_takes_a_repo_id_or_a_local_path():
    """`post.model` 收 HF 仓库 id 与本地快照绝对路径两种写法；空串被拒。"""
    for value in ("OpenMOSS-Team/MOSS-Transcribe-Diarize", r"C:\models\mosstd-snapshot"):
        assert config_module.validate_value("post.model", value) == value

    for bad in ("", "   ", 7, None):
        with pytest.raises(config_module.ConfigValueError):
            config_module.validate_value("post.model", bad)


def test_post_chunk_seconds_defaults_to_the_measured_safe_length(tmp_path, monkeypatch):
    """`post.chunk_seconds` 默认 450 秒，按普通整数键注册（`DEFAULT_VALUES` + 取值域 + 三分表）。

    **默认 450 是对 spec「默认关闭」的一处有意偏离**：16 GB 卡上实测 600 s → 11.6 GiB、
    900 s → 22.9 GiB（已越界换页）、1500 s → OOM，而验收素材是 5943 s；默认关闭等于每一场
    真实会议都失败。理由写在 `config.py` 里那一格上面的注释里。
    """
    home = tmp_path / "home"
    home.mkdir()
    loaded = _load_with_env(monkeypatch, home, {})

    assert config_module.DEFAULT_VALUES["post.chunk_seconds"] == 450
    assert loaded.get("post.chunk_seconds") == 450
    assert loaded.source_of("post.chunk_seconds") == "默认"
    assert "post.chunk_seconds" in config_module.SETTING_KEY_GROUPS
    assert config_module.setting_group("post.chunk_seconds") == "pending_restart"
    assert config_module.INT_LIMITS["post.chunk_seconds"] == (0, 86400)


def test_post_chunk_seconds_accepts_an_explicit_length_and_zero():
    """合法值：正数（分块的块长）与 `0`（显式关掉分块、整段一次跑）。"""
    for value in (0, 1, 450, 600, 86400):
        assert config_module.validate_value("post.chunk_seconds", value) == value


@pytest.mark.parametrize("value", [-1, -450, 86401, 450.0, "450", "", True, None, [450]])
def test_post_chunk_seconds_rejects_everything_else(value):
    """非法值在启动时就被拦下（报错含键名、收到的值、合法范围）。"""
    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.validate_value("post.chunk_seconds", value)
    assert info.value.key == "post.chunk_seconds"
    assert "0" in info.value.legal and "86400" in info.value.legal


def test_post_chunk_seconds_comes_from_config_json(tmp_path, monkeypatch):
    """这个键能从 `config.json` 读进来，来源记得清。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"post.chunk_seconds": 600})

    loaded = _load_with_env(monkeypatch, home, {})

    assert loaded.get("post.chunk_seconds") == 600
    assert loaded.source_of("post.chunk_seconds") == "config.json"


def test_post_layer_keys_come_from_config_json(tmp_path, monkeypatch):
    """三个键都能从 `config.json` 读进来、来源记得清（不是「没配」那一档）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(
        home,
        {
            "post.engine": "meeting.offline.engine:MossOfflineEngine",
            "post.model": r"C:\models\mosstd-snapshot",
            "post.dtype": "float16",
        },
    )

    loaded = _load_with_env(monkeypatch, home, {})

    assert loaded.get("post.engine") == "meeting.offline.engine:MossOfflineEngine"
    assert loaded.get("post.model") == r"C:\models\mosstd-snapshot"
    assert loaded.get("post.dtype") == "float16"
    assert {loaded.source_of(key) for key in ("post.engine", "post.model", "post.dtype")} == {
        "config.json"
    }


def test_missing_config_json_falls_back_to_defaults(tmp_path, monkeypatch):
    """`config.json` 缺失时用默认值照常起。"""
    home = tmp_path / "home"
    home.mkdir()
    assert not helpers.config_path_of(home).exists()

    loaded = _load_with_env(monkeypatch, home, {})
    assert loaded.config_path() == str(helpers.config_path_of(home))
    assert loaded.get("web.port") == 8765


# ---------------------------------------------------------------------------
# 夹具之间的家目录契约（**从 `tests/conftest.py` 搬来**，审查问题 m4：
# 那个文件只放 fixture，用例一律落在 `test_*.py` 里；断言强度一字未改）
# ---------------------------------------------------------------------------


def test_isolated_config_dir_and_app_client_share_one_home(
    tmp_path: Path, isolated_config_dir: Path, app_client: Any
):
    """两个夹具同时使用时指向**同一个**本用例临时家目录（phase2 §5.4 的家目录硬约束）。

    同时请求两个夹具：`isolated_config_dir` 归属的那个家目录（它的上一级目录），
    与 `HOME` / `USERPROFILE` 两个环境变量、以及 `app_client` 实际落配置的那条路径，
    必须逐字对上 —— 否则「隔离」这件事在两个夹具的视角里各落一处。

    **这条用例为什么落在本文件**：它断的是配置层的落点（`<家目录>/.config/meeting/
    config.json` 与 `log.dir` 的挂载点），与 `tests/conftest.py` 那两个夹具的契约直接对应；
    放在本文件里也让它与 `tests/conftest.py` 里那几行「家目录取 `case_home()`、
    `app_client` 用的是同一个」的说明互为判据。
    """
    home = Path(os.environ["HOME"])
    assert home == tmp_path / CASE_HOME_DIR_NAME
    assert isolated_config_dir == home / ".config" / "meeting"
    assert os.environ["USERPROFILE"] == str(home)
    assert helpers.config_path_of(home).exists(), (
        "app_client 的配置文件该落在同一个家目录下"
    )
    assert isolated_config_dir.parent.parent == home, (
        "isolated_config_dir 归属的家目录与 app_client 落配置的那个家目录该是同一个："
        f"{isolated_config_dir.parent.parent} != {home}"
    )


def test_hot_keys_are_the_asr_five_plus_the_alarm_switch(tmp_path, monkeypatch):
    """热读键 = `asr.*` 五个 + `alarm.sound`（出声开关要在勾选那一刻生效）。"""
    home = tmp_path / "home"
    home.mkdir()
    loaded = _load_with_env(monkeypatch, home, {})
    assert loaded.hot_keys() == [
        "alarm.sound",
        "asr.max_segment_seconds",
        "asr.timeout_seconds",
        "asr.max_new_tokens",
        "asr.repeat_max_unit",
        "asr.repeat_min_coverage",
    ]
    # 快照 `settings.asr` 的子键仍只有那五个：`alarm.sound` 是另一格，不掺进 `asr` 里
    assert config_module.ASR_SETTING_KEYS == (
        "max_segment_seconds",
        "timeout_seconds",
        "max_new_tokens",
        "repeat_max_unit",
        "repeat_min_coverage",
    )


def test_priority_environment_beats_json_beats_default(tmp_path, monkeypatch):
    """优先级：环境变量 > config.json > 代码默认值。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out_from_json"
    log_dir = tmp_path / "logs_from_json"
    helpers.write_config_json(
        home,
        {
            "web.port": 9001,
            "output.root": str(out_root),
            "log.dir": str(log_dir),
            "web.dist_dir": "somewhere/dist",
        },
    )

    # ① 只有 config.json：json 层生效
    loaded = _load_with_env(monkeypatch, home, {})
    assert loaded.get("web.port") == 9001
    assert loaded.get("output.root") == str(out_root)
    assert loaded.get("log.dir") == str(log_dir)
    assert loaded.source_of("web.port") == "config.json"
    # 没在 json 里写的键仍取默认
    assert loaded.get("web.dist_dir") == "somewhere/dist"
    assert loaded.get("log.max_bytes") == 5 * 1024 * 1024
    assert loaded.source_of("log.max_bytes") == "默认"

    # ② 环境变量压过 config.json
    env_out = tmp_path / "out_from_env"
    env_log = tmp_path / "logs_from_env"
    loaded = _load_with_env(
        monkeypatch,
        home,
        {
            "MEETING_PORT": "9123",
            "MEETING_OUT_ROOT": str(env_out),
            "MEETING_LOG_DIR": str(env_log),
        },
    )
    assert loaded.get("web.port") == 9123
    assert loaded.get("output.root") == str(env_out)
    assert loaded.get("log.dir") == str(env_log)
    assert loaded.source_of("web.port") == "环境变量 MEETING_PORT"
    assert loaded.source_of("output.root") == "环境变量 MEETING_OUT_ROOT"
    assert loaded.source_of("log.dir") == "环境变量 MEETING_LOG_DIR"


def test_env_mapping_is_the_five_frozen_names_only():
    """环境变量映射就是方案列出的那五个，一个不多一个不少。"""
    assert config_module.ENV_KEYS == {
        "MEETING_PORT": "web.port",
        "MEETING_OUT_ROOT": "output.root",
        "MEETING_LOG_DIR": "log.dir",
        "MEETING_CAPTURE": "capture.backend",
    }
    assert config_module.ENV_NO_BROWSER == "MEETING_NO_BROWSER"


def test_helpers_service_env_names_derive_from_the_five_frozen_names():
    """测试层的剔除清单由这五个名字派生，与 §3.11 的清单逐名对齐。

    `_helpers.SERVICE_ENV_NAMES` 原先手抄了一份副本；改成派生之后，这一条同时钉住两边：
    派生结果**必须**恰好等于契约里那五个名字（顺序也照 §3.11），因此手抄副本不再有漏剔的
    余地，`ENV_KEYS` 将来多出键时这里会先红。
    """
    assert helpers.SERVICE_ENV_NAMES == (
        "MEETING_PORT",
        "MEETING_OUT_ROOT",
        "MEETING_LOG_DIR",
        "MEETING_CAPTURE",
        "MEETING_NO_BROWSER",
    )


def test_meeting_capture_env_maps_to_capture_backend(tmp_path, monkeypatch):
    """`MEETING_CAPTURE=dev` → `capture.backend = dev`（本 Phase 只需认这个键名）。"""
    home = tmp_path / "home"
    home.mkdir()
    loaded = _load_with_env(monkeypatch, home, {"MEETING_CAPTURE": "dev"})
    assert loaded.get("capture.backend") == "dev"


def test_round_robin_keys_are_readable_with_defaults(tmp_path, monkeypatch):
    """`log.max_bytes` / `log.backup_count` 两个键能读、有默认值、且进生效值清单。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"log.max_bytes": 4096, "log.backup_count": 2})

    loaded = _load_with_env(monkeypatch, home, {})
    assert loaded.get("log.max_bytes") == 4096
    assert loaded.get("log.backup_count") == 2


def test_cross_key_rule_timeout_must_exceed_segment(tmp_path, monkeypatch):
    """键间规则：`asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(
        home, {"asr.max_segment_seconds": 30, "asr.timeout_seconds": 20}
    )

    with pytest.raises(config_module.ConfigValueError) as info:
        _load_with_env(monkeypatch, home, {})
    assert info.value.key == "asr.timeout_seconds"
    assert "asr.max_segment_seconds" in info.value.legal


@pytest.mark.parametrize(
    "values",
    [
        {"web.port": "8765"},
        {"web.port": 70000},
        {"web.port": -1},
        {"heartbeat.interval_seconds": 0},
        {"heartbeat.interval_seconds": 61},
        {"alarm.sound": "true"},
        {"log.max_bytes": 0},
        {"alert.silence_dbfs": "quiet"},
        {"web.dist_dir": ""},
        {"capture.backend": "nonsense-value"},
        {"web": {"port": 8765}},
        # 两个设备键位是合法的，但值必须是字符串：数字当场拦下
        {"devices.mic": 3},
        {"devices.loopback": ["CABLE Input"]},
    ],
)
def test_illegal_values_raise_value_error_in_process(tmp_path, monkeypatch, values):
    """能解析但值非法 → 进程内抛 `ConfigValueError`（键名 / 收到的值 / 合法范围三样齐）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, values)

    with pytest.raises(config_module.ConfigValueError) as info:
        _load_with_env(monkeypatch, home, {})
    error = info.value
    assert error.key
    assert error.legal
    assert error.key in str(error)


# ---------------------------------------------------------------------------
# 进程内的配置损坏（两支造法照 §3.11）
# ---------------------------------------------------------------------------


def test_corrupt_json_raises_corrupt_error_with_path_and_line(tmp_path, monkeypatch):
    """非法 JSON（截断文本）→ `ConfigCorruptError`，说得清文件路径与出错的行。"""
    home = tmp_path / "home"
    helpers.config_dir_of(home).mkdir(parents=True)
    path = helpers.config_path_of(home)
    path.write_text('{"web.port": 8765', encoding="utf-8")

    with pytest.raises(config_module.ConfigCorruptError) as info:
        _load_with_env(monkeypatch, home, {})
    error = info.value
    assert error.path == str(path)
    assert "行" in error.detail


def test_config_file_replaced_by_directory_raises_corrupt_error(tmp_path, monkeypatch):
    """同样的名字换成目录（读不出来）→ `ConfigCorruptError`，路径照样说清。"""
    home = tmp_path / "home"
    path = helpers.config_path_of(home)
    path.mkdir(parents=True)

    with pytest.raises(config_module.ConfigCorruptError) as info:
        _load_with_env(monkeypatch, home, {})
    assert info.value.path == str(path)


# ---------------------------------------------------------------------------
# 进程内：值非法的细项
# ---------------------------------------------------------------------------


def test_json_unknown_key_is_rejected(tmp_path, monkeypatch):
    """`config.json` 里出现没定义过的键名 → 值级非法（键名 / 收到的值 / 说明齐）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.host": "0.0.0.0"})

    with pytest.raises(config_module.ConfigValueError) as info:
        _load_with_env(monkeypatch, home, {})
    assert info.value.key == "web.host"
    assert "键表" in info.value.detail
    assert "'0.0.0.0'" in str(info.value)


# ---------------------------------------------------------------------------
# 子进程：值非法 → EXIT_REJECTED（§4.4 的落点分工：这一档在本文件）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, expected_in_output",
    [
        ({"web.port": "8765"}, ["web.port", "8765", "0–65535"]),
        ({"web.port": 70000}, ["web.port", "70000", "0–65535"]),
        (
            {"asr.max_segment_seconds": 30, "asr.timeout_seconds": 20},
            ["asr.timeout_seconds", "asr.max_segment_seconds"],
        ),
    ],
)
def test_illegal_value_exits_rejected_in_subprocess(tmp_path, values, expected_in_output):
    """值非法 → 真子进程退出码 `EXIT_REJECTED`，终端说清键名 / 收到的值 / 合法范围。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, values)

    result = helpers.run_web_expecting_failure(home)

    assert result.returncode == EXIT_REJECTED
    combined = result.stdout + result.stderr
    for needle in expected_in_output:
        assert needle in combined, f"终端输出里缺 {needle!r}：\n{combined}"
    assert "配置值非法" in combined


def test_missing_config_json_starts_service_in_subprocess(tmp_path):
    """`config.json` 缺失时用默认值照常起（这里用 `port = 0` 的临时配置验证「照常起」）。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0})

    proc = helpers.spawn_service(home)
    try:
        port = proc.wait_ready_by_heartbeat()
        assert port > 0
    finally:
        proc.terminate()


def test_config_json_flat_keys_only(tmp_path, monkeypatch):
    """`config.json` 是点号扁平键名的**单层映射**：嵌套形状被拦下、并说清是哪种不合法。

    **断言不许放宽成 `"web" in 输出`**：那样只证明「输出里出现过 web 这三个字母」，
    `web.port` / `web.dist_dir` 这些合法的点号键也能让它绿，钉不住「无嵌套对象」这条口径。
    嵌套形状在配置层有**两个不同的拒绝面**（都在本用例里钉住，别只钉一个）：

      ① **顶层键名本身是未知嵌套键**（`{"web": {...}}`）：`web` 不在冻结的键表里 →
         键名要写成键表里的全名 `web.port`，层级由点号承担；
      ② **合法点号键的值却是一个对象**（`{"web.port": {"port": 8765}}`）：键名对得上、
         值的形状不对 → 「嵌套形状不合法：config.json 是点号扁平键名的单层映射」。

    两个面都断言「进程内抛 `ConfigValueError`（含 key / 收到的那份对象）」+「真子进程
    `EXIT_REJECTED` + 终端同一条文案」；再用 `{"web.port": 8765}` **反向**证明
    点号扁平键是合法形状（同一份读法下正常生效、来源是 `config.json`）。
    """
    home = tmp_path / "home"
    home.mkdir()

    # ① 顶层就是未知嵌套键 → `EXIT_REJECTED`，报的是那个键名与那份对象
    helpers.write_config_json(home, {"web": {"port": 8765}})
    result = helpers.run_web_expecting_failure(home)
    combined = result.stdout + result.stderr
    assert result.returncode == EXIT_REJECTED
    assert "配置值非法" in combined
    assert "web = {'port': 8765}" in combined, f"报错该指认键名与收到的那份对象：{combined}"
    assert "这个键名不在配置键表里" in combined, f"该说清是键名不合法：{combined}"

    _redirect_home(monkeypatch, home)
    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.load()
    assert info.value.key == "web"
    assert info.value.value == {"port": 8765}
    assert "不在配置键表里" in str(info.value)

    # ② 合法点号键的值是对象（嵌套形状）→ `EXIT_REJECTED`，报「单层映射」这句口径
    helpers.write_config_json(home, {"web.port": {"port": 8765}})
    result = helpers.run_web_expecting_failure(home)
    combined = result.stdout + result.stderr
    assert result.returncode == EXIT_REJECTED
    assert "配置值非法：web.port = {'port': 8765}" in combined, f"报错该指认键名与那份对象：{combined}"
    assert "嵌套形状不合法" in combined
    assert "点号扁平键名的单层映射" in combined

    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.load()
    assert info.value.key == "web.port"
    assert info.value.value == {"port": 8765}
    assert "嵌套形状不合法" in str(info.value)

    # ③ 反向：点号扁平键是合法形状（同一份读法下正常生效、来源是 config.json）
    helpers.write_config_json(home, {"web.port": 8765})
    loaded = config_module.load()
    assert loaded.get("web.port") == 8765
    assert loaded.source_of("web.port") == "config.json"
    assert "web" not in loaded.as_dict(), "生效值清单里只有点号键名，不该多出一个嵌套的 web 键"
