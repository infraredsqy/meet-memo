"""token 级判环 / 墙钟超时 / 文本级重复剥离 —— 四层防护里**零重依赖**的三层。

搬自 V1 `mnotes_core/recorder.py`（对照源 `.work/v1-src/recorder.py` **已按
`docs/legacy-assets.md` 1.0 第 5 条删除**，原件见 V1 仓库
`C:/Users/<user>/tools/meeting-notes/mnotes_core/recorder.py`）：

  - token 级常量 `:20-25`             → 本文件「一、token 级判环」一节
  - `_LoopGuard` `:47-146`            → `_LoopGuard`（第 2 层：`logits_processor` 钩子）
  - `_DeadlineStop` `:149-172`        → `_DeadlineStop`（第 3 层：`StoppingCriteria` 钩子）
  - 文本层常量 `:183-187`             → 本文件「二、文本级剥离」一节
  - `_loop_coverage` `:190-226`       → `_loop_coverage`
  - `_strip_repeat_loops` `:229-249`  → `_strip_repeat_loops`（第 4 层：文本兜底）

职责分工（`docs/legacy-assets.md` 的 A1 / A2 两条）：

    本模块     —— 判定内核：纯标准库 + duck-typed 张量，**零 numpy / torch / transformers import**
    glm.py     —— 把上面两个钩子挂进 `generate()`，并在函数体内惰性 import 重依赖

`_LoopGuard` / `_DeadlineStop` 不需要 torch 就能用：前者只碰 `scores.clone()` / `scores.fill_()` /
`scores[:, eos]` 这几个鸭子类型方法，后者只是个接受 `StoppingCriteriaList` 调用的普通可调用对象。

**搬过来时只动了三处**，其余逐字照搬（出处注释逐段标出）：

  1. logger 名：V1 的 `logging.getLogger("meeting")` → `"meeting.asr.guards"`
     （本项目每个模块一个子 logger，`meeting/logging_setup.py` 的 `get_logger()` 同一口径）；
  2. 两个默认值不再取 V1 `mnotes_core/config.py` 的模块级常量 `REPEAT_MAX_UNIT` /
     `REPEAT_MIN_COVERAGE`，改取 `meeting.config` 的 `asr.repeat_max_unit` /
     `asr.repeat_min_coverage` —— 默认值同为 **40 / 0.5**，且这两个键是本项目**每段热读**的
     `asr.*` 键（`meeting/config.py` 的 `HOT_KEYS`），改了立刻生效；
  3. V1 模块顶层的 `import numpy as np` / `import torch` 一行都没有（本模块一行都用不到）。
"""

from __future__ import annotations

import logging
import time

from meeting import config as config_module

#: 本项目自己的 logger（V1 那一行是 `logging.getLogger("meeting")`）
log = logging.getLogger("meeting.asr.guards")


# ═══════════════════════════════════════════════════════════════════════════
# 一、token 级判环（第 2 层）与墙钟硬超时（第 3 层）
# ═══════════════════════════════════════════════════════════════════════════

# 搬自 V1 mnotes_core/recorder.py:20-25
# GLM-ASR tokenizer 是字符级的（1 汉字 ≈ 1 token），故 token 窗口可直接按"字数"理解
_LOOP_TOKEN_WINDOW = 96     # 参与循环检测的尾部窗口（token）
_LOOP_TOKEN_MIN_UNIT = 2    # 最短重复单位（token）
_LOOP_TOKEN_MAX_UNIT = 16   # 最长重复单位（token）
_LOOP_TOKEN_MIN_COUNT = 6   # 非重叠出现次数下限
_LOOP_TOKEN_COVERAGE = 0.7  # 覆盖率下限


# 搬自 V1 mnotes_core/recorder.py:47-146
class _LoopGuard:
    """token 级循环检测 + 墙钟硬超时（logits_processor 钩子，每步调用一次）。

    两件事放在同一个钩子里，是因为循环检测本来就要逐 token 看输出，
    顺手多查一次墙钟几乎零成本。超时另有 StoppingCriteria 与"总耗时超预算"
    兜底（三处判据取或），防任一钩子在别的 transformers 版本里失效。

    职责：
      1) 超时：超过 deadline 即标记 timed_out，并把概率全压到 EOS 强制收尾
      2) 循环：尾部窗口里某个 ≤16 token 单位反复出现即判环，同样强制 EOS
     """

    def __init__(self, eos_token_id: int, window: int = _LOOP_TOKEN_WINDOW,
                 timeout_seconds: float | None = None):
        self.eos_token_id = eos_token_id
        self.window = window
        self.recent: list[int] = []
        self.detected = False
        self.detected_at: int | None = None
        self.timed_out = False
        self.timed_out_at: int | None = None
        self._steps = 0
        self._deadline = (time.monotonic() + float(timeout_seconds)
                          if timeout_seconds else None)

    # transformers 5.x 的 LogitsProcessorList 会内省签名：`len(params) > 2` 时，
    # 把第 3 个起的参数名当成**必传 kwargs** 去校验（logits_process.py:87-93），
    # 于是 *args/**kwargs 也会被当成必传而抛
    # ValueError: Make sure that all the required parameters ... are passed。
    # 因此这里只能有 (self, input_ids, scores) 两个参数，多一个都不行。
    def __call__(self, input_ids, scores):
        # 超时优先：到点立刻收尾（继续采样只会产垃圾并占用整条流水线）
        if self._deadline is not None and time.monotonic() > self._deadline:
            if not self.timed_out:
                self.timed_out = True
                self.timed_out_at = self._steps
                log.warning("ASR deadline hit at step %d, forcing EOS", self._steps)
            return self._force_eos(scores)

        if self.detected:
            return self._force_eos(scores)
        last = self._last_token(input_ids)
        if last is None:
            return scores
        self._steps += 1
        self.recent.append(last)
        if len(self.recent) > self.window:
            del self.recent[:len(self.recent) - self.window]
        if self._steps % 2 == 0 and self._find_loop():
            self.detected = True
            self.detected_at = self._steps
            log.info("ASR loop guard fired at token %d", self._steps)
            return self._force_eos(scores)
        return scores

    @staticmethod
    def _last_token(input_ids):
        """取最新一个 token 的 id（同时兼容 torch.Tensor / list / 嵌套 list）。

        **必须按形状分支**：旧写法对"只含最新一列"的 shape-(1,) 张量调用 .tolist()
        会拿到 int 而不是 list，再取 [-1] 直接 TypeError、被 except 吞掉——检测器
        会永久静默失效（测试正是这么抓出来的）。
        """
        try:
            x = input_ids
            if hasattr(x, "dim") and hasattr(x, "tolist"):
                x = x.tolist()
            # 逐层剥掉 batch 维，直到是一维序列
            while isinstance(x, (list, tuple)) and x and isinstance(x[0], (list, tuple)):
                x = x[0]
            if isinstance(x, (list, tuple)):
                return int(x[-1]) if len(x) else None
            return int(x)
        except Exception:
            return None

    def _force_eos(self, scores):
        try:
            scores = scores.clone()
            scores.fill_(float("-inf"))
            scores[:, self.eos_token_id] = 0.0
        except Exception:
            pass
        return scores

    def _find_loop(self) -> bool:
        n = len(self.recent)
        for u in range(_LOOP_TOKEN_MIN_UNIT, _LOOP_TOKEN_MAX_UNIT + 1):
            if n < u * _LOOP_TOKEN_MIN_COUNT:
                break
            counts: dict = {}
            for i in range(0, n - u + 1, u):   # 非重叠切片，天然不虚增计数
                g = tuple(self.recent[i:i + u])
                counts[g] = counts.get(g, 0) + 1
            if not counts:
                continue
            k = max(counts.values())
            if k >= _LOOP_TOKEN_MIN_COUNT and k * u / n >= _LOOP_TOKEN_COVERAGE:
                return True
        return False


# 搬自 V1 mnotes_core/recorder.py:149-172
class _DeadlineStop:
    """墙钟硬超时（StoppingCriteria 钩子，每生成一个 token 调用一次）。

    model.generate() 本身没有 timeout 参数，这是在不换模型、不改架构的前提下
    唯一能做到"精确到 1 个 token"的硬超时手段。实测 30 秒段正常生成需 13-15 秒、
    10 秒段约 4 秒，因此默认 20 秒对 10 秒段有 ≥5 倍裕量，正常段不会被误杀。
    """

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self._deadline = time.monotonic() + self.seconds
        self.timed_out = False
        self.calls = 0

    def __call__(self, input_ids, scores, **kwargs):
        self.calls += 1
        if time.monotonic() > self._deadline:
            self.timed_out = True
            return True
        return False

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())


# ═══════════════════════════════════════════════════════════════════════════
# 二、文本级重复剥离（第 4 层，兜底）
# ═══════════════════════════════════════════════════════════════════════════

# 搬自 V1 mnotes_core/recorder.py:177-187
# ── 重复循环剥离 ────────────────────────────────────────────────
# 旧实现只扫"周期 2~4 字的单位连续重复 ≥7 次"，对被截断的短单位循环很细，
# 但**真实故障的循环周期是 5~7 字**（2026-09-17 实测两条：周期 7 的
# 「写啊哈，你可以」×399、周期 5 的「是错的，那」×663），完全落在搜索空间之外，
# 2799/3333 字符一个字都没删掉。现在改为"周期上限 40 + 覆盖率判据"：
# 找出覆盖该行比例最高的重复单位，覆盖率 ≥ 阈值即判为循环，只保留一组。
_REPEAT_LOOP_MIN_UNIT = 2      # 重复单位最短长度（2 字起步，避免误删"哈哈哈哈哈"）
_REPEAT_LOOP_MAX_UNIT_FLOOR = 4  # 周期上限至少扫到这里（兼容旧行为）
_REPEAT_LOOP_MIN_COUNT = 4     # 单位出现次数下限（非重叠计数）
_REPEAT_LOOP_MIN_TEXT = 100    # 输出 ≤100 字符时直接跳过（短文本不可能是大规模循环垃圾）
_REPEAT_SCAN_BUDGET = 12_000_000  # 周期×长度运算预算，超预算的长文本只扫小周期


# 搬自 V1 mnotes_core/recorder.py:190-226
def _loop_coverage(text: str, max_unit: int | None = None):
    """找覆盖率最高的重复单位。返回 (best_unit|None, 计数, 覆盖率)。

    对每个周期 u，按"非重叠"计数统计最高频 u-gram 的出现次数 k，
    覆盖率 = k*u/len(text)。取覆盖率最高者。
    """
    n = len(text)
    if n <= 0:
        return None, 0, 0.0
    if max_unit is None:
        # 改自 V1 `:200` 的 `max_unit = REPEAT_MAX_UNIT`（`mnotes_core/config.py` 的模块级
        # 常量，import 时求值一次）：V2 的参数一律从配置读出，这两个键都是热读键。
        max_unit = int(config_module.get("asr.repeat_max_unit"))
    max_unit = max(_REPEAT_LOOP_MAX_UNIT_FLOOR, int(max_unit))
    max_unit = min(max_unit, n // _REPEAT_LOOP_MIN_COUNT)
    if max_unit < _REPEAT_LOOP_MIN_UNIT:
        return None, 0, 0.0

    budget = max(1, _REPEAT_SCAN_BUDGET // max(1, n))
    if max_unit > budget:
        max_unit = max(_REPEAT_LOOP_MAX_UNIT_FLOOR, budget)
        if max_unit < _REPEAT_LOOP_MIN_UNIT:
            return None, 0, 0.0

    best_unit, best_count, best_cov = None, 0, 0.0
    for u in range(_REPEAT_LOOP_MIN_UNIT, max_unit + 1):
        counts: dict[str, int] = {}
        for i in range(0, n - u + 1, u):      # 非重叠切片，天然不虚增计数
            g = text[i:i + u]
            counts[g] = counts.get(g, 0) + 1
        if not counts:
            continue
        g, k = max(counts.items(), key=lambda kv: kv[1])
        if k < _REPEAT_LOOP_MIN_COUNT:
            continue
        cov = k * u / n
        if cov > best_cov:
            best_unit, best_count, best_cov = g, k, cov
    return best_unit, best_count, best_cov


# 搬自 V1 mnotes_core/recorder.py:229-249
def _strip_repeat_loops(text: str, max_unit: int | None = None,
                        min_coverage: float | None = None) -> str:
    """砍掉"某个 ≤max_unit 字单位重复到覆盖大部分文本"的循环，只留一组。

    判据（实测标定：命中真实故障行 0 误报）：
      1) 该单位非重叠出现次数 ≥ _REPEAT_LOOP_MIN_COUNT
      2) k*u/len(text) ≥ min_coverage（默认 0.5）
    命中后从每个出现处删到只剩第一次（保留原文前导正文），
    天然处理"前缀正常 + 后缀无限循环"这种真实形态。
    """
    if len(text) <= _REPEAT_LOOP_MIN_TEXT:
        return text
    if min_coverage is None:
        # 改自 V1 `:242` 的 `min_coverage = REPEAT_MIN_COVERAGE`，理由同 `_loop_coverage`
        # （默认值同为 0.5，改从 `meeting.config` 的 `asr.repeat_min_coverage` 读）。
        min_coverage = float(config_module.get("asr.repeat_min_coverage"))
    unit, count, cov = _loop_coverage(text, max_unit=max_unit)
    if unit is None or cov < min_coverage:
        return text
    first = text.find(unit)
    if first < 0:
        return text
    return text[:first] + unit
