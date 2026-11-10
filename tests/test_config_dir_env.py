"""`MEETING_CONFIG_DIR`：配置目录整组重定向（pytest 之外的探针与手工跑一行隔离）。

三档口径（节点 spec）：
  - **设了它**：`config_dir()` 整组跟着走——`config.json`、`workspaces.json`、
    会后任务记录与日志默认落点全从这一个函数推出；
  - **不设它**：家目录推导（`HOME` → `USERPROFILE`）与改动前逐字相同；
  - **空串按没设处理**。

显式 `log.dir`（config.json 或 `MEETING_LOG_DIR`）优先级不改：那一条由
`tests/test_config.py` 与 `tests/test_cli_post_config.py` 的既有用例钉着，这里不重复。
子进程端到端的落点断言由节点探针（可重跑脚本）负责，这里只钉进程内推导这一层。
"""

from __future__ import annotations

from pathlib import Path

from meeting import config as config_module


def _redirect_home(tmp_path: Path, monkeypatch) -> Path:
    """本用例自己的临时家目录（后设的覆盖 autouse 先设的那一份）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_set_env_redirects_the_whole_group(tmp_path, monkeypatch):
    """设了它：配置文件、日志默认落点的推导整组落到它指定的目录。"""
    _redirect_home(tmp_path, monkeypatch)
    target = tmp_path / "probe-cfg"
    monkeypatch.setenv(config_module.ENV_CONFIG_DIR, str(target))
    assert config_module.config_dir() == str(target)
    assert config_module.config_path() == str(target / config_module.CONFIG_FILE_NAME)
    assert config_module.default_log_dir() == str(target / "logs")


def test_unset_falls_back_to_home_derivation(tmp_path, monkeypatch):
    """不设它：落点与改动前同一套家目录推导，逐字相同。"""
    home = _redirect_home(tmp_path, monkeypatch)
    monkeypatch.delenv(config_module.ENV_CONFIG_DIR, raising=False)
    expected = home / ".config" / "meeting"
    assert config_module.config_dir() == str(expected)
    assert config_module.config_path() == str(expected / config_module.CONFIG_FILE_NAME)
    assert config_module.default_log_dir() == str(expected / "logs")


def test_empty_string_is_treated_as_unset(tmp_path, monkeypatch):
    """空串按没设处理（与既有 env-only 入口的读法同档）：回落家目录推导。"""
    home = _redirect_home(tmp_path, monkeypatch)
    monkeypatch.delenv(config_module.ENV_CONFIG_DIR, raising=False)
    baseline = config_module.config_dir()
    assert baseline == str(home / ".config" / "meeting")
    monkeypatch.setenv(config_module.ENV_CONFIG_DIR, "")
    assert config_module.config_dir() == baseline
    # 纯空白同档；带内容的值原样用（与字符串配置键的取值口径一致，不归一）
    monkeypatch.setenv(config_module.ENV_CONFIG_DIR, "   ")
    assert config_module.config_dir() == baseline
    monkeypatch.setenv(config_module.ENV_CONFIG_DIR, " C:\\某目录 ")
    assert config_module.config_dir() == " C:\\某目录 "
