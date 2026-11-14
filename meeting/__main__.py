"""`python -m meeting` → `meeting.cli.main()`。

只做这一件事：把模块级执行入口接到命令行入口上。
`main()` 自己会 `sys.exit(<退出码常量>)`，其 `SystemExit` 由此处原样冒到解释器。
"""

from meeting.cli import main

if __name__ == "__main__":
    main()
