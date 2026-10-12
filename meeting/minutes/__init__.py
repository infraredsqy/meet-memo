"""会后纪要这一层（`41465462` 节点）。

本包服务 spec「需求冻结 · 归属划分」里那一格：两条转写的行解析、按行切块、分块产物的契约校验、
编排提示词渲染，以及把这条链拉起来的那几件活。phase1 的四个模块各管一段，phase2 接着补上编排：

  - `meeting.minutes.lines`：`[HH:MM] 正文` 与 `[HH:MM] **S01**: 正文` 两种行形一起吃的解析器；
  - `meeting.minutes.chunk`：按行切块 + `00_index.md` 行号区间表（落 `working/<source>/`）；
  - `meeting.minutes.contract`：`segmentation.json` 的契约校验（连续性**按 source 各判一遍**）；
  - `meeting.minutes.prompt`：把路径与档位填进模板，产出子会话读的那份提示词；
  - `meeting.minutes.inputs`：找两条转写、写 `working/inputs.json`（这一场有哪几条输入、走哪一档）；
  - `meeting.minutes.status`：`meeting_minutes_status.json` 的读写（与离线那一份并列、互不覆盖）；
  - `meeting.minutes.launch`：headless DSH 子进程的启动器 + 产物四条判据；
  - `meeting.minutes.runner`：把上面几件串成一次运行（再入闸 → 切块 → 渲染 → 拉起 → 校验 → 登记）；
  - `meeting.minutes.__main__`：`python -m meeting.minutes <chunk|prompt|check>` 三个手动入口。

**本包顶层与函数体都不 import 任何重依赖**：`torch` / `transformers` / `numpy` / `av` /
`webrtcvad` / `starlette` / `sounddevice` 一个都不碰，只用标准库。这条由
`tests/test_minutes_contract.py` 在**全新解释器**里盯着：`import meeting.minutes` 之后
`sys.modules` 里不该出现上面那几样。
"""

__all__: list[str] = []
