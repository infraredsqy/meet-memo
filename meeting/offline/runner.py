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

## 惰性

`torch` / `transformers` / `av` 都不在本模块顶层 import：它们要么在 `meeting/offline/audio.py`
的函数体内，要么在 `MossOfflineEngine.transcribe()` 里。`import meeting.offline.runner`
因此只拉标准库与包内模块（由 `tests/test_post_hook.py` 在全新解释器里盯着）。

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

#: 块内标签的命名空间模板：第 `NN` 块的 `Sxx` 变成 `cNN:Sxx`（`c01:S01`）
CHUNK_LABEL_TEMPLATE = "c{index:02d}:{label}"

#: 分块时临时 wav 住在场次目录下的这个子目录里，跑完一块不留
CHUNK_DIR_NAME = "_offline_chunks"


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
    """一块跑完之后的如实记账（成功与否都记一条）。"""

    index: int
    offset: float
    seconds: float
    segments: int = 0
    wall_seconds: float = 0.0
    error: str | None = None

    @property
    def span(self) -> tuple[float, float]:
        return (round(float(self.offset), 3), round(float(self.offset + self.seconds), 3))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset_seconds": round(float(self.offset), 3),
            "seconds": round(float(self.seconds), 3),
            "span": list(self.span),
            "segments": int(self.segments),
            "wall_seconds": round(float(self.wall_seconds), 3),
            "error": self.error,
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
            }
            for source, row in per_source.items()
        },
        finished_at=_now_iso(),
        error=error,
        partial=partial,
    )

    summary = {
        "session_id": session_id,
        "session_dir": str(directory),
        "state": state,
        "partial": partial,
        "sources": list(sources),
        "error": error,
        "total_wall_seconds": round(total_wall, 3),
        "engine": descriptor,
        "report": report_path.name,
        "status": status_path.name,
        "transcripts": per_source,
    }
    _log.info(
        "会后离线转写结束：%s（%s，%d 路，%.2fs）",
        session_id, state, len(sources), total_wall,
    )
    return summary


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
            plan = _plan_for_source(directory, source, path, config=config)
            metric.audio_seconds = plan.total_seconds
            segments, runs, any_ok = _execute_chunks(
                directory, source, path, engine=engine, plan=plan
            )
        except Exception as exc:  # 切法/解码抛错：这一路失败，另一路照跑
            metric.state = "failed"
            metric.audio_file = Path(path).name
            metric.error = f"{REASON_ENGINE_ERROR}：{type(exc).__name__}: {exc}"
            metric.reasons[REASON_ENGINE_ERROR] = 1
            _log.exception("会后离线：%s 这一路引擎抛错（如实记账，继续跑另一路）", source)
        else:
            _record_chunk_runs(metric, runs)
            if not any_ok:
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
            # 与报告里，与 phase2 立下的口径一致（不留空文件充数）。
            wrote_artifacts = any_ok or len(runs) > 1

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
) -> ChunkPlan:
    """算这一路的切法（块长读 `post.chunk_seconds`，总时长按音频类型分别量）。

    归档 PCM 的时长由**文件字节数**算出（不读内容）：一份 5943 秒的 PCM 是 190 MB，
    为了量长度先读一遍它并不划算。别的容器解一遍取样本数（`decode_audio()` 给出的就是
    16k 单声道波形，样本数 ÷ 16000 即秒数），解码那一次也用来切块。

    量长度这一步失败时**退回单趟**（时长记 0）：解不出来是引擎该报的错（它反正要解一遍），
    在这里替它下结论会让「引擎交回什么」这件事变成「我们量长度成没成」。
    """
    del directory, source  # 切法只认路径与块长，两路之间没有别的差别
    audio_path = Path(path)
    chunk_seconds = read_chunk_seconds(config)
    try:
        if audio_path.suffix.lower() == ".pcm":
            total = _pcm_sample_count(audio_path) / PCM_SAMPLE_RATE
        else:
            total = float(_decode_samples(audio_path).size) / PCM_SAMPLE_RATE
    except Exception:
        _log.warning(
            "会后离线：%s 的时长量不出来（退回整段一次跑，交给引擎如实报错）",
            path, exc_info=True,
        )
        return plan_chunks(0.0, chunk_seconds)
    return plan_chunks(total, chunk_seconds)


def _execute_chunks(
    directory: Path,
    source: str,
    path: str,
    *,
    engine: Any,
    plan: ChunkPlan,
) -> tuple[list[OfflineSegment], list[ChunkRun], bool]:
    """按切法逐块调引擎，交回 `(拼回整场时间轴的分段, 逐块记账, 有没有一块跑成)`。

    单趟（块数为 1）时直接拿原始音频路径喂引擎，**一个临时文件都不写**，于是
    「音频不长于块长」这条路的产物与加 `post.chunk_seconds` 之前逐字一致。

    分块时：块内波形写成临时 wav（`write_chunk_wav()`）→ 调引擎 → 把每段的
    `start` / `end` 加上该块偏移、说话人标签加上块内命名空间。**块与块之间不传任何东西**
    （不比对标签、不映射编号），每一块只往 `merged` 里追加自己的结果。

    任何一块抛错都就地收下、记一笔，继续跑下一块：一块坏掉不该把已经跑出来的
    几十分钟音频一起废掉，但它**必须露在产物里**（见 `ChunkRun.error`）。

    跑完（或抛出来）都把临时块扫干净：分块目录是这一趟的中间产物，留着只会让下一趟
    分不清哪一份是哪一次的。产物里留下的是**逐块的记账**（`ChunkRun`），音频本身可重切。
    """
    steps = plan.chunks
    materialize = len(steps) > 1
    if materialize:
        clear_chunk_dir(directory)
    waveform = None
    if materialize and Path(path).suffix.lower() != ".pcm":
        # 非 PCM（mp3 那条路）：整段解一次，逐块从这段波形上取
        waveform = _decode_samples(path)

    merged: list[OfflineSegment] = []
    runs: list[ChunkRun] = []
    any_ok = False
    try:
        for step in steps:
            try:
                target = (
                    _materialize_chunk(directory, path, step, waveform)
                    if materialize
                    else str(path)
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
                    )
                )
                _log.warning(
                    "会后离线：%s 第 %d 块（%.0fs 起）一段都没交回，如实记账后继续",
                    source, step.index, step.offset,
                )
                continue

            runs.append(
                ChunkRun(
                    index=step.index,
                    offset=step.offset,
                    seconds=step.seconds,
                    segments=len(produced),
                    wall_seconds=wall,
                )
            )
            any_ok = True
            merged.extend(
                _shift_segment(segment, step, namespaced=plan.namespace)
                for segment in produced
            )
    finally:
        if materialize:
            clear_chunk_dir(directory)
    return merged, runs, any_ok


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
    import numpy as np

    target = chunk_file(directory, step.index)
    start = int(round(float(step.offset) * PCM_SAMPLE_RATE))
    end = int(round((float(step.offset) + float(step.seconds)) * PCM_SAMPLE_RATE))
    if waveform is not None:
        piece = np.asarray(waveform)[start:end]
        return str(write_chunk_wav(target, piece))

    with open(path, "rb") as handle:
        handle.seek(start * PCM_SAMPLE_WIDTH)
        raw = handle.read(max(0, end - start) * PCM_SAMPLE_WIDTH)
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    return str(write_chunk_wav(target, samples))


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
    """
    metric.chunks = [run.to_dict() for run in runs]
    for run in runs:
        if run.error is None:
            continue
        metric.missing_spans.append(run.span)
        reason = run.error.split("：", 1)[0]
        metric.reasons[reason] = metric.reasons.get(reason, 0) + 1
    metric.partial = bool(metric.missing_spans)


def _chunk_error_note(runs: Sequence[ChunkRun]) -> str:
    """「哪些块没跑成、为什么」那句话（报告与状态文件里都用它）。

    每一块后面跟它自己的原因（`engine_error：RuntimeError: 显存不够` 或 `no_segments：…`），
    于是这一路的 `error` 同时说得出**丢的是哪一段**与**为什么丢**。
    """
    failed = [run for run in runs if run.error is not None]
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
    if metric.partial:
        lines.insert(0, artifacts.incomplete_note(metric.missing_spans))
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
    "PCM_SAMPLE_RATE",
    "PCM_SAMPLE_WIDTH",
    "REASON_ALL_CHUNKS_FAILED",
    "REASON_EMPTY_PCM",
    "REASON_ENGINE_ERROR",
    "REASON_NO_AUDIO",
    "REASON_NO_SEGMENTS",
    "SOURCE_IDS",
    "audio_path_for",
    "build_engine",
    "chunk_file",
    "chunk_label",
    "clear_chunk_dir",
    "declared_sources",
    "engine_descriptor",
    "plan_chunks",
    "read_chunk_seconds",
    "resolve_audio",
    "resolve_device_of",
    "resolve_engine",
    "run_session",
    "session_id_of",
    "write_chunk_wav",
]
