import { useState, type ReactNode } from 'react';
import { applyTheme, flipTheme, storedTheme, THEME_STORAGE_KEY, type ThemeChoice } from './theme';

/** 浏览器存档的读法：隐私模式等存不进的场合按没存过算。 */
function readStored(): string | null {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** 挂载那一刻的选择：存档说了算，没存过按日间。node 侧没有真 DOM，照日间算。 */
function initialTheme(): ThemeChoice {
  const root = typeof document !== 'undefined' ? document.documentElement : null;
  if (!root) return 'light';
  return storedTheme(readStored) ?? (root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');
}

/**
 * 左栏铭牌旁那一颗日夜间切换。按钮上的字指向**要切去的那一档**
 * （日间下写「夜间」，夜间下写「日间」），按下即翻并写进存档。
 */
export function ThemeToggle(): ReactNode {
  const [theme, setTheme] = useState<ThemeChoice>(initialTheme);
  const toggle = (): void => {
    const next = flipTheme(theme);
    applyTheme(next, document.documentElement);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // 存不进就只活这一页，翻面照常
    }
    setTheme(next);
  };
  const target = theme === 'light' ? '夜间' : '日间';
  return (
    <button
      className="btn btn--small btn--ghost"
      type="button"
      aria-label={`切换到${target}配色`}
      onClick={toggle}
    >
      {target}
    </button>
  );
}
