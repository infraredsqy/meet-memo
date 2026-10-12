"""音频归档 —— raw PCM 边采边写 / 收尾 PCM→MP3（`dca4539b` phase2 §3）。

两个函数从 V1 `mnotes_core/media.py` 搬过来，函数体逐字来自对照源，只有三处按 V2 的接口
层改过（每一处都在下面各自的 docstring 里写明理由）：

  - `_open_pcm`（`media.py:13-21`）→ `open_pcm()`：**无缓冲**（`buffering=0`）
  - `_convert_to_mp3`（`media.py:24-44`）→ `convert_to_mp3()`：去掉续接会议那一段
    （`prepend_mp3`，V1 的会议续接 V2 不做）、**转完不删 PCM**
  - 新增 `PcmArchiveWriter`（写线程 + 无缓冲落盘 + `fsync`）：V1 的写侧是
    `pipeline._audio_loop` 里那一句 `f.write(data)`（`pipeline.py:92-95`），逐字搬过来，
    外面套上 V2 契约要的「各自一条写线程 + 收尾 join + fsync」。

文件名与落盘位置由服务侧定死（`meeting/session.py` 的 `archive_name()`、`start()` 收到的
`session_dir`），本模块不猜路径。
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
    """打开一路**一段**的 raw PCM 录音文件（int16 mono，无 header），录制期间边采边写。

    搬自 V1 `mnotes_core/media.py:13-21`（`_open_pcm`）。V2 的两处差别，各有理由：

      - `open(path, "ab")` 改成 `open(path, "ab", buffering=0)`（**无缓冲**，一个词的改动）。
        理由：服务被强杀时最多丢最后一个块的尾巴，而带缓冲的写法会把几十 KB 压在进程
        内存里一起丢（`dca4539b` §3 的取舍）。
      - 追加语义**翻案**（`docs/design-intent.md` 第 9 条）：V1 的「续接会议 = 追加到旧
        文件末尾」作废——段只表达「这场会录了几次」，续接是**下一个序号的新段文件**，
        不追加旧文件。因此一个段文件只装**本段**的字节，打开模式是 **截断写**
        （`"wb"`）：同名残留（上一场崩在同一目录的半截段）被截掉，而不是被悄悄接上
        ——那正是第 9 条要消灭的「音频比文本长」悬案的来路。
    """
    return open(path, "wb", buffering=0)


def convert_to_mp3(pcm_path: Path | str, mp3_path: Path | str, bitrate: str = MP3_BITRATE) -> bool:
    """把 raw PCM（int16 mono）归档转成 MP3。**返回是否真的产出了 MP3**。

    搬自 V1 `mnotes_core/media.py:24-44`（`_convert_to_mp3`），函数体逐字来自对照源：

        pydub 的 `AudioSegment.from_raw(sample_width=2, frame_rate=SAMPLE_RATE, channels=1)`
        → `seg.export(mp3_path, format="mp3", bitrate=bitrate)`；
        空文件（该源无音频输入）不产出 MP3。

    与 V1 的差别（**有意**，理由写在 `dca4539b` §3）：

      - **转完不删 PCM**：V1 结尾是 `pcm_path.unlink(missing_ok=True)`（转走就把临时的
        `.pcm` 清掉）。V2 的 `session.json` 里 `sources[*].archives` 声明的是那些段 `.pcm`，
        删掉它那份记录就指向空气，所以这里**保留 PCM**；空文件那一档同理不再 `unlink`
        （V1 那里删的是「本来就没有内容」的文件，V2 留一个 0 字节文件与记录对齐）。
      - 去掉 `prepend_mp3`（续接会议时把旧 mp3 拼在本轮 PCM 之前）：V1 的会议续接与音频
        拼接**本节点不做**（§5「不做」那一行的清单），参数与那两行拼接一起去掉。

    `pcm_path` 不存在、或 `pydub` / `ffmpeg` 抛错时**原样上抛**：调用方（
    `meeting/capture/engine.py` 的 `transcode()`）按契约在该步记 `failed` 并保留 PCM。
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
    """一路**一段**归档的写侧：**自己的写线程**，从自己的队列里取一块、写一块。

    V1 的写侧是 `pipeline._audio_loop` 里按 tag 取块再写的那三行（`pipeline.py:89-95`）：

        tag, data = self.recorder.audio_queue.get(timeout=0.1)
        ...
        f = self._audio_files.get(tag)
        if f:
            f.write(data)

    这里逐字保留「取一块、写一块」（连 `timeout=0.1` 的取法都留着），外面按 V2 契约加四样
    东西（每一处都有理由）：

      - **两路各自一条写线程**（V1 是两路共用一条队列 + 一条写线程）：契约要求「一路出事
        另一路照跑」，分开写更直接，一路卡在磁盘上不会把另一路拖住；
      - **无缓冲写**（`open_pcm()` 的 `buffering=0`）：强杀最多丢最后一个块；
      - **收尾 `close()`**：`finalize()` 要能确定地停流、join、关文件、`fsync`；join 的上界
        由这里给（V1 那个 `join(timeout=2.0)` 不保证线程已退出）；
      - **静音块**（本节点新增）：`feed(..., silence=True)` 投进来的块写的是**数字零**
        （暂停期间、用户主动关掉的那一路、没开的那一路都走这一路），并单独记进
        `silence_bytes`——「实际录到的音频时长」这一项只数 `content_bytes`，
        `bytes_written` 仍是落盘总字节数（= 时间轴长度）。
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

    # -- 打开 / 关闭 -------------------------------------------------------

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

    # -- 写侧 -------------------------------------------------------------

    def feed(self, data: bytes, *, silence: bool = False) -> None:
        """读侧投一块进来（`data` 为空时什么都不做）。

        `silence=True` 的块**内容不落盘**：写线程写的是等长的数字零（`_write` 里替换），
        字节数记进 `silence_bytes`。这一格只该由「暂停 / 用户关一路 / 没开一路」的
        写静音路径投，普通内容块保持缺省。
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
