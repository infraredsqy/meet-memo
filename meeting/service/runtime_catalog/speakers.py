"""``RuntimeCatalogMixin`` 的「说话人改名」域分片：``speaker_rename`` 动作。

方法与方法间注释均自拆分前的 ``runtime_catalog.py`` 单文件 mixin 一字不改复制；
本文件自写内容只有模块 docstring、import、``_log`` 一行与类声明行。
"""
from __future__ import annotations

import logging
from typing import Any

from meeting.service.runtime_shared import ActionError
from meeting.speaker_names import SpeakerNamesError, set_speaker_name

_log = logging.getLogger("meeting.service.runtime")


class RuntimeCatalogSpeakersMixin:
    """「说话人改名」：把说话人名字写进这一场目录的人名表。"""

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
