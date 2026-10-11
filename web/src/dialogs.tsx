import type { ReactNode } from 'react';

/**
 * 一只确认弹窗（`dlg` 体系的最小那只）：一句话标题、一段正文、取消 + 确认两颗。
 *
 * 从 `WorkspaceHome.tsx` 搬出共用（160d1a78）：非空目录收编 / 从列表移除 /
 * 「新会议会打断」三问之外，资料区的删除确认与大文件确认也是同一副骨架 ——
 * 弹窗的视觉语言（`dlg__layer` / `dlg` / `dlg__title` / `dlg__text` / `dlg__acts`）
 * 全站只有这一种。
 */
export function ConfirmDialog({
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
