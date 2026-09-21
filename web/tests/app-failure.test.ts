import { describe, expect, it } from 'vitest';
import { failureFrom } from '../src/failure';
import { ActionFailedError, ActionForbiddenError, ActionProtocolError } from '../src/api';

/**
 * 壳页把「动作没成」分几档画 —— `docs/contract.md` 6.4 要求 403 与失败信封**分开**。
 *
 * 复审抓到的那一格：403 曾被打成 `code: 'invalid_setting'` 塞进失败块里显示，
 * 而那个 `code` 服务在中间件那条路径上根本不会回 —— 画出来是界面自己编的一档。
 * 这里把分档口径钉住：403 有自己的 `kind`、自己带状态码与原文，**不冒充任何 `code`**。
 */
describe('动作失败的画法分档（6.4：403 与失败块分开）', () => {
  it('403：走 forbidden 那一档，不折成任何 ErrorCode', () => {
    const view = failureFrom(new ActionForbiddenError(403, '缺少必需请求头'));
    expect(view.kind).toBe('forbidden');
    if (view.kind !== 'forbidden') throw new Error('分支错了');
    expect(view.status).toBe(403);
    expect(view.text).toBe('缺少必需请求头');
    // 这一档**没有** code 这个字段 —— 有的话就说明它又被折回了失败块。
    expect('code' in view).toBe(false);
    expect('detail' in view).toBe(false);
  });

  it('失败信封：走 envelope 那一档，code 与 detail 按第 4 章读', () => {
    const view = failureFrom(
      new ActionFailedError(409, 'already_recording', '已经有一场在录', {
        session_id: '260921',
        dir: 'C:\\out\\260921',
        elapsed_seconds: 12,
      } as never),
    );
    expect(view.kind).toBe('envelope');
    if (view.kind !== 'envelope') throw new Error('分支错了');
    expect(view.code).toBe('already_recording');
    expect(view.message).toBe('已经有一场在录');
    // 三个 detail 键按「有哪个键显示哪个键」逐条摊平。
    expect(view.detail).toHaveLength(3);
    expect(view.detail.join('\n')).toContain('session_id：260921');
  });

  it('失败信封的 failed_steps 逐条列出来（数组里有对象那一支）', () => {
    const view = failureFrom(
      new ActionFailedError(409, 'draining', '还在收尾', {
        failed_steps: [{ id: 'post', error: '钩子抛错' }],
      } as never),
    );
    if (view.kind !== 'envelope') throw new Error('分支错了');
    expect(view.detail).toEqual(['failed_steps.post：钩子抛错']);
  });

  it('别的异常（应答形状不对这一类）归 envelope 那一档，如实显示原文', () => {
    for (const error of [
      new ActionProtocolError(200, '<html>', new Error('不是 JSON')),
      new Error('网络断了'),
      '不是 Error 的抛出物',
    ]) {
      const view = failureFrom(error);
      expect(view.kind).toBe('envelope');
      if (view.kind !== 'envelope') throw new Error('分支错了');
      expect(view.message.length).toBeGreaterThan(0);
      expect(view.detail).toEqual([]);
    }
  });
});
