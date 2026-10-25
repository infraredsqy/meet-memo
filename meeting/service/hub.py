"""`EventHub` 与 SSE 推送（phase2 §3.8）。

定死的做法（逐条照 phase2 §3.8.1）：

  - **连接即快照**：建立连接后第一帧必须是 `snapshot`（完整状态），新开的页面上来就落在
    当前状态、**不经过 `idle`**；
  - 通道四个：`snapshot`（连接 / 重同步时）、`state`（状态变更，全量）、`levels`（4Hz 电平
    与包络）、`transcript`（一行一条）；
  - **首帧时序（防状态断层）**：`stream()` 里「取快照」与「注册订阅」处在**同一段同步代码**、
    两者之间**不出现 `await`**；注册之后发生的每一次状态变更都会作为 `state` 事件进该订阅者
    的队列，于是「注册前发生了一次状态变更 → 首帧落后于订阅时刻」的缝不存在；
  - **广播路径必须同步、不许 `await` 慢消费者**：任何一次广播都不能因为某个订阅者的队列满
    而阻塞（否则一个卡住的页面拖住所有人）；
  - **慢消费者**：每个订阅者一个有界队列（512）。满了就**清空该订阅者的队列、塞一份新
    `snapshot`**（带 `resync: true, dropped: N`）：宁可让它重同步，也不许它拖住别人；
    `levels` 满了**只留最新一帧**；
  - **事件 id**：`state` / `snapshot` 带 `id: <revision>`；`levels` / `transcript` **不带**
    `id:`，只在 `data` 里带当前 `revision` 作参照；
  - 断线重连：开头给 `retry: 2000`；每 **15 秒**发一行注释帧 `: ping` 保活；
  - 客户端断开时把订阅者从 hub 摘掉，**只做这一件事**（不动会话）；
  - hub **只搬运快照，不许自己攒一份状态**（`snapshot_provider` 由 runtime 提供）。

对外广播入口**只有四个名字**：`broadcast_state()` / `broadcast_levels()` /
`broadcast_transcript()` / `resync()`（不存在 `broadcast_snapshot()`）。
首帧 `snapshot` 与重同步 `snapshot` 都收在 hub 内部（由 `stream()` / `resync()` 发出）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

_log = logging.getLogger("meeting.service.hub")

#: SSE 帧类型（四个）
SSE_CHANNELS: tuple[str, ...] = ("snapshot", "state", "levels", "transcript")

#: 每个订阅者的有界队列容量
SUBSCRIBER_QUEUE_SIZE = 512

#: 断线重连的等待时间（毫秒，`retry:` 帧里给）
SSE_RETRY_MILLISECONDS = 2000

#: 保活帧周期（秒）：每 15 秒一行 `: ping`
PING_INTERVAL_SECONDS = 15.0

#: 生成器轮询等待的步长（秒）：同步广播 + 非阻塞取队列，不 await 任何慢消费者
POLL_INTERVAL_SECONDS = 0.05


def encode_event(name: str, data: dict[str, Any], *, event_id: int | None = None) -> str:
    """把一个事件编码成 SSE 帧（`event:` + `data:` + 可选的 `id:`）。"""
    body = json.dumps(data, ensure_ascii=False)
    if event_id is None:
        return f"event: {name}\ndata: {body}\n\n"
    return f"id: {event_id}\nevent: {name}\ndata: {body}\n\n"


def encode_ping() -> str:
    """保活帧：**仅一行注释**（不伪造 `event` / `data`）。"""
    return ": ping\n\n"


def encode_retry() -> str:
    return f"retry: {SSE_RETRY_MILLISECONDS}\n\n"


@dataclass
class Subscriber:
    """一个订阅者：一个有界队列（512）+ 只留最新一帧的 levels 槽。"""

    queue: "queue.Queue[tuple[str, str]]" = field(
        default_factory=lambda: queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
    )
    latest_levels: str | None = None
    dropped: int = 0
    closed: bool = False

    def offer(self, item: tuple[str, str]) -> bool:
        """入队；队列满返回 `False`（由调用方决定重同步还是丢弃）。"""
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            return False

    def take(self) -> tuple[str, str] | None:
        """取一条待发帧：先 `state` / `transcript` 那一路，再取最新的 `levels`。"""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            frame = self.latest_levels
            self.latest_levels = None
            if frame is None:
                return None
            return ("levels", frame)


class EventHub:
    """SSE 广播中心（**只搬运，不攒状态**；那份快照由 runtime 经 `snapshot_provider` 给）。"""

    def __init__(self, snapshot_provider: Callable[[], dict[str, Any]] | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._snapshot_provider = snapshot_provider
        self.ping_interval = PING_INTERVAL_SECONDS

    def set_snapshot_provider(self, provider: Callable[[], dict[str, Any]]) -> None:
        """由 runtime 在装配时写入「当前快照」的产出函数（hub 自己不攒状态）。"""
        self._snapshot_provider = provider

    def _snapshot(self) -> dict[str, Any]:
        if self._snapshot_provider is None:  # pragma: no cover - 未装配
            return {"revision": 0}
        return self._snapshot_provider()

    # 订阅管理

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def register(self) -> Subscriber:
        """注册一个订阅者（同步；`stream()` 里与取快照相邻、中间不许有 `await`）。"""
        subscriber = Subscriber()
        self._subscribers.append(subscriber)
        _log.debug("SSE 订阅者接入（当前 %s 个）", len(self._subscribers))
        return subscriber

    def unregister(self, subscriber: Subscriber) -> None:
        """摘掉一个订阅者，**只做这一件事**（不动会话、不停采集）。"""
        subscriber.closed = True
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)
        _log.debug("SSE 订阅者断开（当前 %s 个）", len(self._subscribers))

    # 广播入口（四个名字，同步、不阻塞）

    def broadcast_state(self, snapshot_json: dict[str, Any]) -> None:
        """状态变更：发一份**全量**快照，带 `id: <revision>`。"""
        revision = int(snapshot_json.get("revision", 0))
        frame = encode_event("state", snapshot_json, event_id=revision)
        for subscriber in list(self._subscribers):
            if subscriber.closed:
                continue
            if subscriber.offer(("state", frame)):
                continue
            subscriber.dropped += 1
            self.resync(subscriber, snapshot_json)

    def broadcast_levels(self, payload: dict[str, Any]) -> None:
        """4Hz 电平：**不带 `id:`**，`data` 里带当前 `revision` 作参照。

        这一路是高频通道，队列满时**只留最新一帧**（不触发重同步）。
        """
        frame = encode_event("levels", payload)
        for subscriber in list(self._subscribers):
            if not subscriber.closed:
                subscriber.latest_levels = frame

    def broadcast_transcript(self, payload: dict[str, Any]) -> None:
        """一行转写：**不带 `id:`**，`data` 里带当前 `revision`。"""
        frame = encode_event("transcript", payload)
        for subscriber in list(self._subscribers):
            if subscriber.closed:
                continue
            if subscriber.offer(("transcript", frame)):
                continue
            subscriber.dropped += 1
            self.resync(subscriber, self._snapshot())

    def resync(self, subscriber: Subscriber, snapshot_json: dict[str, Any]) -> None:
        """慢消费者重同步：清空该订阅者的队列、塞一份带 `resync: true, dropped: N` 的快照。"""
        dropped = max(1, subscriber.dropped)
        self._clear(subscriber)
        payload = dict(snapshot_json)
        payload["resync"] = True
        payload["dropped"] = dropped
        frame = encode_event("snapshot", payload, event_id=int(snapshot_json.get("revision", 0)))
        subscriber.dropped = 0
        try:
            subscriber.queue.put_nowait(("snapshot", frame))
        except queue.Full:  # pragma: no cover - 清空后不该满
            pass
        _log.warning("SSE 慢消费者重同步：丢掉 %s 条后补一份 snapshot", dropped)

    @staticmethod
    def _clear(subscriber: Subscriber) -> None:
        while True:
            try:
                subscriber.queue.get_nowait()
            except queue.Empty:
                break

    # SSE 流

    async def stream(
        self,
        snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """`GET /api/events` 的响应体：先发 `retry` + `snapshot`，再搬运四个通道。

        **首帧时序**：下面这两句「取快照」与「注册订阅」之间**没有 `await`**（硬要求）。
        """
        provider = snapshot_provider or self._snapshot_provider
        if provider is None:  # pragma: no cover - 未装配
            provider = lambda: {"revision": 0}  # noqa: E731
        # —— 同一段同步代码：先取快照、再注册订阅（中间不 await）——
        snapshot_json = provider()
        subscriber = self.register()
        # —— 到这里订阅已生效，之后的每一次状态变更都会进这个队列 ——

        try:
            yield encode_retry()
            yield encode_event(
                "snapshot", snapshot_json, event_id=int(snapshot_json.get("revision", 0))
            )

            last_ping = time.monotonic()
            while not subscriber.closed:
                item = subscriber.take()
                if item is None:
                    if time.monotonic() - last_ping >= self.ping_interval:
                        last_ping = time.monotonic()
                        yield encode_ping()
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue
                yield item[1]
        finally:
            self.unregister(subscriber)


__all__ = [
    "PING_INTERVAL_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "SSE_CHANNELS",
    "SSE_RETRY_MILLISECONDS",
    "SUBSCRIBER_QUEUE_SIZE",
    "EventHub",
    "Subscriber",
    "encode_event",
    "encode_ping",
    "encode_retry",
]
