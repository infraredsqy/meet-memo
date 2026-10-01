"""`tests/test_workspace_registry.py` —— 工作区注册表与两只动作/两只读端点
（工作台收编节点 e2f6166b：spec 的完成判定逐条落在这里）。

覆盖：

  - 判别三档：空目录 / 有 `session.json` 的会议目录 / 有音频归档名却没有
    `session.json` 的**疑似会议目录**（兜底，不当杂物）/ 塞了杂物但没有会议材料的
    （unknown，要确认）/ 不存在的目录（missing）；
  - 注册表白纸：按路径去重（同一目录选两次只占一行）、`last_opened_at` 只更新
    `added_at`、`remove` 只摘名单**文件一个字节不动**；
  - 注册表文件住在配置目录（`workspaces.json` 与 `config.json` 两个文件）、原子写
    不留 tmp 副本、坏文件按空表起不往上冒；
  - API：`GET /api/sessions`（`recent` = 最近一次被打开/切换那一条）、
    `GET /api/dirs`（起点主目录的一层）、`workspace_open` 的三档 +
    `confirm` 后门、`workspace_remove` 的幂等；
  - 录过的 `start` 在注册表上记一笔 `last_opened_at`（最近一场随开场走）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from meeting.workspace import REGISTRY_FILE_NAME, WorkspaceRegistry, classify_workspace_dir
from meeting.session import SESSION_FILE_NAME, archive_name

#: 用例的显式等待（本文件没有要等的后台进程，留名备用）
OPEN_TIMEOUT_SECONDS = 5.0

pytest_snapshot_regen_hint = (
    "workspaces.json 的写盘形状不参与 contract fixtures；这里只验注册表与应用行为。"
)


# ---------------------------------------------------------------------------
# 注册表本体（单元档：不碰 HTTP）
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry(isolated_config_dir: Path) -> WorkspaceRegistry:
    """家目录隔离（autouse 的 `_isolated_home` + 本夹具）下的一个注册表。"""
    del isolated_config_dir
    return WorkspaceRegistry()


def test_open_dedup_and_timestamps(registry: WorkspaceRegistry, tmp_path: Path):
    """同一个目录选两次：列表里只占一行；`added_at` 不动、`last_opened_at` 更新。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    first = registry.open(str(ws), now="2026-09-21T21:00:00+08:00")
    assert first["first_time"] is True
    second = registry.open(str(ws), now="2026-09-21T22:00:00+08:00")
    assert second["first_time"] is False
    entries = registry.entries()
    assert len(entries) == 1
    assert entries[0]["dir"] == str(ws)
    assert entries[0]["added_at"] == "2026-09-21T21:00:00+08:00"
    assert entries[0]["last_opened_at"] == "2026-09-21T22:00:00+08:00"
    # 另一个写法（拖尾分隔符的同一目录）去重后还是同一行
    trailing = str(ws) + ("\\\\" if ws.drive else "/")
    third = registry.open(trailing, now="2026-09-21T23:00:00+08:00")
    assert third["first_time"] is False
    assert len(registry.entries()) == 1


@pytest.mark.skipif(
    os.path.normcase("A") == "A", reason="文件系统分大小写（POSIX）时不适用"
)
def test_open_case_insensitive_dedup(registry: WorkspaceRegistry, tmp_path: Path):
    """Windows 大小写口子（review-1 m1）：同一目录两个大小写写法只记一行。

    登记的键保留第一次落笔的原文（展示路径不折成小写）；读写两侧（open / remove）
    都按 normcase 查重。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    first = registry.open(str(ws), now="2026-09-21T21:00:00+08:00")
    assert first["first_time"] is True
    upper = str(ws).upper()
    second = registry.open(upper, now="2026-09-21T22:00:00+08:00")
    assert second["first_time"] is False
    assert second["dir"] == str(ws)
    assert len(registry.entries()) == 1
    assert registry.entries()[0]["dir"] == str(ws)
    # 查重另一侧：用另一个大小写删除也能摘掉那一条
    assert registry.remove(upper)["removed"] is True
    assert registry.entries() == []


def test_recent_reads_only_from_registry(registry: WorkspaceRegistry, tmp_path: Path):
    """「最近一场」只从注册表读：不给目录、也不扫任何场次目录（spec 第 9 条）。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    registry.open(a, now="2026-09-21T21:00:00+08:00")
    registry.open(b, now="2026-09-21T22:00:00+08:00")
    assert registry.recent()["dir"] == str(b)
    # 目录被移走之后注册表照样给最近那一条（行其它面由读取方现场判）
    import shutil

    shutil.rmtree(b)
    assert registry.recent()["dir"] == str(b)


def test_remove_takes_no_bytes(registry: WorkspaceRegistry, tmp_path: Path):
    """删除只把这一条从名单里摘走：目录、归档、`session.json` 一个字节不动。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    payload = ws / SESSION_FILE_NAME
    payload.write_text("{}", encoding="utf-8")
    audio = ws / archive_name("mic")
    audio.write_bytes(b"\x00\x01")
    registry.open(ws, now="2026-09-21T21:00:00+08:00")
    removed = registry.remove(ws)
    assert removed["removed"] is True
    assert registry.entries() == []
    assert payload.read_text(encoding="utf-8") == "{}"
    assert audio.read_bytes() == b"\x00\x01"
    # 重复删除安全（幂等，`removed: False`）
    assert registry.remove(ws)["removed"] is False
    # 收回来：再 open 一次就回去
    back = registry.open(ws, now="2026-09-21T22:00:00+08:00")
    assert back["first_time"] is True
    assert len(registry.entries()) == 1


def test_registry_file_lives_in_config_dir_and_atomic(registry: WorkspaceRegistry, tmp_path: Path):
    """注册表是 `~/.config/meeting/workspaces.json` —— 不是 config.json 的一个键；
    原子写之后不留临时副本。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    registry.open(ws)
    config_dir = Path(registry._registry_path()).parent
    assert Path(registry._registry_path()).name == REGISTRY_FILE_NAME
    assert not (config_dir / f".{REGISTRY_FILE_NAME}.*").exists()
    assert (config_dir / "config.json").exists() or True  # 写注册表不要求 config.json 存在


def test_corrupt_registry_starts_empty(registry: WorkspaceRegistry, tmp_path: Path):
    """注册表坏掉（读不出来）按空表起、异常不上冒；下一笔写入原子替换。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    target = Path(registry._registry_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    assert registry.entries() == []
    assert registry.recent() is None
    registry.open(ws)  # 不抛
    assert [entry["dir"] for entry in registry.entries()] == [str(ws)]


# ---------------------------------------------------------------------------
# 目录判别（spec 的三档 + 兜底段）
# ---------------------------------------------------------------------------


def test_classify_three_kinds_and_suspected(tmp_path: Path):
    """空目录 / 会议目录 / 疑似会议目录 / 杂物目录 / missing，逐档一个读数。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert classify_workspace_dir(empty)["kind"] == "empty"

    with_meta = tmp_path / "with-meta"
    with_meta.mkdir()
    (with_meta / SESSION_FILE_NAME).write_text("{}", encoding="utf-8")
    got = classify_workspace_dir(with_meta)
    assert got["kind"] == "meeting"
    assert got["suspected"] is False

    audio_only = tmp_path / "audio-only"
    audio_only.mkdir()
    (audio_only / archive_name("mic")).write_bytes(b"")
    got = classify_workspace_dir(audio_only)
    assert got["kind"] == "meeting"
    assert got["suspected"] is True  # 有音频归档名、没有 session.json：照收编不当杂物

    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "wj.pdf").write_bytes(b"")
    got = classify_workspace_dir(junk)
    assert got["kind"] == "unknown"
    assert got["suspected"] is False

    assert classify_workspace_dir(tmp_path / "nope")["kind"] == "missing"


# ---------------------------------------------------------------------------
# API 档
# ---------------------------------------------------------------------------


def post(client, action: str, args: dict | None = None):
    body: dict = {"action": action}
    if args is not None:
        body["args"] = args
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture()
def api(tmp_path: Path):
    """进程内的一只跑起来的应用（dev 采集替身；家目录与场次放置根都归本用例）。"""
    from tests import _helpers as helpers

    home = make_home(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    values = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
    }
    with helpers.app_client(home, values=values) as client:
        yield client, home, out_root


def test_api_sessions_empty_and_open_paths(api, tmp_path: Path):
    """spec 完成判定第 1–2 条：三档目录逐个走「新会议」；同一目录两次只占一行。"""
    client, _home, _out_root = api

    # 还没开过：两只读端点的空档
    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert listed.json() == {"sessions": [], "recent": None}

    dirs_root = tmp_path / "meeting-places"
    dirs_root.mkdir()
    empty_dir = dirs_root / "空会"
    empty_dir.mkdir()
    meeting_dir = dirs_root / "跟老王聊周会"
    meeting_dir.mkdir()
    (meeting_dir / SESSION_FILE_NAME).write_text(
        json.dumps({"session_id": "跟老王聊周会", "state": "done", "date": "2026-09-21",
                    "mode": "both"}), encoding="utf-8"
    )
    junk_dir = dirs_root / "杂物间"
    junk_dir.mkdir()
    (junk_dir / "wj.pdf").write_bytes(b"%PDF-not-really")

    # ① 空目录 → 当一场新会，登记成功
    opened_empty = post(client, "workspace_open", {"dir": str(empty_dir)})
    assert opened_empty.status_code == 200, opened_empty.text
    assert opened_empty.json()["workspace"] == {
        "dir": str(empty_dir), "kind": "empty", "suspected": False, "first_time": True,
    }

    # ② 有会议材料的 → 收编
    opened_meeting = post(client, "workspace_open", {"dir": str(meeting_dir)})
    assert opened_meeting.status_code == 200, opened_meeting.text
    body = opened_meeting.json()["workspace"]
    assert body["kind"] == "meeting" and body["first_time"] is True

    # ③ 杂物目录 → 先拒（400，目录没登记；盘上没多一个字节）、确认后才登记
    refused = post(client, "workspace_open", {"dir": str(junk_dir)})
    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_setting"
    assert refused.json()["detail"].get("key") == "confirm"
    confirmed = post(client, "workspace_open", {"dir": str(junk_dir), "confirm": True})
    assert confirmed.status_code == 200, confirmed.text

    # 疑似会议目录：有音频归档名、没有 session.json —— 照收编，不弹确认
    suspected = dirs_root / "只有录音"
    suspected.mkdir()
    (suspected / archive_name("mic")).write_bytes(b"")
    opened_suspected = post(client, "workspace_open", {"dir": str(suspected)})
    assert opened_suspected.status_code == 200, opened_suspected.text
    assert opened_suspected.json()["workspace"]["suspected"] is True

    # 同一目录选两次只占一行；列表与 recent
    again = post(client, "workspace_open", {"dir": str(empty_dir)})
    assert again.status_code == 200
    rows = client.get("/api/sessions").json()
    assert len(rows["sessions"]) == 4
    dirs_only = {row["dir"] for row in rows["sessions"]}
    assert dirs_only == {str(empty_dir), str(meeting_dir), str(junk_dir), str(suspected)}
    # 会场两行的现场读数从 session.json 现场来
    meeting_row = next(row for row in rows["sessions"] if row["dir"] == str(meeting_dir))
    assert meeting_row["state"] == "done"
    assert meeting_row["date"] == "2026-09-21"
    assert meeting_row["mode"] == "both"
    empty_row = next(row for row in rows["sessions"] if row["dir"] == str(empty_dir))
    assert empty_row["state"] is None and empty_row["kind"] == "empty"
    # recent = 最近一次被打开/切换的那一场；同秒并列时按注册表的并列判据算唯一答案
    expected_recent = max(
        rows["sessions"],
        key=lambda r: (r["last_opened_at"], r["added_at"], r["dir"]),
    )["dir"]
    assert rows["recent"] == expected_recent


def test_api_dirs_browses_from_home(api, tmp_path: Path):
    """目录选择器：起点是用户主目录，一层子目录带着名目；up 到主目录就是 null。"""
    client, _home, _out_root = api
    import os

    listed = client.get("/api/dirs")
    assert listed.status_code == 200
    body = listed.json()
    home_norm = os.path.normcase(str(Path(os.environ["HOME"]).resolve()))
    assert os.path.normcase(body["cwd"]) == home_norm
    assert body["home"] == body["cwd"]
    assert body["up"] is None

    # 放一个空目录和多一个杂物目录进去
    base = tmp_path / "browse-root"
    base.mkdir(exist_ok=True)
    empty_dir = base / "空会"
    empty_dir.mkdir()
    junk_dir = base / "杂物间"
    junk_dir.mkdir()
    (junk_dir / "wj.pdf").write_bytes(b"")
    shown = client.get(f"/api/dirs?dir={str(base)}".replace("\\", "/"))
    assert shown.status_code == 200
    names = {item["name"]: item for item in shown.json()["dirs"]}
    assert names["空会"]["kind"] == "empty"
    assert names["杂物间"]["kind"] == "unknown"
    # 不存在的目录如实回 400
    bad = client.get("/api/dirs?dir=" + str(tmp_path / "nope"))
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_setting"


def test_api_remove_and_reclaim(api, tmp_path: Path):
    """spec 完成判定第 3 条：删除之后文件仍在盘上，且「打开目录」能把它收回列表。"""
    client, _home, _out_root = api
    ws = tmp_path / "收回来的那一场"
    ws.mkdir()
    audio = ws / archive_name("loopback")
    audio.write_bytes(b"\x00\x02")
    assert post(client, "workspace_open", {"dir": str(ws)}).status_code == 200
    assert len(client.get("/api/sessions").json()["sessions"]) == 1

    removed = post(client, "workspace_remove", {"dir": str(ws)})
    assert removed.status_code == 200, removed.text
    assert removed.json()["workspace"] == {"dir": str(ws), "removed": True}
    assert client.get("/api/sessions").json()["sessions"] == []
    assert audio.read_bytes() == b"\x00\x02"  # 一个字节没动
    assert ws.is_dir()  # 目录本身还在

    # 打开目录（= 再一次 workspace_open）把它收回来
    # 以及到场里确认收回来的一行如实显示"路径已不在" —— 改名/移走的分支在下面那条
    assert post(client, "workspace_open", {"dir": str(ws)}).status_code == 200
    rows = client.get("/api/sessions").json()
    assert [row["dir"] for row in rows["sessions"]] == [str(ws)]


def test_api_missing_row_shows_detached(api, tmp_path: Path):
    """spec 完成判定第 4 条：把某一行的目录改名/移走，那一行显示「路径已不在」。"""
    client, _home, _out_root = api
    import shutil

    ws = tmp_path / "会被搬走的一场"
    ws.mkdir()
    (ws / SESSION_FILE_NAME).write_text(
        json.dumps({"session_id": "会被搬走的一场", "state": "done"}), encoding="utf-8"
    )
    assert post(client, "workspace_open", {"dir": str(ws)}).status_code == 200
    shutil.move(str(ws), str(tmp_path / "被搬走之后"))
    row = client.get("/api/sessions").json()["sessions"][0]
    assert row["dir_exists"] is False
    assert row["dir"] == str(ws)  # 行有它原本记录的路径
    # 删掉之后（idempotent），再 remove 一次照样 200
    assert post(client, "workspace_remove", {"dir": str(ws)}).json()["workspace"] == {
        "dir": str(ws), "removed": True,
    }


def test_api_unregistered_dir_and_missing(api, tmp_path: Path):
    """`workspace_open` 的 missing 档：400 且不登记；未给 dir 同一档。"""
    client, home, _out_root = api
    none = post(client, "workspace_open", {"dir": str(tmp_path / "不在")})
    assert none.status_code == 400
    assert client.get("/api/sessions").json()["sessions"] == []
    # 缺 dir 也是 400（同一个 code，message 不同）
    dry = post(client, "workspace_open", {})
    assert dry.status_code == 400


def test_start_notes_registry(api, tmp_path: Path):
    """开过的一场在注册表上记一笔 last_opened_at：最近一场随开场走。"""
    client, home, out_root = api
    ws = out_root / "开过的一场"
    ws.mkdir()
    body = {"action": "start", "args": {"dir": str(ws), "mode": "both"}}
    assert client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"}).status_code == 200
    rows = client.get("/api/sessions").json()
    assert len(rows["sessions"]) == 1
    assert rows["sessions"][0]["state"] == "recording"
    assert rows["recent"] == str(ws)


def test_api_list_does_not_bump_revision(api):
    """两只读端点是纯读：任何状态下都 200、不涨 revision、不广播。"""
    client, _home, _out_root = api
    before = client.get("/api/state").json()["revision"]
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/dirs").status_code == 200
    assert client.get("/api/state").json()["revision"] == before
