import { useEffect, useState, type ReactNode } from 'react';

/**
 * 工作台首页（空闲态那一屏）：左边"我这些会"，右边选中的那一场。
 *
 * 界面手上**只有服务给的原文**：注册表来自 `GET /api/sessions`（服务侧配置目录里那份
 * `workspaces.json`，界面只读、不落任何本地存储 —— 契约章 6 第 2 条）；目录选择器来自
 * `GET /api/dirs`（起点用户主目录）；打开/收编/删除走 `POST /api/actions` 的
 * `workspace_open` / `workspace_remove`。会中那一路（`start` 之后）由外面那层闸
 * （`App.tsx`）切到 `LiveView`，本组件不碰会中屏。
 */

import { postAction } from '../api';
import { confirmWorkspaceOpen } from './confirmWorkspaceOpen';
import { browseDirs, fetchSessions } from '../api/sessions';
import { decisionFor, emptyStateLines, initialSelection, startWhy, statusLabel } from './model';
import {
  POST_MINUTES_ACTION,
  POST_OFFLINE_ACTION,
  postButtonsView,
  postIdleNote,
} from './post';
import { failureFrom, type ActionFailureView } from '../failure';
import type { DirsResponse, SessionRow, SessionsResponse } from '../contract';

// ---------------------------------------------------------------------------
// 小料
// ---------------------------------------------------------------------------

/** 失败块（位置归首页的清单下方；与 LiveView 的同形不同处共用同一套 `fail` 类名）。 */
function HomeFailure({ failure }: { failure: ActionFailureView }): ReactNode {
  if (failure.kind === 'forbidden') {
    return (
      <div className="fail">
        <div className="fail__why">请求被拦截 · HTTP {failure.status}（未到达动作端点）</div>
        <div className="fail__next">{failure.text}</div>
      </div>
    );
  }
  return (
    <div className="fail">
      <div className="fail__why">
        <code>{failure.code}</code> · {failure.message}
      </div>
      {failure.detail.length > 0 && (
        <ul className="fail__next">
          {failure.detail.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 一只确认弹窗（非空目录当工作区 / 从列表移除共用这一只）。 */
function ConfirmDialog({
  title,
  text,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  text: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}): ReactNode {
  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg" role="dialog" aria-modal="true" aria-label={title}>
        <div className="dlg__title">{title}</div>
        <div className="dlg__text">{text}</div>
        <div className="dlg__acts">
          <button className="btn" type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button className="btn btn--primary" type="button" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 目录选择器弹窗：起点用户主目录（服务给 `home`），`up` 回上层。只挑**已有**目录。 */
function DirPicker({
  browse,
  onBrowse,
  onPick,
  onClose,
}: {
  browse: DirsResponse | null;
  onBrowse: (dir: string | null) => void;
  /** 点一项：这一项的 dir 交给调用方，按它的名目走三档（本弹窗不开动作）。 */
  onPick: (item: { dir: string; kind: 'empty' | 'meeting' | 'unknown' | 'missing' }) => void;
  onClose: () => void;
}): ReactNode {
  return (
    <div className="dlg__layer" role="presentation">
      <div className="dlg dlg--picker" role="dialog" aria-modal="true" aria-label="选择已有目录">
        <div className="dlg__title">选择已有目录（起点主目录）</div>
        <p className="hint">起点是用户主目录；选择器只用来挑、软件不建目录。</p>
        <div className="picker__list" role="listbox" aria-label="这一层的子目录">
          {browse === null && <p className="empty">正在取目录…</p>}
          {browse !== null && browse.dirs.length === 0 && (
            <p className="empty">这一层没有子目录。</p>
          )}
          {browse !== null &&
            browse.dirs.map((item) => (
              <div
                key={item.dir}
                className="picker__row"
                role="option"
                aria-selected="false"
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') onPick(item);
                }}
              >
                <button className="picker__pick" type="button" onClick={() => onPick(item)}>
                  <span className="picker__name">{item.name}</span>
                  <span className="picker__kind">
                    {item.kind === 'empty'
                      ? '空目录'
                      : item.kind === 'meeting'
                        ? item.suspected
                          ? '疑似会议目录'
                          : '会议目录'
                        : '普通目录'}
                  </span>
                </button>
                <button
                  className="btn btn--small btn--ghost"
                  type="button"
                  title="把这一层换成这个目录，继续往深处挑（review-2 m1）"
                  onClick={() => onBrowse(item.dir)}
                >
                  进这一层
                </button>
              </div>
            ))}
        </div>
        <div className="dlg__acts">
          <button
            className="btn"
            type="button"
            disabled={browse === null || browse.up === null}
            onClick={() => browse !== null && onBrowse(browse.up)}
          >
            向上一层
          </button>
          <button className="btn btn--primary" type="button" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 名目 → 挑了之后走哪一档（spec 三情形的唯一出处是 `api/sessions.ts` 的 `probeDecision()`，
 * 这里只转手）。`missing` 名目到不了选择器——`GET /api/dirs` 只摆存在的目录；真到了不走
 * 确认弹窗，显式失败（不可当作可收编入口，见 `openFromPicker`）。
 */
type PickPath = { dir: string; kind: 'empty' | 'meeting' | 'unknown' | 'missing' };

/** 两个按钮的动作名 → 界面上的名字（与 `docs/requirements.md` R26 的叫法逐字对齐）。 */
const POST_LABELS: Readonly<Record<string, string>> = {
  [POST_OFFLINE_ACTION]: '生成会后精细稿',
  [POST_MINUTES_ACTION]: '直接出纪要',
};

// ---------------------------------------------------------------------------
// 首页本体
// ---------------------------------------------------------------------------

export function WorkspaceHome(): ReactNode {
  const [sessions, setSessions] = useState<SessionsResponse | null>(null);
  const [selection, setSelection] = useState<string | null>(null);
  const [failure, setFailure] = useState<ActionFailureView | null>(null);
  const [busy, setBusy] = useState(false);
  // 选择器的两层弹窗与待确认的一只
  const [picker, setPicker] = useState(false);
  const [browse, setBrowse] = useState<DirsResponse | null>(null);
  const [confirm, setConfirm] = useState<{ what: 'unknown' | 'remove'; dir: string } | null>(null);
  /** 选择器取数失败的暂存（弹窗里画一行，不顶掉首页的失败块）。 */
  const [browseError, setBrowseError] = useState<string | null>(null);
  /** 会后那两个按钮的动作回执（一句话：这一趟已起 / 被拒的原因在失败块里）。 */
  const [postNote, setPostNote] = useState<string | null>(null);

  // -- 取数 ---------------------------------------------------------------

  /** 刷一遍注册表列表；首次加载时顺带按服务给的 `recent` 定选中行。 */
  const loadList = (): Promise<SessionsResponse | null> =>
    fetchSessions().then(
      (data) => {
        setSessions(data);
        setSelection((current) =>
          current === null ? initialSelection(data.sessions, data.recent) : current,
        );
        return data;
      },
      (error) => {
        setFailure(failureFrom(error));
        return null;
      },
    );

  const reloadAfterAction = (): void => {
    setBusy(true);
    void loadList().then(
      () => setBusy(false),
      () => setBusy(false),
    );
  };

  // 挂载即取一次列表（review-2 C1）：不靠动作回调顺带刷出，首次打开/整页刷新都能
  // 见到列表或空态；与 SSE 快照竞速无碍 —— loadList 自己 setSessions。
  // [] 只在挂载跑一次；loadList 只经由 setter 依赖最新 state，无闭包陈旧问题。
  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- 动作 ----------------------------------------------------------------

  const openDir = (
    dir: string,
    kindHint: 'empty' | 'meeting' | 'need-confirm',
  ): void => {
    if (kindHint === 'need-confirm') {
      setConfirm({ what: 'unknown', dir });
      return;
    }
    setFailure(null);
    setBusy(true);
    void postAction('workspace_open', { dir })
      .then(
        () => undefined,
        (error) => setFailure(failureFrom(error)),
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  const startSelected = (): void => {
    if (selection === null) return;
    const row = sessions?.sessions.find((item) => item.dir === selection) ?? null;
    if (row === null || startWhy(row) !== null) return;
    setFailure(null);
    setBusy(true);
    void postAction('start', { dir: row.dir, mode: 'both' })
      .then(
        () => undefined,
        (error) => setFailure(failureFrom(error)),
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  /**
   * 会后那两个按钮：各自打一个动作，回执只说「这一趟跑的是哪一场」。
   *
   * 起成功之后**不当场宣称跑完了**（那一趟可能跑一小时），只把动作回执如实写在下面，
   * 并重取一遍列表（`state` / `has_offline` 这些现场读数都从服务来）。
   */
  const postSelected = (action: string): void => {
    if (selection === null) return;
    setFailure(null);
    setPostNote(null);
    setBusy(true);
    void postAction(action, { dir: selection })
      .then(
        () => setPostNote(`${POST_LABELS[action]}：这一趟已起（跑成什么样由这一场盘上的状态回答）。`),
        (error) => {
          setFailure(failureFrom(error));
          setPostNote(null);
        },
      )
      .then(() => {
        setBusy(false);
        reloadAfterAction();
      });
  };

  // -- 选择器换层 -----------------------------------------------------------

  const browseInto = (dir: string | null): void => {
    setBrowse(null);
    setBrowseError(null);
    void browseDirs(dir).then(
      (data) => setBrowse(data),
      (error) => {
        const view = failureFrom(error);
        setBrowseError(view.kind === 'forbidden' ? view.text : view.message);
      },
    );
  };

  // -- 画 -------------------------------------------------------------------

  const selected: SessionRow | null = (sessions?.sessions ?? []).find(
    (row) => row.dir === selection,
  ) ?? null;
  const whyForSelected = selected === null ? null : startWhy(selected);
  const post = selected === null ? null : postButtonsView(selected);
  const lines = emptyStateLines();

  const openFromPicker = (item: { dir: string; kind: PickPath['kind'] }): void => {
    setPicker(false);
    if (item.kind === 'missing') {
      // 理论上不可达（选择器只摆存在的目录）；真到达就如实失败，不该弹「确认收编」。
      setFailure(failureFrom(new Error(`该目录已不存在：${item.dir}`)));
      return;
    }
    openDir(item.dir, decisionFor(item.kind));
  };

  return (
    <main className="shell page--home" data-top-state="idle">
      <header className="topbar">
        <div className="topbar__left">
          <span className="title-md">会议工作台</span>
        </div>
        <div className="topbar__main">
          <button
            className="btn btn--primary btn--start"
            type="button"
            disabled={busy}
            onClick={() => {
              setPicker(true);
              browseInto(null);
            }}
          >
            新会议
          </button>
          <button
            className="btn"
            type="button"
            disabled={busy || selected === null || whyForSelected !== null}
            onClick={startSelected}
          >
            开始录音
          </button>
        </div>
        <div className="topbar__right">
          <span className="hint">清单住在服务端 · 界面只读</span>
        </div>
      </header>

      <section className="home">
        <aside className="home__list" aria-label="工作区列表">
          {sessions === null && failure === null && <p className="hint">正在取列表…</p>}
          {sessions !== null && sessions.sessions.length === 0 && (
            <div className="ws-empty">
              <div className="ws-empty__logo" aria-hidden="true">
                ◇
              </div>
              <div className="ws-empty__title">{lines.title}</div>
              <div className="ws-empty__body">{lines.body}</div>
            </div>
          )}
          {sessions !== null && sessions.sessions.length > 0 && (
            <div className="list wslist" role="listbox" aria-label="这些会">
              {sessions.sessions.map((row) => (
                <div
                  key={row.dir}
                  className={`wslist__row ${row.dir === selection ? 'wslist__row--on' : ''}`}
                  role="option"
                  aria-selected={row.dir === selection}
                  tabIndex={0}
                  onClick={() => setSelection(row.dir)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') setSelection(row.dir);
                  }}
                >
                  <span className="wslist__name">{row.session_id ?? row.dir}</span>
                  <span className="wslist__path num">{row.dir}</span>
                  <span
                    className={`wslist__state ${
                      !row.dir_exists || row.state === 'failed' ? 'wslist__state--dim' : ''
                    }`}
                  >
                    {statusLabel(row)}
                  </span>
                  <button
                    className="btn btn--small btn--ghost wslist__remove"
                    type="button"
                    title="只从列表移除，文件一个字节不动；以后可用「新会议」收回"
                    disabled={busy}
                    onClick={(event) => {
                      event.stopPropagation();
                      setConfirm({ what: 'remove', dir: row.dir });
                    }}
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          )}
        </aside>

        <div className="home__aside">
          {selected === null ? (
            <p className="hint">左边挑一场，或按「新会议」。</p>
          ) : (
            <div className="wspane">
              <div className="wspane__title">{selected.session_id ?? selected.dir}</div>
              <p className="kv">
                <span className="kv__k">目录</span>
                <span className="kv__v num">{selected.dir}</span>
              </p>
              <p className="kv">
                <span className="kv__k">状态</span>
                <span className="kv__v">{statusLabel(selected)}</span>
              </p>
              {selected.date !== null && (
                <p className="kv">
                  <span className="kv__k">日期</span>
                  <span className="kv__v num">{selected.date}</span>
                </p>
              )}
              {whyForSelected !== null && <p className="wspane__why">{whyForSelected}</p>}

              {post !== null && (
                <div className="wspane__post" aria-label="会后">
                  <div className="wspane__posttitle">会后</div>
                  <div className="wspane__postacts">
                    <button
                      className="btn btn--primary"
                      type="button"
                      disabled={busy || !post.canPostOffline}
                      title="走离线模型把整段录音重跑一遍，出带说话人编号的那一份稿；慢，占显卡"
                      onClick={() => postSelected(POST_OFFLINE_ACTION)}
                    >
                      生成会后精细稿
                    </button>
                    <button
                      className="btn"
                      type="button"
                      disabled={busy || !post.canPostMinutes}
                      title="把这一场手上有的稿子交给 DSH 出纪要；缺会后稿就按现有档位来"
                      onClick={() => postSelected(POST_MINUTES_ACTION)}
                    >
                      直接出纪要
                    </button>
                  </div>
                  <p className="wspane__postline">{post.offlineNotice}</p>
                  {post.why !== null && <p className="wspane__why">{post.why}</p>}
                  {postNote !== null && <p className="wspane__postline">{postNote}</p>}
                  <p className="hint">{postIdleNote()}</p>
                </div>
              )}
            </div>
          )}
          {failure !== null && <HomeFailure failure={failure} />}
        </div>
      </section>

      {picker && (
        <DirPicker
          browse={browse}
          onBrowse={browseInto}
          onPick={openFromPicker}
          onClose={() => {
            setPicker(false);
            setBrowse(null);
          }}
        />
      )}
      {picker && browseError !== null && (
        <div className="dlg__layer" role="presentation">
          <div className="dlg">
            <div className="dlg__title">目录浏览失败</div>
            <div className="dlg__text">{browseError}</div>
            <div className="dlg__acts">
              <button className="btn" type="button" onClick={() => setPicker(false)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {confirm !== null && confirm.what === 'unknown' && (
        <ConfirmDialog
          title="把非空目录当工作区？"
          text="这个目录不是空的、里面也没有会议材料。确认的话，它会被登记进左边的清单（目录里的文件原样不动）。"
          confirmLabel="确认收编"
          busy={busy}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            setFailure(null);
            setBusy(true);
            void confirmWorkspaceOpen(dir, {
              onFail: setFailure,
              reload: () => {
                setBusy(false);
                reloadAfterAction();
              },
            });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm !== null && confirm.what === 'remove' && (
        <ConfirmDialog
          title="从列表移除这一场？"
          text="只把它从左边的清单里拿掉；工作区目录与里面所有文件一个字节不动。之后随时可用「新会议」把它收回来。"
          confirmLabel="从列表移除"
          busy={busy}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            setFailure(null);
            setBusy(true);
            void postAction('workspace_remove', { dir })
              .then(
                () => undefined,
                (error) => setFailure(failureFrom(error)),
              )
              .then(() => {
                setBusy(false);
                reloadAfterAction();
              });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}
    </main>
  );
}
