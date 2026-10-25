"""音频归档——raw PCM 边采边写 / 收尾 PCM→MP3（`dca4539b` phase2 §3）。

从 V1 `mnotes_core/media.py` 搬过来，函数体逐字来自对照源，三处按 V2 接口层改过：
`open_pcm()` 无缓冲（`buffering=0`）；`convert_to_mp3()` 去掉 `prepend_mp3`（V1 的续接
拼接 V2 不做）、转完不删 PCM；新增 `PcmArchiveWriter`（写线程 + 无缓冲落盘 + `fsync`，
V1 的写侧是 `pipeline._audio_loop` 里那一句 `f.write(data)`，`pipeline.py:92-95`）。

文件名与落盘位置由服务侧定死（`meeting/session.py` 的 `archive_name()`），本模块不猜路径。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path

_log = logging.getLogger("meeting.capture.archive")

#: 归档的 PCM 形状（16k / 单声道 / int16 小端裸 PCM，无头；与 `session.json` 的声明一致）
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1

#: MP3 的码率（照 V1 的默认值）
MP3_BITRATE = "64k"

#: 空 PCM 的判据：一个 int16 样本都装不下（0 或 1 字节）
#: （V1 写的是 `st_size == 0`；「转不动就留下 PCM」之后，1 字节的残缺文件也该按空处理）
EMPTY_PCM_BYTES = SAMPLE_WIDTH

#: 写线程的收队哨兵（`None` 不入队，因此拿它当「不再有块了」）
_CLOSE_SENTINEL = None


def open_pcm(path: Path | str):
    """打开一路一段的 raw PCM 录音文件（int16 mono，无 header），录制期间边采边写。

    V2 两处差别：`"ab"` 改成 `"wb"` + 无缓冲——无缓冲是强杀时最多丢最后一个块的尾巴
    （带缓冲会把几十 KB 压在内存里一起丢）；截断写是第 9 条的翻案（续接 = 下一个序号的
    新段文件，不追加旧文件），同名残留被截掉而不是悄悄接上——那正是第 9 条要消灭的
    「音频比文本长」悬案的来路。
    """
    return open(path, "wb", buffering=0)


def convert_to_mp3(pcm_path: Path | str, mp3_path: Path | str, bitrate: str = MP3_BITRATE) -> bool:
    """把 raw PCM（int16 mono）归档转成 MP3。返回是否真的产出了 MP3。

    搬自 V1 `media.py:24-44`，函数体逐字来自对照源（pydub `AudioSegment.from_raw` →
    `export(format="mp3", bitrate=...)`）。与 V1 的差别（有意，见 `dca4539b` §3）：
    转完不删 PCM——`session.json` 的 `sources[*].archives` 声明的是那些段 `.pcm`，删掉它
    记录就指向空气；空文件那一档同理不再 `unlink`（留 0 字节文件与记录对齐）。去掉
    `prepend_mp3`（续接拼接本节点不做，§5「不做」清单）。

    `pcm_path` 不存在、或 `pydub` / `ffmpeg` 抛错时原样上抛：调用方按契约记 `failed`
    并保留 PCM。
    """
    pcm = Path(pcm_path)
    mp3 = Path(mp3_path)
    if pcm.stat().st_size < EMPTY_PCM_BYTES:
        # 空文件（该源无音频输入）：不产出 MP3（照 V1；V1 那一步顺带删掉空 PCM，V2 不删）
        return False
    from pydub import AudioSegment

    seg = AudioSegment.from_raw(
        pcm, sample_width=SAMPLE_WIDTH, frame_rate=SAMPLE_RATE, channels=CHANNELS
    )
    seg.export(mp3, format="mp3", bitrate=bitrate)
    return True


def mp3_path_for(pcm_path: Path | str) -> Path:
    """PCM 归档的 MP3 产物路径：**同目录同名换扩展名**（照 V1 的 `meeting_audio_mic.mp3`）。"""
    return Path(pcm_path).with_suffix(".mp3")


class PcmArchiveWriter:
    """一路一段归档的写侧：自己的写线程，从自己的队列里取一块、写一块。

    「取一块、写一块」（连 `timeout=0.1` 的取法）逐字保留自 V1 的
    `pipeline._audio_loop`（`pipeline.py:89-95`），外面按 V2 契约加四样：两路各自一条
    写线程（一路卡在磁盘上不拖住另一路）；无缓冲写（强杀最多丢最后一个块）；收尾
    `close()`（`finalize()` 要能确定地停流、join、关文件、`fsync`）；静音块
    （`feed(..., silence=True)` 写的是数字零并单记 `silence_bytes`——「实际录到的音频
    时长」只数 `content_bytes`，`bytes_written` 是落盘总字节数 = 时间轴长度）。
    """

    def __init__(self, path: Path | str, *, label: str = "") -> None:
        self.path = Path(path)
        self.label = label or self.path.name
        self._queue: queue.Queue[tuple[bytes, bool] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._handle = None
        #: 收队信号（写线程正在把队列里剩下的块写完）
        self._closing = False
        #: 真正关掉了（`close()` 走完）
        self._closed = False
        self._lock = threading.Lock()
        #: 真正写下去的字节数（收尾的核对口径：字节数 = 32000 字节/秒 × 秒数）
        self.bytes_written = 0
        self.chunks_written = 0
        #: 其中内容 / 静音各多少字节（「实际录到的音频时长」只数内容那一格）
        self.content_bytes = 0
        self.silence_bytes = 0

    def open(self) -> None:
        """建出归档文件并起写线程（**文件在这一刻就存在**，还没起采的那一路是 0 字节文件）。"""
        with self._lock:
            if self._closing:
                raise RuntimeError(f"{self.label} 的归档已经关过，不复用")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._handle is None:
                self._handle = open_pcm(self.path)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, args=(self._handle,), name=f"archive-{self.label}", daemon=True
                )
                self._thread.start()

    def close(self, *, timeout: float = 5.0) -> None:
        """收队：唤醒写线程、等它退出、关文件并 `fsync`（**幂等**，可重复调）。

        `timeout` 是 join 的**确定上界**：写线程是 daemon，超时也不阻塞进程退出，但那一声
        会落一条 warning（不许静默）。

        次序是「先唤醒、再 join、**最后**关句柄」：写线程收队时会把队列里还压着的块写完，
        而它在写完之前一直拿着句柄 —— 因此 `ValueError: I/O operation on closed file`
        那条竞态不会出现（写线程与 `close()` 共用同一把锁）。
        """
        with self._lock:
            if self._closing:
                return
            self._closing = True
            thread = self._thread
            handle = self._handle
        self._queue.put(_CLOSE_SENTINEL)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                _log.warning(
                    "归档写线程 %s 在 %ss 内没有退出（daemon 线程，不阻塞进程退出）",
                    self.label, timeout,
                )
        with self._lock:
            self._closed = True
            self._thread = None
            self._handle = None
        if handle is not None:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, ValueError):  # pragma: no cover - flush/fsync 失败不掩盖收尾结论
                _log.warning("归档 %s 的 flush/fsync 失败", self.label, exc_info=True)
            finally:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - 同上
                    pass

    def feed(self, data: bytes, *, silence: bool = False) -> None:
        """读侧投一块进来（`data` 为空时什么都不做）。

        `silence=True` 的块内容不落盘：写线程写等长的数字零，字节数记进 `silence_bytes`。
        只该由「暂停 / 用户关一路 / 没开一路」的写静音路径投。
        """
        if not data:
            return
        self._queue.put((bytes(data), bool(silence)))

    def _run(self, handle) -> None:
        """写线程：取一块、写一块（照 `pipeline._audio_loop` 的那三行）。"""
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closing:
                    break
                continue
            if item is _CLOSE_SENTINEL:
                break
            self._write(handle, *item)
        # 收尾时把这一轮还压在队列里的块写完（强杀也最多丢最后一个块）
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _CLOSE_SENTINEL:
                continue
            self._write(handle, *item)

    def _write(self, handle, data: bytes, silence: bool = False) -> None:
        """写一块并登记（句柄已经关掉时不再写：这一支只在 join 超时那条路上会走到）。

        静音块在这里替换成等长数字零：**队列里从不放内容**，替换发生在写线程这一侧，
        暂停期间的真音频到不了盘上（`docs/design-intent.md` 第 8 条的隐私线）。
        """
        with self._lock:
            if self._closed:
                return
            if silence:
                handle.write(bytes(len(data)))
                self.silence_bytes += len(data)
            else:
                handle.write(data)
                self.content_bytes += len(data)
            self.bytes_written += len(data)
            self.chunks_written += 1


__all__ = [
    "CHANNELS",
    "EMPTY_PCM_BYTES",
    "MP3_BITRATE",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "PcmArchiveWriter",
    "convert_to_mp3",
    "mp3_path_for",
    "open_pcm",
]
