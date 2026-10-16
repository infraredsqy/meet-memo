# web/ —— meeting 界面（工作台首页 + 会中主视图）

> 这个目录是 meeting 的界面工程：契约层（zod schema + 静态类型）、服务接口客户端（SSE / 动作 / 心跳）、
> **工作区首页**（空闲态与收尾落定后的那一屏：清单、目录选择器、会后两个按钮）与**会中主视图** ——
> 用户开着它开会，不用盯着屏幕也知道一切正常，出事时第一眼看出是哪一路死了。
>
> 契约正文在 `docs/contract.md`，可机读基准在 `contract/fixtures/`（样本清单与掩码规则见 `contract/README.md`）。
> **契约层逐字段照它们写** —— schema 改了、样本再跑起来红了，说明契约漂了。
> 界面这一侧的权威是 `docs/ui-design.md`。

## 前提

- **Node ≥ 20**（见 `package.json` 的 `engines`）。
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
| `tsconfig.node.json` | `vite.config.ts`、`vitest.config.ts`、`tests` | `node`，`lib` 里另有 `DOM` | node 那一侧：配置文件跑在 node 上，用例要 `node:fs` / `node:path` 读盘上的 golden 样本。`lib` 里另给 `DOM`：挂载用例顺着 import 会走进 `src/App.tsx`，那一份用 `window` / `document` / `HTMLDivElement`；运行时仍是 node（`vitest.config.ts` 的 `environment`），DOM 只是这一档的类型面 |

`tests/` 归 **node** 那条线（`tests/fixtures.ts` 就用 `node:fs` 与 `node:path`），
因此 `tsconfig.app.json` 的 `include` 里不再列它 —— 一次 `npm run typecheck` 只查一遍。

**用例只跑 node 环境**（`vitest.config.ts` 里 `environment: 'node'`）。界面里带 DOM 的那一层
（组件、滚动、WebAudio）**不进 jsdom** —— 仓库里没有 jsdom 这个依赖。
纯逻辑一律抽到 `src/live/` 与 `src/home/` 下的函数里，由 `tests/live-*.test.ts` 与
`tests/home*.test.ts` / `tests/progress.test.ts` / `tests/names.test.ts` 覆盖；组件那一侧只留**挂载断言**，
走仓库里已有的 `react-test-renderer`，只看渲染出来的类名与字：`settings-drawer.test.tsx`（抽屉各项就位）、
`live-pause-screen.test.tsx`（暂停那条横带与它的字、`aria-pressed`）、
`home-mount.test.tsx`（工作台首页挂载与分流）、`home-progress.test.tsx`（会后进度区的换值渲染与行四态）。
需要真 DOM / 真浏览器的那几类验证（可见性、矩形、灰度、发声次数）不进 `tests/`。

## 目录

| 路径 | 是什么 |
|---|---|
| `src/contract/` | 契约层：zod schema 为源，`z.infer` 出静态类型。掩码占位符在 parse 层照实放行（清单见 `contract/README.md`）；动作信封（`actions.ts`：九个 `code`、逐档 `detail`、`summarize` 那份带 `summary` 的成功体、收编两动作那份带 `workspace` 的成功体、会后两按钮与停掉那一趟（`post_stop`）那份带 `post` 的成功体）、本场记录端点（`summaries.ts`：`GET /api/summaries` 的 `{session_id, entries}`）、工作区清单 / 目录现场与新建 / 单场状态（`sessions.ts`：`GET /api/sessions` 的 `{sessions, recent, post_job}` 逐行十二键（行里的 `segments` 是这场会各段的时长与时间）、`GET /api/dirs` 的一层现场（`up` 到盘根才 `null`、`here` 是正在浏览这一层）与 `POST /api/dirs` 的成功体 `directory`、`GET /api/sessions/{id}` 的应答体（`offline.state` 的枚举含被打断的 `interrupted`））与设备清单（`devices.ts`：`GET /api/devices` 的 `{mic, loopback}` 每项六格）、会议资料四条（`materials.ts`：清单三格 / 上传成功体 `material` 三格 / 删除成功体 `material` 一格 / 资料那片自己的失败信封——`material_missing` 不在动作那九个 `code` 里）各一份 |
| `src/api/` | 服务接口：`events.ts`（SSE 四事件 + 快照重同步）、`actions.ts`（动作信封与两个必需头）、`summaries.ts`（本场摘要记录那条 GET）、`sessions.ts`（工作区清单与目录现场两只 GET + 单场状态那条 GET 与它的自排轮询器 + 新建目录那只 POST——写端点头哨 `X-Meeting-Service: 1`，失败落 `DirCreateHttpError`）、`devices.ts`（设备清单那只 GET）、`heartbeat.ts`（可配周期轮询与三态判定 `judgeHeartbeat`）、`materials.ts`（资料四条端点的取数与两条写端点的头哨 `X-Meeting-Service: 1`——403 没有失败信封、自成一档；`?from=` 那一档交服务从盘上拷入） |
| `src/home/` | **工作区首页**：`HomeGate` 按闸分流（判据在 `gate.ts`）——`idle` 且没有场次、或 `ended` 且收尾四步都落定（`post` 停 `waiting` 算落定）→ 开 `WorkspaceHome`，其余给会中主视图（收尾落定那一刻页面自己回到工作台）；`WorkspaceHome` 左侧清单（`GET /api/sessions`，按 `recent` 定选中行，行四态）+ 空态（logo + 开场指引）+ 目录选择器弹窗（`DirPicker.tsx`，取数抽成 `useDirBrowse`；起点 `~/`，单击选中、双击进下层，「选定这个目录」确认后才登记；「向上一层」能一路往上走、到盘根如实置灰；清单顶上是正在浏览的这一层（虚线行，单击即可选它）；地址栏下面一行名字框 + 「新建目录」——就地建一个、建完直接选中（名字必须用户给），建不成把服务回的那句原话画在弹窗里；另有**文件模式**给资料区——同一套「选中再确认」，`?files=1`，确认钮「加入资料」，无当前层行与新建行）+ 非空收编与删除确认弹窗 + Windows 大小写并一行 + **选中那一场详情里的会后两个按钮**（`post_offline` / `post_minutes`，那一格按 `has_offline` 渲染）+ **会后进度区**（`GET /api/sessions/{id}` 轮询：可展开收缩、逐块行往下刷、每路一条的进度条）+ **顶栏的「显卡忙不忙」一格**（R31）+ **续接**：顶栏动作钮按选中行改名（录过的叫「接着录」）、详情里摆「录音 共 N 段 · 合计」与每段的时长、起止时刻 + **说话人名字**：详情会后区一行入口「说话人名字…」点开的弹窗（一行一个身份、输入框 + 确认即写这一场目录的人名表、重名提示「编号是分开的，可以重名」；这一行还在录音中（三档口径 `recording`，即本进程此刻在录的那一场）或目录不存在时与两个按钮同判按不动，收尾走到哪一步不拦）；纯逻辑抽在 `gate.ts`、`post.ts`（两个按钮的可用性 / 禁用原因 / 那一格文案）、`progress.ts`（行四态重映射与联结、进度读数、逐块行、忙/闲读法、要不要轮询）、`model.ts`（选中判据 `startWhy` / 动作钮两档名 `startButtonLabel` / 段读数 `segmentsView` / 空态文案 / 初始选择）、`names.ts` 与 `confirmWorkspaceOpen.ts`（`workspace_open` 的非空确认二段式） |
| `src/live/` | 会中视图的**纯逻辑**（组件里不夹业务判断） |
| `src/materials/` | **会议资料区**：`MaterialsPanel.tsx`（清单、缩略图/类型标签、多选删除 + 确认、原图查看、四个入口——页面级粘贴 / 拖拽 / 提交文字 / 选文件——与逐文件上传反馈；`fetchImpl` 可注入，用例不起服务）与 `model.ts`（纯逻辑：类型判读、100M 口径 `MATERIALS_MAX_BYTES`、提交文字的落盘名、粘贴与拖拽的两把筛子、粘贴让位判据、失败分档读法、时刻读法）。会中那块挂在 `App.tsx`（目标目录 = 当前场次），会后那块挂在 `WorkspaceHome` 详情（目标目录 = 选中那一行） |
| `src/dialogs.tsx` | `ConfirmDialog`（`dlg` 体系的最小那只）：从 `WorkspaceHome` 搬出共用，资料区的删除确认与大文件确认是同一副骨架 |
| `src/App.tsx` | 会中主视图的组件树：外壳、两路通道、告警区、收尾区、实时转写、总结区（四档 / 主按钮 / 进行中 / 结果与失败面板 / 本场摘要记录回看）。收音由顶栏的**四组合直切**集中控制（`data-combo` 分段组，映射在 `src/live/combo.ts`，落到 `pause` / `resume` / `set_source` 三个动作）；告警区最前面是「本场已暂停」整行横带（与致命横幅同级） |
| `src/styles/tokens.css` | **逐字节拷贝** `docs/ui/tokens.css`（出处与拷贝日期见文件头注释），是它进构建的那一份；来历见下面「样式的来源」 |
| `src/styles/console.css` | **暗面 token 层**，也是主题开关所在：`:root` 给每个具名色一份暗面取值（`--t-*`），`[data-theme="light"]` 再给一套亮面覆盖。暗面的值单独写在这里，`tokens.css` 一个字符都不动 —— 那份文件自称设计 token 的唯一定义处，改它等于改设计稿 |
| `src/styles/live.css` | 会中视图样式（**暗面一稿**，来路见下） |
| `tests/` | vitest 用例（`npm test`）：契约层三份（拿 golden 样本过 schema、验 schema 咬得住、验动作信封与心跳三态；工作区样本的对齐、行键数与 `segments` 段读数、被打断档的对齐断言都在 `contract.test.ts` / `contract-guards.test.ts`）、失败分档一份（`app-failure.test.ts`）、**工作区首页四份**（`home.test.ts` 纯逻辑与端点取数，含会后两个按钮的可用性与那一格文案、停止入口 `postStopView` 的判据、续接判定与段读数、`gate.ts` 的分流判据；`home-mount.test.tsx` 首页挂载与分流、停止按钮与「新会议会打断」确认弹窗、「向上一层」的到顶置灰；**目录选择器一份**（`dir-picker.test.tsx` 挂载：当前层行单击可选、名字框起名点「新建目录」建完直接选中、建失败画服务原话且层不动 / 清单不动 / 零选中）；`home-progress.test.tsx` 会后进度区的换值渲染与行四态；`names.test.ts` 说话人名字弹窗的行读法与撞名提示；`progress.test.ts` 进度纯逻辑：四态联结、进度读数（含 `interrupted` 自成一相）、逐块行、忙/闲与要不要轮询）、**接口读法一份**（`api.test.ts`：动作信封、本场记录、设备清单、单场状态那条 GET 与它的自排轮询器、`createDir` 的读法——POST 与查询串、写端点头哨、成功体与失败两档）、**资料三份**（`materials-model.test.ts` 纯逻辑：粘贴收图筛子 / 让位判据 / 大小口径 / 提交文字落盘名 / 失败分档；`materials-api.test.ts` 四条端点的取数——头哨、`from` 档、失败分档、golden 过 schema；`materials-panel.test.tsx` 挂载：删除确认 / 大文件确认框 / 拖拽 / 提交文字 / 选择器文件模式 / 原图查看）、**设置抽屉两份**（`live-settings.test.ts` 纯逻辑：抽屉行清单（ASR 五参数一个都不在）、心跳 1–60 判读、设备格会中闸、下拉摆法与缺省档归一；`settings-drawer.test.tsx` 挂载：各项都在、ASR 五参数一个都不出现、设备选完 `settings_update`+`reprobe`、越界当场拒）、会中视图逻辑**十几份**（`live-*.test.ts` / `live-*.test.tsx`：格式、四态、告警、心跳、转写、发声、总结与它的请求状态机、暂停横带与四组合直切、收尾失败读法等）+ 用例自己的真值快照构造器 `live-fixtures.ts` |

### `src/live/` 里各管什么

| 文件 | 管什么 |
|---|---|
| `format.ts` | 时长（`已录 HH:MM:SS`）、墙钟（分钟级；带秒的那一个只给心跳失联）、dBFS 读数与**唯一的换算**（−60…0 → 0–100%） |
| `status.ts` | 四态胶囊（字 + 图形）、七档 `sources[*].health` → 徽标与各通道状态那一行的整句 |
| `alarm.ts` | 致命横幅的三处文案、提示级细条（上限 3）、**发声判定**（`settings.alarm_sound` 与 `muted` 一起读） |
| `combo.ts` | 会中收音的**四组合直切**：四组合（两路都录 / 仅本地 / 仅远端 / 全停）→ 三个动作（`pause` / `resume` / `set_source`）的映射表。现状只从快照读（暂停看 `session.paused`，某一路关没关看 `health === 'stopped'`），映射产出「还差的那几步」，界面不记第二份状态；幂等先算在客户端，重按当前组合一个请求都不发 |
| `drain.ts` | 收尾失败那一档的读法：失败的那几步各摊一行、动作失败与服务侧收尾失败两个来路合并成一块明细视图 |
| `pause.ts` | 「本场已暂停」横幅的取数与文案（只在 `recording` / `alert` 两态给，照 `alarm.ts` 的形状写：纯函数、逐档可验）。状态一律从快照读 |
| `heartbeat.ts` | 心跳四级（正常 / 迟到 / 失联 / 已恢复）+ 失联横幅正文 + 恢复短记录 + 「采集已停」那一档（判停要**连续两次** `false`：`CAPTURE_STOP_CONFIRMATIONS`，`null` / `true` / 失联都把计数清零） |
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

- **数据进 style 变量的分层照设计章 7.7 的口径**：`--level`（当前电平）/ `--hold`（峰值保持）/
  `--h`（包络一根柱）由组件写到元素上，高度怎么画在 `live.css` 里（`var(--level)` / `var(--hold)` /
  `var(--h)` 三处）。**没有一处内联宽高**，也没有一处「让电平看起来在动」的动画。

## 会中视图的结构

一屏从上到下（1440×900，方向 A）：

1. **顶栏** —— 状态胶囊（字 + 图形）+ 状态说明 / 时长 + 主按钮（开始或停止，同一位置只出现一个）+ **收音四组合直切**（`data-combo` 分段组：两路都录 / 仅本地 / 仅远端 / 全停，按下即把收音摆到那个组合）/ 告警提示音开关
2. **音频采集带** —— 两路通道左右并列（路名 + 设备名 + 健康徽标 + dBFS 刻度 + 电平表 + 峰值保持 + 15 根包络 + 读数；关掉的那一路健康徽标写「采集已停止」，开与关由顶栏的四组合直切管，通道卡上不摆按钮），右侧三分之一是摘要区（四档时间范围 + `快速总结` + 原因行或进行中那一句 + 本场摘要记录入口）
3. **各通道状态** —— 两条文字保证（`本地 · 采样中` / `远端 · 信号丢失，从 20:57 起`），任何宽度都不折叠
4. **摘要结果面板 / 失败面板** —— 三小段（`讲了什么` / `讲到哪里` / `丢掉了什么`）或那一档失败的原因，右上角 `关闭`，**关掉即消失、不落文件**；位置就在这一格（各通道状态之后、告警带之前）。本场摘要记录展开之后也落在这里
5. **告警区** —— **「本场已暂停」整行横带**（`alert--paused`，与致命横幅同一副骨架，排在最前面，带 `继续` 与 `停止录音` 两颗；暂停中才出现）+ 致命级整行横幅（哪一路 + 从何时起 + 已丢多久 + 三个操作）与提示级细条；**心跳失联时整块收掉**，「采集已停」那一档也不与致命横幅同屏
6. **收尾区** —— 已结束时的那条横幅（`正在落盘，先别关机` / `可以关机` / `收尾出事了`）与四项明细
7. **实时转写** —— 草稿、`[HH:MM] 本地/远端 · 正文`、识别失败缺口、心跳中断断口、约 200 行窗口、不自动跳回底部 +「回到最新」

**设置抽屉已落地**：会中顶栏右上角的「设置」展开 `drawer` / `scrim`（类名契约
章 7.10 的预留），设置项就地写进 config —— 换设备（`GET /api/devices` 的清单 + 选完
`reprobe`；会中那两格禁用）、告警提示音、心跳频率（1–60 秒，越界当场拒）、快速总结默认
窗口（四档预选随 `settings.summary_default_window` 走）、快速总结模型（下拉列出「不选预设」
与 `settings.summary_preset_names` 里的各套名字，当前选区随 `settings.summary_active_preset`；
**会中可切**，换一套发 `settings_update` 写盘即生效，不必重启。清单在配置文件
`summary.presets_json` 里配，密钥不进快照、不在这里回显）。**ASR 那五个参数一个都不进这扇窗**
（设计意图第 14 条；`web/tests/settings-drawer.test.tsx` 拿渲染结果逐个查）。仍在保留登记里
的：转写历史回看。

## 两条铁律

1. **界面不持有权威状态。** 权威在服务侧 `ServiceRuntime` 手上那一份快照；界面手上那份是推过来的副本，任何分歧以服务为准。界面也不自己造派生数据 —— 包络、`bar`、告警的 `muted` 都由服务算好。界面自己只记三样**观察**：心跳四级、两路丢过的窗口（服务一恢复就把 `lost_since` 清掉，那个窗口只在还看得见它的时候记得下来）、转写窗口；三样都只活在内存里。
2. **浏览器本地存储不存任何会中状态。** `localStorage` / `sessionStorage` / IndexedDB / Cookie 一概不用来存「哪一态、时长、告警、静音、转写」。心跳周期来自快照的 `settings.heartbeat_interval_seconds`，前端不自造第二份配置源。
