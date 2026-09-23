"""`meeting/summary/parse.py` 的契约用例：`bullets` 的围栏容忍与三小段的标签归类。

**第一组**（`parse_bullets`）照 V1 `_parse_bullets()` 的三步走，四种样本的期望行为逐条钉住：

  - 带 ```json 围栏的样本 → 去掉围栏后正常解析；
  - 前面带解释文字的样本 → 靠 `\\{.*\\}` 那一份候选抠出来；
  - `bullets` 里混了数字的样本 → `None`（触发重试）；
  - `{"bullets": []}` → **空列表**（B15 的「空数组合法」，不算失败）。

**第二组**（`split_sections`）盯三条固定标签的归段：认出的进对应段并去掉标签、认不出的进
第一段并计数、一条都认不出来时调用方（`all_sections_unlabeled()`）能看出来该重试。
"""

from __future__ import annotations

from meeting.summary.parse import (
    SECTION_LABELS,
    SECTION_ORDER,
    all_sections_unlabeled,
    parse_bullets,
    split_sections,
)

#: 一份三小段齐备的模型输出（每条 bullet 用固定标签开头）
GOOD_OUTPUT = (
    '{"bullets": ['
    '"【讲什么】在过下个季度的排期", '
    '"【讲到哪】刚说到第三项的负责人还没定", '
    '"【丢掉什么】21:33 远端有一句识别超时（20 秒），那一句谁说的不知道"'
    "]}"
)


def test_parse_bullets_reads_a_plain_json_object() -> None:
    """最顺的一档：`{"bullets": [...]}` 直接就是合法 JSON。"""
    assert parse_bullets('{"bullets": ["a", "b"]}') == ["a", "b"]


def test_parse_bullets_tolerates_a_json_fence() -> None:
    """外面裹一层 ```json 围栏：去掉首行末行之后照样解析。"""
    fenced = "```json\n" + GOOD_OUTPUT + "\n```"

    assert parse_bullets(fenced) == parse_bullets(GOOD_OUTPUT)
    assert parse_bullets(fenced) is not None


def test_parse_bullets_tolerates_an_unlabelled_fence() -> None:
    """围栏不带语言标记（裸 ```）时同样去掉。"""
    assert parse_bullets('```\n{"bullets": ["a"]}\n```') == ["a"]


def test_parse_bullets_digs_the_object_out_of_surrounding_prose() -> None:
    """模型在前面多写了一句解释：靠 `\\{.*\\}` 那份候选把对象抠出来。"""
    noisy = '好的，这是结果：\n{"bullets": ["一", "二"]}\n希望有用。'

    assert parse_bullets(noisy) == ["一", "二"]


def test_parse_bullets_digs_out_a_multiline_object_wrapped_in_prose() -> None:
    """外面裹着文字、对象自己还跨行：第二份候选要一路吃到收尾那个 `}`（`re.DOTALL` 那一档）。

    这是两份候选里**只有第二份能救**的那一格：整段 `strip()` 之后既不是合法 JSON（前面有
    「好的」），对象又跨了好几行，于是正则 `\\{.*\\}` 必须能跨行匹配才抠得出它 ——
    去掉 `re.DOTALL` 这条用例当场变红（`.` 匹配不到换行，候选只在单行对象上成立）。
    """
    noisy = (
        "好的，这是这次的结果：\n"
        "```json\n"
        "{\n"
        '  "bullets": [\n'
        '    "【讲什么】跨行的第一条",\n'
        '    "【讲到哪】跨行的第二条"\n'
        "  ]\n"
        "}\n"
        "```\n"
        "希望有用。"
    )

    assert parse_bullets(noisy) == ["【讲什么】跨行的第一条", "【讲到哪】跨行的第二条"]


def test_parse_bullets_strips_each_element() -> None:
    """每个元素各自 `strip()`（照 V1 的 `[b.strip() for b in bullets]`）。"""
    assert parse_bullets('{"bullets": ["  收尾的空格  ", "\\t制表符\\t"]}') == [
        "收尾的空格",
        "制表符",
    ]


def test_parse_bullets_accepts_an_empty_array() -> None:
    """`{"bullets": []}` 交回空列表：**合法**，模型判断这一段没有可讲的要点。"""
    assert parse_bullets('{"bullets": []}') == []


def test_parse_bullets_rejects_elements_that_are_not_strings() -> None:
    """`bullets` 里混了数字：整段判失败（元素是字符串是 B15 的信封口径）。"""
    assert parse_bullets('{"bullets": ["正文", 42]}') is None
    assert parse_bullets('{"bullets": [1, 2]}') is None


def test_parse_bullets_rejects_blank_elements() -> None:
    """空串、纯空白串的元素同样判失败（它们不是一条要点）。"""
    assert parse_bullets('{"bullets": ["正文", "   "]}') is None


def test_parse_bullets_rejects_a_missing_or_non_list_bullets_field() -> None:
    """没有 `bullets` 键、或 `bullets` 不是数组：判失败。"""
    assert parse_bullets('{"items": ["a"]}') is None
    assert parse_bullets('{"bullets": "a"}') is None


def test_parse_bullets_rejects_a_non_object_top_level() -> None:
    """顶层是数组或标量：判失败（要的是 dict 那一层）。"""
    assert parse_bullets('["a", "b"]') is None
    assert parse_bullets('"a"') is None
    assert parse_bullets("") is None
    assert parse_bullets("这不是 JSON") is None


def test_split_sections_sorts_the_three_labels() -> None:
    """三条固定标签各归各段，标签本身被去掉。"""
    sections, unlabeled = split_sections(
        [
            "【讲什么】在过排期",
            "【讲到哪】第三项的负责人还没定",
            "【丢掉什么】21:33 远端丢掉一句",
        ]
    )

    assert unlabeled == 0
    assert sections["what"] == ["在过排期"]
    assert sections["where"] == ["第三项的负责人还没定"]
    assert sections["lost"] == ["21:33 远端丢掉一句"]


def test_split_sections_tolerates_leading_whitespace() -> None:
    """标签是前缀，前后允许空白（模型偶尔会缩进）。"""
    sections, unlabeled = split_sections(["  【讲什么】   缩进过的一句  "])

    assert unlabeled == 0
    assert sections["what"] == ["缩进过的一句"]


def test_split_sections_puts_unlabelled_items_into_what_and_counts_them() -> None:
    """个别认不出的条目落到第一段并计数，认得的那几条照常归位。"""
    sections, unlabeled = split_sections(["【讲什么】有标签", "这一条漏了标签"])

    assert unlabeled == 1
    assert sections["what"] == ["有标签", "这一条漏了标签"]
    assert sections["where"] == []
    assert sections["lost"] == []


def test_split_sections_reads_only_the_label_at_the_start_of_a_line() -> None:
    """标签写在句子中间的不算标签：那一条原样留在第一段（别把正文咬掉一块）。"""
    sections, unlabeled = split_sections(["我们讲到了【讲到哪】这一步"])

    assert unlabeled == 1
    assert sections["what"] == ["我们讲到了【讲到哪】这一步"]
    assert sections["where"] == []


def test_split_sections_keeps_a_bare_label_as_an_empty_entry() -> None:
    """只有标签、后面没正文：认得出、归到那一段，正文是空串（调用方看得见这一条）。"""
    sections, unlabeled = split_sections(["【讲到哪】"])

    assert unlabeled == 0
    assert sections["where"] == [""]


def test_split_sections_on_nothing_is_all_empty() -> None:
    """一条都没有（空数组那一档）：三段都是空的，一个认不出的都没有。"""
    sections, unlabeled = split_sections([])

    assert unlabeled == 0
    assert list(sections) == list(SECTION_ORDER)
    assert all(sections[name] == [] for name in SECTION_ORDER)


def test_all_sections_unlabeled_flags_the_retry_case() -> None:
    """一条标签都没认出来 → `True`（调用方据此转成一次重试）；个别认不出 → `False`。"""
    none_recognized, count = split_sections(["漏了标签", "还是漏了"])
    some_recognized, some_count = split_sections(["【讲什么】有标签", "漏了标签"])
    nothing, zero = split_sections([])

    assert all_sections_unlabeled(none_recognized, count) is True
    assert all_sections_unlabeled(some_recognized, some_count) is False
    assert all_sections_unlabeled(nothing, zero) is False


def test_section_labels_match_the_design_wording() -> None:
    """三条标签逐字对齐设计章 2.3 的三小段（界面照它排）。"""
    assert SECTION_LABELS == {
        "what": "【讲什么】",
        "where": "【讲到哪】",
        "lost": "【丢掉什么】",
    }
