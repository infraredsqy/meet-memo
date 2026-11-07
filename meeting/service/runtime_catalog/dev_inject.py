"""``RuntimeCatalogMixin`` 的「dev_inject」域分片：dev 采集替身的故障注入动作。

方法与方法间注释均自拆分前的 ``runtime_catalog.py`` 单文件 mixin 一字不改复制；
本文件自写内容只有模块 docstring、import 与类声明行。
"""
from __future__ import annotations

from typing import Any

from meeting.service.runtime_shared import ActionError


class RuntimeCatalogDevInjectMixin:
    """「dev_inject」：只在 dev 采集替身在场时可用的故障注入动作。"""

    def dev_inject(self, args: dict[str, Any]) -> None:
        """`dev_inject`：只在 dev 采集替身在场时可用，且要有正在进行的场次。"""
        if not self.capture_capabilities.get("dev_inject"):
            raise ActionError(
                "action_unavailable",
                "dev_inject 只在 dev 采集替身在场时可用（当前后端不支持）",
                status=404,
            )
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "没有正在进行的场次，注入动作无对象可挂", status=409)

        source = args.get("source")
        fault = args.get("fault")
        try:
            self.capture_backend.inject_fault(
                source=source,
                fault=fault,
                after_seconds=args.get("after_seconds", 0.0),
                duration_seconds=args.get("duration_seconds"),
            )
        except (TypeError, ValueError) as exc:
            raise ActionError(
                "invalid_setting", str(exc), status=400,
                detail={"source": source, "fault": fault, "error": str(exc)},
            ) from exc
