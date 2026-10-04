"""`tests/test_transcript_hints.py` —— 提示级告警的接线、广播语义与两条清除路径。

这一单元之前 `AlertJudge.on_transcript_health()` / `on_backlog_depth()` 在整个 `meeting/`
包里**零生产调用点**，`alerts.hints` 没有任何写入点，于是快照里那一格恒为空数组 ——
设计要的「实时转写落后了」那条提示条在这套实现里根本画不出来。

这里盯住的四件事（逐条对应 `task.md` 第三节）：

  1. **接线落在 `ServiceRuntime._refresh_transcript_snapshot()` 上**（全仓唯一的转写刷新落点：
     启动、开场、每行、0.5 秒节拍四处都经过它），并且**每次都调判定器** ——
     持续提示条要在窗口内一直刷新 `duration_seconds`，只在 health 迁移那一刻调的话
     条目会停在 `duration_seconds = 0`；
  2. **广播语义**：提示级的**键集合**变化（新增 / 移除 / 被上限挤掉）要能被界面看见，
     于是刷新返回「有变化」、调用点走一次 `commit()`（`revision +1` + 一次 `state` 广播）；
  3. **同一键集合里 `duration_seconds` 的刷新不许为真** —— 0.5 秒的节拍不许把 `revision`
     抖起来（契约第 3.3 节：转写通道不参与状态迁移）；
  4. **两条清除路径**都走得通：health 回 `normal`、积压回落到门槛以下。

转写侧用替身后端摆布健康度与积压量（`dev` 替身恒报 `normal`，真机造不出 `behind`）。
节拍那种「真的等 0.5 秒」的场合只留一条端到端用例，其余按**同一份调用序列**手动推进 ——
`_refresh_transcript_snapshot()` 与节拍调的是同一个函数，于是判据一样、用例快得多。
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

from meeting.alerts import BACKLOG_HINT_THRESHOLD, HINT_LIMIT
from tests import _helpers as helpers

#: 端到端那一条要等节拍真跑起来，所以给它一个明确的超时（不留裸秒数）
TICKER_WAIT_SECONDS = 10.0
TICKER_POLL_SECONDS = 0.02


class StubTranscriber:
    """替身转写后端：`health()` 与 `backlog_depth()` 都由用例摆布。

    只实现 runtime 会问的那几个方法（`bind` / `start` / `stop` / `health` /
    `backlog_depth` / `segment_timeout`），与 `tests/test_transcriber_backend.py` 里那个
    同类替身同形 —— 不 import 它，是为了让这一条用例文件自己站得住。
    """

    def __init__(self, *, health: str = "normal", backlog: int = 0) -> None:
        self.health_value = health
        self.backlog_value = int(backlog)

    def bind(self, line_sink, *, config=None):  # noqa: ARG002 - 替身不产出
        return None

    def start(self, mode: str, session_dir: str) -> None:  # noqa: ARG002
        return None

    def stop(self) -> None:
        return None

    def health(self) -> str:
        return self.health_value

    def backlog_depth(self) -> int:
        return int(self.backlog_value)

    def segment_timeout(self) -> float:
        return 20.0


def wait_until(predicate, *, timeout: float = TICKER_WAIT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(TICKER_POLL_SECONDS)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def snapshot_of(client) -> dict:
    return client.get("/api/state").json()


def live(tmp_path: Path, backend: StubTranscriber) -> tuple[Path, dict]:
    """进程内已启动的运行时，转写后端换成替身，并把它收成一份确定的起点。

    **起点本来就是干净的**：这份文件把转写后端配成 `dev` 替身（见下面的 `values`），
    它恒报 `normal`、积压恒 0，所以 `startup()` 那一次刷新不会留下任何提示条。
    这里照样把判定器里那几条撤掉、把快照那一格清空：一来让每条用例从一份白纸起步，
    二来日后换个报 `stopped` 的后端时这一步不用重写。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    values = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
    }
    return home, values


def settle(runtime, backend: StubTranscriber) -> None:
    """把 stub 转写后端接上，并把起点收成一份白纸。

    收三样东西，缺一样第一条 `pump()` 就会以「有变化」开场、把用例的判据搅浑：

      - `transcript.health`：`startup()` 那一次读的是配在 `values` 里的 `dev` 替身
        （恒报 `normal`），接上 stub（可能报 `behind` / `stopped`）之后这一格先得刷成真值；
      - 判定器里那几条提示：同上，让 stub 的读数从零开始攒；
      - 快照的 `alerts.hints` 与运行时那份记账。
    """
    runtime.transcriber_backend = backend
    runtime._refresh_transcript_snapshot()  # noqa: SLF001 - 把 health 刷成 stub 的真值
    runtime.judge.clear_hints()
    runtime.snapshot.alerts.hints = []
    runtime._hint_keys = ()  # noqa: SLF001 - 与上面那一格一起归零


@contextlib.contextmanager
def recording(tmp_path: Path, backend: StubTranscriber):
    """起一场录音（`top_state: recording`）之后的运行时 —— 提示级的成立范围。

    **提示级只在 `recording` / `alert` 上成立**（`docs/ui-design.md` 章 2.5：空闲态与结束档
    的告警区不出现），所以这些用例都先开一场；`live()` + `settle()` 收的是「空闲态、白纸」
    那份起点，开场那一刻的 `_clear_hints()` 与这一场自己的刷新把提示级重新从零起算。

    `yield` 出来的是三元组 `(client, runtime, started)`：`started` 标记开场那一刻就已经把
    stub 的读数摆进快照（stub 报 `behind` / 积压越线时就是这样），此时第一拍 `pump()`
    算「键集合没动」、返回假。
    """
    home, values = live(tmp_path, backend)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home, values=values) as client:
        runtime = client.app.state.runtime
        settle(runtime, backend)
        response = client.post(
            "/api/actions",
            json={"action": "start", "args": helpers.start_args("start", {"mode": "both"}, out_root)},
            headers={"X-Meeting-Service": "1"},
        )
        assert response.status_code == 200, response.text
        assert snapshot_of(client)["top_state"] == "recording", "本场要真的在录"
        yield client, runtime, bool(snapshot_of(client)["alerts"]["hints"])


def pump(runtime, *, times: int = 1) -> bool:
    """按节拍那一拍的调用序列手动推一遍：刷新 → 有变化就提交。返回「有没有变化」。"""
    changed = False
    for _ in range(times):
        changed = runtime._refresh_transcript_snapshot() or changed  # noqa: SLF001
        if changed:
            runtime.commit()
    return changed


# ---------------------------------------------------------------------------
# 接线：提示级真的进快照
# ---------------------------------------------------------------------------


def test_health_drop_reaches_the_snapshot_as_a_hint(tmp_path):
    """`transcript.health` 掉到 `behind` → 快照 `alerts.hints` 里真的出现那一条提示。

    这是「hints 恒为空」那个缺口的最小判据：判定器算得出来、快照里看不见，就是没接线。
    """
    backend = StubTranscriber()
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is False, "这一档入场时还没有提示级"
        assert snapshot_of(client)["alerts"]["hints"] == [], "起点干净"

        backend.health_value = "behind"
        assert pump(runtime) is True, "提示级新增是一次「有变化」"
        hints = snapshot_of(client)["alerts"]["hints"]
        assert [item["message"] for item in hints] == ["转写落后了"]
        assert hints[0]["level"] == "hint"
        assert hints[0]["source"] == "transcript"
        assert runtime.snapshot.transcript.health == "behind"

        # 掉到 stopped：另一条提示并列（message 不同就是两个键）
        backend.health_value = "stopped"
        assert pump(runtime) is True
        hints = snapshot_of(client)["alerts"]["hints"]
        assert sorted(item["message"] for item in hints) == ["转写停了", "转写落后了"]


def test_backlog_threshold_reaches_the_snapshot_as_a_hint(tmp_path):
    """待转写段数越过门槛 → 快照里出现积压那一条（门槛是常量 20、没有配置键）。"""
    backend = StubTranscriber()
    with recording(tmp_path, backend) as (client, runtime, started):
        backend.backlog_value = BACKLOG_HINT_THRESHOLD - 1
        assert pump(runtime) is False, "还没越线：不该有提示、也不该提交"
        assert snapshot_of(client)["alerts"]["hints"] == []

        backend.backlog_value = BACKLOG_HINT_THRESHOLD
        assert pump(runtime) is True, "越线那一刻是一次「有变化」"
        hints = snapshot_of(client)["alerts"]["hints"]
        assert len(hints) == 1
        assert hints[0]["message"] == f"待转写段数已达 {BACKLOG_HINT_THRESHOLD} 段"
        assert runtime.snapshot.transcript.buffered_lines == BACKLOG_HINT_THRESHOLD


def test_the_hint_judge_is_consulted_on_every_refresh(tmp_path):
    """判定器**每次都调**：持续提示条的 `duration_seconds` 跟着刷新，不走迁移那条时机。

    只在 health 迁移那一刻调判定器的话，这条提示的 `duration_seconds` 会一直停在 `0.0`
    （新建那一条的初值），界面上「已经落后了多久」就成了一个死数。
    """
    backend = StubTranscriber(health="behind")
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is True, "stub 一开场就报 `behind`，这条提示开场那一刻已经在了"
        pump(runtime)
        first = snapshot_of(client)["alerts"]["hints"][0]["duration_seconds"]

        # 后台节拍与手动推进都会刷时长；这里按节拍的调用序列手动推几拍，
        # 每拍之间让墙钟走一点（提示级的钟就是服务侧自己的钟）。
        for _ in range(3):
            time.sleep(0.05)
            pump(runtime)

        hints = snapshot_of(client)["alerts"]["hints"]
        assert len(hints) == 1, "同一个键集合里不新增条目"
        assert hints[0]["duration_seconds"] > first, "时长该跟着走"
        assert hints[0]["since"] == snapshot_of(client)["alerts"]["hints"][0]["since"]


# ---------------------------------------------------------------------------
# 广播语义：键集合变化提交一次，时长刷新不提交
# ---------------------------------------------------------------------------


def test_key_set_change_commits_and_duration_refresh_does_not(tmp_path):
    """键集合变化 → 刷新返回真、`revision` 涨；同一键集合内刷时长 → 返回假、`revision` 不动。

    这一条是本单元最容易写坏的地方：把「每拍都调判定器」直接接到 `changed` 上的话，
    0.5 秒的节拍会把 `revision` 一直推上去，契约给界面的那句「`id` 变了就是状态变了」
    当场变成假命题。
    """
    backend = StubTranscriber()
    with recording(tmp_path, backend) as (client, runtime, started):
        base = int(snapshot_of(client)["revision"])

        # ① 提示级新增：一次变化、一次提交
        backend.health_value = "behind"
        assert pump(runtime) is True
        after_add = snapshot_of(client)
        assert int(after_add["revision"]) == base + 1
        assert len(after_add["alerts"]["hints"]) == 1

        # ② 同一键集合内连推多拍（时长每拍都在刷）：一次都不许变、revision 原地不动
        for _ in range(5):
            time.sleep(0.03)
            assert pump(runtime) is False, "同一个键集合里刷时长不算变化"
        still = snapshot_of(client)
        assert int(still["revision"]) == base + 1, "时长刷新不该推着 revision 走"
        assert len(still["alerts"]["hints"]) == 1

        # ③ 再掉一档：新增一个键 → 又是一次变化
        backend.health_value = "stopped"
        assert pump(runtime) is True
        assert int(snapshot_of(client)["revision"]) == base + 2
        assert len(snapshot_of(client)["alerts"]["hints"]) == 2

        # ④ 健康度回 normal：撤掉两条 → 键集合变化，同样提交一次
        backend.health_value = "normal"
        assert pump(runtime) is True
        after_clear = snapshot_of(client)
        assert int(after_clear["revision"]) == base + 3
        assert after_clear["alerts"]["hints"] == [], "清除也是一次变化"


def test_hints_cap_of_three_also_counts_as_a_change(tmp_path):
    """被上限挤掉那一条同样算键集合变化（`hints` 上限 3 是判定器那边的口径）。"""
    backend = StubTranscriber(health="behind", backlog=BACKLOG_HINT_THRESHOLD)
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is True, "攒了三份读数、开门那一刻就都摆好了：%r" % started
        assert pump(runtime) is False, "开场那一刷已经把这几个键摆好了，键集合没动"
        keys = runtime._hint_keys  # noqa: SLF001 - 就看这一份记账
        assert len(keys) == 2, "健康度 + 积压各一条"
        assert len(snapshot_of(client)["alerts"]["hints"]) == 2

        # 塞到上限：第 4 个键进来会把 `since` 最晚的那些挤掉
        for message in ("第四条", "第五条"):
            runtime.judge._merge_hint(  # noqa: SLF001 - 就为了把上限那一档造出来
                level="hint", source="transcript", message=message
            )
        assert pump(runtime) is True, "被上限挤掉也是键集合变化"
        hints = snapshot_of(client)["alerts"]["hints"]
        assert len(hints) == HINT_LIMIT == 3


# ---------------------------------------------------------------------------
# 两条清除路径（走真实服务的那条路）
# ---------------------------------------------------------------------------


def test_health_recovery_clears_the_hint_from_the_snapshot(tmp_path):
    """health 回 `normal` → 快照里那两条健康度提示被撤掉（走服务侧的刷新路径）。"""
    backend = StubTranscriber(health="behind")
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is True, "开场那一刻这条提示已经到位"
        assert pump(runtime) is False, "这一拍键集合没动"
        assert [item["message"] for item in snapshot_of(client)["alerts"]["hints"]] == ["转写落后了"]

        backend.health_value = "normal"
        assert pump(runtime) is True, "撤条也是一次键集合变化"
        assert snapshot_of(client)["alerts"]["hints"] == []


def test_backlog_falling_back_clears_the_hint_from_the_snapshot(tmp_path):
    """积压回落到门槛以下 → 快照里那条提示被撤掉（对接线的另一半的判据）。"""
    backend = StubTranscriber(backlog=BACKLOG_HINT_THRESHOLD)
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is True, "开场那一刻这条提示已经到位"
        assert pump(runtime) is False, "这一拍键集合没动"
        assert len(snapshot_of(client)["alerts"]["hints"]) == 1

        backend.backlog_value = 0
        assert pump(runtime) is True, "回落撤条也是一次键集合变化"
        assert snapshot_of(client)["alerts"]["hints"] == []


def test_the_two_clear_paths_leave_each_other_alone(tmp_path):
    """健康度回 `normal` 不动积压那条提示（两件事各有各的门槛）。"""
    backend = StubTranscriber(health="behind", backlog=BACKLOG_HINT_THRESHOLD)
    with recording(tmp_path, backend) as (client, runtime, started):
        assert started is True, "开场那一刻这两条提示已经到位"
        assert pump(runtime) is False, "这一拍键集合没动"
        assert len(snapshot_of(client)["alerts"]["hints"]) == 2

        backend.health_value = "normal"
        pump(runtime)
        hints = snapshot_of(client)["alerts"]["hints"]
        assert [item["message"] for item in hints] == [
            f"待转写段数已达 {BACKLOG_HINT_THRESHOLD} 段"
        ], "队列还堆着，那条提示该留下"


# ---------------------------------------------------------------------------
# 端到端：0.5 秒节拍真的会把提示带上来
# ---------------------------------------------------------------------------


def test_the_health_ticker_carries_the_hint_without_any_new_line(tmp_path):
    """起一场之后，后台节拍自己把提示级刷进快照 —— 一行转写都不用来。

    这是「接线必须落在 `_refresh_transcript_snapshot()` 上」的端到端判据：那四条调用路径
    里只有节拍会在没有任何行、没有任何动作的时候继续跑，接在别处的话这一幕永远出不来。
    """
    backend = StubTranscriber(health="behind")
    home, values = live(tmp_path, backend)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home, values=values) as client:
        runtime = client.app.state.runtime
        settle(runtime, backend)
        assert snapshot_of(client)["alerts"]["hints"] == [], "空闲态这一格不出现"
        assert (
            client.post(
                "/api/actions",
                json={"action": "start", "args": helpers.start_args("start", {"mode": "both"}, out_root)},
                headers={"X-Meeting-Service": "1"},
            ).status_code
            == 200
        )

        hints = wait_until(
            lambda: (lambda s: s if s["alerts"]["hints"] else None)(snapshot_of(client)),
            what="节拍把提示级带进快照",
        )
        assert [item["message"] for item in hints["alerts"]["hints"]] == ["转写落后了"]
        assert hints["transcript"]["health"] == "behind"

        # 收尾一份现场，便于复审看「提示条与 revision 同时是什么样」
        revision = int(hints["revision"])
        for _ in range(3):
            time.sleep(0.1)
        settled = snapshot_of(client)
        assert len(settled["alerts"]["hints"]) == 1, "同一键集合里不新增条目"
        assert int(settled["revision"]) >= revision, "revision 只增不减"


def test_hints_stay_empty_while_the_transcriber_reports_normal(tmp_path):
    """`dev` 那种恒报 `normal`、积压 0 的替身：接完线之后 `alerts.hints` 仍是空数组。

    这一条守住「接线没有把提示级变成常驻」：上游那些断言
    `alerts == {"critical": None, "hints": []}` 的用例靠的就是这一格。
    """
    backend = StubTranscriber()
    home, values = live(tmp_path, backend)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home, values=values) as client:
        runtime = client.app.state.runtime
        settle(runtime, backend)
        pump(runtime, times=3)
        assert snapshot_of(client)["alerts"] == {"critical": None, "hints": []}


# ---------------------------------------------------------------------------
# 提示级只在录音 / 告警态成立（C1）：空闲与结束档不出现，离开就清、下一场从零
# ---------------------------------------------------------------------------


def test_hints_stay_empty_in_idle_and_ended(tmp_path):
    """`top_state ∈ {idle, ended}` 时 `alerts.hints` 恒为 `[]`（设计稿章 2.5 那张表）。

    空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过场次」，与录音中
    「转写停了」是两回事：判定权随状态收口，空闲与结束档不把读数交给判定器。
    结束档由 `stop` 走到 `ended`，那一刻起这一格也必须是空数组。
    """
    backend = StubTranscriber(health="behind")
    home, values = live(tmp_path, backend)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home, values=values) as client:
        runtime = client.app.state.runtime
        settle(runtime, backend)
        assert snapshot_of(client)["top_state"] == "idle"
        assert snapshot_of(client)["alerts"]["hints"] == [], "空闲态不出现告警区"

        # 空转三拍（判定器已经不参与），那一格照样是空
        pump(runtime, times=3)
        assert snapshot_of(client)["alerts"]["hints"] == []

        assert (
            client.post(
                "/api/actions",
                json={"action": "start", "args": helpers.start_args("start", {"mode": "both"}, out_root)},
                headers={"X-Meeting-Service": "1"},
            ).status_code
            == 200
        )
        assert snapshot_of(client)["top_state"] == "recording"
        assert [item["message"] for item in snapshot_of(client)["alerts"]["hints"]] == [
            "转写落后了"
        ], "录音态里提示级照常出现"

        assert client.post(
            "/api/actions",
            json={"action": "stop"},
            headers={"X-Meeting-Service": "1"},
        ).status_code == 200
        ended = wait_until(
            lambda: (lambda s: s if s["top_state"] == "ended" else None)(snapshot_of(client)),
            what="收尾走到 ended",
        )
        assert ended["alerts"]["hints"] == [], "结束时提示级一并清掉"


if __name__ == "__main__":  # pragma: no cover - 便于单文件直接跑
    raise SystemExit(pytest.main([__file__, "-q"]))
