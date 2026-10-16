"""工作台 / 场次目录 / 状态视图 / 杂项动作 mixin。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.service.runtime_shared import ActionError, PostJob, _job_matches
from meeting.speaker_names import (
    SpeakerNamesError,
    identity_rows as speaker_identity_rows,
    set_speaker_name,
)
from meeting.state import now_iso
from meeting.workspace import canonical_dir, classify_workspace_dir, has_offline_transcript

_log = logging.getLogger("meeting.service.runtime")

#: 新建目录名字的长度上限（Windows 单段路径成分的实用上限）
_DIR_NAME_MAX_CHARS = 255
#: Windows 保留设备名（大小写不敏感、带扩展名同样保留）：`CON.txt` 也开不出文件
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)


def checked_new_dir_name(name: Any) -> str:
    """把要新建的目录名过一遍**拒绝式**校验，合格原样交回，不合格给一句人话。

    与资料上传那套清洗（`runtime_materials.sanitize_material_name()`）相反：名字必须
    **用户亲手起**（设计意图第 3、4 条的新口径——软件可以替你建目录，但仍不自动命名、
    不自动挑落点），所以这里**不改写、只拒绝**：非法的名字当场 400 说清哪里不行，
    不悄悄清洗出一个别的名字落盘。

    拒绝档：空名 / 两头空白 / `.`、`..` / 带路径分隔符或冒号 / 控制字符 /
    结尾点号（Windows 会悄悄掐掉，落盘名就跟输入对不上）/ 保留设备名 / 超长。
    """
    if not isinstance(name, str):
        raise ValueError("目录名要是一段文字：名字由你来起")
    if name != name.strip():
        raise ValueError("目录名两头不要留空白（Windows 对它有自己的一套掐法，落盘名会跟输入对不上）")
    if not name:
        raise ValueError("目录名不能是空的：名字由你来起，软件不自动命名")
    if name in (".", ".."):
        raise ValueError("目录名不能是「.」或「..」（那是路径里的相对说法，不是名字）")
    for separator in ("\\", "/", ":"):
        if separator in name:
            raise ValueError(
                "目录名里不能带路径分隔符或冒号（\\ / :）：一次只建一层，"
                "层级由地址栏或双击走进去"
            )
    if any(ord(char) < 0x20 for char in name):
        raise ValueError("目录名里不能带控制字符")
    if name.endswith("."):
        raise ValueError("目录名不能以点号结尾（Windows 落盘时会把它悄悄掐掉）")
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_DEVICE_NAMES:
        raise ValueError(f"「{stem}」是 Windows 的保留设备名，不能当目录名")
    if len(name) > _DIR_NAME_MAX_CHARS:
        raise ValueError(f"目录名太长（{len(name)} 字符，上限 {_DIR_NAME_MAX_CHARS}）")
    return name


def _post_job_progress(job: PostJob | None, directory: str) -> Any:
    """会后那条线程手里的逐块进度记录（**是这一场、是离线那一趟**时才有）。

    三样都对上才交回它：这一趟的路是 `offline`、跑的是这一场、记录已经立起来。否则
    `None` —— 调用方据此退回读盘那条路。前两样（是 dict、`dir` 相等）走
    `_job_matches()` 这一处公共判据；`kind` 档位判据留在这里。
    """
    if not _job_matches(job, directory) or str(job.get("kind")) != "offline":
        return None
    return job.get("progress")


def _post_job_view(job: PostJob | None, directory: str) -> dict[str, Any] | None:
    """`GET /api/sessions/{id}` 的 `post` 那一格：上一趟/这一趟是哪一趟、在不在飞。

    `running` 由调用方按 `post_job_state()` 那一套判据盖上来（这里只看线程活没活着，
    不另发明一套判据）。isinstance + dir 相等走 `_job_matches()` 这一处公共判据。
    """
    if not _job_matches(job, directory):
        return None
    return {
        "running": False,
        "kind": job.get("kind"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _offline_progress_module() -> Any:
    """`meeting.offline.progress`（**函数体内 import**：本模块顶层不拉会后那一包）。"""
    from meeting.offline import progress as progress_module

    return progress_module


def _declared_sources_of(directory: str | Path) -> list[str]:
    """这一场跑哪几路（`session.json` 声明的那些，读不到就是两路全集）。"""
    from meeting.offline import runner as offline_runner

    return list(offline_runner.declared_sources(directory))


def _offline_status_document(directory: str | Path) -> dict[str, Any] | None:
    """场次目录里那份 `meeting_offline_status.json`（读不出来就交回 `None`）。

    只取两样东西：起止时刻与终态那一档。成败的结论归状态文件与三份产物，这里借它
    给读数补上"这一趟什么时候起的、跑完没有"的位置感。
    """
    from meeting.offline import artifacts as offline_artifacts

    path = Path(directory) / offline_artifacts.STATUS_NAME
    try:
        return offline_artifacts.read_status(path)
    except Exception:
        return None


def _session_status_error(code: str, message: str, *, detail: dict[str, Any]) -> ActionError:
    """`GET /api/sessions/{id}` 的失败体（与动作失败同一副信封，HTTP 404）。"""
    return ActionError(code, message, status=404, detail=detail)


def _disk_str_or_none(record: dict[str, Any] | None, key: str) -> str | None:
    """盘上 `session.json` 的一个字符串格（`date` / `mode`）：缺席或不是字符串如实 `null`。"""
    if not isinstance(record, dict):
        return None
    value = record.get(key)
    return str(value) if isinstance(value, str) else None


class RuntimeCatalogMixin:
    """工作台 / 场次目录 / 状态视图 / 杂项动作 mixin。"""

    # -- 设置更新 ---------------------------------------------------------

    def settings_update(self, args: dict[str, Any]) -> dict[str, Any]:
        """写 `config.json`（原子写）；**可热读的键立刻生效，其余键写盘成功但要重启**。

        「写盘成功」与「快照立刻反映」是两件事：归 `pending_restart` 的那些键本次只落盘，
        运行期仍取启动时的生效值，快照 `settings` 里读到的还是旧值（可观察判据是主 log 那一行
        「……仍取启动值」，见 §7.1 高危点 7）；本次改动里凡有热读键（`asr.*` 五个、
        `alarm.sound` 与设置抽屉那四项：`devices.mic` / `devices.loopback` /
        `heartbeat.interval_seconds` / `summary.default_window`），就在下面重取一次
        `_settings_view()` 并提交 —— 出声开关勾选即生效；设备改完，紧接着的 `reprobe`
        按**新值**挑（`_config_view()` 读的就是内存生效值）；心跳周期与总结缺省档的
        快照格当场换新值。
        """
        try:
            changes = config_module.validate_setting_changes(args, current=self.config)
        except config_module.ConfigValueError as exc:
            raise ActionError(
                "invalid_setting",
                str(exc),
                status=400,
                detail={"key": exc.key, "value": exc.value, "legal": exc.legal},
            ) from exc

        try:
            config_module.write_config_values(changes)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"配置写盘失败（{type(exc).__name__}: {exc}）；这次改动一处都没生效",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        except config_module.ConfigCorruptError as exc:
            raise ActionError(
                "persist_failed",
                f"现有 config.json 读不出来，没法安全叠加：{exc}",
                status=500,
                detail={"error": str(exc)},
            ) from exc

        applied, pending = config_module.split_setting_changes(changes)
        # 热读键立刻生效：生效值在内存里换掉（下一段读到的就是新值）
        if applied and self.config is not None:
            values = dict(self.config.applied)
            values.update({key: changes[key] for key in applied})
            self.config = config_module.Config(
                applied=values, sources=dict(self.config.sources), path=self.config.path
            )
            config_module.set_current(self.config)
            self.snapshot.settings = self._settings_view()
            self.commit()

        for key in changes:
            env_name = config_module.env_name_for(key)
            if env_name and env_name in os.environ:
                _log.warning(
                    "settings_update：%s 被环境变量 %s 压着（写盘值 %r），运行期仍由环境变量说话",
                    key, env_name, changes[key],
                )
        # 这一行只对**真正要重启才生效**的那些键说：热读键（含 `alarm.sound`）走上面的
        # `applied` 分支，写盘那一刻快照与运行期行为就已经换过去了，报「仍取启动值」
        # 会是一句与事实相反的话（`alarm.sound` 原先归 `pending_restart`，这一行是那时的形状）。
        if pending:
            _log.info(
                "settings_update：%s 归 pending_restart，仍取启动时的生效值（改了要重启才生效）",
                "、".join(sorted(pending)),
            )
        if "alarm.sound" in changes:
            _log.info(
                "settings_update：alarm.sound 收在热读档，本刻起出声开关 = %r（下一次提交的快照一并可见）",
                self._value("alarm.sound"),
            )
        return {"applied": applied, "pending_restart": pending}

    def dev_inject(self, args: dict[str, Any]) -> None:
        """`dev_inject`：只在 dev 采集替身在场时可用，且要有正在进行的场次。"""
        if not self.capture_capabilities.get("dev_inject"):
            raise ActionError(
                "action_unavailable",
                "dev_inject 只在 dev 采集替身在场时可用（当前后端不支持）",
                status=404,
            )
        if self.snapshot.top_state not in ("recording", "alert"):
            raise ActionError("not_recording", "没有正在进行的场次，注入动作无对象可挂", status=409)

        source = args.get("source")
        fault = args.get("fault")
        try:
            self.capture_backend.inject_fault(
                source=source,
                fault=fault,
                after_seconds=args.get("after_seconds", 0.0),
                duration_seconds=args.get("duration_seconds"),
            )
        except (TypeError, ValueError) as exc:
            raise ActionError(
                "invalid_setting", str(exc), status=400,
                detail={"source": source, "fault": fault, "error": str(exc)},
            ) from exc

    # -- 会中总结（`1987cb20` phase2：动作、记录表、缓冲的清理） ------------

    def _clear_session_summary(self, *, reason: str) -> None:
        """把会中总结那两样**一起**清成空：内存缓冲（`summary_buffer`）与本场记录表
        （`summary_records`）。

        **唯一入口**（照 `_clear_hints()` 那种同进同退的先例）：两样必须一起清 —— 只清缓冲
        会留下「记录里那几条还挂着、它们指的窗口已经取不回」的现场，只清记录又会把上一场的行
        留给这一场。两个调用点：
        `start_session()`（新的一场从零开始）与 `shutdown()`（关停之后这两笔内存不留）。
        **不动** `summary_client`（那是注入进来的协作者，与本场的数据无关）。
        """
        had_lines = len(self.summary_buffer)
        had_records = len(self.summary_records)
        self.summary_buffer.clear()
        self.summary_records.clear()
        if had_lines or had_records:
            _log.info(
                "会中总结的两笔内存已清空（%s）：转写缓冲 %s 行、本场记录 %s 条",
                reason, had_lines, had_records,
            )

    def _session_id_for_summary(self) -> str | None:
        """本场总结记录归属的场次目录名（没有场次时 `null`）。"""
        session = getattr(self.snapshot, "session", None)
        return None if session is None else str(session.id)

    # -- 工作区注册表（工作台收编节点 e2f6166b）---------------------------

    def _is_actively_recording(self, path: str | Path) -> bool:
        """`path` 这一场是不是**本进程此刻在录**的那一场（三档口径里 `recording` 的判据）。

        **判据只此一处**（`GET /api/sessions` 的行读数与会后动作的参数校验同源）：
        `top_state ∈ recording / alert` 且目录就是快照里那一场。盘上 `session.json` 停在
        `state="recording"` 的残档（被强杀在录音半路、本进程没在录它）**不是**录音中 ——
        那一场的读数是 `ended`（`_disk_record_cells()`）、会后动作照常受理。
        """
        session = self.snapshot.session
        return (
            self.snapshot.top_state in ("recording", "alert")
            and session is not None
            and os.path.normcase(canonical_dir(str(session.dir)))
            == os.path.normcase(canonical_dir(str(path)))
        )

    def _disk_record_cells(
        self, record: dict[str, Any] | None, path: str | Path
    ) -> tuple[str | None, str | None, str | None]:
        """工作区清单那三格的读数：`(state, date, mode)`。

        **`GET /api/sessions` 与 `GET /api/sessions/{id}` 前七格共用这一处**（契约 4.9
        写着前七格与 4.8 同一行同源、逐格语义见 4.8）：同一个事实在两处必须给同一个值。

        `state` 是**三档口径**（只表达「这场录音走到哪了」，落盘 / 转码 / 会后那几步
        不进这个口径）：

        - `"recording"` —— **仅当**这一行是本进程此刻在录的活跃场次（`_is_actively_recording()`）；
        - `"ended"` —— 盘上有 `session.json`（录音已经发生过；盘上 `state` 停在哪一档
          —— 收尾半路 / 收完 / 失败 / 录音半路的残档 —— 一律不进这个口径，那几步的进度另有读数）；
        - `null` —— 没有 `session.json`（空白工作区 / 尚未开过会）。

        `date` / `mode` 仍是盘上原文的字符串判读（盘上落成数字或对象时如实 `null`）。
        `dir_exists` 与 `kind` 由调用方照旧现场判，不折进这三档。
        """
        if self._is_actively_recording(path):
            return "recording", _disk_str_or_none(record, "date"), _disk_str_or_none(record, "mode")
        if record is None:
            return None, None, None
        return "ended", _disk_str_or_none(record, "date"), _disk_str_or_none(record, "mode")

    def _disk_segment_cells(self, record: dict[str, Any] | None) -> list[dict[str, Any]]:
        """盘上 `session.json` 的**段读数**（界面上「两段的时长与时间」的来源）。

        每段四格：`index` / `started_at` / `ended_at` / `duration_seconds`——
        段记录里给人看的就这四样（`pauses` / `sources` 是核对用的格子，不进界面）。
        盘上形状不对的那一条如实跳过；这一格只是把盘上的读数端出来给人看（第 6 条）。
        状态文件不在场时交回空表。
        """
        if not isinstance(record, dict):
            return []
        rows: list[dict[str, Any]] = []
        raw = record.get("segments")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            duration = item.get("duration_seconds")
            rows.append(
                {
                    "index": item.get("index") if isinstance(item.get("index"), int) else None,
                    "started_at": item.get("started_at")
                    if isinstance(item.get("started_at"), str)
                    else None,
                    "ended_at": item.get("ended_at")
                    if isinstance(item.get("ended_at"), str)
                    else None,
                    "duration_seconds": float(duration)
                    if isinstance(duration, (int, float))
                    else None,
                }
            )
        return rows

    def workspace_open(self, dir: Any, confirm: bool = False) -> dict[str, Any]:
        """`workspace_open`：打开/收编一个**已存在**的目录，登记进注册表。

        判别三档（`meeting/workspace.py` 的 `classify_workspace_dir()`）：

        - `empty` → 当一场新会，直接登记；
        - `meeting`（含**疑似会议目录**：有音频归档名但没有 `session.json`）→ 收编，
          直接登记；
        - `unknown`（非空、没有会议材料）→ 用户确认之后再登记：`confirm=False` 回
          `invalid_setting`(400)，**目录与盘面一个字节没动**；`confirm=True` 才落笔。
        - `missing` → `invalid_setting`(400)（已存在是硬前提，本服务不建目录）。

        已经登记过的路径只更新 `last_opened_at`（去重判据就是规范化路径）。
        成功返回进应答体的 `workspace` 键：`{dir, kind, suspected, first_time}`。
        """
        info = classify_workspace_dir(dir)
        kind = str(info["kind"])
        path = str(info["dir"])
        if kind == "missing":
            raise ActionError(
                "invalid_setting",
                f"目标工作区目录不存在（只接受已存在的目录，不自动建）：{path}",
                status=400,
                detail={"key": "dir", "value": None if dir is None else str(dir),
                        "legal": ["已存在的目录路径（空目录当新会；有会议材料的收编）"]},
            )
        if kind == "unknown" and not confirm:
            raise ActionError(
                "invalid_setting",
                "选中的目录不是空的、也没有会议材料（session.json 或 meeting_audio_*.pcm）："
                "确认把它当工作区收编之后才登记",
                status=400,
                detail={"key": "confirm", "value": None,
                        "legal": ["确认收编时带 confirm: true 重新发一次本动作"]},
            )
        stamp = now_iso()
        try:
            entry = self.registry.open(path, now=stamp)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"工作区注册表写不下去，这次打开没有生效：{type(exc).__name__}: {exc}",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return {
            "dir": entry["dir"],
            "kind": kind,
            "suspected": bool(info["suspected"]),
            "first_time": bool(entry["first_time"]),
        }

    def workspace_remove(self, dir: Any) -> dict[str, Any]:
        """`workspace_remove`：**只把这一条从注册表移走**；文件一个字节不动（第 5 条）。

        目录被事先移走/删掉的那一条也照样移（`removed: False` 只是"名单里本来没有"），
        重复删除安全。
        """
        return self.registry.remove(dir)

    def sessions_dict(self) -> dict[str, Any]:
        """`GET /api/sessions` 的应答体：注册表 + 每一行的现场读数 + 会后那一趟的全局读数。

        条目只记两个时刻（`added_at` / `last_opened_at`），**"目录还在不在、里面是什么"
        由本函数现场判**（注册表不反映目录内容）：`dir_exists` 由 `Path.is_dir()` 给、
        `kind` 由 `classify_workspace_dir()` 给；`state` 是三档口径
        （`"recording"` 仅当这一行就是本进程此刻在录的活跃场次；盘上有 `session.json`
        一律 `"ended"`；没有则 `null` —— 见 `_disk_record_cells()`），
        `date` / `mode` 只在盘上读得出来时带值。
        `recent` 是最近一次被打开/切换的那一条的目录（**只从注册表读**，不扫任何场次
        目录 —— 启动序列对它的口径见 `docs/state.md`「重启自洽」）。

        `has_offline` 是「这一场有没有会后精细稿」那一格（R29）：判据是场次目录里在不在
        `meeting_offline_transcript_<source>.md`（`workspace.has_offline_transcript()`，
        只看名目、不读内容）。「出纪要」那个按钮按它渲染 —— 界面因此一眼看得出 AI 手上
        有没有那份更细的稿。**这一格走本端点，不塞进快照**（推送出去的东西只认那九个顶层键，
        前端两道硬门钉着）。

        顶层第三格 `post_job`（会后进度界面节点 `52b24392`）：`post_job_state()` 的原文
        —— 此刻服务进程里在飞的那一趟会后（`{kind, dir, started_at}`）或 `null`。它是
        界面那格全局「显卡忙不忙」的会后一半读数（另一半「本机正在录」从快照状态读，
        不走这里）；**每行十二键**，这一格在应答体顶层、不进任何一行，快照九键
        一个字不动。行里的 `segments` 是这场会**各段的时长与时间**（续接节点加上去的
        读数，盘上段记录原文四格，两段的会看得出接过）。
        """
        rows: list[dict[str, Any]] = []
        for entry in self.registry.entries():
            path = str(entry["dir"])
            probe = classify_workspace_dir(path)
            record = self.store.read_session_json(path)
            state, date, mode = self._disk_record_cells(record, path)
            rows.append(
                {
                    "dir": path,
                    "session_id": None if record is None else record.get("session_id")
                    or Path(path).name,
                    "dir_exists": probe["kind"] != "missing",
                    "kind": probe["kind"],
                    "suspected": bool(probe["suspected"]),
                    "state": state,
                    "date": date,
                    "mode": mode,
                    "segments": self._disk_segment_cells(record),
                    "has_offline": has_offline_transcript(path),
                    "added_at": entry["added_at"],
                    "last_opened_at": entry["last_opened_at"],
                }
            )
        recent = self.registry.recent()
        return {
            "sessions": rows,
            "recent": None if recent is None else str(recent["dir"]),
            "post_job": self.post_job_state(),
        }

    # -- 会后的两趟活（人按才起：`post_offline` / `post_minutes` 两个动作）-----

    def post_job_state(self) -> dict[str, Any] | None:
        """会后那两趟活的现场读数（`None` = 没有在跑的）。

        `running` 由线程自己落（跑完那一刻清掉），因此这一格不会永远停在"在跑"。
        它只进动作回执与 log，不进快照。**跑完那一刻不把它抹掉**：抹掉之后按「生成会后
        精细稿」跑完一场、再去读那一场的进度，读到的就是一片空白（`GET /api/sessions/{id}`
        的 `post` 那一格要回答「上一趟是哪一趟、什么时候起的」）。判"有没有在飞"看
        `thread.is_alive()`，`post_job['finished_at']` 是它收工的时刻。
        """
        job = self.post_job
        if job is None:
            return None
        thread = job.get("thread")
        if thread is not None and not thread.is_alive():
            if job.get("finished_at") is None:
                job["finished_at"] = now_iso()
            return None
        return {
            "kind": job["kind"],
            "dir": job["dir"],
            "started_at": job["started_at"],
        }

    # -- 单场状态（`GET /api/sessions/{id}`：会后视图的进度区）-----------------

    def _offline_job_running(self, directory: str) -> bool:
        """这一场**此刻有没有离线那一趟在本进程里跑**（`GET /api/sessions/{id}` 的
        `offline.in_process` 那一格，判据与 `post_job_state()` 同源）。

        三样都对上才算「在跑」：这一趟的路是 `offline`、跑的是这一场、线程还活着。
        **跑完那一刻这条线程就死了** —— 与 `post_job_state()` 同一套判据（那一格跑完之后
        也不把 job 抹掉，靠 `thread.is_alive()` 分「在飞」与「跑完了」）。不另发明一套。
        **在飞判据是这一处自己的形状**（与 `post_stop()` 那一处逐字同形：`thread`
        缺席就算「不在飞」；`post_job_state()` 对 `thread` 缺席答「在飞」，三处并不同形，
        复审 #2 的裁定因此只抽 isinstance + dir 相等这一段公共判据）。
        """
        job = self.post_job
        if not _job_matches(job, directory) or str(job.get("kind")) != "offline":
            return False
        thread = job.get("thread")
        return thread is not None and thread.is_alive()

    def session_status_dict(self, target: Any) -> dict[str, Any]:
        """`GET /api/sessions/{id}` 的应答体：**这一场此刻跑到哪儿了**。

        `{id}` 是**这一场的目录路径**（URL 编码的绝对路径）：目录名就是这一场会的名字
        （`docs/design-intent.md` 第 3 条），`GET /api/sessions` 每一行也以 `dir` 为主键。
        路径要过与那只清单端点同一套判据（规范化后的绝对路径比对）：目录已不在原地、
        或不在工作台清单里，都按 404 如实回一句（见 `_session_status_error()`）。

        `segments` 那一格与清单端点同一来源（`_disk_segment_cells()`，盘上段记录原文），
        界面在详情区摆「各段的时长与时间」。

        `speakers` 那一格是**这一场盘上的身份集**（人名节点 `91b6752e`）：每个身份一行
        （限定键 / 路 / 人话标签 / 现名或 `null` / 可得的次数读数），名字读这一场目录里
        的 `meeting_speaker_names.json`。它同样是**给人看的读数**：人名表读不出就如实
        当作「没有确认过的名字」（名字一格 `null`），不报错、不挡这只端点
        （`docs/design-intent.md` 第 6 条）。

        `offline` 那一格是**会后离线的进度读数**，两路各一份：跑过几块 / 共几块、
        此刻在跑哪一路、从起跑到现在过了多久。三样东西合起来就是会后视图那一片进度区
        要的「第几块 / 共几块、正在跑哪一路、已经跑了多久」。

        读数有两个来源，取到哪个用哪个：

          - **这一趟正在本进程里跑** → 用会后那条线程手里的现场（`post_job['progress']`），
            「已经跑了多久」由它按单调钟当场算；
          - **本进程没在跑这一趟**（服务重启过、命令行跑的那一趟、跑完很久了）→ 读场次
            目录里那份 `meeting_offline_progress.jsonl` 记录，起点与终点问状态文件。
            跨进程算不出单调钟的差值，那一格如实交回 `null`（它本来就只作附注，
            见 `docs/design-intent.md` 第 6、7 条）。

        `offline` 里**两块回答的是两件事**，各有各的判据（契约 4.9）：`state` 照盘上那份
        如实答（状态文件写着 `running` 就答 `running`，写着 `partial` / `failed` /
        `interrupted` 就照它那一档答），`in_process` 答「**本进程里此刻有没有这一趟
        活**」。两块合起来才分得清「真的在跑」与「盘上停着一版 `running`、其实谁都没在
        跑」—— 后者照盘上的话答，不替它二选一。

        `offline` 是**给人看的读数**：不提交、不涨 `revision`、不广播。这只端点是纯读，
        快照九键一个字不动。
        """
        directory = canonical_dir(target)
        probe = classify_workspace_dir(directory)
        if probe["kind"] == "missing":
            raise _session_status_error(
                "session_dir_missing",
                f"这一场的目录已不在原地：{directory}",
                detail={"dir": str(directory), "kind": "missing"},
            )
        if self._registry_entry_for(directory) is None:
            raise _session_status_error(
                "invalid_setting",
                "这一场不在工作台清单里（`GET /api/sessions` 读的就是那份注册表）："
                "先在界面上把它打开，再把会话 id 指过来",
                detail={"dir": str(directory), "kind": probe["kind"]},
            )
        record = self.store.read_session_json(directory)
        state, date, mode = self._disk_record_cells(record, directory)
        post = _post_job_view(self.post_job, directory)
        if post is not None:
            post["running"] = self.post_job_state() is not None
        return {
            "dir": str(directory),
            "session_id": None if record is None else (
                record.get("session_id") or Path(str(directory)).name
            ),
            "kind": probe["kind"],
            "state": state,
            "date": date,
            "mode": mode,
            "segments": self._disk_segment_cells(record),
            "has_offline": has_offline_transcript(directory),
            "post": post,
            "offline": self._offline_progress_for(directory),
            "speakers": speaker_identity_rows(directory),
        }

    def _offline_progress_for(self, directory: str) -> dict[str, Any]:
        """这一场的会后离线进度读数（现场优先，落盘那份记录兜底）。见 `session_status_dict()`。

        **两块分开说，别混**：

          - `offline.state` 照**盘上那份**如实答（契约 4.9：`running` / `done` / `partial` /
            `failed` / `interrupted` / `idle`，外加「盘上有记录、而这一趟既不在飞、也没有状态文件回答」那一档
            `ledger`）。状态文件写着 `done` / `partial` / `failed` / `interrupted` 就照它自己那一档答，
            写着 `running` 也照它自己那一档答；**进程崩在半路的那一场因此如实答
            `running`**，不在这里替盘上的话做二次解读；
          - `offline.in_process` 另立一格答「**本进程里此刻有没有这一趟活**」，判据取
            `_offline_job_running()`（这一趟的路是 `offline`、跑的是这一场、那条线程还活着
            —— 与「这一趟在不在飞」同一套现场）。**跑完那一刻它翻回 `false`**：活着的现场
            与盘上那份记录因此各按各的判据出（跑完再读这一场，读的是盘上那份记录）。

        两块合起来才读得清崩在半路那一档：`state: "running"` 说的是盘上停着的那版状态，
        `in_process: false` 说的是此刻没有任何东西在跑。**`interrupted` 是盘上自己的
        终态**（b1c6a58e 落地）：停掉的那一场照盘上答 `interrupted`，这一格照旧只透传、
        不替它下结论，也**不参与任何判定**（进度是给人看的读数）。
        """
        ledger = _post_job_progress(self.post_job, directory)
        running = self._offline_job_running(directory)
        if ledger is not None and running:
            # 这一趟正在本进程里跑：块数、当前哪一路、已跑多久都由现场给
            snapshot = ledger.snapshot()
            snapshot["in_process"] = True
            return snapshot
        progress_module = _offline_progress_module()
        snapshot = progress_module.snapshot_of(
            directory,
            sources=_declared_sources_of(directory),
            session_id="",
        )
        read = _offline_status_document(directory)
        if read is not None:
            snapshot["started_at"] = read.get("started_at")
            snapshot["finished_at"] = read.get("finished_at")
            # 盘上有结论可读：状态文件落在 `done` / `partial` / `failed` / `interrupted`
            # 就**照它那一档**答，别的档（`running`）也照它自己那一档如实答 —— 那一版可能
            # 是「真的还在跑」，也可能是「崩在半路停在那儿」，这一格不替它二选一
            # （「此刻有没有东西在跑」由 `in_process` 那一格回答）。**这几档都不许折成
            # `done`**：折了之后读方（界面这一格、`GET /api/sessions/{id}` 的
            # `offline.state`）看到的是一趟整个失败的会后与一趟跑成的会后同形 ——
            # 那正是「读方被骗」那一档。**`interrupted` 尤其不许折**（b1c6a58e）：
            # 折成 `running` 就把「人停的、可以重按」说回「还在跑」。
            mapped = str(read.get("state") or "")
            snapshot["state"] = (
                mapped
                if mapped in ("done", "partial", "failed", "interrupted")
                else "running"
            )
        snapshot["in_process"] = False
        return snapshot

    def _reject_post_job(self) -> None:
        """会后那两趟活的两道闸：本场还在录音，或已经有一趟没跑完。

        「本场还在录音」按 `draining` 档报（409）：会还在录着，会后那两个按钮要等
        录音结束（工作区清单上那一行还是「录音中」）。**「落盘没落定」不再是闸**
        ——收尾走到哪一步、成没成，都不拦会后动作；盘上停在收尾半路的那一场，
        工作区读数就是「录音结束」，按钮照按。

        「已经有一趟在飞」同样按 `draining` 报：一张显卡上不并排开两份模型
        （`docs/design-intent.md` 第 21 条）。`action_unavailable`（404）留给
        「这台机器上没接这一层的实现」那一档。
        """
        job = self.post_job_state()
        if job is not None:
            raise ActionError(
                "draining",
                f"会后的活儿正在跑（{job['kind']}）：{job['dir']}；等它跑完再按",
                status=409,
                detail={"kind": job["kind"], "dir": job["dir"], "started_at": job["started_at"]},
            )
        if self.snapshot.top_state in ("recording", "alert"):
            session = self.snapshot.session
            raise ActionError(
                "draining",
                "这一场还在录音中：会后的两个按钮要等录音结束",
                status=409,
                detail={
                    "top_state": self.snapshot.top_state,
                    "session_id": None if session is None else session.id,
                    "dir": None if session is None else session.dir,
                },
            )

    def _registry_entry_for(self, target: str) -> dict[str, Any] | None:
        """`target` 在注册表里那一条（判据与 `GET /api/sessions` **同一套**：`normcase` 后的
        规范路径）。没登记过 → `None`。"""
        wanted = os.path.normcase(canonical_dir(target))
        for entry in self.registry.entries():
            if os.path.normcase(canonical_dir(str(entry["dir"]))) == wanted:
                return entry
        return None

    def speaker_rename(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """`speaker_rename` 动作：把某个说话人编号的名字写进**这一场目录里的人名表**。

        R10 第四条 / R30：名字要能改、并且记住——「记住」落在这场目录根的
        `meeting_speaker_names.json`（`meeting.speaker_names` 的字段契约），同一场以后
        再出纪要，`{{SPEAKER_NAMES}}` 把它渲染进提示词。**这只动作只写这一份表**：
        不拉起离线、不拉起纪要（R26），不改任何已生成的 `meeting_final.md`（R13），
        也不动快照——不提交、不涨 `revision`、不广播。

        校验全在动手之前（`code` 全落在既有那几个上，不加新的）：

          - `args["dir"]` **必给**，过 `_session_dir_for_post()` 同一套判据
            （注册表里 / 目录在原地 / 不是本进程此刻在录的那一场），
            否则 `invalid_setting`(400)——会中不提供改名（这一屏根本不出现入口）；
            盘上残档不拒（收尾半路、录音半路的强杀残档都放行）；
          - `args["key"]` 要是盘上身份集形状的一个键（`mic` / `loopback` 或
            `mic:S01` 这种带路前缀的限定名），`args["name"]` 要是非空字符串；
            跳过 = 不起名，空名字不落任何状态；
          - 人名表写不进去（盘上出岔）按 `persist_failed`(500) 如实回。

        成功体是通用信封 + 一个 `speaker` 键（先例是 `post` 的回执）：这一格写的是
        「写了哪一场、哪一个键、现在叫什么」；界面上改完当场生效走的是**重问单场状态**
        那一跳（`speakers` 一格），回执本身不带货。
        """
        args = dict(args or {})
        raw_dir = args.get("dir")
        if raw_dir is None or not str(raw_dir).strip():
            raise ActionError(
                "invalid_setting",
                "speaker_rename 要 `dir`（写的是哪一场的人名表；会中不提供改名）",
                status=400,
                detail={
                    "key": "dir",
                    "value": "" if raw_dir is None else str(raw_dir),
                    "legal": ["工作台清单里、目录还在原地、不是本进程正在录的一场"],
                },
            )
        target = self._session_dir_for_post(args)
        key = args.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ActionError(
                "invalid_setting",
                "speaker_rename 要 `key`（哪一个说话人）",
                status=400,
                detail={
                    "key": "key",
                    "value": "" if key is None else str(key),
                    "legal": ["mic / loopback，或 mic:S01 这种带路前缀的限定名"],
                },
            )
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ActionError(
                "invalid_setting",
                "speaker_rename 要非空的 `name`（跳过 = 不起名，空名字不落任何状态）",
                status=400,
                detail={
                    "key": "name",
                    "value": "" if name is None else str(name),
                    "legal": ["一个非空的名字"],
                },
            )
        try:
            saved = set_speaker_name(target, key.strip(), name.strip())
        except SpeakerNamesError as exc:
            raise ActionError(
                "invalid_setting",
                str(exc),
                status=400,
                detail={
                    "key": "key",
                    "value": key.strip(),
                    "legal": ["mic / loopback，或 mic:S01 这种带路前缀的限定名"],
                },
            ) from exc
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"人名表写不进去：{type(exc).__name__}: {exc}",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        _log.info("说话人改名落盘：%s（%s → %s）", target, saved["key"], saved["name"])
        return {"dir": target, "key": saved["key"], "name": saved["name"]}

    def dirs_listing(self, dir: Any, with_files: bool = False) -> dict[str, Any]:
        """`GET /api/dirs` 的应答体：目录选择器的一个现场（起点是用户主目录）。

        `cwd` 是规范化后的当前层；`up` 是上一层——**到盘根才到顶**：用户主目录不再是
        天花板，从 `~/` 一路向上能走到 `C:\\` 这类盘根，盘根那一档 `up` 落 `null`
        （界面按它把「向上一层」置灰）。UNC 路径不在口径里，不在场也不造需求。
        `here` 是**正在浏览的这一层自己**的名目判读（与 `dirs` 每项同一套四格）——
        选择器里这一层本身可选，不必退到上一层再点那一行。`home` 恒是主目录
        （选择器打开时的起点，落点由用户挑）。

        `with_files=True`（`?files=1`，资料节点的选择器文件模式要的）时应答体**多一个
        `files` 键**：这一层里的**普通文件**每项 `{name, dir, size_bytes}`（`dir` 是绝对
        路径，`size_bytes` 是现读的文件大小），按名字排序、与 `dirs` 分开两列——目录
        进不了 `files`，文件也进不了 `dirs`。不带这个参数时**没有这个键**。同样纯读：
        只列名目与大小，不读内容。
        """
        raw = config_module.home_dir() if dir is None else str(dir)
        cwd = Path(canonical_dir(raw))
        home = Path(config_module.home_dir())
        if not cwd.is_dir():
            raise ActionError(
                "invalid_setting",
                f"要浏览的目录不存在：{cwd}",
                status=400,
                detail={"key": "dir", "value": str(dir),
                        "legal": ["已存在的目录路径；省略时从用户主目录起步"]},
            )
        rows: list[dict[str, Any]] = []
        try:
            children = sorted((item for item in cwd.iterdir() if item.is_dir()),
                              key=lambda item: item.name.lower())
            file_rows: list[dict[str, Any]] = []
            if with_files:
                for item in sorted((item for item in cwd.iterdir() if item.is_file()),
                                   key=lambda item: item.name.lower()):
                    file_rows.append(
                        {"name": item.name, "dir": str(item),
                         "size_bytes": int(item.stat().st_size)}
                    )
        except OSError as exc:
            raise ActionError(
                "invalid_setting",
                f"要浏览的目录读不动（{type(exc).__name__}: {exc}）",
                status=400,
                detail={"key": "dir", "value": str(cwd), "legal": []},
            ) from exc
        for item in children:
            probe = classify_workspace_dir(item)
            rows.append(
                {"name": item.name, "dir": str(item), "kind": probe["kind"],
                 "suspected": bool(probe["suspected"])}
            )
        here = classify_workspace_dir(cwd)
        here_row = {
            "name": cwd.name or str(cwd),  # 盘根没有末段名，如实给整条路径
            "dir": str(cwd),
            "kind": here["kind"],
            "suspected": bool(here["suspected"]),
        }
        payload: dict[str, Any] = {
            "home": str(home),
            "cwd": str(cwd),
            # 盘根的 parent 是它自己：那一条是「向上一层」唯一的到顶档
            "up": None if cwd == cwd.parent else str(cwd.parent),
            "here": here_row,
            "dirs": rows,
        }
        if with_files:
            payload["files"] = file_rows
        return payload

    def dirs_create(self, dir: Any, name: Any) -> dict[str, Any]:
        """`POST /api/dirs` 的本体：在指定的那一层里就地新建一个目录。

        新口径（设计意图第 3、4 条：软件可以替你建目录，但仍不自动命名、不自动挑落点）：
        落点是**正在浏览的这一层**（`dir` 省略时主目录，与 `GET /api/dirs` 同一起点）、
        名字必须用户给（`checked_new_dir_name()` 拒绝式校验，不清洗不改写）。只建一层、
        不递归；不做删除、不做改名。

        拒绝档三条，各给一句能看懂的人话，不留半截状态（动手前校验全过才碰盘）：

        - 那一层不存在 / 读不动 → 400 `invalid_setting`（与 `GET /api/dirs` 同一档）；
        - 名字非法 → 400 `invalid_setting`（`detail.key = "name"`，`legal` 里是规则原话）；
        - 同名已存在（目录或文件都算占位）→ 409 `dir_exists`——这一片自己的 `code`
          （先例是资料的 `material_missing`、单场状态的 `session_dir_missing`）；
        - 盘上出岔（无权限等）→ 500 `persist_failed`，盘上不落半截东西
          （`mkdir()` 单调用原子：建不出来就是什么都没有）。

        成功体里的 `directory` 是新建那一行的名目判读（刚建出来是 `empty`，判读照走
        同一套 `classify_workspace_dir()`，不替它下别的结论）；本写入**不提交、不涨
        `revision`、不广播**——选择器的现场独立于四态快照（先例是资料四条端点）。
        """
        raw = config_module.home_dir() if dir is None else str(dir)
        parent = Path(canonical_dir(raw))
        if not parent.is_dir():
            raise ActionError(
                "invalid_setting",
                f"要在里面新建目录的那一层不存在：{parent}",
                status=400,
                detail={"key": "dir", "value": str(dir),
                        "legal": ["已存在的目录路径；省略时用用户主目录"]},
            )
        try:
            checked = checked_new_dir_name(name)
        except ValueError as exc:
            raise ActionError(
                "invalid_setting",
                str(exc),
                status=400,
                detail={"key": "name", "value": None if name is None else str(name),
                        "legal": ["一层名字：不带路径分隔符、不以点或空白结尾、"
                                  "不是 Windows 保留设备名"]},
            ) from exc
        target = parent / checked
        if target.exists():
            raise ActionError(
                "dir_exists",
                f"这一层里已经有叫「{checked}」的目录或文件：{target}；"
                "换个名字，或者双击进去直接用它",
                status=409,
                detail={"dir": str(parent), "name": checked},
            )
        try:
            target.mkdir()
        except FileExistsError as exc:
            # 查重到落盘之间被人抢了位：同一档如实报，不静默让位（名字是人起的，不换）
            raise ActionError(
                "dir_exists",
                f"这一层里已经有叫「{checked}」的目录或文件：{target}；"
                "换个名字，或者双击进去直接用它",
                status=409,
                detail={"dir": str(parent), "name": checked},
            ) from exc
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"目录建不出来（{type(exc).__name__}: {exc}）；盘上不落半截东西",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        probe = classify_workspace_dir(target)
        _log.info("选择器里新建目录：%s（名目 %s）", target, probe["kind"])
        return {
            "name": target.name,
            "dir": str(target),
            "kind": probe["kind"],
            "suspected": bool(probe["suspected"]),
        }

    def devices_listing(self) -> dict[str, Any]:
        """`GET /api/devices` 的应答体：两路各自的候选端点表（**只读、纯枚举、不开设备**）。

        设置抽屉的两个设备下拉用它（`311f84f5`）。枚举走 `meeting/capture/sources.py` 的
        `list_input_endpoints()` / `list_render_endpoints()` —— **只读端点表**，不试开、
        不采集（`probe()` 会短暂试开，因此**不能**拿它当清单来源）。与 `GET /api/dirs`
        同一档纯读：不提交、不涨 `revision`、不广播；**任何状态下都 200**（会中打开抽屉
        时清单照给，换不换得了由界面按第 7 章禁用）。

        每项六格照 `DeviceEndpoint` 原样给：`name` / `host_api` / `role` / `index` /
        `channels` / `is_default`。render 端点没有 host API 与序号概念
        （`host_api` 为空串、`index` 是枚举序），形状两端一致，界面不挑着读。
        """
        from meeting.capture import sources as capture_sources

        try:
            inputs = list(capture_sources.list_input_endpoints() or [])
            renders = list(capture_sources.list_render_endpoints() or [])
        except Exception as exc:
            raise ActionError(
                "capture_unavailable",
                f"设备端点表读不出来（{type(exc).__name__}: {exc}）；这次一枚举都没有发生",
                status=503,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc

        def row(endpoint: Any) -> dict[str, Any]:
            return {
                "name": str(endpoint.name),
                "host_api": str(endpoint.host_api or ""),
                "role": str(endpoint.role),
                "index": None if endpoint.index is None else int(endpoint.index),
                "channels": int(endpoint.channels),
                "is_default": bool(endpoint.is_default),
            }

        return {
            "mic": [row(item) for item in inputs],
            "loopback": [row(item) for item in renders],
        }

    def _note_registry_open(self, session_dir: str, *, why: str) -> None:
        """把开过的这场顺手在注册表上记一笔 `last_opened_at`（失败只警告、不阻塞录音）。"""
        try:
            self.registry.open(session_dir)
        except OSError as exc:
            _log.warning(
                "工作区注册表写不进去（%s: %s）；场次的 %s 照旧运转（%s）",
                type(exc).__name__, exc, session_dir, why,
            )
