import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { browseDirs, createDir } from '../api/sessions';
import { failureFrom } from '../failure';
import { formatBytes } from '../materials/model';
import type { DirRow, DirsResponse } from '../contract';

/**
 * 目录选择器弹窗（从工作台首页原样搬出，起多一种**文件模式**给资料区用；目录模式多出
 * **当前这一层**与**新建目录**两样）。
 *
 * 两种模式、一套交互与视觉语言：
 * - **目录模式**：清单顶上多一行「当前这一层」，地址栏下面一行名字框 + 「新建目录」；
 *   单击选中、双击进下层，确认钮「选定这个目录」；
 * - **文件模式**（`?files=1`）：同一层多出**文件行**，文件单击选中、确认钮换成「加入资料」；
 *   文件模式不摆当前层行与新建行（确认只收文件）。
 */

/** 名目 → 挑了之后走哪一档（判据的唯一出处是 `api/sessions.ts` 的 `probeDecision()`，这里只转手）。`missing` 名目到不了选择器。 */
export type PickPath = { dir: string; kind: 'empty' | 'meeting' | 'unknown' | 'missing' };

export type PickerPick =
  | { what: 'dir'; dir: string; kind: PickPath['kind'] }
  | { what: 'file'; name: string; path: string; sizeBytes: number };

/** 子目录行与「当前这一层」行共用同一套读法。 */
function kindLabel(item: DirRow): string {
  if (item.kind === 'empty') return '空目录';
  if (item.kind === 'meeting') return item.suspected ? '疑似会议目录' : '会议目录';
  return '普通目录';
}

/** 新行按名字插进清单，次序跟服务同一条判据（名字小写序）。 */
function insertSorted(dirs: DirRow[], row: DirRow): DirRow[] {
  const next = [...dirs, row];
  next.sort((a, b) => {
    const left = a.name.toLowerCase();
    const right = b.name.toLowerCase();
    return left < right ? -1 : left > right ? 1 : 0;
  });
  return next;
}

/**
 * 选择器的取数（换层 / 跳层 / 新建）+ 取数失败那一行。
 *
 * `withFiles` 打开时请求带 `?files=1`（文件模式）；老路径不带头，服务照旧不给 `files`。
 */
export function useDirBrowse(withFiles: boolean): {
  browse: DirsResponse | null;
  browseError: string | null;
  browseInto: (dir: string | null) => void;
  jumpTo: (dir: string) => Promise<string | null>;
  /** 在 `parent` 那一层里新建一个目录：成功 resolve 新那一行（清单就地补上行），失败 resolve 服务给的那句人话。 */
  createIn: (parent: string, name: string) => Promise<DirRow | string>;
  reset: () => void;
} {
  const [browse, setBrowse] = useState<DirsResponse | null>(null);
  const [browseError, setBrowseError] = useState<string | null>(null);

  const reset = useCallback((): void => {
    setBrowse(null);
    setBrowseError(null);
  }, []);

  /** 清当前层地取新层（清单双击 / 向上层那半边）。老路径（目录模式）保持单参调用 —— 请求形状一个字节不动。 */
  const browseInto = useCallback(
    (dir: string | null): void => {
      setBrowse(null);
      setBrowseError(null);
      void (withFiles ? browseDirs(dir, { withFiles: true }) : browseDirs(dir)).then(
        (data) => setBrowse(data),
        (error) => {
          const view = failureFrom(error);
          setBrowseError(view.kind === 'envelope' ? view.message : view.text);
        },
);
    },
    [withFiles],
);

  /**
   * 地址栏跳层：**不清当前层**地取新层。跳不过去把服务回的那句原话带上「跳不过去」
   * resolve 回来，层原地不动。
   */
  const jumpTo = useCallback(
    (dir: string): Promise<string | null> =>
      (withFiles ? browseDirs(dir, { withFiles: true }) : browseDirs(dir)).then(
        (data) => {
          setBrowse(data);
          return null;
        },
        (error) => {
          const view = failureFrom(error);
          const message = view.kind === 'envelope' ? view.message : view.text;
          return `跳不过去：${message}`;
        },
),
    [withFiles],
);

  /**
   * 就地新建：打 `POST /api/dirs`，成功把新那一行按服务同一条次序插进**当前层**的清单、
   * resolve 行给弹窗去选中。服务不清洗不改写名字：非法名原样拒回来。
   */
  const createIn = useCallback(
    (parent: string, name: string): Promise<DirRow | string> =>
      createDir(parent, name).then(
        (result) => {
          const row = result.directory;
          setBrowse((current) =>
            current !== null && current.cwd === parent
              ? { ...current, dirs: insertSorted(current.dirs, row) }
              : current,
);
          return row;
        },
        (error) => (error instanceof Error ? error.message : String(error)),
),
    [],
);

  return { browse, browseError, browseInto, jumpTo, createIn, reset };
}

export function DirPicker({
  mode = 'dir',
  browse,
  onBrowse,
  onJump,
  onCreate,
  onPick,
  onClose,
}: {
  mode?: 'dir' | 'file';
  browse: DirsResponse | null;
  onBrowse: (dir: string | null) => void;
  /** 地址栏回车：按输入的路径跳层。成功 resolve `null`；跳不过去把那句说清的话 resolve 回来。 */
  onJump: (dir: string) => Promise<string | null>;
  /** 就地新建（目录模式才有）：成功 resolve 新那一行，失败 resolve 那句人话。 */
  onCreate?: (parent: string, name: string) => Promise<DirRow | string>;
  /** 确认一颗：选中项交给调用方，按它的名目走（目录登记 / 文件拷入资料）。 */
  onPick: (pick: PickerPick) => void;
  onClose: () => void;
}): ReactNode {
  const fileMode = mode === 'file';
  const [picked, setPicked] = useState<PickerPick | null>(null);
  const [addr, setAddr] = useState('');
  const [jumpError, setJumpError] = useState<string | null>(null);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // 层一换（首次取到现场、跳层成功）：选中不跨层带走，地址栏与名字框重置成新层的现场。
  // 判据是层（`cwd`）不是应答对象：同一层里补一行（新建成功）不清选中。
  const cwd = browse?.cwd ?? null;
  useEffect(() => {
    setPicked(null);
    setJumpError(null);
    setCreateError(null);
    setNewName('');
    setAddr(cwd ?? '');
  }, [cwd]);

  const jumpFromAddr = (): void => {
    const target = addr.trim();
    // 空输入与「输入的就是当前层」都不动层；失败那句由 onJump resolve 回来画。
    if (browse === null || target === '' || target === browse.cwd) return;
    setJumpError(null);
    void onJump(target).then((error) => {
      if (error !== null) setJumpError(error);
    });
  };

  /** 就地新建：落点就是正在浏览的这一层，名字是输入框里那一份原话。 */
  const createHere = (): void => {
    if (browse === null || creating || newName.trim() === '') return;
    setCreateError(null);
    setCreating(true);
    void (onCreate ?? (() => Promise.resolve('这一档没有新建目录的入口')))(browse.cwd, newName).then(
      (result) => {
        setCreating(false);
        if (typeof result === 'string') {
          // 失败如实画一句：层不动、清单不动、输入框留着原话好改
          setCreateError(result);
          return;
        }
        setNewName('');
        setPicked({ what: 'dir', dir: result.dir, kind: result.kind });
      },
);
  };

  const confirmLabel = fileMode ? '加入资料' : '选定这个目录';
  /** 文件模式只收文件：选中目录时确认钮置灰（目录是走进去的，不是放资料的）。 */
  const confirmReady = picked !== null && (!fileMode || picked.what === 'file');

  return (
    <div className="dlg__layer" role="presentation">
      <div
        className="dlg dlg--picker"
        role="dialog"
        aria-modal="true"
        aria-label={fileMode ? '选择要放进资料的文件' : '选择目录'}
      >
        <div className="dlg__title">{fileMode ? '选择要放进资料的文件' : '选择目录'}</div>
        <p className="hint">
          {fileMode
            ? '单击选中、双击进下层；选中文件后点「加入资料」，文件内容原样拷进这一场的资料。'
            : '单击选中、双击进下层；地址栏可直接输入路径回车跳转。清单顶上是正在浏览的这一层，单击选中；在名字框里起个名字点「新建目录」，建完直接选中。'}
        </p>
        <div className="picker__addrrow">
          <input
            className="field__input picker__addr"
            type="text"
            value={addr}
            aria-label="当前层路径，可输入路径回车跳转"
            disabled={browse === null}
            onChange={(event) => setAddr(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') jumpFromAddr();
            }}
          />
          <button
            className="btn"
            type="button"
            disabled={browse === null || browse.up === null}
            onClick={() => browse !== null && browse.up !== null && onBrowse(browse.up)}
          >
            向上一层
          </button>
        </div>
        {jumpError !== null && <p className="picker__jumperr">{jumpError}</p>}
        {!fileMode && (
          <div className="picker__newrow">
            <input
              className="field__input"
              type="text"
              value={newName}
              aria-label="新目录的名字"
              placeholder="在这里起名字，就建在这一层"
              disabled={browse === null || creating}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') createHere();
              }}
            />
            <button
              className="btn"
              type="button"
              disabled={browse === null || creating || newName.trim() === ''}
              title="在正在浏览的这一层里新建一个目录；名字就是你输入的，建完直接选中"
              onClick={createHere}
            >
              新建目录
            </button>
          </div>
)}
        {createError !== null && <p className="picker__newerr">{createError}</p>}
        <div
          className="picker__list"
          role="listbox"
          aria-label={fileMode ? '这一层的子目录与文件' : '这一层的子目录'}
        >
          {browse === null && <p className="empty">正在取目录…</p>}
          {browse !== null && browse.dirs.length === 0 && !fileMode && (
            <p className="empty">这一层没有子目录。</p>
)}
          {browse !== null && !fileMode && browse.here.kind !== 'missing' && (
            (() => {
              const here = browse.here;
              const on = picked !== null && picked.what === 'dir' && picked.dir === here.dir;
              return (
                <div
                  className={`picker__row picker__row--here ${on ? 'picker__row--on' : ''}`}
                  role="option"
                  aria-selected={on}
                  aria-label={`选定当前这一层：${here.name}`}
                  tabIndex={0}
                  onClick={() => setPicked({ what: 'dir', dir: here.dir, kind: here.kind })}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      setPicked({ what: 'dir', dir: here.dir, kind: here.kind });
                    }
                  }}
                >
                  <span className="picker__name">{here.name}</span>
                  <span className="picker__kind">当前这一层 · {kindLabel(here)}</span>
                </div>
);
            })()
)}
          {browse !== null &&
            browse.dirs.map((item) => (
              <div
                key={item.dir}
                className={`picker__row ${
                  picked !== null && picked.what === 'dir' && picked.dir === item.dir
                    ? 'picker__row--on'
                    : ''
                }`}
                role="option"
                aria-selected={
                  picked !== null && picked.what === 'dir' && picked.dir === item.dir
                }
                tabIndex={0}
                onClick={() => setPicked({ what: 'dir', dir: item.dir, kind: item.kind })}
                onDoubleClick={() => onBrowse(item.dir)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    setPicked({ what: 'dir', dir: item.dir, kind: item.kind });
                  }
                }}
              >
                <span className="picker__name">{item.name}</span>
                <span className="picker__kind">{kindLabel(item)}</span>
              </div>
))}
          {browse !== null &&
            fileMode &&
            (browse.files ?? []).map((file) => {
              const on = picked !== null && picked.what === 'file' && picked.path === file.dir;
              return (
                <div
                  key={file.dir}
                  className={`picker__row ${on ? 'picker__row--on' : ''}`}
                  role="option"
                  aria-selected={on}
                  tabIndex={0}
                  onClick={() =>
                    setPicked({ what: 'file', name: file.name, path: file.dir, sizeBytes: file.size_bytes })
                  }
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      setPicked({
                        what: 'file',
                        name: file.name,
                        path: file.dir,
                        sizeBytes: file.size_bytes,
                      });
                    }
                  }}
                >
                  <span className="picker__name">{file.name}</span>
                  <span className="picker__kind num">{formatBytes(file.size_bytes)}</span>
                </div>
);
            })}
          {browse !== null &&
            fileMode &&
            browse.dirs.length === 0 &&
            (browse.files ?? []).length === 0 && <p className="empty">这一层什么都没有。</p>}
        </div>
        <div className="dlg__acts">
          <button className="btn" type="button" onClick={onClose}>
            关闭
          </button>
          <button
            className="btn btn--primary"
            type="button"
            disabled={!confirmReady}
            title={fileMode && picked !== null && picked.what === 'dir' ? '先挑一个文件；目录用双击走进去' : undefined}
            onClick={() => picked !== null && confirmReady && onPick(picked)}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
);
}
