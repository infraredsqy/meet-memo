"""工作台 / 场次目录 / 状态视图 / 杂项动作 mixin。

本包是 meeting.service.runtime_catalog：对外的公共面只有拼装好的
RuntimeCatalogMixin（meeting/service/runtime.py 按名取用），九域各归一片、
域间不互相伸手——

- 设置写盘（settings_update）：包内 settings.py；
- dev_inject：包内 dev_inject.py；
- 会中总结清理：包内 summary_cleanup.py；
- 工作区注册表：包内 workspace.py；
- 单场状态：包内 session_view.py；
- 稿子正文：包内 transcripts.py；
- 目录选择器：包内 picker.py；
- 设备清单：包内 devices.py；
- 说话人改名：包内 speakers.py。

工作区与稿子正文两域共用的盘上 session.json 字符串格读法
（_disk_str_or_none()）住 runtime_shared.py，从共享叶子取。
"""

from __future__ import annotations

from meeting.service.runtime_catalog.devices import RuntimeCatalogDevicesMixin
from meeting.service.runtime_catalog.dev_inject import RuntimeCatalogDevInjectMixin
from meeting.service.runtime_catalog.picker import RuntimeCatalogPickerMixin
from meeting.service.runtime_catalog.session_view import RuntimeCatalogSessionViewMixin
from meeting.service.runtime_catalog.settings import RuntimeCatalogSettingsMixin
from meeting.service.runtime_catalog.speakers import RuntimeCatalogSpeakersMixin
from meeting.service.runtime_catalog.summary_cleanup import RuntimeCatalogSummaryCleanupMixin
from meeting.service.runtime_catalog.transcripts import RuntimeCatalogTranscriptsMixin
from meeting.service.runtime_catalog.workspace import RuntimeCatalogWorkspaceMixin


class RuntimeCatalogMixin(
    RuntimeCatalogSettingsMixin,
    RuntimeCatalogDevInjectMixin,
    RuntimeCatalogSummaryCleanupMixin,
    RuntimeCatalogWorkspaceMixin,
    RuntimeCatalogSessionViewMixin,
    RuntimeCatalogTranscriptsMixin,
    RuntimeCatalogSpeakersMixin,
    RuntimeCatalogPickerMixin,
    RuntimeCatalogDevicesMixin,
):
    """工作台 / 场次目录 / 状态视图 / 杂项动作 mixin（九域各一片，本类只拼装）。"""


__all__ = ["RuntimeCatalogMixin"]
