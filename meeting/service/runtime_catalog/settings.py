"""RuntimeCatalogMixin 的「设置写盘」域分片：settings_update 动作。"""
from __future__ import annotations

import logging
import os
from typing import Any

from meeting import config as config_module
from meeting.service.runtime_shared import ActionError

_log = logging.getLogger("meeting.service.runtime")


class RuntimeCatalogSettingsMixin:
    def settings_update(self, args: dict[str, Any]) -> dict[str, Any]:
        """写 `config.json`（原子写）；**可热读的键立刻生效，其余键写盘成功但要重启**。

        「写盘成功」与「快照立刻反映」是两件事：归 `pending_restart` 的那些键本次只落盘，
        运行期仍取启动时的生效值，快照 `settings` 里读到的还是旧值（可观察判据是主 log 那一行
        「……仍取启动值」）；本次改动里凡有热读键（`asr.*` 五个、`alarm.sound` 与设置抽屉那四项：
        `devices.mic` / `devices.loopback` / `heartbeat.interval_seconds` /
        `summary.default_window`），就在下面重取一次 `_settings_view()` 并提交 ——
        出声开关勾选即生效；设备改完，紧接着的 `reprobe` 按**新值**挑（`_config_view()`
        读的就是内存生效值）；心跳周期与总结缺省档的快照格当场换新值。
        """
        try:
            changes = config_module.validate_setting_changes(args, current=self.config)
        except config_module.ConfigValueError as exc:
            raise ActionError(
                "invalid_setting",
                str(exc),
                status=400,
                detail={"key": exc.key, "value": exc.value, "legal": exc.legal},
            ) from exc

        try:
            config_module.write_config_values(changes)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"配置写盘失败（{type(exc).__name__}: {exc}）；这次改动一处都没生效",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        except config_module.ConfigCorruptError as exc:
            raise ActionError(
                "persist_failed",
                f"现有 config.json 读不出来，没法安全叠加：{exc}",
                status=500,
                detail={"error": str(exc)},
            ) from exc

        applied, pending = config_module.split_setting_changes(changes)
        if applied and self.config is not None:
            values = dict(self.config.applied)
            values.update({key: changes[key] for key in applied})
            self.config = config_module.Config(
                applied=values, sources=dict(self.config.sources), path=self.config.path
            )
            config_module.set_current(self.config)
            self.snapshot.settings = self._settings_view()
            self.commit()

        for key in changes:
            env_name = config_module.env_name_for(key)
            if env_name and env_name in os.environ:
                _log.warning(
                    "settings_update：%s 被环境变量 %s 压着（写盘值 %r），运行期仍由环境变量说话",
                    key, env_name, changes[key],
                )
        # 这一行只对真正要重启才生效的键说：热读键（含 `alarm.sound`）写盘那一刻快照与
        # 运行期行为就已换过去，报「仍取启动值」会与事实相反。
        if pending:
            _log.info(
                "settings_update：%s 归 pending_restart，仍取启动时的生效值（改了要重启才生效）",
                "、".join(sorted(pending)),
            )
        if "alarm.sound" in changes:
            _log.info(
                "settings_update：alarm.sound 收在热读档，本刻起出声开关 = %r（下一次提交的快照一并可见）",
                self._value("alarm.sound"),
            )
        return {"applied": applied, "pending_restart": pending}
