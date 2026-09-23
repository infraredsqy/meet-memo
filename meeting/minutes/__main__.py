"""`python -m meeting.minutes <子命令>`：会后纪要这一层的三个手动入口。

三个子命令各自转发到对应模块的 `_main()`，本文件自己不做任何解析之外的事：

  - `chunk <场次目录>`：把场次目录里的转写切成块 + 写行号区间表；
  - `prompt <场次目录>`：渲染编排提示词（模板归 `prompts/meeting_final.md`）；
  - `check <segmentation.json>`：校验一份分段方案（顺带把 `working/inputs.json` 里
    逐 source 的真实行数读进来，走「以输入清单为准」那一路）。

`prompt` 与 `check` 两个子命令是给**手动排查**用的（提示词阶段 2 末尾那条
`python -m meeting.minutes check working/segmentation.json` 走的就是 `check`）：phase2 的
runner 直接调那三个模块的函数，不经过这里。两个入口都不加载 `meeting.config`
（一个后端子命令不必把家目录、日志、输出根那条配置链捆在自己身上）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from meeting.minutes import chunk as chunk_module
from meeting.minutes import contract as contract_module
from meeting.minutes import prompt as prompt_module

#: 子命令与它转发到的那个 `_main()`（顺序就是 `--help` 里的顺序）
SUBCOMMANDS: dict[str, str] = {
    "chunk": "按行切块并写出行号区间表",
    "prompt": "渲染会后纪要的编排提示词",
    "check": "校验一份 segmentation.json",
}


def _inputs_near(segmentation: Path) -> Path | None:
    """在传进来的那份方案旁边找 `working/inputs.json`（找不着给 `None`）。

    `check` 拿到的路径，两种**参数写法**都认（跟落点口径无关，纯粹是手动排查时好使：方案平时
    在 `working/` 这一层，翻某一条 source 的东西时也可能传来一条子目录下的路径）：先看方案自己
    那一层，再看 `working/` 那一层。
    """
    working = segmentation.parent
    candidates = (
        working / contract_module.INPUTS_NAME,
        working.parent / contract_module.INPUTS_NAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _expected_lines_from_file(path: Path) -> dict[str, int] | None:
    """读一份 `inputs.json` 的逐 source 真实行数；读不动时返回 `None`（退回方案自述那一档）。

    **读不动不报错、只是不核**：手动排查时输入清单还没落盘（只跑了一次 `chunk`）是正常用法，
    这一档的判据退回方案自己声明的 `total_lines`（对象形状）；而多 source + 整数那一档
    在那边照样会报错，不会因为这里读不动就静默放过。
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    lines = contract_module.expected_lines_from_inputs(document)
    return lines or None


def _check_main(argv: Sequence[str] | None = None) -> int:
    """`python -m meeting.minutes check <segmentation.json> [--max-segments N] [--inputs P]`。

    只回答「这一份契约过不过」：不过就把那道错误原文打到 stderr 并退 1，读盘的人据此知道
    是哪一条 source 的哪一段不对。

    **顺带把「以输入清单为准」那一路接上**：在方案旁边找到 `inputs.json` 就拿它里面逐 source
    的真实行数当 `expected_lines`（正是 `runner` 平时走的那一路）；找不到就退回方案自己声明的
    `total_lines`。这一路是本条判据平时真正生效的那条路——此前 `check` 手上没有逐 source 的
    事实，多 source 场次里 `total_lines` 写整数的那一份会一路错到「静默不核」。
    """
    parser = argparse.ArgumentParser(
        prog="python -m meeting.minutes check",
        description="校验一份 segmentation.json 是否满足分块→逻辑段的契约。",
    )
    parser.add_argument("path", help="segmentation.json 的路径")
    parser.add_argument(
        "--max-segments",
        type=int,
        default=contract_module.DEFAULT_MAX_SEGMENTS,
        help=f"每条 source 的段数上限（默认 {contract_module.DEFAULT_MAX_SEGMENTS}）",
    )
    parser.add_argument(
        "--inputs",
        default=None,
        help=(
            "输入清单 `working/inputs.json` 的路径（默认在方案旁边自己找："
            "`<方案所在目录>/inputs.json`，再看它的上一层）"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    # 「以输入清单为准」那一路：读得到 inputs.json 就拿逐 source 的真实行数去核，
    # 读不到就退回方案自己声明的 total_lines（多 source + 整数那一档在那边照样报错）。
    inputs_path = Path(args.inputs) if args.inputs else _inputs_near(Path(args.path))
    expected = _expected_lines_from_file(inputs_path) if inputs_path is not None else None
    try:
        payload = contract_module.load_segmentation(
            args.path, expected_lines=expected, max_segments=args.max_segments
        )
    except FileNotFoundError:
        print(f"分段方案不在：{args.path}", file=sys.stderr)
        return 2
    except contract_module.SegmentationContractError as exc:
        print(f"契约不过：{exc}", file=sys.stderr)
        return 1
    sources = sorted({str(segment.get("source")) for segment in payload["segments"]})
    print(f"契约通过：{len(payload['segments'])} 段，覆盖 {sources}")
    if expected:
        print(f"逐 source 的真实行数取自 {inputs_path}：{expected}")
    else:
        print("没有读到输入清单：末段核对按方案自己声明的 total_lines")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m meeting.minutes <子命令> …` 的总入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m meeting.minutes",
        description="会后纪要这一层的文字机器（切块 / 提示词 / 契约校验）。",
    )
    parser.add_argument("command", choices=sorted(SUBCOMMANDS), help="要跑的活")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="转发给子命令的参数")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "chunk":
        return chunk_module.main(args.args, prog="python -m meeting.minutes chunk")
    if args.command == "prompt":
        return prompt_module.main(args.args, prog="python -m meeting.minutes prompt")
    return _check_main(args.args)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())


__all__ = ["SUBCOMMANDS", "main"]
