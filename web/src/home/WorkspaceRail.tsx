import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';

import { probeDecision } from '../api/sessions';
import { ConfirmDialog, DialogFrame } from '../dialogs';
import { confirmWorkspaceOpen } from './confirmWorkspaceOpen';
import { runAction } from './dispatch';
import { DirPicker, useDirBrowse, type PickerPick } from './DirPicker';
import { ThemeToggle } from './ThemeToggle';
import { rowDurationLabel } from './model';
import { rowStatusView } from './progress';
import { failureFrom, type ActionFailureView } from '../failure';
import type { GpuBusyView } from './progress';
import type { PostJobCell, SessionsResponse } from '../contract';
import {
  applyRailWidth,
  clampRailWidth,
  railGripAllowedNow,
  railWidthBoundsPx,
  RAIL_GRIP_MEDIA_QUERY,
  RAIL_WIDTH_STORAGE_KEY,
  remPixelSize,
  storedRailWidth,
} from './railWidth';

/** 浏览器存档的读法：隐私模式等存不进的场合按没存过算。 */
function readStoredRailWidth(): string | null {
  try {
    return window.localStorage.getItem(RAIL_WIDTH_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * 「显卡忙不忙」那一格（R31）：读法与判据在 `home/progress.ts` 的 `gpuBusyView()`，
 * 画法只有这一份 —— 会中屏顶栏（LiveTopBar.tsx 的 topbar__right）与这条栏的栏底两处同款
 * （web-gpucell-markup-duplicated）。全站没有全局进度条 —— 进度跟着各自的工作区走。
 */
export function GpuCell({ gpu }: { gpu: GpuBusyView }): ReactNode {
  return (
    <span
      className={`gpucell ${gpu.busy ? 'gpucell--busy' : ''}`}
      title={gpu.note}
      data-busy={gpu.busy ? 'true' : 'false'}
    >
      <span className={`glyph ${gpu.mark}`} aria-hidden="true" />
      {gpu.label}
    </span>
  );
}

/**
 * 常驻工作区栏（左栏那条，`home__list`）：会中屏与工作台**共用同一条**（d61829f1）——
 * 两屏读起来像同一个应用的两面。这一条里住着：铭牌、添加工作区、清单、（可选的）
 * 选中那一场的小详情、栏底那格忙闲；目录选择器与收编 / 移除两只确认弹窗也从这里走。
 *
 * 判据与动作的分界：**登记类动作（`workspace_open` / `workspace_remove`）归这条栏**，
 * 做完经 `onReload()` 让调用方重取列表、经 `onFailure()` 把失败交给调用方的失败块；
 * **行激活归调用方**（`onRowActivate`）—— 工作台点行是本地选中，会中屏点行要先发
 * `workspace_open`（成功后才切详情），两屏语义不同，这条栏不替它们拍板。
 *
 * `detail` 是会中屏塞进来的那一小块（选中工作区的读数 + 那颗开始录音钮）；
 * 工作台不给 —— 它的详情是右栏那整块 `wspane`。
 *
 * 栏宽可拖（`./railWidth`）：拖柄是右沿那条分隔线本身，两屏共用同一条宽度
 * （读根节点上的 `--rail-w`）、上下限夹住、拖完记住（只记宽度这一件事）、
 * 双击回默认；窄屏那一档不摆拖柄。
 */
export function WorkspaceRail({
  sessions,
  selection,
  postJobs,
  gpu,
  busy,
  onRowActivate,
  onReload,
  onFailure,
  detail = null,
}: {
  sessions: SessionsResponse | null;
  selection: string | null;
  postJobs: readonly PostJobCell[];
  gpu: GpuBusyView;
  /** 调用方的忙（它自己的动作在飞时，这条栏的按钮一并按不动）。 */
  busy: boolean;
  /** 行被激活（点击 / 回车 / 空格）。 */
  onRowActivate: (dir: string) => void;
  /** 登记动作落地后重取列表（调用方的列表是唯一一份）。 */
  onReload: () => void;
  onFailure: (failure: ActionFailureView | null) => void;
  detail?: ReactNode;
}): ReactNode {
  const [picker, setPicker] = useState(false);
  /** 选择器的取数（换层 / 跳层 / 失败那一行）——本体在 `./DirPicker`（目录模式）。 */
  const dirBrowse = useDirBrowse(false);
  /** 待确认的两只弹窗：收编非空目录 / 从列表移除。 */
  const [confirm, setConfirm] = useState<
    { what: 'unknown'; dir: string } | { what: 'remove'; dir: string } | null
  >(null);
  /** 这条栏自己动作的忙（与调用方的忙取或，按钮才不会两边各按各的）。 */
  const [railBusy, setRailBusy] = useState(false);
  const busyOr = busy || railBusy;

  // -- 栏宽（可拖，两屏共用一条） -------------------------------------------
  /**
   * 这条栏的宽度：存档说了算，没存过按默认档。宽度是界面偏好，只记这一件事 ——
   * 写盘只有 `RAIL_WIDTH_STORAGE_KEY` 一格，不记任何会话状态；两屏各自挂载都读
   * 同一份存档，所以在哪一屏拖完另一屏也是那一档。三档与夹法在 `./railWidth`。
   */
  const [railWidth, setRailWidth] = useState<number>(
    () => storedRailWidth(readStoredRailWidth, remPixelSize()) ?? railWidthBoundsPx(remPixelSize()).def,
  );
  /** 拖动 listeners 要拿「此刻的宽度」，走 ref 转手，闭包不握旧值。 */
  const railWidthRef = useRef(railWidth);
  railWidthRef.current = railWidth;
  /** 拖柄的出身：宽屏分栏那档才有；窄屏（左栏摞到内容上面）没有可拖的两栏。 */
  const [gripAllowed, setGripAllowed] = useState(railGripAllowedNow);
  /** 拖动的起点（按下那一刻的指针位置与栏宽）。 */
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const [dragging, setDragging] = useState(false);

  // 宽度落到根节点的 `--rail-w` 上：工作台 `.home` 与会中 `.wsbar-page` 两处 grid
  // 列宽都读这一个变量（live.css），两屏天然同一条宽度。
  useEffect(() => {
    const rootEl = typeof document !== 'undefined' ? document.documentElement : null;
    if (!rootEl) return; // node 侧挂载用例没有真 DOM（document 是个空壳）
    applyRailWidth(railWidth, rootEl.style);
  }, [railWidth]);

  // 窄档 ↔ 宽档跨越时拖柄跟着出没（matchMedia 的 change 一拍就翻，不必重挂载）。
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    let mql: MediaQueryList;
    try {
      mql = window.matchMedia(RAIL_GRIP_MEDIA_QUERY);
    } catch {
      return;
    }
    const update = (): void => setGripAllowed(!mql.matches);
    update();
    if (typeof mql.addEventListener !== 'function') return;
    mql.addEventListener('change', update);
    return () => mql.removeEventListener('change', update);
  }, []);

  const onGripPointerDown = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) return; // 主键才拖
    event.preventDefault(); // 不起文本选区、不触发拖拽
    dragRef.current = { startX: event.clientX, startWidth: railWidthRef.current };
    setDragging(true);
    const target = event.currentTarget;
    if (typeof target.setPointerCapture === 'function') {
      target.setPointerCapture(event.pointerId);
    }
  };

  // 拖动中：指针横移多少，栏宽就在存档那档上跟着走多少，越界夹住；松手才写盘
  // （写盘只在拖完那一刻，拖动过程不折腾 localStorage）。
  useEffect(() => {
    if (!dragging) return;
    const start = dragRef.current;
    if (start === null) return;
    const remPx = remPixelSize();
    const move = (event: PointerEvent): void => {
      setRailWidth(clampRailWidth(start.startWidth + (event.clientX - start.startX), remPx));
    };
    const finish = (): void => {
      try {
        window.localStorage.setItem(RAIL_WIDTH_STORAGE_KEY, String(Math.round(railWidthRef.current)));
      } catch {
        // 存不进就只活这一页，宽度照常生效
      }
      setDragging(false);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
    };
  }, [dragging]);

  /** 双击拖柄回到默认档：退路。存档清掉，宽度回到缺省那一档。 */
  const onGripDoubleClick = (): void => {
    try {
      window.localStorage.removeItem(RAIL_WIDTH_STORAGE_KEY);
    } catch {
      // 存档清不掉也无碍：宽度先回默认
    }
    setRailWidth(railWidthBoundsPx(remPixelSize()).def);
  };

  /** 登记本体（`workspace_open` 动作 + 失败块 + 重取；骨架在 `./dispatch`）。 */
  const openDirNow = (dir: string): void => {
    void runAction({
      name: 'workspace_open',
      args: { dir },
      onFail: onFailure,
      setBusy: setRailBusy,
      settle: onReload,
    });
  };

  /** 名目 → 挑了之后走哪一档（判据唯一出处 `api/sessions.ts` 的 `probeDecision()`）。 */
  const openFromPicker = (pick: PickerPick): void => {
    setPicker(false);
    if (pick.what !== 'dir') return; // 这只选择器是目录模式：文件那一档归资料区
    if (pick.kind === 'missing') {
      // 理论上不可达（选择器只摆存在的目录）；真到达就如实失败，不该弹「确认收编」。
      onFailure(failureFrom(new Error(`该目录已不存在：${pick.dir}`)));
      return;
    }
    const decision = probeDecision(pick.kind);
    if (decision === 'empty' || decision === 'meeting') {
      openDirNow(pick.dir);
      return;
    }
    setConfirm({ what: 'unknown', dir: pick.dir });
  };

  const rows = sessions?.sessions ?? [];

  return (
    <>
      <aside className="home__list" aria-label="工作区">
        <div className="home__listhead">
          <div className="title-sm">会议工作台</div>
          <ThemeToggle />
        </div>
        <button
          className="btn btn--ghost home__new"
          type="button"
          disabled={busyOr}
          onClick={() => {
            setPicker(true);
            dirBrowse.browseInto(null);
          }}
        >
          添加工作区
        </button>

        {sessions === null && <p className="hint">正在读取列表…</p>}
        {rows.length > 0 && (
          <>
            {/* 清单头顶这一行只说两件事：这一列是什么、共几个 */}
            <div className="wslist__head">
              <span>工作区</span>
              <span className="num">{rows.length}</span>
            </div>
            <div className="list wslist" role="listbox" aria-label="工作区列表">
              {rows.map((row) => {
                const cell = rowStatusView(row, postJobs);
                const dur = rowDurationLabel(row);
                return (
                  <div
                    key={row.dir}
                    className={`wslist__row ${row.dir === selection ? 'wslist__row--on' : ''}`}
                    role="option"
                    aria-selected={row.dir === selection}
                    tabIndex={0}
                    onClick={() => onRowActivate(row.dir)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') onRowActivate(row.dir);
                    }}
                  >
                    <span className="wslist__name">{row.session_id ?? row.dir}</span>
                    <span className="wslist__path num">{row.dir}</span>
                    {/* 盘上录到的总时长：清单里一眼看出这一场多长 */}
                    {dur !== null && <span className="wslist__dur num">{dur}</span>}
                    {/* 行四档（d61829f1）：空白工作区 / 尚未开始 / 录音中 / 录音结束，
                        另有「目录不存在」如实保留。字 + 图形两条通道并存，
                        颜色只是冗余的那一半。判据在 `home/progress.ts` 的 `rowStatusView()`。 */}
                    <span
                      className={`wslist__state wslist__state--${cell.tone} ${
                        cell.dim ? 'wslist__state--dim' : ''
                      }`}
                    >
                      <span className={`glyph ${cell.mark}`} aria-hidden="true" />
                      {cell.label}
                    </span>
                    <button
                      className="btn btn--small btn--ghost wslist__remove"
                      type="button"
                      title="仅从列表移除，不修改磁盘文件；可通过「添加工作区」重新加入"
                      disabled={busyOr}
                      onClick={(event) => {
                        event.stopPropagation();
                        setConfirm({ what: 'remove', dir: row.dir });
                      }}
                    >
                      删除
                    </button>
                  </div>
                );
              })}
            </div>
          </>
        )}

        {/* 会中屏塞进来的那小块：选中工作区的读数与那颗开始录音钮（工作台不给） */}
        {detail}

        {/* 忙闲一格住在这一列的末尾（R31）：读法在 `home/progress.ts` 的
            `gpuBusyView()`，画法与顶栏共用同一份（`GpuCell`）。 */}
        <div className="home__foot">
          <GpuCell gpu={gpu} />
        </div>
      </aside>

      {/* 拖柄 = 左栏右沿那条分隔线本身：1px 的线不长大，把手是线上一层透明的
          命中区（`live.css` 的 `.railgrip`，7px 一档），光标左右箭头；拖动改宽度
          （两屏共用、上下限夹住、松手记住），双击回到默认档。它是这条栏的兄弟
          而不是子元素：挂在两屏的 grid 容器上（`.home` / `.wsbar-page`），贴着
          `--rail-w` 那条分界整高铺开，窄屏摞成一栏的那档不出（gripAllowed）。 */}
      {gripAllowed && (
        <div
          className="railgrip"
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整左栏宽度；双击回到默认宽度"
          onPointerDown={onGripPointerDown}
          onDoubleClick={onGripDoubleClick}
        />
      )}

      {picker && (
        <DirPicker
          browse={dirBrowse.browse}
          onBrowse={dirBrowse.browseInto}
          onJump={dirBrowse.jumpTo}
          onCreate={dirBrowse.createIn}
          onPick={openFromPicker}
          onClose={() => {
            setPicker(false);
            dirBrowse.reset();
          }}
        />
      )}
      {picker && dirBrowse.browseError !== null && (
        <DialogFrame
          title="目录浏览失败"
          modal={false}
          acts={
            <button className="btn" type="button" onClick={() => setPicker(false)}>
              关闭
            </button>
          }
        >
          <div className="dlg__text">{dirBrowse.browseError}</div>
        </DialogFrame>
      )}

      {confirm !== null && confirm.what === 'unknown' && (
        <ConfirmDialog
          title="将该目录添加为工作区？"
          text="该目录非空，且不含会议材料。确认后将登记到左侧列表，目录内文件保持不变。"
          confirmLabel="确认添加"
          busy={busyOr}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            // 收编这一问过了（R20）：登记不打断任何在飞的活（一刀切已废），直接登记。
            onFailure(null);
            setRailBusy(true);
            void confirmWorkspaceOpen(dir, {
              onFail: onFailure,
              reload: () => {
                setRailBusy(false);
                onReload();
              },
            });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm !== null && confirm.what === 'remove' && (
        <ConfirmDialog
          title="从列表移除该工作区？"
          text="仅从左侧列表移除，工作区目录及其中的文件保持不变；之后可通过「添加工作区」重新加入。"
          confirmLabel="从列表移除"
          busy={busyOr}
          onConfirm={() => {
            const dir = confirm.dir;
            setConfirm(null);
            void runAction({
              name: 'workspace_remove',
              args: { dir },
              onFail: onFailure,
              setBusy: setRailBusy,
              settle: onReload,
            });
          }}
          onCancel={() => setConfirm(null)}
        />
      )}
    </>
  );
}
