/** 日间 / 夜间这一档纯偏好的读、存、翻。 */

export type ThemeChoice = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'meeting.theme';

export function flipTheme(theme: ThemeChoice): ThemeChoice {
  return theme === 'light' ? 'dark' : 'light';
}

export function storedTheme(read: (key: string) => string | null): ThemeChoice | null {
  const value = read(THEME_STORAGE_KEY);
  return value === 'light' || value === 'dark' ? value : null;
}

/** 日间不挂属性：样式层 `:root` 的缺省就是日面，属性一摘就翻回来。 */
export function applyTheme(theme: ThemeChoice, root: Element): void {
  if (theme === 'dark') {
    root.setAttribute('data-theme', 'dark');
  } else {
    root.removeAttribute('data-theme');
  }
}
