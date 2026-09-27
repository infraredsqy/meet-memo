import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/console.css';
import './styles/live.css';

/**
 * 页面根节点上挂三样东西，都归 `index.html` 管不到的那一块：
 *
 * - `data-layout="a"`：`live.css` 里 `[data-layout="a"]` / `[data-layout="b"]` 那几条
 *   是设计稿的方向开关（`docs/ui-design.md` 章 7.8）。这一版做的是**方向 A**
 *   （两路左右并列、告警横幅插在采集带之外），所以恒挂 `"a"`；`"b"` 那几条留在
 *   样式里不动，本轮不做方向 B。
 * - `class="ui page--live"`：`.ui` 是页面字体与盒模型的底座，`.page--live` 是
 *   会中视图的修饰（`ui.css` 里那两条 `.page--live .statusline__caps` 就挂它）。
 * - `data-theme`：暗面 / 亮面这一档。**默认暗面**（`console.css` 的 `:root` 就是暗的），
 *   地址栏加 `?theme=light` 翻到亮面那一套，`?theme=dark` 显式钉回暗面。
 *   这一档只决定配色，与任何会中状态无关，所以照铁律 2 不落本地存储 ——
 *   刷新之后回到默认的暗面。
 */
const html = document.documentElement;
html.dataset.layout = 'a';
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
