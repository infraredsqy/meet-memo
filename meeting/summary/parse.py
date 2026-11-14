"""模型输出的解析：`bullets` 的信封 + 三小段的标签归类。

两块各管一件事，合起来把「模型吐回来的一段文本」变成「界面上的三小段」。

`parse_bullets()` 三步：

  1. `strip()`；
  2. 整体以 ``` 开头就去掉首行、末行（```json 围栏的容忍）；
  3. 两份候选：整段 + 正则 `\\{.*\\}`（带 `re.DOTALL`）抓到的第一段 —— 模型在前面
     多写了一句解释时，靠这一份把那个对象抠出来。

只接受「顶层是 dict、`bullets` 是 list、每个元素 strip 后是非空字符串」这一种形状。
`{"bullets": []}` 交回**空列表**（`[]` 是合法结果：模型判断这一段没有可讲的要点，
比如纯噪声或重复，它**不算失败**）；其余一切（不是 JSON、没有 `bullets` 键、
`bullets` 里混了数字、元素是空串）交回 `None`，由调用方转成一次重试。

`split_sections()`：把每一条归到三小段。

三条固定标签是**前缀**（允许前后空白）：

    刚才在讲什么 → 【讲什么】
    讲到哪儿了 → 【讲到哪】
    丢掉了什么 → 【丢掉什么】

认出来的进对应那一段并**去掉标签**；认不出的进第一段（`what`），并且计入第二个返回值。
**一条都认不出来算解析失败**（由 `run_summary()` 转成一次重试）：三条都认不出，
说明模型压根没按约定的形状答，那几条放进哪一段都是猜的。个别认不出不算失败 ——
一个漏标签的条目不该毁掉整次总结。

标签只认**行首**那一处：`我们讲到了【讲到哪】这一步` 这种把标签写在句子中间的，
算认不出的条目（原样留在 `what` 里），这样「去掉标签」这一步不会把正文咬掉一块。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

#: 代码围栏开头的判据（只看整段是不是以 ``` 起头）
FENCE_PREFIX = "```"

#: 从整段里抠 JSON 对象的那条正则
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: 三条固定标签 → 三小段的名字
SECTION_LABELS: dict[str, str] = {
    "what": "【讲什么】",
    "where": "【讲到哪】",
    "lost": "【丢掉什么】",
}

#: 三小段的次序（`split_sections()` 的返回字典按它建，界面照它排）
SECTION_ORDER: tuple[str, ...] = ("what", "where", "lost")

#: 标签出现在**行首**时的匹配形状（前缀 + 允许的前后空白）
_LABEL_RE = re.compile(r"^\s*(【(?:讲什么|讲到哪|丢掉什么)】)\s*")

#: 标签本体 → 段名
_LABEL_TO_SECTION: dict[str, str] = {
    label: name for name, label in SECTION_LABELS.items()
}


def parse_bullets(raw: str) -> list[str] | None:
    """从模型输出里抠出 `{"bullets": [...]}`，交回字符串列表；解析不出来交回 `None`。

    三条路各自的结果：

      - `{"bullets": ["a", "b"]}` → `["a", "b"]`（元素各自 `strip()`）；
      - 外面裹一层 ` ```json ` 围栏的同一段 → 同上（围栏去掉）；
      - `这就是结果：{"bullets": [...]}` → 同上（靠第二份候选抠出来）；
      - `{"bullets": []}` → `[]`（**合法**：这一段没有可讲的要点）；
      - `{"bullets": [1, 2]}` / `{"items": [...]}` / `不是 JSON` → `None`（触发重试）。
    """
    text = str(raw or "").strip()
    if text.startswith(FENCE_PREFIX):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith(FENCE_PREFIX):
            lines = lines[1:]
        if lines and lines[-1].strip() == FENCE_PREFIX:
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    candidates = [text]
    matched = JSON_OBJECT_RE.search(text)
    if matched:
        candidates.append(matched.group(0))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        bullets = data.get("bullets") if isinstance(data, dict) else None
        if isinstance(bullets, list) and all(
            isinstance(item, str) and item.strip() for item in bullets
):
            return [item.strip() for item in bullets]
    return None


def split_sections(bullets: Sequence[str]) -> tuple[dict[str, list[str]], int]:
    """把每一条按固定标签归到三小段。

    交回 `({"what": [...], "where": [...], "lost": [...]}, 认不出标签的条数)`。
    认不出的那些**同时**进 `what`（漏一个标签不该让那一条消失），并计进第二个返回值。
    """
    sections: dict[str, list[str]] = {name: [] for name in SECTION_ORDER}
    unlabeled = 0
    for bullet in bullets:
        text = str(bullet).strip()
        matched = _LABEL_RE.match(text)
        section = _LABEL_TO_SECTION.get(matched.group(1)) if matched else None
        if section is None:
            unlabeled += 1
            sections["what"].append(text)
            continue
        sections[section].append(text[matched.end() :].strip())
    return sections, unlabeled


def all_sections_unlabeled(sections: dict[str, list[str]], unlabeled: int) -> bool:
    """是不是「一条标签都没认出来」那一档（调用方据此把它转成一次重试）。

    判据是「有内容、且认出来的条数为零」：空列表那一档（`bullets == []`）本来就没有
    可归的东西，它走成功那一条路（B15 的空数组合法）。
    """
    return bool(sections["what"]) and unlabeled == len(sections["what"])


__all__ = [
    "FENCE_PREFIX",
    "JSON_OBJECT_RE",
    "SECTION_LABELS",
    "SECTION_ORDER",
    "all_sections_unlabeled",
    "parse_bullets",
    "split_sections",
]
