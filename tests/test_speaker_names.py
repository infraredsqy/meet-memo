"""人名表（节点 `91b6752e`）的用例：文件契约、盘上身份集、改名动作、单场状态的 `speakers` 一格。

R10 第四条 / R30 的三条口径各有一组用例盯着：

  - **存在这一场的目录里**：文件落在场次目录根（`meeting_speaker_names.json`），
    一份记两路（键里带路，两路的「1 号」写不出同一个键）；
  - **只存确认过的 (编号, 名字) 对**：没起过名的编号不进文件，跳过 = 不落任何状态；
  - **读不出不挡下游**：人名表坏了记一行、如实当作「没有确认过的名字」，
    提示词不带表、单场状态的名字一格 `null`，不报错、不改结论。

改名动作（`speaker_rename`）的用例全走真运行时 + 真目录：**不拉离线、不拉纪要**
（R26：动作只写表），断言 `post_job` 始终是空的。
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.offline.artifacts import (
    SPEAKERS_NAME_TEMPLATE,
    build_speakers_document,
    write_speakers,
)
from meeting.service.app import build_runtime
from meeting.service.runtime import ActionError
from meeting.speaker_names import (
    KNOWN_SCHEMA_MAJOR,
    NAMES_FILE_NAME,
    SCHEMA_VERSION,
    SpeakerNamesError,
    build_names_document,
    identity_rows,
    names_path,
    parse_names_document,
    qualified_key_shape,
    read_names_entries,
    set_speaker_name,
)


class _LogRecorder(logging.Handler):
    """直接挂到被测 logger 上的记录器（`meeting` 那一层 `propagate = False`，`caplog` 收不到）。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _captured_speaker_names_log():
    """抓 `meeting.speaker_names` 自己那几行 log（记一行 = 判据，读实测文本）。"""
    logger = logging.getLogger("meeting.speaker_names")
    recorder = _LogRecorder()
    logger.addHandler(recorder)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)

# ---------------------------------------------------------------------------
# 一、文件契约：形状、版本、坏文件
# ---------------------------------------------------------------------------


def test_build_and_parse_round_trip_preserves_the_confirmed_pairs() -> None:
    """写出去与读回来是同一份：版本、键序、键值逐格对上。"""
    document = build_names_document([("mic:S01", "老王"), ("loopback", "小李")])

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["names"] == [
        {"key": "mic:S01", "name": "老王"},
        {"key": "loopback", "name": "小李"},
    ]
    assert parse_names_document(document) == document
    assert parse_names_document(json.dumps(document, ensure_ascii=False)) == document


def test_parse_rejects_a_foreign_or_missing_version() -> None:
    """主版本号读方认识才收：缺席、不是整数、不认识，三档都拒。"""
    for bad in (
        {"names": []},
        {"schema_version": "1", "names": []},
        {"schema_version": True, "names": []},
        {"schema_version": KNOWN_SCHEMA_MAJOR + 1, "names": []},
    ):
        with pytest.raises(SpeakerNamesError):
            parse_names_document(bad)


def test_parse_rejects_bad_entry_shapes() -> None:
    """`names` 不是列表、某项缺 `key` / `name` 非空字符串，都拒。"""
    for bad in (
        {"schema_version": 1},
        {"schema_version": 1, "names": {}},
        {"schema_version": 1, "names": [{"key": "mic:S01"}]},
        {"schema_version": 1, "names": [{"key": "mic:S01", "name": "  "}]},
        {"schema_version": 1, "names": ["mic:S01"]},
    ):
        with pytest.raises(SpeakerNamesError):
            parse_names_document(bad)


def test_qualified_key_shape_keeps_the_two_roads_from_colliding() -> None:
    """键只有两种合法形状：路本身，或带路前缀的限定名——裸 `S01` 说不出是哪一路，拒。"""
    for good in ("mic", "loopback", "mic:S01", " loopback:S02 "):
        assert qualified_key_shape(good) == str(good).strip()
    for bad in ("", "S01", "video:S01", "mic:", "mic:S01:x", "mic:", None):
        with pytest.raises(SpeakerNamesError):
            qualified_key_shape(bad)


def test_build_rejects_an_empty_name() -> None:
    """跳过 = 不起名：空名字不落任何状态（写侧当场拦）。"""
    with pytest.raises(SpeakerNamesError, match="空"):
        build_names_document([("mic:S01", "   ")])


def test_read_names_entries_answers_none_for_absent_and_broken(tmp_path: Path) -> None:
    """缺席 → `None`；坏文件 → 记一行、交回 `None`——读侧只做结构解析，不抛、不挡。"""
    assert read_names_entries(tmp_path) is None

    (tmp_path / NAMES_FILE_NAME).write_text("{ 坏了", encoding="utf-8")
    with _captured_speaker_names_log() as recorder:
        assert read_names_entries(tmp_path) is None
    assert any(NAMES_FILE_NAME in message for message in recorder.messages)


# ---------------------------------------------------------------------------
# 二、改名 = 写表：覆盖、追加、两路各记各的
# ---------------------------------------------------------------------------


def test_set_speaker_name_appends_then_overwrites_in_place(tmp_path: Path) -> None:
    """第一次落追加、同键再落覆盖（改名 = 再起一次名），键序保持、盘上文件读得回来。"""
    set_speaker_name(tmp_path, "mic:S01", "老王")
    set_speaker_name(tmp_path, "loopback", "小李")
    set_speaker_name(tmp_path, "mic:S01", "老王头")

    assert read_names_entries(tmp_path) == {"mic:S01": "老王头", "loopback": "小李"}
    on_disk = json.loads(names_path(tmp_path).read_text(encoding="utf-8"))
    assert [item["key"] for item in on_disk["names"]] == ["mic:S01", "loopback"]


def test_two_roads_share_a_local_number_without_colliding(tmp_path: Path) -> None:
    """完成判定的第 4 条读数：两路的「1 号」各是各的键，同盘共存、互不覆盖。"""
    set_speaker_name(tmp_path, "mic:S01", "老王")
    set_speaker_name(tmp_path, "loopback:S01", "小李")

    entries = read_names_entries(tmp_path)
    assert entries == {"mic:S01": "老王", "loopback:S01": "小李"}


def test_set_speaker_name_rejects_a_key_without_its_road(tmp_path: Path) -> None:
    """裸 `S01` 这种键在写侧就被拦下（两路不撞靠形状，不靠约定）。"""
    with pytest.raises(SpeakerNamesError):
        set_speaker_name(tmp_path, "S01", "老王")
    assert not names_path(tmp_path).exists()


def test_set_speaker_name_refuses_when_the_table_is_on_disk_but_unreadable(
    tmp_path: Path,
) -> None:
    """表在盘上但读不出（坏 JSON / 未来主版本）时**拒写**：旧文件一个字节不动。

    读侧宽容（缺表不挡出纪要，d7）与写侧谨慎（不覆盖用户还没抢救出来的名字）
    并存，各管各的方向（review-1 m2）：这一档原先拿空映射组新文档整份换上去，
    旧表无声消失——现在拒写，交动作侧按既有 `invalid_setting` 登记回执。
    """
    path = tmp_path / NAMES_FILE_NAME
    for broken in (
        "{ 坏了",
        json.dumps(
            {"schema_version": KNOWN_SCHEMA_MAJOR + 1, "names": []}, ensure_ascii=False
        ),
    ):
        path.write_text(broken, encoding="utf-8")
        on_disk_before = path.read_bytes()
        with pytest.raises(SpeakerNamesError, match="拒写"):
            set_speaker_name(tmp_path, "mic:S01", "老王")
        assert path.read_bytes() == on_disk_before, "拒写的改名不碰盘上那份旧表"


# ---------------------------------------------------------------------------
# 三、盘上身份集（单场状态与提示词共用的那份读数）
# ---------------------------------------------------------------------------


def _write_live_transcript(session: Path, source: str, lines: int = 3) -> None:
    (session / f"meeting_transcript_{source}.md").write_text(
        "\n".join(f"[21:{minute:02d}] {source} 第 {minute} 句。" for minute in range(lines))
        + "\n",
        encoding="utf-8",
    )


def _write_speakers_artifact(
    session: Path, source: str, locals: list[str], segments: int = 3
) -> None:
    document = build_speakers_document(
        session_id=session.name,
        source=source,
        engine={"name": "fake-engine"},
        audio={"seconds": 60.0},
        segments=[],
        speakers=[
            {
                "id": f"{source}:{local}",
                "local": local,
                "label_raw": f"speaker#{local}",
                "segments": segments,
                "seconds": 12.0,
            }
            for local in locals
        ],
    )
    write_speakers(session / SPEAKERS_NAME_TEMPLATE.format(source=source), document)


def test_identity_rows_fall_back_to_the_roads_without_clustering(tmp_path: Path) -> None:
    """没跑过离线聚类的场次：身份就是路本身（人话标签 + 名字空），次数读数如实 `null`。"""
    _write_live_transcript(tmp_path, "mic")

    rows = identity_rows(tmp_path)

    assert rows == [
        {"key": "mic", "source": "mic", "label": "本地", "name": None, "segments": None}
    ]
    assert identity_rows(tmp_path / " nowhere ") == [], "盘上什么材料都没有时如实给空"


def test_identity_rows_use_the_cluster_ids_when_the_artifact_is_readable(
    tmp_path: Path,
) -> None:
    """有对照表：每个编号一行（键带路、次数读数带上），路本身那行让位。"""
    _write_live_transcript(tmp_path, "mic")
    _write_speakers_artifact(tmp_path, "mic", ["S01", "S02"])

    rows = identity_rows(tmp_path)

    assert [row["key"] for row in rows] == ["mic:S01", "mic:S02"]
    assert all(row["label"] == "本地" for row in rows)
    assert all(row["segments"] == 3 for row in rows)
    assert all(row["name"] is None for row in rows)


def test_identity_rows_carry_the_confirmed_names(tmp_path: Path) -> None:
    """改过名之后：身份行里的名字一格换成人名表里那份。"""
    _write_live_transcript(tmp_path, "mic")
    _write_speakers_artifact(tmp_path, "mic", ["S01"])
    _write_live_transcript(tmp_path, "loopback")
    set_speaker_name(tmp_path, "mic:S01", "老王")

    rows = identity_rows(tmp_path)

    by_key = {row["key"]: row for row in rows}
    assert by_key["mic:S01"]["name"] == "老王"
    assert by_key["loopback"]["name"] is None


def test_identity_rows_survive_a_broken_names_file(tmp_path: Path) -> None:
    """人名表坏了：身份照摆、名字一格如实 `null`（不挡这只读数）。"""
    _write_live_transcript(tmp_path, "mic")
    set_speaker_name(tmp_path, "mic", "老王")
    (tmp_path / NAMES_FILE_NAME).write_text("[]", encoding="utf-8")

    with _captured_speaker_names_log() as recorder:
        rows = identity_rows(tmp_path)

    assert rows[0]["name"] is None
    assert any(NAMES_FILE_NAME in message for message in recorder.messages)


def test_identity_rows_survive_a_broken_speakers_artifact(tmp_path: Path) -> None:
    """对照表坏了：这一路按「没跑过聚类」处理，落回路本身那一行。"""
    _write_live_transcript(tmp_path, "mic")
    (tmp_path / SPEAKERS_NAME_TEMPLATE.format(source="mic")).write_text(
        "{ 坏了", encoding="utf-8"
    )

    with _captured_speaker_names_log() as recorder:
        rows = identity_rows(tmp_path)

    assert rows == [
        {"key": "mic", "source": "mic", "label": "本地", "name": None, "segments": None}
    ]
    assert any("对照表" in message for message in recorder.messages)


# ---------------------------------------------------------------------------
# 四、运行时的 `speaker_rename` 动作（真运行时 + 真目录；不拉离线、不拉纪要）
# ---------------------------------------------------------------------------


def _runtime(tmp_path: Path, monkeypatch, **values):
    """起一台真运行时（dev 采集替身），配置里覆盖 `**values`（与 `test_post_actions` 同一口径）。"""
    home = tmp_path / "home-speaker-names"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()
    applied = {
        **config_module.DEFAULT_VALUES,
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        **values,
    }
    runtime = build_runtime(
        config_module.Config(applied=applied, sources={key: "默认" for key in applied}, path="")
    )
    runtime._resolve_backends()
    return runtime, previous


def _registered_session(tmp_path: Path, monkeypatch):
    """登记一场只有会中稿的会（带一份盘上 `session.json`，判读落 `meeting` 档）。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    session = tmp_path / "250919"
    session.mkdir()
    _write_live_transcript(session, "mic")
    (session / "session.json").write_text(
        json.dumps({"session_id": "250919", "state": "done"}), encoding="utf-8"
    )
    opened = runtime.workspace_open(str(session))
    assert opened["dir"] == str(session)
    return runtime, session, previous


def test_speaker_rename_writes_the_roster_and_answers_a_receipt(
    tmp_path, monkeypatch, restore_config
) -> None:
    """改一次名：回执说清写了哪一场、哪一个键、现在叫什么；盘上读得回来；revision 不动。"""
    runtime, session, previous = _registered_session(tmp_path, monkeypatch)
    try:
        revision_before = runtime.revision

        receipt = runtime.speaker_rename(
            {"dir": str(session), "key": "mic", "name": " 老王 "}
        )

        assert receipt == {"dir": str(session), "key": "mic", "name": "老王"}
        assert read_names_entries(session) == {"mic": "老王"}
        assert runtime.revision == revision_before, "写表不动快照：不提交、不涨 revision"
        assert runtime.post_job_state() is None, "改名不拉起任何会后活（R26）"
    finally:
        config_module.set_current(previous)


def test_session_status_carries_the_speakers_cell(tmp_path, monkeypatch, restore_config) -> None:
    """单场状态端点的 `speakers` 一格：改前名字 `null`、改后带名，身份行照盘上产物摆。"""
    runtime, session, previous = _registered_session(tmp_path, monkeypatch)
    try:
        before = runtime.session_status_dict(str(session))
        assert before["speakers"] == [
            {"key": "mic", "source": "mic", "label": "本地", "name": None, "segments": None}
        ]

        runtime.speaker_rename({"dir": str(session), "key": "mic", "name": "老王"})

        after = runtime.session_status_dict(str(session))
        assert after["speakers"][0]["name"] == "老王"
    finally:
        config_module.set_current(previous)


def test_speaker_rename_validation_refusals(tmp_path, monkeypatch, restore_config) -> None:
    """四档校验：缺 `dir` / 缺 `key` / 空名字 / 键没有路前缀，全按 `invalid_setting`(400) 拒。"""
    runtime, session, previous = _registered_session(tmp_path, monkeypatch)
    try:
        with pytest.raises(ActionError) as missing_dir:
            runtime.speaker_rename({"key": "mic", "name": "老王"})
        assert (missing_dir.value.code, missing_dir.value.status) == ("invalid_setting", 400)

        for bad_args in (
            {"dir": str(session), "name": "老王"},
            {"dir": str(session), "key": "mic", "name": "   "},
            {"dir": str(session), "key": "S01", "name": "老王"},
        ):
            with pytest.raises(ActionError) as refused:
                runtime.speaker_rename(bad_args)
            assert (refused.value.code, refused.value.status) == ("invalid_setting", 400)
            assert refused.value.detail.get("key") in ("key", "name")
        assert read_names_entries(session) is None, "拒掉的改名一个字都不落盘"
    finally:
        config_module.set_current(previous)


def test_speaker_rename_refuses_a_directory_outside_the_workspace_list(
    tmp_path, monkeypatch, restore_config
) -> None:
    """没登记的目录拒掉：写的是「工作台里那几场」的表，不往任意路径落文件。"""
    runtime, _session, previous = _registered_session(tmp_path, monkeypatch)
    stranger = tmp_path / "260999"
    stranger.mkdir()
    try:
        with pytest.raises(ActionError) as refused:
            runtime.speaker_rename({"dir": str(stranger), "key": "mic", "name": "老王"})
        assert (refused.value.code, refused.value.status) == ("invalid_setting", 400)
        assert not names_path(stranger).exists()
    finally:
        config_module.set_current(previous)


def test_speaker_rename_refuses_while_the_session_is_still_recording(
    tmp_path, monkeypatch, restore_config
) -> None:
    """本进程正在录的那一场拒掉（会中不提供改名，d11）。

    判据是「本进程此刻在录、就是快照里那一场」（与工作区清单三档口径同一支，
    `runtime_catalog._is_actively_recording()`）：盘上停在 `recording` 的强杀残档
    **不是**录音中，照常放行（下一条）。
    """
    runtime, _session, previous = _registered_session(tmp_path, monkeypatch)
    live = tmp_path / "260930"
    live.mkdir()
    _write_live_transcript(live, "mic")
    runtime.start_session(dir=str(live))
    try:
        with pytest.raises(ActionError) as refused:
            runtime.speaker_rename({"dir": str(live), "key": "mic", "name": "老王"})
        assert (refused.value.code, refused.value.status) == ("invalid_setting", 400)
        assert not names_path(live).exists()
    finally:
        config_module.set_current(previous)


def test_speaker_rename_accepts_a_session_killed_mid_recording(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上停在 `recording`（被强杀在录音半路）的残档放行：旧拒判据作废。

    「盘上 `state=recording` 就拒」只会拦住强杀在录音半路留下的残档 —— 永久卡死类
    就此收口：这一场清单读数是「录音结束」，改名照用；在录的那一场才拒（上一条）。
    """
    runtime, _session, previous = _registered_session(tmp_path, monkeypatch)
    killed = tmp_path / "260932"
    killed.mkdir()
    _write_live_transcript(killed, "mic")
    (killed / "session.json").write_text(
        json.dumps(
            {"session_id": "260932", "state": "recording", "interrupted": True},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime.workspace_open(str(killed))
    try:
        receipt = runtime.speaker_rename({"dir": str(killed), "key": "mic", "name": "老王"})
        assert receipt == {"dir": str(killed), "key": "mic", "name": "老王"}
        assert read_names_entries(killed) == {"mic": "老王"}
    finally:
        config_module.set_current(previous)


def test_speaker_rename_accepts_a_session_killed_mid_drain(
    tmp_path, monkeypatch, restore_config
) -> None:
    """盘上停在 `draining`（被强杀在收尾半路）的那一场放行：旧拒判据作废。

    「落盘没落定」不再是闸 —— 改名只写这一场目录里的人名表，与收尾走到哪一步无关；
    盘上 `recording` 的残档同档放行（下一条），在录的那一场才拒（上上一条）。
    """
    runtime, _session, previous = _registered_session(tmp_path, monkeypatch)
    draining = tmp_path / "260931"
    draining.mkdir()
    _write_live_transcript(draining, "mic")
    (draining / "session.json").write_text(
        json.dumps(
            {"session_id": "260931", "state": "draining", "interrupted": True},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime.workspace_open(str(draining))
    try:
        receipt = runtime.speaker_rename({"dir": str(draining), "key": "mic", "name": "老王"})
        assert receipt == {"dir": str(draining), "key": "mic", "name": "老王"}
        assert read_names_entries(draining) == {"mic": "老王"}
    finally:
        config_module.set_current(previous)


@pytest.fixture()
def restore_config():
    """用例结束后把配置单例放回去（与别的用例同一口径）。"""
    previous = config_module.current()
    yield
    config_module.set_current(previous)
