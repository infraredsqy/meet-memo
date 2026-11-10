from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest


from meeting import config as config_module
from meeting.backends.base import ReadOnlyConfig, TranscriptLine
from meeting.service import runtime as runtime_module
from meeting.service.hub import SUBSCRIBER_QUEUE_SIZE
from meeting.state import ENVELOPE_BAR_COUNT
from tests import _contract
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._contract import emit, normalize, normalize_frame
from tests._helpers import wait_until
from tests._service import SseClient, parse_sse_frame, post_action

from tests.test_contract_fixtures._common import (
    _act,
    _action_record,
    contract_regen,
    make_client,
)

#: 慢档标记（68927c27 收尾轮）：真服务/真样本采集的收尾验收，不进每次迭代圈。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 工作区注册表与目录选择器（工作台收编节点 e2f6166b）
# ---------------------------------------------------------------------------


def test_capture_workspace_group(tmp_path, make_client, monkeypatch, contract_regen):
    """`GET /api/sessions` 两份 + `workspace_open` / `workspace_remove` 两份成功体。

    注册表在 `~/.config/meeting/workspaces.json`（按路径去重）；每行的现场读数
    （`dir_exists` / `kind` / `suspected` / 盘上 `session.json` 的 `state`）由服务判，
    样本记的是**形状**：三个目的目录在采集用的临时根下造好（空目录 / 带 `session.json`
    的会议目录 / 有音频归档名没有 `session.json` 的疑似会议目录），路径与目录名一律
    掩成 `<path>` / `<session-id>`。清单按「最近在用」倒序给出（7058f2e1）——时间戳
    是判据，秒精度的墙钟连着三次登记可能落进同一秒，两次再生成的清单次序就会在
    「照登记倒序」与「目录字典序兜底」之间翻转（幂等硬要求红）；采集把登记时钟
    钉成三拍递增的固定值，样本里的清单次序因此永远确定。
    """
    client = make_client({"capture.backend": "dev"})

    from meeting.service.runtime_catalog import workspace as catalog_workspace

    stamps = iter([
        "2026-09-21T21:00:01+08:00",
        "2026-09-21T21:00:02+08:00",
        "2026-09-21T21:00:03+08:00",
    ])
    monkeypatch.setattr(catalog_workspace, "now_iso", lambda: next(stamps))

    # 空注册表的那一份（顶层第三格 `post_jobs`：此刻没有活儿在飞 → 空数组）
    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    empty_listing = listed.json()
    assert empty_listing == {"sessions": [], "recent": None, "post_jobs": []}

    # 造三个目的目录并逐个走「新会议」：空的 / 有会议材料的 / 疑似会议目录
    place = tmp_path / "contract-workspaces"
    place.mkdir()
    empty_dir = place / "empty_ws"
    empty_dir.mkdir()
    meeting_dir = place / "meeting_ws"
    meeting_dir.mkdir()
    # 段记录给两段（续接过的形状）：时间戳按 ISO 形状写（掩成 <iso>），
    # 秒数走掩码（`duration_seconds` 在 ZERO_KEYS 里，落 0）
    (meeting_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "meeting_ws",
                "state": "done",
                "date": "2026-09-21",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2026-09-21T10:00:00+08:00",
                        "ended_at": "2026-09-21T10:19:00+08:00",
                        "duration_seconds": 61.5,
                    },
                    {
                        "index": 2,
                        "started_at": "2026-09-21T11:30:00+08:00",
                        "ended_at": "2026-09-21T11:32:00+08:00",
                        "duration_seconds": 122.3,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    suspected_dir = place / "suspected_ws"
    suspected_dir.mkdir()
    (suspected_dir / "meeting_audio_mic.pcm").write_bytes(b"")
    # 一个非空又没有材料的目录：被确认之前拒（`invalid_setting` + confirm 提示）
    junk_dir = place / "junk_ws"
    junk_dir.mkdir()
    (junk_dir / "wj.pdf").write_bytes(b"%PDF")

    opened_empty = _act(client, "workspace_open", {"dir": str(empty_dir)})
    assert opened_empty.status_code == 200, opened_empty.text
    assert opened_empty.json()["workspace"]["kind"] == "empty"

    opened_meeting = _act(client, "workspace_open", {"dir": str(meeting_dir)})
    assert opened_meeting.status_code == 200, opened_meeting.text

    suspected = _act(client, "workspace_open", {"dir": str(suspected_dir)})
    assert suspected.status_code == 200, suspected.text
    assert suspected.json()["workspace"]["suspected"] is True

    confirm_required = _act(client, "workspace_open", {"dir": str(junk_dir)})
    assert confirm_required.status_code == 400
    assert confirm_required.json()["code"] == "invalid_setting"

    after = client.get("/api/sessions").json()
    assert len(after["sessions"]) == 3
    assert after["recent"] is not None
    # 删除的一档：从列表摘走（返回 `removed`），文件无人碰 —— 文件面归单测，这里只采形状
    removed = _act(client, "workspace_remove", {"dir": str(empty_dir)})
    assert removed.status_code == 200
    assert removed.json()["workspace"]["removed"] is True

    emit(
        {
            "sessions/empty.json": normalize(empty_listing),
            "sessions/after-open.json": normalize(after),
            "actions/workspace-open-ok.json": _action_record(
                opened_meeting, "workspace_open", {"dir": "<path>"}
            ),
            "actions/workspace-remove-ok.json": _action_record(
                removed, "workspace_remove", {"dir": "<path>"}
            ),
        },
        regen=contract_regen,
    )


def test_capture_dirs_group(tmp_path, make_client, contract_regen):
    """目录选择器那一档的样本（目录选择器节点）：`GET` 现场 + `POST` 新建目录三档。

    采样路径是**真实服务路径**：临时根下造两个子目录（空 / 杂物），打 `GET /api/dirs`
    采一只现场（`up` 到盘根才 null、`here` 是正在浏览这一层自己的名目）；再打
    `POST /api/dirs` 采成功体（`directory` 是新建那一行的名目判读）与两档拒绝
    （同名已存在 409 `dir_exists`、非法名 400 `invalid_setting`）。与资料那组同构：
    请求侧是**三件套**（`method` + `dir` + `name`），父层路径掩成 `<path>`、名字是
    确定值原样进样本。选择器的现场独立于四态快照，这一档不涨 `revision`（成功体里
    的 `revision` 只是当前版本号）。
    """
    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-dirs"
    place.mkdir()
    (place / "空会").mkdir()
    junk = place / "杂物间"
    junk.mkdir()
    (junk / "wj.pdf").write_bytes(b"%PDF")

    listed = client.get(f"/api/dirs?dir={quote(str(place), safe='')}")
    assert listed.status_code == 200, listed.text
    listing = listed.json()
    assert set(listing) == {"home", "cwd", "up", "here", "dirs"}
    assert listing["here"] == {
        "name": "contract-dirs", "dir": str(place), "kind": "unknown", "suspected": False,
    }
    assert listing["up"] == str(place.parent)
    assert [row["name"] for row in listing["dirs"]] == ["杂物间", "空会"]  # 按名字排序

    created = client.post(
        f"/api/dirs?dir={quote(str(place), safe='')}&name={quote('新会', safe='')}",
        headers={"X-Meeting-Service": "1"},
    )
    assert created.status_code == 200, created.text
    created_body = created.json()
    assert created_body["directory"]["kind"] == "empty"
    assert (place / "新会").is_dir()

    # 同名再建：409 dir_exists（这一片自己的 code，先例是 material_missing）
    exists = client.post(
        f"/api/dirs?dir={quote(str(place), safe='')}&name={quote('新会', safe='')}",
        headers={"X-Meeting-Service": "1"},
    )
    assert exists.status_code == 409, exists.text
    exists_body = exists.json()
    assert exists_body["code"] == "dir_exists"

    # 非法名：服务不清洗不改写，原样拒回
    illegal = client.post(
        f"/api/dirs?dir={quote(str(place), safe='')}&name={quote('../逃逸', safe='')}",
        headers={"X-Meeting-Service": "1"},
    )
    assert illegal.status_code == 400, illegal.text
    illegal_body = illegal.json()
    assert illegal_body["code"] == "invalid_setting"
    assert illegal_body["detail"]["key"] == "name"

    emit(
        {
            "dirs/listing.json": normalize(listing),
            "dirs/create-ok.json": {
                "method": "POST",
                "dir": "<path>",
                "name": "新会",
                "status": created.status_code,
                "body": normalize(created_body),
            },
            "dirs/fail-exists.json": {
                "method": "POST",
                "dir": "<path>",
                "name": "新会",
                "status": exists.status_code,
                "body": normalize(exists_body),
            },
            "dirs/fail-invalid-name.json": {
                "method": "POST",
                "dir": "<path>",
                "name": "../逃逸",
                "status": illegal.status_code,
                "body": normalize(illegal_body),
            },
        },
        regen=contract_regen,
    )


def test_capture_devices_group(tmp_path, make_client, monkeypatch, contract_regen) -> None:
    """`GET /api/devices` 那一份（设置抽屉的两个设备下拉，节点 `311f84f5`）。

    设备枚举在样本采集里**一律走假件**（`tests/conftest.py` 的看门只挡「真开设备」，
    清单端点的确定性靠这里把 `sources.py` 的两个枚举函数换成固定端点表）：名字与
    `is_default` 是确定值，原样进样本；**不开任何设备**这一点由端点实现本身保证
    （`devices_listing()` 只枚举，`probe()` 的试开不在它的路上）。
    """
    from meeting.capture import sources as sources_module
    from meeting.capture.devices import DeviceEndpoint

    fake_inputs = [
        DeviceEndpoint("假-Focusrite Analogue 1 + 2", "MME", "input", index=0, channels=4,
                       is_default=True),
        DeviceEndpoint("假-CABLE-Output (VB-Audio Virtual Cable)", "MME", "input", index=2,
                       channels=16),
    ]
    fake_renders = [
        DeviceEndpoint("假-扬声器 (Focusrite USB Audio)", "", "render", index=0),
        DeviceEndpoint("假-CABLE Input (VB-Audio Virtual Cable)", "", "render", index=1),
    ]
    monkeypatch.setattr(sources_module, "list_input_endpoints", lambda: list(fake_inputs))
    monkeypatch.setattr(sources_module, "list_render_endpoints", lambda: list(fake_renders))

    client = make_client({"capture.backend": "dev"})
    before = client.get("/api/state").json()

    listed = client.get("/api/devices")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert set(body) == {"mic", "loopback"}
    assert body["mic"][0]["name"] == "假-Focusrite Analogue 1 + 2"
    assert body["mic"][0]["is_default"] is True
    assert body["loopback"][1]["name"] == "假-CABLE Input (VB-Audio Virtual Cable)"
    # 纯读：不涨 revision、不动快照
    assert int(client.get("/api/state").json()["revision"]) == int(before["revision"])

    emit({"devices/all.json": normalize(body)}, regen=contract_regen)


def test_capture_session_status_group(tmp_path, make_client, contract_regen) -> None:
    """`GET /api/sessions/{id}` 那一份（会后视图的进度区，节点 `a9affaa8`）。

    这一份的**来源是真实服务路径上的只读端点**：场次目录里先按真实形状落下那份逐块
    进度记录（`meeting_offline_progress.jsonl`，两路各两块，含一块静音、一块失败）与
    状态文件，再把这一场登记进工作台、打一次那只端点。

    样本记的是**形状**：路径与场次目录名掩成 `<path>` / `<session-id>`，两个墙钟戳
    掩成 `<iso>`，走秒的量（`elapsed_seconds` / `wall_seconds`）掩成 `0`。块号、
    块数、四档计数、四段文字这些是**确定值**（造出来的记录是固定的），原样留着 ——
    它们正是这一份样本要给人看的形状。
    """
    from meeting.offline import artifacts as offline_artifacts
    from meeting.offline import progress as offline_progress

    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-session-status"
    place.mkdir()
    session_dir = place / "250906"
    session_dir.mkdir()
    # 段记录给一段（形状样本；时间戳按 ISO 形状写、秒数走掩码落 0）
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "250906",
                "state": "done",
                "date": "2025-09-06",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2025-09-06T20:00:00+08:00",
                        "ended_at": "2025-09-06T20:03:00+08:00",
                        "duration_seconds": 3.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 这一场有一份会后精细稿（`has_offline` 那一格因此为真）
    (session_dir / "meeting_offline_transcript_mic.md").write_text(
        "[00:00] **S01**: 会后稿的一行\n", encoding="utf-8"
    )
    # 那一份逐块记录：两路各两块，mic 的第 2 块没声跳过、loopback 的第 2 块没跑成
    offline_progress.write_progress(
        offline_progress.ledger_path(session_dir),
        session_id="250906",
        sources=["mic", "loopback"],
        rows=[
            offline_progress.ChunkProgress(
                "mic", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 第一块的一句话", 3, 45.247,
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "mic", 2, 450.0, 450.0, "silent",
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "loopback", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 这一路第一块的一句话", 2, 30.5,
            ).to_row(chunks_total=2),
            offline_progress.ChunkProgress(
                "loopback", 2, 450.0, 450.0, "failed",
                error="engine_error：RuntimeError: 这一块没过",
            ).to_row(chunks_total=2),
        ],
    )
    offline_artifacts.write_status(
        session_dir / offline_artifacts.STATUS_NAME,
        session_id="250906",
        state="done",
        started_at="2025-09-06T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
        finished_at="2025-09-06T20:02:30",
        partial=True,
    )

    opened = _act(client, "workspace_open", {"dir": str(session_dir)})
    assert opened.status_code == 200, opened.text

    queried = client.get(f"/api/sessions/{quote(str(session_dir), safe='')}")
    assert queried.status_code == 200, queried.text
    body = queried.json()
    assert body["offline"]["sources"][0]["total"] == 2

    emit(
        {"sessions/status-after-run.json": normalize(body)},
        regen=contract_regen,
    )


def test_capture_session_status_interrupted_group(tmp_path, make_client, contract_regen) -> None:
    """`GET /api/sessions/{id}` 的**被打断档**（b1c6a58e）：`offline.state` 照盘上答
    `interrupted`。

    采集形状与 `status-after-run` 同一条路：场次目录里落一份逐块记录（mic 两块里跑了
    一块、loopback 没跑）与一版 `interrupted` 状态文件，登记进工作台之后打那只端点。
    打断这一档要回答的是「停了、可以重按」：`offline.state` 是盘上自己的终态
    `interrupted`（折档行把它照盘透传，不许折回 `running`）、`in_process` 是 `false`。
    """
    from meeting.offline import artifacts as offline_artifacts
    from meeting.offline import progress as offline_progress

    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-session-interrupted"
    place.mkdir()
    session_dir = place / "260907"
    session_dir.mkdir()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": "260907",
                "state": "done",
                "date": "2026-09-07",
                "mode": "both",
                "segments": [
                    {
                        "index": 1,
                        "started_at": "2026-09-07T20:00:00+08:00",
                        "ended_at": "2026-09-07T20:03:00+08:00",
                        "duration_seconds": 3.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    offline_progress.write_progress(
        offline_progress.ledger_path(session_dir),
        session_id="260907",
        sources=["mic", "loopback"],
        rows=[
            offline_progress.ChunkProgress(
                "mic", 1, 0.0, 450.0, "transcribed",
                "[00:00] **c01:S01**: 被打断前跑完的那一块", 3, 41.7,
            ).to_row(chunks_total=2),
        ],
    )
    offline_artifacts.write_status(
        session_dir / offline_artifacts.STATUS_NAME,
        session_id="260907",
        state="interrupted",
        started_at="2026-09-07T20:00:00",
        sources=["mic", "loopback"],
        artifacts={},
        finished_at="2026-09-07T20:01:30",
        error="interrupted：跑到第 1/2 块被停下；没开跑：loopback",
    )

    opened = _act(client, "workspace_open", {"dir": str(session_dir)})
    assert opened.status_code == 200, opened.text

    queried = client.get(f"/api/sessions/{quote(str(session_dir), safe='')}")
    assert queried.status_code == 200, queried.text
    body = queried.json()
    assert body["offline"]["state"] == "interrupted"
    assert body["offline"]["in_process"] is False

    emit(
        {"sessions/status-interrupted.json": normalize(body)},
        regen=contract_regen,
    )


def test_capture_session_transcripts_group(tmp_path, make_client, contract_regen) -> None:
    """`GET /api/sessions/{id}/transcripts` 那只稿子正文端点的四档样本（本节点）。

    spec 完成判定点的四档：**两路都有 / 缺一路 / 两路都缺 / 目录不在**，逐份采集
    （`contract/README.md`的清单随改）。样本记的是**形状**：

    - `transcripts-both.json`：四份文件都在，正文逐字原样（`text` 是确定值）；
    - `transcripts-one-missing.json`：`mode = "mic"` 只采了一路——另一路的缺稿落
      `unrecorded`（那一截音频从没录过），会后稿 mic 那一路落 `absent`（采了但还没跑过）；
    - `transcripts-none.json`：`mode = "both"`、一份稿都没有（强杀现场）→ 四路全部
      如实 `absent`；
    - `transcripts-dir-missing.json`：登记过的目录被移走 → 404 + `session_dir_missing`
      的失败信封（`message` 里内嵌的路径走掩码）。

    路径掩 `<path>`；正文行是确定值原样留（`[20:01]` 这类分钟戳不是墙钟时刻）。
    """
    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-transcripts"
    place.mkdir()

    def _write_session_json(session: Path, mode: str) -> None:
        (session / "session.json").write_text(
            json.dumps(
                {"session_id": session.name, "mode": mode, "state": "done"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # -- 档 1：两路都有（四份文件都在，正文逐字原样）------------------------
    both_dir = place / "250906"
    both_dir.mkdir()
    _write_session_json(both_dir, "both")
    for name, text in {
        "meeting_transcript_mic.md": "[20:01] 大家先过一下上周的结论\n[20:03] 行，那就这么定\n",
        "meeting_transcript_loopback.md": "[20:02] 远端这一句\n",
        "meeting_offline_transcript_mic.md": "[00:00] **S01**: 会后稿本地的一行\n",
        "meeting_offline_transcript_loopback.md": "[00:01] **S01**: 会后稿远端的一行\n",
    }.items():
        (both_dir / name).write_text(text, encoding="utf-8")
    _act(client, "workspace_open", {"dir": str(both_dir)})
    got = client.get(f"/api/sessions/{quote(str(both_dir), safe='')}/transcripts")
    assert got.status_code == 200, got.text
    both_body = got.json()
    # 结构断言（掩码前）：两份稿 × 两路的次序都是契约
    assert [item["kind"] for item in both_body["kinds"]] == ["live", "offline"]
    for kind_cell in both_body["kinds"]:
        assert [item["source"] for item in kind_cell["sources"]] == ["mic", "loopback"]
        for cell in kind_cell["sources"]:
            assert set(cell) == {"source", "text"}

    # -- 档 2：缺一路（mode = mic → 另一路 unrecorded；会后稿 mic 还没跑过 → absent）
    one_dir = place / "260907"
    one_dir.mkdir()
    _write_session_json(one_dir, "mic")
    (one_dir / "meeting_transcript_mic.md").write_text(
        "[20:01] 只有本地一路\n", encoding="utf-8"
    )
    _act(client, "workspace_open", {"dir": str(one_dir)})
    got = client.get(f"/api/sessions/{quote(str(one_dir), safe='')}/transcripts")
    assert got.status_code == 200, got.text
    one_body = got.json()
    live = next(item for item in one_body["kinds"] if item["kind"] == "live")
    offline = next(item for item in one_body["kinds"] if item["kind"] == "offline")
    live_cells = {cell["source"]: cell for cell in live["sources"]}
    offline_cells = {cell["source"]: cell for cell in offline["sources"]}
    assert live_cells["loopback"] == {"source": "loopback", "reason": "unrecorded"}
    assert offline_cells["mic"] == {"source": "mic", "reason": "absent"}
    assert offline_cells["loopback"] == {"source": "loopback", "reason": "unrecorded"}

    # -- 档 3：两路都缺（mode = both、一份稿都没有 → 四路全部 absent）--------
    none_dir = place / "260908"
    none_dir.mkdir()
    _write_session_json(none_dir, "both")
    _act(client, "workspace_open", {"dir": str(none_dir)})
    got = client.get(f"/api/sessions/{quote(str(none_dir), safe='')}/transcripts")
    assert got.status_code == 200, got.text
    none_body = got.json()
    for kind_cell in none_body["kinds"]:
        for cell in kind_cell["sources"]:
            assert cell == {"source": cell["source"], "reason": "absent"}

    # -- 档 4：目录不在（登记过的目录被移走 → 404 session_dir_missing）------
    gone_dir = place / "260909"
    gone_dir.mkdir()
    _write_session_json(gone_dir, "both")
    _act(client, "workspace_open", {"dir": str(gone_dir)})
    import shutil as _shutil

    _shutil.rmtree(gone_dir)
    gone = client.get(f"/api/sessions/{quote(str(gone_dir), safe='')}/transcripts")
    assert gone.status_code == 404, gone.text
    gone_body = gone.json()
    assert gone_body["ok"] is False
    assert gone_body["code"] == "session_dir_missing"
    assert gone_body["detail"] == {"dir": gone_body["detail"]["dir"], "kind": "missing"}

    emit(
        {
            "sessions/transcripts-both.json": {
                "method": "GET",
                "dir": "<path>",
                "status": 200,
                "body": normalize(both_body),
            },
            "sessions/transcripts-one-missing.json": {
                "method": "GET",
                "dir": "<path>",
                "status": 200,
                "body": normalize(one_body),
            },
            "sessions/transcripts-none.json": {
                "method": "GET",
                "dir": "<path>",
                "status": 200,
                "body": normalize(none_body),
            },
            "sessions/transcripts-dir-missing.json": {
                "method": "GET",
                "dir": "<path>",
                "status": gone.status_code,
                "body": normalize(gone_body),
            },
        },
        regen=contract_regen,
    )


def test_capture_materials_group(tmp_path, make_client, contract_regen) -> None:
    """会议资料四条端点的样本（资料节点 285bf58c）：上传 / 清单 / 删除 / 取不到。

    采样路径是**真实服务路径**：往临时根下造一个目的目录、经 `workspace_open` 登记，
    然后打 `/api/sessions/{id}/materials` 那四条端点。样本记的是**形状**：

    - 上传三连：干净名字原样存、重名自动加序号后缀（`报告.pdf` → `报告 (2).pdf`）、
      穿越加非法字符的脏名字清洗后落盘 —— 名字是**确定值**，原样留在样本里；
    - 清单一条三格（`name` / `size_bytes` / `added_at`），贴上时刻走 `<iso>` 掩码、
      路径走 `<path>` 掩码、字节数是确定值；
    - 删除成功体与「取不到」的 404 失败体（`material_missing`）各一份。

    资料层独立于快照：这一档不提交、不涨 `revision`，样本里的 `revision` 是登记那一刻
    的现值（确定）。
    """
    client = make_client({"capture.backend": "dev"})

    place = tmp_path / "contract-materials"
    place.mkdir()
    session = place / "250906"
    session.mkdir()
    opened = _act(client, "workspace_open", {"dir": str(session)})
    assert opened.status_code == 200, opened.text

    base = f"/api/sessions/{quote(str(session), safe='')}/materials"

    first = client.post(
        f"{base}?name={quote('报告.pdf', safe='')}",
        content=b"%PDF-1.4\ncontract fixture bytes\n",
        headers={"X-Meeting-Service": "1"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert set(first_body) == {"ok", "revision", "material"}
    assert set(first_body["material"]) == {"name", "size_bytes", "added_at"}
    assert first_body["material"]["name"] == "报告.pdf"
    assert first_body["material"]["size_bytes"] == len(b"%PDF-1.4\ncontract fixture bytes\n")

    duplicate = client.post(
        f"{base}?name={quote('报告.pdf', safe='')}",
        content=b"duplicate bytes",
        headers={"X-Meeting-Service": "1"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["material"]["name"] == "报告 (2).pdf"

    hostile = client.post(
        f"{base}?name={quote('..\\假报告<v>?.pdf ', safe='')}",
        content=b"cleaned bytes",
        headers={"X-Meeting-Service": "1"},
    )
    assert hostile.status_code == 200, hostile.text
    assert hostile.json()["material"]["name"] == "假报告_v__.pdf"

    listed = client.get(base)
    assert listed.status_code == 200
    listing = listed.json()
    assert set(listing) == {"dir", "materials_dir", "items"}
    assert [row["name"] for row in listing["items"]] == [
        "假报告_v__.pdf", "报告 (2).pdf", "报告.pdf",
    ]
    for row in listing["items"]:
        assert set(row) == {"name", "size_bytes", "added_at"}

    fetched = client.get(f"{base}/{quote('报告.pdf', safe='')}")
    assert fetched.status_code == 200
    assert fetched.content == b"%PDF-1.4\ncontract fixture bytes\n"
    assert fetched.headers["content-type"].startswith("application/pdf")

    deleted = client.delete(
        f"{base}/{quote('报告 (2).pdf', safe='')}", headers={"X-Meeting-Service": "1"}
    )
    assert deleted.status_code == 200, deleted.text
    deleted_body = deleted.json()
    assert deleted_body["material"] == {"name": "报告 (2).pdf"}

    missing = client.get(f"{base}/{quote('报告 (2).pdf', safe='')}")
    assert missing.status_code == 404
    missing_body = missing.json()
    assert missing_body["ok"] is False
    assert missing_body["code"] == "material_missing"
    assert missing_body["detail"] == {"name": "报告 (2).pdf"}

    emit(
        {
            "materials/upload-ok.json": {
                "method": "POST",
                "name": "报告.pdf",
                "status": first.status_code,
                "body": normalize(first_body),
            },
            "materials/after-upload.json": normalize(listing),
            "materials/delete-ok.json": {
                "method": "DELETE",
                "name": "报告 (2).pdf",
                "status": deleted.status_code,
                "body": normalize(deleted_body),
            },
            "materials/missing.json": {
                "method": "GET",
                "name": "报告 (2).pdf",
                "status": missing.status_code,
                "body": normalize(missing_body),
            },
        },
        regen=contract_regen,
    )


