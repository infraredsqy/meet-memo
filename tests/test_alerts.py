"""`tests/test_alerts.py` —— 告警判定与阈值（设计口径第三行；§3.3）。

**这一层是纯判定、进程内单测**（`AlertJudge` 不持有快照、不写状态、不广播）：
逐帧累计、分级、抑制窗口与 `muted` 都由它算出，`ServiceRuntime` 只把结论落到快照上。

覆盖：

  - 静默秒数**用帧自己的时间戳**逐帧累计（不拿墙钟做差），到 `alert.silence_seconds`
    才起致命级；
  - 连续有信号 `alert.recover_seconds` 才恢复；抖动式一帧不来回告警（**两条独立的连续计数**）；
  - `alert.alarm_suppress_seconds` 内同一事件（哪一路 + 故障类型）不重复出声，
    **恢复后再发生算新事件**；抑制窗口的锚点是**上一次 raise 的时刻**，于是**首发那一帧
    的 `muted` 是 `False`**（界面据此响一声），起事件不会被自己压住；
  - `mute_session` 期间新起的告警**继承**会话级静音；
  - 两条提示级（`transcript.health` 掉档、`backlog_depth() ≥ BACKLOG_HINT_THRESHOLD`）
    各出一条，**同类合并的键是 `(level, source, message)` 三元组**、**上限 3**，
    合并时沿用原有那一条的 `since` 并刷新 `duration_seconds`；两条**对称的清除路径**都走得通
    （health 回 `normal`、积压回落到门槛以下），且各清各的、互不牵连；
  - **阈值只许更大、不许更小**（默认值 180 / 10 / 60 / −60 dBFS 在代码里不被兜底改小）。
"""

from __future__ import annotations

import pytest

from meeting.alerts import (
    BACKLOG_HINT_MESSAGES,
    BACKLOG_HINT_THRESHOLD,
    BACKLOG_SOURCE,
    HEALTH_HINT_MESSAGES,
    HINT_LIMIT,
    TRANSCRIPT_SOURCE,
    AlertJudge,
    AlertThresholds,
    _backlog_message,
    _health_message,
    build_hint,
    muted_at,
)
from meeting import config as config_module

#: 用例里显式调小的阈值（**只写在用例里，不改代码默认值**）
TEST_SILENCE_SECONDS = 3.0
TEST_RECOVER_SECONDS = 2.0
TEST_SUPPRESS_SECONDS = 60.0


def judge(**overrides) -> AlertJudge:
    """构造一个判定器：阈值由用例**显式**给出（不改代码默认值）。"""
    base = {
        "silence_dbfs": -60.0,
        "silence_seconds": TEST_SILENCE_SECONDS,
        "recover_seconds": TEST_RECOVER_SECONDS,
        "alarm_suppress_seconds": TEST_SUPPRESS_SECONDS,
    }
    base.update(overrides)
    return AlertJudge(thresholds=AlertThresholds(**base))


def clock_from(values: list[float]):
    """一个可控的钟（给提示级的 `duration_seconds` 用）：每调一次取下一个数。"""
    remaining = list(values)

    def read() -> float:
        if not remaining:
            raise AssertionError("可控的钟被调用的次数超出用例给出的刻度")
        return remaining.pop(0)

    return read


# ---------------------------------------------------------------------------
# 逐帧累计
# ---------------------------------------------------------------------------


def test_silence_accumulates_by_frame_timestamps_up_to_the_threshold():
    """静默秒数逐帧累计：到 `alert.silence_seconds` 才起致命级，之前一次都不响。"""
    j = judge()
    # 帧间隔 1 秒，但墙钟几乎没动 —— 累计只认帧时间戳
    fired = []
    for index in range(4):
        result = j.on_level_frame("mic", float(index), -80.0)
        if result.alert is not None:
            fired.append((index, result.alert))
    assert [index for index, _alert in fired] == [3], "第 3 秒（累计 3.0s）才够阈值"
    alert = fired[0][1]
    assert alert.level == "critical"
    assert alert.source == "mic"
    assert alert.duration_seconds == pytest.approx(TEST_SILENCE_SECONDS)
    assert alert.message
    assert alert.since


def test_silence_accumulation_ignores_wall_clock():
    """累计只认真实帧时间戳：同一序列在慢机器上跑的结论一样（不拿墙钟做差）。

    累计的是**帧与帧之间的时间**（第一帧给出基准、不贡献秒数），所以 0.5 秒一帧要 7 帧
    才凑满 3 秒。
    """
    j = judge()
    results = [j.on_level_frame("loopback", index * 0.5, -70.0) for index in range(7)]
    assert all(item.alert is None for item in results[:6])
    assert results[6].alert is not None


def test_jitter_resets_the_recovery_count_without_touching_the_state():
    """抖动容忍：恢复计数期间出现一帧低于门限 → 恢复计数清零（**两条独立计数**）。"""
    j = judge()
    # 先起一条致命级（帧时间戳 0..3，累计 3.0 秒）
    result = None
    for index in range(4):
        result = j.on_level_frame("mic", float(index), -80.0)
    assert result.alert is not None

    # 恢复累计走到 1.5 秒（还差 0.5 秒），然后来一帧静默
    assert j.on_level_frame("mic", 5.0, -30.0).recovered is False
    jitter = j.on_level_frame("mic", 6.5, -80.0)
    assert jitter.recovered is False
    assert jitter.alert is None, "抖动不产生新事件"
    # 恢复计数被清零：再连续有信号仍要重新等满 recover_seconds
    assert j.on_level_frame("mic", 7.5, -30.0).recovered is False
    assert j.on_level_frame("mic", 8.5, -30.0).recovered is False
    assert j.on_level_frame("mic", 9.6, -30.0).recovered is True


def test_recovery_needs_the_full_recover_seconds():
    """连续有信号达 `alert.recover_seconds` 才算恢复。"""
    j = judge()
    for index in range(4):
        j.on_level_frame("mic", float(index), -80.0)
    assert j.on_level_frame("mic", 4.0, -30.0).recovered is False
    assert j.on_level_frame("mic", 5.5, -30.0).recovered is False
    assert j.on_level_frame("mic", 6.2, -30.0).recovered is True


# ---------------------------------------------------------------------------
# 暂停：暂停区间不计入两条累计（`docs/design-intent.md` 第 17 条）
# ---------------------------------------------------------------------------


def test_pause_freezes_the_silence_count_however_long_it_lasts():
    """安静房间里暂停三分钟：一帧都不判，那条致命级不冒出来。"""
    j = judge()
    j.on_level_frame("mic", 0.0, -80.0)
    assert j.on_level_frame("mic", 1.0, -80.0).alert is None
    j.pause_session()
    # 暂停这一段：时钟走过好几分钟（真机上足够越过 silence_seconds）
    for ts in (60.0, 120.0, 180.0, 240.0):
        frame = j.on_level_frame("mic", ts, -80.0)
        assert frame.alert is None, "暂停区间不该累计静默秒数"
        assert frame.below_threshold is False
    # 这一段结束时静默记录一个数都没涨
    assert j._tracks["mic"].silent_seconds == pytest.approx(1.0)


def test_resume_starts_the_silence_count_from_scratch():
    """继续之后的第一帧从零起算：暂停那一整段不会以「一帧跨了几分钟」的形式补记进去。"""
    j = judge()
    j.on_level_frame("mic", 0.0, -80.0)
    j.pause_session()
    j.on_level_frame("mic", 600.0, -80.0)
    j.resume_session()
    # 恢复之后的第一帧：delta 从这一帧起算（阈值 3 秒要真的再走满 3 秒）
    assert j.on_level_frame("mic", 601.0, -80.0).alert is None
    assert j.on_level_frame("mic", 602.0, -80.0).alert is None
    assert j.on_level_frame("mic", 603.5, -80.0).alert is None
    assert j.on_level_frame("mic", 604.5, -80.0).alert is not None


def test_pause_keeps_the_recovery_count_frozen_too():
    """暂停同样不算进恢复累计：这一路在告警里时，暂停期间攒不出「恢复」。"""
    j = judge()
    for index in range(4):
        j.on_level_frame("mic", float(index), -80.0)
    j.pause_session()
    for ts in (10.0, 20.0, 30.0):
        assert j.on_level_frame("mic", ts, -30.0).recovered is False
    j.resume_session()
    assert j.on_level_frame("mic", 31.0, -30.0).recovered is False
    assert j.on_level_frame("mic", 32.5, -30.0).recovered is False
    assert j.on_level_frame("mic", 33.2, -30.0).recovered is True


def test_reset_for_new_session_clears_the_pause():
    """上一场停在暂停里结束，不该把「暂停中」带进新一场。"""
    j = judge()
    j.pause_session()
    j.reset_for_new_session()
    for index in range(4):
        frame = j.on_level_frame("mic", float(index), -80.0)
    assert frame.alert is not None


# ---------------------------------------------------------------------------
# 抑制窗口与 muted
# ---------------------------------------------------------------------------


def test_alarm_suppression_window_and_new_event_after_recovery():
    """同一事件在抑制窗口内不重复出声；**恢复后再发生算新事件**。"""
    j = judge()
    for index in range(4):
        first = j.on_level_frame("mic", float(index), -80.0)
    assert first.alert is not None
    assert first.alert.muted is False, "第一次事件在抑制窗口里还没出现过，该出声"

    # 恢复
    j.on_level_frame("mic", 4.5, -30.0)
    recovered = j.on_level_frame("mic", 7.0, -30.0)
    assert recovered.recovered is True
    j.mark_recovered("mic")

    # 恢复之后再次静默：算新事件（抑制窗口重算 → 又该出声）
    second = None
    for index in range(4):
        result = j.on_level_frame("mic", 8.0 + index, -80.0)
        if result.alert is not None:
            second = result.alert
    assert second is not None
    assert second.muted is False, "恢复后再发生算新事件，抑制窗口重算"


def test_muted_is_true_while_the_session_is_muted():
    """`mute_session` 期间**新起的告警同样继承**会话级静音。"""
    j = judge()
    j.mute_session = True
    for index in range(4):
        result = j.on_level_frame("mic", float(index), -80.0)
    assert result.alert is not None
    assert result.alert.muted is True
    assert j.is_muted("mic") is True

    # 下一场 `start` 时自动复位（复位函数由 runtime 在开场时调）
    j.reset_session_mute()
    assert j.mute_session is False


def test_refresh_muted_recomputes_the_present_answer():
    """`muted` 是**当下**的判定：两个输入都按当下读，任一条件松开，重算出来就回到 `False`。

    这一条盯的是「`muted` 只升不降」那个缺陷：把一条已经 `muted: true` 的告警交给
    `refresh_muted()`（快照提交时调它，`muted` 的唯一入口），结论会按当下的两个输入重算。
    """
    j = judge()
    j.mute_session = True
    for index in range(4):
        result = j.on_level_frame("mic", float(index), -80.0)
    alert = result.alert
    assert alert is not None and alert.muted is True

    # 会话级静音撤销，而这一条事件本身还在抑制窗口里 → 当下的结论仍是「不出声」
    j.reset_session_mute()
    j.refresh_muted([alert])
    assert alert.muted is True, "窗口还在，这一条此刻仍然不出声"

    # 把窗口起点推旧（这一条事件已经过去很久）→ 当下的结论回到「会出声」
    track = j._tracks["mic"]  # noqa: SLF001 - 就为了把窗口起点推旧
    assert track.last_raise_ts is not None
    track.last_raise_ts -= TEST_SUPPRESS_SECONDS + 1.0
    j.refresh_muted([alert])
    assert alert.muted is False, "两个条件都松开之后，这条告警该回到「会出声」"


def test_refresh_muted_recomputes_when_the_suppression_window_is_past():
    """窗口这一半同样按当下重算：窗口已经走完的那条告警，重算出来是 `False`。

    会话级静音**没开**，所以这一条的结论只由抑制窗口决定：起告警那一刻窗口刚开（该出声），
    帧时钟往前走过 `alarm_suppress_seconds` 之后重算，结论就是 `False`。
    """
    j = judge()
    for index in range(4):
        result = j.on_level_frame("mic", float(index), -80.0)
    alert = result.alert
    assert alert is not None and alert.muted is False, "窗口刚开，这一刻该出声"

    # 帧时钟走过抑制窗口：之后每来一帧都按当下重算，结论变成 False
    silence = None
    for index in range(70):
        silence = j.on_level_frame("mic", 3.0 + index, -80.0)
    assert silence is not None
    j.refresh_muted([alert])
    assert alert.muted is False, "窗口过去之后这条告警该回到「会出声」"

    # 反向再走一遍：窗口打开期间（第一帧之后、窗口之内）那条告警该是「不出声」
    fresh = judge()
    opened = fresh.on_source_lost("mic", 0.0)
    assert opened.muted is False, "起事件那一刻窗口刚开"
    inside = fresh.on_source_lost("mic", 1.0)
    assert inside.muted is True
    fresh.refresh_muted([inside])
    assert inside.muted is True, "窗口之内重算，仍是「不出声」"


def test_refresh_muted_follows_the_suppression_window():
    """抑制窗口是一条**有期**的判定：同一路的下一条事件在窗口内 `True`，出窗口 `False`。

    窗口按「哪一路 + 故障类型」跟踪，起点是这一条事件发生的时刻。两处判据：
    ① `muted_at()` 这个纯判定在窗口内给 `True`、在窗口外给 `False`（边界处逐点断言）；
    ② 走判定器的入口：窗口内的下一条事件当场就是 `True`，而**恢复**（`mark_recovered()`）
    把窗口关掉之后新起的那一条该 `False`（契约的「恢复后再发生算新事件」）。
    """
    window = TEST_SUPPRESS_SECONDS
    started = 1000.0
    # ① 纯判定：窗口内 / 边界上 / 窗口外
    assert muted_at(last_raise_ts=started, moment=started, suppress_seconds=window) is True
    assert muted_at(last_raise_ts=started, moment=started + window - 0.01,
                    suppress_seconds=window) is True
    assert muted_at(last_raise_ts=started, moment=started + window,
                    suppress_seconds=window) is False
    assert muted_at(last_raise_ts=started, moment=started + window * 5,
                    suppress_seconds=window) is False
    assert muted_at(last_raise_ts=None, moment=started, suppress_seconds=window) is False

    # ② 判定器：窗口内的下一条当场就不出声；恢复之后新起的那一条该出声
    j = judge()
    first = j.on_source_lost("mic", started)
    assert first.muted is False, "第一条事件当下就该出声"
    again = j.on_source_lost("mic", started + 1.0)
    assert again.muted is True, "同一路在抑制窗口里的下一条，当下就不出声"

    j.mark_recovered("mic")
    later = j.on_source_lost("mic", started + window + 1.0)
    assert later.muted is False, "恢复后再发生算新事件，窗口重算"


def test_refresh_muted_on_a_hint_reads_the_session_switch_only():
    """提示级没有抑制窗口：它出声与否只由会话级静音决定（两个方向各算一次）。"""
    j = judge()
    hint = build_hint(source="transcript", message="转写落后了", muted=True)
    j.refresh_muted([hint])
    assert hint.muted is False

    j.mute_session = True
    j.refresh_muted([hint])
    assert hint.muted is True


def test_source_lost_starts_a_critical_event_immediately():
    """后端报「这一路的流中断了」→ 立刻起致命级（不等静默阈值）。"""
    j = judge()
    alert = j.on_source_lost("loopback", 100.0, reason="设备被拔了")
    assert alert.level == "critical"
    assert alert.source == "loopback"
    assert "设备被拔了" in alert.message
    assert alert.duration_seconds == 0.0


def test_alert_encodes_the_six_frozen_fields():
    """`Alert` 的字段集固定为六项（合成 / 合并不许新增或改名）。"""
    hint = build_hint(source="transcript", message="转写落后了", duration_seconds=1.0)
    assert set(hint.encode()) == {
        "level", "source", "since", "duration_seconds", "message", "muted",
    }


# ---------------------------------------------------------------------------
# 提示级
# ---------------------------------------------------------------------------


def test_transcript_health_drop_produces_one_hint():
    """`transcript.health` 掉档 → 出一条提示级；回到 `normal` 之后不再有它。"""
    j = judge()
    assert j.on_transcript_health("normal") is None
    behind = j.on_transcript_health("behind")
    assert behind is not None
    assert behind.level == "hint"
    assert behind.source == "transcript"
    assert len(j.hints()) == 1

    stopped = j.on_transcript_health("stopped")
    assert stopped is not None
    assert len(j.hints()) == 2, "掉到 stopped 是另一条提示（message 不同）"


def test_on_transcript_and_backlog_hints_carry_build_hints_fields():
    """判定器入口新起的那条与 `build_hint()` **同字段**（复审 #4 的核对）。

    `on_transcript_health()` / `on_backlog_depth()` 都并入 `_merge_hint()`，新起那条
    的组装从那里走 `build_hint()`：字段集到此只有一处写法。`since` 是秒精度的墙钟
    串、两回调用可能隔一个秒界，那格不逐字符比（其余五格逐项锚死）。
    """
    j = judge()

    produced = j.on_transcript_health("behind")
    assert produced is not None
    expected = build_hint(source=TRANSCRIPT_SOURCE, message=_health_message("behind"))
    for name in ("level", "source", "duration_seconds", "message", "muted"):
        assert getattr(produced, name) == getattr(expected, name), name
    assert produced.since

    backlog = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert backlog is not None
    expected_backlog = build_hint(
        source=BACKLOG_SOURCE, message=_backlog_message()
    )
    for name in ("level", "source", "duration_seconds", "message", "muted"):
        assert getattr(backlog, name) == getattr(expected_backlog, name), name
    assert backlog.since


def test_backlog_depth_threshold_produces_one_hint():
    """`backlog_depth() ≥ 20` → 出一条提示级（门槛 20 是常量、不加配置键）。"""
    j = judge()
    assert BACKLOG_HINT_THRESHOLD == 20
    assert j.on_backlog_depth(19) is None
    hint = j.on_backlog_depth(20)
    assert hint is not None
    assert hint.level == "hint"
    assert hint.source == "transcript"
    assert "20" in hint.message


def test_hints_merge_by_the_triple_key_and_stop_at_three():
    """同类合并的键是 `(level, source, message)` 三元组；上限 3；沿用原有那条的 `since`。"""
    j = judge()
    first = j.on_backlog_depth(20)
    assert first is not None
    since = first.since

    # 同一个三元组再次出现不新增条目（沿用原来那一条、`since` 不动）
    again = j.on_backlog_depth(25)
    assert again is first
    assert again.since == since
    assert len(j.hints()) == 1

    # 再塞三个不同的键 → 总量被压到上限 3
    for message in ("另一条提示一", "另一条提示二", "另一条提示三"):
        j._merge_hint(level="hint", source="transcript", message=message)  # noqa: SLF001
    hints = j.hints()
    assert len(hints) == HINT_LIMIT == 3
    assert all(item.level == "hint" for item in hints)


def test_merging_the_same_hint_refreshes_duration_seconds():
    """同一个三元组再次出现时**沿用原有那一条的 `since`**，并把 `duration_seconds` 刷新到当下。

    走**生产路径**（`on_backlog_depth()`）而不是另找一个模块级的合并函数：刷新必须发生在
    `ServiceRuntime` 真正会调到的那条路上。钟由用例给出（提示级没有帧时间戳可依），
    于是「第一次 0.0、第二次刷新到当下」这件事可以逐字断言。
    """
    j = AlertJudge(
        thresholds=AlertThresholds(
            silence_dbfs=-60.0,
            silence_seconds=TEST_SILENCE_SECONDS,
            recover_seconds=TEST_RECOVER_SECONDS,
            alarm_suppress_seconds=TEST_SUPPRESS_SECONDS,
        ),
        _clock=clock_from([100.0, 108.5]),
    )
    first = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert first is not None
    assert first.duration_seconds == 0.0
    since = first.since

    again = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD + 5)
    assert again is first, "同一个三元组沿用原来那一条"
    assert again.since == since, "`since` 保持首次发生时刻"
    assert again.duration_seconds == 8.5, "`duration_seconds` 刷新到当下（8.5 秒那一刻）"

    # 同一路再合并一次：数值随钟继续变大（不是一个恒定的 0.0）
    j._clock = clock_from([120.0])  # noqa: SLF001 - 换一只钟，模拟时间继续走
    third = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD + 9)
    assert third is first
    assert third.duration_seconds == 20.0, "第二次合并刷新到 120.0 那一刻（20 秒）"
    assert third.duration_seconds > 0.0, "`duration_seconds` 已经离开新建时那个 0.0"


def test_hints_keep_the_earliest_three():
    """上限之外的新键：**保留 `since` 最早的三条、丢掉其余**（丢掉那条只进 log）。"""
    j = judge()
    first = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    second = j.on_transcript_health("behind")
    third = j.on_transcript_health("stopped")
    assert first is not None and second is not None and third is not None
    for index, item in enumerate((first, second, third)):
        item.since = f"2025-09-06T10:00:0{index}+08:00"

    # 第四条（`since` 最晚）进来：快照里仍是前三条，丢掉的那条留一条 warning
    j._merge_hint(level="hint", source="transcript", message="第四条")  # noqa: SLF001
    kept = j.hints()
    assert len(kept) == HINT_LIMIT
    assert [item.message for item in kept] == [first.message, second.message, third.message]

    # 同一个键再次出现时沿用原有那一条（对象本身就是原来那个），`since` 保持不动
    same = j.on_transcript_health("behind")
    assert same is second
    assert same.since == "2025-09-06T10:00:01+08:00"


def test_the_cap_reclaims_the_evicted_key_from_the_judge_bookkeeping():
    """上限 3 挤掉的那条**从登记里回收**：`hints()` 与 `_hints` 恒同长（复审 m4）。

    只截断 `hints()` 的返回值、把多出来的键留在 `_hints` 里的话，登记会只增不减
    （探针实测：`hints()` 返回 3 条而内部 4 条，转写回 `normal` 之后仍是 4 条）。
    回收之后：两条清除路径能把这类键清干净，同一个键再进来算**新起的那一条**。
    """
    j = judge()
    first = j.on_transcript_health("behind")
    second = j.on_transcript_health("stopped")
    third = j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert first is not None and second is not None and third is not None
    for index, item in enumerate((first, second, third)):
        item.since = f"2025-09-06T11:00:0{index}+08:00"

    # 第四条（`since` 最晚）进来：三条留下，多出来的那个键离开登记
    evicted = j._merge_hint(level="hint", source="transcript", message="第四条")  # noqa: SLF001
    assert len(j._hints) == HINT_LIMIT == 3, "登记被上限回收，与 `hints()` 同长"  # noqa: SLF001
    assert ("hint", "transcript", "第四条") not in j._hints  # noqa: SLF001
    assert len(j._hint_started_at) == 3, "记时表同步收回，不留无主刻度"  # noqa: SLF001

    # 被挤掉的那个键重新进来：算新起的那一条（不是把旧对象翻出来）
    again = j._merge_hint(level="hint", source="transcript", message="第四条")  # noqa: SLF001
    assert again is not evicted, "回收过的键重新进来是一条新条目"
    assert again.duration_seconds == 0.0
    assert len(j._hints) == HINT_LIMIT  # noqa: SLF001

    # 两条清除路径把剩下的健康度 + 积压提示清干净，一个键都不留在登记里
    j.on_transcript_health("normal")
    j.on_backlog_depth(0)
    assert j.hints() == []
    assert j._hints == {}, "健康度与积压这两类清完，登记里没有残留"  # noqa: SLF001
    assert j._hint_started_at == {}  # noqa: SLF001


def test_reset_for_new_session_clears_the_hint_bookkeeping():
    """`reset_for_new_session()` 清判定器时，提示级的**两份登记一起清**（复审 m3）。

    清 `_hints` 而留下 `_hint_started_at`（或反过来）会让「登记与快照是同一份真相」这条
    不变量有一刻不成立，下一场开场之后旧刻度还会影响新一场的 `duration_seconds`。
    """
    j = judge()
    j.on_transcript_health("behind")
    j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert len(j._hints) == 2 and len(j._hint_started_at) == 2  # noqa: SLF001

    j.reset_for_new_session()
    assert j.hints() == []
    assert j._hints == {}  # noqa: SLF001
    assert j._hint_started_at == {}, "记时表与条目一起清"  # noqa: SLF001
    assert j.mute_session is False

    # 新一场里同一个键重新进来：`duration_seconds` 从这一刻重新起算
    fresh = j.on_transcript_health("behind")
    assert fresh is not None
    assert fresh.duration_seconds == 0.0


# ---------------------------------------------------------------------------
# 阈值
# ---------------------------------------------------------------------------


def test_thresholds_come_from_config_and_are_never_shrunk():
    """四条阈值从配置读；**代码里不出现「取更小值兜底」**（默认值保持 180 / 10 / 60）。"""
    assert config_module.default_for("alert.silence_seconds") == 180
    assert config_module.default_for("alert.recover_seconds") == 10
    assert config_module.default_for("alert.alarm_suppress_seconds") == 60
    assert config_module.default_for("alert.silence_dbfs") == -60

    class FakeConfig:
        def __init__(self, values):
            self.values = values

        def get(self, key, default=None):
            return self.values.get(key, default)

    loaded = AlertThresholds.from_config(
        FakeConfig(
            {
                "alert.silence_dbfs": -70,
                "alert.silence_seconds": 300,
                "alert.recover_seconds": 30,
                "alert.alarm_suppress_seconds": 120,
            }
        )
    )
    assert loaded.silence_seconds == 300.0
    assert loaded.recover_seconds == 30.0
    assert loaded.alarm_suppress_seconds == 120.0
    assert loaded.silence_dbfs == -70.0
    # 判定器原样用它给出的值（没有「取更小值兜底」这一层）
    assert judge(silence_seconds=300.0).thresholds.silence_seconds == 300.0


# ---------------------------------------------------------------------------
# 抑制窗口的锚点：首发那一帧必须放行
# ---------------------------------------------------------------------------


def test_first_raise_is_audible_and_the_anchor_is_written_after_the_judgement():
    """首发那条致命级广播出去是 `muted: False`（该响一声），锚点落在这一刻。

    这一条咬的是「先算 muted、后写锚点」这个次序，以及首发那一条**扛得住紧随其后的重算**：
    `commit()` 的重算就发生在锚点刚开的同一刻，少了 `raise_in_flight` 这个记号，
    同一条告警会被自己刚开的窗口压住，广播出去的第一帧成了 `True`（界面一声都不响）。
    """
    j = judge()
    assert j._tracks == {}, "起点干净：这一路还没 raise 过"

    result = j.on_level_frame("mic", 0.0, -80.0)
    for index in range(1, 4):
        result = j.on_level_frame("mic", float(index), -80.0)
    alert = result.alert
    assert alert is not None, "静默累计到阈值该起致命级"
    assert alert.muted is False, "首发那一刻算出来该出声"

    track = j._tracks["mic"]  # noqa: SLF001 - 就看这一刻的锚点与记号
    assert track.last_raise_ts == 3.0, "锚点取这一路上一次 raise 的时刻"
    assert track.raise_in_flight is alert, "这一条还没广播出去，记号挂着"

    # 广播那一刻的重算（`commit()` 的那一次）：原样放行，广播出去的就是 False
    j.refresh_muted([alert])
    assert alert.muted is False, "首发帧广播出去必须仍然是「会出声」"
    assert track.raise_in_flight is None, "这一条出去了，记号到此为止"

    # 此后同一条告警再算就落进窗口里，不再重复出声
    j.refresh_muted([alert])
    assert alert.muted is True, "同一条告警不重复出声"

    # 窗口之内新起的下一条（新事件）当下就不出声
    second = j.on_source_lost("mic", 3.5)
    assert second.muted is True, "窗口内的下一条不出声"


def test_refresh_muted_never_releases_the_same_alert_again():
    """首发之后任意次 `refresh_muted()` 都不把**同一条**告警重新放行。

    `refresh_muted()` 是 `muted` 的唯一入口、每次提交快照都会被调一次。首发那一帧是
    `False`（响一声），此后同一条告警重算必须落进刚写下的窗口里、保持 `True` ——
    否则「不重复出声」这道闸在每一次提交上都会被自己放开。
    """
    j = judge()
    for index in range(4):
        alert = j.on_level_frame("mic", float(index), -80.0).alert
    assert alert is not None and alert.muted is False

    # 首发帧那一次（广播时刻的重算）：原样放行，仍然是 False
    j.refresh_muted([alert])
    assert alert.muted is False, "首发帧广播出去仍然会出声"

    # 一帧都没往前推（帧时钟停在 3.0），此后连着重算多次都保持 True
    for attempt in range(5):
        j.refresh_muted([alert])
        assert alert.muted is True, f"第 {attempt + 1} 次重算把同一条告警又放行了"

    # 会话级静音期间同样一律 True（这一档今天已经对，别改坏）
    j.mute_session = True
    j.refresh_muted([alert])
    assert alert.muted is True
    j.reset_session_mute()

    # 恢复之后再出事：按新事件处理，首发又是 False
    j.mark_recovered("mic")
    track = j._tracks["mic"]  # noqa: SLF001 - 恢复该把锚点清空
    assert track.last_raise_ts is None, "恢复关掉这一条事件的窗口"
    fresh = j.on_source_lost("mic", 90.0)
    assert fresh.muted is False, "恢复后再发生算新事件，首发该出声"


def test_session_mute_holds_through_the_raise_in_flight_pass_through():
    """首发帧那条「刚起、还没广播」的条目在**本场静音期间照样是 `true`**（复审 m5）。

    `refresh_muted()` 对 `raise_in_flight` 那一条跳过的是**抑制窗口那一半**的重算（好让
    广播出去的首发帧保持 `false`），会话级静音那一半照算。少了这一层，静音期间过一遍
    标记条目就会把一条本该不出声的告警算成 `false` —— 界面按契约看这个布尔值，本场静音
    当场失效。运行时里 raise 与 commit 同在一个同步调用里、中间没有 `await`，所以这一档
    眼下靠调用序撑着；这一条用例把它钉死。
    """
    j = judge()
    first = j.on_source_lost("mic", 100.0)
    assert first.muted is False, "首发那一刻窗口还开着，该出声"
    track = j._tracks["mic"]  # noqa: SLF001 - 就看这一刻的记号
    assert track.raise_in_flight is first, "这一条还没广播出去，记号挂着"

    # 静音在本条广播之前生效（下一场 `start` 之外，静音只由 `mute_session` 动作翻）
    j.mute_session = True
    j.refresh_muted([first])
    assert first.muted is True, "本场静音期间，标记那一条照样不出声"
    assert track.raise_in_flight is None, "这一条出去了，记号到此为止"

    # 记号清掉之后再算：静音仍然说了算
    j.refresh_muted([first])
    assert first.muted is True

    # 静音复位之后回到窗口口径：窗口里的同一条仍是 `true`，恢复后新事件又是 `false`
    j.reset_session_mute()
    j.refresh_muted([first])
    assert first.muted is True, "窗口还没过，这一条仍然不出声"
    j.mark_recovered("mic")
    fresh = j.on_source_lost("mic", 200.0)
    assert fresh.muted is False, "恢复后新事件的首发又该出声"


# ---------------------------------------------------------------------------
# 提示级的清除路径
# ---------------------------------------------------------------------------


def test_health_hint_is_cleared_by_the_normal_transition():
    """健康度回 `normal` → 那条提示真的从 `hints()` 里消失（含掉到 `stopped` 那条）。

    原先 `on_transcript_health("normal")` 拿 `_health_message("normal")`（`转写正常`）去
    `pop()`，而存进去的键是 `转写落后了` / `转写停了` —— 那个键一次都没进过字典，于是
    两条健康度提示一旦出现就**永远消不掉**。这一条按生产的调用路径走一遍。
    """
    j = judge()
    assert HEALTH_HINT_MESSAGES == ("转写落后了", "转写停了")

    j.on_transcript_health("behind")
    assert [item.message for item in j.hints()] == ["转写落后了"]

    # 掉到 stopped：两条并列（message 不同就是两个键）
    j.on_transcript_health("stopped")
    assert sorted(item.message for item in j.hints()) == ["转写停了", "转写落后了"]

    # 回到 normal：这一类的两条一起撤掉
    assert j.on_transcript_health("normal") is None
    assert j.hints() == [], "健康度回 normal 之后不该还挂着那两条提示"


def test_backlog_hint_is_cleared_when_the_depth_falls_back():
    """积压回落到门槛以下 → 那条提示真的消失（清除分支与出条分支对称）。

    原先 `on_backlog_depth()` 在低于阈值时直接 `return None`，没有任何撤条动作，于是
    「待转写段数已达 20 段」在队列清空之后还留在快照上。
    """
    j = judge()
    assert BACKLOG_HINT_THRESHOLD == 20

    j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert [item.message for item in j.hints()] == list(BACKLOG_HINT_MESSAGES)

    # 恰好落在门槛上还算越线（判据是 `≥`，这一档不能提前撤）
    j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert len(j.hints()) == 1

    # 回落到门槛以下：撤掉
    assert j.on_backlog_depth(BACKLOG_HINT_THRESHOLD - 1) is None
    assert j.hints() == [], "积压回落之后那条提示该消失"

    # 连续回落也不出错（幂等）
    assert j.on_backlog_depth(0) is None
    assert j.hints() == []


def test_the_two_clear_paths_do_not_touch_each_others_hint():
    """两条清除路径各清各的：健康度回 `normal` 不动积压那条，反之亦然。

    两类提示共用 `source = transcript`，靠 `message` 分辨。拿 source 一把清会让
    「转写跟上了」把「队列还堆着 20 段」一起带走 —— 两件事各有各的门槛。
    """
    j = judge()
    j.on_transcript_health("behind")
    j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert len(j.hints()) == 2

    # 健康度回 normal：积压那条留下
    j.on_transcript_health("normal")
    assert [item.message for item in j.hints()] == list(BACKLOG_HINT_MESSAGES)

    # 再把健康度掉回来，这回清积压：健康度那条留下
    j.on_transcript_health("behind")
    assert len(j.hints()) == 2
    j.on_backlog_depth(0)
    assert [item.message for item in j.hints()] == ["转写落后了"]

    # 清除之后再出同一条：`since` 从这一刻重新起算（不是沿用被撤掉那一条的）
    j.on_backlog_depth(BACKLOG_HINT_THRESHOLD)
    assert len(j.hints()) == 2

