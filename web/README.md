# web/ —— meeting 界面工程

> meeting 的前端：契约层、服务接口客户端（SSE / 动作 / 心跳）、工作区首页与会中主视图。
> 上游指针：契约正文 `docs/contract.md`（可机读样本与掩码规则见 `contract/README.md`）；
> 界面口径的正本（信息架构、交互清单、多开 / 关闭 / 刷新行为、设计 token）在 `docs/ui-design.md`；
> 设计稿本体在 `docs/ui/`（不进构建、不被服务引用）。

## 前提

- **Node ≥ 20**（`package.json` 的 `engines`）；包管理器是 **npm**，`package-lock.json` 一并入库。服务监听 `127.0.0.1`、默认端口 `8765`（配置键 `web.port`，`0` 时由系统挑，真实端口看快照）。
- **零外链、零依赖新增**：字体全走系统字体栈，图标用 `clip-path` 的 `glyph`，声音用 WebAudio 就地合成。

## 命令

```powershell
cd web
npm install        # 装依赖
npm run dev        # vite 开发服务器；/api 转发到 http://127.0.0.1:${MEETING_PORT ?? 8765}
npm run typecheck  # 两条 tsc --noEmit（app / node 各查各的 project，不产 tsbuildinfo）
npm run build      # typecheck + vite build，产物落 web/dist（与服务配置键 web.dist_dir 默认值对上）
npm test           # vitest run
```

两份 tsconfig 各管一条线、收进来的文件不重叠：`tsconfig.app.json` 收 `src`（全局类型 `vite/client`）；
`tsconfig.node.json` 收 `vite.config.ts` / `vitest.config.ts` / `tests`（全局类型 `node`，`lib` 另给
`DOM`——挂载用例顺着 import 走进组件树，运行时仍是 node）。

## 测试怎么写

- **只跑 node 环境**（`vitest.config.ts` 的 `environment: 'node'`），仓库里没有 jsdom。纯逻辑一律抽到
  `src/live/` 与 `src/home/` 下的函数；组件那一侧只留**挂载断言**（`react-test-renderer`，只看渲染出来的类名与字）。需要真 DOM / 真浏览器的验证（可见性、矩形、灰度、发声次数）不进 `tests/`。
- 契约层用例拿 `contract/fixtures/` 的 golden 样本过 schema——schema 改了、样本跑红了，说明契约漂了。

## 目录

| 路径 | 是什么 |
|---|---|
| `src/contract/` | 契约层：zod schema 为源，`z.infer` 出静态类型（动作信封、清单 / 单场状态 / 目录 / 设备 / 资料各端点的应答体） |
| `src/api/` | 服务接口：SSE 四事件与快照重同步、动作信封、心跳轮询与三态判定、各端点取数（写端点带 `X-Meeting-Service: 1` 头哨） |
| `src/home/` | 工作区首页：闸分流（`gate.ts`）、清单与详情、目录选择器弹窗、会后两按钮、会后进度区、工序链与稿子正文、说话人名字弹窗；纯逻辑抽在 `gate.ts` / `post.ts` / `progress.ts` / `model.ts` / `names.ts` |
| `src/live/` | 会中视图的纯逻辑，每文件一个主题：格式化、四态、告警、四组合直切映射（`combo.ts`）、心跳四级、转写累积、电平取数、发声、总结请求状态机、设置抽屉判读等 |
| `src/materials/` | 会议资料区：`MaterialsPanel.tsx` 与纯逻辑 `model.ts`（大小口径、粘贴筛子、失败分档） |
| `src/App.tsx` | 会中主视图的组件树；收音由顶栏的四组合直切集中控制 |
| `src/styles/`、`tests/` | 三份样式（见下）与 vitest 用例（契约、纯逻辑、挂载断言、golden 样本对齐） |

## 样式

`src/main.tsx` 按 `tokens.css` → `console.css` → `live.css` 的顺序引入：

- **`tokens.css`** —— `docs/ui/tokens.css` 的逐字节拷贝（出处与拷贝日期见文件头注释）。那份文件是设计
  token 的唯一定义处，改它等于改设计稿；本文件正文一个字符都不改。
- **`console.css`** —— 暗面 token 层 + 主题开关：`:root` 给每个具名色一份暗面取值（`--t-*`），
  `[data-theme="light"]` 给亮面覆盖。默认暗面；`?theme=light` 翻亮面、`?theme=dark` 钉回（不落本地存储）。
- **`live.css`** —— 会中视图样式，类名照旧（与 `docs/ui/ui.css` 同一套）；取色与字体全部 `var()` 自前两份。
  数据进 style 变量的分层口径见 `docs/ui-design.md`：`--level` / `--hold` / `--h` 由组件写到元素上，高度怎么画在 `live.css`；没有一处内联宽高，也没有一处"让电平看起来在动"的动画。

## 行为口径

界面不持有权威状态（权威在服务侧快照，界面那份是推过来的副本，只记心跳、丢失窗口、转写窗口三样内存观察）；
浏览器本地存储不存任何会中状态；心跳周期来自快照的 `settings.heartbeat_interval_seconds`。完整口径的正本在 `docs/ui-design.md`。
