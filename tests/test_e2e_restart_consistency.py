"""§9-4（服务重启后状态自洽，不谎报）的进程级用例（phase3 §3.8 / §3.6 / §5.5）。

三条用例：两个「被杀」现场各写一条，再加一条「再重启一次读到的还是同一副面孔」。

  - ① `recording` 中被杀：`start` → 确认盘上 `state == "recording"` → `proc.kill()`
    （**不发信号、不等收尾**，模拟被杀 / 断电）→ 断言盘上仍是 `recording`（收尾没走完的现场）；
  - ② `stop` 之后（盘上已落 `draining`）被杀：`start` → `POST stop` → 等到盘上落成
    `state == "draining"` → `proc.kill()` → 断言盘上是 `draining`；
  - 两个现场都用**同一输出根**重启 → `GET /api/state`：`top_state == "idle"`、
    `notice.kind == "restart_unfinished"`、`notice.session_dir` 等于刚那场的场次目录、
    `notice.log_path` 非空且与本次启动的服务级日志路径 `service.log_path` 一致、
    **`session` 为 `null`**（不自动续录）→ 读盘：`state == "draining"` 且 `interrupted == true`、
    原 `drain.steps` 原样保留（不被伪造成终态）→ **再重启一次**：仍是 `restart_unfinished`
    + `draining`（同一副面孔，不改口）→ 全程没有任何自动开录。

② 那一条里**顺带核**「重启后被改写成 `draining` 的场次仍占着同一时刻只录一场」这条口径
（§3.6 末条 / §3.4 第 6 条 / §3.10 表格第 3 行、审查问题 M7）：新实例里对该场 `POST start`
→ **409**、`code == "draining"`、带上 log 路径（与本次启动的 `service.log_path` 一致）与
`failed_steps` 那一栏。组件级那半边由 `tests/test_drain.py` 第 ⑧ 条（收尾失败现场）覆盖。

重启自洽的实现（扫未收尾 + 改写 + 不自动续录）由 phase2 的
`tests/test_restart_consistency.py` 在组件级保；本文件只做**进程级**那一遍。
"""

from __future__ import annotations

import time
from pathlib import Path

from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._service import post_action, read_session_json, wait_until
from meeting.state import REVISION_START

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见 phase3.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`；下面那一段说明
# 写清了它与看门 hook 的关系。数值一律不改。
# ---------------------------------------------------------------------------

#: 单条用例的墙钟上限（秒）：与 `tests/conftest.py` 的看门 hook
#: （`CASE_WALL_CLOCK_LIMIT_SECONDS`）同一条红线 —— 用户把「每条用例 ≤ 5 秒」定为硬要求，
#: 全局 5 秒看门取代了契约 §3.8 里原先那条 60 秒口径；本常量在用例正文里**真的被断言**
#: （`_finish_case()`），不再是一行只写在文件头部的声明。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0


def _finish_case(started: float) -> None:
    """用例正文的**墙钟断言**：实测值不超过 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`。"""
    spent = time.monotonic() - started
    assert spent <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS, (
        f"本用例正文墙钟 {spent:.2f}s 超过上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS:.1f}s"
    )


#: 进程级用例的轮询超时（秒）
E2E_POLL_TIMEOUT_SECONDS = 30


def _start(proc) -> None:
    started = post_action(proc.base_url, "start")
    assert started.status_code == 200, started.text


def _session_dirs(out_root: Path) -> list[Path]:
    if not out_root.is_dir():
        return []
    return sorted((item for item in out_root.iterdir() if item.is_dir()), key=lambda p: p.name)


def _latest_session_dir(out_root: Path) -> Path:
    dirs = _session_dirs(out_root)
    assert dirs, f"输出根下还没有场次目录：{out_root}"
    return dirs[-1]


def _restart(service_process, meeting_env, out_root: Path, tag: str):
    """在**同一输出根**上顺序起下一个服务（旧进程已收场，不受「同一时刻只录一场」限制）。

    **采集后端由这个新家目录自己的 `config.json` 表达**（第七轮审查 m9）：重启用的是
    `<父目录>/home-<tag>` 这个**另一个**家目录，它的配置文件是空的 —— 不在那里写下
    `capture.backend`，子进程就会走 `auto` 那条路去找约定路径的实现（找不到 → 回退替身），
    「重启后照常能开一场」这件事因此验的不是我们想要的那一份配置。env 里那一项
    （`MEETING_CAPTURE`）一个都不写，理由见 `tests/conftest.py` 的 `meeting_env`：环境变量
    会把配置键静默压住，两条子进程路径的采集后端必须同源。
    """
    home = meeting_env.home.parent / f"home-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    helpers.write_config_json(home, {"capture.backend": "dev"})
    proc = service_process(
        home=home,
        out_root=out_root,
        log_dir=meeting_env.log_dir,
        extra_env={
            "HOME": str(home),
            "USERPROFILE": str(home),
            "MEETING_NO_BROWSER": "1",
            "MEETING_PORT": "0",
            "MEETING_OUT_ROOT": str(out_root),
            "MEETING_LOG_DIR": str(meeting_env.log_dir),
        },
    )
    proc.wait_ready()
    return proc


def _assert_restart_unfinished(proc, session_dir: Path, *, previous_revision: int) -> dict:
    """重启后的空闲态：挂 `restart_unfinished` 提示、`session` 为 `null`、不自动续录。

    **`revision` 从头开始计**（§3.6「重启后必须被重置的东西」五项里的最后一项、审查问题 M12）：
    重启后的那一份快照必须回到 `REVISION_START`（每次服务启动从同一个值重新计，**不接着上一场
    的数字往下走**）。上一场那一版由调用方经 `previous_revision` 传进来，两个值一起断言 ——
    「从同一个起点重来」这件事因此既在取值上钉住、也在与上一场的对比上钉住。
    """
    snapshot = proc.snapshot()
    assert snapshot["top_state"] == "idle", snapshot
    assert snapshot["revision"] == REVISION_START, (
        f"重启后 revision 该从 {REVISION_START} 重新计：读到 {snapshot['revision']}"
    )
    assert snapshot["revision"] < previous_revision, (
        f"重启后的 revision（{snapshot['revision']}）该小于上一场走到的那一版"
        f"（{previous_revision}）—— 这个计数不跨进程延续"
    )
    print(
        f"[M12 证据] 重启后 revision={snapshot['revision']}"
        f"（REVISION_START={REVISION_START}）；上一场走到的那一版={previous_revision}；"
        f"top_state={snapshot['top_state']!r}；"
        f"notice.kind={(snapshot['notice'] or {}).get('kind')!r}"
    )
    assert snapshot["session"] is None, "重启后不该留着上一场的 session（不自动续录）"
    assert snapshot["ended"] is None, "重启后不该留着上一场的 ended"
    # 重启后的空闲档告警区**不出现**（`docs/ui-design.md` 章 2.5 那张表）。下面那格
    # `transcript.health == "stopped"` 报的是「真转写后端还没起过场次」，空闲态本来就没有
    # 「转写停了」这条提示。
    assert snapshot["alerts"] == {"critical": None, "hints": []}
    assert snapshot["transcript"]["health"] == "stopped"

    notice = snapshot["notice"]
    assert notice is not None, "重启后的空闲态该挂 restart_unfinished 提示"
    assert notice["kind"] == "restart_unfinished", notice
    assert notice["session_dir"] == str(session_dir), notice
    assert notice["log_path"], notice
    assert notice["log_path"] == snapshot["service"]["log_path"], (
        "notice.log_path 要与本次启动的服务级日志路径一致"
        f"：{notice['log_path']} != {snapshot['service']['log_path']}"
    )
    return snapshot


def _assert_disk_draining_with_original_steps(session_dir: Path, before: dict) -> dict:
    record = read_session_json(session_dir)
    assert record is not None, f"盘上读不出 {session_dir}/session.json"
    assert record["state"] == "draining", record
    assert record["interrupted"] is True, record
    assert record["ended_at"] is None, record
    assert record["drain"]["steps"] == before["drain"]["steps"], "原 steps 要原样保留"
    return record


def test_killed_session_surfaces_restart_unfinished(service_process, meeting_env):
    """① `recording` 中被杀 → 重启后空闲态带 `restart_unfinished`，且不自动续录。"""
    case_started = time.monotonic()
    out_root = meeting_env.out_root
    # **采集后端由本用例自己的 `config.json` 表达**（审查问题 m5 / m6）：重启用的是另一个
    # 家目录（`home-<tag>`），所以首启与重启各自写下这一项，四条 `tests/test_e2e_*.py` 同源。
    meeting_env.write_config({"capture.backend": "dev"})
    proc = service_process(
        home=meeting_env.home,
        out_root=out_root,
        log_dir=meeting_env.log_dir,
        extra_env={
            "HOME": str(meeting_env.home),
            "USERPROFILE": str(meeting_env.home),
            "MEETING_NO_BROWSER": "1",
            "MEETING_PORT": "0",
            "MEETING_OUT_ROOT": str(out_root),
            "MEETING_LOG_DIR": str(meeting_env.log_dir),
        },
    )
    proc.wait_ready()
    _start(proc)
    session_dir = _latest_session_dir(out_root)
    before = read_session_json(session_dir)
    assert before["state"] == "recording"
    # 上一场走到的那一版（重启之后要**从头计**，两个值一起比）
    previous_revision = proc.snapshot()["revision"]

    # 被杀 / 断电：不发信号、不等收尾
    proc.kill()

    assert read_session_json(session_dir)["state"] == "recording", "被杀之后盘上该还是「录音中」现场"

    restarted = _restart(service_process, meeting_env, out_root, "killed-1")
    try:
        _assert_restart_unfinished(restarted, session_dir, previous_revision=previous_revision)
        _assert_disk_draining_with_original_steps(session_dir, before)
        # 不自动续录：这一刻没有正在录的场次
        assert restarted.snapshot()["session"] is None

        # 再重启一次：同一副面孔（不会因为「刚才还写着 recording」而改口）
        restarted.kill()
        again = _restart(service_process, meeting_env, out_root, "killed-2")
        try:
            _assert_restart_unfinished(again, session_dir, previous_revision=previous_revision)
            _assert_disk_draining_with_original_steps(session_dir, before)
        finally:
            again.terminate()
    finally:
        restarted.terminate()
    _finish_case(case_started)


def test_stopped_then_killed_session_surfaces_restart_unfinished(service_process, meeting_env):
    """② `stop` 之后（盘上已落 `draining`）被杀 → 走同一条未收尾留痕路径。

    假会后钩子把收尾最后一步钉在 `running` 上（`BlockingThreadPostHook`，经 `PYTHONPATH` +
    `post.hook` 配进子进程），盘上那一版 `draining` 因此真的看得见 —— 骨架构建下四步几乎
    瞬间落定，抢那几毫秒的窗口去杀进程是 flaky 的写法。
    """
    case_started = time.monotonic()
    out_root = meeting_env.out_root
    plugin = fakes.write_blocking_post_hook(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)
    proc = service_process(
        home=meeting_env.home,
        out_root=out_root,
        log_dir=meeting_env.log_dir,
        pythonpath=plugin.pythonpath(helpers.REPO_ROOT),
        extra_env={
            "HOME": str(meeting_env.home),
            "USERPROFILE": str(meeting_env.home),
            "MEETING_NO_BROWSER": "1",
            "MEETING_PORT": "0",
            "MEETING_OUT_ROOT": str(out_root),
            "MEETING_LOG_DIR": str(meeting_env.log_dir),
            **plugin.extra_env,
        },
    )
    proc.wait_ready()
    wait_until(plugin.module_loaded, timeout=E2E_POLL_TIMEOUT_SECONDS)
    _start(proc)
    session_dir = _latest_session_dir(out_root)
    previous_revision = proc.snapshot()["revision"]

    assert post_action(proc.base_url, "stop").status_code == 200
    # 等盘上真的落成 `draining`（`stop` 一进收尾就把盘上写成这一档）
    proc.snapshot()
    draining = wait_until(
        lambda: (
            lambda record: record
            if record and record["state"] == "draining" and record["ended_at"] is None
            else None
        )(read_session_json(session_dir)),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
    )
    assert draining["state"] == "draining"
    # 收尾的每一步都在提交（`revision` 因此已经往前走了一截）：取被杀前的那一版做对比基线
    previous_revision = max(previous_revision, proc.snapshot()["revision"])

    proc.kill()
    assert read_session_json(session_dir)["state"] == "draining", "被杀之后盘上该还是「收尾中」现场"

    restarted = _restart(service_process, meeting_env, out_root, "stopped-1")
    try:
        _assert_restart_unfinished(restarted, session_dir, previous_revision=previous_revision)
        record = _assert_disk_draining_with_original_steps(session_dir, draining)
        assert record["interrupted"] is True
        # **重启后被改写成 `draining` 的场次仍占着「同一时刻只录一场」**（§3.6 末条 / §3.4 第 6 条
        # / §3.10 表格第 3 行、审查问题 M7）：新实例里对这一场的 `start` 按 `draining` 这一支
        # 回 409，并把 log 路径一起带上（这一场盘上留下的 `post` 还停在 `running`，因此失败步
        # 那一栏此刻是空的 —— 换成「失败现场被杀」的场次时，同一栏里就是那一场 `failed` 的那一步）。
        refused = post_action(restarted.base_url, "start")
        assert refused.status_code == 409, (
            f"重启现场的场次该拦住新场次：实测 {refused.status_code} {refused.text}"
        )
        payload = refused.json()
        assert payload["ok"] is False, payload
        assert payload["code"] == "draining", payload
        detail = payload["detail"]
        assert detail["dir"] == str(session_dir), detail
        assert detail["log_path"], f"拒绝路径要带上 log 路径：{detail}"
        assert detail["log_path"] == restarted.snapshot()["service"]["log_path"], detail
        assert isinstance(detail["failed_steps"], list), detail
        print(
            f"[M7 证据] 重启后（notice.kind=restart_unfinished、盘上 state="
            f"{record['state']!r}）POST start -> {refused.status_code} "
            f"code={payload['code']!r}；应答体：{refused.text}"
        )
    finally:
        restarted.terminate()
    _finish_case(case_started)


def test_second_restart_reads_same_face(service_process, meeting_env):
    """连续重启两次：第二次读到的仍是 `restart_unfinished` + `draining`（同一副面孔）。"""
    case_started = time.monotonic()
    out_root = meeting_env.out_root
    # 采集后端同 ① 与 `_restart()`：由各自家目录的 `config.json` 表达（审查问题 m5 / m6）
    meeting_env.write_config({"capture.backend": "dev"})
    proc = service_process(
        home=meeting_env.home,
        out_root=out_root,
        log_dir=meeting_env.log_dir,
        extra_env={
            "HOME": str(meeting_env.home),
            "USERPROFILE": str(meeting_env.home),
            "MEETING_NO_BROWSER": "1",
            "MEETING_PORT": "0",
            "MEETING_OUT_ROOT": str(out_root),
            "MEETING_LOG_DIR": str(meeting_env.log_dir),
        },
    )
    proc.wait_ready()
    _start(proc)
    session_dir = _latest_session_dir(out_root)
    assert read_session_json(session_dir)["state"] == "recording"
    previous_revision = proc.snapshot()["revision"]
    proc.kill()

    faces = []
    for index in (1, 2):
        restarted = _restart(service_process, meeting_env, out_root, f"twice-{index}")
        try:
            snapshot = _assert_restart_unfinished(
                restarted, session_dir, previous_revision=previous_revision
            )
            faces.append(
                {
                    "top_state": snapshot["top_state"],
                    "revision": snapshot["revision"],
                    "notice_kind": snapshot["notice"]["kind"],
                    "session_dir": snapshot["notice"]["session_dir"],
                    "disk_state": read_session_json(session_dir)["state"],
                    "disk_interrupted": read_session_json(session_dir)["interrupted"],
                }
            )
        finally:
            restarted.kill()

    assert faces[0] == faces[1], f"两次重启读到的不是同一副面孔：{faces}"
    assert all(face["disk_state"] == "draining" for face in faces)
    assert all(face["disk_interrupted"] is True for face in faces)
    assert all(face["top_state"] == "idle" for face in faces)
    assert all(face["revision"] == REVISION_START for face in faces)
    _finish_case(case_started)
