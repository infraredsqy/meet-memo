"""会后钩子（`7c6952e5` phase2）：会后离线转写那一层的**解析落点**。

`registry.resolve_post_hook()` 按 `post.hook` 找的就是本模块的 `PostProcessHook`
（`DEFAULT_PATHS["post.hook"]` 那条约定路径）。**它只是一个解析落点**：收尾第 4 步
不拉起任何会后动作（那一格做完释放显存就停在 `waiting`），「生成会后精细稿」按钮走
`offline.runner.run_session(..., minutes=False)` 的直路也不经它 —— `post.hook` 在那一侧
只答「这一层接没接」（`null` → 按钮 404 `action_unavailable`）。起 daemon 线程跑
`meeting.cli.run_post()` 的那套钩子机器（`launch` / `_run` / `_mark_failed`）没有任何
产品调用方，已随收尾第 4 步的退场一并删除：**「跑会后一套」的产品入口只剩
`meeting post` 这一条**。

类的 `__module__` 是 `meeting.backends.post` —— `registry.capabilities_of()` 靠它区分
替身与本实现（写进别的模块名下会被当成替身）。**不实现 `finalize` / `transcode`**：
那两项是采集后端的能力，采集侧那两步的闸门只看采集实现。
"""

from __future__ import annotations

#: 状态文件里 `state` 的五档（`meeting.offline.artifacts.STATES` 同值）
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
#: 一路整条没交回、另一路跑成那一档：产物可用（它是终态），顶层那一格与 `done` 分开写
STATE_PARTIAL = "partial"
#: 跑到块边界被人停那一档（b1c6a58e）：也是**终态**。补账判据把它算进「已落定」——
#: 打断之后同目录再炸出来的异常不许把 `interrupted` 盖成 `failed`（那一格说的是
#: 「人停的」，不是「引擎炸了」）。
STATE_INTERRUPTED = "interrupted"


class PostProcessHook:
    """会后离线转写那一层的解析落点：**本类不带任何钩子方法**。

    `post.hook` 的四档取值里 `auto` 落在这里；`null` 落 `registry._AbsentPostHook`
    （缺席记 `skipped`，那是另一个档位的事）。
    """


__all__ = [
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_INTERRUPTED",
    "STATE_PARTIAL",
    "STATE_RUNNING",
    "PostProcessHook",
]
