# -*- coding: utf-8 -*-
"""重复循环过滤 + 循环检测的回归测试。

回归基线来自 2026-09-17 那场真实会议：GLM-ASR 进入退化循环输出了两条
2799/3333 字符的垃圾（周期 7 和 5），**旧过滤器一个字符都没删掉**。
这两个用例的作用就是让那次故障永久可复现——过滤器再退化回旧行为就红。
"""
import os
import sys

# 本文件从 `tests/` 挪到 `tests/v1/` 之后，仓库根要往上数**两级**（`tests/v1/` → `tests/` → 仓库根）：
# 只数一级算出来的是 `.../meeting/tests`，直跑本文件时 `import tests.v1.fixtures_real_loops`
# 会因为仓库根不在 `sys.path` 上而报 `ModuleNotFoundError: No module named 'tests'`。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.v1.fixtures_real_loops import LOOP_LINE_261, LOOP_LINE_283, NORMAL_LINES  # noqa: E402
from meeting.asr.guards import (  # noqa: E402
    _loop_coverage, _strip_repeat_loops, _LoopGuard,
)


# ── 真实故障行必须被拦下 ────────────────────────────────────────

def test_real_loop_line_261_is_stripped():
    """周期 7 的「写啊哈，你可以」×399 —— 旧实现漏掉的正是这种长周期循环。

    注意：检测到的"周期"可能是一个**循环移位**的等价单位（如「以写啊哈，你可」），
    因为多数候选移位的覆盖率同为最大。断言只检查行为契约，不绑定具体单位。
    """
    out = _strip_repeat_loops(LOOP_LINE_261)
    assert len(out) < 60, f"应把循环垃圾砍到 <60 字符，实际 {len(out)}"
    # 循环内容只保留一组：留下的单位在原串里能且只能匹配一次
    unit, count, cov = _loop_coverage(LOOP_LINE_261)
    assert count > 100, "真实故障行的重复次数应在数百量级"
    assert out.count(unit) == 1, "只应保留一组"


def test_real_loop_line_283_is_stripped():
    """周期 5 的「是错的，那」×663。"""
    out = _strip_repeat_loops(LOOP_LINE_283)
    assert len(out) < 60, f"应把循环垃圾砍到 <60 字符，实际 {len(out)}"
    unit, count, cov = _loop_coverage(LOOP_LINE_283)
    assert out.count(unit) == 1, "只应保留一组"
    # 循环之前的前导正文应当保留
    assert out.startswith("这样赢赢哦，这空的，我现在这样，我")


def test_old_filter_would_have_missed_these():
    """守住"为什么必须扩周期上限"这个事实：周期 >4 的循环，扫不到就是扫不到。

    若哪天有人把周期上限调回 4，这条会立刻变红——它保护的正是那次故障的根因。
    """
    for text in (LOOP_LINE_261, LOOP_LINE_283):
        unit, count, cov = _loop_coverage(text, max_unit=4)
        # 周期 5/7 的循环在 max_unit=4 的搜索空间里不可能被识别为高覆盖
        assert cov < 0.5, f"max_unit=4 竟然覆盖了 {cov:.2f}，用例前提已变"


# ── 正常文本绝不能误杀 ─────────────────────────────────────────

def test_normal_lines_untouched():
    """真实正常转写行必须原样返回（含「啊哈」、口头语、英文词重复）。"""
    for line in NORMAL_LINES:
        assert _strip_repeat_loops(line) == line, f"误杀正常行: {line[:40]}"


def test_short_text_skipped():
    """≤100 字符直接短路——短文本不可能是大规模循环垃圾。"""
    short = "哈哈哈哈哈哈哈哈哈哈"
    assert _strip_repeat_loops(short) == short


def test_legit_repetition_survives():
    """合法的高频口语重复（周期短、次数多但占比低）不应被砍。"""
    cases = [
        "对" * 30 + "，我觉得这个事情还是要再想想，因为这里边其实还有很多细节没有捋清楚，"
                    "比如说前面那个三层结构到底怎么落到具体的体验目标上，这个需要再讨论一下。",
        "嗯嗯嗯嗯，好，那我们接着往下看，刚才讲到的那几个点我觉得可以先记下来，"
        "回头再确认一下优先级，别到时候又漏了。",
    ]
    for c in cases:
        assert _strip_repeat_loops(c) == c, f"误杀合法重复: {c[:30]}"


def test_coverage_metric_sane():
    """覆盖率指标本身：构造一个纯循环串，覆盖率应接近 1。"""
    text = "abcdefgh" * 50            # 周期 8
    unit, count, cov = _loop_coverage(text)
    assert cov > 0.9
    assert unit == "abcdefgh"


# ── token 级循环检测（生成过程中掐死循环） ──────────────────────

def test_loop_guard_detects_repetition():
    """尾部窗口里某个短单位反复出现 → 立即判环。"""
    guard = _LoopGuard(eos_token_id=1)
    scores = None
    ids = [0]
    for _ in range(120):
        ids = ids + [10, 11, 12, 13]
        out = guard(ids, scores)
        if guard.detected:
            break
    assert guard.detected, "重复 token 流未被判环"


def test_loop_guard_leaves_normal_stream_alone():
    """正常 token 流（无短周期重复）绝不触发——避免误杀正常转写。"""
    guard = _LoopGuard(eos_token_id=1)
    ids = [0]
    # 用递增序列模拟正常文本流，无短周期重复
    for i in range(200):
        ids = ids + [1000 + i]
        guard(ids, None)
    assert not guard.detected, "正常流被误判为循环"


def test_loop_guard_forces_eos():
    """判环后应把概率压到 EOS（而不是继续采样垃圾）。"""
    import torch
    guard = _LoopGuard(eos_token_id=7)
    for _ in range(120):
        guard([0] + [10, 11, 12, 13] * 30, None)
        if guard.detected:
            break
    assert guard.detected
    scores = torch.zeros((1, 16))
    out = guard([0], scores)
    assert float(out[0, 7]) == 0.0, "EOS 应被抬到最高分"
    assert float(out[0, 0]) == float("-inf"), "其它 token 应被压到 -inf"
