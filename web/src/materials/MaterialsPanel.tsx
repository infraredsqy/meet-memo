import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react';
import {
  deleteMaterial,
  fetchMaterials,
  materialFileUrl,
  uploadMaterial,
  uploadMaterialFromDisk,
} from '../api/materials';
import { ConfirmDialog } from '../dialogs';
import { DirPicker, useDirBrowse, type PickerPick } from '../home/DirPicker';
import {
  dragFiles,
  eventTargetInEditable,
  filesOverLimit,
  formatBytes,
  isImageMaterial,
  materialKindLabel,
  materialTimeLabel,
  materialsFailureText,
  pasteImageFiles,
  sizeOverLimit,
  textMaterialName,
  type UploadTask,
} from './model';
import type { MaterialItem } from '../contract';

/**
 * 会议资料区（节点 160d1a78）：往这一场目录的 `meeting_materials/` 里放东西与管理的
 * 界面入口。会中工作台（`App.tsx`，目标目录 = 当前场次）与会后详情（`WorkspaceHome`，
 * 目标目录 = 选中那一行）各摆一块；服务端那一半是资料节点 285bf58c 的四条端点。
 *
 * **四个入口，摩擦都要低**（spec 口径）：
 * 1. **页面级粘贴**：`document` 上收 `paste`——剪贴板里是图片就收进资料；焦点在输入框 /
 *    文本域 / 可编辑元素里时不抢；纯文本不收（筛子与判据在 `./model`）；
 * 2. **拖拽**：文件拖进这一块即上传；
 * 3. **提交文字**：小弹窗，正文必填、标题可选；落成资料里的一个 md，标题即文件名、
 *    没填用时间戳名（`YYYYMMDD-HHMMSS.md`）；
 * 4. **文件选择器**：`DirPicker` 的文件模式——盘上的文件挑一条，「加入资料」把它
 *    原样拷进来（服务端 `?from=` 那一档；界面上没有那些字节）。
 *
 * 清单是 `GET /api/sessions/{id}/materials` 的原文（目录就是清单，界面不派生）；
 * 图片行走缩略图（`img src` 直接指到取原文件端点，服务不另做缩略文件）、点名字看原图
 * （浏览器端缩放）；多选删除走一次确认整批删（逐文件调 DELETE，硬删——工具内的
 * 保护本来就是软的）；单文件超过 100M 弹一句「确认要放进资料吗」，确认后照传——
 * 粘贴 / 拖拽 / 选文件三个上传入口同一道门，大小口径不按入口打折。
 * 逐文件给进行中 / 完成 / 失败的反馈，失败写明原因（网络 / 403 头哨 / 404 目录 /
 * 其他服务错误），不悄悄吞。
 */

/** 提交文字的小弹窗：正文必填、标题可选。 */
function SubmitTextDialog({
  sending,
  onSubmit,
  onClose,
}: {
  sending: boolean;
  /** 确认一颗：resolve `true` = 已放进资料（弹窗关）；`false` = 没放上去（弹窗留着，正文不丢）。 */
  onSubmit: (title: string, text: string) => Promise<boolean>;
  onClose: () => void;
}): ReactNode {
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [note, setNote] = useState<string | null>(null);

  const submit = (): void => {
    if (text.trim() === '') {
      setNote('正文必填：要放进资料的那段话不能是空的。');
      return;
    }
    setNote(null);
    void onSubmit(title, text).then((ok) => {
      if (!ok) setNote('没有放上去，原因在资料区那几行里；这段正文还留在这里。');
    });
  };

  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg" role="dialog" aria-modal="true" aria-label="提交文字">
        <div className="dlg__title">提交文字</div>
        <p className="hint">
          落成资料里的一份 Markdown：标题就是文件名（不填用贴上那一刻的时间戳），正文必填。
        </p>
        <input
          className="field__input"
          type="text"
          value={title}
          placeholder="标题（可不填）"
          aria-label="标题（可不填）"
          disabled={sending}
          onChange={(event) => setTitle(event.target.value)}
        />
        <textarea
          className="field__input mat__textinput"
          value={text}
          placeholder="正文（必填）"
          aria-label="正文（必填）"
          rows={6}
          disabled={sending}
          onChange={(event) => setText(event.target.value)}
        />
        {note !== null && <p className="picker__jumperr">{note}</p>}
        <div className="dlg__acts">
          <button className="btn" type="button" disabled={sending} onClick={onClose}>
            取消
          </button>
          <button
            className="btn btn--primary"
            type="button"
            disabled={sending}
            onClick={submit}
          >
            放进资料
          </button>
        </div>
      </div>
    </div>
  );
}

/** 大文件确认框压着的东西：粘贴 / 拖拽的一批 `File`，或选择器挑中的那一份盘上文件。 */
type PendingConfirm =
  | { what: 'files'; files: File[] }
  | { what: 'pick'; pick: Extract<PickerPick, { what: 'file' }> };

export function MaterialsPanel({
  dir,
  fetchImpl,
}: {
  dir: string;
  /** 覆盖 fetch（测试注入用）；缺省走全局。 */
  fetchImpl?: typeof fetch;
}): ReactNode {
  const [items, setItems] = useState<MaterialItem[] | null>(null);
  /** 清单取数失败那一行（如实画，不顶掉清单；给一颗重试）。 */
  const [listError, setListError] = useState<string | null>(null);
  /** 勾中待删的名字集合（多选删除一次确认整批删）。 */
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  /** 逐文件的上传反馈（进行中 / 完成 / 失败；失败写明原因）。 */
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  /** 大文件确认框压着的那批 / 那份（确认后照传；取消就什么都不发）。 */
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [textOpen, setTextOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  /** 删除那一趟的结果行（有没删成的把原因带上）。 */
  const [deleteNote, setDeleteNote] = useState<string | null>(null);
  /** 原图查看（图片行点名字；关掉即收，不落任何东西）。 */
  const [viewer, setViewer] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  /** 文件模式的选择器取数（`?files=1`）。 */
  const { browse, browseError, browseInto, jumpTo, reset } = useDirBrowse(true);

  const refresh = useCallback((): void => {
    void fetchMaterials(dir, { fetchImpl }).then(
      (data) => {
        setItems(data.items);
        setListError(null);
      },
      (error) => {
        setListError(materialsFailureText(error));
      },
    );
  }, [dir, fetchImpl]);

  // 换场次即重取：清单、勾选、反馈、结果行都是这一场的，不跨场带走。
  useEffect(() => {
    setItems(null);
    setChecked(new Set());
    setTasks([]);
    setListError(null);
    setDeleteNote(null);
    refresh();
  }, [refresh]);

  // -- 上传那一条链 ---------------------------------------------------------

  const taskSeqRef = useRef(0);
  /** 起一条任务并跑它：进行中 / 完成 / 失败（写明原因）三档逐文件反馈。 */
  const startTask = useCallback(
    (name: string, size: number, run: () => Promise<unknown>): Promise<boolean> => {
      taskSeqRef.current += 1;
      const key = `task-${taskSeqRef.current}`;
      setTasks((current) => [
        ...current,
        { key, name, size, status: 'uploading', note: null },
      ]);
      return run().then(
        () => {
          setTasks((current) =>
            current.map((task) => (task.key === key ? { ...task, status: 'done' } : task)),
          );
          return true;
        },
        (error) => {
          setTasks((current) =>
            current.map((task) =>
              task.key === key
                ? { ...task, status: 'failed', note: materialsFailureText(error) }
                : task,
            ),
          );
          return false;
        },
      );
    },
    [],
  );

  /** 一批文件逐个上传（顺序发，不并排压服务），全部落定后重取清单。 */
  const runUpload = useCallback(
    (files: File[]): void => {
      if (files.length === 0) return;
      void (async () => {
        for (const file of files) {
          await startTask(file.name, file.size, () =>
            uploadMaterial(dir, file.name, file, { fetchImpl }),
          );
        }
        refresh();
      })();
    },
    [dir, fetchImpl, refresh, startTask],
  );

  /** 入口共用的那道门：混着超大文件就先弹确认，确认后照传（不弹硬拒）。 */
  const addFiles = useCallback(
    (files: File[]): void => {
      if (files.length === 0) return;
      if (filesOverLimit(files).length > 0) {
        setPending({ what: 'files', files });
        return;
      }
      runUpload(files);
    },
    [runUpload],
  );

  const addFilesRef = useRef(addFiles);
  addFilesRef.current = addFiles;

  /**
   * 页面级粘贴（会中工作台 / 会后详情两页各有一块，一块恰一个面板）：焦点在输入框 /
   * 文本域 / 可编辑元素里时不抢；**只有图片项才收**，纯文本不收。
   * 挂载用例（react-test-renderer，node 环境）没有 document：没有监听挂点就跳过。
   */
  useEffect(() => {
    if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') {
      return;
    }
    const onPaste = (event: ClipboardEvent): void => {
      if (eventTargetInEditable(event.target)) return;
      const files = pasteImageFiles(
        Array.from(event.clipboardData?.items ?? []) as Parameters<typeof pasteImageFiles>[0],
      );
      if (files.length === 0) return;
      event.preventDefault();
      addFilesRef.current(files);
    };
    document.addEventListener('paste', onPaste);
    return () => document.removeEventListener('paste', onPaste);
  }, []);

  /** 提交文字：标题填了用它当文件名，没填用时间戳名；正文原样字节交上去。 */
  const submitText = useCallback(
    (title: string, text: string): Promise<boolean> => {
      const name = textMaterialName(title, new Date());
      const bytes = new TextEncoder().encode(text);
      return startTask(name, bytes.byteLength, () =>
        uploadMaterial(dir, name, bytes, { fetchImpl }),
      ).then((ok) => {
        if (ok) refresh();
        return ok;
      });
    },
    [dir, fetchImpl, refresh, startTask],
  );

  /** 文件模式挑中的那一份盘上文件：`?from=` 交给服务流式拷入（确认框确认后也走这里）。 */
  const uploadPick = useCallback(
    (pick: Extract<PickerPick, { what: 'file' }>): void => {
      void startTask(pick.name, pick.sizeBytes, () =>
        uploadMaterialFromDisk(dir, pick.name, pick.path, { fetchImpl }),
      ).then((ok) => {
        if (ok) refresh();
      });
    },
    [dir, fetchImpl, refresh, startTask],
  );

  const pickFromDisk = useCallback(
    (pick: PickerPick): void => {
      setPickerOpen(false);
      if (pick.what !== 'file') return;
      reset();
      // 大小口径不按入口打折（spec）：选择器挑中的超过 100M 也弹同一句
      // 「确认要放进资料吗」，确认后照传，取消则什么都不发。
      if (sizeOverLimit(pick.sizeBytes)) {
        setPending({ what: 'pick', pick });
        return;
      }
      uploadPick(pick);
    },
    [reset, uploadPick],
  );

  // -- 删除与查看 -----------------------------------------------------------

  const deleteChecked = useCallback((): void => {
    const names = [...checked];
    if (names.length === 0) return;
    setDeleting(true);
    void (async () => {
      let deleted = 0;
      const failures: string[] = [];
      // 一次确认整批删：逐文件调 DELETE，删不掉的（已不在 / 盘上出岔）把原因带回结果行，
      // 不悄悄吞。删除不走上传那几行任务——「已放进资料」那句话跟删除是两码事。
      for (const name of names) {
        try {
          await deleteMaterial(dir, name, { fetchImpl });
          deleted += 1;
        } catch (error) {
          failures.push(`${name}：${materialsFailureText(error)}`);
        }
      }
      setDeleting(false);
      setConfirmDelete(false);
      setChecked(new Set());
      setDeleteNote(
        failures.length === 0
          ? `已删除 ${deleted} 份。`
          : `删除了 ${deleted} 份，有没删成的：${failures.join('；')}`,
      );
      refresh();
    })();
  }, [checked, dir, fetchImpl, refresh]);

  const toggleCheck = useCallback((name: string): void => {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const onDrop = useCallback(
    (event: DragEvent<HTMLElement>): void => {
      event.preventDefault();
      setDragOver(false);
      addFilesRef.current(dragFiles(event.dataTransfer));
    },
    [],
  );

  const anyBusy = deleting || tasks.some((task) => task.status === 'uploading');
  const pendingOver =
    pending === null || pending.what !== 'files' ? [] : filesOverLimit(pending.files);

  return (
    <section
      className={`mat ${dragOver ? 'mat--drop' : ''}`}
      aria-label="资料"
      onDragOver={(event) => {
        event.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
    >
      <div className="mat__head">
        <span className="mat__title">资料</span>
        {items !== null && <span className="mat__count num">{items.length}</span>}
        <span className="mat__hint">图片可直接粘贴或拖进这一块</span>
        <div className="mat__acts">
          {checked.size > 0 && (
            <button
              className="btn btn--small btn--danger"
              type="button"
              disabled={anyBusy}
              onClick={() => setConfirmDelete(true)}
            >
              删除所选({checked.size})
            </button>
          )}
          <button
            className="btn btn--small"
            type="button"
            disabled={anyBusy}
            onClick={() => setTextOpen(true)}
          >
            提交文字
          </button>
          <button
            className="btn btn--small btn--ghost"
            type="button"
            disabled={anyBusy}
            onClick={() => {
              setPickerOpen(true);
              browseInto(null);
            }}
          >
            选文件…
          </button>
        </div>
      </div>

      {listError !== null && (
        <p className="mat__bad" role="status">
          {listError}{' '}
          <button className="btn btn--small btn--ghost" type="button" onClick={refresh}>
            重试
          </button>
        </p>
      )}
      {deleteNote !== null && <p className="mat__note">{deleteNote}</p>}

      {items !== null && items.length === 0 && listError === null && (
        <p className="empty">这一场还没有资料。粘贴图片、拖入文件，或用上面两个入口放东西进来。</p>
      )}

      {items !== null && items.length > 0 && (
        <div className="mat__list" role="listbox" aria-label="资料清单" aria-multiselectable="true">
          {items.map((item) => {
            const image = isImageMaterial(item.name);
            const on = checked.has(item.name);
            return (
              <div
                className={`mat__item ${on ? 'mat__item--on' : ''}`}
                role="option"
                aria-selected={on}
                key={item.name}
              >
                <input
                  type="checkbox"
                  className="mat__check"
                  aria-label={`选中 ${item.name}`}
                  checked={on}
                  onChange={() => toggleCheck(item.name)}
                />
                {image ? (
                  <img
                    className="mat__thumb"
                    src={materialFileUrl(dir, item.name)}
                    alt=""
                    loading="lazy"
                  />
                ) : (
                  <span className="tag mat__icon">{materialKindLabel(item.name)}</span>
                )}
                {image ? (
                  <button
                    className="mat__name"
                    type="button"
                    title="点开看原图"
                    onClick={() => setViewer(item.name)}
                  >
                    {item.name}
                  </button>
                ) : (
                  <span className="mat__name">{item.name}</span>
                )}
                <span className="mat__meta num">
                  {formatBytes(item.size_bytes)} · {materialTimeLabel(item.added_at)}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {tasks.length > 0 && (
        <div className="mat__tasks" role="list" aria-label="上传反馈">
          {tasks.map((task) => (
            <p
              className={`mat__task ${task.status === 'failed' ? 'mat__task--bad' : ''}`}
              role="listitem"
              key={task.key}
            >
              {task.status === 'uploading' && <span className="spinner" aria-hidden="true" />}
              {task.status === 'done' && (
                <span className="glyph glyph--check" aria-hidden="true" />
              )}
              {task.status === 'failed' && (
                <span className="glyph glyph--cross" aria-hidden="true" />
              )}
              {task.name} ·{' '}
              {task.status === 'uploading'
                ? '放进资料中…'
                : task.status === 'done'
                  ? `已放进资料（${formatBytes(task.size)}）`
                  : `没有放上去：${task.note}`}
            </p>
          ))}
        </div>
      )}

      {/* 大文件确认框（spec 口径：超过 100M 弹「确认要放进资料吗」，确认后照传；
          粘贴 / 拖拽 / 选文件三个上传入口同一副门） */}
      {pending !== null && (
        <ConfirmDialog
          title="确认要放进资料吗"
          text={
            pending.what === 'files'
              ? `有 ${pendingOver.length} 份超过 100 MB（${pendingOver
                  .map((file) => `${file.name}，${formatBytes(file.size)}`)
                  .join('；')}）。确认后整批照传。`
              : `这一份超过 100 MB（${pending.pick.name}，${formatBytes(
                  pending.pick.sizeBytes,
                )}）。确认后照传。`
          }
          confirmLabel="照传"
          busy={false}
          onConfirm={() => {
            const current = pending;
            setPending(null);
            if (current.what === 'files') runUpload(current.files);
            else uploadPick(current.pick);
          }}
          onCancel={() => setPending(null)}
        />
      )}

      {confirmDelete && (
        <ConfirmDialog
          title="删除选中的资料？"
          text={`将删除选中的 ${checked.size} 份资料，删了就找不回来（场次目录里别的文件一个字节不动）。`}
          confirmLabel="删除"
          busy={deleting}
          onConfirm={deleteChecked}
          onCancel={() => setConfirmDelete(false)}
        />
      )}

      {textOpen && (
        <SubmitTextDialog
          sending={anyBusy}
          /** 成功即收（弹窗的存亡归面板）；失败弹窗留着，正文还在输入框里。 */
          onSubmit={(title, text) =>
            submitText(title, text).then((ok) => {
              if (ok) setTextOpen(false);
              return ok;
            })
          }
          onClose={() => setTextOpen(false)}
        />
      )}

      {pickerOpen && (
        <DirPicker
          mode="file"
          browse={browse}
          onBrowse={browseInto}
          onJump={jumpTo}
          onPick={pickFromDisk}
          onClose={() => {
            setPickerOpen(false);
            reset();
          }}
        />
      )}
      {pickerOpen && browseError !== null && (
        <div className="dlg__layer" role="presentation">
          <div className="dlg">
            <div className="dlg__title">目录浏览失败</div>
            <div className="dlg__text">{browseError}</div>
            <div className="dlg__acts">
              <button className="btn" type="button" onClick={() => setPickerOpen(false)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 原图查看：原样字节就是取原文件端点那一副，缩放归浏览器（服务不另做缩略文件） */}
      {viewer !== null && (
        <div className="dlg__layer" role="presentation">
          <div className="dlg dlg--viewer" role="dialog" aria-modal="true" aria-label={viewer}>
            <div className="dlg__title">{viewer}</div>
            <img className="mat__view" src={materialFileUrl(dir, viewer)} alt={viewer} />
            <div className="dlg__acts">
              <button className="btn" type="button" onClick={() => setViewer(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
