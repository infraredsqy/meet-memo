import { describe, expect, it } from 'vitest';

import { applyTheme, flipTheme, storedTheme } from '../src/home/theme';

/**
 * 日间 / 夜间这一档的读、存、翻（`home/theme.ts`）。
 *
 * 存档只有两档合法取值，其余按没存过算；DOM 的写法是「夜面挂属性、
 * 日间摘属性」——缺省即日面，属性一摘样式层就翻回来。
 */

describe('日间 / 夜间的读存翻', () => {
  it('按一下翻到另一档，两下回到原档', () => {
    expect(flipTheme('light')).toBe('dark');
    expect(flipTheme(flipTheme('light'))).toBe('light');
  });

  it('存档里认得两档取值，其余按没存过算', () => {
    expect(storedTheme(() => 'dark')).toBe('dark');
    expect(storedTheme(() => 'light')).toBe('light');
    expect(storedTheme(() => null)).toBeNull();
    expect(storedTheme(() => 'midnight')).toBeNull();
    expect(storedTheme(() => '')).toBeNull();
  });

  it('夜面挂 data-theme，日间把属性摘掉', () => {
    const record: string[] = [];
    const root = {
      setAttribute: (key: string, value: string) => record.push(`set ${key}=${value}`),
      removeAttribute: (key: string) => record.push(`remove ${key}`),
    } as unknown as Element;
    applyTheme('dark', root);
    applyTheme('light', root);
    expect(record).toEqual(['set data-theme=dark', 'remove data-theme']);
  });
});
