import {
  ActionFailedError,
  ActionForbiddenError,
} from './api';
import type { ErrorCode } from './contract';

export type FailureDetail = Record<string, unknown>;

/**
 * **两样都留着**：`lines` 是摊平后给人看的（通用失败块直接用），`fields` 是 `detail`
 * 原文（按 `code` + `detail` 里**有哪些键**分派画法的那几条要读它）。摊平是有损的
 * （键名被拼进句子里了），所以分派不许从 `lines` 上做字符串匹配。
 */
export type ActionFailureView =
  | { kind: 'forbidden'; status: number; text: string }
  | { kind: 'unclassified'; text: string }
  | {
      kind: 'envelope';
      code: ErrorCode;
      message: string;
      detail: string[];
      fields: FailureDetail;
    };

export function describeDetail(detail: FailureDetail): string[] {
  const lines: string[] = [];
  for (const [key, value] of Object.entries(detail)) {
    if (Array.isArray(value)) {
      // failed_steps 是 {id, error} 数组，逐条列出来。
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

export function failureFrom(error: unknown): ActionFailureView {
  if (error instanceof ActionForbiddenError) {
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
    kind: 'unclassified',
    text: error instanceof Error ? error.message : String(error),
  };
}
