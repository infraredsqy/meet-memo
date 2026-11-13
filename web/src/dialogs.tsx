import type { ReactNode } from 'react';

/**
 * 弹窗骨架（`dlg` 体系的公共那半边，web-dialog-markup-handcopied）：遮罩层 +
 * 弹窗壳 + 标题行 + 底部动作行。中段的正文归调用方（children）。
 *
 * `dlg__layer` / `dlg` / `dlg__title` / `dlg__text` / `dlg__acts` 的视觉语言
 * 全站只有这一种 —— 骨架也只剩这一份：确认弹窗（`ConfirmDialog`）、说话人名字、
 * 提交文字、原图查看与两只目录浏览失败弹窗都从这里铺。
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
  /** 弹窗自己的名字（`aria-label`）；老壳（目录浏览失败那两只）不挂，省略即可。 */
  label?: string;
  /** 弹窗壳的附加类（原图查看的 `dlg--viewer`）。 */
  className?: string;
  /** 挂不挂 `role="dialog" aria-modal="true"`（目录浏览失败那两只老壳本来就不挂）。 */
  modal?: boolean;
  children?: ReactNode;
  /** 底部动作行（`dlg__acts`）里的那几颗。 */
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

/**
 * 一只确认弹窗（`dlg` 体系的最小那只）：一句话标题、一段正文、取消 + 确认两颗。
 *
 * 从工作台首页（`home/WorkspaceHome/`）搬出共用：非空目录收编 / 从列表移除 /
 * 「新会议会打断」三问之外，资料区的删除确认与大文件确认也是同一副骨架。
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
