"""§9-1（关掉全部页面录音照跑）+ §9-3 后半句的进程级复证（phase3 §3.8）。

一条用例：起 dev 真服务 → `POST start` → 开两条 SSE → **全部断开** → 用 `wait_until()`
轮询 `/api/state` 直到 `session.elapsed_seconds` 比断开时刻**增长 ≥
`E2E_ELAPSED_GROWTH_SECONDS`**（超时 `E2E_POLL_TIMEOUT_SECONDS`，显式给出、不留裸秒数）→
断言这段窗口内**没有状态变更**（断开时读到的 `revision` 与再读到的 `revision` 相等）→
重新连上 → **第一帧事件名必须是 `snapshot`**、`top_state == "recording"`、
`elapsed_seconds` 大于断开时刻的值 → `POST stop` 等收尾落定（`ended.phase == "done"`）→
**停录之后再连一次** → 第一帧事件名同样是 `snapshot`、`top_state == "ended"`，紧接着读一次
`GET /api/state`（两次读取之间不发任何动作请求），断言重连首帧的 body 与这次 `/api/state` 的
JSON **逐字段原样相等**（§9-3 后半句「同一时刻拿到同一份 JSON」在进程级的复证；组件级的单点
断言归 phase2 的 `tests/test_sse.py`）。

**比对时点为什么在停录之后**（审查问题 M2 选的落法）：录音期那几处「读的时刻天然不一样」的
活字段（本场秒数 / 30 秒包络数组 / 两路瞬时电平）在 `ended` 冻结之后全部静止 —— 秒数由
`session.frozen` 定住，包络与电平只在收到电平帧时变（`meeting/state.py` 的 `push_level()`），
采集一停就没有新帧。于是两份读数可以**逐字段**对上，不必先摘字段再比：这条断言因此与契约
原文一样强，没有任何归一化在后面弱化它。诊断用的逐字段差异由 `_diff()` 报出。

「杀掉浏览器」在无头环境里由「断开全部 SSE 订阅者」**等价表达**（方案 §4.4 第 1 条：
断开订阅者即全部副作用）；§9-1 的「等 10 秒」落成「等 `elapsed_seconds` 增长 ≥
`E2E_ELAPSED_GROWTH_SECONDS` 秒」的可判定形式。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests import _helpers as helpers
from tests._service import post_action, wait_until

#: 慢档标记（68927c27 收尾轮）：真服务/真样本采集的收尾验收，不进每次迭代圈。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见 phase3.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS` 与
# `E2E_ELAPSED_GROWTH_SECONDS`；下面各自那一段说明写清了判据的形状与这两笔钱的来路。
# ---------------------------------------------------------------------------

#: 单条用例的墙钟上限（秒）。
#:
#: **口径按用户新定的红线收口**：契约 §3.8 文末那张表原先写 60 秒，用户后来把
#: 「每一条用例 setup + call + teardown ≤ 5.0 秒」定为硬要求，`tests/conftest.py` 的看门
#: hook（`CASE_WALL_CLOCK_LIMIT_SECONDS`）自动判红 —— 全局 5 秒看门取代了 60 秒那条口径。
#: 本常量与看门同值，并在用例正文里作为**显式断言**真的被用上（`_finish_case()`）。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: §9-1 那条观察窗口的判据：本场已录秒数要长这么多。
#: 用户把「单条用例 ≤ 5 秒」定为硬要求，观察窗口因此从 8 秒收到 1.0 秒 ——
#: 判据的形状一字未改（仍然是「断开之后 `elapsed_seconds` 还在往前走、且这一段里
#: `revision` 一次都没动」），只是把「等 10 秒」的可判定形式调到了用例预算之内。
E2E_ELAPSED_GROWTH_SECONDS = 1.0

#: 进程级用例的轮询超时（秒）
E2E_POLL_TIMEOUT_SECONDS = 30

#: SSE 读取的单帧超时（秒）
SSE_READ_TIMEOUT_SECONDS = 20


def _finish_case(started: float) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。

    与 `tests/conftest.py` 的看门 hook 是同一个上限、同一条红线（那条看门量的是
    setup + call + teardown 三段合计，这里量的是用例正文这一段）。
    """
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"本用例正文墙钟 {spent:.2f}s 超过上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


def _start(proc) -> None:
    workspace = helpers.allocate_start_dir(proc.sessions_root)
    started = post_action(proc.base_url, "start", {"dir": workspace})
    assert started.status_code == 200, started.text


def test_recording_continues_after_all_clients_disconnect(service_process, meeting_env, sse_client):
    """关掉全部订阅者之后录音照跑，重连第一帧就是当前快照。"""
    started = time.monotonic()
    # **采集后端由本用例自己的 `config.json` 表达**（审查问题 m5 / m6）：env 里那一项
    # （`MEETING_CAPTURE`）一个都不写 —— 它的优先级高于配置文件，留着就会把这条键静默压住。
    # 四条 `tests/test_e2e_*.py` 因此同源，与 `tests/conftest.py` 的 `meeting_env` 也同源。
    meeting_env.write_config({"capture.backend": "dev"})
    proc = service_process(
        home=meeting_env.home,
        log_dir=meeting_env.log_dir,
        extra_env=meeting_env.env,
    )
    proc.wait_ready()
    _start(proc)
    # 先读一次 `/api/state`：把本用例自己那条 HTTP 连接建好（本机新建一个 `httpx.Client`
    # 要 0.83 秒），否则这笔构造代价会落进下面那段观察窗口里
    proc.snapshot()

    first = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    second = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    # 两条订阅各自先拿到首帧 `snapshot`（连上就有状态，不用等下一次变更）
    for client in (first, second):
        frame = client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
        assert frame["event"] == "snapshot", frame
        assert frame["data"]["top_state"] == "recording"

    before = proc.snapshot()
    assert before["top_state"] == "recording"
    elapsed_at_disconnect = before["session"]["elapsed_seconds"]
    revision_at_disconnect = before["revision"]

    # 「关掉全部页面」= 断开全部 SSE 订阅者（无头环境里这两件事等价）
    first.close()
    second.close()

    # **重连那条订阅现在就建好**（本机新建一个 SSE client 要 0.8 秒上下：Windows 上
    # SSL 上下文的初始化，见 `tests/_helpers.py` 的 `local_get()`）：建好之后它只是
    # 挂在流上等首帧，这笔构造代价因此落在观察窗口之外 —— 窗口里量到的是
    # 「断开之后录音还在往前走」这件事本身。
    reconnected = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)

    def grew():
        snapshot = proc.snapshot()
        return snapshot if snapshot["session"]["elapsed_seconds"] >= (
            elapsed_at_disconnect + E2E_ELAPSED_GROWTH_SECONDS
        ) else None

    after = wait_until(grew, timeout=E2E_POLL_TIMEOUT_SECONDS)

    # 这一段窗口里没有状态变更（没有任何动作请求，也不该有推送把 revision 顶上去）
    assert after["revision"] == revision_at_disconnect, (
        f"断开期间窗口里出现了状态变更：{revision_at_disconnect} -> {after['revision']}"
    )

    # 重连的第一帧：事件名必须是 `snapshot`，且拿到的是「此刻」的状态
    frame = reconnected.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
    assert frame["event"] == "snapshot", frame
    assert frame["data"]["top_state"] == "recording"
    assert frame["data"]["session"]["elapsed_seconds"] > elapsed_at_disconnect

    # §9-3 后半句：重连首帧的 body 与紧接着一次 `GET /api/state` 的 JSON **逐字段相等**。
    # **比对时点挪到已停录之后**（审查问题 M2 选的落法：更贴契约原文的那个「逐字段相等」）：
    # `ended` 一冻结，随墙钟自己往前走的字段就全停了 —— 本场秒数由 `session.frozen` 定住，
    # 包络数组与两路瞬时电平只在收帧时变（`push_level()`），采集停了就没有新帧。于是两份读数
    # 可以**原样逐字段**对上，不必先摘字段再比（`_stable_view()` 那层归一化因此删掉了）。
    assert post_action(proc.base_url, "stop").status_code == 200
    stopped = wait_until(
        lambda: (
            lambda snap: snap
            if (snap.get("ended") or {}).get("phase") == "done"
            else None
        )(proc.snapshot()),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    assert stopped["top_state"] == "ended"
    assert stopped["session"]["frozen"] is True

    after_stop = sse_client(proc, timeout=SSE_READ_TIMEOUT_SECONDS)
    ended_frame = after_stop.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
    assert ended_frame["event"] == "snapshot", ended_frame
    assert ended_frame["data"]["top_state"] == "ended"
    fresh = proc.snapshot()
    assert ended_frame["data"] == fresh, (
        "停录之后重连首帧与紧接着的 /api/state 该是同一份状态（逐字段）："
        f"\n帧：{_diff(ended_frame['data'], fresh)}\n状态：{_diff(fresh, ended_frame['data'])}"
    )
    _finish_case(started)


def _diff(left: Any, right: Any, *, path: str = "") -> dict[str, Any]:
    """逐字段找出两边的不同（**只用于断言失败时报出差异**，判据仍是上面的整份相等）。

    递归着走进字典与等长列表，把「哪条路径上、左边是什么、右边是什么」列出来。比对本身不
    摘任何字段：**字段一个都不少**，这也是本用例把比对时点挪到停录之后换来的东西。
    """
    out: dict[str, Any] = {}
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            out.update(_diff(left.get(key), right.get(key), path=f"{path}.{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, (one, other) in enumerate(zip(left, right)):
            out.update(_diff(one, other, path=f"{path}[{index}]"))
        return out
    if left != right:
        out[path or "<根>"] = (left, right)
    return out
