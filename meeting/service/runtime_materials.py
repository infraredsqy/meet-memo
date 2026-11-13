"""会议资料层 mixin：场次目录下固定的 `meeting_materials/` 子目录（资料节点）。

产品口径（节点 spec 与 `docs/design-intent.md` 第 3、5、6 条）：

  - **资料是给纪要 AI 的 context**：`meeting_materials/` 这个目录名是与服务端和纪要
    提示词两侧共享的固定产品约定，不设配置键、不可改名；**首次上传时由服务创建**
    （清单那一侧只读，目录不在就如实给空数组、不顺手建目录）；
  - **原样字节落盘**：不转码、不压缩、不限类型；上传**流式写盘**（收到多少写多少，
    不整段进内存），大小不设服务端硬顶；**不保留原文件时间戳**——落盘那一刻的 mtime
    就是「贴上时刻」；
  - **没有旁注清单文件**：目录就是清单（第 6 条「元信息不校验」的延伸），不另设 JSON
    记录、无备注元数据；每条资料给名字、大小、贴上时刻（= 文件修改时间）；
  - **只认两种目标目录**：服务的**当前活跃场次**、工作区注册表（`workspaces.json`）
    里**登记过的场次**；其余一律拒收（404，与 `GET /api/sessions/{id}` 同一套寻址口径）；
  - **独立于四态快照**：纯读写文件——不提交、不涨 `revision`、不广播（spec 点名的最小口径）。

上传的文件名**不可信**：`sanitize_material_name()` 做清洗防路径穿越（本机是 Windows：
非法字符、保留设备名、结尾点 / 空格都要处理）；重名自动加序号后缀、保留用户看到的名字。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio

from meeting.service.runtime_shared import (
    WINDOWS_RESERVED_DEVICE_NAMES,
    ActionError,
)
from meeting.workspace import canonical_dir

_log = logging.getLogger("meeting.service.runtime")

#: 场次目录下的**固定**子目录名（与服务端和纪要提示词两侧共享的产品约定；
#: 不设配置键、不可改名）
MATERIALS_DIR_NAME = "meeting_materials"

#: 清洗后文件名的长度上限（字符）：给「序号后缀」留出余量，也挡住病态长名
#: （真写不进去的那一档由 OSError 按盘上的话如实回错，这里只收掉明显越界的名字）
MATERIAL_NAME_MAX_CHARS = 120

#: 名字清洗到空时的兜底名（用户给的名字一个可用字符都没剩时的如实落点）
FALLBACK_MATERIAL_NAME = "未命名"

#: Windows 文件名的非法字符（正斜杠与反斜杠另在「只留最后一段」那一步切掉）
_ILLEGAL_NAME_CHARS = '<>:"|?*'


def sanitize_material_name(raw: Any) -> str:
    """把一个不可信的文件名清洗成**可以直接放进资料目录的名字**。

    五步，次序固定：

      1. **只留最后一段**：两种分隔符（`/` 与 `\\`）都切开取末段——`..\\..\\session.json`
         这样的穿越名在这里就被剥成裸文件名，解析结果永远落在资料目录这一层；
      2. **非法字符与控制字符换成 `_`**：Windows 文件名里的 `<>:"|?*` 与 0x00–0x1F、0x7F；
      3. **掐掉结尾的点与空格**：Windows 落盘时本就忽略它们（`name. ` 与 `name` 是同一个
         文件），落盘前收掉，清单与盘面才对得上；
      4. **保留设备名前置 `_`**：`CON` / `NUL` / `COM1`…（按扩展名前的那一段判，
         大小写不敏感）——不处理的话 `CON.pdf` 在 Windows 上写不进去；
      5. **超长截断（保留扩展名）**、**清到空交兜底名**。

    清洗**不报错**：名字是用户给的东西，服务只保证「落得进去、解析得回来」，
    不对内容做任何校验（`docs/design-intent.md` 第 6 条的同一姿态）。
    """
    text = FALLBACK_MATERIAL_NAME if raw is None else str(raw)
    # ① 只留最后一段路径（两种分隔符都切）
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    # ② 非法字符与控制字符
    cleaned = "".join(
        "_" if (ord(char) < 0x20 or ord(char) == 0x7F or char in _ILLEGAL_NAME_CHARS) else char
        for char in text
    )
    # ③ 掐首尾空白与结尾的点/空格
    cleaned = cleaned.strip().rstrip(". ")
    # ④ 保留设备名前置 `_`（判扩展名之前的那一段；表是 runtime_shared 的单一权威表）
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_DEVICE_NAMES:
        cleaned = "_" + cleaned
    # ⑤ 超长截断（保留扩展名）；清到空交兜底名
    if len(cleaned) > MATERIAL_NAME_MAX_CHARS:
        root, ext = os.path.splitext(cleaned)
        keep = max(1, MATERIAL_NAME_MAX_CHARS - len(ext)) if ext else MATERIAL_NAME_MAX_CHARS
        cleaned = (root[:keep] + ext) if ext else root[:MATERIAL_NAME_MAX_CHARS]
    return cleaned or FALLBACK_MATERIAL_NAME


def _iso_from_stamp(stamp: float) -> str:
    """文件 mtime → 本地时区的 ISO 时间戳（秒精度，与 `now_iso()` 同一形状）。"""
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")


def _sequence_name(stored: str, counter: int) -> str:
    """重名时的序号后缀名：`报告.pdf` → `报告 (2).pdf`（用户看到的名字原样保留）。"""
    root, ext = os.path.splitext(stored)
    return f"{root} ({counter}){ext}"


class RuntimeMaterialsMixin:
    """会议资料层：目标目录校验、清单、落盘位次、上传的域编排与单文件寻址（删除的盘上动作在路由侧）。"""

    # 目标目录（两种：当前活跃场次 / 注册表登记过的场次）

    def materials_target(self, raw_dir: Any) -> str:
        """资料要进哪一场的目录：先认**当前活跃场次**，再认**注册表登记过的场次**。

        其余一律拒收，口径与 `GET /api/sessions/{id}` 同一套寻址（404 + 通用失败信封）：
        目录已不在原地回 `session_dir_missing`、目录在但两样都不是回 `invalid_setting`。
        活跃场次放最前：录制中场次当然在注册表里（`start` 顺手登记过），但用户可以
        一边录一边把那一行从清单摘掉（`workspace_remove` 只摘名单）——那一档资料照收，
        「给纪要 AI 的 context」跟着场次走、不跟着清单行走。
        """
        directory = canonical_dir(raw_dir)
        session = self.snapshot.session
        if (
            session is not None
            and os.path.normcase(str(session.dir)) == os.path.normcase(directory)
        ):
            return directory
        # 活跃场次之外的两档寻址判读收在 `_require_session_dir()` 一处（404 两档，
        # 与单场状态/稿子正文同一份）；不在清单那一档的失败句是资料自己的话。
        return self._require_session_dir(
            directory,
            unregistered_message=(
                "资料只进两种目录：服务的当前活跃场次、工作台清单里登记过的场次；"
                "这一场两样都不是"
            ),
        )

    # 清单（目录就是清单，没有 JSON 记录）

    def materials_listing(self, raw_dir: Any) -> dict[str, Any]:
        """`GET …/materials` 的应答体：`{dir, materials_dir, items}`。

        每条三格：`name` / `size_bytes` / `added_at`（贴上时刻 = 文件修改时间）。
        **目录就是清单**：只读 `meeting_materials/` 的名目，没有任何记录文件可核对；
        子目录还不在（一场还没传过资料）时 `items` 是空数组、**目录也不建**——
        「首次上传时由服务创建」是上传那一侧的事。清单按名字排序（casefold），
        次序确定，界面与契约样本都吃得到确定值。
        """
        directory = self.materials_target(raw_dir)
        materials_dir = Path(directory) / MATERIALS_DIR_NAME
        items: list[dict[str, Any]] = []
        if materials_dir.is_dir():
            try:
                entries = list(materials_dir.iterdir())
            except OSError as exc:
                raise ActionError(
                    "persist_failed",
                    f"资料目录读不出来（{type(exc).__name__}: {exc}）",
                    status=500,
                    detail={"error": f"{type(exc).__name__}: {exc}"},
                ) from exc
            for item in entries:
                try:
                    if not item.is_file():
                        continue  # 只列文件；目录里混进的子目录不冒充一条资料
                    stat = item.stat()
                except OSError:
                    continue  # 清单与删除竞态的那一条如实跳过（下一刷就没了）
                items.append(
                    {
                        "name": item.name,
                        "size_bytes": int(stat.st_size),
                        "added_at": _iso_from_stamp(stat.st_mtime),
                    }
                )
        items.sort(key=lambda row: row["name"].casefold())
        return {
            "dir": str(directory),
            "materials_dir": str(materials_dir),
            "items": items,
        }

    # 落盘位次（上传的前半段：清洗 + 建目录 + 重名让位）

    def materials_reserve(self, raw_dir: Any, raw_name: Any) -> Path:
        """给一次上传定下**落盘路径**：目标校验 → 名字清洗 → 建目录 → 重名让位。

        `raw_name` 必给（空白也算没给）→ 400 `invalid_setting`；清洗规则见
        `sanitize_material_name()`。`meeting_materials/` 在这一步创建（**首次上传时
        由服务创建**；场次目录本身不在原地时 `mkdir` 如实失败 → 500 `persist_failed`，
        不悄悄替用户重建工作区）。重名自动加序号后缀、保留用户看到的名字：
        `报告.pdf` 已在 → `报告 (2).pdf`。
        """
        directory = self.materials_target(raw_dir)
        name = "" if raw_name is None else str(raw_name).strip()
        if not name:
            raise ActionError(
                "invalid_setting",
                "上传要 `name`（贴进去的文件名；盘上重名时自动加序号后缀）",
                status=400,
                detail={
                    "key": "name",
                    "value": "" if raw_name is None else str(raw_name),
                    "legal": ["一个文件名；目录就是清单，名字就是这一条的身份证"],
                },
            )
        materials_dir = Path(directory) / MATERIALS_DIR_NAME
        try:
            materials_dir.mkdir(exist_ok=True)
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"资料目录建不出来（{type(exc).__name__}: {exc}）",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        stored = sanitize_material_name(name)
        path = materials_dir / stored
        counter = 2
        while path.exists():
            path = materials_dir / _sequence_name(stored, counter)
            counter += 1
        return path

    def materials_next_free(self, path: Path) -> Path:
        """「查完重名到真正落盘」之间被人抢了位的兜底：从这条被占的路径往后找下一个空位。

        路由那一侧用独占创建（`xb`）落盘，`FileExistsError` 时拿这一把换下一个序号名
        ——检查与创建之间的竞态窗口因此只会多花一次让位，不会把别人正在写的文件截断。
        名字里已带的序号后缀先剥回去再数（`报告 (2).pdf` 被抢 → 找 `报告 (3).pdf`）。
        """
        materials_dir = path.parent
        stem, ext = os.path.splitext(path.name)
        stripped = re.fullmatch(r"(?P<base>.*?) \(\d+\)", stem)
        if stripped is not None:
            stem = stripped.group("base")
        counter = 2
        while True:
            candidate = materials_dir / f"{stem} ({counter}){ext}"
            if not candidate.exists():
                return candidate
            counter += 1

    # 上传的域编排（路由层只留 HTTP 面；`routes._api_materials_upload` 是它的 HTTP 面）

    #: `from` 档（选择器文件模式）拷贝源文件时的块大小：一块一兆，原样字节、不整段进内存
    MATERIALS_COPY_CHUNK_BYTES = 1 << 20

    async def materials_receive(
        self,
        raw_dir: Any,
        raw_name: Any,
        *,
        body: Any = None,
        from_param: str | None = None,
    ) -> dict[str, Any]:
        """一次上传的域编排：`from` 档源校验 → 占名 → 流式写盘 → 读数。

        - `from` 档（选择器文件模式）：`from_param` 必须指到**已存在的普通
          文件**（先验再占名），给到不存在的文件回 400 `invalid_setting`
          （`detail = {key: "from", value, legal}`），此时盘上什么都不落；给了 `from`
          时 `body`（请求正文那路字节流）被忽略，字节从盘上那个**已有文件流式拷入**；
        - 正文档：`body` 是请求正文的字节流（`request.stream()`），收到多少写多少；
        - 写盘走**独占创建**（`xb`）+ 异步文件（`anyio`，写在工作线程上、事件循环照转，
          心跳与三个 SSE 通道不被一次大上传按住）；「查完重名到真正落盘」之间被人抢了
          位的竞态由 `materials_next_free()` 让位重试，不截断别人正在写的文件；
        - 中途失败（盘上出岔 / 客户端断开）把**半截文件收走**，不留残骸；盘上出岔按
          500 `persist_failed` 如实上抛（失败信封由路由层套）。

        成功交回一条读数（`materials_record()`：`{name, size_bytes, added_at}`，
        贴上时刻 = 落盘那一刻的文件修改时间，不保留原文件的时间戳）。
        """
        source_path: Path | None = None
        if from_param is not None and from_param.strip() != "":
            source_path = Path(from_param)
            if not source_path.is_file():
                raise ActionError(
                    "invalid_setting",
                    f"要拷入的文件不存在：{from_param}",
                    status=400,
                    detail={
                        "key": "from",
                        "value": from_param,
                        "legal": ["已存在的文件路径（目录选择器文件模式给的那一个）"],
                    },
                )

        path = self.materials_reserve(raw_dir, raw_name)

        def _discard_partial() -> None:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 残骸清理失败只留痕
                _log.warning("上传失败后的半截文件收不走：%s", path)

        try:
            while True:
                try:
                    handle = await anyio.open_file(path, "xb")
                    break
                except FileExistsError:
                    # 查完重名到真正落盘之间被人抢了位：让到下一个序号名再试
                    path = self.materials_next_free(path)
            try:
                if source_path is None:
                    async for chunk in body:
                        await handle.write(chunk)
                else:
                    # from 档：流式拷贝盘上那份源文件（原样字节，一块一兆，不整段进内存）
                    source = await anyio.open_file(source_path, "rb")
                    try:
                        while True:
                            chunk = await source.read(self.MATERIALS_COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            await handle.write(chunk)
                    finally:
                        await source.aclose()
            finally:
                await handle.aclose()
        except ActionError:
            _discard_partial()
            raise
        except OSError as exc:
            _discard_partial()
            raise ActionError(
                "persist_failed",
                f"资料写盘失败（{type(exc).__name__}: {exc}）",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        except BaseException:
            _discard_partial()
            raise
        material = self.materials_record(path)
        _log.info("会议资料落盘：%s（%d 字节）", path, material["size_bytes"])
        return material

    # 单文件寻址（取原文件 / 删除）

    def materials_item_path(self, raw_dir: Any, raw_name: Any) -> tuple[str, Path]:
        """取原文件 / 删除那一条的盘上位置：目标校验 + 同一套名字清洗。

        清洗在寻址这一侧**再过一遍**（与上传同源）：URL 里手写的穿越段在这里被剥成
        裸文件名，解析结果永远落在 `meeting_materials/` 这一层，`session.json` 等
        场次文件够不着。
        """
        directory = self.materials_target(raw_dir)
        stored = sanitize_material_name(raw_name)
        path = Path(directory) / MATERIALS_DIR_NAME / stored
        return directory, path

    def materials_record(self, path: Path) -> dict[str, Any]:
        """一条资料的读数（上传成功体与清单同一形状）：`{name, size_bytes, added_at}`。"""
        stat = path.stat()
        return {
            "name": path.name,
            "size_bytes": int(stat.st_size),
            "added_at": _iso_from_stamp(stat.st_mtime),
        }


__all__ = [
    "FALLBACK_MATERIAL_NAME",
    "MATERIALS_DIR_NAME",
    "MATERIAL_NAME_MAX_CHARS",
    "RuntimeMaterialsMixin",
    "sanitize_material_name",
]
