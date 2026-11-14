import type { ReactNode } from 'react';

/**
 * 弹窗骨架：遮罩层 + 弹窗壳 + 标题行 + 底部动作行。中段的正文归调用方（children）。
 */
export function DialogFrame({
  title,
  label,
  className,
  modal = true,
  children,
  acts,
}: {
  title: string;
  label?: string;
  className?: string;
  modal?: boolean;
  children?: ReactNode;
  acts: ReactNode;
}): ReactNode {
  return (
    <div className="dlg__layer" role="presentation">
      <div
        className={className === undefined ? 'dlg' : `dlg ${className}`}
        {...(modal ? { role: 'dialog', 'aria-modal': true, 'aria-label': label } : {})}
      >
        <div className="dlg__title">{title}</div>
        {children}
        <div className="dlg__acts">{acts}</div>
      </div>
    </div>
);
}

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
    <DialogFrame
      title={title}
      label={title}
      acts={
        <>
          <button className="btn" type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button className="btn btn--primary" type="button" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className="dlg__text">{text}</div>
    </DialogFrame>
);
}
