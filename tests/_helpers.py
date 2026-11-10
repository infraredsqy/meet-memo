"""测试层的共用辅助模块（**测试支持模块**，phase1 §4.3）。

**定位**：本文件是测试支持模块 —— 文件名不以 `test_` 开头，pytest **不收集**它，
它只在四个测试文件里被 `import _helpers` 使用；它也不是 `conftest.py`，因此**不提供 fixture**
（fixture 写在各自用到它的测试文件里）。phase2 新增 `tests/conftest.py` / `tests/_service.py`
时**按需吸收**这里的辅助（helper 与它们职责重叠的部分随那时一起收拢），本文件不预先替它们
定义任何东西。

本 Phase 的契约冻结项 24 把测试文件清单定死为四个（`test_config.py` / `test_logging_setup.py`
/ `test_port_policy.py` / `test_static.py`），且 `tests/conftest.py` 属于 phase2（phase1 明写
「不创建 `tests/conftest.py`」）。

测试写的规矩（§4.3，全部来自契约）：
  - 一律**同步**写；ASGI 层用 Starlette 的 `TestClient`，进程层用**真服务子进程 + 同步 httpx**；
    不引 `pytest-asyncio`。
  - 子进程启动命令只有一种形态：`[sys.executable, "-m", "meeting", <动作词>]`，`cwd` 显式给仓库根。
  - 子进程 env 从「**剔除全部本服务环境变量后的宿主环境副本**」出发，
    `MEETING_NO_BROWSER` 是本 Phase 的用例唯一允许出现的本服务环境变量。
  - **绝不许有任何一条用例在测试里触达真浏览器**（一次事故的收口）：验「打开浏览器失败只警告」
    那一支时，用 `fake_browser_module()` 把 `tests/_fake_webbrowser/webbrowser.py` 经 `PYTHONPATH`
    前置注入子进程，子进程里的 `webbrowser.open()` 因此是那份假替身；曾用过的
    `BROWSER=no-such-browser` 在 Windows 上会退到 `windows-default`（用户默认浏览器），
    已经废掉，**不要再写回来**。文件的位置安排见 `preserve_pythonpath()` 上的说明。
  - 配置与日志的隔离一律靠**重定向家目录**（`HOME` / `USERPROFILE`），不靠环境变量换路径。
  - **本服务的环境变量清单不手抄**：`SERVICE_ENV_NAMES` 由 `meeting.config.ENV_KEYS` 的值
    加 `ENV_NO_BROWSER` 派生（`meeting.config` 加键时这里自动跟上）。
  - **不允许任何用例直接调 `webbrowser.open(...)`**：进程内用例必须自己替换
    `meeting.service.app.webbrowser.open`，子进程一律走 `fake_browser_module()` 注入；
    pytest 父进程**没有**全局兜底（`tests/conftest.py` 归 phase2）。这条风险与将来的落点
    在 `app_client()` 的 docstring 里写得更细，写新用例前先读那一段。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from meeting import config as config_module

#: 仓库根（`tests/` 的上一级；所有子进程的 cwd 都给它）
REPO_ROOT = Path(__file__).resolve().parent.parent

#: 假 `webbrowser` 模块的落点（`PYTHONPATH` 前置注入子进程时指向这一层）
FAKE_BROWSER_MODULE_DIR = Path(__file__).resolve().parent / "_fake_webbrowser"

#: 指挥假 `webbrowser` 的四个变量名（模块里读、测试层写；都不是本服务的环境变量）
FAKE_BROWSER_PATH_ENV = "MEETING_TEST_FAKE_BROWSER_PATH"
FAKE_BROWSER_CALLS_ENV = "MEETING_TEST_FAKE_BROWSER_CALLS"
FAKE_BROWSER_OUTCOME_ENV = "MEETING_TEST_FAKE_BROWSER_OUTCOME"
FAKE_BROWSER_MARKER_ENV = "MEETING_TEST_FAKE_BROWSER_MARKER"

#: 记录 `PYTHONPATH` 的初始取值（`PYTHONPATH` 是宿主变量，与 `MEETING_*` 清单无关）
PYTHONPATH_BASELINE = os.environ.get("PYTHONPATH")

#: 本服务的全部环境变量（子进程 env 一律先剔除它们）
#:
#: **从 `meeting.config` 的公开常量派生**（`ENV_KEYS` 的**键**加 `ENV_NO_BROWSER`
#: 与 `ENV_CONFIG_DIR` 这两个 env-only 变量——后者设了会把 `config_dir()` 整组
#: 拐走，子进程用例的家目录重定向因此必须剔它），不再手抄一份：将来给 `ENV_KEYS`
#: 加键时这里自动跟上，不会漏剔（手抄的副本一旦漏掉新键，宿主设过同名变量就会
#: 让用例红得莫名其妙）。派生结果由 `tests/test_config.py` 里那一条 pin 住。
SERVICE_ENV_NAMES = (
    tuple(config_module.ENV_KEYS)
    + (config_module.ENV_NO_BROWSER, config_module.ENV_CONFIG_DIR)
)

#: 子进程启动命令的动作词（R15 的两个词）
ACTION_WEB = "web"
ACTION_POST = "post"

#: **收场纪律里的那个宽限秒数**（契约 §3.8 文末「测试侧常量」表：`PROCESS_TERMINATE_GRACE_SECONDS` = 5）。
#:
#: 收场动作写死为「先 `terminate()` → 等 grace 秒 → 仍在则 `kill()`」（§5.2 的收场纪律）；
#: 这里把它落成具名常量，`tests/_service.py` 的 `ServiceProcess` 从本模块取同一个名字 ——
#: 两处收场逻辑因此共用一份取值，谁都不会再写一个来路不明的裸秒数。
PROCESS_TERMINATE_GRACE_SECONDS = 5

#: test extra 里的同步 httpx（只服务测试层）
import httpx  # noqa: E402

_httpx = httpx

#: `local_get()` / `local_post()` 的连接对象缓存（见那两个函数的说明：本机构造一次
#: `httpx.Client` 要 0.83 秒）。键是请求 URL，值形如 `{"client": ..., "max_timeout": ...}`。
_LOCAL_GET_CLIENTS: dict[str, Any] = {}
_LOCAL_GET_LOCK = threading.Lock()

#: 所有本机探针**共用**的 SSL 上下文（本机实测的那 0.83 秒就花在它身上）。
#:
#: `httpx.Client()` 每次都会自己 `ssl.create_default_context()`，本机上一次 0.83 秒；
#: 建好一个传进 `verify=` 之后，`httpx.Client()` 只要 0.027 秒（第二次起 0.000 秒）。
#: 本项目的探针一律走 `http://127.0.0.1:<port>`（**明文回环**），TLS 装配对请求结果毫无影响，
#: 但用例为此省下的时间很实在：每一条起服务的用例都要问一次 `/api/state` 与一次动作请求。
_LOCAL_SSL_CONTEXT = ssl.create_default_context()


#: 建 client 时认的参数（其余关键字参数一律当作**这一次请求**的参数交给 httpx，
#: 例如 `headers` / `params`）—— `httpx.get()` 与 `httpx.Client.get()` 的分工就是这个形状
_CLIENT_KWARGS = ("timeout", "follow_redirects", "verify")


def _local_client(url: str, kwargs: dict) -> "httpx.Client":
    """按 `url` 取一个**复用**的本机 client（见 `local_get()` 的说明）。

    **键只有 url**（不带超时）：GET 探针与 POST 动作指向同一个服务、同一个回环地址，
    它们可以共用同一条连接。键里带超时的话，`local_get(timeout=5)` 与
    `local_post(timeout=10)` 会各建一个 client —— 每一次 0.83 秒的构造代价就付两遍，
    而下面这两类调用在每个用例里都是第一次就撞上它。

    超时按这个规则来：同一 url 上取**已见的最大值**（`max_timeout`），每次请求再按自己的
    调用点覆盖一次（`client.get(url, timeout=...)` / `client.post(..., timeout=...)`）。
    于是「谁的超时更大」既有上界、又不会让某个调用点悄悄用上比它自己要的更短的超时。
    `timeout=None` 表示「不设上限」（SSE 这一类长读用它）。
    """
    want = kwargs.get("timeout", 5.0)
    with _LOCAL_GET_LOCK:
        entry = _LOCAL_GET_CLIENTS.get(url)
        if entry is None:
            entry = {
                "client": _httpx.Client(
                    trust_env=False,
                    timeout=_httpx.Timeout(want),
                    verify=_LOCAL_SSL_CONTEXT,
                ),
                # `None`（不设上限）在「取最大值」这件事上就是无穷大，用 0.0 登记
                "max_timeout": 0.0 if want is None else float(want),
            }
            _LOCAL_GET_CLIENTS[url] = entry
        if want is None:
            entry["client"].timeout = _httpx.Timeout(None)
            entry["max_timeout"] = 0.0
        else:
            entry["max_timeout"] = max(float(want), float(entry["max_timeout"]))
            entry["client"].timeout = _httpx.Timeout(entry["max_timeout"])
        return entry["client"]


def _request_kwargs(kwargs: dict) -> dict:
    """把调用点给的关键字参数分成「建 client 的那几个」与「这一次请求的那几个」。"""
    return {k: v for k, v in kwargs.items() if k not in _CLIENT_KWARGS}


def _drop_local_client(url: str, kwargs: dict) -> None:
    """把某个 url 下缓存的 client 关掉并移出缓存（下一次取会新建一条干净连接）。

    用在 `local_get()` / `local_post()` 的重连那一支：连接池里那条连到「上一个子进程」的
    旧连接会一直被认为是活的（对端进程已收场、端口已关），重试时又拿到它，于是同样失败。
    这里把它扔掉，重试因此真的换一条新连接。
    """
    del kwargs
    with _LOCAL_GET_LOCK:
        entry = _LOCAL_GET_CLIENTS.pop(url, None)
    if entry is not None:
        with contextlib.suppress(Exception):
            entry["client"].close()


def local_get(url: str, *, client_kwargs: dict | None = None, **kwargs) -> "httpx.Response":
    """问本机服务的一次同步 GET。

    **一律 `trust_env=False`**：本机（Windows）可能配着系统级代理，httpx 会去读它，
    于是发往 `127.0.0.1` 的请求被塞进代理（显式给了 `Host` 头时尤其明显），
    拿回来的是一个与本服务无关的 `502`。测试要问的是本服务，就不走代理。

    **连接对象按 url 复用**：本机实测 `httpx.Client` 的构造要 0.83 秒（Windows 上 SSL
    上下文的初始化），而同一个连接上的一次请求只要 0.001 秒 —— 每次探针都新建 client，
    会让「轮询 `/api/state`」这类断言每一次读都白花 0.8 秒，收尾窗口、SSE 就绪、告警等待
    全都跟着被拖长。同一个 url 上的 GET 与 POST 共用一条连接（见 `_local_client()` 的说明），
    于是每一条用例只付一次构造代价。缓存是**进程级**的，进程退出自然收场。

    **参数分工与 `httpx.get()` 一致**：`timeout` / `follow_redirects` / `verify` 这几个
    （或整份 `client_kwargs=`）归 client，其余关键字参数（`headers` / `params` …）
    **原样交给这一次请求** —— 漏掉后半句会让「带 `Host: evil.example` 的请求得 403」
    这类用例静默失效（请求照发、头没带上、拿到的是 200）。

    **池里那条连到「上一个子进程」的旧连接**：用例顺序重启（旧进程已收场、同一输出根再起
    一个）之后，池里那条连接指向的是一个已经不在的端口，第一次读会拿到 `ConnectError`
    （[WinError 10061]）。这不是被测行为：这里把那个 client 扔掉、换一条干净连接重试一次。
    只重试这一种错误 —— 服务真的没起来时，第二次同样是它，照旧抛出去。
    """
    merged = dict(client_kwargs or {})
    merged.update({k: v for k, v in kwargs.items() if k in _CLIENT_KWARGS})
    merged.setdefault("timeout", 5.0)
    request_kwargs = _request_kwargs(kwargs)
    client = _local_client(url, merged)
    try:
        return client.get(url, **request_kwargs)
    except _httpx.ConnectError:
        _drop_local_client(url, merged)
        return _local_client(url, merged).get(url, **request_kwargs)


def local_post(url: str, *, client_kwargs: dict | None = None, **kwargs) -> "httpx.Response":
    """问本机服务的一次同步 POST（与 `local_get()` 同一套复用、去代理与重连口径）。"""
    merged = dict(client_kwargs or {})
    merged.update({k: v for k, v in kwargs.items() if k in _CLIENT_KWARGS})
    merged.setdefault("timeout", 10.0)
    request_kwargs = _request_kwargs(kwargs)
    client = _local_client(url, merged)
    try:
        return client.post(url, **request_kwargs)
    except _httpx.ConnectError:
        _drop_local_client(url, merged)
        return _local_client(url, merged).post(url, **request_kwargs)


def local_client_for(url: str) -> "httpx.Client":
    """取本机探针共用的那个 client（用例要自己 `.stream(...)` 时用它，别再新建一个）。

    新建一个 `httpx.Client` 在本机要 0.83 秒（见 `local_get()` 的说明），SSE 这一类
    「同一条连接上开一次流」的读法同样不该为它买单。client 级不设超时：这一次流的时限由
    调用点在 `.stream(..., timeout=...)` 上自己给。
    """
    return _local_client(url, {"timeout": None})


def local_ssl_context() -> "ssl.SSLContext":
    """本机探针**共用**的那个 SSL 上下文（`local_get()` 那一份，见它的说明）。

    用例自己新建 `httpx.Client`（例如 SSE 的流式读法要按条持有自己的连接）时把它传进
    `verify=`，构造代价就从 0.83 秒落到 0.03 秒 —— TLS 装配那笔钱在明文回环上对请求结果
    毫无影响，谁都不必为它反复买单。上下文是进程级共享的只读对象，传出去不会被改动。
    """
    return _LOCAL_SSL_CONTEXT


# ---------------------------------------------------------------------------
# 家目录 / 配置
# ---------------------------------------------------------------------------


def service_env(home: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程 env：剔除全部本服务变量后的宿主环境副本 + 家目录重定向 + 本用例显式需要项。"""
    env = {key: value for key, value in os.environ.items() if key not in SERVICE_ENV_NAMES}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def config_dir_of(home: Path) -> Path:
    return Path(home) / ".config" / "meeting"


def config_path_of(home: Path) -> Path:
    return config_dir_of(home) / "config.json"


def log_dir_of(home: Path) -> Path:
    return config_dir_of(home) / "logs"


def service_log_path_of(home: Path) -> Path:
    return log_dir_of(home) / "meeting.log"


def write_config_json(home: Path, values: dict[str, Any]) -> Path:
    """把点号扁平的生效值写进 `<临时家目录>/.config/meeting/config.json`。"""
    path = config_path_of(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return path


def allocate_start_dir(root: Path) -> str:
    """测试专用：在 `root` 之下**预建**一个工作区目录并返回它的路径。

    产品代码不再建目录（落点由 start 的 `args.dir` 显式给、且必须是已存在的目录），
    这里替**测试层**把目录建出来：按 `ws1` / `ws2`… 递增（撞到已存在的就让位）。
    `root` 本身是各用例夹具给的放置根（`meeting_env.out_root` 这一个名字沿用），
    它不再有任何「输出根」的配置语义。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        candidate = root / f"ws{index}"
        try:
            candidate.mkdir()
            return str(candidate)
        except FileExistsError:
            index += 1


def start_args(action: str, args: dict[str, Any] | None, root: Path) -> dict[str, Any]:
    """测试层给 `start` 动作补缺省落点：`args` 里没显式给 `dir` 时由 `root` 预建一个。"""
    merged = dict(args or {})
    if action == "start":
        merged.setdefault("dir", allocate_start_dir(root))
    return merged


#: **测试层给 `capture.backend` 的共用缺省值**（`dev`：项目既有的开发替身）。
#:
#: 为什么必须有一个缺省：`capture.backend` 的产品缺省是 `auto`，它解析到
#: `meeting.backends.capture:CaptureBackend` —— 那个后端在 `probe()` 里真的枚举声卡端点、
#: 真的以 16k 试开一遍并读回样本（`meeting/capture/sources.py`）。换句话说，**一条忘了写这
#: 个键的用例，会在服务 startup 那一步走到真实采集入口上**：有声卡的机器上就是打开机主的
#: 输入设备（隐私硬约束，任务书 §0.1），无声卡的机器上则是另一条路径（用例的结论随「这台
#: 机器有没有声卡」而变）。测试层的共用辅助因此统一把缺省值收成 `dev` 替身：它不碰任何设备，
#: 两路如实报 `ready` + `dev-<source>`。
#:
#: **需要别的档的用例自己显式传**：要 `unprobed` 那一态的快照就传 `null`（或经
#: `helpers.write_config_json()` 自己写一份），要验「`auto` 解析到真实现」就显式写 `auto`
#: （`tests/test_capture_probe.py` 那几条走的就是这一档）。`auto` 因此**只留在明确要验它的
#: 地方**，不再是一条用例「什么都没写」时的意外落点。
DEFAULT_CAPTURE_BACKEND = "dev"

#: **会后钩子在测试层的共用缺省**：`null`（缺席档）。
#:
#: 产品缺省是 `auto`：它会解析到真实现（`meeting/backends/post.py`），收尾第 4 步于是走到
#: 「解析到真实现」那一档。R26 之后那一格**不拉起任何会后动作**——做完释放会中模型就停在
#: `waiting`，不起线程、不往场次目录落产物；测试层仍把缺省收成 `null`，为的是「缺席」这半边
#: 的确定性：缺席档第 4 步直接记 `skipped`，数收尾编排、采集归档或服务状态记录的用例因此
#: 钉死在这一档上，不必跟「解析到真实现」那一档的面孔搅在一起。
#:
#: **要验真实现那一档的用例显式传 `post.hook = "auto"`**（`tests/test_post_hook.py` 走的就是这一档）。
DEFAULT_POST_HOOK = "null"

#: **纪要钩子在测试层的共用缺省**：`null`（缺席档）。
#:
#: 与 `DEFAULT_POST_HOOK` 同一个理由，而且这一档更要紧：本机 PATH 上真的有 `dsh`
#: （`%APPDATA%\npm\dsh.cmd`），产品缺省 `auto` 会让每一场跑完整条离线链的
#: 用例都**真的拉起一个 headless DSH 子会话**去跑纪要（那是一次真模型调用，几十分钟的
#: 墙钟、还占着显卡），而用例只想知道离线那三份产物长什么样。
#: 要验钩子本身的用例显式给 `minutes.hook = "auto"` 并注入假启动器
#: （`tests/test_minutes_runner.py` 走的就是那一档）。
DEFAULT_MINUTES_HOOK = "null"

#: 测试层补缺省的键 → 值（只补缺，不覆盖用例自己写的值）
DEFAULT_BACKEND_VALUES: dict[str, str] = {
    "capture.backend": DEFAULT_CAPTURE_BACKEND,
    "post.hook": DEFAULT_POST_HOOK,
    "minutes.hook": DEFAULT_MINUTES_HOOK,
}


def apply_backend_defaults(home: Path) -> Path | None:
    """把 `DEFAULT_BACKEND_VALUES` 里缺的那几项填进本用例的 `config.json`
    （**只补缺、不覆盖已有的值**）。

    子进程那条路上的辅助（`spawn_service()` / `ServiceProcess`）不能像 `app_client()` 那样
    「先把值拼好再写盘」—— 用例可能已经自己写了一份 `config.json`（`write_config_json()`），
    也可能故意把它写坏（非法 JSON / 同名目录）让服务拒绝启动。因此这里只做一件事：
    **文件存在且是合法 JSON 且已经有那个键就一个字节都不动**；缺哪一项就补哪一项，
    文件不存在就建一份只有这几项的。写不进去（同名目录、权限）时**不抛错也不改**：那正是
    用例要的现场，让被测代码照实报错。

    返回被写入的路径；什么都没写（都已有值 / 写不进去）时返回 `None`。
    """
    path = config_path_of(home)
    if path.exists():
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # 读不动 / 不是合法 JSON：那是用例自己的现场，别去动它
        if not isinstance(values, dict):
            return None
    else:
        values = {}
    missing = {
        key: value for key, value in DEFAULT_BACKEND_VALUES.items() if key not in values
    }
    if not missing:
        return None
    values.update(missing)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return None
    return path


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.05, what: str = "条件"):
    """同步等待一个条件成立（`predicate` 返回真值即返回它；测试一律同步写，不用事件循环）。

    **全仓只有这一份 `wait_until()`**（§2.2 / §3.8：等待助手只有这一个名字，`wait_for` 这个
    旧名全仓作废）；原先散在 `tests/_service.py` 与各 `test_*.py` 里的副本已并到这里，
    用法一律 `from tests._helpers import wait_until`。签名沿用本模块原签名。

    **异常留在这里、只当诊断用**（这是本助手最容易走歪的一格；语义取自 `tests/_service.py`
    修好的那份）：谓词抛出的异常**一律不当成返回值**。它若被摆进「最后观察值」，而异常本身
    就是真值 —— 一次**瞬时**的读故障（本机实测过
    `httpx.ReadError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。` 与
    `httpx.ReadTimeout: timed out`）就会让 `wait_until()` 当场把它**当成「等到了」交回去**，
    调用点接着去取字段，炸成一句与现场无关的
    `TypeError: string indices must be integers, not 'str'`。
    现在的语义：窗口内探针报错就地重试（探针还没就绪是常态），故障被下一次读绕过去、用例
    照常过；窗口内一直读不到 → 那条报错作为诊断带进超时报告，红的时候手上有一句能读的现场。
    """
    deadline = time.monotonic() + float(timeout)
    last_failure: BaseException | None = None
    observed: Any = None
    while True:
        try:
            observed = predicate()
        except Exception as exc:  # 探针自身报错时继续等（不把它当成「等到了」）
            last_failure = exc
            observed = None
        if observed:
            return observed
        if time.monotonic() >= deadline:
            detail = f"等待超时（{timeout}s）：{what}（最后观察值 {observed!r}）"
            if last_failure is not None:
                detail += (
                    f"；窗口内探针最后一次报错：{type(last_failure).__name__}: {last_failure}"
                )
            raise AssertionError(detail)
        time.sleep(interval)


def wait_for_session_record(session_dir: Path, predicate, timeout: float = 20.0) -> dict:
    """等到盘上那份 `session.json` 满足 `predicate`（返回它解析出来的那一版）。

    用途：启动扫描的改写、收尾的逐步落盘都是**服务进程**在做，`wait_ready()` 只保证服务起来了，
    不保证盘上已经写到哪一版；直接读文件会撞上「读到早一版」的偶发红。
    """
    path = Path(session_dir) / "session.json"

    def loaded():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and predicate(data) else None

    return wait_until(loaded, timeout=timeout, what=f"{path} 落到期望的那一版")


# ---------------------------------------------------------------------------
# 真服务子进程
# ---------------------------------------------------------------------------


class ServiceProcess:
    """一个真服务子进程（`[sys.executable, "-m", "meeting", <动作词>]`，cwd = 仓库根）。

    子进程的前台服务不会自己退出，所以 stdout / stderr 落到**临时文件**里，
    测试可以边跑边读（就绪判定要读终端输出与 log）。
    """

    def __init__(self, proc: subprocess.Popen, home: Path, out_file, err_file) -> None:
        self.proc = proc
        self.home = Path(home)
        self.log_path = service_log_path_of(home)
        self._out_path = Path(out_file.name)
        self._err_path = Path(err_file.name)
        self._out_file = out_file
        self._err_file = err_file

    # -- 读数 -------------------------------------------------------------

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self):
        return self.proc.poll()

    def stdout(self) -> str:
        self._out_file.flush()
        return read_text(self._out_path)

    def stderr(self) -> str:
        self._err_file.flush()
        return read_text(self._err_path)

    def log_text(self) -> str:
        return read_text(self.log_path)

    def port(self, timeout: float = 20.0) -> int:
        """从终端输出的 `URL：http://127.0.0.1:<port>` 读出选中端口。"""
        def parsed():
            for line in self.stdout().splitlines():
                if line.startswith("URL："):
                    return int(line.rsplit(":", 1)[1].strip())
            return None

        return int(wait_until(parsed, timeout=timeout, what="终端打印 URL"))

    def wait_ready(self, timeout: float = 20.0) -> None:
        """就绪判定 = 服务级主 log 里出现信号接管自检通过那一行（§3.5 第 6 步）。"""
        wait_until(
            lambda: "信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数" in self.log_text(),
            timeout=timeout,
            what="信号接管自检通过行",
        )

    def wait_ready_by_heartbeat(self, timeout: float = 20.0) -> int:
        """另一条就绪判定：用选中的端口真的问一次 `GET /api/heartbeat`。"""
        port = self.port(timeout=timeout)

        def ok():
            try:
                response = local_get(f"http://127.0.0.1:{port}/api/heartbeat", timeout=1.0)
            except Exception:
                return None
            return response if response.status_code == 200 else None

        wait_until(ok, timeout=timeout, what="GET /api/heartbeat 200")
        return port

    # -- 结束 -------------------------------------------------------------

    def send_break(self) -> None:
        """发 `CTRL_BREAK_EVENT`（对应 `SIGBREAK`）。"""
        os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)

    def wait(self, timeout: float = 20.0) -> tuple[str, str]:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"服务子进程没有在 {timeout}s 内退出")
        return self.stdout(), self.stderr()

    def terminate(self) -> None:
        """兜底清理：先 `terminate`、等 `PROCESS_TERMINATE_GRACE_SECONDS` 秒、仍在则 `kill`。

        只在测试的 `finally` 里用（收场纪律照 §5.2：终止 → 等 grace → 仍活着才硬杀）。
        """
        if self.proc.poll() is None:
            with contextlib.suppress(OSError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - terminate 无效时
                self.proc.kill()
                self.proc.wait()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._out_file.close()
        with contextlib.suppress(OSError):
            self._err_file.close()


def spawn_service(
    home: Path,
    action: str = ACTION_WEB,
    *,
    env_extra: dict[str, str] | None = None,
) -> ServiceProcess:
    """起一个真服务子进程（默认带 `MEETING_NO_BROWSER=1`，本 Phase 唯一允许的服务变量）。

    **后端在这里补上共用缺省**（`apply_backend_defaults(home)`）：本用例的 `config.json`
    里没写 `capture.backend` 时补 `dev` 替身（不补的话产品缺省 `auto` 会让子进程 startup
    里的 `probe()` 去真枚举、真试开声卡设备），没写 `post.hook` 时补 `null`（缺席档：收尾
    第 4 步记 `skipped`；R26 之后真实现那一档也不自动跑会后）。两处的说明分别见
    `DEFAULT_CAPTURE_BACKEND` / `DEFAULT_POST_HOOK`。用例自己写过的值一个字节都不动。

    子进程要走到「打开浏览器」这一步（`MEETING_NO_BROWSER` 被显式拿掉）时，**必须**同时把
    `fake_browser_module()` 给的环境变量传进来，让子进程的 `webbrowser` 是那份假替身 ——
    这一步的看门断言就在这里，漏了就直接把用例判红，不留「悄悄开真浏览器」的口子。
    """
    apply_backend_defaults(Path(home))
    extra = {"MEETING_NO_BROWSER": "1"}
    if env_extra:
        extra.update(env_extra)
    if extra.get("MEETING_NO_BROWSER", "") != "1" and not extra.get(FAKE_BROWSER_PATH_ENV):
        raise AssertionError(
            "子进程要走到「打开浏览器」这一步，就必须经 fake_browser_module() 注入假 "
            "webbrowser（env_extra 里带 MEETING_TEST_FAKE_BROWSER_PATH）：否则这一步会落到"
            "宿主的默认浏览器上，测试会打开真窗口。"
        )
    out_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".out", delete=False, encoding="utf-8"
    )
    err_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".err", delete=False, encoding="utf-8"
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "meeting", action],
            cwd=str(REPO_ROOT),
            env=service_env(home, extra=extra),
            stdout=out_file,
            stderr=err_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except BaseException:
        out_file.close()
        err_file.close()
        raise
    return ServiceProcess(proc, home, out_file, err_file)


def run_cli(
    home: Path,
    action: str | None = ACTION_WEB,
    *,
    env_extra: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    """跑一次 CLI 并等它自己结束（用法错误、`post` 桩、启动前提不成立这些快路径）。

    **后端同样在这里补共用缺省**（与 `spawn_service()` 同源）：`web` 这一档在
    「启动前提不成立」之外的现场会真的起服务，那时缺省 `auto` 一样会走到真实采集入口上，
    缺省 `post.hook` 一样把收尾第 4 步钉在缺席档（记 `skipped`；R26 之后真实现那一档
    也不自动跑会后）。
    只补缺、不动用例自己写的值（含用例故意写坏的那份配置）。
    """
    apply_backend_defaults(Path(home))
    args = [sys.executable, "-m", "meeting"]
    if action is not None:
        args.append(action)
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        env=service_env(home, extra=env_extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_web_expecting_failure(home: Path, *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """跑 `meeting web`，用于「它会拒绝启动」这类用例（正常情况它自己会退出）。"""
    return run_cli(home, ACTION_WEB, timeout=timeout)


# ---------------------------------------------------------------------------
# ASGI 层（`with TestClient(...)` 才触发应用的生命周期钩子）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def app_client(
    home: Path, *, values: dict[str, Any] | None = None, browser: str = "safe"
) -> Iterator[Any]:
    """装配好的 ASGI 客户端：家目录重定向到 `home`，`base_url` 带上回环 Host。

    **必须走上下文管理器形式** —— 只有 `with` 才会触发应用的生命周期钩子，
    `ServiceRuntime.startup()` 与快照的服务信息字段都在那一步落定。

    **采集后端有一个共用缺省**（`DEFAULT_CAPTURE_BACKEND = "dev"`）：`values` 里没写
    `capture.backend` 时，这里补 `dev` 替身再落盘。不补的话产品缺省 `auto` 会解析到真实现，
    startup 里的 `probe()` 就去真枚举、真试开声卡设备了（见 `DEFAULT_CAPTURE_BACKEND` 的
    说明）。要 `null` / `auto` / 别的档的用例**自己显式传**，那时以 `values` 为准。

    **会后钩子同样有一个共用缺省**（`DEFAULT_POST_HOOK = "null"`）：`values` 里没写
    `post.hook` 时补缺席档。不补的话收尾第 4 步会解析到真实现那一档（R26 之后那一格停在
    `waiting`、不自动跑会后；数收尾编排记录的用例要的是缺席档记 `skipped` 的确定性，
    见 `DEFAULT_POST_HOOK` 的说明）。要验真实现那一档的用例显式传 `auto`。

    **这一步里绝不许触达真浏览器**：应用一进 startup 钩子，尾部的「打开浏览器」就会跑。
    夹具因此在构造应用之前把 `service_app.webbrowser.open` 换成本进程内的假实现，离开时还原。
    假实现与真浏览器无关：它只把收到的 URL 追加进 `browser_calls`，并按 `browser` 决定的
    行为返回/抛错（`safe` = 返回 `False`，`open` = 返回 `True`，`raise` = 抛 `RuntimeError`）。
    用例读结果走 `client.browser_calls` / `client.browser_calls.read_calls()`。

    ============================================================================
    ⚠ **进程内触达「打开浏览器」这一步的真实风险（下一个人先读这段）**

    pytest **父进程**里 `meeting.service.app.webbrowser` 就是**标准库那个模块对象**：
    谁在 `app_client()` 之外直接让 `launch_browser()` 跑起来（或让 startup 钩子跑到它），
    而**没有**先把 `webbrowser.open` 换掉，那一下就会落到宿主机器的默认浏览器上 ——
    **真会弹出窗口**（本项目已经因此误触过一次用户的 Chrome，标准库那条路上
    `BROWSER=no-such-browser` 在 Windows 上无效：`register_standard_browsers()` 会补上
    `windows-default` 并真的开窗，见 `tests/_fake_webbrowser/webbrowser.py` 的说明）。

    因此本 Phase 的硬要求（**任何用例都不得例外**）：
      - 进程内：用例**自己**在调用前把 `meeting.service.app.webbrowser.open` 换成抛错型或
        记录型假替身（`monkeypatch.setattr(service_app.webbrowser, "open", ...)`，
        参考 `tests/test_static.py` 里 `launch_browser()` 那几条直接断言）；
      - 子进程：**必须**经 `fake_browser_module()` + `fake_browser_env()` 把
        `tests/_fake_webbrowser/webbrowser.py` 用 `PYTHONPATH` 前置注入（`spawn_service()`
        里的看门断言会拦下漏注入的用例）；
      - **绝对不许**直接调用 `webbrowser.open(...)`，也不许拿 `BROWSER` 环境变量当唯一保护。

    **硬保护的落点（phase2 的活，本 Phase 不做）**：phase2 新增 `tests/conftest.py` 时，
    应当在那里装一个 **autouse fixture**，默认把 `meeting.service.app.webbrowser.open`
    换成「抛错 / 只记录」的假替身（需要真行为的用例再在自己的用例里显式覆盖它）。
    本 Phase 契约明令不创建 `tests/conftest.py`（§1.2 / §4.1），因此这条保护**此刻只在
    各用例与 `app_client()` 里各自落地**，没有全局兜底 —— 写新用例时别指望有人替你挡住。
    ============================================================================
    """
    from starlette.testclient import TestClient

    from meeting import config as config_module
    from meeting.service import app as service_app

    home = Path(home)
    effective = dict(values or {})
    # 共用缺省：没写就收成测试层的那两档（都不产生副作用，见两个常量的说明）
    effective.setdefault("capture.backend", DEFAULT_CAPTURE_BACKEND)
    effective.setdefault("post.hook", DEFAULT_POST_HOOK)
    write_config_json(home, effective)
    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    old_config = config_module.current()
    old_handlers = _swap_logging(home)

    prev_int = signal.getsignal(signal.SIGINT)
    prev_break = signal.getsignal(getattr(signal, "SIGBREAK", signal.SIGINT))
    real_open = service_app.webbrowser.open
    browser_calls = ServiceBrowser(browser)
    service_app.webbrowser.open = browser_calls.fake_open
    service_app.install_shutdown_signal_handlers()
    try:
        loaded = config_module.load()
        runtime = service_app.build_runtime(loaded)
        runtime.set_binding("127.0.0.1", 8765)
        app = service_app.build_app(runtime, loaded)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.browser_calls = browser_calls
            yield client
    finally:
        service_app.webbrowser.open = real_open
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), prev_break)
        config_module.set_current(old_config)
        _restore_logging(old_handlers)
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile


class ServiceBrowser:
    """进程内那一支的假 `webbrowser`：`fake_open` 只把 URL 记进 `calls`。

    `outcome` 决定 `fake_open` 的行为：`safe` = 返回 `False`（正常路径上这一步本来就开不上）、
    `open` = 返回 `True`、`raise` = 抛 `RuntimeError`。它**不碰任何浏览器**，
    因此在 ASGI 层用例里 `client.browser_calls.calls` 就是「这一步被调到了吗」的观测面。
    """

    def __init__(self, outcome: str = "safe") -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def fake_open(self, url, *args, **kwargs):
        self.calls.append(str(url))
        if self.outcome == "raise":
            raise RuntimeError("假 webbrowser：按测试要求模拟没有可用的浏览器")
        return self.outcome == "open"

    def calls_log(self) -> list[str]:
        """`fake_open` 依次收到的 URL（与 `calls` 同一份，写成方法的读法更显眼）。"""
        return list(self.calls)


def _swap_logging(home: Path):
    """把服务级日志接到临时家目录（ASGI 层用例的家目录隔离手段之一）。

    走 `logging_setup.handler_snapshot()` 这个公用查询入口拿快照，用完在
    `_restore_logging` 里走 `restore_handlers()` 放回去 —— 测试因此不去读被测模块的
    模块级私有状态。
    """
    from meeting import logging_setup

    previous = logging_setup.handler_snapshot()
    logging_setup.reset_for_tests()
    logging_setup.setup_service_logging(
        str(log_dir_of(home)),
        level="INFO",
        max_bytes=5 * 1024 * 1024,
        backup_count=3,
    )
    return previous


def _restore_logging(saved: dict) -> None:
    from meeting import logging_setup

    logging_setup.restore_handlers(saved)


def preserve_pythonpath() -> str:
    """把 `PYTHONPATH` 固定成「测试开始时的那一份」——**必须先于 `import meeting`**。

    `pytest` 的 `pythonpath` ini 选项会把仓库根 `insert(0)` 进 `sys.path`，`sys.path` 会渗进
    子进程 env 的 `PYTHONPATH`。仓库根一旦排在前面，`tests/_fake_webbrowser/webbrowser.py`
    就被仓库根上那个同名的 `meeting` 包挡住了（包比模块优先）；更麻烦的是「本地先跑过一遍、
    再跑用例文件」这两种启动方式会因此得到不同的 `PYTHONPATH`，用例红得莫名其妙。
    这里在 `import meeting` 之前把那份渗进来的仓库根摘掉，两种启动方式于是得到同一份
    `PYTHONPATH`（`tests/_fake_webbrowser` 仍由 `fake_browser_module()` 现加现用）。
    """
    os.environ["PYTHONPATH"] = PYTHONPATH_BASELINE or ""
    return PYTHONPATH_BASELINE or ""


# `import meeting` 之前先把 `PYTHONPATH` 定死（见 `preserve_pythonpath()`）。
preserve_pythonpath()


# ---------------------------------------------------------------------------
# 假 `webbrowser`：子进程里验「打开浏览器」这一步的唯一安全手段
# ---------------------------------------------------------------------------


def _fake_browser_path_dir(tmp_path: Path) -> Path:
    """假模块的落点：`<tmp_path>/fake-path/webbrowser.py` —— 一份**拷贝**。

    **位置说明（别把这份拷贝挪到 `tests/` 下）**：`PYTHONPATH` 的环境变量语法**没有
    「只搜这个目录」的写法**，一旦把假模块放在需要顺带搜到别的目录的地方，就得往
    `PYTHONPATH` 里再加一项；那一项会连带影响子进程的 import 搜索路径。这里让假模块
    **自成一个只装一个文件的目录**：`PYTHONPATH` 里因此只有它一项，多出来的影响面为零。
    拷贝源是随仓库走的 `tests/_fake_webbrowser/webbrowser.py`。
    """
    target = Path(tmp_path) / "fake-path"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FAKE_BROWSER_MODULE_DIR / "webbrowser.py", target / "webbrowser.py")
    return target


class FakeBrowser:
    """一次「假 `webbrowser` 注入」的现场：`calls_file` 是「这一步被调到了吗」的观测面。"""

    def __init__(self, calls: Path, marker: Path, module_dir: Path, outcome: str) -> None:
        self.calls_file = Path(calls)
        self.marker_file = Path(marker)
        self.module_dir = Path(module_dir)
        self.outcome = outcome

    def calls(self) -> list[str]:
        """`webbrowser.open()` 依次收到的 URL（一行一个）。"""
        text = read_text(self.calls_file)
        return [line for line in text.splitlines() if line.strip()]

    def raw_calls_text(self) -> str:
        return read_text(self.calls_file)

    def loaded(self) -> bool:
        """子进程里命中的 `webbrowser` 是不是这份假替身（假模块载入时会写 marker）。"""
        return "fake-webbrowser-loaded" in read_text(self.marker_file)


@contextlib.contextmanager
def fake_browser_module(
    tmp_path: Path, *, home: Path | None = None, outcome: str = "false"
) -> Iterator[FakeBrowser]:
    """在**本测试进程**里注入假 `webbrowser`，用完把 `PYTHONPATH` 恢复原样。

    用途一：给 `spawn_service(env_extra=fake_browser_env(...))` 提供环境变量
    （子进程里 `import webbrowser` 命中假模块）；用途二：`module_dir` 交给要自己起子进程的
    用例（例如「假模块真的排到了标准库前面」那条断言）。

    `outcome`：`false`（`open()` 返回 `False`）/ `true` / `raise`（抛 `RuntimeError`）。
    `home` 给定时，`calls` 与 `marker` 两个文件落在 `<home>/fake-browser/` 下（该目录会建好）。
    """
    base = Path(home) if home is not None else Path(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    record_dir = base / "fake-browser"
    record_dir.mkdir(parents=True, exist_ok=True)
    calls = record_dir / "open-calls.txt"
    marker = record_dir / "module-marker.txt"
    module_dir = _fake_browser_path_dir(tmp_path)

    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(module_dir)] + ([previous] if previous else [])
    )
    try:
        yield FakeBrowser(calls, marker, module_dir, outcome)
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous


def fake_browser_env(fake: FakeBrowser) -> dict[str, str]:
    """假模块所需的子进程环境变量（与假模块里的变量名一一对应）。

    这些变量与 `MEETING_NO_BROWSER` 是**两码事**：它们指挥的是测试注入的那份假替身，
    因此即使子进程真的走到「打开浏览器」这一步，也只会往 `calls` 文件里记一行。
    """
    return {
        FAKE_BROWSER_PATH_ENV: str(fake.module_dir),
        FAKE_BROWSER_CALLS_ENV: str(fake.calls_file),
        FAKE_BROWSER_OUTCOME_ENV: fake.outcome,
        FAKE_BROWSER_MARKER_ENV: str(fake.marker_file),
    }


def open_browser_step_trace(home: Path) -> str:
    """读服务级主 log，返回与「打开浏览器」这一步有关的那几行（两种情形都在这里面找）。"""
    return "\n".join(
        line
        for line in read_text(service_log_path_of(home)).splitlines()
        if "浏览器" in line
    )


# ---------------------------------------------------------------------------
# 网络小工具（端口占用与探针用例用；都用标准库）
# ---------------------------------------------------------------------------


class FakeMeetingServer:
    """一个「meeting 形状」的应答者：`GET /api/heartbeat` 回 `{"service": "meeting", ...}`。

    `service_name` 换成别的值时，它就是一个「别的程序」的应答者（用来验探针的判据）。
    """

    def __init__(self, port: int, *, service_name: str = "meeting") -> None:
        self.port = port
        self.service_name = service_name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._started = False

    def start(self) -> "FakeMeetingServer":
        self._started = True
        self._thread.start()
        return self

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            with contextlib.suppress(OSError):
                conn.settimeout(1.0)
                with contextlib.suppress(OSError):
                    conn.recv(4096)
                body = (
                    '{"service": "' + self.service_name + '", "server_time": '
                    '"2026-01-01T00:00:00+08:00", "revision": 1, "top_state": "idle"}'
                ).encode("utf-8")
                head = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                )
                with contextlib.suppress(OSError):
                    conn.sendall(head + body)
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.sock.close()
        if self._started:
            self._thread.join(timeout=2)


class SilentListener:
    """接受连接但不应答的 socket（造「探针 1 秒总超时」那一路）。"""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def start(self) -> "SilentListener":
        self._thread.start()
        return self

    def _hold(self) -> None:
        self.sock.settimeout(0.5)
        conns = []
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                conns.append(conn)  # 收下连接，什么都不回
            except (TimeoutError, OSError):
                continue
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.close()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.sock.close()
        self._thread.join(timeout=2)


class OccupiedPort:
    """用一个普通 socket 占住端口（造「别的程序占着」那一路）。"""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


def can_bind(port: int) -> bool:
    """端口空闲吗（不复用地址，尽量反映真实占用）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def tcp_connect(host: str, port: int, timeout: float = 3.0) -> bool:
    """能连上吗（只回真假，异常交给调用方判类型时自己写）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True


def non_loopback_ipv4_addresses() -> list[str]:
    """本机非回环的 IPv4 地址（过滤 `127.0.0.0/8` 与 `0.0.0.0`）。"""
    found: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:  # pragma: no cover - 主机名解析不了时按「没有候选」处理
        return found
    for info in infos:
        addr = info[4][0]
        if addr.startswith("127.") or addr == "0.0.0.0":
            continue
        if addr not in found:
            found.append(addr)
    return found


def missing_dist_dir(home: Path) -> Path:
    """一个确定不存在的产物目录（ASGI 层用例用它造「产物缺失」）。"""
    return Path(home) / "nowhere" / "dist"


def write_dist_dir(home: Path, marker: str = "dist-here") -> Path:
    """造一个带 `index.html` 的真产物目录。"""
    dist = Path(home) / "built" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        f"<!doctype html><meta charset=\"utf-8\"><title>{marker}</title><h1>{marker}</h1>",
        encoding="utf-8",
    )
    return dist


def lines_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


__all__ = [
    "ACTION_POST",
    "ACTION_WEB",
    "FAKE_BROWSER_CALLS_ENV",
    "FAKE_BROWSER_MARKER_ENV",
    "FAKE_BROWSER_MODULE_DIR",
    "FAKE_BROWSER_OUTCOME_ENV",
    "FAKE_BROWSER_PATH_ENV",
    "DEFAULT_CAPTURE_BACKEND",
    "DEFAULT_POST_HOOK",
    "DEFAULT_BACKEND_VALUES",
    "FakeBrowser",
    "FakeMeetingServer",
    "OccupiedPort",
    "REPO_ROOT",
    "PROCESS_TERMINATE_GRACE_SECONDS",
    "SERVICE_ENV_NAMES",
    "ServiceProcess",
    "ServiceBrowser",
    "SilentListener",
    "app_client",
    "apply_backend_defaults",
    "can_bind",
    "config_dir_of",
    "config_path_of",
    "fake_browser_env",
    "fake_browser_module",
    "lines_with",
    "local_client_for",
    "local_get",
    "local_post",
    "local_ssl_context",
    "log_dir_of",
    "missing_dist_dir",
    "non_loopback_ipv4_addresses",
    "open_browser_step_trace",
    "preserve_pythonpath",
    "read_text",
    "run_cli",
    "run_web_expecting_failure",
    "service_env",
    "service_log_path_of",
    "spawn_service",
    "tcp_connect",
    "wait_for_session_record",
    "wait_until",
    "write_config_json",
    "write_dist_dir",
]
