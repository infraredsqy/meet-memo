"""`tests/test_dirs_create.py` —— 目录选择器的新写入口 `POST /api/dirs` 与
「向上 / 当前这一层」的新口径（目录选择器节点）。

覆盖：

  - 端到端：在选择器里新建目录 → 目录在盘上带着用户起的名字 → `workspace_open`
    登记 → `GET /api/sessions` 清单里有这一行（spec 完成判定的 e2e）；
  - 建失败三档，各给一句人话、不留半截状态：同名已存在（409 `dir_exists`，
    目录与文件都算占位）/ 非法名（400 `invalid_setting`，不清洗不改写）/
    无权限（500 `persist_failed`）；外加父层不存在那一档；
  - 「建完直接选中」的服务端一侧：成功体 `directory` 是新建那一行的名目判读
    （刚建出来是 `empty`），选定与登记走既有链路；
  - 写端点头哨：缺 `X-Meeting-Service: 1` 403，请求没走到端点；
  - `up` 到盘根才置灰 / `here` 是正在浏览这一层自己的名目（服务端口径，
    界面用例在 `web/tests/dir-picker.test.tsx`）。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest

from meeting.session import SESSION_FILE_NAME


@pytest.fixture()
def api(tmp_path: Path):
    """进程内的一只跑起来的应用（dev 采集替身；家目录归本用例）。"""
    from tests import _helpers as helpers

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    values = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
    }
    with helpers.app_client(home, values=values) as client:
        yield client, home


def _create(client, parent: Path | str | None, name: str | None, *, with_header: bool = True):
    """打一次 `POST /api/dirs`（写端点头哨默认带全）。"""
    parts = []
    if parent is not None:
        parts.append(f"dir={quote(str(parent), safe='')}")
    if name is not None:
        parts.append(f"name={quote(name, safe='')}")
    query = "&".join(parts)
    headers = {"X-Meeting-Service": "1"} if with_header else {}
    return client.post(f"/api/dirs?{query}" if query else "/api/dirs", headers=headers)


def test_dirs_create_then_open_registers_workspace(api, tmp_path: Path):
    """端到端：新建 → 建完直接选中（成功体 `directory` 即选定那一行）→ 登记进清单。"""
    client, _home = api
    place = tmp_path / "place"
    place.mkdir()

    created = _create(client, place, "跟老王聊周会")
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    # 新建那一行是服务的名目判读（刚建出来是 empty），界面拿它直接置选定
    assert body["directory"] == {
        "name": "跟老王聊周会",
        "dir": str(place / "跟老王聊周会"),
        "kind": "empty",
        "suspected": False,
    }
    # 盘上真有一个目录，名字就是用户起的那一份（软件不自动命名）
    assert (place / "跟老王聊周会").is_dir()

    # 选定之后的确认走既有链路：empty 直接登记
    opened = client.post(
        "/api/actions",
        json={"action": "workspace_open", "args": {"dir": body["directory"]["dir"]}},
        headers={"X-Meeting-Service": "1"},
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["workspace"]["kind"] == "empty"

    rows = client.get("/api/sessions").json()
    assert [row["dir"] for row in rows["sessions"]] == [str(place / "跟老王聊周会")]
    assert rows["recent"] == str(place / "跟老王聊周会")


def test_dirs_create_rejection_tiers_leave_no_half_state(api, tmp_path: Path):
    """三档拒绝各给一句人话；每一档盘上都不落半截东西。"""
    client, _home = api
    place = tmp_path / "place"
    place.mkdir()

    # ① 同名已存在（409 dir_exists）：目录占位
    (place / "已有").mkdir()
    exists_dir = _create(client, place, "已有")
    assert exists_dir.status_code == 409
    assert exists_dir.json()["code"] == "dir_exists"
    assert "已有" in exists_dir.json()["message"]
    assert exists_dir.json()["detail"]["name"] == "已有"
    # 文件也占位（同名的普通文件同样不许踩）
    (place / "报告.pdf").write_bytes(b"%PDF")
    exists_file = _create(client, place, "报告.pdf")
    assert exists_file.status_code == 409
    assert exists_file.json()["code"] == "dir_exists"

    # ② 非法名（400 invalid_setting）：不清洗、不改写，原话拒回
    for bad in ["../逃逸", "a/b", "a\\b", "C:x", ".", "..", "name.", " 名字 ", "", "con"]:
        refused = _create(client, place, bad)
        assert refused.status_code == 400, f"{bad!r}: {refused.text}"
        assert refused.json()["code"] == "invalid_setting"
        assert refused.json()["detail"]["key"] == "name"
        assert refused.json()["message"], f"{bad!r} 得带一句能看懂的话"
    # 名字参数缺席同档
    missing_name = _create(client, place, None)
    assert missing_name.status_code == 400
    assert missing_name.json()["detail"]["key"] == "name"

    # ③ 父层不存在（400 invalid_setting，与 GET /api/dirs 同一档）
    missing_parent = _create(client, tmp_path / "nope", "随便")
    assert missing_parent.status_code == 400
    assert missing_parent.json()["code"] == "invalid_setting"
    assert missing_parent.json()["detail"]["key"] == "dir"

    # ④ 无权限（500 persist_failed）：mkdir 抛 PermissionError 如实转述，不静默
    def _forbidden(self, mode=0o777):
        raise PermissionError(13, "权限不给")

    original = Path.mkdir
    Path.mkdir = _forbidden  # type: ignore[method-assign]
    try:
        denied = _create(client, place, "建不出来")
    finally:
        Path.mkdir = original  # type: ignore[method-assign]
    assert denied.status_code == 500
    assert denied.json()["code"] == "persist_failed"
    assert "PermissionError" in denied.json()["detail"]["error"]

    # 半截状态核对：失败的每一档之后，盘上都只有动手前就有的那几样
    assert sorted(item.name for item in place.iterdir()) == ["已有", "报告.pdf"]


def test_dirs_create_header_guard(api, tmp_path: Path):
    """写端点头哨：缺 `X-Meeting-Service: 1` 403（请求没走到端点，盘上不动）。"""
    client, _home = api
    place = tmp_path / "guarded"
    place.mkdir()
    refused = _create(client, place, "没头的", with_header=False)
    assert refused.status_code == 403
    assert (place / "没头的").exists() is False


def test_dirs_create_missing_name_and_parent_shapes(api, tmp_path: Path):
    """省略 `dir` 落到主目录档；两参都缺时拒在名字那一档（校验次序：先层后名）。"""
    client, home = api
    created = _create(client, None, "主目录下的一个")
    assert created.status_code == 200, created.text
    # `dir` 省略时与 GET /api/dirs 同一个起点（用户主目录）
    assert created.json()["directory"]["dir"] == str(home / "主目录下的一个")
    assert (home / "主目录下的一个").is_dir()

    both_missing = client.post("/api/dirs", headers={"X-Meeting-Service": "1"})
    assert both_missing.status_code == 400
    assert both_missing.json()["detail"]["key"] == "name"


def test_dirs_listing_up_reaches_drive_root_and_here_row(api, tmp_path: Path):
    """`up` 到盘根才 null（主目录不是天花板）；`here` 是这一层自己的名目。"""
    client, home = api
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    listed = client.get(f"/api/dirs?dir={quote(str(deep), safe='')}")
    assert listed.status_code == 200
    body = listed.json()
    assert body["up"] == str(deep.parent)
    # here：正在浏览的这一层自己（空层如实 empty）
    assert body["here"] == {"name": "c", "dir": str(deep), "kind": "empty", "suspected": False}

    # 一路向上：c → b → a → tmp 根 → 盘根（up 逐层给上一层，到盘根落 null）
    layer = deep
    seen_ups: list[str | None] = []
    for _ in range(40):
        payload = client.get(f"/api/dirs?dir={quote(str(layer), safe='')}").json()
        if payload["up"] is None:
            break
        assert payload["up"] == str(layer.parent)
        seen_ups.append(payload["up"])
        layer = Path(payload["up"])
    assert layer == Path(deep.anchor), f"逐层向上应停在盘根，停在了 {layer}"
    assert len(seen_ups) >= 3

    # 带会议材料的一层，here 的名目判读跟着走（meeting 档）
    meeting = tmp_path / "带材料的层"
    meeting.mkdir()
    (meeting / SESSION_FILE_NAME).write_text("{}", encoding="utf-8")
    payload = client.get(f"/api/dirs?dir={quote(str(meeting), safe='')}").json()
    assert payload["here"]["kind"] == "meeting"

    # 真主目录起点同样能向上（实机读数的进程内对照档）
    from_home = client.get("/api/dirs").json()
    assert from_home["up"] == str(Path(os.environ["HOME"]).resolve().parent)
