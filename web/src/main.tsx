import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/console.css';
import './styles/live.css';

/**
 * 页面根节点上挂两样东西，都归 `index.html` 管不到的那一块：
 *
 * - `class="ui page--live"`：`.ui` 是页面字体与盒模型的底座，`.page--live` 是
 *   会中视图的修饰（`live.css` 里 `.page--live` 那一族规则就挂它）。
 * - `data-theme`：暗面 / 亮面这一档。**默认暗面**（`console.css` 的 `:root` 就是暗的），
 *   地址栏加 `?theme=light` 翻到亮面那一套，`?theme=dark` 显式钉回暗面。
 *   这一档只决定配色，与任何会中状态无关，所以照铁律 2 不落本地存储 ——
 *   刷新之后回到默认的暗面。
 *
 * 版面是**方向 A**（两路左右并列、告警横幅插在采集带之外，见 `docs/ui-design.md` 章 7.8）：
 * 设计稿当初在 `<html>` 上挂 `data-layout="a"`、靠一对方向开关切两套版面；暗面一稿把
 * 布局直接写成了 A，那对开关随之取消，这个属性也就不再挂、样式里也不再有它的位置。
 */
const html = document.documentElement;
html.classList.add('ui', 'page--live');
const wanted = new URLSearchParams(window.location.search).get('theme');
if (wanted === 'light' || wanted === 'dark') html.dataset.theme = wanted;

const container = document.getElementById('root');
if (container === null) {
  throw new Error('会中视图缺少 #root 挂载点');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
