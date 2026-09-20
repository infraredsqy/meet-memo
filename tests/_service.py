"""真服务子进程夹具的驱动件与全部解析 / 等待助手（phase2 §5.4）。

**定位**：本文件是测试支持模块 —— 文件名不以 `test_` 开头，`pytest` **不收集**它；
它**不是 `conftest.py`**，因此不提供 fixture（fixture 全在 `tests/conftest.py`）。

**导出全集（九个名字，逐字冻结；phase3 只引用、不自起别名）**：

    ServiceProcess / SseClient / post_action() / dev_inject() / read_session_json() /
    write_config_json() / parse_sse_frame() / read_sse_frames() / wait_until()

`__all__` **恰为这九个**；模块内另有几个**常量**（`WAIT_POLL_INTERVAL_SECONDS` /
`SERVICE_ID` 与三条 SSE 行正则）随本模块一起工作，它们是本模块的实现细节，**不进导出名单**
（名单一漂移，phase3 就会照着一个并不存在的导出名去 import）。`iter_session_dirs()` 这个
无人使用的第十个公开名已删掉。

`wait_for()` 这个旧名**全仓作废**（等待助手只有 `wait_until()` 一个名字）。

与 phase1 的 `tests/_helpers.py` 的分工（**同一职责只留一处落点**）：子进程的底层构造
（`spawn_service()` / 家目录隔离 / 假 `webbrowser` 注入）与 ASGI 客户端仍在 `_helpers.py`
里；本模块**复用**它们并在之上补齐 phase2 契约要求的能力（`base_url` / 发中断信号 /
读退出码 / 完整的环境变量覆盖），**不另起第二套子进程管理**。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

import _helpers as helpers
from _helpers import REPO_ROOT

#: `wait_until()` 的缺省轮询间隔（秒）；用例仍须显式传 `timeout`
WAIT_POLL_INTERVAL_SECONDS = 0.05

#: 服务标识（就绪判定与端口探针同一判据）
SERVICE_ID = "meeting"

#: SSE 帧里 `event:` / `data:` / `id:` 三行的解析
_EVENT_RE = re.compile(r"^event:\s*(.*)$")
_DATA_RE = re.compile(r"^data:\s?(.*)$")
_ID_RE = re.compile(r"^id:\s*(.*)$")

__all__ = [
    "ServiceProcess",
    "SseClient",
    "dev_inject",
    "parse_sse_frame",
    "post_action",
    "read_session_json",
    "read_sse_frames",
    "wait_until",
    "write_config_json",
]


# ---------------------------------------------------------------------------
# 等待助手
# ---------------------------------------------------------------------------


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    interval: float = WAIT_POLL_INTERVAL_SECONDS,
) -> Any:
    """同步等待一个条件成立（`predicate` 返回真值即返回它）。

    `timeout` **必传**（用例里显式给具名秒数、不留裸数字）；到超时抛 `AssertionError`。
    谓词自身抛异常时继续等（探针还没就绪是常态）。
    """
    deadline = time.monotonic() + float(timeout)
    last: Any = None
    while True:
        try:
            last = predicate()
        except Exception as exc:  # 探针自身报错时继续等
            last = f"{type(exc).__name__}: {exc}"
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"wait_until 超时（{timeout}s，间隔 {interval}s）：最后观察值 {last!r}"
            )
        time.sleep(interval)


# ---------------------------------------------------------------------------
# 真服务子进程
# ---------------------------------------------------------------------------


class ServiceProcess(helpers.ServiceProcess):
    """一个真服务子进程（`[sys.executable, "-m", "meeting", "web"]`，cwd = 仓库根）。

    在 phase1 的 `_helpers.ServiceProcess` 之上补齐 phase2 契约点名的三项能力与七项环境变量
    覆盖：**`base_url`**（`MEETING_PORT=0` + 子进程启动输出解析）、**向子进程发中断信号**、
    **读退出码**（`returncode`）；环境变量覆盖由调用方给全（`env=` 完整映射，或给
    `home` + 各项；`PYTHONPATH` = 仓库根 + 临时假件目录，不需要假件时至少保留仓库根）。
    """

    def __init__(
        self,
        home: Path,
        *,
        action: str = "web",
        capture: str | None = None,
        port: int | None = 0,
        out_root: Path | str | None = None,
        log_dir: Path | str | None = None,
        no_browser: bool = True,
        pythonpath: str | None = None,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        self.home = home

        # 环境变量覆盖：调用方给全的 `env` 优先；否则按七项布置（家目录两项都设）
        overrides: dict[str, str] = {}
        overrides["HOME"] = str(home)
        overrides["USERPROFILE"] = str(home)
        if port is not None:
            overrides["MEETING_PORT"] = str(port)
        if out_root is not None:
            overrides["MEETING_OUT_ROOT"] = str(out_root)
        if log_dir is not None:
            overrides["MEETING_LOG_DIR"] = str(log_dir)
        if capture is not None:
            overrides["MEETING_CAPTURE"] = str(capture)
        if no_browser:
            overrides["MEETING_NO_BROWSER"] = "1"
        else:
            overrides.pop("MEETING_NO_BROWSER", None)
        overrides["PYTHONPATH"] = (
            str(REPO_ROOT) if pythonpath is None else str(pythonpath)
        )
        if extra_env:
            overrides.update(extra_env)
        if env:
            overrides = dict(env)

        # 看门断言（与 `_helpers.spawn_service()` 同一条）：走到「打开浏览器」这一步而不注入
        # 假 webbrowser，就会落到宿主的默认浏览器上 —— 直接把用例判红。
        if overrides.get("MEETING_NO_BROWSER", "") != "1" and not overrides.get(
            helpers.FAKE_BROWSER_PATH_ENV
        ):
            raise AssertionError(
                "子进程要走到「打开浏览器」这一步，就必须经 fake_browser_module() 注入假 "
                "webbrowser（env 里带 MEETING_TEST_FAKE_BROWSER_PATH）：否则这一步会落到"
                "宿主的默认浏览器上，测试会打开真窗口。"
            )

        self._proc = subprocess.Popen(
            [sys.executable, "-m", "meeting", action],
            cwd=str(REPO_ROOT),
            env=helpers.service_env(home, extra=overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._all_lines: list[str] = []
        self._reader = threading.Thread(target=self._pump, name="service-stdout", daemon=True)
        self._reader.start()
        self._base_url: str | None = None

    # -- 读数 -------------------------------------------------------------

    def _pump(self) -> None:
        stream = self._proc.stdout
        if stream is None:  # pragma: no cover - PIPE 一定在
            return
        for line in stream:
            self._all_lines.append(line)
            self._lines.put(line)

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        """读退出码（进程还在跑时为 `None`）。"""
        return self._proc.poll()

    def poll(self) -> int | None:
        return self._proc.poll()

    def stdout(self) -> str:
        """子进程 stdout 到目前为止的全部内容。"""
        return "".join(self._all_lines)

    def stdout_text(self) -> str:
        return self.stdout()

    def stderr(self) -> str:
        """子进程 stderr（进程已收场时读管道；还在跑就返回空串，避免阻塞）。"""
        stream = self._proc.stderr
        if stream is None or self._proc.poll() is None:
            return ""
        try:
            return stream.read()
        except Exception:  # pragma: no cover - 管道已关
            return ""

    def log_path(self) -> Path:
        return helpers.service_log_path_of(self.home)

    def log_text(self) -> str:
        return helpers.read_text(self.log_path())

    @property
    def base_url(self) -> str:
        """`MEETING_PORT=0` 时从子进程启动输出里解析出的地址（**不猜端口**）。"""
        if self._base_url is None:
            self._base_url = self.wait_for_base_url()
        return self._base_url

    def wait_for_base_url(self, *, timeout: float = 20.0) -> str:
        """等子进程打印 `URL：http://127.0.0.1:<port>` 并解析成 `base_url`。"""

        def parsed() -> str | None:
            for line in list(self._all_lines):
                match = re.search(r"URL：(http://127\.0\.0\.1:\d+)", line)
                if match and not match.group(1).endswith(":0"):
                    return match.group(1)
            return None

        return str(wait_until(parsed, timeout=timeout))

    def port(self, *, timeout: float = 20.0) -> int:
        return int(self.base_url.rsplit(":", 1)[1])

    def wait_ready(self, *, timeout: float = 20.0) -> str:
        """就绪判定 = `/api/heartbeat` 回 200 且 body 里 `"service": "meeting"`。"""
        url = self.wait_for_base_url(timeout=timeout)

        def ready() -> bool:
            if self._proc.poll() is not None:
                raise AssertionError(
                    f"服务子进程提前退出（退出码 {self._proc.poll()}）；"
                    f"stdout={self.stdout()[-2000:]} stderr={self.stderr()[-2000:]}"
                )
            try:
                response = helpers.local_get(f"{url}/api/heartbeat", timeout=1.0)
            except Exception:
                return False
            if response.status_code != 200:
                return False
            return f'"{SERVICE_ID}"' in response.text or f"'{SERVICE_ID}'" in response.text

        wait_until(ready, timeout=timeout)
        return url

    def snapshot(self) -> dict[str, Any]:
        response = helpers.local_get(f"{self.base_url}/api/state", timeout=5.0)
        return response.json()

    # -- 信号 / 收场 -------------------------------------------------------

    def send_interrupt(self) -> None:
        """向子进程发中断信号（Windows 上经 `CTRL_BREAK_EVENT`，对应服务侧的 `SIGBREAK`）。"""
        if os.name == "nt":
            os.kill(self._proc.pid, 6)  # 6 = CTRL_BREAK_EVENT
        else:  # pragma: no cover - 本项目在 Windows 上跑
            os.kill(self._proc.pid, 2)

    def send_ctrl_c(self) -> None:
        """发 `CTRL_C_EVENT`（与 `CTRL_BREAK_EVENT` 走服务侧的同一个处理函数）。"""
        if os.name == "nt":
            os.kill(self._proc.pid, 0)
        else:  # pragma: no cover
            self._proc.send_signal(2)

    def wait(self, *, timeout: float = 20.0) -> int:
        """等子进程收场并返回退出码。"""
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
            raise AssertionError(f"服务子进程没有在 {timeout}s 内退出")
        return int(self._proc.returncode or 0)

    def kill(self) -> None:
        """硬杀（模拟断电 / 被任务管理器杀掉）。"""
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)

    def terminate(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._proc.kill()
                self._proc.wait()

    def __enter__(self) -> "ServiceProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.terminate()


# ---------------------------------------------------------------------------
# SSE 解析与读取
# ---------------------------------------------------------------------------


def parse_sse_frame(text: str) -> dict[str, Any]:
    """把一个 SSE 帧的原文解析成 `{"event", "data", "id", "comment", "raw"}`。

    `data` 是**解析后的 JSON**（解析不了时保留原文并标 `"parsed": False`）；
    注释帧（`: ping`）里 `comment` 有值、`event` 为 `None`。
    """
    frame: dict[str, Any] = {
        "event": None,
        "data": None,
        "id": None,
        "comment": None,
        "raw": text,
        "parsed": False,
        "retry": None,
    }
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(":"):
            frame["comment"] = line[1:].strip()
            continue
        match = _EVENT_RE.match(line)
        if match:
            frame["event"] = match.group(1).strip()
            continue
        match = _DATA_RE.match(line)
        if match:
            data_lines.append(match.group(1))
            continue
        match = _ID_RE.match(line)
        if match:
            frame["id"] = match.group(1).strip()
            continue
        if line.startswith("retry:"):
            frame["retry"] = line.split(":", 1)[1].strip()
    if data_lines:
        raw = "\n".join(data_lines)
        try:
            frame["data"] = json.loads(raw)
            frame["parsed"] = True
        except ValueError:
            frame["data"] = raw
    return frame


def _split_frame(buffer: str) -> tuple[str, str, str]:
    """从缓冲区里切出一个 SSE 帧：返回 `(帧原文, 分隔符, 剩下的部分)`。

    SSE 的空行既可能是 `\\n\\n`，也可能是 `\\r\\n\\r\\n`（uvicorn 在某些实现下会规范成后者），
    两者都要认，缺一种就会「一帧都读不到」。
    """
    for separator in ("\r\n\r\n", "\n\n"):
        index = buffer.find(separator)
        if index != -1:
            return buffer[:index], separator, buffer[index + len(separator):]
    return buffer, "", ""


class SseClient:
    """一条 SSE 订阅（同步 `httpx.stream` + 后台读线程；`close()` 保证断开）。

    用法（测试一律同步写）：:

        with SseClient(base_url) as client:
            first = client.read_frame(timeout=10.0)
            frames = client.read_frames(3, timeout=10.0)
    """

    def __init__(self, base_url: Any, *, path: str = "/api/events", timeout: float = 30.0) -> None:
        self.base_url = _resolve_base_url(base_url)
        self.path = path
        self._frames: queue.Queue[dict[str, Any]] = queue.Queue()
        self._raw: list[str] = []
        self._buffer = ""
        self._stop = threading.Event()
        self._client = httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout, read=timeout))
        # 注意：`Client.stream()` 返回的是**上下文管理器**，真正的响应要靠 `__enter__()` 取出来
        self._stream = self._client.stream("GET", f"{self.base_url}{path}")
        self._response = self._stream.__enter__()
        self._reader = threading.Thread(target=self._pump, name="sse-reader", daemon=True)
        self._reader.start()

    # -- 读 ---------------------------------------------------------------

    def _pump(self) -> None:
        try:
            for chunk in self._response.iter_text():
                if self._stop.is_set():
                    break
                self._buffer += chunk
                while True:
                    raw, separator, rest = _split_frame(self._buffer)
                    if not separator:
                        break
                    self._buffer = rest
                    if raw.strip():
                        self._raw.append(raw)
                        self._frames.put(parse_sse_frame(raw))
        except Exception:  # pragma: no cover - 连接被关时读线程退出即可
            pass

    @property
    def raw_text(self) -> str:
        return "\n\n".join(self._raw)

    def read_frame(self, *, timeout: float) -> dict[str, Any]:
        """读一帧（到超时抛 `AssertionError`，不留无上限等待）。"""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"{timeout}s 内没有读到 SSE 帧（已读到 {len(self._raw)} 帧；"
                f"原文尾部：{self.raw_text[-500:]!r}）"
            ) from None

    def read_frames(self, count: int, *, timeout: float) -> list[dict[str, Any]]:
        """连读 `count` 帧（每一帧各自用同一个 `timeout` 兜住）。"""
        return [self.read_frame(timeout=timeout) for _ in range(int(count))]

    def read_until(self, name: str, *, timeout: float) -> dict[str, Any]:
        """读到某个 `event:` 名为止（读到的其它帧丢弃）。"""
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{timeout}s 内没有读到 event: {name}")
            frame = self.read_frame(timeout=remaining)
            if frame.get("event") == name:
                return frame

    def read_event_frame(self, *, timeout: float) -> dict[str, Any]:
        """读**第一条带 `event:` 的帧**（`retry:` 与 `: ping` 这两类先跳过）。

        SSE 流开头那一条 `retry:` 是重连间隔的控制指令，它只带一个字段；契约说的
        「第一帧必须是 `snapshot`」指的是**第一条事件帧**。
        """
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{timeout}s 内没有读到带 event: 的帧")
            frame = self.read_frame(timeout=remaining)
            if frame.get("event"):
                return frame

    def frames(self) -> list[dict[str, Any]]:
        """已经读到的全部帧（不等待）。"""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._frames.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self._stop.set()
        try:
            self._stream.__exit__(None, None, None)
        except Exception:  # pragma: no cover
            pass
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "SseClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _resolve_base_url(target: Any) -> str:
    """`base_url` 可以给：字符串 / `ServiceProcess` / Starlette `TestClient`。"""
    if isinstance(target, str):
        return target.rstrip("/")
    for attr in ("base_url",):
        value = getattr(target, attr, None)
        if isinstance(value, str):
            return value.rstrip("/")
        if value is not None and hasattr(value, "scheme"):
            return f"{value.scheme}://{value.netloc}".rstrip("/")
    raise TypeError(f"认不出 base_url：{target!r}")


def read_sse_frames(client: Any, count: int = 1, *, timeout: float) -> list[dict[str, Any]]:
    """按计数连读若干帧（`timeout` 必传）。"""
    if isinstance(client, SseClient):
        return client.read_frames(count, timeout=timeout)
    return [parse_sse_frame(item) for item in _read_via_httpx(client, count, timeout)]


def _read_via_httpx(base_url: str, count: int, timeout: float) -> list[str]:  # pragma: no cover
    """给「直接给一个 URL 字符串」的调用点用的兜底读法。"""
    out: list[str] = []
    buffer = ""
    with httpx.stream("GET", f"{_resolve_base_url(base_url)}/api/events",
                      timeout=httpx.Timeout(timeout, read=timeout), trust_env=False) as response:
        for chunk in response.iter_text():
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                if raw.strip():
                    out.append(raw)
                    if len(out) >= count:
                        return out
    return out


# ---------------------------------------------------------------------------
# 动作 / 会话文件 / 配置
# ---------------------------------------------------------------------------


def post_action(base_url: Any, action: str, args: dict[str, Any] | None = None, **kwargs: Any):
    """打一次 `POST /api/actions`（**两个必需请求头由这里带全**，测试不自己拼头）。"""
    body: dict[str, Any] = {"action": action}
    if args is not None:
        body["args"] = args
    headers = {"Content-Type": "application/json", "X-Meeting-Service": "1"}
    headers.update(kwargs.pop("headers", {}) or {})
    url = f"{_resolve_base_url(base_url)}/api/actions"
    timeout = kwargs.pop("timeout", 10.0)
    return httpx.post(url, json=body, headers=headers, timeout=timeout, trust_env=False, **kwargs)


def dev_inject(
    base_url: Any,
    *,
    source: str,
    fault: str,
    after_seconds: float = 0.0,
    duration_seconds: float | None = None,
    **kwargs: Any,
):
    """`dev_inject` 动作：**透传 `after_seconds` 与 `duration_seconds`**（「注入 → 恢复」可写）。"""
    args: dict[str, Any] = {"source": source, "fault": fault, "after_seconds": after_seconds}
    if duration_seconds is not None:
        args["duration_seconds"] = duration_seconds
    return post_action(base_url, "dev_inject", args, **kwargs)


def read_session_json(session_dir: Path | str) -> dict[str, Any] | None:
    """读一个场次目录里的 `session.json`（读不出来返回 `None`）。"""
    path = Path(session_dir) / "session.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_config_json(home: Path | str, values: dict[str, Any]) -> Path:
    """把「点号扁平键名 → 值」的映射写进 `<家目录>/.config/meeting/config.json`。

    键形与 `settings_update` 的 `args` **逐字同形**（只有一层点号扁平键名，不写嵌套）。
    """
    return helpers.write_config_json(Path(home), values)
