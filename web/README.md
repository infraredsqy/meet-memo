# web/ —— meeting 界面

> 这个目录是 meeting 的界面工程：契约层（zod schema + 静态类型）、服务接口客户端（SSE / 动作 / 心跳），
> 以及一页把契约链路走通的壳页。
>
> 契约正文在 `docs/contract.md`，可机读基准在 `contract/fixtures/`（25 个 golden 样本）。
> **契约层逐字段照它们写** —— schema 改了、样本再跑起来红了，说明契约漂了。

## 前提

- **Node ≥ 20**（见 `package.json` 的 `engines`）。本机实测 node v26.9.0 / npm 11.18.0。
- 包管理器是 **npm**，`package-lock.json` 一并入库。
- 服务只监听 `127.0.0.1`、默认端口 `8765`（配置键 `web.port`；`web.port = 0` 时由系统挑，真实端口看快照的 `service.port`）。

## 命令

```powershell
cd web
npm install          # 装依赖
npm run dev          # vite 开发服务器；/api 转发到 http://127.0.0.1:${MEETING_PORT ?? 8765}
npm run typecheck    # tsc --noEmit -p tsconfig.app.json && tsc --noEmit -p tsconfig.node.json
npm run build        # npm run typecheck && vite build，产物落 web/dist
                     # （与服务配置键 web.dist_dir 的默认值对上）
npm run preview      # 预览构建产物
npm test             # vitest run（等价于 npx vitest run）
```

`build` 里的类型检查走 **`--noEmit`**（两条 `tsc -p`，各查各的 project），因此它**不产**
`*.tsbuildinfo` —— 那是 `tsc -b` 增量构建的产物，当前链路不走那条路（理由见 `vitest.config.ts`
开头那一段与根 `.gitignore` 的 `*.tsbuildinfo` 行）。

跑测试前不需要先 `npm run build` —— vitest 直接读 `tests/` 与 `src/`。

**两份 tsconfig 各管一条线，收进来的文件不重叠**（同一份文件被两条线各查一遍，会让同一个
类型错报两次，还会让两份全局类型环境打架）：

| project | 收什么 | 全局类型 | 为什么 |
|---|---|---|---|
| `tsconfig.app.json` | `src` | `vite/client` | 浏览器那一侧：DOM、`import.meta.env`、react-jsx |
| `tsconfig.node.json` | `vite.config.ts`、`vitest.config.ts`、`tests` | `node` | node 那一侧：配置文件跑在 node 上，用例要 `node:fs` / `node:path` 读盘上的 golden 样本 |

`tests/` 归 **node** 那条线（`tests/fixtures.ts` 就用 `node:fs` 与 `node:path`），
因此 `tsconfig.app.json` 的 `include` 里不再列它 —— 一次 `npm run typecheck` 只查一遍。

## 目录

| 路径 | 是什么 |
|---|---|
| `src/contract/` | 契约层：zod schema 为源，`z.infer` 出静态类型。掩码占位符（`<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`）在 parse 层照实放行 |
| `src/api/` | 服务接口：`events.ts`（SSE 四事件 + 快照重同步）、`actions.ts`（动作信封与两个必需头）、`heartbeat.ts`（可配周期轮询与三态判定） |
| `src/App.tsx` | 壳页：连接状态 / resync 计数 / `top_state` / `revision` / 心跳三态 / 两路 health 与电平 / 开始停止按钮 / 错误按 `code` 显示（403 单独一档，不折成 `code`） |
| `tests/` | vitest：`contract.test.ts` 拿 golden 样本逐份过 schema，`contract-guards.test.ts` 验 schema 真咬得住，`api.test.ts` 验动作信封与心跳三态，`app-failure.test.ts` 验失败分档 |

## 两条铁律

1. **界面不持有权威状态。** 权威在服务侧 `ServiceRuntime` 手上那一份快照；界面手上那份是推过来的副本，任何分歧以服务为准。界面也不自己造派生数据 —— 包络、`bar`、告警的 `muted` 都由服务算好。
2. **浏览器本地存储不存任何会中状态。** `localStorage` / `sessionStorage` / IndexedDB / Cookie 一概不用来存「哪一态、时长、告警、静音、转写」。心跳周期来自快照的 `settings.heartbeat_interval_seconds`，前端不自造第二份配置源。

## 范围

这一轮只到壳页：把连接、快照、动作、心跳这条链路走通给人看。
电平表盘、告警横幅、设置抽屉属于 `070b31e0` 的监控界面。
