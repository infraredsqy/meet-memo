"""RuntimeCatalogMixin 的「目录选择器」域分片：GET /api/dirs 与 POST /api/dirs。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.service.runtime_shared import ActionError, WINDOWS_RESERVED_DEVICE_NAMES
from meeting.workspace import canonical_dir, classify_workspace_dir

_log = logging.getLogger("meeting.service.runtime")

#: 新建目录名字的长度上限（Windows 单段路径成分的实用上限）
_DIR_NAME_MAX_CHARS = 255


def checked_new_dir_name(name: Any) -> str:
    """把要新建的目录名过一遍**拒绝式**校验，合格原样交回，不合格给一句人话。

    与资料上传那套清洗（`runtime_materials.sanitize_material_name()`）相反：名字必须
    **用户亲手起**（软件可以替你建目录，但仍不自动命名、
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
    if stem in WINDOWS_RESERVED_DEVICE_NAMES:
        raise ValueError(f"「{stem}」是 Windows 的保留设备名，不能当目录名")
    if len(name) > _DIR_NAME_MAX_CHARS:
        raise ValueError(f"目录名太长（{len(name)} 字符，上限 {_DIR_NAME_MAX_CHARS}）")
    return name


class RuntimeCatalogPickerMixin:
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

        新口径（软件可以替你建目录，但仍不自动命名、不自动挑落点）：
        落点是**正在浏览的这一层**（`dir` 省略时主目录，与 `GET /api/dirs` 同一起点）、
        名字必须用户给（`checked_new_dir_name()` 拒绝式校验，不清洗不改写）。只建一层、
        不递归；不做删除、不做改名。

        拒绝档三条，各给一句能看懂的人话，不留半截状态（动手前校验全过才碰盘）：

        - 那一层不存在 / 读不动 → 400 `invalid_setting`（与 `GET /api/dirs` 同一档）；
        - 名字非法 → 400 `invalid_setting`（`detail.key = "name"`，`legal` 里是规则原话）；
        - 同名已存在（目录或文件都算占位）→ 409 `dir_exists`——这一片自己的 `code`；
        - 盘上出岔（无权限等）→ 500 `persist_failed`，盘上不落半截东西
          （`mkdir()` 单调用原子：建不出来就是什么都没有）。

        成功体里的 `directory` 是新建那一行的名目判读（刚建出来是 `empty`，判读照走
        同一套 `classify_workspace_dir()`，不替它下别的结论）；本写入**不提交、不涨
        `revision`、不广播**——选择器的现场独立于四态快照。
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
