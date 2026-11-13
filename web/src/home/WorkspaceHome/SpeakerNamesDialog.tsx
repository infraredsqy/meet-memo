import { useState } from 'react';
import type { ReactNode } from 'react';
import { DialogFrame } from '../../dialogs';
import { duplicateNameHolders, type NamesRowView } from '../names';

/**
 * 一只改名字的弹窗（人名节点，R10 第四条 / R30）：一行一个身份
 * （路标签 + 编号 + 现名/待确认），输入框 + 确认，动作回包即重问单场状态（当场生效）。
 *
 * 身份读数来自服务（单场状态的 `speakers` 一格），名字跟着这一场的目录走——界面
 * 只递话，落盘在服务侧那一半。两个编号叫同一个名合法：撞名时提示一句
 * 「编号是分开的，可以重名」，提交照走（ui-design §5.2 的既有口径）；这句话只在
 * 动作**成功之后**才说——失败那一档弹窗不留成功话术（review-1 m3）。
 */
export function SpeakerNamesDialog({
  rows,
  busy,
  onRename,
  onClose,
}: {
  rows: NamesRowView[];
  busy: boolean;
  /** 确认一颗：打改名动作 + 重问单场状态；失败画在首页的失败块，弹窗只清提示、收忙。 */
  onRename: (key: string, name: string) => Promise<void>;
  onClose: () => void;
}): ReactNode {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const confirmRow = (row: NamesRowView): void => {
    const name = (drafts[row.key] ?? '').trim();
    if (!name) {
      setNote('请先为该行填写名字。');
      return;
    }
    setPending(row.key);
    void onRename(row.key, name).then(
      () => {
        // 撞名提示挪进成功分支（review-1 m3）：动作结果落地之前不冒「已提交」。
        setNote(
          duplicateNameHolders(rows, row.key, name).length > 0
            ? '编号相互独立，允许重名；已按输入提交。'
            : null,
);
        setPending(null);
        setDrafts((current) => ({ ...current, [row.key]: '' }));
      },
      () => {
        // 失败清掉撞名提示、不留成功话术：失败归首页的失败块如实说（review-1 m3）。
        setNote(null);
        setPending(null);
      },
);
  };

  return (
    <DialogFrame
      title="说话人名字"
      label="说话人名字"
      acts={
        <button className="btn" type="button" disabled={pending !== null} onClick={onClose}>
          关闭
        </button>
      }
    >
      <p className="hint">
        名字随该工作区目录保存，生成纪要时按这里确认的名字填写；编号按路区分。
      </p>
      {rows.length === 0 && (
        <p className="empty">该工作区暂无待命名的身份（录音过的工作区才有本地、远端或编号）。</p>
)}
      {rows.map((row) => (
        <div className="picker__row" key={row.key}>
          <button className="picker__pick" type="button" onClick={() => confirmRow(row)}>
            <span className="picker__name num">{row.title}</span>
            <span className="picker__kind">{row.name ?? '待确认'}</span>
          </button>
          <input
            className="field__input"
            type="text"
            value={drafts[row.key] ?? ''}
            placeholder={row.name ?? '待确认'}
            aria-label={`${row.title} 的名字`}
            disabled={pending !== null || busy}
            onChange={(event) => {
              const value = event.target.value;
              setDrafts((current) => ({ ...current, [row.key]: value }));
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') confirmRow(row);
            }}
          />
          <button
            className="btn btn--small btn--primary"
            type="button"
            disabled={pending !== null || busy}
            onClick={() => confirmRow(row)}
          >
            确认
          </button>
        </div>
))}
      {note !== null && <p className="hint">{note}</p>}
    </DialogFrame>
);
}
