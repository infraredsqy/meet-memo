"""会议资料四条端点的用例（资料节点 285bf58c）：上传 / 清单 / 取原文件 / 删除。

本文件盯住七件事：

  - **上传**：原样字节落盘、重名自动加序号后缀（用户看到的名字保留）、文件名清洗
    （穿越 / 非法字符 / 保留设备名 / 结尾点与空格）、`name` 必给；
  - **目录校验拒绝**：只认两种目标目录——当前活跃场次、注册表登记过的场次；
    未登记与目录不在原地都拒收（404），且**盘上不落任何新东西**；
  - **活跃场次**：一边录一边传照收——哪怕那一行刚被 `workspace_remove` 摘出清单
    （资料跟着场次走，不跟着清单行走）；
  - **清单**：目录就是清单（没有 JSON 记录），没传过就是空数组且**不建目录**；
  - **取原文件**：字节原样、`Content-Type` 按扩展名（认不出 `application/octet-stream`）；
  - **删除**：只删那一个文件；重复删除如实 404；
  - **独立于快照 + 流式**：不涨 `revision`、快照九键不动；几 MB 级上传照常落盘；
    写请求缺 `X-Meeting-Service: 1` 被中间件的头哨拦下（403）。

**全部走假件**（项目测试纪律）：采集用 dev 替身（`app_client` 的共用缺省），
一条真实模型端点请求都不发、不碰任何声卡。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from meeting.service.routes import api_routes
from tests import _helpers as helpers

#: 流式大文件那一档的数据量：几 MB 级够验「收到多少写多少」，不造几百 MB 的用例
LARGE_UPLOAD_BYTES = 4 * 1024 * 1024


@pytest.fixture()
def materials_client(tmp_path: Path):
    """进程内起真实应用的客户端（dev 采集替身），另给一个场次放置根。"""
    import contextlib

    home = tmp_path / "home-materials"
    home.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home, values={}) as client:
        client.place_root = tmp_path / "place-materials"
        client.place_root.mkdir(parents=True, exist_ok=True)
        yield client


def _open_session(client, name: str = "250906") -> Path:
    """造一个目录并登记进工作台清单（`workspace_open` 走真动作端点）。"""
    session = client.place_root / name
    session.mkdir(parents=True, exist_ok=True)
    opened = client.post(
        "/api/actions",
        json={"action": "workspace_open", "args": {"dir": str(session)}},
        headers={"X-Meeting-Service": "1"},
    )
    assert opened.status_code == 200, opened.text
    return session


def _materials_base(session: Path) -> str:
    return f"/api/sessions/{quote(str(session), safe='')}/materials"


def _upload(client, session: Path, name: str, content: bytes):
    return client.post(
        f"{_materials_base(session)}?name={quote(name, safe='')}",
        content=content,
        headers={"X-Meeting-Service": "1"},
    )


def _materials_dir(session: Path) -> Path:
    return session / "meeting_materials"


# ---------------------------------------------------------------------------
# 注册与寻址
# ---------------------------------------------------------------------------


def test_the_four_routes_are_registered_before_the_session_catch_all() -> None:
    """资料四条注册在 `{dir:path}` 之前（注册在后会被星形路由整个吃进 `{dir}`）。"""
    paths = [route.path for route in api_routes()]
    assert paths.index("/api/sessions/{dir:path}/materials") < paths.index(
        "/api/sessions/{dir:path}"
    )
    assert paths.index("/api/sessions/{dir:path}/materials/{name}") < paths.index(
        "/api/sessions/{dir:path}"
    )


def test_summaries_dirs_and_upload_keep_the_runtime_missing_envelope() -> None:
    """未装配 runtime 时三只端点回 500 JSON 信封（与 `/api/state` 等 12 只同形）。

    只挂路由与头哨中间件、`app.state` 不放 runtime：`GET /api/summaries`、
    `GET /api/dirs`、`POST …/materials` 三只由 `_needs_runtime` 的未装配档兜住，
    回 `{"error": "runtime 未装配"}`，不漏成 Starlette 裸 500。上传那条带头哨
    放行的自定义头——请求真走到装饰器才被兜底接住。
    """
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from meeting.service.app import _LocalGuardsMiddleware

    app = Starlette(routes=api_routes())
    app.add_middleware(_LocalGuardsMiddleware)
    client = TestClient(app, base_url="http://127.0.0.1")
    for method, path in (
        ("get", "/api/summaries"),
        ("get", "/api/dirs"),
        ("post", "/api/sessions/x/materials"),
    ):
        headers = {"X-Meeting-Service": "1"} if method == "post" else {}
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 500, f"{method.upper()} {path}: {response.text}"
        assert response.json() == {"error": "runtime 未装配"}, (
            f"{method.upper()} {path}: {response.text}"
        )


def test_an_unregistered_dir_is_rejected_without_touching_the_disk(
    materials_client,
) -> None:
    """未登记、又不是活跃场次 → 404 `invalid_setting`；目录不存在 → 404 `session_dir_missing`。

    拒收路径**盘上不落任何新东西**：连 `meeting_materials/` 都不许冒出来。
    """
    stranger = materials_client.place_root / "stranger"
    stranger.mkdir()
    gone = materials_client.place_root / "never-existed"

    for target, code in ((stranger, "invalid_setting"), (gone, "session_dir_missing")):
        uploaded = _upload(materials_client, target, "x.pdf", b"x")
        assert uploaded.status_code == 404, uploaded.text
        assert uploaded.json()["ok"] is False
        assert uploaded.json()["code"] == code
        listed = materials_client.get(_materials_base(target))
        assert listed.status_code == 404
        deleted = materials_client.delete(
            f"{_materials_base(target)}/{quote('x.pdf', safe='')}",
            headers={"X-Meeting-Service": "1"},
        )
        assert deleted.status_code == 404

    assert not (stranger / "meeting_materials").exists(), "拒收路径不建资料目录"


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def test_upload_stores_bytes_verbatim_and_reports_the_stored_name(
    materials_client,
) -> None:
    """原样字节落盘；成功体 `{ok, revision, material:{name, size_bytes, added_at}}`。

    `added_at`（贴上时刻）取自落盘那一刻的 mtime——**不保留原文件的时间戳**。
    """
    session = _open_session(materials_client)
    payload = b"%PDF-1.4\n" + "假的会议资料\n".encode("utf-8")

    before = datetime.now().astimezone()
    uploaded = _upload(materials_client, session, "报告.pdf", payload)
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert set(body) == {"ok", "revision", "material"}
    assert body["ok"] is True
    material = body["material"]
    assert set(material) == {"name", "size_bytes", "added_at"}
    assert material["name"] == "报告.pdf"
    assert material["size_bytes"] == len(payload)

    stored = _materials_dir(session) / "报告.pdf"
    assert stored.is_file(), "首次上传时由服务创建 meeting_materials/"
    assert stored.read_bytes() == payload, "原样字节：一个字节都不动"

    stamped = datetime.fromisoformat(material["added_at"])
    assert abs((stamped - before).total_seconds()) < 120, "贴上时刻就是落盘时刻"
    disk_mtime = datetime.fromtimestamp(stored.stat().st_mtime).astimezone()
    assert abs((stamped - disk_mtime).total_seconds()) <= 1, "added_at 与盘上 mtime 同源"

    # 目录就是清单：没有记录文件，资料目录里只有那一份上传
    assert {item.name for item in _materials_dir(session).iterdir()} == {"报告.pdf"}


def test_duplicate_names_get_sequence_suffixes(materials_client) -> None:
    """重名自动加序号后缀、保留用户看到的名字：`报告.pdf` → `报告 (2).pdf` → `(3)`。"""
    session = _open_session(materials_client)

    names = [
        _upload(materials_client, session, "报告.pdf", b"first").json()["material"]["name"],
        _upload(materials_client, session, "报告.pdf", b"second").json()["material"]["name"],
        _upload(materials_client, session, "报告.pdf", b"third").json()["material"]["name"],
    ]
    assert names == ["报告.pdf", "报告 (2).pdf", "报告 (3).pdf"]
    assert (_materials_dir(session) / "报告 (2).pdf").read_bytes() == b"second"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("..\\..\\session.json", "session.json"),  # 穿越：只留最后一段
        ("report<v>?.pdf", "report_v__.pdf"),  # Windows 非法字符
        ("CON.pdf", "_CON.pdf"),  # 保留设备名（按扩展名前那段判）
        ("NUL", "_NUL"),
        ("name .pdf ", "name .pdf"),  # 结尾空格掐掉、中间原样
        ("quieter port.txt.. ", "quieter port.txt"),  # 结尾点与空格一起收掉
        ("autoexec.cfg", "autoexec.cfg"),  # 干净名字原样通过
    ],
)
def test_upload_cleans_hostile_names(
    materials_client, tmp_path: Path, raw: str, stored: str
) -> None:
    """上传的文件名不可信：清洗之后永远落在 `meeting_materials/` 这一层。"""
    session = _open_session(materials_client)

    uploaded = _upload(materials_client, session, raw, b"cleaned")
    assert uploaded.status_code == 200, uploaded.text
    material = uploaded.json()["material"]
    assert material["name"] == stored

    target = _materials_dir(session) / stored
    assert target.read_bytes() == b"cleaned"
    assert target.parent == _materials_dir(session).resolve(), "解析结果不越出资料目录"
    assert not (session / "session.json").exists(), "穿越名够不着场次目录本层"


def test_upload_requires_a_name(materials_client) -> None:
    """`name` 必给（空白也算没给）→ 400 `invalid_setting`，`detail.key` 点名 `name`。"""
    session = _open_session(materials_client)
    base = _materials_base(session)

    missing = materials_client.post(
        base, content=b"x", headers={"X-Meeting-Service": "1"}
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "invalid_setting"
    assert missing.json()["detail"]["key"] == "name"

    blank = materials_client.post(
        f"{base}?name={quote('   ', safe='')}",
        content=b"x",
        headers={"X-Meeting-Service": "1"},
    )
    assert blank.status_code == 400
    assert blank.json()["detail"]["key"] == "name"
    assert not _materials_dir(session).exists()


def test_the_active_session_accepts_uploads_while_recording(materials_client) -> None:
    """录制中的活跃场次照收——哪怕那一行刚被摘出清单（活跃与登记是两档并列的准入）。

    `start` 会顺手登记注册表，这里用 `workspace_remove` 把那一行摘掉（只摘名单、
    文件不动），剩下的准入判据就只有「当前活跃场次」这一档。
    """
    from tests._helpers import wait_until

    session = materials_client.place_root / "live-260910"
    session.mkdir(parents=True, exist_ok=True)
    started = materials_client.post(
        "/api/actions",
        json={"action": "start", "args": {"mode": "both", "dir": str(session)}},
        headers={"X-Meeting-Service": "1"},
    )
    assert started.status_code == 200, started.text
    wait_until(
        lambda: materials_client.get("/api/state").json()["top_state"] == "recording",
        timeout=4.0,
        what="dev 替身开一场进 recording",
    )

    removed = materials_client.post(
        "/api/actions",
        json={"action": "workspace_remove", "args": {"dir": str(session)}},
        headers={"X-Meeting-Service": "1"},
    )
    assert removed.status_code == 200

    uploaded = _upload(materials_client, session, "现场补充.txt", "录制中贴进来的资料".encode("utf-8"))
    assert uploaded.status_code == 200, uploaded.text
    assert (_materials_dir(session) / "现场补充.txt").read_bytes() == "录制中贴进来的资料".encode("utf-8")


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def test_listing_is_the_directory_itself(materials_client) -> None:
    """没传过 = `items: []` 且**不建目录**；传过 = 名字 / 大小 / 贴上时刻，按名字排序。"""
    session = _open_session(materials_client)

    empty = materials_client.get(_materials_base(session))
    assert empty.status_code == 200
    listing = empty.json()
    assert set(listing) == {"dir", "materials_dir", "items"}
    assert listing["items"] == []
    assert not _materials_dir(session).exists(), "清单不建目录（首次上传才建）"

    _upload(materials_client, session, "b.pdf", b"bb")
    _upload(materials_client, session, "a.pdf", b"a")

    after = materials_client.get(_materials_base(session)).json()
    assert [row["name"] for row in after["items"]] == ["a.pdf", "b.pdf"]
    sizes = {row["name"]: row["size_bytes"] for row in after["items"]}
    assert sizes == {"a.pdf": 1, "b.pdf": 2}
    for row in after["items"]:
        assert set(row) == {"name", "size_bytes", "added_at"}


# ---------------------------------------------------------------------------
# 取原文件
# ---------------------------------------------------------------------------


def test_fetch_returns_original_bytes_with_a_extension_content_type(
    materials_client,
) -> None:
    """字节原样；`Content-Type` 按扩展名判（认不出 `application/octet-stream`）。"""
    session = _open_session(materials_client)
    payload = b"%PDF-1.4\n" + "原样取回\n".encode("utf-8")
    _upload(materials_client, session, "讲义.pdf", payload)
    _upload(materials_client, session, "神秘物件.x9z", b"\x00\x01binary")

    fetched = materials_client.get(
        f"{_materials_base(session)}/{quote('讲义.pdf', safe='')}"
    )
    assert fetched.status_code == 200
    assert fetched.content == payload
    assert fetched.headers["content-type"].startswith("application/pdf")

    unknown = materials_client.get(
        f"{_materials_base(session)}/{quote('神秘物件.x9z', safe='')}"
    )
    assert unknown.status_code == 200
    assert unknown.content == b"\x00\x01binary"
    assert unknown.headers["content-type"].startswith("application/octet-stream")


def test_fetching_a_missing_or_hostile_name_is_a_404(materials_client) -> None:
    """存不在的名字 → 404 `material_missing`；穿越名清洗后同样落在这个失败档（够不着外面）。"""
    session = _open_session(materials_client)
    base = _materials_base(session)

    missing = materials_client.get(f"{base}/{quote('没有的这一份.pdf', safe='')}")
    assert missing.status_code == 404
    body = missing.json()
    assert body["ok"] is False
    assert body["code"] == "material_missing"
    assert body["detail"] == {"name": "没有的这一份.pdf"}

    # URL 里手写穿越段：清洗剥成裸文件名 → 资料目录里没有 → 同一个 404，够不着 session.json
    traversal = materials_client.get(f"{base}/{quote('..\\..\\session.json', safe='')}")
    assert traversal.status_code == 404
    assert traversal.json()["code"] == "material_missing"


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_removes_exactly_one_file(materials_client) -> None:
    """删除只动那一个文件；重复删除如实 404（`material_missing`）。"""
    session = _open_session(materials_client)
    _upload(materials_client, session, "留着的.pdf", b"keep")
    _upload(materials_client, session, "删掉的.pdf", b"drop")

    deleted = materials_client.delete(
        f"{_materials_base(session)}/{quote('删掉的.pdf', safe='')}",
        headers={"X-Meeting-Service": "1"},
    )
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert set(body) == {"ok", "revision", "material"}
    assert body["material"] == {"name": "删掉的.pdf"}

    assert (_materials_dir(session) / "留着的.pdf").is_file()
    assert not (_materials_dir(session) / "删掉的.pdf").exists()

    again = materials_client.delete(
        f"{_materials_base(session)}/{quote('删掉的.pdf', safe='')}",
        headers={"X-Meeting-Service": "1"},
    )
    assert again.status_code == 404
    assert again.json()["code"] == "material_missing"


# ---------------------------------------------------------------------------
# 流式大文件 / 快照独立 / 头哨
# ---------------------------------------------------------------------------


def test_a_few_megabyte_upload_streams_through(materials_client) -> None:
    """几 MB 级上传照常落盘：字节逐段写（分块发送）、盘上逐字节一致。"""
    session = _open_session(materials_client)
    payload = os.urandom(LARGE_UPLOAD_BYTES)
    chunks = [payload[offset : offset + 65536] for offset in range(0, len(payload), 65536)]

    uploaded = materials_client.post(
        f"{_materials_base(session)}?name={quote('大块头.bin', safe='')}",
        content=iter(chunks),
        headers={"X-Meeting-Service": "1"},
    )
    assert uploaded.status_code == 200, uploaded.text
    material = uploaded.json()["material"]
    assert material["size_bytes"] == LARGE_UPLOAD_BYTES
    assert material["name"] == "大块头.bin"

    stored = _materials_dir(session) / "大块头.bin"
    assert stored.stat().st_size == LARGE_UPLOAD_BYTES
    assert stored.read_bytes() == payload, "原样字节：几 MB 也一个字节不差"


def test_materials_endpoints_leave_the_snapshot_alone(materials_client) -> None:
    """资料层独立于四态快照：上传 / 清单 / 取 / 删除都不涨 `revision`、九键不动。"""
    session = _open_session(materials_client)
    runtime = materials_client.app.state.runtime
    snapshot_before = runtime.to_dict()

    assert _upload(materials_client, session, "x.bin", b"12345").status_code == 200
    assert materials_client.get(_materials_base(session)).status_code == 200
    assert (
        materials_client.get(f"{_materials_base(session)}/{quote('x.bin', safe='')}").status_code
        == 200
    )
    assert (
        materials_client.delete(
            f"{_materials_base(session)}/{quote('x.bin', safe='')}",
            headers={"X-Meeting-Service": "1"},
        ).status_code
        == 200
    )

    assert runtime.revision == snapshot_before["revision"], "纯读写文件，不涨 revision"
    snapshot_after = runtime.to_dict()
    assert set(snapshot_after) == set(snapshot_before)
    assert snapshot_after == snapshot_before, "快照九键与内容一个字不动"


def test_materials_writes_require_the_custom_header(materials_client) -> None:
    """上传与删除缺 `X-Meeting-Service: 1` → 403（中间件头哨扩到资料写端点）；读不拦。"""
    session = _open_session(materials_client)
    base = _materials_base(session)

    bare_upload = materials_client.post(f"{base}?name=x.txt", content=b"x")
    assert bare_upload.status_code == 403
    assert "X-Meeting-Service" in bare_upload.text

    bare_delete = materials_client.delete(f"{base}/{quote('x.txt', safe='')}")
    assert bare_delete.status_code == 403

    assert not _materials_dir(session).exists(), "被拦下的写请求盘上不落东西"

    # 纯读那两条与其余 GET 同档：Host 校验已盖住，不要求自定义头
    assert materials_client.get(base).status_code == 200

    # 头带齐之后照常放行
    assert (
        materials_client.post(
            f"{base}?name=x.txt", content=b"x", headers={"X-Meeting-Service": "1"}
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 选择器文件模式的盘上拷入（160d1a78）：`?name=<名>&from=<路径>`
# ---------------------------------------------------------------------------


def _upload_from(client, session: Path, name: str, source: Path):
    return client.post(
        f"{_materials_base(session)}?name={quote(name, safe='')}"
        f"&from={quote(str(source), safe='')}",
        headers={"X-Meeting-Service": "1"},
    )


def test_upload_from_copies_an_existing_file_verbatim(materials_client) -> None:
    """`from` 档：盘上那个已有文件**原样字节流式拷入**，清洗/重名/头哨与正文上传同一套。

    资料界面的选择器文件模式挑中的是盘上的路径，界面上没有那些字节——拷入这一下
    归服务做。源文件一个字节不动；成功体与正文上传同一形状。
    """
    session = _open_session(materials_client)
    source = materials_client.place_root / "季度报表.pdf"
    payload = b"%PDF-1.4\n" + "从盘上拷进来的那份\n".encode("utf-8")
    source.write_bytes(payload)
    source_mtime = source.stat().st_mtime

    bare = materials_client.post(
        f"{_materials_base(session)}?name=x.pdf&from={quote(str(source), safe='')}"
    )
    assert bare.status_code == 403, "from 档也是写端点：头哨照拦"

    uploaded = _upload_from(materials_client, session, "季度报表.pdf", source)
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert set(body) == {"ok", "revision", "material"}
    material = body["material"]
    assert material["name"] == "季度报表.pdf"
    assert material["size_bytes"] == len(payload)

    stored = _materials_dir(session) / "季度报表.pdf"
    assert stored.read_bytes() == payload, "原样字节拷入"
    assert source.read_bytes() == payload, "源文件一个字节不动"
    assert source.stat().st_mtime == source_mtime

    # 清单里看得到这一份（目录就是清单）
    listed = materials_client.get(_materials_base(session))
    assert [item["name"] for item in listed.json()["items"]] == ["季度报表.pdf"]


def test_upload_from_a_missing_file_is_a_400_and_lands_nothing(materials_client) -> None:
    """`from` 给到不存在的文件：400 `invalid_setting`（detail.key = "from"），盘上不落任何东西。"""
    session = _open_session(materials_client)
    missing = materials_client.place_root / "没有这份.pdf"

    uploaded = _upload_from(materials_client, session, "资料.pdf", missing)
    assert uploaded.status_code == 400
    body = uploaded.json()
    assert body["code"] == "invalid_setting"
    assert body["detail"]["key"] == "from"
    assert not _materials_dir(session).exists(), "源头验不过就什么都不落"


def test_upload_from_duplicate_names_get_sequence_suffixes(materials_client) -> None:
    """`from` 档的重名让位与正文上传同一套：`a.md` 已在 → 拷入同名落 `a (2).md`。"""
    session = _open_session(materials_client)
    source = materials_client.place_root / "来源.md"
    source.write_bytes("第二批\n".encode("utf-8"))

    assert _upload_from(materials_client, session, "纪要.md", source).status_code == 200
    uploaded = _upload_from(materials_client, session, "纪要.md", source)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["material"]["name"] == "纪要 (2).md"
