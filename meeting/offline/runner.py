"""把会后离线这条链串起来（`7c6952e5` phase2）。

一次 `run_session(session_dir)` 走完这些事：

    读场次元信息 → 逐路取音频 → 逐路调引擎 → 逐路编号（`S01`、`S02`…）
    → 落三份产物（每路各一份 + 一份运行报告）→ 状态文件从 `running` 落成终态

**两路之间没有共享状态**：每一路的音频、分段编号、产物文件、度量各归各的，
两路的编号因此不会互相看见，「这条路的一号等于那条路的一号」在数据结构上表达不出来
（限定名 `mic:S01` / `loopback:S01` 里始终带着 `source`）。

## 音频来源

一路的音频按两档取，先到先用：

  1. 归档 PCM `meeting_audio_<source>.pcm`（采集侧边采边写的裸流）；
  2. 归档 MP3 `meeting_audio_<source>.mp3`（收尾转码的产物）。

PCM 是 0 字节而 MP3 在时走第二档。**两档都没有的那一路如实记成失败**（报告与状态文件里
都留痕），另一路照常跑完 —— 整场不许因为一路缺音频就崩掉。

**两路分别调用引擎，绝不先混音再跑**：混了就没法「各自编号、互不相认」（R10）。

## 分块（`post.chunk_seconds`）

音频长过 `post.chunk_seconds` 秒时切成**连续不重叠**的块，逐块调引擎，再把结果拼回整场
时间轴。三条规则写在下面，改之前先读完：

  1. **块内编号带命名空间**：第 `NN` 块的引擎标签 `Sxx` 进产物前变成 `cNN:Sxx`
     （`c01:S01`），`label_raw` 记的就是带命名空间的那个形式。块与块之间**不做任何映射、
     不做相似度比较、不做合并** —— 想合并得先判断「这两段是不是同一个人」，判断错就把
     两个人并成一个编号（R10 里不可接受的错），不合并最多把一个人算成两个编号（可接受的错）。
     `phase3` 实测那对 0.970 / 0.922 的相似度正说明「看起来像同一个人」的证据确实在，
     但这个判断归下游：`label_raw` 里带着 `cNN`，下游随时能自己算。
  2. **整场时间轴**：每块的 `start` / `end` 都加上该块在原录音上的偏移秒数，JSON 与转写
     报的都是原录音上的位置。秒级只进 JSON，转写正文照旧分钟级 `[HH:MM]`。
  3. **块失败必须露出来**：某一块抛错或一段都没交回时，那一路落 `partial`（状态文件里
     `partial = true`、`error` 写明是哪一段区间没有转写），报告里单列一节列出缺失区间，
     转写文件第一行是一条注释行说明缺了哪一段。**静默 `done` 加一段悄悄消失的音频
     是禁止的**：这份转写是下游的权威底本。

块数为 1 时（音频不长于块长、或 `post.chunk_seconds = 0`）走的就是单趟：引擎拿到的是
原始音频路径，标签不带命名空间，产物与加这个键之前逐字一致。

## 数字静音门（调引擎**之前**的一道闸）

一段音频的样本全是零时，那一块**根本不送进引擎**，如实记成「跳过」。判据在
`is_digital_silence()`：**峰值低于半个 int16 步长**（换算成满量程格数之后没有一个够得着
半格的样本），并且这一段至少有 `MIN_GATE_SAMPLES` 个样本。

**判据为什么只认「全零」这一个尺寸**：`250906` 那次验收实测的正是这一档 —— 环回那一路
的五块（第 8、16、17、18、19 块）经 `volumedetect` 读出 `-91.0 dB`、样本全为零，而引擎
在它们身上编出了 20 690 字（占那一路整份输出的 69.32%）：四个块各吐一句
`[S01][S01][S01]I'm sorry, I can't assist with that request.`，末块把 `The.` 重复了 4091 遍。
那一档的样本**一个非零值都没有**，因此「峰值够不着半格」这条判据把实测那一档一条不漏地
拦下。往上一格放宽（例如按 dBFS 给一个 -60 dB 的地板）就开始赌「安静的话还算不算话」：
`meeting/asr/glm.py` 的 `_is_noise_segment` 用 VAD + RMS(<150) + 语音帧(<5) 三条 AND
判「近静音」，那条口径服务于**会中**的逐段预判（丢一段的代价是那一行没有字，且那一段
本来就只有 10 秒），会后这一层丢一段的代价是**权威底本整块消失**，两者不对称，
所以这里取的是比它更严、也不需要 VAD 的一档：**只要有一个够得着半格的样本，这一块照跑**。
`_is_noise_segment` 与这里的分工是「会中预筛按概率丢、会后闸门按全零丢」，
两处都把命中的那一段记成**成功的「什么都没识别出来」**，都不记成失败。

**两种波形形态用同一把尺子、同一个分界**：归档 PCM 上读出来的是 int16 计数（整数，
一格就是满量程的 `1/32767`），`decode_audio()` 解出来的是 `[-1, 1]` 的 float32。
判据把两者都过 `samples_to_count_floats()` 换算成**满量程格数**再比，而且**不截断**成整数：
截断会让 float 的 `3.0e-5` 与 `3.05e-5`（半格上下）一起变成 0，于是同一条判据在两种形态上
分界不一致（那正是审查提的 m2）。换到格数上之后两侧的分界都落在**半格**：int16 的 ±1 是
1 格、float 的 `1/32767` 也是 1 格，两侧对同一个样本给出同一个答案。少了这一步换算，
float32 的整段波形会被 `int()` 截成一串 0 —— 一串真有声的音频看着像全零，19 块一条不漏地
全被跳过（复跑第一版实测踩到过，用例
`test_a_float_waveform_with_a_real_signal_never_looks_silent` 盯着它）。切块与写 wav 那条路
仍走 `samples_to_counts()` 的**整数**换算（与 `write_chunk_wav()` 同形），判据这一层不碰它。

**安静但有声的块必须照跑**：`SILENT_PEAK_FLOOR = 0.5` 这条判据下，峰值为 ±1 的样本
（16 bit 满量程的 -90.3 dB，也就是 1 格）照样送进引擎。「写出来的块全是零」这个自检因此
依然不放在产品代码里当错误判据（见 `write_chunk_wav()` 的说明）——它只在**调引擎之前**
决定要不要花那一次推理。

**单趟与分块两条路都过这道门**。`measure_span()` 按成本从低到高挑一条路：

  1. **归档 PCM** 上按字节扫那一段（`pcm_span_is_silent()`）：见到第一个非零字节就出结果，
     一场静音会议才需要读满；它排在波形前面，因为单趟跑的 MP3 与归档 PCM 是**同一段**
     音频，而已经解好的波形还要占内存。
  2. **已经解好的波形**切片判：分块跑的那份波形由 `_plan_for_source()` 量长度时解出来
     （量长度、切块、过门共用一次解码），单趟跑的 MP3 也用它 —— 同一个文件在量长度那一步
     就已经解成波形了，门口不必再解第二遍。
  3. **现解一次音频文件**：这一档留给「波形没到手、又不是 PCM」的情形，产品路径不经过它
     （见下面「产物里的尺子名」那一节）。

代价这一栏如实说：单趟跑的 MP3 现在**多花一次解码**（短于块长的录音才走这条路，默认块长
450 秒，也就是一次**短会议**的解码），换回来的是「短会议的权威底本不许被整段假正文污染」。
整段一次跑**不再有绕开门的那一档**。

**产物里的尺子名对得上产品路径实际走的那一条**（`ChunkRun.gate_method` / 逐路的
`gate.method`，词表见 `GATE_METHOD_*`）。**单趟跑的 MP3 记的是 `waveform`**：
`_plan_for_source()` 为了量长度已经把整份音频解成了波形，那一段波形接着被门切片用掉，
所以门口不会现解第二遍、也不会记成别的名字。四条落点：

  - **归档 PCM 缺席、MP3 解得开**（产品路径上短会议那一档）→ `waveform`；门量的那份波形
    被写成 wav 喂给引擎（同一次解码，不重解）；
  - **归档 PCM 是 0 字节、音频改走 MP3**（`resolve_audio()` 只认字节数 `>= 2` 的归档 PCM）
    → 仍是 `waveform`：尺子按**喂给引擎的那份音频**的容器挑，`.pcm` / 非 `.pcm` 看 `path`
    自己，归档 PCM 在场但空着不改变这一格；
  - **音频走归档 PCM**（MP3 缺席、或 MP3 是 0 字节而 PCM 非空）→ `pcm_bytes`，按字节扫，
    引擎拿到的是同一个文件；
  - **量长度那一步就解不开** → `decode_at_plan`，结论落「判不了」（那一段照常送引擎，
    由引擎如实报错）。

前三条合起来说的是**同一件事**：门量的那份音频与喂给引擎的那份**是同一份**（归档 PCM 直接
喂原文件，解好的波形写成 wav 再喂），于是 `audio.file` 与 `chunks[].gate_method` 对得上。
第四条是唯一的例外：解不开时门没量到东西（`gate = "unjudged"`），引擎拿到的是**原文件**
（单趟那一趟）或归档 PCM，由它自己如实报错 —— 这一档的产物形状与健康那一趟不同形，
`gate.applied = false` 就是给这件事留的。

`decode_now` 这个词在**产品路径上拿不到**：非 PCM 的音频要么在量长度那一步解开了（走 2、记
`waveform`），要么在那里就没解开（走「判不了」、记 `decode_at_plan`）。`measure_span()` 里
现解一次那一支只在「量长度那一步压根没试过」（调用方自己注入波形或另给路径）时才会走到，
产品路径不经过它。

**门判不了的时候**：音频解不开、要的那一段读不全（文件比区间短）时，`measure_span()` 交回
`silent = None`（`gate_unjudged`），那一段**照常送引擎** —— 读不到就按「可能有声」放行，
与 `pcm_span_is_silent()` 的判据同源，两条路因此不会对同一段给出两个答案。**这件事必须露在
产物里**：

  - 逐块那一行记 `gate = "unjudged"` 与 `gate_detail`（为什么判不了），分段为 0 时 `reason`
    落 `gate_unjudged`；
  - 逐路 `gate.applied = false` + `unjudged_spans`（对照表 `audio.gate`、状态文件
    `artifacts.<source>.gate`）；
  - 状态文件顶层 `gate` 按路列出判不了的那几段，报告单列一节。

**每一块都带一个结论词**（`ChunkRun.reason`，机器可读）：

  - `digital_silence`：没声，引擎调用都没发生；
  - `engine_error`：引擎抛错；
  - `no_segments`：引擎跑成了，一段都没交回；
  - `zero_span_segments`：跑成了，交回的段**每一段都是零时长**；
  - `transcribed`：跑成了，交回的段有真实时长；
  - `gate_unjudged`：跑成了、段有真实时长，但这一段的静音与否**没人判过**。

前两个词说的是「转写缺这一段」，第三、四个词说的是「有字，但形状要人看一眼」，最后一个词
说的是「这一段的静音与否没有依据」。于是「这一段为什么没有字」在逐块那一行上直接读得出来，
不必从整场的那几格反推；「门判过没有」另看同一行的 `gate`（`silent` / `not_silent` /
`unjudged`）与 `gate_method`（凭什么判的）。

**跳过的区间要露出来**（四档必须分得开：正常跑完 / 没声跳过 / 引擎失败 / 跑成而段全零时长）：

  - 逐块记账 `ChunkRun.silent`、`ChunkRun.gate` 与 `ChunkRun.reason`（`digital_silence`）；
  - 对照表 `audio.silent_spans` 与 `audio.silent`，状态文件顶层的
    `silent` / `silent_spans`，报告单列一节，转写第一行一条注释行（`silent_note()`）；
  - `state` 仍是 `done`、`partial` 仍是 `false`：**没声这一档什么都没出错**。

`REASON_DIGITAL_SILENCE` 与 `REASON_GATE_UNJUDGED` 只进 `reasons` 这个分布，**不进**缺失区间：
`missing_spans` 说的是「该有转写而没有」，没声的块没有内容可转、判不了的块照常跑了引擎，
几批区间不相交。

## 零时长的段

零时长（`end <= start`）的段在产物里**留着、只数不改**：解析层对「模型漏写了 `[结束]`」
的那一段故意把 `end` 退回 `start`（`engine.py` 的 `_build()`），丢掉它就是丢掉一段真正文。
实测的假正文**每一段都是零时长**，这个形状因此是有用的异常信号，逐路记在对照表的
`audio.zero_span_segments` 与报告里，**逐块那一行上也数一份**（哪一块出了这个形状）。

## 惰性

`torch` / `transformers` / `av` 都不在本模块顶层 import：它们要么在 `meeting/offline/audio.py`
的函数体内，要么在 `MossOfflineEngine.transcribe()` 里。`import meeting.offline.runner`
因此只拉标准库与包内模块（由 `tests/test_post_hook.py` 在全新解释器里盯着）。

## 尾巴上的会后纪要钩子（`minutes.hook`）

`run_session()` 把三份产物与状态文件的终态都落完之后，**最后**通知一次会后纪要那一层
（`_announce_minutes()`，`41465462` phase2 加的挂点）：

  - 顺序不能反（纪要的输入就是这条离线转写，先出纪要就永远只有降级档）；
  - 这一层炸了**不影响离线那三份产物、也不改 `meeting_offline_status.json`**（全程
    `try/except BaseException` 兜住，只记 log）；
  - `minutes.hook = null` 与环境里的 `MEETING_MINUTES_CHILD=1` 两档都安静跳过；
  - 它自己的账记在**另一份**状态文件 `meeting_minutes_status.json` 里（那一份归纪要那一层写）。

音频时长的换算只有一处口径：PCM = 字节数 / 2 / 16000（16k 单声道 int16），
MP3 = 解码后样本数 / 16000（`decode_audio()` 给出的就是 16k 单声道波形）。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, TYPE_CHECKING

from meeting import config as config_module
from meeting.offline import artifacts
from meeting.offline.engine import OfflineEngine, OfflineSegment
from meeting.offline.speakers import number_speakers

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注，运行时不 import（惰性纪律）
    import numpy as np

_log = logging.getLogger("meeting.offline.runner")

#: 两路（与 `meeting.backends.base.SOURCE_IDS` 的口径一致；这里不 import 它，
#: 免得 `meeting.backends` 这条链被包进会后路径）
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")

#: 归档 PCM 的形状（`meeting/capture/archive.py` 的同一档）
PCM_SAMPLE_RATE = 16000
PCM_SAMPLE_WIDTH = 2

#: 空 PCM 的判据：一个 int16 样本都装不下（与 `capture.archive.EMPTY_PCM_BYTES` 同档）
EMPTY_PCM_BYTES = PCM_SAMPLE_WIDTH

#: `post.chunk_seconds` 缺席时的块长（秒）。**与 `config.DEFAULT_VALUES` 里那一格同值**：
#: 这里留着是为了让本模块在拿不到配置对象时也有一个实测安全档可用（`post.hook` 那条后台
#: 线程会走到这条路）。取值理由见 `config.py` 里 `post.chunk_seconds` 上面那段注释。
DEFAULT_CHUNK_SECONDS: float = 450.0

#: `post.chunk_seconds` 的显式关掉档：整段一次跑（R9 的「拿录音整段跑」）
CHUNK_SECONDS_OFF: float = 0.0

#: `post.engine` 的缺省落点（`auto` → 本节点的引擎实现，与 `post.hook` 的 `auto` 同形）
DEFAULT_ENGINE_TARGET = "meeting.offline.engine:MossOfflineEngine"

#: 失败原因的词表（报告与状态文件里都用这几个名字，下游按它分档）
REASON_NO_AUDIO = "no_audio"
REASON_EMPTY_PCM = "empty_pcm"
REASON_ENGINE_ERROR = "engine_error"
REASON_NO_SEGMENTS = "no_segments"
#: 分块模式下**每一块都失败**：这一路一段都没留下来（与「一块坏、别的块还在」的
#: `partial` 分开记，下游按这个词档区分「全丢」与「缺一段」）
REASON_ALL_CHUNKS_FAILED = "all_chunks_failed"
#: 数字静音门跳过的那一块。**它不是失败**：这个词只进 `reasons` 这个分布，
#: 不进缺失区间（见模块头「数字静音门」）。
REASON_DIGITAL_SILENCE = "digital_silence"
#: 引擎跑成了、交回的段**每一段都是零时长**（`end <= start`）。这一档有字，字却落在
#: 一个没有长度的区间上：零时长段是解析层对「模型漏写了结束标签」的正常形状，而实测那台
#: 引擎在数字静音上编出来的假正文**每一段都是零时长**。零段与真内容都读不出来时，
#: 这一格是唯一的分辨依据。
REASON_ZERO_SPAN_SEGMENTS = "zero_span_segments"
#: 引擎跑成了、交回的段一个都不少（每一段都有真实时长）。正常那一档的名字。
REASON_TRANSCRIBED = "transcribed"
#: 门**判不了**这一段的静音与否（音频解不开、读不全）：这一段照常送引擎，跳过与否由引擎
#: 说了算。这一档什么都没出错，但它必须与「门判过、放行」分得开 —— 少了这一格，一次没门
#: 可用的运行在产物里与一次健康的运行长得一模一样（见模块头「门判不了的时候」那一节）。
REASON_GATE_UNJUDGED = "gate_unjudged"

#: 数字静音门的判据：峰值的上界（int16 满量程的**格数**）。**半格是开区间**：
#: 峰值**低于**半格（`peak < SILENT_PEAK_FLOOR`）算静音，正好落在半格上算有声。
#: 这一格就是文档那条规则「峰值低于半个 int16 步长算静音」在代码里的落点。
#:
#: 取值理由见模块头：实测那一档（`250906` 第 8/16/17/18/19 块，`-91.0 dB`）的样本
#: 一个非零值都没有，而峰值为 ±1 的样本（-90.3 dB）照样是「可能有声」。
#: 放宽这一格就开始赌「安静的话还算不算话」，会后这一层赌不起。
#:
#: **int16 与 float32 两种形态用同一条判据**：峰值按 `samples_to_count_floats()` 换算成
#: 「满量程的几格」再比，两侧都**不截断**。于是 int16 的 ±1（1 格）与 float 的
#: `1/32767`（1 格）落在同一侧，判据对两种形态给出同一个答案。整数那一侧的半格
#: 落不进任何样本（整数里没有比 1 更小的非零值），开区间与闭区间在 int16 上同解；
#: 差只出现在 float 上**正好等于半格**的那一个样本（`0.5 / 32767`），
#: 按上面的规则它算有声。
SILENT_PEAK_FLOOR = 0.5

#: 门只判**够长的一段**：比这更短的音频不判静音（`250906` 的块都是 450 秒）。
#:
#: 收尾时被截断的那一小截尾巴（采集侧少写了几个样本）同样是全零，按全零跳过它是
#: 安全的；这一格拦住的是「短到 RMS 说不清有没有声」的那一档，宁可多花一次引擎调用，
#: 也不在样本不足时替引擎下结论。
MIN_GATE_SAMPLES = PCM_SAMPLE_RATE // 10  # 0.1 秒

#: 数字静音门的另一条路（单趟）：按字节块扫 PCM 文件，累计够这么多字节还没见到
#: 非零样本就判静音。按块读而不是整段读进内存：一场 2 小时的会归档 260 MB，
#: 静音那一趟会把它们全读一遍，用字节块扫省下的是**内存**而不是时间。
_PCM_SCAN_BLOCK_BYTES = 1 << 22

#: 「这一段 PCM 里一个非零样本都没有」的判据落在字节上：逐个试 `\x01`…`\xff` 要试 255 次，
#: 而 `bytes.translate()` + `bytes.count(0)` 一次就够 —— 256 字节的翻译表把每个非零字节
#: 映成 `\x01`、零字节映成 `\x00`，于是「非零字节数 = 长度 − 零字节数」。
_PCM_SCAN_TABLE = bytes(0 if index == 0 else 1 for index in range(256))

#: 块内标签的命名空间模板：第 `NN` 块的 `Sxx` 变成 `cNN:Sxx`（`c01:S01`）
CHUNK_LABEL_TEMPLATE = "c{index:02d}:{label}"

#: 门在一段音频上的三种结论（`ChunkRun.gate` 与逐路的 `gate` 那一节都用这三个词）
GATE_SILENT = "silent"
GATE_NOT_SILENT = "not_silent"
GATE_UNJUDGED = "unjudged"

#: 门判静音时用的那把尺子（`MeasureResult.method`）：按字节扫归档 PCM / 量长度那一步解好的
#: 波形 / 量长度那一步解过而没成 / 门现解一次。产物里记下用的是哪一把，读方能看出这一段
#: 凭什么被判的。
#:
#: 四个词说的是**四种不同的事实**，各自落在哪里见模块头「产物里的尺子名对得上产品路径
#: 实际走的那一条」：
#:
#:   - `pcm_bytes`：按字节扫归档 PCM（见到第一个非零字节就出结果）；
#:   - `waveform`：量长度那一步（`_plan_for_source()`）解好的那份波形上切片判 —— **单趟跑
#:     的 MP3 落在这一格**；
#:   - `decode_at_plan`：量长度那一步解过、没成，门因此判不了这一段（结论必然是
#:     `silent = None`）。它说的是「解过、没成」，**从来不表示门现解了一次音频**；
#:   - `decode_now`：门**现解一次**音频文件再判。产品路径上到不了这一格（非 PCM 的音频在
#:     量长度那一步已经解过一遍了），留给直接调 `measure_span()` 的调用方。
GATE_METHOD_PCM = "pcm_bytes"
GATE_METHOD_WAVEFORM = "waveform"
GATE_METHOD_DECODE_AT_PLAN = "decode_at_plan"
GATE_METHOD_DECODE_NOW = "decode_now"

#: 分块时临时 wav 住在场次目录下的这个子目录里，跑完一块不留
CHUNK_DIR_NAME = "_offline_chunks"

#: 会后纪要那一层的状态文件名（`meeting/minutes/status.py` 的 `STATUS_NAME` 同值）。
#: 这里只留一个字符串常量、**不 import 那个包**：离线这条链不该因为多一个钩子就把
#: `meeting.minutes` 整包拉进自己的 import 图（那一包的模块归钩子自己去 import）。
MINUTES_STATUS_NAME = "meeting_minutes_status.json"


def chunk_label(index: int, label: str) -> str:
    """把第 `index` 块（从 1 起）的引擎标签加上块内命名空间（`S01` → `c01:S01`）。

    这是「块内编号绝不跨块合并」在数据形状上的落点：同一个 `S01` 在第 2 块里变成
    `c02:S01`，与第 1 块的 `c01:S01` **不相等**，于是两个块里的编号在
    `number_speakers()` 眼里天然是两个键，永远拿不到同一个 `local`。
    """
    return CHUNK_LABEL_TEMPLATE.format(index=int(index), label=str(label))


@dataclass(slots=True)
class ChunkStep:
    """一块音频在原录音时间轴上的位置（`offset` + `seconds` 就是它覆盖的那一段）。"""

    index: int
    offset: float
    seconds: float

    @property
    def span(self) -> tuple[float, float]:
        """这一块覆盖的区间（秒），落在原录音的时间轴上。"""
        return (round(float(self.offset), 3), round(float(self.offset + self.seconds), 3))


@dataclass(slots=True)
class ChunkRun:
    """一块跑完之后的如实记账（成功、跳过、失败三种都记一条）。

    这一条记账要回答的是**「这一段为什么没有字」**，读方按 `reason` 那一格分档即可，
    不必从整场的字段反推：

      | `reason` | 这一块发生了什么 | `error` |
      |---|---|---|
      | `digital_silence` | 门判出样本全零，引擎调用都没发生（`silent = True`） | `None` |
      | `engine_error` | 引擎抛错，这一段没跑成 | 有 |
      | `no_segments` | 引擎跑成了但一段都没交回 | 有 |
      | `zero_span_segments` | 引擎跑成了，交回的段**每一段都是零时长**（有字、形状可疑） | `None` |
      | `transcribed` | 引擎跑成了，交回的段有真实时长 | `None` |
      | `gate_unjudged` | 引擎跑成、交回的段都有真实时长，但**这一段的静音与否没人判过** | `None` |

    最后那一行与它的邻居分得开：门判不了那一段时，那一段照常送引擎，于是它可能跑成
    （`gate_unjudged`）、可能交出 0 段（`no_segments`）、也可能抛错（`engine_error`）——
    三种情形下 `gate` 那一格都是 `unjudged`，`reason` 说的是**引擎那一边的结果**。
    两个结论各说一件事，读方按 `gate` 看「门判过没有」、按 `reason` 看「这段为什么没有字」。
    """

    index: int
    offset: float
    seconds: float
    segments: int = 0
    wall_seconds: float = 0.0
    error: str | None = None
    #: 这一块是数字静音、跳过了引擎调用（见模块头「数字静音门」）
    silent: bool = False
    #: 门在这一块上的结论：`silent` / `not_silent` / `unjudged`
    gate: str = GATE_NOT_SILENT
    #: 门凭什么判的（`pcm_bytes` / `waveform` / `decode_at_plan` / `decode_now`；判不了的那一档为空）
    gate_method: str | None = None
    #: 门判不了时的那一句话（`gate == "unjudged"` 时才有）
    gate_detail: str | None = None
    #: 这一块交回的段里 `end <= start` 的条数（零时长段，见模块头；0 = 没有）
    zero_span_segments: int = 0

    @property
    def span(self) -> tuple[float, float]:
        return (round(float(self.offset), 3), round(float(self.offset + self.seconds), 3))

    @property
    def reason(self) -> str:
        """这一块的**机器可读结论词**（表见类说明）。

        「没声跳过」「这一块没跑成」「引擎跑成了却没交回段」「跑成了但段全是零时长」这四档
        各有一个词；`gate_unjudged` 是叠在上面的一层：门判不了那一段时，它在上面四档之外
        再多说一句「这一段的静音与否没人判过」。
        """
        if self.silent:
            return REASON_DIGITAL_SILENCE
        if self.error is not None:
            return str(self.error).split("：", 1)[0]
        if self.segments == 0:
            return REASON_NO_SEGMENTS
        if self.gate == GATE_UNJUDGED:
            return REASON_GATE_UNJUDGED
        if self.zero_span_segments:
            return REASON_ZERO_SPAN_SEGMENTS
        return REASON_TRANSCRIBED

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset_seconds": round(float(self.offset), 3),
            "seconds": round(float(self.seconds), 3),
            "span": list(self.span),
            "segments": int(self.segments),
            "wall_seconds": round(float(self.wall_seconds), 3),
            "error": self.error,
            "silent": bool(self.silent),
            "reason": self.reason,
            "gate": self.gate,
            "gate_method": self.gate_method,
            "gate_detail": self.gate_detail,
            "zero_span_segments": int(self.zero_span_segments),
        }


@dataclass(slots=True)
class ChunkPlan:
    """一路音频的切法（纯描述，不碰盘、不调引擎）。

    `chunks` 只有一条时就是单趟：引擎拿原始音频路径、标签不带命名空间。
    """

    total_seconds: float
    chunk_seconds: float
    chunks: list[ChunkStep]
    namespace: bool

    @property
    def single_pass(self) -> bool:
        return len(self.chunks) <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_seconds": round(float(self.chunk_seconds), 3),
            "namespace": bool(self.namespace),
            "chunk_count": len(self.chunks),
            "chunks": [
                {
                    "index": step.index,
                    "offset_seconds": round(float(step.offset), 3),
                    "seconds": round(float(step.seconds), 3),
                }
                for step in self.chunks
            ],
        }


def read_chunk_seconds(config: Any = None) -> float:
    """`post.chunk_seconds` 的生效值（秒）；拿不到配置时退回 `DEFAULT_CHUNK_SECONDS`。

    负数与认不出的值一律退回默认档（本模块**不抛**：配置层的取值范围由
    `config.validate_value()` 把关，这里只兜「调用方塞了一份残缺的配置对象」）。

    取值口径：`0` = 显式关掉分块（整段一次跑）；`> 0` = 每块最多这么长。
    """
    getter = getattr(config, "get", None)
    if callable(getter):
        raw = getter("post.chunk_seconds")
    else:
        try:
            raw = config_module.get("post.chunk_seconds")
        except Exception:  # pragma: no cover - 配置层缺席时退回默认档
            raw = None
    if raw is None:
        try:
            raw = config_module.default_for("post.chunk_seconds")
        except Exception:  # pragma: no cover - 键表里没有这一格时退回本模块的默认档
            raw = DEFAULT_CHUNK_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_SECONDS
    if value < 0:
        return DEFAULT_CHUNK_SECONDS
    return value


def plan_chunks(total_seconds: float, chunk_seconds: float) -> ChunkPlan:
    """把「总时长 + 块长」算成切法：**连续、不重叠、从头开始**。

    块长 `<= 0` 或总时长不超过块长时交回单块（覆盖整段），于是单趟与分块两种情况共用
    同一条主循环，产物形状只在「标签带不带命名空间」这一处分叉。

    切点是从头开始的固定间隔（`0`、`chunk_seconds`、`2 × chunk_seconds`…），
    `phase3` 报告 10.2 记的另一条路（在切点附近找静默中点）**本轮没有实现、没有测**，
    因此这里也不假装做了。
    """
    total = max(0.0, float(total_seconds))
    step_seconds = float(chunk_seconds)
    if step_seconds <= 0 or total <= step_seconds:
        return ChunkPlan(
            total_seconds=total,
            chunk_seconds=step_seconds,
            chunks=[ChunkStep(index=1, offset=0.0, seconds=round(total, 3))],
            namespace=False,
        )
    steps: list[ChunkStep] = []
    offset = 0.0
    index = 1
    while offset < total:
        length = min(step_seconds, total - offset)
        steps.append(
            ChunkStep(index=index, offset=round(offset, 3), seconds=round(length, 3))
        )
        offset += step_seconds
        index += 1
    return ChunkPlan(
        total_seconds=total,
        chunk_seconds=step_seconds,
        chunks=steps,
        namespace=True,
    )


def write_chunk_wav(path: Path | str, samples: "np.ndarray") -> Path:
    """把一段 16k 单声道波形写成 wav（引擎的 `decode_audio()` 吃它）。

    ⚠ **float32 波形必须先乘 32767 再转 int16**：`decode_audio()` 交回的是 `[-1, 1]` 的
    float32，直接 `.astype("<i2")` 会把每个样本截断成 0，写出一份**数字静音**的 wav。
    `phase3` 报告第十二节第 8 条记的就是这个坑：模型对静音照样吐 `[0.5][S01]…`，
    一整块只出一类，看起来像聚类失效。

    自检放在用例里，**不放在这里**：真正安静的块（会议里十几分钟没人说话）写出来本来就是
    零，靠「全零」拦错误会把安静的会议误判成故障。
    `tests/test_offline_runner.py::test_write_chunk_wav_keeps_the_signal` 直接盯着这条
    换算（喂一段有信号的波形，断言写出来的 wav 里还有信号）。

    临时文件住在 `<场次目录>/_offline_chunks/` 下面（`_execute_chunks()` 开跑前先清干净，
    跑完一块都不留）。`wave` / `numpy` 在函数体内 import（本模块的惰性纪律）。
    """
    import wave

    import numpy as np

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    wave_data = np.asarray(samples, dtype=np.float32)
    pcm = (np.clip(wave_data, -1.0, 1.0).astype(np.float32) * np.float32(32767.0)).astype("<i2")
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(PCM_SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return target


def clear_chunk_dir(session_dir: Path | str) -> None:
    """清掉分块用的临时目录（上一次崩在半路留下的块也一并扫掉）。"""
    target = Path(session_dir) / CHUNK_DIR_NAME
    if not target.exists():
        return
    for item in target.iterdir():
        try:
            if item.is_file():
                item.unlink()
        except OSError:  # pragma: no cover - 清不掉不挡住这一趟，交给系统清理临时目录
            _log.warning("分块临时文件清不掉：%s", item)
    try:
        target.rmdir()
    except OSError:  # pragma: no cover - 目录里还有别的东西时留着，不影响结果
        pass


def chunk_file(session_dir: Path | str, index: int) -> Path:
    """第 `index` 块的临时 wav 路径。"""
    return Path(session_dir) / CHUNK_DIR_NAME / f"chunk_{int(index):02d}.wav"


def _decode_samples(path: str | Path) -> "np.ndarray":
    """解出整段 16k 单声道 float32 波形（**用例的接缝**：分块那条路只在非 PCM 音频上走它）。

    单列一个函数是为了让用例把真解码换掉（`tests/conftest.py` 不许碰真模型，也不让
    一条用例为了一次解码等上几秒）。
    """
    from meeting.offline.audio import decode_audio

    return decode_audio(path)


def _pcm_sample_count(path: Path) -> int:
    """归档 PCM 的样本数（字节数 ÷ 2，不读内容）。"""
    return int(os.path.getsize(path) // PCM_SAMPLE_WIDTH)


# ---------------------------------------------------------------------------
# 数字静音门（调引擎之前的那一道闸，判据与留痕见模块头）
# ---------------------------------------------------------------------------


def samples_to_counts(samples: Any) -> Any:
    """归一化波形 → **int16 计数**（一个样本在 16 bit 满量程上占几格）。

    这一层的音频有两种形态，判据必须用同一把尺子量：

      - 归档 PCM 上读出来的**已经是 int16 计数**（`_pcm_chunk_counts()`）；
      - `decode_audio()` 解出来的是 `[-1, 1]` 的 float32（`write_chunk_wav()` 的入参同形）。

    换算与 `write_chunk_wav()` **同一条**：`float × 32767` 后截断成整数。
    实测那个坑正是这里：float32 的整段波形直接拿去和「计数」比，`int()` 会把
    每一个 `[-1, 1]` 的小数都截成 0 —— 一串真有声的音频于是看着像全零
    （`250906` 的复跑第一版就是这样把 19 块全判成静音的，用例
    `test_a_float_waveform_with_a_real_signal_never_looks_silent` 盯着这一条）。
    """
    import numpy as np

    data = np.asarray(samples)
    if data.dtype.kind == "f":
        # `32767`:与 `write_chunk_wav()` 同一档（写出去的 wav 就是这么换算的）
        return (np.clip(data, -1.0, 1.0) * np.float32(32767.0)).astype(np.int32)
    if data.dtype.kind in "iu":
        # `int32` 上取绝对值：`abs(int16 的 -32768)` 会溢出成负数，
        # 于是「峰值 <= 0」对满量程的负样本也成立（**更**保守的一侧）。
        return data.astype(np.int32)
    raise TypeError(f"波形的 dtype 该是 float 或 int，收到 {data.dtype}")


def pcm_sample_chunk_counts(path: Path | str, *, offset: float, seconds: float) -> Any:
    """归档 PCM 上的一段读成 int16 计数（`_materialize_chunk()` 用的同一个换算）。

    读不出来时交回空数组（调用方各自决定怎么办：切块那一步照抛、门那一步放行）。
    """
    import numpy as np

    start = int(round(float(offset) * PCM_SAMPLE_RATE))
    end = int(round((float(offset) + float(seconds)) * PCM_SAMPLE_RATE))
    with open(path, "rb") as handle:
        handle.seek(max(0, start) * PCM_SAMPLE_WIDTH)
        raw = handle.read(max(0, end - start) * PCM_SAMPLE_WIDTH)
    return np.frombuffer(raw, dtype="<i2").astype(np.int32)


def _pcm_span_scan(path: Path | str, *, offset: float, seconds: float) -> bool | None:
    """按字节块扫归档 PCM 的一段：读到非零字节交回 `False`，读满全零交回 `True`。

    要的那一段**没读全**（文件比它短）时交回 `None`（这一段的静音与否没有依据），
    读不动（文件没了、读错）同样交回 `None`：门在「确实读到全零」以外一律不下结论。

    成本落在这里：见到第一个非零字节就返回，一段有声的音频几乎在第一个字节块里就出结果；
    只有整段全零（实测那一档）才需要读满。
    """
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    begin = start * PCM_SAMPLE_WIDTH
    remaining = count * PCM_SAMPLE_WIDTH
    try:
        with open(path, "rb") as handle:
            handle.seek(begin)
            while remaining > 0:
                block = handle.read(min(_PCM_SCAN_BLOCK_BYTES, remaining))
                if not block:
                    break
                if block.translate(_PCM_SCAN_TABLE).count(1):
                    return False
                remaining -= len(block)
    except OSError:  # 读不到（文件没了、读错）就按「可能有声」放行，引擎那一层照样报错
        _log.warning("会后离线：数字静音门读不动 %s，照常送引擎", path, exc_info=True)
        return None
    if remaining > 0:
        return None
    return True


def samples_to_count_floats(samples: Any) -> Any:
    """归一化波形 → **满量程的格数**（浮点那把尺子，**不截断**成整数）。

    这一步与 `samples_to_counts()` 的差别只有一处：这里保留小数。判据需要的是「这一段里
    有没有一个非零样本」，而 int16 那一边的「一个格」在 float 这一边等于 `1/32767` ——
    截断会把 `3.0e-5` 与 `3.05e-5`（半格上下）一起抹成 0，于是同一条「峰值恰好为 0」的
    规则在两种形态上给出两种答案。保留小数之后，两侧的分界都落在「满量程半格」上。

    `samples_to_counts()` 仍是**切块与判据共用的那把整数尺子**（`write_chunk_wav()` 的
    换算同形），这里单列一把浮点尺子只为判据服务，切块的字节形状一个字不动。
    """
    import numpy as np

    data = np.asarray(samples)
    if data.dtype.kind == "f":
        # `32767`:与 `write_chunk_wav()` 同一档（写出去的 wav 就是这么换算的）
        return np.clip(data, -1.0, 1.0) * np.float32(32767.0)
    if data.dtype.kind in "iu":
        # `int32` 上取绝对值：`abs(int16 的 -32768)` 会溢出成负数，于是满量程的负样本
        # 会被读成 0（**更**保守的一侧）。这里先升位再交给 `_peak_counts()`。
        return data.astype(np.int32)
    raise TypeError(f"波形的 dtype 该是 float 或 int，收到 {data.dtype}")


def _peak_counts(scaled: Any) -> float:
    """一串**满量程格数**（浮点或整数）的峰值（空数组记 0）。"""
    import numpy as np

    if scaled.size == 0:
        return 0.0
    # 先取绝对值、再降成 `int64` 取最大：`int16` 的 `-32768` 因此不会被 `abs` 溢出吃掉
    magnitudes = np.abs(scaled)
    if magnitudes.dtype.kind == "f":
        return float(np.max(magnitudes))
    return float(np.max(magnitudes.astype(np.int64)))


def is_digital_silence(samples: Any) -> bool:
    """这一段波形是不是**数字静音**（样本全为零，且长度够得上判）。

    两条判据，缺一不可：

      1. 样本数 `>= MIN_GATE_SAMPLES`（0.1 秒）：**短于这一档的一律不算静音**，
         交给引擎去听 —— 样本不足时「全零」这件事说明不了什么；
      2. 峰值**低于** `SILENT_PEAK_FLOOR`（**半格**）：一个够得着半格的样本都没有。
         **半格是开区间**：正好落在半格上的样本算有声（文档那条「峰值低于半个 int16
         步长算静音」与这里逐字一致）。

    入参两种形态都收（int16 计数，或 `[-1, 1]` 的 float32），两侧用**同一把尺子、同一个
    分界**：峰值都按 `samples_to_count_floats()` 换算成满量程格数，且**都不截断**。整数那
    一侧「半格」落在 0 与 1 之间（整数里没有比 1 更小的非零值），于是 int16 的 ±1 算有声；浮
    点那一侧 `1/32767` 正好是一格。两侧因此对同一个样本给同一个答案 —— 判据的名字仍是
    「全零」，尺子的读数换成格数是为了让小数形态也能落在同一条线上。

    **文档那条规则与这一行代码逐字对得上**：`SILENT_PEAK_FLOOR = 0.5` 是「半个 int16 步长」，
    判据是 `peak < SILENT_PEAK_FLOOR`（**开区间**），也就是**峰值低于半个 int16 步长算静音**。
    正好落在半格上的样本（float 的 `0.5 / 32767 ≈ 1.526e-5`）算**有声**：它在 int16 上没有
    对应物，规则说的是「低于」而不是「不高于」。用例
    `test_the_gate_boundary_is_the_documented_half_step_rule` 把这一点按常量钉住。

    空波形（0 个样本）算静音：那一段没有音频，引擎没有可吃的东西。**「这一段读不全」
    自有另一条路管**（`measure_span()` 那一层，见模块头）—— 截断的音频在那里放行，
    不会走到这里被当成空。

    与 `meeting/asr/glm.py` 的 `_is_noise_segment` 的关系写在模块头：那一条服务于会中
    逐段预判，三条 AND 里带 VAD；这一条服务于会后权威底本，只认「一个够得着半格的样本
    都没有」。
    """
    scaled = samples_to_count_floats(samples)
    if scaled.size == 0:
        return True
    if int(scaled.size) < MIN_GATE_SAMPLES:
        return False
    return _peak_counts(scaled) < float(SILENT_PEAK_FLOOR)


def pcm_span_is_silent(path: Path | str, *, offset: float, seconds: float) -> bool:
    """归档 PCM 上 `[offset, offset + seconds)` 这一段是不是数字静音（**不解析成波形**）。

    按字节块扫（`_PCM_SCAN_BLOCK_BYTES`），见到任何一个非零字节就当场交回 `False`：
    有声的音频几乎在第一块里就出结果，静音那一档才读满整段。长度按样本数判
    （`MIN_GATE_SAMPLES` 那一条，与 `is_digital_silence()` 同一条口径）。

    **只有「整整这一段都读到了、且一个非零字节都没有」才判静音**：文件比 `[offset,
    offset + seconds)` 短（采集侧少写了尾巴、切法把索引算到文件之外）时交回 `False`，
    交给引擎去听它到底有多少样本。少了这一条，「读不到」与「读到全零」会撞成同一个答案，
    一段没读到的音频就被当成静音跳过了。

    读不出来（文件没了、读错）同样交回 `False`（**照常送引擎**）：门只在「确实读到全零」
    时拦人，读不到就按「可能有声」放行。

    这一档是「判不了就放行」的**简化版**（只有是与否两种交回值），要连「判没判得了」一起
    带出来请走 `measure_span()`。
    """
    return measure_span(path, offset=offset, seconds=seconds).silent is True


@dataclass(slots=True)
class MeasureResult:
    """门对**一段音频**的结论：判没判得了（`silent` 是不是 `None`）、凭什么判的、为什么。

    `silent = True` / `False` 是门判过之后的两种答案；`silent = None` 说的是**门判不了**
    这一段（音频解不开、读不全），那一段照常送引擎，由引擎说了算。第三种形态必须存在：
    布尔值说不出「我没判」这件事，而一次没门可用的运行正是靠它才在产物里露得出来。
    """

    method: str
    silent: bool | None
    detail: str | None = None

    @property
    def judged(self) -> bool:
        """门有没有就这一段给出结论。"""
        return self.silent is not None


def _span_samples(waveform: Any, *, offset: float, seconds: float) -> Any | None:
    """已经解好的波形上取 `[offset, offset + seconds)` 那一段；取不全时交回 `None`。

    「取不全」按**这一段没读全**判：文件比要的那一段短（采集侧少写了尾巴、切法把索引算到
    文件之外）时，那一段的静音与否没有依据 —— 与 `pcm_span_is_silent()` 的判据同源，
    两条路因此给出同一个答案（见模块头 m1 那一节）。
    """
    import numpy as np

    data = np.asarray(waveform)
    if data.size == 0:
        # 一点波形都没有：那一段没有音频可判（空波形本身算静音，见 `is_digital_silence()`）
        return data
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    if start + count > int(data.size):
        return None
    return data[start:start + count]


def measure_span(
    path: str | Path,
    *,
    offset: float,
    seconds: float,
    waveform: Any = None,
    waveform_length: int | None = None,
    decode_attempted: bool = False,
) -> MeasureResult:
    """门在**一段音频**上的结论：先按归档 PCM 的字节扫，再用量好的波形，最后现解一次。

    三条路按成本从低到高排（前一条用得上就不走后面）：

      1. **归档 PCM**（`meeting_audio_<source>.pcm`）按字节扫那一段：见到第一个非零字节就
         出结果，一场静音会议才需要读满，成本可以忽略。**它排在波形前面**：单趟跑的 MP3
         是转码产物，归档 PCM 与它同一段音频而更便宜。
      2. **已经解好的波形**（`_plan_for_source()` 量长度时解出来的那一份，分块跑与单趟跑
         共用同一份）切片判 —— **单趟跑的 MP3 走的是这一条**，门口不再现解第二遍。
      3. **现解一次音频文件**：`decode_audio()`（`av`）解成 16k 单声道波形再判。这一支在产品
         路径上到不了：非 PCM 的音频要么在量长度那一步就解成了波形（走 2），要么在那里就
         没解开（走 `decode_attempted` 那一支）。留着它是为了「调用方自己给了一段波形之外的
         音频、又没量过长度」那一档，以及直接调本函数的用例。

     `waveform_length` 是**已经解好的那段波形的总样本数**（不给就按 `len()` 算）：有了它，
    「这一段没读全」与「读到全零」才分得开。`decode_attempted` 说明调用方**已经为这一段
    解过一次**（`_plan_for_source()` 量长度那一步）：那一次没成时这里不再解第二遍，
    直接照「门判不了」记账（`GATE_METHOD_DECODE_AT_PLAN`）—— 同一个坏文件解两次只会把
    同一句话打两遍日志。
    """
    audio_path = Path(path)
    if audio_path.suffix.lower() == ".pcm":
        count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
        if count < MIN_GATE_SAMPLES:
            # 短于 0.1 秒的那一档不判静音（样本不足，交给引擎）
            return MeasureResult(
                method=GATE_METHOD_PCM,
                silent=False,
                detail="这一段短于 0.1 秒，样本不足时不替引擎下结论",
            )
        verdict = _pcm_span_scan(audio_path, offset=offset, seconds=seconds)
        if verdict is None:
            return MeasureResult(
                method=GATE_METHOD_PCM,
                silent=None,
                detail=(
                    f"归档 PCM 上这一段没读全（要 {count} 个样本，"
                    f"{audio_path.name} 比这个区间短或者读不动）"
                ),
            )
        return MeasureResult(method=GATE_METHOD_PCM, silent=verdict)

    if waveform_length is not None:
        piece = _span_samples(waveform, offset=offset, seconds=seconds)
        if piece is None:
            return MeasureResult(
                method=GATE_METHOD_WAVEFORM,
                silent=None,
                detail=(
                    f"解好的波形只有 {int(waveform_length)} 个样本，"
                    "盖不住这一段的区间"
                ),
            )
        return MeasureResult(
            method=GATE_METHOD_WAVEFORM, silent=is_digital_silence(piece)
        )

    if decode_attempted:
        # 量长度那一步已经解过一次、没解出来：同一句话不再说第二遍，直接照「判不了」记
        return MeasureResult(
            method=GATE_METHOD_DECODE_AT_PLAN,
            silent=None,
            detail="这个文件解不出波形（量长度那一步已经试过一次）",
        )

    try:
        decoded = _decode_samples(audio_path)
    except Exception as exc:  # 解不开：门判不了，那一段照常送引擎
        return MeasureResult(
            method=GATE_METHOD_DECODE_NOW,
            silent=None,
            detail=f"{type(exc).__name__}: {exc}",
        )
    start = max(0, int(round(float(offset) * PCM_SAMPLE_RATE)))
    count = max(0, int(round(float(seconds) * PCM_SAMPLE_RATE)))
    if int(decoded.size) and start + count > int(decoded.size):
        return MeasureResult(
            method=GATE_METHOD_DECODE_NOW,
            silent=None,
            detail=(
                f"解出来只有 {int(decoded.size)} 个样本，盖不住这一段的区间"
                f"（{start} + {count}）"
            ),
        )
    return MeasureResult(
        method=GATE_METHOD_DECODE_NOW,
        silent=is_digital_silence(decoded[start:start + count]),
    )


def span_is_silent(
    path: str | Path, *, offset: float, seconds: float, waveform: Any = None
) -> bool:
    """一段音频（原录音时间轴上的 `[offset, offset + seconds)`）是不是数字静音。

    交回值只有「是 / 否」两种，**「门判不了」那一档在这里与「放行」撞成同一个 `False`**
    —— 判断要不要跳过引擎，这个布尔值够用（判不了就送引擎）；要把「判没判得了」也带出来
    （产物里的留痕要这件事），请用 `measure_span()`。判据与三条路见 `measure_span()`。
    """
    return measure_span(
        path, offset=offset, seconds=seconds, waveform=waveform
    ).silent is True


# ---------------------------------------------------------------------------
# 场次元信息
# ---------------------------------------------------------------------------


def session_id_of(session_dir: Path | str) -> str:
    """场次编号 = 场次目录名（`250919` 这种；目录名是服务侧生成的，本模块不另起规则）。"""
    return Path(session_dir).name


def declared_sources(session_dir: Path | str) -> tuple[str, ...]:
    """这一场该跑哪几路：优先读 `session.json` 的 `sources`，读不到就按两路全集。

    `session.json` 的 `sources` 每项带一个 `id`（两路各自一条），本模块按顺序取它。
    """
    raw = _read_session_json(session_dir)
    declared = raw.get("sources")
    if isinstance(declared, list):
        names = [
            str(item.get("id"))
            for item in declared
            if isinstance(item, dict) and item.get("id")
        ]
        if names:
            return tuple(names)
    return SOURCE_IDS


def _read_session_json(session_dir: Path | str) -> dict[str, Any]:
    """读场次目录里的 `session.json`（缺失或读不出来时交回空映射，由调用方决定怎么办）。"""
    import json

    path = Path(session_dir) / "session.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _log.warning("场次元信息读不出来：%s（按默认口径继续）", path)
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# 逐路取音频
# ---------------------------------------------------------------------------


def audio_path_for(session_dir: Path | str, source: str) -> Path:
    """一路音频的首选路径（归档的裸 PCM；`meeting/session.py` 的 `archive_name()` 同形）。"""
    return Path(session_dir) / f"meeting_audio_{source}.pcm"


def resolve_audio(
    session_dir: Path | str, source: str
) -> tuple[str | None, str | None]:
    """挑一路的音频：交回 `(路径, 说明)`；挑不出来时 `路径` 为 `None`、`说明` 里是原因。

    判据按顺序走：

      - 归档 PCM 字节数 `>= 2`（装得下至少一个样本）→ 用它；
      - PCM 缺席或 0 字节，但同名 `.mp3` 在 → 用 MP3（收尾转码的产物），说明里记一笔；
      - 两档都挑不出来 → `(None, "no_audio…")`。
    """
    pcm = audio_path_for(session_dir, source)
    if pcm.exists():
        size = pcm.stat().st_size
        if size >= EMPTY_PCM_BYTES:
            return str(pcm), None
        mp3 = pcm.with_suffix(".mp3")
        if mp3.exists() and mp3.stat().st_size > 0:
            return str(mp3), f"归档 PCM 是 {size} 字节，改用 MP3"
        return None, f"{REASON_EMPTY_PCM}：归档 PCM 是 {size} 字节、同目录没有可用的 MP3"
    mp3 = pcm.with_suffix(".mp3")
    if mp3.exists() and mp3.stat().st_size > 0:
        return str(mp3), "归档 PCM 缺席，改用 MP3"
    return None, f"{REASON_NO_AUDIO}：{pcm.name} 与 {mp3.name} 都不在场"


# ---------------------------------------------------------------------------
# 引擎解析与构造
# ---------------------------------------------------------------------------


def resolve_engine(selector: str | None = None) -> tuple[Any, str]:
    """按 `post.engine` 解析引擎类，交回 `(类, 说明)`；解析不出来即抛。

    取值口径与 `capture.backend` / `transcriber.backend` / `post.hook` 那三个键同形
    （`config.BACKEND_SELECTORS`）：

      - `auto`（缺省）→ `meeting.offline.engine:MossOfflineEngine`；
      - `null` → 抛：会后离线这一层没有「替身引擎」，显式关掉它的写法是 `post.hook = null`；
      - `module:Class` → 按它导入。

    **与会后钩子分开解析**：`post.engine` 换掉时 `post.hook` 那一层一个字不动
    （spec 的「换引擎时这一层不动」）。
    """
    if selector is None:
        raw = config_module.get("post.engine")
        selector = raw.strip() if isinstance(raw, str) and raw.strip() else "auto"
    selector = str(selector).strip()
    if selector == "null":
        raise LookupError(
            "post.engine = null：会后离线这一层没有替身引擎（关掉整条链请把 post.hook 取 null）"
        )
    target = DEFAULT_ENGINE_TARGET if selector == "auto" else selector
    if ":" not in target:
        raise LookupError(f"取值不是 module:Class 形状：{target!r}")
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise LookupError(f"{module_name} 里没有 {attr}")
    return getattr(module, attr), f"post.engine = {selector} → {target}"


def _engine_options(config: Any = None) -> dict[str, str]:
    """模型 / 档位 / 引擎选择三项：优先读传进来的那份配置，没有就读 `config.get()` 的生效值。"""
    getter = getattr(config, "get", None)

    def value(key: str) -> Any:
        if callable(getter):
            return getter(key)
        return config_module.get(key)

    model = value("post.model")
    dtype = value("post.dtype")
    selector = value("post.engine")
    return {
        "model": str(model) if model else config_module.default_for("post.model"),
        "dtype": str(dtype) if dtype else config_module.default_for("post.dtype"),
        "selector": str(selector).strip() if selector else "auto",
    }


def build_engine(config: Any = None) -> Any:
    """造一台引擎（入参可换实现：换引擎时这一层下面的一切都不动）。

    引擎实例上留着自己的 `device`（`MossOfflineEngine` 构造时就把它解析成 `cuda:0` /
    `cpu`）；`run_session` 造完引擎再从它身上读回这一格写进产物。
    """
    options = _engine_options(config)
    cls, detail = resolve_engine(options["selector"])
    _log.info("会后离线引擎：%s（%s，dtype=%s）", detail, options["model"], options["dtype"])
    return cls(model=options["model"], dtype=options["dtype"])


def resolve_device_of(engine: Any) -> str:
    """一台已经造好的引擎**实际落在**哪个设备上（读它自己解析出来的那一个）。

    `engine.device` 是引擎构造时定的（`MossOfflineEngine.__init__` 里 `_resolve_device("auto")`
    已经把 `auto` 落成 `cuda:0` 或 `cpu`），产物里要记的是这个落点，与配置里那个选择器
    字符串不是一回事。引擎对象没有 `device` 这一格时交回 `None`（调用方如实退让）。
    """
    device = getattr(engine, "device", None)
    if device is None:
        return None
    text = str(device).strip()
    return text or None


def engine_descriptor(config: Any = None, engine: Any = None) -> dict[str, Any]:
    """写进产物 `engine` 那一格的四项（`selector` 也留着：换引擎时可追溯）。

    `device` 记**引擎实际解析出来的设备**（`cuda:0` / `cpu`），来源是已造好的那台引擎
    自己（`engine` 入参）；拿不到引擎时退回配置里的选择器字符串（`auto`）。产物要能让
    人看出这一趟真跑在什么设备上，选择器字符串做不到这件事。
    """
    options = _engine_options(config)
    return {
        "model": options["model"],
        "dtype": options["dtype"],
        "device": resolve_device_of(engine) or "auto",
        "selector": options["selector"],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_session(
    session_dir: str | Path,
    *,
    engine: OfflineEngine | None = None,
    config: Any = None,
) -> dict:
    """对一场会跑完会后离线转写，落三份产物 + 状态文件，返回摘要 dict。

    `engine` 入参可注入（用例靠它塞假引擎）；不给就按 `post.engine` / `post.model` /
    `post.dtype` 现造一台。`config` 是可选的那一份配置对象（`meeting.config.Config`），
    缺省读进程当前生效值。

    **一路的失败不带走整场**：那一路的失败原因落进报告与状态文件，另一路照常跑。
    全部路都失败时整场落 `failed`，否则落 `done`。

    **没声跳过的那几段不影响终态**：`state` 照样 `done`、`partial` 照样 `false`，
    跳过的区间进摘要与状态文件的 `silent` / `silent_spans`（见模块头「数字静音门」）。

    **尾巴上还会通知一次会后纪要那一层**（`minutes.hook`，`41465462` phase2 的挂点）：
    三份产物与状态文件的终态都落完之后调一次，晚了不行（纪要的输入就是这条离线转写）、
    早了也不行（状态文件的终态还没写）。这一层炸了只记 log：离线那三份产物与
    `meeting_offline_status.json` 一个字节都不动（见 `_announce_minutes()`）。
    """
    directory = Path(session_dir) if session_dir else None
    if directory is None or not str(directory):
        raise ValueError("run_session 需要场次目录（收到空值）")
    directory.mkdir(parents=True, exist_ok=True)

    session_id = session_id_of(directory)
    started_at = _now_iso()
    sources = declared_sources(directory)
    status_path = directory / artifacts.STATUS_NAME

    engine_error: str | None = None
    if engine is None:
        try:
            engine = build_engine(config)
        except Exception as exc:  # 引擎造不出来：整场如实失败，异常不外传
            engine_error = f"{type(exc).__name__}: {exc}"
            _log.error("会后离线引擎解析失败：%s", engine_error)

    # 描述符在引擎**造好之后**才定：`device` 记的是引擎实际解析出来的那个设备
    # （见 `engine_descriptor`），不是配置里的选择器字符串。
    descriptor = engine_descriptor(config, engine)

    # 状态文件先落一版 `running`：钩子那边「返回即算启动成功」，界面上这一格当场可读。
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state="running",
        started_at=started_at,
        sources=list(sources),
        artifacts={},
    )

    begun = time.monotonic()
    metrics: list[artifacts.SourceMetrics] = []
    per_source: dict[str, dict[str, Any]] = {}
    for source in sources:
        metrics.append(
            _run_one_source(
                directory,
                source,
                engine=engine,
                engine_error=engine_error,
                session_id=session_id,
                descriptor=descriptor,
                config=config,
                per_source=per_source,
            )
        )
    total_wall = time.monotonic() - begun

    report_path = artifacts.write_report(
        directory / artifacts.REPORT_NAME,
        session_id=session_id,
        session_dir=directory,
        engine=descriptor,
        metrics=metrics,
        total_wall_seconds=total_wall,
    )

    failed = [item for item in metrics if item.state != "done"]
    state = "failed" if metrics and len(failed) == len(metrics) else "done"
    error = None
    if failed:
        error = "；".join(f"{item.source}：{item.error or item.state}" for item in failed)
    # 有一路缺了一段（跑了分块、其中某块没成）时整场仍落 `done`（产物可用、那一格是终态），
    # 但 `partial` 明确立起来、`error` 里补一句「哪一段没有转写」：
    # **一份少了 450 秒却报 `done` 的转写，下游读不出它少了东西**。
    degraded = [item for item in metrics if item.state == "done" and item.partial]
    partial = bool(degraded)
    if degraded:
        note = "；".join(
            f"{item.source}：{_missing_spans_note(item.missing_spans)}"
            f"（原因：{'/'.join(sorted(_failure_reasons(item))) or REASON_ENGINE_ERROR}，"
            "转写缺这一段）"
            for item in degraded
        )
        error = f"{error}；{note}" if error else note
    # 没声跳过的那几段**不进 `error`、也不立 `partial`**：这一档什么都没出错，
    # 它只写在自己的三格里（状态文件顶层的 `silent` / `silent_spans`、逐路 `artifacts`
    # 那一项、对照表的 `audio.silent_spans`）。把它混进 `error` 就等于说这一场坏过。
    silent_spans: list[tuple[float, float]] = [
        span for item in metrics for span in item.silent_spans
    ]
    # 门**在这一场上做成没做成判断**：逐路那一份进状态文件的 `artifacts.<source>`，
    # 整场这一份按路聚合后进顶层 `gate`。一次没门可用的运行因此与健康的运行分得开。
    gate = _aggregate_gate(metrics)
    artifacts.write_status(
        status_path,
        session_id=session_id,
        state=state,
        started_at=started_at,
        sources=list(sources),
        artifacts={
            source: {
                "transcript": row["transcript"],
                "speakers": row["speakers"],
                "report": report_path.name,
                "silent": row["silent"],
                "silent_spans": row["silent_spans"],
                "gate": row["gate"],
            }
            for source, row in per_source.items()
        },
        finished_at=_now_iso(),
        error=error,
        partial=partial,
        silent=bool(silent_spans),
        silent_spans=silent_spans,
        gate=gate,
    )

    summary = {
        "session_id": session_id,
        "session_dir": str(directory),
        "state": state,
        "partial": partial,
        "silent": bool(silent_spans),
        "silent_spans": [[round(float(a), 3), round(float(b), 3)] for a, b in silent_spans],
        "gate": gate,
        "sources": list(sources),
        "error": error,
        "total_wall_seconds": round(total_wall, 3),
        "engine": descriptor,
        "report": report_path.name,
        "status": status_path.name,
        "transcripts": per_source,
        "minutes": _announce_minutes(directory),
    }
    _log.info(
        "会后离线转写结束：%s（%s，%d 路，%.2fs）",
        session_id, state, len(sources), total_wall,
    )
    return summary


def _announce_minutes(session_dir: Path | str) -> dict[str, Any]:
    """三份离线产物与状态文件都落定之后，通知一次会后纪要那一层（`minutes.hook`）。

    **顺序不能反**：离线这条转写就是纪要那一层的输入，先出纪要就永远只有降级档
    （spec「自动跑挂在哪一步」）。调用点因此落在 `run_session()` 的**最后**：状态文件的
    终态已经写完了，纪要这一层读到的东西才是齐的。

    三条边界，一条都不许放宽：

      - 这一层炸了**不许影响离线那三份产物**，也不许改 `meeting_offline_status.json`
        —— 整段用 `try/except BaseException` 兜住，只在 logger 上记一条；
      - `minutes.hook = null`（配置里写 JSON `null`）→ 解析成缺席档 → **安静跳过**；
      - 环境里有 `MEETING_MINUTES_CHILD=1`（这一趟是 DSH 子会话自己）→ 同样安静跳过
        （再入闸还有一道在 `meeting.minutes.runner.run()` 的最前面，这里早一步省掉一次解析）。

    返回值只进 `run_session()` 的摘要（`{"hook": …, "announced": …, "status": …}`），
    **不写进任何产物**：这一格的读者是调用方与用例，界面与下游读的是
    `meeting_minutes_status.json`（它归纪要那一层自己写）。
    """
    import os as _os

    if str(_os.environ.get("MEETING_MINUTES_CHILD", "")).strip() == "1":
        return {"hook": "skipped", "announced": False, "reason": "子会话（MEETING_MINUTES_CHILD=1）"}
    try:
        from meeting.backends.registry import resolve_minutes_hook

        resolution = resolve_minutes_hook()
        if not resolution.has_implementation:
            _log.info("会后纪要未启用（minutes.hook = %s）：这一场不产生纪要", resolution.selector)
            return {
                "hook": "absent",
                "announced": False,
                "selector": resolution.selector,
                "reason": "minutes.hook 缺席（自动跑已关掉）",
            }
        minutes_status_path = Path(session_dir) / MINUTES_STATUS_NAME
        resolution.implementation.launch(str(session_dir))
        # **返回即算启动成功**（与收尾第 4 步同一条契约）：后台那条线程跑成什么样，
        # 由 `meeting_minutes_status.json` 回答。
        return {
            "hook": "launched",
            "announced": True,
            "selector": resolution.selector,
            "status": MINUTES_STATUS_NAME,
            "status_path": str(minutes_status_path),
        }
    except BaseException:  # 纪要这一层炸了绝不带走离线产物，也不改离线状态文件
        _log.exception(
            "会后纪要那一层没能拉起来（离线产物与状态文件照旧）：%s", session_dir
        )
        return {"hook": "failed", "announced": False, "reason": "纪要钩子抛出异常（详见主 log）"}


def _run_one_source(
    directory: Path,
    source: str,
    *,
    engine: Any,
    engine_error: str | None,
    session_id: str,
    descriptor: dict[str, Any],
    config: Any,
    per_source: dict[str, dict[str, Any]],
) -> artifacts.SourceMetrics:
    """跑一路：取音频 → 算切法 → 逐块调引擎 → 编号 → 落两份按路的产物 → 交回度量。

    本函数**不抛**：这一路的任何失败都记进度量（`state = "failed"` + `error`），
    让另一路照常跑完。返回时 `per_source` 里已经补上这一路的产物名。

    分块由 `post.chunk_seconds` 定（见模块头那一节）：块数为 1 时引擎拿到原始音频路径
    （单趟），块数多于一的时候逐块切、逐块调，块内编号带 `cNN:` 命名空间、时间轴加偏移。
    **有一块失败就落 `partial`**（转写照落，缺哪一段写进状态文件与报告），
    每一块都失败时这一路落 `failed`（`all_chunks_failed`），两块产物照落：转写只剩一条
    写明缺失区间的注释行、对照表是空表，终态与原因写在对照表的 `state` / `error` 上。

    **没声而跳过的块不算失败也不算缺段**（见模块头「数字静音门」）：`state` 仍落 `done`，
    `partial` 仍为 `false`，跳过的区间进 `metric.silent_spans`，逐块记账里 `silent = True`。
    整路**一块音频都没有**（全是数字静音）时也照落产物：那是一份空转写配一条说明的
    正常结果，读方从 `audio.silent_spans` 与状态文件的 `silent` 看得出它为什么是空的。
    """
    metric = artifacts.SourceMetrics(source=source, state="running")
    started = time.monotonic()
    path, note = resolve_audio(directory, source)
    segments: list[OfflineSegment] = []
    wrote_artifacts = False

    if path is None:
        metric.state = "failed"
        metric.error = note
        metric.reasons[
            REASON_NO_AUDIO if REASON_NO_AUDIO in (note or "") else REASON_EMPTY_PCM
        ] = 1
        _log.warning("会后离线：%s 这一路没有可用音频（%s）", source, note)
    elif engine is None:
        metric.state = "failed"
        metric.error = f"{REASON_ENGINE_ERROR}：引擎不可用（{engine_error}）"
        metric.reasons[REASON_ENGINE_ERROR] = 1
    else:
        if note:
            _log.info("会后离线：%s 的音频取 %s（%s）", source, path, note)
        # 引擎抛错那一档在 `_execute_chunks` 的**切法计算**里收（`plan`），
        # 与逐块的失败分开记：切法算不出来时一块都没跑过。
        try:
            metric.audio_file = Path(path).name
            plan, waveform, decode_attempted = _plan_for_source(
                directory, source, path, config=config
            )
            metric.audio_seconds = plan.total_seconds
            segments, runs = _execute_chunks(
                directory,
                source,
                path,
                engine=engine,
                plan=plan,
                waveform=waveform,
                decode_attempted=decode_attempted,
            )
        except Exception as exc:  # 切法/解码抛错：这一路失败，另一路照跑
            metric.state = "failed"
            metric.audio_file = Path(path).name
            metric.error = f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}"
            metric.reasons[REASON_ENGINE_ERROR] = 1
            _log.exception("会后离线：%s 这一路引擎抛错（如实记账，继续跑另一路）", source)
        else:
            _record_chunk_runs(metric, runs)
            metric.gate = _chunk_gate(runs)
            # 零时长段逐路数一遍（逐块那几行的数之和与它一致）：整路那一格在产物里
            # 与对照表的 `audio.zero_span_segments` 同源，见模块头「零时长的段」。
            metric.zero_span_segments = sum(run.zero_span_segments for run in runs)
            # 「跑成了」与「没声跳过」两档都留下了有用的东西（前者有分段、后者是
            # 「这里本来就没有音频」这个事实），**一块都没剩下**才落 `failed`。
            ran = any(run.error is None for run in runs)
            if not ran:
                metric.state = "failed"
                metric.error = (
                    f"{REASON_ALL_CHUNKS_FAILED}：{_chunk_error_note(runs)}"
                )
                _log.error(
                    "会后离线：%s 这一路的 %d 块一块都没跑成（%s）",
                    source, len(runs), metric.error,
                )
            # 真的切了块（块数 > 1）而每一块都没跑成时，两块产物**照落**：留下一份空表，
            # `state = "failed"` 与 `error` 明写「这一路一段都没转出来」。少了这一档，这一趟
            # 在场次目录里什么都不留，盘上那份同名文件就是**上一趟**留下的旧表——读方按文件名
            # 取它，会把上一趟的成功结果当成这一趟的（`250906` 两路跑进同一个目录时撞过这一类事）。
            #
            # 单趟（块数 1）失败照旧不落产物：那一档没有「哪几块没成」可说，失败原因在状态文件
            # 与报告里，与 phase2 立下的口径一致（不留空文件充数）。**没声那一档照落产物**：
            # 一份 0 段的转写配一条注释，与「引擎没吐东西」是两件事。
            wrote_artifacts = ran or bool(metric.silent_spans) or len(runs) > 1

    if wrote_artifacts:
        if metric.state == "running":
            # 上面那一支（每一块都没跑成）已经把终态定成 `failed`，不许在这里被盖成 `done`：
            # 「14 块一块都没成」与「这一路真的没人说话」在产物里必须分得开。
            metric.state = "done"
        _write_source_artifacts(
            directory,
            source,
            session_id=session_id,
            descriptor=descriptor,
            segments=segments,
            metric=metric,
            per_source=per_source,
        )

    metric.wall_seconds = time.monotonic() - started
    if metric.audio_seconds > 0:
        metric.rtf = metric.wall_seconds / metric.audio_seconds
    return metric


# ---------------------------------------------------------------------------
# 分块：切法、逐块跑、把结果拼回整场时间轴
# ---------------------------------------------------------------------------


def _plan_for_source(
    directory: Path, source: str, path: str, *, config: Any = None
) -> tuple[ChunkPlan, Any, bool]:
    """算这一路的切法（块长读 `post.chunk_seconds`，总时长按音频类型分别量）。

    归档 PCM 的时长由**文件字节数**算出（不读内容）：一份 5943 秒的 PCM 是 190 MB，
    为了量长度先读一遍它并不划算。别的容器解一遍取样本数（`decode_audio()` 给出的就是
    16k 单声道波形，样本数 ÷ 16000 即秒数），解码那一次也用来切块。

    **解出来的那段波形一并交回**（`(切法, 波形, 解过没有)`）：同一个文件不该为了不同的理由
    解两遍 —— 单趟跑的 MP3 要在门口看一眼峰值（C1），分块跑的 MP3 要逐块切，两处都用这一段
    波形。第三个交回值说的是「这个容器**试过**解码了没有」：试过而没成就别再试第二遍，
    直接照「门判不了」记账（同一个坏文件解两次只会把同一句话打两遍日志）。

    量长度这一步失败时**退回单趟**（时长记 0）：解不出来是引擎该报的错（它反正要解一遍），
    在这里替它下结论会让「引擎交回什么」这件事变成「我们量长度成没成」。
    """
    del directory, source  # 切法只认路径与块长，两路之间没有别的差别
    audio_path = Path(path)
    chunk_seconds = read_chunk_seconds(config)
    if audio_path.suffix.lower() == ".pcm":
        # 归档 PCM 不在这里解波形：门按字节扫那一段（比解一遍便宜），切块按字节定位读
        try:
            total = _pcm_sample_count(audio_path) / PCM_SAMPLE_RATE
        except OSError:
            _log.warning(
                "会后离线：%s 的时长量不出来（退回整段一次跑，交给引擎如实报错）",
                path, exc_info=True,
            )
            return plan_chunks(0.0, chunk_seconds), None, False
        return plan_chunks(total, chunk_seconds), None, False
    try:
        waveform = _decode_samples(audio_path)
        total = float(waveform.size) / PCM_SAMPLE_RATE
    except Exception:
        _log.warning(
            "会后离线：%s 的时长量不出来（退回整段一次跑，交给引擎如实报错）",
            path, exc_info=True,
        )
        return plan_chunks(0.0, chunk_seconds), None, True
    return plan_chunks(total, chunk_seconds), waveform, True


def _execute_chunks(
    directory: Path,
    source: str,
    path: str,
    *,
    engine: Any,
    plan: ChunkPlan,
    waveform: Any = None,
    decode_attempted: bool = False,
) -> tuple[list[OfflineSegment], list[ChunkRun]]:
    """按切法逐块调引擎，交回 `(拼回整场时间轴的分段, 逐块记账)`。

    单趟（块数为 1）时**不切块**：归档 PCM 那一条路把原始音频路径喂引擎，产物与加
    `post.chunk_seconds` 之前逐字一致；解好了波形的那一条路（mp3）喂的是解好的那份 wav ——
    那一次解码已经为过门花掉了，让引擎再解一遍原文件没有意义。**没声那一档两路都除外**：
    那一段跳过引擎，产物里留下 `silent_spans`。

    分块时：块内波形写成临时 wav（`write_chunk_wav()`）→ 调引擎 → 把每段的
    `start` / `end` 加上该块偏移、说话人标签加上块内命名空间。**块与块之间不传任何东西**
    （不比对标签、不映射编号），每一块只往 `merged` 里追加自己的结果。

    **每一块都先过数字静音门**（`measure_span()`，见模块头）：全零样本的块根本不进引擎，
    记成「跳过」而不是「失败」（引擎在数字静音上编出整段假正文是有实测记录的）。单趟与
    分块两条路在这里没有分叉 —— 归档 PCM 按字节扫、别的容器用解好的波形或现解一次，
    于是**每一条路都过门**。门判不了那一段时（音频解不开、读不全）照常送引擎，同时把
    「判不了」记进 `ChunkRun.gate` 与 `reason`：一次没门可用的运行必须在产物里看得出来。

    任何一块抛错都就地收下、记一笔，继续跑下一块：一块坏掉不该把已经跑出来的
    几十分钟音频一起废掉，但它**必须露在产物里**（见 `ChunkRun.error`）。

    跑完（或抛出来）都把临时块扫干净：分块目录是这一趟的中间产物，留着只会让下一趟
    分不清哪一份是哪一次的。产物里留下的是**逐块的记账**（`ChunkRun`），音频本身可重切。
    """
    steps = plan.chunks
    materialize = len(steps) > 1
    if materialize:
        clear_chunk_dir(directory)
    # 非 PCM 的音频在这里现解一次（分块跑与单趟跑都要）：分块跑拿它切片，单趟跑拿它过门。
    # 归档 PCM 不走这一步（字节扫与按字节切块都比解一遍便宜）。
    # **解不开不算这一路失败**：解不动是引擎该报的错，这里的门记一笔「判不了」
    # （`GATE_UNJUDGED`）之后照常把原文件送进去，让引擎给出权威的说法。
    if waveform is None and not decode_attempted and Path(path).suffix.lower() != ".pcm":
        try:
            waveform = _decode_samples(path)
        except Exception:
            _log.warning(
                "会后离线：%s 解不出波形，门在这一段上判不了（照常送引擎）",
                path, exc_info=True,
            )
    waveform_length = None if waveform is None else len(waveform)

    merged: list[OfflineSegment] = []
    runs: list[ChunkRun] = []
    try:
        for step in steps:
            try:
                target = (
                    _materialize_chunk(directory, path, step, waveform)
                    if materialize
                    else str(path)
                )
                # **单趟与分块走同一个门**（C1）：这一段是归档 PCM 就按字节扫那一段，
                # 是别的容器就用解好的波形（单趟跑的 mp3 在门口现解一次），判不了就照常送引擎。
                measured = measure_span(
                    path,
                    offset=step.offset,
                    seconds=step.seconds,
                    waveform=waveform,
                    waveform_length=waveform_length,
                    decode_attempted=decode_attempted,
                )
                if measured.judged is False:
                    # 「判不了」也要在产物里露出来：这一段照常送引擎，但读方能看出
                    # 这一趟的门没在这段上做成判断（一次没门可用的运行不许与健康运行同形）。
                    # 已经解好了波形时（mp3 单趟 / 分块）就喂解好的那份 wav：那一次解码
                    # 已经花掉了，让引擎再解一遍原文件没有意义；解不出来时照旧喂原路径，
                    # 由引擎如实报错（那才是「这个文件读不了」的权威说法）。
                    if not materialize and waveform is not None:
                        target = str(write_chunk_wav(chunk_file(directory, 1), waveform))
                    _log.warning(
                        "会后离线：%s 第 %d 块（%.0fs 起）的静音与否判不了（%s），照常送引擎",
                        source, step.index, step.offset, measured.detail,
                    )
                gate_verdict = (
                    GATE_UNJUDGED if measured.silent is None
                    else (GATE_SILENT if measured.silent else GATE_NOT_SILENT)
                )
            except Exception as exc:
                # 切块/写临时文件这一步抛错：记成这一块的失败（音频没变坏，是这一步没成）
                runs.append(
                    ChunkRun(
                        index=step.index,
                        offset=step.offset,
                        seconds=step.seconds,
                        error=f"{REASON_ENGINE_ERROR}：切块失败 {type(exc).__name__}: {exc}",
                    )
                )
                _log.exception("会后离线：%s 第 %d 块切不出来", source, step.index)
                continue

            if measured.silent:
                # 这一段没声：**引擎调用一次都不发生**。跳过的区间照样进产物，
                # 但它是「跳过」那一档（`silent = True`、`error = None`），
                # 与「这一块没跑成」在每一格上都分得开。
                runs.append(
                    ChunkRun(
                        index=step.index,
                        offset=step.offset,
                        seconds=step.seconds,
                        silent=True,
                        gate=gate_verdict,
                        gate_method=measured.method,
                    )
                )
                _log.info(
                    "会后离线：%s 第 %d 块（%.0fs 起）是数字静音，跳过引擎调用",
                    source, step.index, step.offset,
                )
                continue

            begun = time.monotonic()
            try:
                produced = list(engine.transcribe(target))
            except Exception as exc:  # 这一块失败：记账，下一块照跑
                runs.append(
                    ChunkRun(
                        index=step.index,
                        offset=step.offset,
                        seconds=step.seconds,
                        wall_seconds=time.monotonic() - begun,
                        error=f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}",
                        gate=gate_verdict,
                        gate_method=measured.method,
                        gate_detail=measured.detail if measured.silent is None else None,
                    )
                )
                _log.warning(
                    "会后离线：%s 第 %d 块（%.0fs 起）引擎抛错，如实记账后继续",
                    source, step.index, step.offset,
                )
                continue

            wall = time.monotonic() - begun
            if not produced:
                runs.append(
                    ChunkRun(
                        index=step.index,
                        offset=step.offset,
                        seconds=step.seconds,
                        wall_seconds=wall,
                        error=f"{REASON_NO_SEGMENTS}：引擎交回 0 段",
                        gate=gate_verdict,
                        gate_method=measured.method,
                        gate_detail=measured.detail if measured.silent is None else None,
                    )
                )
                _log.warning(
                    "会后离线：%s 第 %d 块（%.0fs 起）一段都没交回，如实记账后继续",
                    source, step.index, step.offset,
                )
                continue

            # 零时长段**逐块**数一遍：整路那一格（`audio.zero_span_segments`）是这些数的和，
            # 而「这一块有几段是零时长」在逐块那一行上让读方看得出是**哪一块**出了这个形状。
            zero_span = sum(
                1 for segment in produced
                if float(segment.end) <= float(segment.start)
            )
            runs.append(
                ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    segments=len(produced),
                    wall_seconds=wall,
                    gate=gate_verdict,
                    gate_method=measured.method,
                    gate_detail=measured.detail if measured.silent is None else None,
                    zero_span_segments=zero_span,
                )
            )
            merged.extend(
                _shift_segment(segment, step, namespaced=plan.namespace)
                for segment in produced
            )
    finally:
        if materialize:
            clear_chunk_dir(directory)
    return merged, runs


def _materialize_chunk(
    directory: Path, path: str, step: ChunkStep, waveform: Any
) -> str:
    """把第 `step` 块的音频写成临时 wav，交回它的路径。

    PCM 走**按字节定位**那条路（`seek` 到块起点、读到块终点），因此块长 450 秒时占的内存
    是那 14 MB，与整段多长无关。别的容器用已经解好的那段波形切片。

    读完这一段可能**短于标称块长**：归档 PCM 在收尾那一刻被截断、或采集侧少写了尾巴时，
    末尾那一块就是这么短。短了就按实际读到的样本数写（不补零、不报错），于是这一块覆盖
    的秒数由实际样本数说话。
    """
    target = chunk_file(directory, step.index)
    if waveform is not None:
        import numpy as np

        start = int(round(float(step.offset) * PCM_SAMPLE_RATE))
        end = int(round((float(step.offset) + float(step.seconds)) * PCM_SAMPLE_RATE))
        piece = np.asarray(waveform)[start:end]
        return str(write_chunk_wav(target, piece))

    samples = pcm_sample_chunk_counts(
        path, offset=step.offset, seconds=step.seconds
    ).astype("<i2")
    # PCM 上读出来的**已经是 int16 计数**，换算回 `[-1, 1]` 是为了走 `write_chunk_wav()`
    # 那一条（它按 `× 32767` 换算，与 32768 的一格差在最低位上，无损可逆）。
    return str(write_chunk_wav(target, samples.astype("float32") / 32768.0))


def _shift_segment(segment: OfflineSegment, step: ChunkStep, *, namespaced: bool) -> OfflineSegment:
    """把一块里的分段搬到**原录音的时间轴**上，并按需加上块内命名空间。

    `start` / `end` 各加该块的偏移秒数（末块的 `end` 因此可能落在音频末尾之后一点点：
    引擎偶尔会把结束时间戳写到块末之外，照实平移、不夹到总时长上，读方能从 `end` 看出
    这一段的原文在块里的位置）。说话人标签加 `cNN:` 前缀，块与块之间因此不会撞名。
    """
    return OfflineSegment(
        start=round(float(segment.start) + float(step.offset), 3),
        end=round(float(segment.end) + float(step.offset), 3),
        speaker=(
            chunk_label(step.index, segment.speaker) if namespaced else str(segment.speaker)
        ),
        text=segment.text,
    )


def _record_chunk_runs(metric: artifacts.SourceMetrics, runs: Sequence[ChunkRun]) -> None:
    """把逐块的记账写进度量：失败的块进缺失区间、进失败原因分布、把这一路标成 `partial`。

    **缺失区间按「块覆盖的那一段」记**，不按引擎吐出来的那一两句记：一块 OOM 掉的时候
    没有任何段落能说明它覆盖了哪里，而块的位置是切法算出来的、始终知道。

    **没声跳过的块走另一条线**（`run.silent`）：进 `silent_spans` 与 `reasons`，
    **不进** `missing_spans`、也不立 `partial` —— 那一块什么都没出错，只是没有音频。
    两批区间因此不相交，「缺了一段」与「这一段没声」在读方那里分得开。

    **门判不了的那几块走第三条线**（`run.gate == "unjudged"`）：进 `unjudged_spans` 与
    `reasons`（`gate_unjudged`），同样不进缺失区间、不立 `partial`。这一档没有出错，
    它说的是「这一段的静音与否没人判过」，读方靠它看出这一趟的门在哪几段上没做成判断。
    """
    metric.chunks = [run.to_dict() for run in runs]
    for run in runs:
        if run.silent:
            metric.silent_spans.append(run.span)
            metric.reasons[REASON_DIGITAL_SILENCE] = (
                metric.reasons.get(REASON_DIGITAL_SILENCE, 0) + 1
            )
            continue
        if run.gate == GATE_UNJUDGED:
            metric.unjudged_spans.append(run.span)
            metric.reasons[REASON_GATE_UNJUDGED] = (
                metric.reasons.get(REASON_GATE_UNJUDGED, 0) + 1
            )
        if run.error is None:
            continue
        metric.missing_spans.append(run.span)
        reason = str(run.error).split("：", 1)[0]
        metric.reasons[reason] = metric.reasons.get(reason, 0) + 1
    metric.partial = bool(metric.missing_spans)


def _chunk_gate(runs: Sequence[ChunkRun]) -> dict[str, Any]:
    """这一路的门**做成没做成判断**（逐路那一格 `gate`，见模块头「门判不了的时候」）。

    `applied` 说的是「这一段音频的静音与否，门给出了结论吗」。它是假的两种情形都在这里
    如实写出来：门判不了那几段（`unjudged_reason` 带一句为什么），或者这一趟压根没有
    span 可判（音频缺失、引擎造不出来这些更早的失败档）。

    逐块那一行上的 `gate` 记的是**这一段**的结论（`silent` / `not_silent` / `unjudged`），
    这里记的是**整路**的：两处合起来，读方能回答「这一趟到底有没有门」。

    `detail` 那句人话按**这一趟实际发生的事**写：判过几块、其中几块是数字静音跳过了引擎
    （一块都没跳过时如实写出来，不许把「跳过」说成发生了），有块判不了时照旧写
    「判不了、照常送了引擎」，判不了与跳过同时发生时两句一并写出来（读者否则看不出这一趟
    真跳过块）。
    """
    judged = [run for run in runs if run.gate != GATE_UNJUDGED]
    unjudged = [run for run in runs if run.gate == GATE_UNJUDGED]
    skipped = [run for run in runs if run.silent]
    if not runs:
        return {
            "applied": False,
            "method": None,
            "spans_total": 0,
            "spans_judged": 0,
            "spans_unjudged": 0,
            "unjudged_spans": [],
            "unjudged_reason": None,
            "detail": "这一路没有可判的音频（更早的一步已经落了失败）",
        }
    methods = sorted({run.gate_method for run in judged if run.gate_method})
    if unjudged and skipped:
        detail = (
            f"{len(unjudged)} 块的静音与否判不了，照常送了引擎；"
            f"{len(judged)} 块判过，其中 {len(skipped)} 块是数字静音（跳过了引擎）"
        )
    elif unjudged:
        detail = f"{len(unjudged)} 块的静音与否判不了，照常送了引擎"
    elif skipped:
        detail = f"{len(judged)} 块判过，其中 {len(skipped)} 块是数字静音（跳过了引擎）"
    else:
        detail = f"{len(judged)} 块判过，没有一块是数字静音（一块都没跳过引擎）"
    return {
        "applied": not unjudged,
        # 门凭什么判的（`pcm_bytes` / `waveform` / `decode_at_plan`）：读方能看出这一段音频
        # 走的是哪一条路，与模块头那一节对得上
        "method": "/".join(methods) if methods else None,
        "spans_total": len(runs),
        "spans_judged": len(judged),
        "spans_unjudged": len(unjudged),
        "unjudged_spans": [list(run.span) for run in unjudged],
        "unjudged_reason": unjudged[0].gate_detail if unjudged else None,
        "detail": detail,
    }


def _aggregate_gate(metrics: Sequence[artifacts.SourceMetrics]) -> dict[str, Any]:
    """这一场的门**做成没做成判断**（状态文件顶层那一格 `gate`）。

    `applied` 为假时逐路写出**是哪些路的哪几段**判不了（那份逐路的 `gate` 在状态文件的
    `artifacts.<source>` 与对照表的 `audio.gate` 上）：读方一眼看得出这一趟的门没做成判断，
    不必从整场那几个字段去猜。
    """
    unjudged = {
        item.source: list(item.gate.get("unjudged_spans", []))
        for item in metrics
        if item.gate.get("spans_unjudged")
    }
    judged = sum(int(item.gate.get("spans_judged", 0)) for item in metrics)
    total = sum(int(item.gate.get("spans_total", 0)) for item in metrics)
    return {
        "applied": all(bool(item.gate.get("applied")) for item in metrics) if metrics else False,
        "spans_total": total,
        "spans_judged": judged,
        "spans_unjudged": total - judged,
        "unjudged_spans": unjudged,
    }


def _chunk_error_note(runs: Sequence[ChunkRun]) -> str:
    """「哪些块没跑成、为什么」那句话（报告与状态文件里都用它）。

    每一块后面跟它自己的原因（`engine_error：RuntimeError: 显存不够` 或 `no_segments：…`），
    于是这一路的 `error` 同时说得出**丢的是哪一段**与**为什么丢**。

    一块**失败**的都没有（全是没声跳过的）时如实写上那一句：这一档不是失败，
    只是那几块没有音频可转。
    """
    failed = [run for run in runs if run.error is not None]
    if not failed:
        return "没有一块失败（那几块是数字静音，跳过了引擎调用）"
    detail = "、".join(
        f"第 {run.index} 块（{run.span[0]:.1f}–{run.span[1]:.1f}s）没跑成：{run.error}"
        for run in failed
    )
    return f"{len(failed)}/{len(runs)} 块没跑成：{detail}"


def _missing_spans_note(spans: Sequence[Sequence[float]]) -> str:
    """把缺失区间列成一句话：`0.0–450.0s、900.0–1350.0s`。

    秒级精度落在这里，与报告、状态文件一致；转写正文照旧分钟级（`ui-design.md` 门槛 10）。
    """
    listed = "、".join(
        f"{float(item[0]):.1f}–{float(item[1]):.1f}s" for item in spans
    )
    return f"缺 {len(spans)} 段：{listed}"


def _failure_reasons(metric: artifacts.SourceMetrics) -> set[str]:
    """这一路缺区间的原因词档（从逐块记账里取，`engine_error` / `no_segments` 这些）。

    摘要与状态文件里那句话带上它，读的人一眼分得清「丢的那几块是显存不够」与
    「丢的那几块一段都没交回」。
    """
    return {
        str(run["error"]).split("：", 1)[0]
        for run in metric.chunks
        if run.get("error")
    }


def _write_source_artifacts(
    directory: Path,
    source: str,
    *,
    session_id: str,
    descriptor: dict[str, Any],
    segments: Sequence[OfflineSegment],
    metric: artifacts.SourceMetrics,
    per_source: dict[str, dict[str, Any]],
) -> None:
    """落这一路的两份按路产物（转写 + 对照表），并把文件名记进度量与摘要。

    编号规则来自 `meeting/offline/speakers.py`：**本路内按首次出现顺序**，`source`
    原样进限定名。分块模式下喂进来的分段已经带着 `cNN:` 命名空间（见 `_shift_segment`），
    于是「不同块的 `S01`」在 `number_speakers()` 眼里天然是两个键，各自拿一个编号。

    这一路缺了区间（`metric.partial`）时，转写**第一行**是一条注释行写明缺的是哪一段：
    转写是下游的权威底本，读它的人与程序都该在第一行就看到这件事
    （`artifacts.read_transcript_lines()` 认这条注释并跳过，行格式契约不受影响）。

    没声跳过的区间另起**一条**注释行（`silent_note()`）：两份注释各说一件事，
    读方跳过两者、机器从 `audio.missing_spans` 与 `audio.silent_spans` 分辨。

    **每一块都没跑成的那一路也走这里**：转写只有那一条注释行、对照表是一份空表，
    终态与失败原因写在对照表的 `state` / `error` 两格上。`speakers` 的长度因此有了
    「0 类、0 段、这一趟失败」这个明确的读法，读方不会把它当成一段正常但没人说话的结果。
    """
    spans = number_speakers(source, segments)
    summary = artifacts.speaker_rows(
        spans, raw_labels=[segment.speaker for segment in segments]
    )
    transcript_name = artifacts.TRANSCRIPT_NAME_TEMPLATE.format(source=source)
    speakers_name = artifacts.SPEAKERS_NAME_TEMPLATE.format(source=source)

    lines = [
        artifacts.format_transcript_line(span.start, span.local, span.text)
        for span in spans
    ]
    # 零时长的段是**数出来的**（`end` 不晚于 `start`）：解析层对「模型漏写了结束标签」
    # 的那一段故意把 `end` 退回 `start`，这个形状本身正常；实测的假正文每一段都是零时长，
    # 因此这一格同时是有用的异常信号。**只数不改**，正文一个字都不动。
    zero_span = sum(1 for span in spans if float(span.end) <= float(span.start))
    if metric.partial:
        lines.insert(0, artifacts.incomplete_note(metric.missing_spans))
    if metric.silent_spans:
        lines.insert(0, artifacts.silent_note(metric.silent_spans))
    artifacts.write_transcript(directory / transcript_name, lines)
    document = artifacts.build_speakers_document(
        session_id=session_id,
        source=source,
        engine=descriptor,
        audio={
            "file": metric.audio_file,
            "seconds": round(metric.audio_seconds, 3),
            "chunks": len(metric.chunks) if metric.chunks else 1,
            "missing_spans": [list(item) for item in metric.missing_spans],
            "partial": bool(metric.partial),
            # 没声而跳过的区间（每条 = 一块）与零时长段的条数：两格都分开记，
            # 读方按它们分得开「那里本来就没音频」「引擎漏写了结束标签」「这一段丢了」
            "silent": bool(metric.silent_spans),
            "silent_spans": [list(item) for item in metric.silent_spans],
            "zero_span_segments": zero_span,
            # 门**做成没做成判断**：判不了的那几段单独列出来（`applied = false` 时
            # 这一趟的产物与一次健康运行不再同形，见模块头「门判不了的时候」）
            "gate": dict(metric.gate),
        },
        segments=[
            {
                "start": span.start,
                "end": span.end,
                "local": span.local,
                "text": span.text,
            }
            for span in spans
        ],
        speakers=[
            {
                "local": row["local"],
                "label_raw": row["label_raw"],
                "segments": row["segments"],
                "seconds": row["seconds"],
            }
            for row in summary
        ],
        chunks=[
            {
                "index": run["index"],
                "offset_seconds": run["offset_seconds"],
                "seconds": run["seconds"],
                "segments": run["segments"],
                "error": run["error"],
                # 逐块那一行现在自带**结论词**（`digital_silence` / `engine_error` /
                # `no_segments` / `zero_span_segments` / `transcribed` / `gate_unjudged`）
                # 与门在这一块上的结论（`silent` / `not_silent` / `unjudged`）：
                # 「这一段为什么没有字」读这一格就知道，不必从整场那几格反推。
                "silent": bool(run["silent"]),
                "reason": run["reason"],
                "gate": run["gate"],
                "gate_method": run["gate_method"],
                "gate_detail": run["gate_detail"],
                "zero_span_segments": run["zero_span_segments"],
            }
            for run in metric.chunks
        ],
        state=metric.state,
        error=metric.error,
    )
    artifacts.write_speakers(directory / speakers_name, document)

    metric.segments = len(spans)
    metric.speakers = len(summary)
    metric.seconds = sum(float(row["seconds"]) for row in summary)
    metric.zero_span_segments = zero_span
    per_source[source] = {
        "transcript": transcript_name,
        "speakers": speakers_name,
        "segments": len(spans),
        "speaker_count": len(summary),
        "audio_seconds": round(metric.audio_seconds, 3),
        # 终态也进这一格：摘要与状态文件的 `artifacts` 里那一项因此自带 `failed`，
        # 读方不用回头再看整场的 `state` 才知道这一路是空表
        "state": metric.state,
        "partial": bool(metric.partial),
        "missing_spans": [list(item) for item in metric.missing_spans],
        "silent": bool(metric.silent_spans),
        "silent_spans": [list(item) for item in metric.silent_spans],
        "gate": dict(metric.gate),
    }


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/state.py` 的 `now_iso()` 同形，这里不 import 它）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "CHUNK_DIR_NAME",
    "CHUNK_LABEL_TEMPLATE",
    "CHUNK_SECONDS_OFF",
    "ChunkPlan",
    "ChunkRun",
    "ChunkStep",
    "DEFAULT_CHUNK_SECONDS",
    "DEFAULT_ENGINE_TARGET",
    "EMPTY_PCM_BYTES",
    "GATE_METHOD_DECODE_AT_PLAN",
    "GATE_METHOD_DECODE_NOW",
    "GATE_METHOD_PCM",
    "GATE_METHOD_WAVEFORM",
    "GATE_NOT_SILENT",
    "GATE_SILENT",
    "GATE_UNJUDGED",
    "MIN_GATE_SAMPLES",
    "MeasureResult",
    "PCM_SAMPLE_RATE",
    "PCM_SAMPLE_WIDTH",
    "REASON_ALL_CHUNKS_FAILED",
    "REASON_DIGITAL_SILENCE",
    "REASON_EMPTY_PCM",
    "REASON_ENGINE_ERROR",
    "REASON_GATE_UNJUDGED",
    "REASON_NO_AUDIO",
    "REASON_NO_SEGMENTS",
    "REASON_TRANSCRIBED",
    "REASON_ZERO_SPAN_SEGMENTS",
    "SILENT_PEAK_FLOOR",
    "SOURCE_IDS",
    "audio_path_for",
    "build_engine",
    "chunk_file",
    "chunk_label",
    "clear_chunk_dir",
    "declared_sources",
    "engine_descriptor",
    "is_digital_silence",
    "measure_span",
    "pcm_sample_chunk_counts",
    "pcm_span_is_silent",
    "plan_chunks",
    "read_chunk_seconds",
    "resolve_audio",
    "resolve_device_of",
    "resolve_engine",
    "run_session",
    "samples_to_count_floats",
    "samples_to_counts",
    "session_id_of",
    "span_is_silent",
    "write_chunk_wav",
]
