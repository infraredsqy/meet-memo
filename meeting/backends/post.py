"""会后钩子：会后离线转写那一层的**解析落点**。

`registry.resolve_post_hook()` 按 `post.hook` 找的就是本模块的 `PostProcessHook`
（`DEFAULT_PATHS["post.hook"]` 那条约定路径）。**它只是一个解析落点**：不拉起任何会后动作，
「生成会后精细稿」按钮走 `offline.runner.run_session(..., minutes=False)` 的直路也不经它 ——
`post.hook` 在那一侧只答「这一层接没接」（`null` → 按钮 404 `action_unavailable`）。
**「跑会后一套」的产品入口只剩 `meeting post` 这一条**。

**不实现 `finalize` / `transcode`**：那两项是采集后端的能力，采集侧那两步的闸门只看采集
实现——`registry.capabilities_of()` 按实现自己声明的能力位（`writes_real_audio`）回答，
本类不声明，两项照旧为假。
"""

from __future__ import annotations

#: 状态文件里 `state` 的五档（`meeting.offline.artifacts.STATES` 同值）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
#: 一路整条没交回、另一路跑成那一档：产物可用（它是终态），顶层那一格与 `done` 分开写
STATE_PARTIAL = "partial"
#: 跑到块边界被人停那一档：也是**终态**。补记录判据把它算进「已落定」——
#: 打断之后同目录再炸出来的异常不许把 `interrupted` 盖成 `failed`（那一格说的是
#: 「人停的」，不是「引擎炸了」）。
STATE_INTERRUPTED = "interrupted"


class PostProcessHook:
    """会后离线转写那一层的解析落点：**本类不带任何钩子方法**。"""


__all__ = [
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_INTERRUPTED",
    "STATE_PARTIAL",
    "STATE_RUNNING",
    "PostProcessHook",
]
