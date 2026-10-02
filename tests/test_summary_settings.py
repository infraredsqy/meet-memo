"""`meeting/summary/settings.py` 的契约用例：七个键怎么读、怎么归一、缺哪几个。

盯的是五件事：

  - **四种来源都收**：`ReadOnlyConfig`（服务 `bind()` 给后端的那种只读视图）、
    `meeting.config.Config`（`load()` 的产物）、一份裸 dict、`None`（走默认值表）；
  - **归一化**：`extra_body_json` 的空串与 `None` 两处同解、非对象文本按「没配」收；
    `default_window` 认不出的落缺省档；`deadline_seconds` 的非正数落缺省档；
    `max_attempts` 至少一次；
  - **`missing` 点名的是配置键全名**（动作层拿它说「缺哪几个键」）；
  - **端点配齐之后 `ready` 为真**，一个键都不缺；
  - **七个键在配置层真的登记了**（默认值表、取值域、环境变量映射、三分表四处都能查到）——
    这一层只是读者，键的落地归 `meeting/config.py`。
"""

from __future__ import annotations

from meeting.backends.base import ReadOnlyConfig
from meeting import config as config_module
from meeting.summary import settings as settings_module
from meeting.summary.settings import (
    API_KEY_KEY,
    BASE_URL_KEY,
    DEFAULT_WINDOW,
    DEFAULT_WINDOW_KEY,
    MODEL_KEY,
    SETTING_KEYS,
    SummarySettings,
    normalize_window,
    parse_extra_body,
    window_seconds_of,
)

#: 一份配齐的只读视图（四个端点字段都在）
FULL_VALUES = {
    "summary.base_url": "https://api.example.invalid/v1",
    "summary.api_key": "test-key-not-a-real-one",
    "summary.model": "example-model",
    "summary.extra_body_json": '{"thinking": {"type": "disabled"}}',
    "summary.deadline_seconds": 12.5,
    "summary.max_attempts": 2,
    "summary.default_window": "30m",
}


def test_from_config_reads_a_read_only_view() -> None:
    """`ReadOnlyConfig`（服务 `bind()` 给后端的那一种）七个键都读得到、都归一好了。"""
    settings = SummarySettings.from_config(ReadOnlyConfig(values=dict(FULL_VALUES)))

    assert settings.base_url == "https://api.example.invalid/v1"
    assert settings.api_key == "test-key-not-a-real-one"
    assert settings.model == "example-model"
    assert settings.extra_body == {"thinking": {"type": "disabled"}}
    assert settings.deadline_seconds == 12.5
    assert settings.max_attempts == 2
    assert settings.default_window == "30m"
    assert settings.missing == ()
    assert settings.ready is True


def test_from_config_reads_a_plain_dict() -> None:
    """一份裸 dict 同样收（探针与用例最省事的造法）。"""
    settings = SummarySettings.from_config({"summary.base_url": "http://127.0.0.1:9/v1"})

    assert settings.base_url == "http://127.0.0.1:9/v1"
    assert settings.model == config_module.default_for(MODEL_KEY)


def test_from_config_without_a_source_uses_the_default_table() -> None:
    """什么都不给：七个键都落 `meeting/config.py` 的默认值。"""
    settings = SummarySettings.from_config(None)

    assert settings.base_url == config_module.default_for(BASE_URL_KEY)
    assert settings.api_key == ""
    assert settings.model == config_module.default_for(MODEL_KEY)
    assert settings.extra_body is None
    assert settings.deadline_seconds == 25.0
    assert settings.max_attempts == 3
    assert settings.default_window == DEFAULT_WINDOW


def test_missing_names_the_configuration_keys_in_order() -> None:
    """`missing` 报的是**配置键全名**，次序是端点三件套那一序。"""
    settings = SummarySettings(base_url="", api_key="", model="")

    assert settings.missing == (BASE_URL_KEY, API_KEY_KEY, MODEL_KEY)
    assert settings.ready is False


def test_missing_ignores_whitespace_only_values() -> None:
    """只有空白的那几个字段与「没配」同解（空值形状只有一个）。"""
    settings = SummarySettings(base_url="  ", api_key="\t", model=" ")

    assert settings.missing == (BASE_URL_KEY, API_KEY_KEY, MODEL_KEY)


def test_missing_reports_only_the_gap() -> None:
    """缺一个就只报那一个（动作层那句话因此能说到点上）。"""
    settings = SummarySettings(base_url="https://example.invalid/v1", api_key="", model="m")

    assert settings.missing == (API_KEY_KEY,)


def test_extra_body_empty_and_none_are_the_same_thing() -> None:
    """`extra_body_json` 的空串与 `None` 两处同解：都是「不并」。"""
    assert parse_extra_body("") is None
    assert parse_extra_body("   ") is None
    assert parse_extra_body(None) is None


def test_extra_body_reads_a_json_object() -> None:
    """一段 JSON 对象的文本翻成 dict（键值原样保留，两份都给全）。"""
    assert parse_extra_body('{"thinking": {"type": "disabled"}, "top_p": 0.9}') == {
        "thinking": {"type": "disabled"},
        "top_p": 0.9,
    }


def test_extra_body_treats_a_readable_non_object_as_unset() -> None:
    """能解析但不是对象的（数组、数字、字符串）按「没配」收：一段不是请求体的文本。"""
    assert parse_extra_body("[1, 2]") is None
    assert parse_extra_body("42") is None
    assert parse_extra_body('"abc"') is None


def test_extra_body_treats_an_unreadable_text_as_unset() -> None:
    """读不出来的文本也按「没配」收（装载那一层已经拦过一道，这里兜手写的配置）。"""
    assert parse_extra_body("{不是 json}") is None


def test_normalize_window_keeps_the_four_choices() -> None:
    """四档取值原样收下。"""
    assert [normalize_window(choice) for choice in ("5m", "15m", "30m", "full")] == [
        "5m",
        "15m",
        "30m",
        "full",
    ]


def test_normalize_window_falls_back_for_anything_else() -> None:
    """认不出的取值（`7m` / 空 / `None`）落缺省档，不让它把这一层带崩。"""
    assert normalize_window("7m") == DEFAULT_WINDOW
    assert normalize_window("") == DEFAULT_WINDOW
    assert normalize_window(None) == DEFAULT_WINDOW
    assert normalize_window("FULL") == DEFAULT_WINDOW


def test_window_seconds_maps_the_four_choices() -> None:
    """四档的秒数：5 / 15 / 30 分钟，`full` 是 `None`（整场取全部）。"""
    assert window_seconds_of("5m") == 300.0
    assert window_seconds_of("15m") == 900.0
    assert window_seconds_of("30m") == 1800.0
    assert window_seconds_of("full") is None


def test_deadline_and_attempts_fall_back_for_nonsense_values() -> None:
    """预算与尝试次数收到没道理的值时落缺省档（0 秒的预算、0 次尝试都跑不起来）。"""
    settings = SummarySettings.from_config(
        {"summary.deadline_seconds": 0, "summary.max_attempts": 0}
    )

    assert settings.deadline_seconds == 25.0
    assert settings.max_attempts == 1

    broken = SummarySettings.from_config(
        {"summary.deadline_seconds": "很久", "summary.max_attempts": "很多"}
    )
    assert broken.deadline_seconds == 25.0
    assert broken.max_attempts == 3


def test_setting_keys_lists_the_seven_names() -> None:
    """七个键名按「端点 → 预算 → 缺省档」这一序排。"""
    assert SETTING_KEYS == (
        "summary.base_url",
        "summary.api_key",
        "summary.model",
        "summary.extra_body_json",
        "summary.deadline_seconds",
        "summary.max_attempts",
        "summary.default_window",
    )


def test_the_seven_keys_are_registered_in_the_config_layer() -> None:
    """七个键在配置层四处都有登记：默认值、三分表、取值域、环境变量那一格。

    `summary.default_window` 原与同组键一起归 `pending_restart`；设置抽屉节点（`311f84f5`）
    把它改收热读档（界面的缺省预选随快照走），同组其余六键不动。
    """
    for key in SETTING_KEYS:
        assert key in config_module.DEFAULT_VALUES, key
        assert key in config_module.SETTING_KEY_GROUPS, key
        if key == DEFAULT_WINDOW_KEY:
            assert config_module.SETTING_KEY_GROUPS[key] == "applied", key
        else:
            assert config_module.SETTING_KEY_GROUPS[key] == "pending_restart", key

    # 取值域：三个字符串键、一个浮点键、一个整数键各有自己的档
    assert config_module.STRING_LIMITS["summary.default_window"] == ("5m", "15m", "30m", "full")
    assert "summary.deadline_seconds" in config_module.FLOAT_LIMITS
    assert "summary.max_attempts" in config_module.INT_LIMITS
    assert API_KEY_KEY in config_module.NULLABLE_KEYS

    # 环境变量：密钥那一格
    assert config_module.ENV_KEYS["MEETING_SUMMARY_API_KEY"] == API_KEY_KEY
    assert config_module.ENV_SUMMARY_API_KEY == "MEETING_SUMMARY_API_KEY"


def test_the_keys_are_absent_from_the_snapshot_subsets() -> None:
    """`summary.*` 一个都不许混进快照那两格子键清单里（它们只出现在 `settings_update` 的键面）。"""
    assert not any(key.startswith("summary.") for key in config_module.ASR_SETTING_KEYS)
    assert not any(key.startswith("summary.") for key in config_module.DEVICE_SETTING_KEYS)
    assert set(config_module.DEVICE_SETTING_KEYS) == {"mic", "loopback"}


def test_load_reads_the_keys_from_a_temp_config_file(tmp_path, monkeypatch) -> None:
    """从一份临时 `config.json` 装载（走 `load()` 那条真路），非法值当场被拒。"""
    home = tmp_path / "home"
    (home / ".config" / "meeting").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    config_path = home / ".config" / "meeting" / "config.json"
    config_path.write_text(
        '{"summary.default_window": "30m", "summary.deadline_seconds": 8,'
        ' "summary.extra_body_json": "{\\"thinking\\": {\\"type\\": \\"disabled\\"}}"}',
        encoding="utf-8",
    )
    loaded = config_module.load()

    settings = SummarySettings.from_config(loaded)
    assert settings.default_window == "30m"
    assert settings.deadline_seconds == 8.0
    assert settings.extra_body == {"thinking": {"type": "disabled"}}


def test_load_rejects_an_illegal_window_and_a_non_object_extra_body(
    tmp_path, monkeypatch
) -> None:
    """两个非法值各被 `validate_value()` 拒掉：`default_window = "7m"`、
    `extra_body_json` 给一段不是 JSON 对象的文本。"""
    home = tmp_path / "home"
    (home / ".config" / "meeting").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config_path = home / ".config" / "meeting" / "config.json"

    for payload in (
        '{"summary.default_window": "7m"}',
        '{"summary.extra_body_json": "[1, 2]"}',
        '{"summary.extra_body_json": "不是 JSON"}',
    ):
        config_path.write_text(payload, encoding="utf-8")
        try:
            config_module.load()
        except config_module.ConfigValueError as exc:
            assert "summary." in str(exc)
        else:  # pragma: no cover - 走不到这里
            raise AssertionError(f"这个值该被拒掉：{payload}")


def test_the_api_key_can_come_from_the_environment(tmp_path, monkeypatch) -> None:
    """密钥那一格走环境变量：`MEETING_SUMMARY_API_KEY` 压住配置文件（优先级最高）。

    密钥只经环境变量进进程 —— 这一条同时说明「配置文件里不写密钥」这条路真的通。
    """
    home = tmp_path / "home"
    (home / ".config" / "meeting").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".config" / "meeting" / "config.json").write_text(
        '{"summary.api_key": "from-file"}', encoding="utf-8"
    )
    monkeypatch.setenv("MEETING_SUMMARY_API_KEY", "from-env")

    loaded = config_module.load()

    assert loaded.get("summary.api_key") == "from-env"
    assert loaded.source_of("summary.api_key") == "环境变量 MEETING_SUMMARY_API_KEY"
    assert SummarySettings.from_config(loaded).api_key == "from-env"


def test_settings_module_reexports_the_seven_key_names() -> None:
    """本模块把七个键名当常量再导出一遍（调用方不必自己拼字符串）。"""
    assert settings_module.BASE_URL_KEY == "summary.base_url"
    assert settings_module.MODEL_KEY == "summary.model"
    assert settings_module.EXTRA_BODY_KEY == "summary.extra_body_json"
    assert settings_module.DEADLINE_KEY == "summary.deadline_seconds"
    assert settings_module.MAX_ATTEMPTS_KEY == "summary.max_attempts"
    assert settings_module.DEFAULT_WINDOW_KEY == "summary.default_window"
