"""``RuntimeCatalogMixin`` 的「单场状态」域分片：``GET /api/sessions/{id}`` 的进度区读数。

方法与方法间注释均自拆分前的 ``runtime_catalog.py`` 单文件 mixin 一字不改复制；
本文件自写内容只有模块 docstring、import 与类声明行（模块级的会后现场读数小函数一并在此）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from meeting.service.runtime_shared import ActionError, PostJob, _job_matches
from meeting.speaker_names import identity_rows as speaker_identity_rows
from meeting.workspace import canonical_dir, classify_workspace_dir, has_offline_transcript


def _post_job_progress(job: PostJob | None, directory: str) -> Any:
    """会后那条线程手里的逐块进度记录（**是这一场、是离线那一趟**时才有）。

    三样都对上才交回它：这一趟的路是 `offline`、跑的是这一场、记录已经立起来。否则
    `None` —— 调用方据此退回读盘那条路。前两样（是 dict、`dir` 相等）走
    `_job_matches()` 这一处公共判据；`kind` 档位判据留在这里。
    """
    if not _job_matches(job, directory) or str(job.get("kind")) != "offline":
        return None
    return job.get("progress")


def _post_flight_view(job: PostJob, directory: str) -> dict[str, Any]:
    """`GET /api/sessions/{id}` 的 `post` 清单里的一行：哪一趟、起止时刻、在不在飞。

    `running` 按线程活没活着答（`post_jobs_state()` 同一套判据，不另发明）。
    isinstance + dir 相等走 `_job_matches()` 这一处公共判据。
    """
    thread = job.get("thread")
    running = thread is not None and thread.is_alive()
    return {
        "running": running,
        "kind": job.get("kind"),
        "dir": str(job.get("dir")),
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


class RuntimeCatalogSessionViewMixin:
    """「单场状态」：这一场此刻跑到哪儿了（纯读，不提交、不涨 ``revision``、不广播）。"""

    # 单场状态（`GET /api/sessions/{id}`：会后视图的进度区）

    def _offline_job_running(self, directory: str) -> bool:
        """这一场**此刻有没有离线那一趟在本进程里跑**（`GET /api/sessions/{id}` 的
        `offline.in_process` 那一格，判据与 `post_jobs_state()` 同源）。

        三样都对上才算「在跑」：这一趟的路是 `offline`、跑的是这一场、线程还活着。
        **跑完那一刻这条线程就死了** —— 与 `post_jobs_state()` 同一套判据（那一格跑完
        之后也不把登记抹掉，靠 `thread.is_alive()` 分「在飞」与「跑完了」）。判据收在
        `_post_job_slot()` 一处，不另发明一套。
        """
        return self._post_job_slot("offline", directory) is not None

    def session_status_dict(self, target: Any) -> dict[str, Any]:
        """`GET /api/sessions/{id}` 的应答体：**这一场此刻跑到哪儿了**。

        `{id}` 是**这一场的目录路径**（URL 编码的绝对路径）：目录名就是这一场会的名字
        （`docs/design-intent.md` 第 3 条），`GET /api/sessions` 每一行也以 `dir` 为主键。
        路径要过与那只清单端点同一套判据（规范化后的绝对路径比对）：目录已不在原地、
        或不在工作台清单里，都按 404 如实回一句（`_require_session_dir()`，
        两档判读的唯一出处）。

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
        directory = self._require_session_dir(
            target,
            unregistered_message=(
                "这一场不在工作台清单里（`GET /api/sessions` 读的就是那份注册表）："
                "先在界面上把它打开，再把会话 id 指过来"
            ),
        )
        record = self.store.read_session_json(directory)
        state, date, mode = self._disk_record_cells(record, directory)
        wanted = canonical_dir(directory)
        probe = classify_workspace_dir(directory)
        # 契约 4.9：这张清单只收 `post_offline` / `post_minutes` 两趟；转码那一趟不进
        # （它的读数在下面的 `transcode` 格）——收工后留在 `post_jobs` 里的那一格不外泄。
        post = [
            _post_flight_view(job, directory)
            for (_kind, job_dir), job in sorted(self.post_jobs.items())
            if job_dir == wanted and _kind in ("offline", "minutes")
        ]
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
            "transcode": self._transcode_progress_for(directory),
            "minutes": self._minutes_progress_for(directory),
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
        ledger = _post_job_progress(
            self.post_jobs.get(("offline", canonical_dir(directory))), directory
        )
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
            # 盘上有结论可读：状态文件落在哪一档就照它那一档答（`running` 也照实答，「此刻
            # 有没有在跑」由 `in_process` 那格回答）。这几档都不许折成 `done`——折了之后
            # 读方看到的是失败的会后与跑成的同形；`interrupted` 尤其不许折（b1c6a58e）：
            # 折成 `running` 就把「人停的、可以重按」说回「还在跑」。
            mapped = str(read.get("state") or "")
            snapshot["state"] = (
                mapped
                if mapped in ("done", "partial", "failed", "interrupted")
                else "running"
            )
        snapshot["in_process"] = False
        return snapshot

    def _minutes_progress_for(self, directory: str) -> dict[str, Any]:
        """这一场的纪要读数（`GET /api/sessions/{id}` 的 `minutes` 那一格）。

        **判据读盘**（与 `transcode` 那一格同一套读法）：场次目录里那份
        `meeting_minutes_status.json`（`meeting/minutes/status.py`，读方逐字段校验）
        回答跑到哪一档（`running` / `done` / `failed` / `delivered`）；文件不在、读不动
        或形状不合契约时如实答 `idle` —— 纪要那一层的现状读数是**给人看的**，读不出来
        就是不认为有，不挡这只端点（`docs/design-intent.md` 第 6 条）。「本进程里此刻
        有没有这一趟」不另立一格：`post` 清单里 `kind: "minutes"` 的那一项带着
        `running`，两处合起来才分得清「真的在跑」与「盘上停着一版 `running`」。

        盘上那一版 `running` 的 `finished_at` 落的是**空串**（`mark_running()` 的口径），
        这里把它折成 `null` —— 应答体里不摆没有数的空格。
        """
        from meeting.minutes import status as minutes_status

        try:
            document = minutes_status.read_status(minutes_status.status_path(directory))
        except Exception:
            return {"state": "idle", "started_at": None, "finished_at": None, "error": None}
        finished = document.get("finished_at")
        error = document.get("error")
        return {
            "state": str(document.get("state")),
            "started_at": str(document["started_at"]) if document.get("started_at") else None,
            "finished_at": str(finished) if finished else None,
            "error": str(error) if error else None,
        }
