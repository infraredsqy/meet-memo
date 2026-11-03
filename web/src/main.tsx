import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { storedTheme, applyTheme } from './home/theme';
import './styles/tokens.css';
import './styles/console.css';
import './styles/live.css';

/**
 * 页面根节点上挂两样东西，都归 `index.html` 管不到的那一块：
 *
 * - `class="ui page--live"`：`.ui` 是页面字体与盒模型的底座，`.page--live` 是
 *   会中视图的修饰（`live.css` 里 `.page--live` 那一族规则就挂它）。
 * - `data-theme`：日间 / 夜间这一档。**日间是默认**（`console.css` 的 `:root`
 *   就是日面），左栏铭牌旁那颗「夜间 / 日间」随时翻；存档在 localStorage 的
 *   `meeting.theme` 一格（键名归 `home/theme.ts`），翻过之后刷新照旧。
 *   主题只管配色，与快照那些会中读数各存各的。
 *
 * 版面是**方向 A**（两路左右并列、告警横幅插在采集带之外，见 `docs/ui-design.md` 章 7.8）：
 * 设计稿当初在 `<html>` 上挂 `data-layout="a"`、靠一对方向开关切两套版面；后来布局
 * 直接写成了 A，那对开关随之取消，这个属性也就不再挂、样式里也不再有它的位置。
 */
const html = document.documentElement;
html.classList.add('ui', 'page--live');
const stored = storedTheme((key) => {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
});
if (stored !== null) applyTheme(stored, html);

const container = document.getElementById('root');
if (container === null) {
  throw new Error('会中视图缺少 #root 挂载点');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
