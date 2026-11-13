"""`web.dist_dir` 指到一个不存在的路径时，**真 `meeting web` 子进程**的端到端用例（设计）。

这一条盯的是「前端产物还没构建」这个现场在产品路径上的样子 —— 起一个真服务子进程
（`[sys.executable, "-m", "meeting", "web"]`，地址从启动输出里解析、**不猜端口**），
配置里把 `web.dist_dir` 指到 `<本用例临时家目录>\\missing-dist`（**这个目录不存在**），
然后逐条确认：

  1. 子进程起来了：终端那行 `URL：http://127.0.0.1:<port>` 解析出的端口真的连得上；
  2. `GET /` 是 `200`，正文里是**产物缺失的说明文案**（抄现成的常量
     `meeting/service/app.py::DIST_MISSING_MESSAGE` 的内容，那个常量一个字不改）；
  3. `GET /api/heartbeat` 是 `200`，body 里 `service == "meeting"`；
  4. `GET /api/state` 是 `200`；
  5. 那个不存在的产物目录**跑完仍然不存在** —— 服务不为了这一页去把它建出来；
  6. 发一次中断信号之后退出码是 `EXIT_OK`（空闲态退出档）。

## 为什么落在慢档

判据 ①（真服务子进程）与判据 ②（真墙钟等待）两条都成立：这个文件起一个真子进程、读它的
真墙钟。归位判据见 `docs/service.md`；文件头这一行 `pytestmark` 是它进慢档
的唯一落点（**不塞进任何已有的快档文件**：那会让那个文件的单文件耗时越过 10 秒红线）。

## 为什么不删 `web/dist`

本机那个构建产物是存在的。删掉它等于把仓库里的东西弄没了，而这条用例要演的只是
「产物目录缺席」这一个现场 —— 因此**把 `web.dist_dir` 指到一个不存在的路径**，
效果与「产物还没构建」逐字相同，仓库里的任何东西一个字节都不动
（判据 ⑤ 正是盯着这一点的另一面：服务自己也不会去建它）。

## 进程内那一档已经有了

`tests/test_static.py` 在 ASGI 层逐条验过同一个现场（说明页、`/api/*` 照常、目录按每请求判定）。
本文件补的是**真子进程 + 真信号 + 真退出码**那一层：进程内 `TestClient` 覆盖不到
「服务自己选端口、自己装信号处理函数、收到中断之后按哪一档退出」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest

from meeting.cli import EXIT_OK
from meeting.service.app import DIST_MISSING_MESSAGE
from tests._service import SERVICE_ID

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

#: 本用例那份不存在的产物目录名（落在临时家目录下，与仓库里的 `web/dist` 无关）
MISSING_DIST_DIR_NAME = "missing-dist"

#: 起子进程与等就绪的墙钟上界（秒）—— 冷启动一个真服务在本机实测 1~2 秒，留足余量
SERVICE_STARTUP_TIMEOUT_SECONDS = 30.0

#: 等子进程收场的墙钟上界（秒）
SERVICE_EXIT_TIMEOUT_SECONDS = 30.0


@pytest.fixture()
def missing_dist(meeting_env: Any) -> Path:
    """`web.dist_dir` 要指到的那个路径：**它此刻不存在，跑完也不该被建出来**。"""
    return Path(meeting_env.home) / MISSING_DIST_DIR_NAME


def test_a_real_web_process_serves_the_missing_dist_notice_and_exits_ok(
    meeting_env: Any,
    service_process: Callable[..., Any],
    missing_dist: Path,
) -> None:
    """真 `meeting web` 子进程 + 产物目录缺席：说明页、`/api/*` 照常、中断后 `EXIT_OK`。"""
    # 前置：这个路径此刻确实不存在（不存在这件事本身就是被测现场的一部分）
    assert not missing_dist.exists(), f"{missing_dist} 在起服务之前就已经在了"
    meeting_env.write_config({"web.dist_dir": str(missing_dist)})

    proc = service_process(
        home=meeting_env.home,
        out_root=meeting_env.out_root,
        log_dir=meeting_env.log_dir,
        env=dict(meeting_env.env),
        pythonpath=str(Path(__file__).resolve().parent.parent),
    )

    # ① 子进程起来了：那一行 `URL：…` 是**子进程自己**打在它自己的终端上的，而且它住在
    # 另一个进程里 —— 这两条一起把「真子进程」这个前提钉住：进程内 `TestClient` 那一档
    # 两头都做不到（它没有子进程的 stdout，pid 也是父进程自己）。
    base_url = proc.wait_ready(timeout=SERVICE_STARTUP_TIMEOUT_SECONDS)
    assert f"URL：{base_url}" in proc.stdout(), (
        "这一趟该起真子进程：它自己的终端输出里没有那一行 URL。"
        f"stdout 末尾={proc.stdout()[-500:]!r}"
    )
    assert proc.pid != os.getpid(), (
        f"这一条该由真子进程承担，实测 pid={proc.pid}（跑用例的进程是 {os.getpid()}）"
    )

    # ② `GET /` 是 200，正文是现成那个常量的内容（常量本身一个字不改）
    root = _get(f"{base_url}/")
    assert root.status_code == 200, f"GET / 该回 200，实测 {root.status_code}"
    assert DIST_MISSING_MESSAGE in root.text, (
        f"说明页里该有产物缺失的说明文案：{DIST_MISSING_MESSAGE!r}"
    )

    # ③ `GET /api/heartbeat` 照常应答
    heartbeat = _get(f"{base_url}/api/heartbeat")
    assert heartbeat.status_code == 200, f"GET /api/heartbeat 该回 200，实测 {heartbeat.status_code}"
    assert heartbeat.json().get("service") == SERVICE_ID, f"心跳 body 不对：{heartbeat.text[:200]}"

    # ④ `GET /api/state` 照常应答
    state = _get(f"{base_url}/api/state")
    assert state.status_code == 200, f"GET /api/state 该回 200，实测 {state.status_code}"

    # ⑤ 服务没有为了这一页去把那个产物目录建出来
    assert not missing_dist.exists(), (
        f"服务把 {missing_dist} 建出来了；说明页承诺的是「产物一建好，刷新就能见」，"
        "它自己不该动那个目录"
    )

    # ⑥ 发一次中断信号 → 退出码 `EXIT_OK`（空闲态退出档）
    proc.send_interrupt()
    code = proc.wait(timeout=SERVICE_EXIT_TIMEOUT_SECONDS)
    assert code == EXIT_OK, (
        f"空闲态收到一次中断该以 EXIT_OK 收场，实测退出码 {code}；"
        f"stdout 末尾={proc.stdout()[-800:]!r} stderr 末尾={proc.stderr()[-800:]!r}"
    )

    # 收场之后再确认一次：整个生命周期里它都没被建出来
    assert not missing_dist.exists(), f"服务收场之后 {missing_dist} 仍然不该存在"


def _get(url: str) -> Any:
    """问本机服务的一次同步 GET（**走仓库现成的复用口径**：`trust_env=False`、按 url 复用连接）。

    直接用 `tests/_helpers.py::local_get()` 那一个：它只是一支探针，被测对象是那个子进程，
    而本机 `httpx.Client()` 每次构造要 0.83 秒（见那个函数的说明）—— 不复用的话这一条用例
    会被探针的构造代价顶过 5 秒红线。
    """
    from tests import _helpers as helpers

    return helpers.local_get(url, timeout=5.0)
