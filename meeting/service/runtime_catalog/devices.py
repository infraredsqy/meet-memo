"""RuntimeCatalogMixin 的「设备清单」域分片：GET /api/devices。

方法与方法间注释均自拆分前的 runtime_catalog.py 单文件 mixin 一字不改复制；
本文件自写内容只有模块 docstring、import 与类声明行。
"""
from __future__ import annotations

from typing import Any

from meeting.service.runtime_shared import ActionError


class RuntimeCatalogDevicesMixin:
    """「设备清单」：两路各自的候选端点表（只读、纯枚举、不开设备）。"""

    def devices_listing(self) -> dict[str, Any]:
        """`GET /api/devices` 的应答体：两路各自的候选端点表（**只读、纯枚举、不开设备**）。

        设置抽屉的两个设备下拉用它。枚举走 `meeting/capture/sources.py` 的
        `list_input_endpoints()` / `list_render_endpoints()` —— **只读端点表**，不试开、
        不采集（`probe()` 会短暂试开，因此**不能**拿它当清单来源）。与 `GET /api/dirs`
        同一档纯读：不提交、不涨 `revision`、不广播；**任何状态下都 200**（会中打开抽屉
        时清单照给，换不换得了由界面按第 7 章禁用）。

        每项六格照 `DeviceEndpoint` 原样给：`name` / `host_api` / `role` / `index` /
        `channels` / `is_default`。render 端点没有 host API 与序号概念
        （`host_api` 为空串、`index` 是枚举序），形状两端一致，界面不挑着读。
        """
        from meeting.capture import sources as capture_sources

        try:
            inputs = list(capture_sources.list_input_endpoints() or [])
            renders = list(capture_sources.list_render_endpoints() or [])
        except Exception as exc:
            raise ActionError(
                "capture_unavailable",
                f"设备端点表读不出来（{type(exc).__name__}: {exc}）；这次一枚举都没有发生",
                status=503,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        def row(endpoint: Any) -> dict[str, Any]:
            return {
                "name": str(endpoint.name),
                "host_api": str(endpoint.host_api or ""),
                "role": str(endpoint.role),
                "index": None if endpoint.index is None else int(endpoint.index),
                "channels": int(endpoint.channels),
                "is_default": bool(endpoint.is_default),
            }

        return {
            "mic": [row(item) for item in inputs],
            "loopback": [row(item) for item in renders],
        }
