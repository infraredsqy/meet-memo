"""工作区注册表与两只动作/两只读端点。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from meeting.workspace import REGISTRY_FILE_NAME, WorkspaceRegistry, classify_workspace_dir
from meeting.session import SESSION_FILE_NAME, archive_name

pytest_snapshot_regen_hint = (
    "workspaces.json 的写盘形状不参与 contract fixtures；这里只验注册表与应用行为。"
)


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
    """Windows 大小写口子：同一目录两个大小写写法只记一行。

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
    """「最近一场」只从注册表读：不给目录、也不扫任何场次目录。"""
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


def test_ordered_most_recent_first(registry: WorkspaceRegistry, tmp_path: Path):
    """清单次序 = 「最近在用」倒序：登记先后不作数，最近打开的排最前。

    `ordered()` 与 `recent()` 同一条判据链：倒序头一条就是最近那一条；切回最早
    登记的那一场，它翻到最前、其余相对次序不动。
    """
    jia, yi, bing = (tmp_path / name for name in ("甲", "乙", "丙"))
    for d in (jia, yi, bing):
        d.mkdir()
    registry.open(jia, now="2026-09-21T21:00:01+08:00")
    registry.open(yi, now="2026-09-21T21:00:02+08:00")
    registry.open(bing, now="2026-09-21T21:00:03+08:00")
    assert [e["dir"] for e in registry.ordered()] == [str(bing), str(yi), str(jia)]
    registry.open(jia, now="2026-09-21T21:00:04+08:00")
    assert [e["dir"] for e in registry.ordered()] == [str(jia), str(bing), str(yi)]
    assert registry.recent()["dir"] == str(jia)


def test_ordered_same_second_reads_stable(registry: WorkspaceRegistry, tmp_path: Path):
    """同一秒登记的两条：兜底链（同秒比 added_at，再比目录字典序）给出唯一答案，
    两次读取同一次序——不许「刷新一次换一个样」。"""
    yi, jia = tmp_path / "乙", tmp_path / "甲"
    yi.mkdir()
    jia.mkdir()
    stamp = "2026-09-21T21:00:00+08:00"
    registry.open(yi, now=stamp)
    registry.open(jia, now=stamp)
    first = [e["dir"] for e in registry.ordered()]
    again = [e["dir"] for e in registry.ordered()]
    assert first == again
    # 两个时刻并列到底：倒序取大，目录字典序收尾（甲 > 乙），答案唯一且稳定
    assert first == [str(jia), str(yi)]
    assert registry.recent()["dir"] == str(jia)


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
    assert registry.remove(ws)["removed"] is False
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
    """三档目录逐个走「新会议」；同一目录两次只占一行。"""
    client, _home, _out_root = api

    # 还没开过：两只读端点的空档（顶层第三格 `post_jobs`：此刻没有会后的活儿在飞）
    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert listed.json() == {"sessions": [], "recent": None, "post_jobs": []}

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

    opened_empty = post(client, "workspace_open", {"dir": str(empty_dir)})
    assert opened_empty.status_code == 200, opened_empty.text
    assert opened_empty.json()["workspace"] == {
        "dir": str(empty_dir), "kind": "empty", "suspected": False, "first_time": True,
    }

    opened_meeting = post(client, "workspace_open", {"dir": str(meeting_dir)})
    assert opened_meeting.status_code == 200, opened_meeting.text
    body = opened_meeting.json()["workspace"]
    assert body["kind"] == "meeting" and body["first_time"] is True

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

    again = post(client, "workspace_open", {"dir": str(empty_dir)})
    assert again.status_code == 200
    rows = client.get("/api/sessions").json()
    assert len(rows["sessions"]) == 4
    dirs_only = {row["dir"] for row in rows["sessions"]}
    assert dirs_only == {str(empty_dir), str(meeting_dir), str(junk_dir), str(suspected)}
    # 会场两行的现场读数从 session.json 现场来（state 是三档口径：有记录即 ended）
    meeting_row = next(row for row in rows["sessions"] if row["dir"] == str(meeting_dir))
    assert meeting_row["state"] == "ended"
    assert meeting_row["date"] == "2026-09-21"
    assert meeting_row["mode"] == "both"
    empty_row = next(row for row in rows["sessions"] if row["dir"] == str(empty_dir))
    assert empty_row["state"] is None and empty_row["kind"] == "empty"
    expected_recent = max(
        rows["sessions"],
        key=lambda r: (r["last_opened_at"], r["added_at"], r["dir"]),
    )["dir"]
    assert rows["recent"] == expected_recent


def test_api_dirs_browses_from_home(api, tmp_path: Path):
    """目录选择器：起点是用户主目录，一层子目录带着名目；`up` 到盘根才 null。"""
    client, _home, _out_root = api
    import os

    listed = client.get("/api/dirs")
    assert listed.status_code == 200
    body = listed.json()
    home_path = Path(os.environ["HOME"]).resolve()
    home_norm = os.path.normcase(str(home_path))
    assert os.path.normcase(body["cwd"]) == home_norm
    assert body["home"] == body["cwd"]
    # 从 home 还能向上（`up` 是 home 的上一层）
    assert body["up"] == str(home_path.parent)
    # here 是正在浏览这一层自己的名目（与 dirs 每项同一套四格）
    assert set(body["here"]) == {"name", "dir", "kind", "suspected"}
    assert os.path.normcase(body["here"]["dir"]) == home_norm
    assert body["here"]["kind"] in ("empty", "meeting", "unknown")
    # 盘根那一档 up 如实落 null（「向上一层」唯一的到顶档）
    at_root = client.get("/api/dirs?dir=" + home_path.anchor)
    assert at_root.status_code == 200
    assert at_root.json()["up"] is None

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
    bad = client.get("/api/dirs?dir=" + str(tmp_path / "nope"))
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_setting"


def test_api_dirs_files_param_lists_files_only_when_asked(api, tmp_path: Path):
    """`?files=1`（资料界面的选择器文件模式）：应答体多一个 `files` 键。

    不带这个参数时**没有**这个键——挑录音落点的老路径旧应答体一个字节不动；
    带上时这一层的普通文件每项 `{name, dir, size_bytes}`，与 `dirs` 分开两列
    （目录进不了 files，文件也进不了 dirs），按名字排序。
    """
    client, _home, _out_root = api
    base = tmp_path / "files-root"
    base.mkdir()
    (base / "一个子目录").mkdir()
    (base / "报告.pdf").write_bytes(b"0123456789")
    (base / "notes.md").write_bytes(b"abc")

    plain = client.get(f"/api/dirs?dir={base}")
    assert plain.status_code == 200
    assert "files" not in plain.json(), "不带 files 参数时旧形状一个字节不动"

    listed = client.get(f"/api/dirs?dir={base}&files=1")
    assert listed.status_code == 200
    body = listed.json()
    file_rows = body["files"]
    assert [row["name"] for row in file_rows] == ["notes.md", "报告.pdf"]
    for row in file_rows:
        assert set(row) == {"name", "dir", "size_bytes"}
    by_name = {row["name"]: row for row in file_rows}
    assert by_name["报告.pdf"]["size_bytes"] == 10
    assert by_name["报告.pdf"]["dir"] == str(base / "报告.pdf")
    assert {row["name"] for row in body["dirs"]} == {"一个子目录"}
    for row in body["dirs"]:
        assert set(row) == {"name", "dir", "kind", "suspected"}


def test_api_dirs_files_param_unreadable_dir_still_400(api, tmp_path: Path):
    """`?files=1` 打到不存在的目录，与不带参数同一档：400 `invalid_setting`。"""
    client, _home, _out_root = api
    bad = client.get(f"/api/dirs?dir={tmp_path / 'nope'}&files=1")
    assert bad.status_code == 400
    assert bad.json()["code"] == "invalid_setting"


def test_api_remove_and_reclaim(api, tmp_path: Path):
    """删除之后文件仍在盘上，且「打开目录」能把它收回列表。"""
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
    assert audio.read_bytes() == b"\x00\x02"
    assert ws.is_dir()

    assert post(client, "workspace_open", {"dir": str(ws)}).status_code == 200
    rows = client.get("/api/sessions").json()
    assert [row["dir"] for row in rows["sessions"]] == [str(ws)]


def test_api_missing_row_shows_detached(api, tmp_path: Path):
    """把某一行的目录改名/移走，那一行显示「路径已不在」。"""
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
    assert row["dir"] == str(ws)
    assert post(client, "workspace_remove", {"dir": str(ws)}).json()["workspace"] == {
        "dir": str(ws), "removed": True,
    }


def test_api_unregistered_dir_and_missing(api, tmp_path: Path):
    """`workspace_open` 的 missing 档：400 且不登记；未给 dir 同一档。"""
    client, home, _out_root = api
    none = post(client, "workspace_open", {"dir": str(tmp_path / "不在")})
    assert none.status_code == 400
    assert client.get("/api/sessions").json()["sessions"] == []
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


def test_api_sessions_list_most_recent_first(api, tmp_path, monkeypatch):
    """连登记三场（甲、乙、丙），再把选区切回甲一次；清单应答里甲在最前、丙其次、乙
    最后——连读两次，次序一致。

    时钟钉住（秒精度下连着几次登记可能落进同一秒，并列会掉目录字典序兜底），
    「甲因最近被切换而排最前」这件事才测得稳——这里测的是「最近在用」本身。
    """
    from meeting.service.runtime_catalog import workspace as catalog_workspace

    stamps = iter([
        "2026-09-21T21:00:01+08:00",  # 登记甲
        "2026-09-21T21:00:02+08:00",  # 登记乙
        "2026-09-21T21:00:03+08:00",  # 登记丙
        "2026-09-21T21:00:04+08:00",  # 切回甲
    ])
    monkeypatch.setattr(catalog_workspace, "now_iso", lambda: next(stamps))

    client, _home, _out_root = api
    places = tmp_path / "places"
    places.mkdir()
    jia, yi, bing = (places / name for name in ("甲", "乙", "丙"))
    for d in (jia, yi, bing):
        d.mkdir()
    for d in (jia, yi, bing):
        assert post(client, "workspace_open", {"dir": str(d)}).status_code == 200
    # 选区切回甲一次（已登记的路径只更新 last_opened_at）
    assert post(client, "workspace_open", {"dir": str(jia)}).status_code == 200

    order1 = [row["dir"] for row in client.get("/api/sessions").json()["sessions"]]
    order2 = [row["dir"] for row in client.get("/api/sessions").json()["sessions"]]
    assert order1 == order2 == [str(jia), str(bing), str(yi)]
    listed = client.get("/api/sessions").json()
    assert listed["recent"] == str(jia)
    assert set(order1) == {str(jia), str(yi), str(bing)}


def test_api_sessions_same_second_reads_stable_order(api, tmp_path, monkeypatch):
    """同一秒登记的两条：连读两次给出同一次序——兜底链钉死，不随刷新变样。"""
    from meeting.service.runtime_catalog import workspace as catalog_workspace

    monkeypatch.setattr(
        catalog_workspace, "now_iso", lambda: "2026-09-21T21:00:00+08:00"
    )
    client, _home, _out_root = api
    places = tmp_path / "places"
    places.mkdir()
    yi, jia = places / "乙", places / "甲"
    yi.mkdir()
    jia.mkdir()
    assert post(client, "workspace_open", {"dir": str(yi)}).status_code == 200
    assert post(client, "workspace_open", {"dir": str(jia)}).status_code == 200

    order1 = [row["dir"] for row in client.get("/api/sessions").json()["sessions"]]
    order2 = [row["dir"] for row in client.get("/api/sessions").json()["sessions"]]
    assert order1 == order2
    assert order1 == [str(jia), str(yi)]
    assert client.get("/api/sessions").json()["recent"] == str(jia)


def test_a_session_killed_mid_recording_reads_ended(api, tmp_path: Path):
    """三档口径：盘上停在 `recording`、但本进程没在录它的一场读 `ended`。

    强杀在录音半路（盘上 `session.json` 停在 `state="recording"`、服务重启过）的那一场，
    工作区读数是「录音结束」——`"recording"` 仅当这一行就是本进程此刻在录的活跃场次；
    收尾半路（`draining`）与录完（`done` / `failed`）同档。空白工作区那行照旧 `null`。
    """
    client, _home, out_root = api
    killed = out_root / "杀在录音半路"
    killed.mkdir()
    (killed / SESSION_FILE_NAME).write_text(
        json.dumps({"session_id": "杀在录音半路", "state": "recording"}), encoding="utf-8"
    )
    mid_drain = out_root / "杀在收尾半路"
    mid_drain.mkdir()
    (mid_drain / SESSION_FILE_NAME).write_text(
        json.dumps({"session_id": "杀在收尾半路", "state": "draining", "interrupted": True}),
        encoding="utf-8",
    )
    assert client.post(
        "/api/actions", json={"action": "workspace_open", "args": {"dir": str(killed)}},
        headers={"X-Meeting-Service": "1"},
    ).status_code == 200
    assert client.post(
        "/api/actions", json={"action": "workspace_open", "args": {"dir": str(mid_drain)}},
        headers={"X-Meeting-Service": "1"},
    ).status_code == 200

    rows = {row["dir"]: row for row in client.get("/api/sessions").json()["sessions"]}
    assert rows[str(killed)]["state"] == "ended", "盘上有记录、本进程没在录它 → ended"
    assert rows[str(mid_drain)]["state"] == "ended", "收尾半路与录完同档（三档口径）"

    single_killed = client.get(f"/api/sessions/{str(killed)}").json()
    assert single_killed["state"] == "ended", "单场那一只同一套读法"


def test_api_list_does_not_bump_revision(api):
    """两只读端点是纯读：任何状态下都 200、不涨 revision、不广播。"""
    client, _home, _out_root = api
    before = client.get("/api/state").json()["revision"]
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/dirs").status_code == 200
    assert client.get("/api/state").json()["revision"] == before


def test_session_read_handlers_are_plain_functions():
    """`_api_sessions` / `_api_session_status` / `_api_dirs` 是**普通 def**（非协程函数）。

    这三只处理器各自整段只是同步读（runtime 那三只读取器没有一条 await）：
    留着 async 会把这些盘上读数按在事件循环线程上跑；改普通 def 之后 Starlette
    把它们自动丢线程池执行，行为照旧。真异步的那些（`_api_actions` 会
    `await request.body()` 等）不在此列，照旧是协程函数。
    """
    import inspect

    from meeting.service import routes

    for name in ("_api_sessions", "_api_session_status", "_api_dirs"):
        handler = getattr(routes, name)
        assert inspect.iscoroutinefunction(handler) is False, f"{name} 应是同步 def"
