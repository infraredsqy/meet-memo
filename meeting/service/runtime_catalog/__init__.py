"""工作台 / 场次目录 / 状态视图 / 杂项动作 mixin 的九域分片。

对外的公共面只有拼装好的 RuntimeCatalogMixin（meeting/service/runtime.py 按名取用），
九域各归一片、域间不互相伸手。
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
