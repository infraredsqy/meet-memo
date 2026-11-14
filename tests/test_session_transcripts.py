"""`GET /api/sessions/{id}/transcripts` 那只稿子正文端点的用例（会后区的两份稿）。

**全部用假件**：只往临时目录里按真实形状落文件、经 `workspace_open` 登记，
零真实模型端点、零网络。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from meeting.service.routes import api_routes
from tests import _helpers as helpers

ACTION_HEADERS = {"X-Meeting-Service": "1"}


@pytest.fixture()
def client(tmp_path: Path):
    """进程内起真实应用的客户端（dev 采集 + 空钩子是夹具的共用缺省）。"""
    home = tmp_path / "home-transcripts"
    home.mkdir(parents=True, exist_ok=True)
    with helpers.app_client(home) as app_client:
        yield app_client


def _open(client, session: Path) -> None:
    """把这一场登记进工作台清单（成功才算现场成立）。"""
    opened = client.post(
        "/api/actions",
        json={"action": "workspace_open", "args": {"dir": str(session)}},
        headers=ACTION_HEADERS,
    )
    assert opened.status_code == 200, opened.text


def _make_session(tmp_path: Path, name: str, *, mode: str | None = "both") -> Path:
    """一场会：`session.json` 给 `mode`（`None` = 不落这份文件），目录里什么都不放。"""
    session = tmp_path / "transcripts" / name
    session.mkdir(parents=True)
    if mode is not None:
        (session / "session.json").write_text(
            json.dumps({"session_id": name, "mode": mode, "state": "done"}, ensure_ascii=False),
            encoding="utf-8",
        )
    return session


def _get(client, session: Path):
    return client.get(f"/api/sessions/{quote(str(session), safe='')}/transcripts")


def _kind(body: dict, kind: str) -> dict:
    return next(item for item in body["kinds"] if item["kind"] == kind)


def _source(kind_cell: dict, source: str) -> dict:
    return next(item for item in kind_cell["sources"] if item["source"] == source)


def test_the_route_is_registered() -> None:
    """路由注册点里那条路径在（与资料四条同一批，排在 `{dir:path}` 之前）。"""
    paths = [route.path for route in api_routes()]
    assert "/api/sessions/{dir:path}/transcripts" in paths


def test_both_roads_of_both_kinds_come_back_verbatim(tmp_path: Path, client) -> None:
    """四份文件都在：正文逐字原样给回，两处次序都是契约（live→offline、mic→loopback）。"""
    session = _make_session(tmp_path, "250906", mode="both")
    bodies = {
        "meeting_transcript_mic.md": "[20:01] 大家先过一下上周的结论\n[20:03] 行，那就这么定\n",
        "meeting_transcript_loopback.md": "[20:02] 远端这一句\n",
        "meeting_offline_transcript_mic.md": "[00:00] **S01**: 会后稿的一行\n",
        "meeting_offline_transcript_loopback.md": "[00:01] **S01**: 会后稿远端的一行\n",
    }
    for name, text in bodies.items():
        (session / name).write_text(text, encoding="utf-8")
    _open(client, session)

    response = _get(client, session)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dir"] == str(session)
    assert [item["kind"] for item in body["kinds"]] == ["live", "offline"]
    for kind in ("live", "offline"):
        cell = _kind(body, kind)
        assert [item["source"] for item in cell["sources"]] == ["mic", "loopback"]
    assert _source(_kind(body, "live"), "mic")["text"] == bodies["meeting_transcript_mic.md"]
    assert (
        _source(_kind(body, "live"), "loopback")["text"]
        == bodies["meeting_transcript_loopback.md"]
    )
    assert (
        _source(_kind(body, "offline"), "mic")["text"]
        == bodies["meeting_offline_transcript_mic.md"]
    )
    assert (
        _source(_kind(body, "offline"), "loopback")["text"]
        == bodies["meeting_offline_transcript_loopback.md"]
    )
    # 在的那一格只有 `text`，不摆缺稿的 `reason`（服务不摆没有数的空格）
    assert set(_source(_kind(body, "live"), "mic")) == {"source", "text"}


def test_missing_road_by_mode_says_unrecorded(tmp_path: Path, client) -> None:
    """`mode` 只采了一路：另一路的缺稿落 `unrecorded`（那一截音频从没录过）。"""
    session = _make_session(tmp_path, "260907", mode="mic")
    (session / "meeting_transcript_mic.md").write_text("[20:01] 只有本地一路\n", encoding="utf-8")
    _open(client, session)

    body = _get(client, session).json()
    live_mic = _source(_kind(body, "live"), "mic")
    assert live_mic["text"] == "[20:01] 只有本地一路\n"
    assert _source(_kind(body, "live"), "loopback") == {
        "source": "loopback",
        "reason": "unrecorded",
    }
    assert _source(_kind(body, "offline"), "mic") == {"source": "mic", "reason": "absent"}
    assert _source(_kind(body, "offline"), "loopback") == {
        "source": "loopback",
        "reason": "unrecorded",
    }


def test_missing_road_with_both_mode_says_absent(tmp_path: Path, client) -> None:
    """`mode` 两路都采：缺的那一份落 `absent`（文件不在，不是没采）。"""
    session = _make_session(tmp_path, "260908", mode="both")
    (session / "meeting_transcript_mic.md").write_text("[20:01] 本地有\n", encoding="utf-8")
    _open(client, session)

    body = _get(client, session).json()
    assert _source(_kind(body, "live"), "loopback") == {"source": "loopback", "reason": "absent"}


def test_no_files_at_all_says_absent_everywhere(tmp_path: Path, client) -> None:
    """一份稿都没有（强杀现场、正文一行都没落下）：四路全部如实 `absent`。"""
    session = _make_session(tmp_path, "260909", mode="both")
    _open(client, session)

    body = _get(client, session).json()
    for kind in ("live", "offline"):
        for source in ("mic", "loopback"):
            assert _source(_kind(body, kind), source) == {"source": source, "reason": "absent"}


def test_no_session_json_falls_back_to_both_roads(tmp_path: Path, client) -> None:
    """`session.json` 都没有的疑似目录：mode 判不出就按两路全集，缺稿落 `absent` 不落 `unrecorded`。"""
    session = _make_session(tmp_path, "260910", mode=None)
    _open(client, session)

    body = _get(client, session).json()
    reasons = {
        _source(_kind(body, kind), source).get("reason")
        for kind in ("live", "offline")
        for source in ("mic", "loopback")
    }
    assert reasons == {"absent"}


def test_bad_bytes_become_replacement_characters(tmp_path: Path, client) -> None:
    """半个坏字节折成替换字符（与纪要切块读稿同一口径），不让一份稿把端点带走。"""
    session = _make_session(tmp_path, "260911", mode="both")
    (session / "meeting_transcript_mic.md").write_bytes(
        b"[20:01] \xff\xfe" + "中".encode("utf-8") + b"\n"
    )
    _open(client, session)

    body = _get(client, session).json()
    text = _source(_kind(body, "live"), "mic")["text"]
    assert "\ufffd" in text
    assert "中" in text


def test_missing_dir_is_a_404_with_the_same_envelope(tmp_path: Path, client) -> None:
    """目录已不在原地 → 404 `session_dir_missing`（与单场状态那一只同一套失败口径）。"""
    session = _make_session(tmp_path, "250912", mode="both")
    _open(client, session)
    import shutil

    shutil.rmtree(session)

    response = _get(client, session)
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "session_dir_missing"
    assert body["detail"] == {"dir": str(session), "kind": "missing"}


def test_unregistered_dir_is_a_404(tmp_path: Path, client) -> None:
    """目录在但不在工作台清单里 → 404 `invalid_setting`（先登记再读）。"""
    session = _make_session(tmp_path, "260913", mode="both")

    response = _get(client, session)
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "invalid_setting"
    assert body["detail"]["dir"] == str(session)


def test_reading_is_pure(tmp_path: Path, client) -> None:
    """读稿是纯读：revision 不动、盘上一个字节不碰。"""
    session = _make_session(tmp_path, "260914", mode="both")
    (session / "meeting_transcript_mic.md").write_text("[20:01] 一行\n", encoding="utf-8")
    _open(client, session)

    before = client.get("/api/state").json()["revision"]
    assert _get(client, session).status_code == 200
    assert client.get("/api/state").json()["revision"] == before
