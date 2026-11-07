"""`tests/_offline_fakes.py` —— 会后离线链的跨文件共用假件（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点）：`FakeEngine` /
`FakeConfig` / `synth_session()` 一族造「音频是合成的、引擎是假的」的场次，`sparse_pcm()` /
`chunk_ranges()` / `silence_session()` 造分块与静音档，`LoadingEngine` / `GatedEngine`
探加载与闸门那两档时序。原先住在 `test_offline_runner.py`（`GatedEngine` 在
`test_offline_progress.py`）、被各用例文件互相 import 的正本搬到这里，用法一律
`from tests._offline_fakes import ...`。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from meeting.offline.engine import OfflineSegment


SAMPLE_RATE = 16000


#: 分块用例统一用这一档块长（10 秒，够短到用例里跑得完，也够长到一片一段装得下）
CHUNK_SECONDS = 10.0


class FakeEngine:
    """假引擎：按 `script` 交回分段，也可以按 `boom` 抛错、或按 `gate` 卡住不动。

    它记下每次收到的音频路径（用例据此证明两路**分别**调用、没有先混音）。

    「慢引擎」那一档用 `gate`（一条闸门 `threading.Event`）来当：真实引擎的一次整段
    推理是**有明确终点的长活儿**，闸门由用例放行，比按秒数干等更贴近它。
    """

    def __init__(self, script=None, *, boom: str | None = None, gate=None,
                 device: str = "auto") -> None:
        self.script = script
        self.boom = boom
        self.gate = gate
        #: 真引擎构造时就把 `auto` 解析成 `cuda:0` / `cpu` 存在这一格上；产物读的是它
        self.device = device
        self.calls: list[str] = []

    def transcribe(self, audio_path):
        if self.gate is not None:
            self.gate.wait(timeout=10.0)
        self.calls.append(str(audio_path))
        if self.boom:
            raise RuntimeError(self.boom)
        if self.script is not None:
            return list(self.script)
        return [
            OfflineSegment(0.0, 1.0, "S01", f"{Path(audio_path).name} 的一句"),
            OfflineSegment(1.0, 2.5, "S02", f"{Path(audio_path).name} 的另一句"),
        ]


class FakeConfig:
    """最小的一份配置：只有会后那几个键（`runner` 只读它们）。"""

    def __init__(self, **values) -> None:
        self.values = {
            "post.engine": "auto",
            "post.model": "OpenMOSS-Team/MOSS-Transcribe-Diarize",
            "post.dtype": "bfloat16",
            # 缺省与 `config.DEFAULT_VALUES` 同值：用例的 3 秒音频一块装得下，走单趟
            "post.chunk_seconds": 450,
            # 会后纪要那一层的钩子：测试层收成缺席档（`minutes.hook = null`）——
            # 本机 PATH 上真的有 `dsh`，`auto` 会让每一场跑完离线链的用例都真的拉起一个
            # headless 子会话去跑纪要。要验钩子的用例自己传 `minutes.hook` 覆盖它。
            "minutes.hook": "null",
        }
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


def synth_session(
    root: Path,
    *,
    sources=("mic", "loopback"),
    seconds=3.0,
    empty=(),
    missing=(),
    session_id="250919",
) -> Path:
    """合成一个场次目录：两路 16k 单声道 int16 裸 PCM + 一份最小的 `session.json`。"""
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        path = root / f"meeting_audio_{source}_001.pcm"
        if source in missing:
            continue
        if source in empty:
            path.write_bytes(b"")
            continue
        path.write_bytes(b"\x00\x01" * int(seconds * SAMPLE_RATE))
    # `session.json` 声明的是**这一场开过的路**（文件在不在场由 `missing` / `empty` 说了算）
    declared = list(sources)
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "dir": str(root),
                "mode": "both" if len(declared) > 1 else (declared or ["mic"])[0],
                "state": "done",
                "sources": [
                    {"id": source, "archive": f"meeting_audio_{source}_001.pcm"}
                    for source in declared
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def sparse_pcm(session: Path, source: str, seconds: float) -> Path:
    """造一份**指定长度**的归档 PCM，并且**每一块里都有信号**。

    用例不需要真音频（引擎是假的），只需要「文件字节数 = 时长」这一个事实；但分块那条路
    会把每块写成 wav，而用例整段零值音频写出来的分块就是一片零，谁也分不清「切对了位置」
    与「切出一堆静音」。做法：先 `seek` 到末尾写出长度（稀疏文件，当场就有、不占盘），
    再按 0.5 秒一块把带信号的字节块写在**稀疏区之上**（长度只按真正写下的那些算）。
    """
    path = session / f"meeting_audio_{source}_001.pcm"
    total = int(seconds * SAMPLE_RATE) * 2
    pattern = b"\x01\x01" * 4096  # 8192 字节的非零样本（int16 的 257），每 4 秒铺一块
    with open(path, "wb") as handle:
        handle.seek(total - 2)
        handle.write(b"\x01\x00")
        step = len(pattern)
        for begin in range(0, total - step, step * 2):
            handle.seek(begin)
            handle.write(pattern)
    assert path.stat().st_size == total
    return path


def chunk_ranges(seconds: float, *, chunk_seconds: float = CHUNK_SECONDS):
    """按 `runner` 的同一条切法算出每块覆盖的字节区间：`[(起点, 终点, 块号), …]`。"""
    step = int(chunk_seconds * SAMPLE_RATE) * 2
    total = int(seconds * SAMPLE_RATE) * 2
    out = []
    for index, begin in enumerate(range(0, total, step), 1):
        out.append((begin, min(begin + step, total), index))
    return out


def silence_session(
    tmp_path,
    *,
    seconds=30.0,
    chunk_seconds=CHUNK_SECONDS,
    silent_chunks=(),
    quiet_chunks=(),
    source="mic",
    name="silence",
    ranges=None,
):
    """合成一个「哪几块是数字静音」写在脸上的场次：`silent_chunks` 那几块全零。

    零时长音频与真音频在盘上只有字节的区别：`silent_chunks` 那几块写全零，
    其余块写一个有信号的字节（`\\x00\\x01` = int16 的 256，与 `synth_session()` 同一档
    最小信号）。`quiet_chunks` 那几块另外只写**一个**样本（`\\x01\\x00` → 16 bit 满量程的
    -90.3 dB），用来验「安静但有声」那一半。交回 `(场次目录, 块区间表)`。

    `ranges` 显式给出块区间表（`chunk_seconds = 0` 那一档算不出块表：整段就是一块）。
    """
    session = tmp_path / name
    session.mkdir(parents=True, exist_ok=True)
    if ranges is None:
        ranges = chunk_ranges(seconds, chunk_seconds=chunk_seconds)
    path = session / f"meeting_audio_{source}_001.pcm"
    payload = bytearray(int(seconds * SAMPLE_RATE) * 2)
    for begin, _end, index in ranges:
        if index in silent_chunks:
            continue  # 全零：这一块就是实测那一档的数字静音
        payload[begin:begin + 2] = b"\x01\x00" if index in quiet_chunks else b"\x00\x01"
    path.write_bytes(bytes(payload))
    (session / "session.json").write_text(
        json.dumps(
            {
                "session_id": session.name,
                "dir": str(session),
                "mode": source,
                "state": "done",
                "sources": [{"id": source}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return session, ranges


class LoadingEngine:
    """假引擎：带 `_load()` 这道接缝（探加载那一下打在它身上），两个计数都给用例看。

    `_load()` **每被调一次就记一次**（真引擎那一版自己认「已经加载好了」，这里不认）：
    「探了几次」于是直接读 `loads`，而 `calls` 是 `transcribe()` 被调了几次。
    """

    def __init__(self, *, load_error: str | None = None, device: str = "cuda:0") -> None:
        #: `_ensure_engine_loaded()` 先读这一格；假件留 `None`，让它走 `_load()` 那条路
        self.load_error = None
        self._failure = load_error
        self.device = device
        self.loads = 0
        self.calls: list[str] = []

    def _load(self) -> None:
        self.loads += 1
        if self._failure is not None:
            raise OSError(self._failure)

    def transcribe(self, audio_path):
        self.calls.append(str(audio_path))
        return [OfflineSegment(0.0, 1.0, "S01", f"{Path(audio_path).name} 的一句")]


#: 块卡住时的放行/等待上限（等到了就立刻回，不赌固定时长）
GATE_TIMEOUT_SECONDS = 5.0


class GatedEngine(FakeEngine):
    """按**第几次**调用放行的假引擎：第 `block_at` 次调用卡在闸门上（从 1 数起）。"""

    def __init__(self, gate: threading.Event, *, block_at: int) -> None:
        super().__init__()
        self._gate = gate
        self._block_at = int(block_at)
        self._calls = 0
        self.reached = threading.Event()

    def transcribe(self, audio_path):
        self._calls += 1
        if self._calls == self._block_at:
            self.reached.set()
            self._gate.wait(timeout=GATE_TIMEOUT_SECONDS)
        return super().transcribe(audio_path)
