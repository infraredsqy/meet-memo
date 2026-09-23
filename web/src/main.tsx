import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/live.css';

/**
 * 页面根节点上挂两样东西，都归 `index.html` 管不到的那一块：
 *
 * - `data-layout="a"`：`live.css` 里 `[data-layout="a"]` / `[data-layout="b"]` 那几条
 *   是设计稿的方向开关（`docs/ui-design.md` 章 7.8）。这一版做的是**方向 A**
 *   （两路左右并列、告警横幅插在采集带之外），所以恒挂 `"a"`；`"b"` 那几条留在
 *   样式里不动，本轮不做方向 B。
 * - `class="ui page--live"`：`.ui` 是页面字体与盒模型的底座，`.page--live` 是
 *   会中视图的修饰（`ui.css` 里那两条 `.page--live .statusline__caps` 就挂它）。
 */
const html = document.documentElement;
html.dataset.layout = 'a';
html.classList.add('ui', 'page--live');

const container = document.getElementById('root');
if (container === null) {
  throw new Error('会中视图缺少 #root 挂载点');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
