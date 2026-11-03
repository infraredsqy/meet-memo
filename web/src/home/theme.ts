/**
 * 日间 / 夜间这一档纯偏好的读、存、翻。
 *
 * 主题只管配色：写盘只在 localStorage 的 `meeting.theme` 一格（键名进
 * `THEME_STORAGE_KEY`），快照、电平、心跳那些会中读数各存各的，两不相扰。
 * 读与写都做成可注入或缺席可过的形状——node 侧的挂载用例里 `localStorage`
 * 与真 DOM 都不在场，纯函数照常可测。
 */

export type ThemeChoice = 'light' | 'dark';

/** localStorage 里的键名。 */
export const THEME_STORAGE_KEY = 'meeting.theme';

/** 按一下翻到另一档。 */
export function flipTheme(theme: ThemeChoice): ThemeChoice {
  return theme === 'light' ? 'dark' : 'light';
}

/**
 * 从存档读上一次的选择；没存过或存了别的值交回 `null`，调用方按日间起。
 * `read` 注入（浏览器里就是 `localStorage.getItem` 包一层 try）。
 */
export function storedTheme(read: (key: string) => string | null): ThemeChoice | null {
  const value = read(THEME_STORAGE_KEY);
  return value === 'light' || value === 'dark' ? value : null;
}

/**
 * 把选择写到根节点上：夜面挂 `data-theme="dark"`，日间不挂属性
 * （样式层 `:root` 的缺省就是日面，属性一摘就翻回来）。
 */
export function applyTheme(theme: ThemeChoice, root: Element): void {
  if (theme === 'dark') {
    root.setAttribute('data-theme', 'dark');
  } else {
    root.removeAttribute('data-theme');
  }
}
