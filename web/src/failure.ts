import {
  ActionFailedError,
  ActionForbiddenError,
} from './api';
import type { ErrorCode } from './contract';

/** 失败信封的 `detail` 原文（一把开放袋子 —— 6.7：界面只认自己会画的那几把键）。 */
export type FailureDetail = Record<string, unknown>;

/**
 * 失败那一档的读法（`docs/contract.md` 6.4 与 6.7）。
 *
 * 这一支从 `App.tsx` 抽出来单独放（那两个文件都不带 JSX）：分档的口径属于「怎么读服务的
 * 应答」，与怎么把它画到页面上是两件事，而 `web/tsconfig.node.json` 那条线（测试走的那条）
 * 不开 `jsx`，`App.tsx` 因此进不了用例 —— 逻辑放这儿，用例就能直接验。
 *
 * 两档的来路不同，形状也不同：
 *
 * - `forbidden`：请求被中间件挡在端点之前（缺必需请求头 / Host 不对），服务回的是**一行
 *   纯文本**。它**没有失败信封**，因此也**没有 `code`** —— 早先这里把它折成
 *   `code: 'invalid_setting'` 一起显示，那个 code 服务在这条路径上根本不会回，
 *   画出来是界面自己编的（复审抓到的那一格）。
 * - `envelope`：走到了端点，服务回了失败信封，`code` 与 `detail` 都按第 4 章读。
 *
 * **两样都留着**：`lines` 是摊平后给人看的（通用失败块直接用），`fields` 是 `detail`
 * 原文（按 `code` + `detail` 里**有哪些键**分派画法的那几条要读它 —— 例如会中总结的
 * 五档失败，见 `live/summary.ts`）。摊平是有损的（键名被拼进句子里了），所以分派不许
 * 从 `lines` 上做字符串匹配（契约 6.7 明说：不按 `message` 做字符串匹配）。
 */
export type ActionFailureView =
  | { kind: 'forbidden'; status: number; text: string }
  | {
      kind: 'envelope';
      code: ErrorCode;
      message: string;
      detail: string[];
      fields: FailureDetail;
    };

/** 把动作失败的 `detail` 按「有哪个键就显示哪个键」摊平（第 6 章第 7 条）。 */
export function describeDetail(detail: FailureDetail): string[] {
  const lines: string[] = [];
  for (const [key, value] of Object.entries(detail)) {
    if (Array.isArray(value)) {
      // failed_steps 是 {id, error} 数组，逐条列出来；其余数组直接 join。
      if (value.length > 0 && typeof value[0] === 'object' && value[0] !== null) {
        for (const item of value as Array<Record<string, unknown>>) {
          lines.push(`${key}.${String(item['id'] ?? '?')}：${String(item['error'] ?? '')}`);
        }
      } else {
        lines.push(`${key}：${value.join(' / ')}`);
      }
      continue;
    }
    lines.push(
      `${key}：${typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}`,
    );
  }
  return lines;
}

/** 把一次动作的异常收成上面两档之一。 */
export function failureFrom(error: unknown): ActionFailureView {
  if (error instanceof ActionForbiddenError) {
    // 403 走的是中间件自己那条出口：无 code、无 detail，整句如实显示。
    return { kind: 'forbidden', status: error.status, text: error.text };
  }
  if (error instanceof ActionFailedError) {
    const fields = error.detail as FailureDetail;
    return {
      kind: 'envelope',
      code: error.code,
      message: error.message,
      detail: describeDetail(fields),
      fields,
    };
  }
  return {
    kind: 'envelope',
    code: 'invalid_setting',
    message: error instanceof Error ? error.message : String(error),
    detail: [],
    fields: {},
  };
}
