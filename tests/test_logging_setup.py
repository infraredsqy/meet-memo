"""`tests/test_logging_setup.py` —— 日志（phase1 §5 第 4 / 5 / 9 条 / §4.1 文件清单第 15 行）。

覆盖：
  - 服务级 log 路径（家目录重定向到临时目录）与格式串；
  - bootstrap 日志先落默认目录、读配置后按生效 `log.dir` 重建；
  - **轮转按 `log.max_bytes` / `log.backup_count` 配置值生效**（改这两个键造出轮转：
    备份份数、保留整份旧文件、最早那几行落在 `.2`、`.3` 不出现，**不改固定常量**）；
  - logger `meeting` + `propagate=False`；幂等（重复调用不重复加 handler）；
  - `detach_session_log()` 写失败**抛 `OSError`**；
  - **`EXIT_STARTUP_UNMET` 这一档的唯一断言处**：主 log 不可写（`log.dir = <普通文件>/sub/`）
    与 `config.json` 损坏（非法 JSON / 同名目录）各自的真子进程退出码断言；
  - 启动扫描未收尾场次的 WARNING（只取最新的一场、每次启动最多一行、不建目录），
    **以及 phase2 的同源替换**：扫描命中后那份 `session.json` 被改写成
    `state: "draining"` + `interrupted: true`（`drain.steps` 原样保留、不伪造终态），
    场次目录里其余文件逐字不变、不新建任何目录、不动别的场次
    （phase2 §2.3 第 7 条：phase1 那条「内容一字未变」assertion 的唯一落点在本文件），
    以及两类「最新一场未命中」的**条数口径**：`session.json` **不存在** → 一行警告都不落
    （与「输出根下没有场次目录」同口径）；`session.json` **存在却读不出来**（含非 UTF-8
    字节）→ 按未命中处理且恰落一行「读不出来」的 WARNING，服务照常起。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting import config as config_module
from meeting import logging_setup
from meeting.cli import EXIT_STARTUP_UNMET

EXPECTED_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s"


# ---------------------------------------------------------------------------
# 进程内：logger 名、propagate、格式串、幂等
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_logging(tmp_path, monkeypatch):
    """把服务级日志接到临时家目录（重定向家目录解析，不新增环境变量）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    logging_setup.reset_for_tests()
    try:
        yield home
    finally:
        logging_setup.reset_for_tests()


def _service_handlers() -> list:
    """`meeting` logger 上本模块自己加的服务级 handler（走公开标记，不读模块私有状态）。"""
    return [
        item
        for item in logging.getLogger("meeting").handlers
        if getattr(item, "_meeting_logging_handler", False)
    ]


def _service_handler():
    """当前生效的服务级 handler（断言的读法：从 logger 的 handler 列表里认标记）。"""
    handlers = _service_handlers()
    assert len(handlers) == 1, f"服务级 handler 应当恰好一个：{handlers}"
    return handlers[0]


def test_logger_name_and_propagate(temp_logging):
    """记录器名是 `meeting`，且 `propagate is False`（不污染 torch / transformers 的日志）。"""
    logging_setup.setup_service_logging(str(helpers.log_dir_of(temp_logging)))
    logger = logging.getLogger("meeting")
    assert logger.name == logging_setup.LOGGER_NAME
    assert logger.propagate is False


def test_service_log_path_is_under_config_meeting_logs(temp_logging):
    """服务级 log 落在 `<家目录>/.config/meeting/logs/meeting.log`。"""
    logging_setup.setup_service_logging(str(helpers.log_dir_of(temp_logging)))
    assert logging_setup.service_log_path() == str(helpers.service_log_path_of(temp_logging))
    assert os.path.isfile(logging_setup.service_log_path())


def test_format_string_is_the_frozen_one(temp_logging):
    """格式串逐字冻结；`session` 缺省 `-`，注入用挂在 handler 上的 `SessionFilter`。"""
    logging_setup.setup_service_logging(str(helpers.log_dir_of(temp_logging)))
    handler = _service_handler()
    assert handler.formatter._fmt == EXPECTED_FORMAT  # noqa: SLF001 - logging 的公开属性名带下划线
    filters = [item for item in handler.filters if isinstance(item, logging_setup.SessionFilter)]
    assert filters and filters[0].session == logging_setup.SESSION_PLACEHOLDER

    logging.getLogger("meeting.probe").info("格式串核对")
    line = helpers.wait_until(
        lambda: next(
            (item for item in helpers.read_text(helpers.service_log_path_of(temp_logging)).splitlines()
             if "格式串核对" in item),
            None,
        ),
        timeout=5,
        what="日志落盘",
    )
    assert "session=-" in line
    assert "meeting.probe" in line
    assert "INFO   " in line  # levelname 是左对齐的 7 宽


def test_setup_is_idempotent(temp_logging):
    """重复调用初始化不重复加 handler（同参数直接复用）。"""
    log_dir = str(helpers.log_dir_of(temp_logging))
    logging_setup.setup_service_logging(log_dir)
    first = _service_handler()
    logging_setup.setup_service_logging(log_dir)
    logging_setup.setup_service_logging(log_dir)
    assert _service_handlers() == [first]


def test_rebuild_replaces_handler_and_notes_bootstrap_path(tmp_path, monkeypatch):
    """bootstrap 日志先落默认目录；读配置后按生效 `log.dir` 重建，并写明 bootstrap 的落点。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    logging_setup.reset_for_tests()
    try:
        bootstrap_dir = config_module.default_log_dir()
        logging_setup.setup_service_logging(bootstrap_dir)
        logger = logging.getLogger("meeting")
        logger.info("这一行属于 bootstrap 阶段")
        bootstrap_path = logging_setup.service_log_path()
        assert os.path.isfile(bootstrap_path)

        # 生效 log.dir 与默认日志目录不同（换到临时目录里的另一处）
        effective_dir = str(tmp_path / "effective_logs")
        handlers_before = len(logger.handlers)
        logging_setup.setup_service_logging(
            effective_dir, level="INFO", max_bytes=4096, backup_count=2
        )
        assert len(logger.handlers) == handlers_before  # 旧的摘掉了，没有叠加
        effective_path = logging_setup.service_log_path()
        assert effective_path == str(Path(effective_dir) / "meeting.log")

        logger.info("这一行属于生效阶段")
        text = helpers.read_text(Path(effective_path))
        assert "这一行属于生效阶段" in text
        assert "bootstrap 阶段的日志留在" in text
        assert bootstrap_path in text
        # 旧 handler 已经关掉：bootstrap 那份只剩它自己那一行
        assert "这一行属于生效阶段" not in helpers.read_text(Path(bootstrap_path))
    finally:
        logging_setup.reset_for_tests()


def _earliest_filler_index(text: str) -> int:
    """这份日志里出现过的**最小**填充行号（拿它比「谁更早」）。"""
    indexes = [int(match) for match in re.findall(r"填充行 (\d{4})", text)]
    assert indexes, f"这份日志里没有填充行：{text[:200]!r}"
    return min(indexes)


def test_rotation_uses_config_values(temp_logging):
    """轮转按 `log.max_bytes` / `log.backup_count` 生效：备份份数、整份保留、最早在 `.2`。

    上限与行数都压得很小，让「轮转多份时最早的一份在最后那一份里」这条判据可判：
    本案恰好轮转三次，最早的那一份因此停在 `.2`。
    """
    logging_setup.setup_service_logging(
        str(helpers.log_dir_of(temp_logging)), level="INFO", max_bytes=700, backup_count=2
    )
    logger = logging.getLogger("meeting")

    log_path = Path(logging_setup.service_log_path())
    log_dir = log_path.parent
    logger.info("最先写进来的一行 MARK_FIRST")
    for index in range(14):
        logger.info("填充行 %04d %s", index, "x" * 60)

    first_backup = log_path.parent / (log_path.name + ".1")
    oldest_backup = log_path.parent / (log_path.name + ".2")
    helpers.wait_until(
        lambda: first_backup.exists() and oldest_backup.exists(),
        timeout=10,
        what="轮转产生 .1 / .2",
    )
    assert not (log_path.parent / (log_path.name + ".3")).exists(), (
        "备份份数按 log.backup_count 生效，不该有 .3"
    )

    backups = sorted(
        name for name in os.listdir(log_dir) if name.startswith(log_path.name + ".")
    )
    assert backups == [log_path.name + ".1", log_path.name + ".2"]
    assert len(backups) == 2  # 备份文件数等于 log.backup_count

    # 旧内容**整份保留**：最后那一份备份装的是轮转出去的最早内容
    oldest_text = helpers.read_text(oldest_backup)
    newer_text = helpers.read_text(first_backup)
    assert "MARK_FIRST" in oldest_text
    assert _earliest_filler_index(oldest_text) < _earliest_filler_index(newer_text)


def test_attach_and_detach_session_log(temp_logging):
    """场次级 log 的挂/摘机制：写进 `<场次目录>/meeting.log`，行里带场次目录名。"""
    logging_setup.setup_service_logging(str(helpers.log_dir_of(temp_logging)))
    session_dir = temp_logging / "out" / "250906"
    session_dir.mkdir(parents=True)

    logging_setup.attach_session_log(str(session_dir))
    try:
        logging.getLogger("meeting.session").info("场次级这一行")
    finally:
        logging_setup.detach_session_log()

    text = helpers.read_text(session_dir / "meeting.log")
    assert "场次级这一行" in text
    assert "session=250906" in text
    # 摘掉之后不再写场次 log
    logging.getLogger("meeting.session").info("摘掉之后的这一行")
    assert "摘掉之后的这一行" not in helpers.read_text(session_dir / "meeting.log")


def test_detach_session_log_raises_oserror_when_unwritable(temp_logging):
    """`detach_session_log()` 写失败时抛 `OSError`（**不返回错误对象、不返回布尔值**）。"""
    logging_setup.setup_service_logging(str(helpers.log_dir_of(temp_logging)))
    session_dir = temp_logging / "out" / "260907"
    session_dir.mkdir(parents=True)
    handler = logging_setup.attach_session_log(str(session_dir))

    # 造一个「落盘这一步会失败」的现场：把 handler 的流换成一个 flush 就报 OSError 的替身
    # （Windows 上场次 log 被本进程打开着时没法删/换，这里直接让写盘这一步失败）
    class _UnwritableStream:
        def write(self, text):
            raise OSError("模拟磁盘满：这一行写不进去")

        def flush(self):
            raise OSError("模拟磁盘满：刷不动")

        def close(self):
            return None

    handler.stream = _UnwritableStream()

    with pytest.raises(OSError):
        logging_setup.detach_session_log()
    # 出错后 handler 也已经从 logger 上摘掉，不留半挂状态
    assert logging_setup.session_log_dir() is None


def test_build_rotating_handler_uses_given_parameters(temp_logging):
    """构造入口按调用方给的配置值造 handler（`logging_setup.py` 里没有写死的 5MiB/3）。"""
    handler = logging_setup.build_rotating_handler(
        str(temp_logging / "logs2"), level="WARNING", max_bytes=1234, backup_count=7,
        filename="custom.log",
    )
    try:
        assert handler.maxBytes == 1234
        assert handler.backupCount == 7
        assert handler.level == logging.WARNING
        assert handler.baseFilename.endswith("custom.log")
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# 子进程：EXIT_STARTUP_UNMET 这一档的唯一落点
# ---------------------------------------------------------------------------


def test_main_log_unwritable_exits_startup_unmet(tmp_path):
    """主 log 不可写（`log.dir = <普通文件>/sub/`）→ 子进程退出码 `EXIT_STARTUP_UNMET`。"""
    home = tmp_path / "home"
    home.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("这里放一个普通文件，挡住宿主目录的建立", encoding="utf-8")
    bad_log_dir = blocker / "sub" / "logs"
    helpers.write_config_json(home, {"web.port": 0, "log.dir": str(bad_log_dir)})

    # ① 初始化本身抛错
    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        logging_setup.reset_for_tests()
        with pytest.raises(OSError):
            logging_setup.setup_service_logging(str(bad_log_dir))
    finally:
        logging_setup.reset_for_tests()
        if old_home is not None:
            os.environ["HOME"] = old_home
        if old_profile is not None:
            os.environ["USERPROFILE"] = old_profile

    # ② 真子进程退出码
    result = helpers.run_web_expecting_failure(home)
    assert result.returncode == EXIT_STARTUP_UNMET
    combined = result.stdout + result.stderr
    assert "主 log 不可写" in combined
    assert str(bad_log_dir) in combined


def test_corrupt_config_json_exits_startup_unmet(tmp_path):
    """`config.json` 是截断的非法 JSON → `EXIT_STARTUP_UNMET`，终端说清文件路径。"""
    home = tmp_path / "home"
    helpers.config_dir_of(home).mkdir(parents=True)
    path = helpers.config_path_of(home)
    path.write_text('{"web.port": 8765', encoding="utf-8")

    result = helpers.run_web_expecting_failure(home)
    assert result.returncode == EXIT_STARTUP_UNMET
    combined = result.stdout + result.stderr
    assert str(path) in combined
    assert "行" in combined


def test_config_json_is_a_directory_exits_startup_unmet(tmp_path):
    """同名目录顶替配置文件（读不出来）→ `EXIT_STARTUP_UNMET`，路径照样说清。"""
    home = tmp_path / "home"
    path = helpers.config_path_of(home)
    path.mkdir(parents=True)

    result = helpers.run_web_expecting_failure(home)
    assert result.returncode == EXIT_STARTUP_UNMET
    assert str(path) in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# 子进程：启动扫描未收尾场次（只取最新的一场、每次启动最多一行）
# ---------------------------------------------------------------------------


def _session_record(state: str, *, interrupted: bool = False) -> str:
    return json.dumps(
        {
            "session_id": "x",
            "dir": "x",
            "mode": "both",
            "state": state,
            "created_at": "2025-09-06T10:00:00+08:00",
            "started_at": "2025-09-06T10:00:00+08:00",
            "ended_at": None,
            "sources": [{"id": "mic", "device": None, "archive": "meeting_audio_mic.pcm"}],
            "drain": {"steps": []},
            "interrupted": interrupted,
            "service": {"pid": 1, "started_at": "2025-09-06T10:00:00+08:00"},
        },
        ensure_ascii=False,
        indent=2,
    )


def test_scan_warns_once_for_the_latest_unfinished_session(tmp_path):
    """最新一场未收尾 → 主 log 一行 WARNING；那份记录被改写成「收尾中 + 已打断」。

    phase2 §2.3 第 5 / 7 条的同源替换（**这一条是本文件里唯一的落点**）：
      - phase1 的那行 `WARNING`（含场次目录与 `state` / `interrupted`）**逐字保留**，
        断言照旧（每次启动恰一行「发现未收尾的场次」）；
      - 追加：该场次记录被改写成 `state: "draining"` + `interrupted: true`，
        **原 `steps` 原样保留、不伪造终态**；
      - 场次目录里**其余文件逐字不变、不新建任何目录**（扫描动作只该有这一处写盘）。
    """
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = out_root / "250906"
    session_dir.mkdir(parents=True)
    record_path = session_dir / "session.json"
    original = json.loads(_session_record("recording"))
    record_path.write_text(_session_record("recording"), encoding="utf-8")
    # 场次目录里的另一个文件：扫描**不许**碰它（phase2 §2.3 第 7 条）
    audio_path = session_dir / "meeting_audio_mic.pcm"
    audio_path.write_bytes(b"\x00" * 32)
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})

    before_children = sorted(item.name for item in out_root.iterdir())

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        # 扫描的改写是**服务进程**在做：等盘上真的落到「收尾中 + 已打断」那一版再读
        rewritten = helpers.wait_for_session_record(
            session_dir, lambda data: data.get("state") == "draining"
        )
        log_text = proc.log_text()
    finally:
        proc.terminate()

    warning_lines = helpers.lines_with(log_text, "WARNING")
    unfinished = [line for line in warning_lines if "启动扫描：发现未收尾" in line]
    assert len(unfinished) == 1, f"未收尾场次的 WARNING 应当只有一行：{unfinished}"
    assert "250906" in unfinished[0]
    assert "recording" in unfinished[0]

    # 那份 session.json 被改写成「收尾中 + 已打断」，原 steps 原样保留、不伪造终态
    assert rewritten["state"] == "draining"
    assert rewritten["interrupted"] is True
    assert rewritten["drain"]["steps"] == original["drain"]["steps"]
    assert rewritten["ended_at"] is None
    # 改写后的盘上内容仍是合法 JSON、字段全集与 schema 一致
    assert set(rewritten) == set(original)

    # 场次目录里其余文件逐字不变、不新建任何目录、不动别的场次
    assert audio_path.read_bytes() == b"\x00" * 32
    assert sorted(item.name for item in session_dir.iterdir()) == [
        "meeting_audio_mic.pcm", "session.json",
    ]
    assert sorted(item.name for item in out_root.iterdir()) == before_children
    # 不自动开始录音：没有场次开始行
    assert not helpers.lines_with(log_text, "场次开始")


def test_scan_reports_only_the_newest_of_two_unfinished_sessions(tmp_path):
    """同时放更早的未收尾场次 → WARNING 仍然只有一行，且路径是最新那一场。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    older = out_root / "260901"
    newer = out_root / "260902"
    for entry in (older, newer):
        entry.mkdir(parents=True)
        (entry / "session.json").write_text(_session_record("recording"), encoding="utf-8")
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        helpers.wait_for_session_record(newer, lambda data: data.get("state") == "draining")
        log_text = proc.log_text()
    finally:
        proc.terminate()

    unfinished = [
        line for line in helpers.lines_with(log_text, "WARNING")
        if "启动扫描：发现未收尾" in line
    ]
    assert len(unfinished) == 1, f"只取最新的一场，WARNING 应当只有一行：{unfinished}"
    assert "260902" in unfinished[0]
    assert "260901" not in unfinished[0]
    # 只看最新那一场：更早那一场不会被改写（phase2 §2.3 第 7 条「不动别的场次」）
    assert json.loads((older / "session.json").read_text(encoding="utf-8"))["state"] == "recording"
    assert json.loads((newer / "session.json").read_text(encoding="utf-8"))["state"] == "draining"


def test_scan_is_silent_when_nothing_is_unfinished(tmp_path):
    """最新一场已收尾 / 输出根不存在 → 那一行不出现（也不趁机建目录）。"""
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = out_root / "250906"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(_session_record("done"), encoding="utf-8")
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})

    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        log_text = proc.log_text()
    finally:
        proc.terminate()
    assert not [line for line in helpers.lines_with(log_text, "WARNING") if "启动扫描" in line]

    # 输出根不存在时同样不出现，且不建目录
    home2 = tmp_path / "home2"
    home2.mkdir()
    missing_root = tmp_path / "not_created_yet"
    helpers.write_config_json(home2, {"web.port": 0, "output.root": str(missing_root)})
    proc2 = helpers.spawn_service(home2)
    try:
        proc2.wait_ready()
        log_text2 = proc2.log_text()
    finally:
        proc2.terminate()
    assert not [line for line in helpers.lines_with(log_text2, "WARNING") if "启动扫描" in line]
    assert not missing_root.exists()


def test_scan_survives_non_utf8_session_json(tmp_path):
    """最新一场的 `session.json` 是非 UTF-8 字节 → 按未命中处理，**服务照常起来**。

    非 UTF-8 字节会让 `json.load()` 抛 `UnicodeDecodeError`（它属于 `ValueError` 这一族，
    跟 `JSONDecodeError` 同支）；这一条盯的正是「解码失败不许冒到启动序列」。
    """
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = out_root / "250906"
    session_dir.mkdir(parents=True)
    record_path = session_dir / "session.json"
    blob = b'{"state": "recording", "\xff\xfe\x00": 1}'
    record_path.write_bytes(blob)
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})

    proc = helpers.spawn_service(home)
    try:
        port = proc.wait_ready_by_heartbeat()  # 服务真的起来了（HTTP 答得上）
        assert port > 0
        assert proc.poll() is None, "扫到读不出来的 session.json 只当未命中，不许把服务带崩"
        log_text = proc.log_text()
    finally:
        proc.terminate()

    # 按未命中口径：没有「未收尾场次」那行 WARNING
    assert not [line for line in helpers.lines_with(log_text, "WARNING") if "启动扫描：发现未收尾" in line]
    # 读不出来的事实照旧留痕（不许静默），每次启动最多一行
    unreadable = [line for line in helpers.lines_with(log_text, "WARNING") if "读不出来" in line]
    assert len(unreadable) == 1, f"读不出来的 WARNING 应当只有一行：{unreadable}"
    assert "250906" in unreadable[0]
    # 那份文件原样不动
    assert record_path.read_bytes() == blob


def test_scan_is_silent_when_session_json_is_absent(tmp_path):
    """场次目录里**没有** `session.json` → 两条警告都不落，服务照常起（契约 §3.5 第 2 条）。

    「最新一场未命中时不出现」含两种情形：**输出根下没有场次目录**（已由
    `test_scan_is_silent_when_nothing_is_unfinished` 覆盖）与**场次目录里没有 `session.json`**
    （本用例）。这一支与前一支的区别是定死的：

      - `session.json` 根本不存在（`FileNotFoundError`）→ 与「没有场次目录」同口径，
        **一行 WARNING 都不落**；
      - 文件存在却读不出来 → 仍要落**恰一行**「读不出来…按未命中处理」的 WARNING
        （`test_scan_survives_non_utf8_session_json` 钉住）。

    两条断言都必须在位，否则「条数上限是 1」这条口径会被第二种警告突破。
    """
    home = tmp_path / "home"
    home.mkdir()
    out_root = tmp_path / "out"
    session_dir = out_root / "250906"
    session_dir.mkdir(parents=True)  # 场次目录在，里面**只有**一份音频归档，没有 session.json
    (session_dir / "meeting_audio_mic.pcm").write_bytes(b"\x00" * 16)
    assert not (session_dir / "session.json").exists()
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})

    before_children = sorted(item.name for item in out_root.iterdir())
    before_files = sorted(item.name for item in session_dir.iterdir())

    proc = helpers.spawn_service(home)
    try:
        port = proc.wait_ready_by_heartbeat()  # 服务照常起来（HTTP 答得上）
        assert port > 0
        assert proc.poll() is None, "最新一场没有 session.json 只当未命中，不许影响启动"
        log_text = proc.log_text()
    finally:
        proc.terminate()

    warnings = helpers.lines_with(log_text, "WARNING")
    assert not [line for line in warnings if "启动扫描" in line], (
        f"场次目录里没有 session.json 时不该落任何启动扫描警告：{warnings}"
    )
    assert not [line for line in warnings if "读不出来" in line], (
        f"「读不出来」这一类警告同样不该出现（它专指文件存在却读不出的情形）：{warnings}"
    )
    assert not [line for line in warnings if "未收尾" in line], f"不该报未收尾场次：{warnings}"

    # 不趁机建目录、不补写 session.json、不改动已有的内容
    assert sorted(item.name for item in out_root.iterdir()) == before_children
    assert sorted(item.name for item in session_dir.iterdir()) == before_files
    assert not (session_dir / "session.json").exists()
