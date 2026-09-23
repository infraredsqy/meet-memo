# web/ —— meeting 界面（会中主视图）

> 这个目录是 meeting 的界面工程：契约层（zod schema + 静态类型）、服务接口客户端（SSE / 动作 / 心跳），
> 以及**会中主视图** —— 用户开着它开会，不用盯着屏幕也知道一切正常，出事时第一眼看出是哪一路死了。
>
> 契约正文在 `docs/contract.md`，可机读基准在 `contract/fixtures/`（29 个 golden 样本）。
> **契约层逐字段照它们写** —— schema 改了、样本再跑起来红了，说明契约漂了。
> 界面这一侧的权威是 `docs/ui-design.md`（章 2 会中信息架构、章 4 状态机、章 7 类名契约、章 8 十一条硬门槛）。

## 前提

- **Node ≥ 20**（见 `package.json` 的 `engines`）。本机实测 node v26.9.0 / npm 11.18.0。
- 包管理器是 **npm**，`package-lock.json` 一并入库。
- 服务只监听 `127.0.0.1`、默认端口 `8765`（配置键 `web.port`；`web.port = 0` 时由系统挑，真实端口看快照的 `service.port`）。
- **零外链、零依赖新增**：样式只有 `src/styles/` 下两份，字体全走系统字体栈，图标用 `clip-path` 画的 `glyph`，声音用 WebAudio 就地合成。

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

**用例只跑 node 环境**（`vitest.config.ts` 里 `environment: 'node'`）。界面里带 DOM 的那一层
（组件、滚动、WebAudio）不写 jsdom 用例 —— 本轮不新增任何 npm 依赖。纯逻辑一律抽到
`src/live/` 下的函数里，由 `tests/live-*.test.ts` 覆盖；需要真 DOM 的那几条（可见性、矩形、
灰度、发声次数）走 CDP 无头浏览器，脚本与证据在 `.work/070b31e0-phase2/`（不属于产品代码）。

## 目录

| 路径 | 是什么 |
|---|---|
| `src/contract/` | 契约层：zod schema 为源，`z.infer` 出静态类型。掩码占位符（`<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`）在 parse 层照实放行；动作信封（`actions.ts`：九个 `code`、逐档 `detail`、`summarize` 那份带 `summary` 的成功体）与本场记录端点（`summaries.ts`：`GET /api/summaries` 的 `{session_id, entries}`）各一份 |
| `src/api/` | 服务接口：`events.ts`（SSE 四事件 + 快照重同步）、`actions.ts`（动作信封与两个必需头）、`summaries.ts`（本场总结记录那条 GET）、`heartbeat.ts`（可配周期轮询与三态判定 `judgeHeartbeat`） |
| `src/live/` | 会中视图的**纯逻辑**（组件里不夹业务判断） |
| `src/App.tsx` | 会中主视图的组件树：外壳、两路通道、告警区、收尾区、实时转写、总结区（四档 / 主按钮 / 进行中 / 结果与失败面板 / 本场总结记录回看） |
| `src/styles/tokens.css` | **逐字节拷贝** `docs/ui/tokens.css`（除文件头那段注释）。设计 token 的唯一定义处是 `docs/ui/tokens.css`，这一份是它进构建的那一份 |
| `src/styles/live.css` | 从 `docs/ui/ui.css` 抽出的会中视图样式（抽取口径见下） |
| `tests/` | vitest 用例（`npm test`，12 个文件 326 个用例）：契约层三份（拿 golden 样本过 schema、验 schema 咬得住、验动作信封与心跳三态）、失败分档一份（`app-failure.test.ts`）、会中视图逻辑**八份** `live-*.test.ts`（`live-format` / `live-status` / `live-alarm` / `live-heartbeat` / `live-transcript` / `live-sound` / `live-summary` / `live-summary-controller`）+ 用例自己的真值快照构造器 `live-fixtures.ts` |

### `src/live/` 里各管什么

| 文件 | 管什么 |
|---|---|
| `format.ts` | 时长（`已录 HH:MM:SS`）、墙钟（分钟级；带秒的那一个只给心跳失联）、dBFS 读数与**唯一的换算**（−60…0 → 0–100%） |
| `status.ts` | 四态胶囊（字 + 图形）、七档 `sources[*].health` → 徽标与每路健康那一行的整句 |
| `alarm.ts` | 致命横幅的三处文案、提示级细条（上限 3）、**发声判定**（`settings.alarm_sound` 与 `muted` 一起读） |
| `heartbeat.ts` | 心跳四级（正常 / 迟到 / 失联 / 已恢复）+ 失联横幅正文 + 恢复短记录 + 「采集已停」那一档 |
| `loss.ts` | 界面自己记的那一份「本场哪一路丢过」的观察（两个时刻都是服务给的，只在本页内存里） |
| `transcript.ts` | 转写行累积（约 200 行窗口）、缺口、心跳中断的断口插位、「回到最新」的判据 |
| `levels.ts` | 两路电平的取数（`levels` 帧优先、快照兜底、陈旧帧挡住）与包络逐根映射 |
| `sound.ts` | WebAudio 一声（单次短促、非循环）+ 自动播放策略的授权补齐 |
| `summary.ts` | 总结区那份纯逻辑：四档窗口与取值、可用性与禁用原因、三小段的取值与空态、五档失败的分派（按 `code` + `detail` 的键）、结果面板与记录条目的排版、一次请求的往返与**客户端那条 30 秒兜底线** |
| `summary-controller.ts` | 总结区那台**请求状态机**的宿主：这一次请求的状态与当前档位、兜底超时、迟到的应答不覆盖已画出来的东西、本场记录那一份的取数 |
| `view.ts` | 把一份快照摊成组件要摆的视图模型（**只读快照**） |

## 样式的来源与抽取口径

两份样式都从 `docs/ui/` 来，`docs/ui/**` 是只读的设计稿，不进构建：

- **`src/styles/tokens.css`** —— `docs/ui/tokens.css` 的逐字节拷贝（文件头一段注释写出处与拷贝日期，正文一个字符都没改；拷贝脚本比对过 sha256）。
- **`src/styles/live.css`** —— 由 `.work/070b31e0-phase2/tools/extract_live_css.py` 从
  `docs/ui/ui.css` 里抽出来，脚本可重跑，末尾会打一份逐条审计清单
  （`.work/070b31e0-phase2/live-css-audit.txt`）。口径三条：

  | 怎么处理 | 是什么 |
  |---|---|
  | **留** | 骨架（`.shell` / `.topbar` / `.band` / `.rail`）+ 通用控件（`.btn` / `.seg` / `.glyph` / `.readout` / `.tag` / `.kv` / `.codeblock` / `.spinner` / `.num`）+ 会中视图规则（`.page--live` / `.m-band` / `.m-ch` / `.m-grid` / `.m-meter` / `.m-env` / `.m-health` / `.m-chip` / `.alert` / `.heartbeat` / `.recovered-note` / `.statusline` / `.clock` / `.transcript` / `.sumbar` / `.meter-legend` / `.live-tools`）+ 两个宽度断点（720 / 400） |
  | **剔** | 设计稿靠 `?state=` 切档的那一批规则（选择器里带 `.is-<state>` 状态类的：显隐与就地微调）。React 版由条件渲染负责显隐，这一批进 CSS 就是死代码。剔掉的地方在审计清单里逐条列出 |
  | **改** | 四处把 `?state=` 作用域换成挂在部件自己身上的类：胶囊两档配色（`.statusline__caps--on` / `--alert`）、空闲时长的灰字（`.clock--faint`）、失联时顶栏让位（`.topbar--hb-lost`）、判据说明（`#hb-guidance` → `.hb-guidance`）；失联的冻结压灰那条作用域由 `<html>` 换到 `<body>` 的 `.is-heartbeat-lost` |

  `[data-layout="a"]` / `[data-layout="b"]` 那几条**一个字都没改**：`src/main.tsx` 在
  `<html>` 上恒挂 `data-layout="a"`（方向 A：两路左右并列、告警横幅插在采集带之外），
  所以 A 的写法照原样生效；本轮不做方向 B，B 那几条留在样式里，它们是设计稿自己的开关。

  **类名一个新造的都没有**（类名契约在 `docs/ui-design.md` 章 7.10）。文件末尾另有一块
  「本单元新增」——产品侧自己需要的那几条规则单独成块，逐条写了理由，用的是既有 token
  与既有类名。

- **数据进 style 变量的分层照搬设计稿**：`--level`（当前电平）/ `--hold`（峰值保持）/
  `--h`（包络一根柱）由组件写到元素上，高度怎么画在 CSS 里。**没有一处内联宽高**，也没有
  一处「让电平看起来在动」的动画。

## 会中视图的结构

一屏从上到下（1440×900，方向 A）：

1. **顶栏** —— 状态胶囊（字 + 图形）+ 状态说明 / 时长 + 主按钮（开始或结束，同一位置只出现一个）/ 告警出声开关
2. **采集带** —— 两路通道左右并列（路名 + 设备名 + 健康徽标 + dBFS 刻度 + 电平表 + 峰值保持 + 15 根包络 + 读数），右侧三分之一是总结区（四档窗口 + `总结一下` + 原因行或进行中那一句 + 本场总结记录入口）
3. **每路健康** —— 两条文字保证（`本地 · 还在采` / `远端 · 采不到了，从 20:57 起`），任何宽度都不折叠
4. **总结结果面板 / 失败面板** —— 三小段（`刚才在讲什么` / `讲到哪儿了` / `丢掉了什么`）或那一档失败的原因，右上角 `关闭`，**关掉即消失、不落文件**；位置就在这一格（每路健康之后、告警带之前）。本场总结记录展开之后也落在这里
5. **告警区** —— 致命级整行横幅（哪一路 + 从何时起 + 已丢多久 + 三个操作）与提示级细条；**心跳失联时整块收掉**，「采集已停」那一档也不与致命横幅同屏
6. **收尾区** —— 已结束时的那条横幅（`正在落盘，先别关机` / `可以关机` / `收尾出事了`）与四项明细
7. **实时转写** —— 临时稿、`[HH:MM] 本地/远端 · 正文`、识别失败缺口、心跳中断断口、约 200 行窗口、不自动跳回底部 +「回到最新」

**不在这一轮的范围里**（契约章 7 的保留登记：不探测、不留死代码分支）：设置抽屉、转写历史回看、设备重探的界面。

## 两条铁律

1. **界面不持有权威状态。** 权威在服务侧 `ServiceRuntime` 手上那一份快照；界面手上那份是推过来的副本，任何分歧以服务为准。界面也不自己造派生数据 —— 包络、`bar`、告警的 `muted` 都由服务算好。界面自己只记三样**观察**：心跳四级、两路丢过的窗口（服务一恢复就把 `lost_since` 清掉，那个窗口只在还看得见它的时候记得下来）、转写窗口；三样都只活在内存里。
2. **浏览器本地存储不存任何会中状态。** `localStorage` / `sessionStorage` / IndexedDB / Cookie 一概不用来存「哪一态、时长、告警、静音、转写」。心跳周期来自快照的 `settings.heartbeat_interval_seconds`，前端不自造第二份配置源。
