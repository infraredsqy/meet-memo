"""`tests/_app_drivers.py` —— 进程内 app 的 HTTP 驱动助手（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点）：`live()` 起一个
进程内已启动的运行时 + ASGI 客户端，`post()` / `snapshot_of()` / `wait_for_done()` 是
打在它上面的动作与读数。原先住在 `tests/test_api_actions.py`、被各用例文件互相 import
的那一份正本搬到这里，用法一律 `from tests._app_drivers import ...`；各用例文件里
重抄的弱副本已并进来（断言与行为以这一份为准）。
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from tests import _helpers as helpers
from tests._helpers import wait_until


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """起一个进程内已启动的运行时；`yield (client, runtime, home, out_root)`。

    `out_root` 沿用旧名，语义变了：它只是**测试层的场次放置根**（给 start 的 `args.dir`
    预建工作区目录用），不再是任何配置键的值 —— 产品没有默认落点。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        yield client, client.app.state.runtime, home, out_root


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None or action == "start":
        place_root = getattr(client, "place_root", None)
        assert place_root is not None, (
            "start 要显式给 dir：live() 给的 client 自带放置根（client.place_root）"
        )
        body["args"] = helpers.start_args(action, args, place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def snapshot_of(client) -> dict:
    return client.get("/api/state").json()


def wait_for_done(client):
    return wait_until(
        lambda: (lambda s: s if s["ended"] and s["ended"]["phase"] == "done" else None)(
            snapshot_of(client)
        ),
        what="收尾停在 done",
    )
