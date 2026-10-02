# web/ —— meeting 界面（工作台首页 + 会中主视图）

> 这个目录是 meeting 的界面工程：契约层（zod schema + 静态类型）、服务接口客户端（SSE / 动作 / 心跳）、
> **工作区首页**（空闲态与收尾落定后的那一屏：清单、目录选择器、会后两个按钮）与**会中主视图** ——
> 用户开着它开会，不用盯着屏幕也知道一切正常，出事时第一眼看出是哪一路死了。
>
> 契约正文在 `docs/contract.md`，可机读基准在 `contract/fixtures/`（34 个 golden 样本）。
> **契约层逐字段照它们写** —— schema 改了、样本再跑起来红了，说明契约漂了。
> 界面这一侧的权威是 `docs/ui-design.md`（章 2 会中信息架构、章 4 状态机、章 7 类名契约、章 8 十一条硬门槛）。

## 前提

- **Node ≥ 20**（见 `package.json` 的 `engines`）。本机实测 node v26.9.0 / npm 11.18.0。
- 包管理器是 **npm**，`package-lock.json` 一并入库。
- 服务只监听 `127.0.0.1`、默认端口 `8765`（配置键 `web.port`；`web.port = 0` 时由系统挑，真实端口看快照的 `service.port`）。
- **零外链、零依赖新增**：样式只有 `src/styles/` 下三份（`tokens.css` / `console.css` / `live.css`），字体全走系统字体栈，图标用 `clip-path` 画的 `glyph`，声音用 WebAudio 就地合成。

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
| `tsconfig.node.json` | `vite.config.ts`、`vitest.config.ts`、`tests` | `node`，`lib` 里另有 `DOM` | node 那一侧：配置文件跑在 node 上，用例要 `node:fs` / `node:path` 读盘上的 golden 样本。`lib` 那句 `DOM` 是 `10770b41` 补的：挂载用例顺着 import 会走进 `src/App.tsx`，那一份用 `window` / `document` / `HTMLDivElement`；运行时仍是 node（`vitest.config.ts` 的 `environment`），DOM 只是这一档的类型面 |

`tests/` 归 **node** 那条线（`tests/fixtures.ts` 就用 `node:fs` 与 `node:path`），
因此 `tsconfig.app.json` 的 `include` 里不再列它 —— 一次 `npm run typecheck` 只查一遍。

**用例只跑 node 环境**（`vitest.config.ts` 里 `environment: 'node'`）。界面里带 DOM 的那一层
（组件、滚动、WebAudio）**不进 jsdom** —— 仓库里没有 jsdom 这个依赖，本轮也没新增任何 npm 依赖。
纯逻辑一律抽到 `src/live/` 下的函数里，由 `tests/live-*.test.ts` 覆盖；组件那一侧只留两条**挂载断言**，
走仓库里已有的 `react-test-renderer`，只看渲染出来的类名与字：`settings-drawer.test.tsx`（抽屉四项就位，
`311f84f5`）与 `live-pause-screen.test.tsx`（暂停那条横带、顶栏那颗的字与 `aria-pressed`，`10770b41`）。
需要真 DOM 的那几条（可见性、矩形、
灰度、发声次数）当时用 CDP 无头浏览器量过一遍 —— 那些一次性脚本与证据不属于产品代码，
不随仓库保留。

## 目录

| 路径 | 是什么 |
|---|---|
| `src/contract/` | 契约层：zod schema 为源，`z.infer` 出静态类型。掩码占位符（`<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`）在 parse 层照实放行；动作信封（`actions.ts`：九个 `code`、逐档 `detail`、`summarize` 那份带 `summary` 的成功体、收编两动作那份带 `workspace` 的成功体、会后两按钮那份带 `post` 的成功体）、本场记录端点（`summaries.ts`：`GET /api/summaries` 的 `{session_id, entries}`）、工作区清单 / 目录现场（`sessions.ts`：`GET /api/sessions` 的 `{sessions, recent}` 逐行十一键、`GET /api/dirs` 的一层现场）与设备清单（`devices.ts`：`GET /api/devices` 的 `{mic, loopback}` 每项六格，`311f84f5`）各一份 |
| `src/api/` | 服务接口：`events.ts`（SSE 四事件 + 快照重同步）、`actions.ts`（动作信封与两个必需头）、`summaries.ts`（本场摘要记录那条 GET）、`sessions.ts`（工作区清单与目录现场两只 GET）、`devices.ts`（设备清单那只 GET）、`heartbeat.ts`（可配周期轮询与三态判定 `judgeHeartbeat`） |
| `src/home/` | **工作区首页**（e2f6166b；`05a48b6a` 补两会后按钮）：`HomeGate` 按闸分流（判据在 `gate.ts`）——`idle` 且没有场次、或 `ended` 且收尾四步都落定（`post` 停 `waiting` 算落定）→ 开 `WorkspaceHome`，其余给会中主视图（收尾落定那一刻页面自己回到工作台）；`WorkspaceHome` 左侧清单（`GET /api/sessions`，按 `recent` 定选中行）+ 空态（logo + 开场指引）+ 目录选择器弹窗（`GET /api/dirs`，起点 `~/`、非空父目录「进这一层」）+ 非空收编与删除确认弹窗 + Windows 大小写并一行 + **选中那一场详情里的会后两个按钮**（`post_offline` / `post_minutes`，那一格按 `has_offline` 渲染）；纯逻辑抽在 `gate.ts`、`post.ts`（两个按钮的可用性 / 禁用原因 / 那一格文案）、`model.ts`（选中判据 `startWhy` / 空态文案 / 初始选择）与 `confirmWorkspaceOpen.ts`（`workspace_open` 的非空确认二段式） |
| `src/live/` | 会中视图的**纯逻辑**（组件里不夹业务判断） |
| `src/App.tsx` | 会中主视图的组件树：外壳、两路通道、告警区、收尾区、实时转写、总结区（四档 / 主按钮 / 进行中 / 结果与失败面板 / 本场摘要记录回看）。三处与暂停有关的落点（`10770b41`）：顶栏那颗「暂停 / 继续」、告警区最前面那条「本场已暂停」整行横带（与致命横幅同级）、通道头那颗逐路开关 |
| `src/styles/tokens.css` | **逐字节拷贝** `docs/ui/tokens.css`（除文件头那段注释）。设计 token 的唯一定义处是 `docs/ui/tokens.css`，这一份是它进构建的那一份 |
| `src/styles/console.css` | **暗面 token 层**，也是主题开关所在：`:root` 给每个具名色一份暗面取值（`--t-*`），`[data-theme="light"]` 再给一套亮面覆盖。暗面的值单独写在这里，`tokens.css` 一个字符都不动 —— 那份文件自称设计 token 的唯一定义处，改它等于改设计稿 |
| `src/styles/live.css` | 会中视图样式（**暗面一稿**，来路见下） |
| `tests/` | vitest 用例（`npm test`，18 个文件 408 个用例）：契约层三份（拿 golden 样本过 schema、验 schema 咬得住、验动作信封与心跳三态）、失败分档一份（`app-failure.test.ts`）、**工作区首页两份**（`home.test.ts` 纯逻辑与端点取数，含会后两个按钮的可用性与那一格文案、`gate.ts` 的分流判据；`home-mount.test.tsx` 首页挂载与分流）、**设置抽屉两份**（`live-settings.test.ts` 纯逻辑：抽屉行清单（ASR 五参数一个都不在）、心跳 1–60 判读、设备格会中闸、下拉摆法与缺省档归一；`settings-drawer.test.tsx` 挂载：四项都在、ASR 五参数一个都不出现、设备选完 `settings_update`+`reprobe`、越界当场拒，`311f84f5`）、会中视图逻辑**十份**（`live-*.test.ts` / `live-*.test.tsx`：`live-format` / `live-status` / `live-alarm` / `live-heartbeat` / `live-transcript` / `live-sound` / `live-summary` / `live-summary-controller` / `live-pause` / `live-pause-screen`，末尾两份是「本场已暂停」那条横带、暂停按钮字与逐路开关）+ 用例自己的真值快照构造器 `live-fixtures.ts` |

### `src/live/` 里各管什么

| 文件 | 管什么 |
|---|---|
| `format.ts` | 时长（`已录 HH:MM:SS`）、墙钟（分钟级；带秒的那一个只给心跳失联）、dBFS 读数与**唯一的换算**（−60…0 → 0–100%） |
| `status.ts` | 四态胶囊（字 + 图形）、七档 `sources[*].health` → 徽标与各通道状态那一行的整句 |
| `alarm.ts` | 致命横幅的三处文案、提示级细条（上限 3）、**发声判定**（`settings.alarm_sound` 与 `muted` 一起读） |
| `pause.ts` | 暂停与「会中开关某一路」两处入口的取数与文案（`10770b41`，照 `alarm.ts` 的形状写：纯函数、逐档可验）：那颗按钮的字（`暂停` / `继续`）、告警区顶上那条「本场已暂停」横幅的两句话（只在 `recording` / `alert` 两态给）、通道头那颗开关的字与方向（按那一路的 `health` 读，`stopped` 就是已关掉）。状态一律从快照读 |
| `heartbeat.ts` | 心跳四级（正常 / 迟到 / 失联 / 已恢复）+ 失联横幅正文 + 恢复短记录 + 「采集已停」那一档 |
| `loss.ts` | 界面自己记的那一份「本场哪一路丢过」的观察（两个时刻都是服务给的，只在本页内存里） |
| `transcript.ts` | 转写行累积（约 200 行窗口）、缺口、心跳中断的断口插位、「回到最新」的判据 |
| `levels.ts` | 两路电平的取数（`levels` 帧优先、快照兜底、陈旧帧挡住）与包络逐根映射 |
| `sound.ts` | WebAudio 一声（单次短促、非循环）+ 自动播放策略的授权补齐 |
| `summary.ts` | 总结区那份纯逻辑：四档窗口与取值、可用性与禁用原因、三小段的取值与空态、五档失败的分派（按 `code` + `detail` 的键）、结果面板与记录条目的排版、一次请求的往返与**客户端那条 30 秒兜底线** |
| `summary-controller.ts` | 总结区那台**请求状态机**的宿主：这一次请求的状态与当前档位、兜底超时、迟到的应答不覆盖已画出来的东西、本场记录那一份的取数、**缺省档随快照走**（`applyDefaultWindow`，用户显式挑过的档不被覆盖） |
| `settings.ts` | 设置抽屉那份纯逻辑（`311f84f5`）：抽屉的全部行清单（**ASR 五参数一个都不在**）、心跳 1–60 的判读与那句拒绝文案、设备格的会中闸、设备下拉的摆法（按默认策略挑恒在第一）、缺省档归一，以及 change 事件的两条**结构式读法**（`stringValueOf` / `checkedOf` —— 组件会被 node 线 tsconfig 顺着用例 import，那一侧没有 lib.dom） |
| `view.ts` | 把一份快照摊成组件要摆的视图模型（**只读快照**） |

## 样式的来源

三份样式各有来路，`src/main.tsx` 按 `tokens.css` → `console.css` → `live.css` 的顺序引入。
`docs/ui/**`（设计稿本体）留在仓库里供人翻看，不进构建、不被服务引用。

- **`src/styles/tokens.css`** —— `docs/ui/tokens.css` 的逐字节拷贝（文件头一段注释写出处与拷贝日期，正文一个字符都没改）。
  间距、字号、时长、尺寸，以及亮面那六个具名色，都住在这里。它是设计 token 的唯一定义处，
  `docs/ui-design.md` 第 7 章的名单与它逐字对齐。
- **`src/styles/console.css`** —— **暗面 token 层**，主题开关也在这儿：`:root` 给每个具名色一份暗面取值（`--t-*`），
  `[data-theme="light"]` 再给一套亮面覆盖。默认暗面；地址栏加 `?theme=light` 翻到亮面，
  `?theme=dark` 显式钉回暗面（这一档只管配色，不落本地存储，刷新回默认）。
  暗面的值单独写在这一份里，`tokens.css` 一个字符都不动 —— 那份文件自称设计 token 的唯一定义处、
  还带着拷贝来源与日期，改它等于改设计稿，两个来源从此对不上。
- **`src/styles/live.css`** —— 会中视图样式，**暗面一稿**。它的来路是 `src/App.tsx` 的组件树：
  照着这棵树重画，**类名照旧**（类名契约在 `docs/ui-design.md` 章 7.10），换掉的是配色、字体、
  层级与排版。取色与字体全部 `var()` 自 `console.css` 与 `tokens.css`；正文从底座第 1 节排到
  宽度断点第 14 节，每一节顶上一条横线标题。
  早先那一版是从 `docs/ui/ui.css` 抽出来的抽取物，已被这一稿整份取代 —— 抽取物与抽取脚本都不在仓库里了。

- **数据进 style 变量的分层照设计章 7.7 的口径**：`--level`（当前电平）/ `--hold`（峰值保持）/
  `--h`（包络一根柱）由组件写到元素上，高度怎么画在 `live.css` 里（`var(--level)` / `var(--hold)` /
  `var(--h)` 三处）。**没有一处内联宽高**，也没有一处「让电平看起来在动」的动画。

## 会中视图的结构

一屏从上到下（1440×900，方向 A）：

1. **顶栏** —— 状态胶囊（字 + 图形）+ 状态说明 / 时长 + 主按钮（开始或停止，同一位置只出现一个）+ **暂停 / 继续**那颗（`btn--pause`，录音中与告警态才摆，压在停止左边；暂停中它是实心的、`aria-pressed` 为真）/ 告警提示音开关
2. **音频采集带** —— 两路通道左右并列（路名 + 设备名 + 健康徽标 + **逐路开关**那颗小按钮（字是 `关掉` / `打开`，录音中与告警态才摆；关掉的那一路健康徽标写「采集已停止」）+ dBFS 刻度 + 电平表 + 峰值保持 + 15 根包络 + 读数），右侧三分之一是摘要区（四档时间范围 + `快速总结` + 原因行或进行中那一句 + 本场摘要记录入口）
3. **各通道状态** —— 两条文字保证（`本地 · 采样中` / `远端 · 信号丢失，从 20:57 起`），任何宽度都不折叠
4. **摘要结果面板 / 失败面板** —— 三小段（`讲了什么` / `讲到哪里` / `丢掉了什么`）或那一档失败的原因，右上角 `关闭`，**关掉即消失、不落文件**；位置就在这一格（各通道状态之后、告警带之前）。本场摘要记录展开之后也落在这里
5. **告警区** —— **「本场已暂停」整行横带**（`alert--paused`，与致命横幅同一副骨架，排在最前面，带 `继续` 与 `停止录音` 两颗；暂停中才出现）+ 致命级整行横幅（哪一路 + 从何时起 + 已丢多久 + 三个操作）与提示级细条；**心跳失联时整块收掉**，「采集已停」那一档也不与致命横幅同屏
6. **收尾区** —— 已结束时的那条横幅（`正在落盘，先别关机` / `可以关机` / `收尾出事了`）与四项明细
7. **实时转写** —— 草稿、`[HH:MM] 本地/远端 · 正文`、识别失败缺口、心跳中断断口、约 200 行窗口、不自动跳回底部 +「回到最新」

**设置抽屉已落地**（`311f84f5`）：会中顶栏右上角的「设置」展开 `drawer` / `scrim`（类名契约
章 7.10 的预留），四项设置就地写进 config —— 换设备（`GET /api/devices` 的清单 + 选完
`reprobe`；会中那两格禁用）、告警提示音、心跳频率（1–60 秒，越界当场拒）、快速总结默认
窗口（四档预选随 `settings.summary_default_window` 走）。**ASR 那五个参数一个都不进这扇窗**
（设计意图第 14 条；`web/tests/settings-drawer.test.tsx` 拿渲染结果逐个查）。仍在保留登记里
的：转写历史回看。

## 两条铁律

1. **界面不持有权威状态。** 权威在服务侧 `ServiceRuntime` 手上那一份快照；界面手上那份是推过来的副本，任何分歧以服务为准。界面也不自己造派生数据 —— 包络、`bar`、告警的 `muted` 都由服务算好。界面自己只记三样**观察**：心跳四级、两路丢过的窗口（服务一恢复就把 `lost_since` 清掉，那个窗口只在还看得见它的时候记得下来）、转写窗口；三样都只活在内存里。
2. **浏览器本地存储不存任何会中状态。** `localStorage` / `sessionStorage` / IndexedDB / Cookie 一概不用来存「哪一态、时长、告警、静音、转写」。心跳周期来自快照的 `settings.heartbeat_interval_seconds`，前端不自造第二份配置源。
