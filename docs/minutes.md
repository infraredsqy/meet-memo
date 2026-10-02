# meeting · 会后纪要生成

> 这份文档写会后纪要那一层**已经落地**的那一块（`41465462`）：它吃什么、经哪几步出一份纪要、产物校验是哪四条、跑砸了会怎么样、怎么手动跑一遍。
>
> 产物长什么样（文件清单、状态字段、附录口径）写在 `docs/state.md` 第 3 章「会后纪要生成」；钩子怎么被 `minutes.hook` 解析到、在哪个协议的格子里，写在 `docs/backend-protocols.md` 第 1 章末。配置键与默认值见 `docs/service.md` 第 4 章。

## 1. 谁在什么时候拉起它

`meeting post` 那条链上，离线那一趟跑完之后，`meeting/offline/runner.py` 的 `run_session()` 在**最后**通知一次这一层（`_announce_minutes()`）。顺序不能反：离线那条转写就是这一层的输入之一，先出纪要就永远只有降级档。工作台上「生成会后精细稿」按钮那一趟走的是 `run_session(..., minutes=False)`——**不**通知这一层，纪要归「直接出纪要」那个按钮。

钩子住在 `meeting/backends/minutes.py` 的 `MinutesHook`：`launch(session_dir)` 起一条 daemon 线程跑 `meeting/minutes/runner.py` 的 `run()`，**返回即算启动成功**。离线那一趟不等它跑完，它跑多久都不占收尾预算；它炸了也不带走离线那四份产物、不改 `meeting_offline_status.json`（全程 `try/except BaseException` 兜住，只在主 log 记一条）。

一次 `run()` 按这个顺序做五件事：

| 步 | 做什么 | 落什么 |
|---|---|---|
| 0 | 再入闸 → 先落一份 `state: "running"` 的状态 | `meeting_minutes_status.json`（拉起方在**建线程之前**先落一版，见 1.1） |
| 1 | 找两条转写、判档位 | `working/inputs.json` |
| 2 | 按行切块 | `working/<source>/<kind>/chunks/chunk_NN.md` + `working/<source>/<kind>/00_index.md` |
| 3 | 渲染编排提示词 → **探一次 DSH web 服务端**：在跑就把这一场交付给它（终态 `delivered`），没在跑就拉起 `dsh --profile headless`（终态 `done` / `failed`） | `working/run_prompt.md`、`working/minutes-run.log` |
| 4 | 覆盖成终态（headless 那一趟先按四条判据校验产物，过了才记 `done`；web 分支交付成功即记 `delivered`，不跑那四条） | `meeting_minutes_status.json` |

**先落 `running` 再拉起 DSH**：一次生成最长 `minutes.timeout_seconds` 秒（默认 14400），这期间读方（`meeting post`、界面）要分得出「这一场还没跑」与「这一场正在跑」。进程被杀时钩子把那一份补成 `failed`，状态不会永远停在 `running`。

**再入闸放在 `run()` 的最前面**：环境里有 `MEETING_MINUTES_CHILD=1`（启动器给子进程加的那一格）说明这一趟是 DSH 子会话自己，它当场返回 `skipped`、**不写状态文件** —— 子会话的场次目录里那份状态文件归父进程那一趟所有。提示词里另有一道嘱咐（「不要再拉起任何会后流程」），两道闸各管一头。

### 1.1 在飞标记：同一场挂不上两个子会话

拉起这一层的地方有三处 —— 离线那一趟的尾巴（`_announce_minutes()`，`meeting post` 首跑走它）、`meeting post` 在「离线段已是终态、只补纪要」时自己经同一个拉起点补上的那一趟，以及工作台的「直接出纪要」按钮（`post_minutes`，按 `minutes.hook` 解析出的实现直接 `launch()`）。三处走的是同一条拉起契约，也都可能被用户敲的 `meeting post` 与按下的按钮**同时**走到，因此「这一场已经在飞」这件事必须有盘上的落点：

- `MinutesHook.launch()` 在**建线程之前**同步调一次 `meeting/minutes/status.py` 的 `mark_running()`：方法返回时盘上的 `meeting_minutes_status.json` 已经读得出 `state = "running"`，那一趟起线程的动作在它**之后**；
- `mark_running()` 把「读一遍现状 + 写一版 `running`」这段用一把 `O_CREAT | O_EXCL` 的 **claim 锁**（场次目录根的瞬态文件 `meeting_minutes_status.lock`）裹住，跨进程只有一个进得来：拿不到锁就等 `0.05` 秒再读一遍（最多三次），读到 `running` 就抛 `AlreadyRunningError`、**不覆盖另一趟的账**；
- `AlreadyRunningError` 一路交回调用方：`meeting post` 据此按现成的 `running` 拒跑档收场（退出码 `2`、点名状态文件、给出 `--force`），那一趟**没有起线程、没有拉起子会话**；
- `--force` 是**唯一**的出路：`launch(..., force=True)` 跳过在飞判据，这一趟把那一版 `running` 原子覆盖成自己的（「无视现状重跑」那条语义由此一路走到拉起这一步）；
- 上一趟崩在半路时那枚锁会留在盘上，`mark_running()` 的三次有界等待走完之后照常往下拉起 —— 「上一趟崩了」这条出路必须留着；
- 锁是**瞬态**文件：写完那一版 `running` 就删掉，它**不在产物清单里**（`docs/state.md` 第 3 章那张表里也没有它的名字）。

**拉起 ≠ 落定**：`launch()` 返回只说明这一层被拉起来了，终态要等那条线程或 headless 子进程写。因此 `meeting post` 拉起成功之后**等这一层落终态**才报结论，上界取现成的 `minutes.timeout_seconds`，判据是「盘上那份状态与**拉起之前**的基线不同、并且已经是终态」——上一趟留在盘上的终态不算这一趟的账；到上界还没落定就如实报 `timeout`（既不谎报成功，也不写成 `failed`）。

## 2. 输入

输入只有**场次目录**，两条转写按名字找（`meeting/minutes/chunk.py` 的两个模板），`<source>` 只有 `mic` / `loopback` 两个值：

| 稿 | 文件名 | 行形 |
|---|---|---|
| 会中稿（`meeting/asr/pipeline.py` 写） | `meeting_transcript_<source>.md` | `[HH:MM] 正文`，无说话人标签 |
| 会后稿（`meeting/offline/artifacts.py` 写） | `meeting_offline_transcript_<source>.md` | `[HH:MM] **S01**: 正文` |

两种行形由 `meeting/minutes/lines.py` 的同一个正则一起吃掉（`[HH:MM:SS]` 也认）。

- **档位逐 source 判、整体取公共档位**：每一路自己有哪几份稿是 `both` / `live_only` / `offline_only`；两路不同时整体取 `both`（至少有一份真·会后稿在场上，不把整场说成降级）。逐 source 那一层事实由提示词的说明段逐条写出来。
- **两份稿平级、一起切、一起喂**（`docs/design-intent.md` 第 11 条、R11）：两条都在时**两份各切一套块**，
  落在 `working/<source>/live/` 与 `working/<source>/offline/`（各带自己的 `chunks/` 与 `00_index.md`），
  提示词清单里逐份写清在不在、在哪一份 —— **谁也不叫"权威"，两路永不合并**。
- **行数一律 `lines.count_lines()`**（`splitlines()`，空行计入），但**每份稿各算各的分母**：行号区间表用自己那一份稿的总行数；分段契约那一层的分母跟「在场那份稿」走 —— 两份都在按会中稿，只有会后稿的场次按会后稿（`meeting/minutes/contract.py` 的 `expected_lines_from_inputs()` 与提示词阶段 2 同一条口径）。
- **两个来源一条都没有** → 不启动 DSH、不渲染提示词，状态落 `failed` + 原因。一份没有输入的纪要会以「这场会什么都没定」的样子交出去，这一档不许静默出空产物。

## 3. 提示词与五个阶段

模板是仓库根的 `prompts/meeting_final.md`，渲染器是 `meeting/minutes/prompt.py`。渲染器只做两件事：把路径与档位填进模板、把输入清单拼出来（人话一段 + 机器读的 JSON 一份）。

- 给模型看的路径**一律正斜杠、一律相对场次目录**（`{{SESSION_DIR}}` 填的是 `.`）；绝对路径只出现在清单那段 JSON 里。反斜杠在一段文本里容易被当成转义。
- 模板缺席、有对不上号的占位符、某条输入的路径为空 —— 一律抛 `PromptTemplateError`，**不拿半份提示词去启动子会话**。认识的占位符是 `SESSION_DIR` / `MODE` / `MODE_NOTE` / `INPUTS` / `MAX_SEGMENTS` / `PHASE_BODY`；前五处必须逐处写在模板里，`PHASE_BODY` 可以省略（两块 `PHASE_BODY` 注释标记之间的正文本身就是落点，渲染时整块就地替换）。

模板正文规定五阶段，主 agent 只编排、不读块、不读段总结、不读原始转写：

| 阶段 | 谁做 | 读什么 | 产出 |
|---|---|---|---|
| 1 分割 | **不用做** —— 块已经切好了 | `working/<source>/<kind>/00_index.md` | 认下盘上的分块产物 |
| 2 分割方案 | sub agent A | 每一条输入的区间表与全部块 | `working/segmentation.json` |
| 3 逐段总结 | sub agent B1…Bn（每段一个） | 这一段覆盖的块，两条稿都读 | `working/segment-<id>.md` |
| 4 合并 | sub agent C | `segmentation.json` + 全部 `segment-*.md` | 场次目录根的 `meeting_final.md` |
| 5 只读检查 | 1–2 个只读 sub agent | 终稿与块 | 报告，主 agent 一次性改稿 |

两条转写的对账在流程内部完成：第 3 阶段每个 sub agent 把这一段在两条稿上并排读，按内容判哪一边可信；判不出哪边对的地方如实写「两条稿不一致」。

## 4. 拉起子会话

拉起之前**先探一次 DSH web 服务端**（`meeting/minutes/dsh_web.py` 的 `probe()`，地址取配置键 `minutes.dsh_url`），由这一步定这一趟走哪条分支。探针先做一次 TCP（端口没人应答就到此为止），再发一次**只读、无参数**的 RPC（`session/modelCatalog`）确认协议与鉴权，交回 `(在跑吗, 说明)`；**说明是给用户看的实话**，三档分得开：端口没人听、签不出 cookie、只读探针没通过。在跑 → **web 分支**（交付），没在跑 → **headless 分支**（本进程自己起子进程）。探针自己炸了（读不到凭据、端口上跑着别的程序）也按「没在跑」算，落回现成的 headless 分支，理由进主 log。

`branch` 那一格记**实际走的那一条**（`web` / `headless`）：配置里写 `web` 而那一台没在跑时，状态文件里记的是 `headless`。

### web 分支：交付给 DSH web 服务端

`dsh_web.handoff(session_dir, prompt_text=…, url=…, timeout=…)` 走 DSH 的 `/api`，三步**次序固定**：

1. `workspace/create{path=<场次目录>}` —— 幂等：同一个目录交回既有的那个工作区；
2. `session/create{workspaceId}` —— **只带 `workspaceId`，不带 `cwd`**；
3. `session/prompt{requestId, sessionId, mode: "queue", content: [{type: "text", text: <提示词全文>}]}`。

- **为什么必须先建工作区**：`dsh --profile headless` 建的会话**不带工作区归属**（它直接 `agents.create({meta: {cwd}})`，压根不碰 DSH 的 workspace registry），在 GUI 里落在「未分组」；而 `session/create` **只给 `cwd` 也不进工作区分组**。要让这一场出现在场次目录对应的那个工作区里，只有「先 `workspace/create`、再只带 `workspaceId` 建会话」这一条路。
- **交付语义：投递 accepted 即算交付完成**。这一层**不等** DSH 那边把纪要写完、**也不校验产物**（产物校验是 headless 那条路的事：那是本进程自己起的子进程，等得起）；**停不停 DSH 是用户的事**，超出这一层的边界。跑成什么样，在 DSH GUI 的那条会话里看得见。
- **两份盘上的记录**：`working/run_prompt.md`（发出去的提示词全文）与 `working/minutes-run.log`（三步 RPC 的账：头几行说清这一趟怎么交付的、成没成，后面逐段是三步的请求与应答摘要 —— 这条路没有子进程的 stdout / stderr 可落）。两份都是状态文件 `artifacts` 里声明的那两格。
- **五个失败档各有各的名字**（`HandoffResult.failure`）：`credentials`（签不出 browser-session cookie：读不到凭据、那条记录不在、密钥长度不对）、`workspace_failed`、`session_failed`、`prompt_failed`、`timeout`（交付预算用完：`runner` 递下来的那一格被夹在 `1–60` 秒之间 —— `minutes.timeout_seconds` 是给**生成**用的预算，拿它当一次 HTTP 交付的上限等于没上限）。连不上服务端那一档不在这张表里：那种情况走不到交付这一步（`probe()` 已经把「没在跑」判在前面，于是落回 headless 分支）。中间任何一步失败都**如实记 `failed`，不偷偷改走 headless** —— 交付失败要看得见；成功那一档的状态记 `delivered`（见 `docs/state.md` 第 3 章）。
- **权限不由这一层说了算**：headless 那条路靠子进程环境里那格 `DSH_PERMISSION_MODE=danger-full-access`；走 `/api` 建的会话**权限取 DSH 服务端自己的设置**，本层给不了。DSH 那边若落在 `approval=ask`，无人值守的纪要会卡在等审批 —— 这是走 web 分支要认下的一件事。
- **鉴权**：`/api` 只认 HMAC 签名的 browser-session cookie，没有它一律 401。这一层用 `~/.dsh/.credentials.yaml` 里 `client-connection/browser-session` 那条记录里的**持久签名密钥**自签一枚（有效期 30 天，与 dsh 默认一致，跨重启有效），不依赖任何一次性进程态。

三样协议细节按 **DSH 0.1.5-rc.2 实测**校准，缺任何一个都只会看到 401 / 404：

1. **端点写斜杠形式**（`workspace/create` 这类）：点号写法回 not found，而且 `workspace.list` 这个一元方法**根本不存在** —— 工作区清单走 `workspace/follow` 那条流，因此存活探针用的是只读、无参数的 `session/modelCatalog`；
2. **信封是 `{type, rpcId, method, payload: {args: <参数名>: {…}}}`**：少一层 `args` 回 `arguments-invalid`；参数名逐端点不同，取自端点描述符的 `wire` 字段，这三个端点都叫 `request`；
3. **没有 Cookie 一律 401**。

### headless 分支：本进程自己起子进程

`meeting/minutes/launch.py` 拉起一个 headless DSH 子进程：`shutil.which("dsh")` 解析真实路径（Windows 上 npm 全局安装只生成 `.cmd` / `.ps1` shim，传裸名会 `WinError 2`），argv 是 `[exe, "--profile", "headless", <短指针>]`，`cwd` 是场次目录。

- **argv 里只放一段短指针**（「请读 `working/run_prompt.md` 并完整执行其中的流程」），提示词正文写进 `working/run_prompt.md`。`cmd.exe` 会在第一个换行处截断多行参数，正文走 argv 是把整份提示词赌在命令行解析上。
- **子进程环境显式加三格**：`DSH_PERMISSION_MODE=danger-full-access`（headless 没有界面应答审批）、`PYTHONIOENCODING=utf-8`（Windows 上中文输出的口径）、`MEETING_MINUTES_CHILD=1`（再入闸）。子进程不带窗口（Windows 上给 `CREATE_NO_WINDOW`）。
- **stdout / stderr 落 `working/minutes-run.log`**，头三行是命令、cwd、退出码与耗时，失败原因也写在头里。
- **四档失败各有各的名字**（`launch.py` 的 `LaunchResult.failure`）：`executable_missing`（解析不到可执行文件）、`exit_code`（退出码非 0）、`artifact_missing`（退出码 0 但产物没落盘 / 不合判据）、`timeout`（超时按失败处理，错误里写清超时秒数与提示词路径）。**不能只看退出码** —— V1 遇到过「进程正常退出但纪要不落盘」，退出码 0 只是必要条件。

## 5. 产物校验：四条判据

子进程退出码为 0 之后，`launch.py` 的 `check_final_artifact()` 还要逐条核这四条，缺任何一条都算这一趟失败（`artifact_missing`）：

> **这四条是 headless 分支的收尾校验**：只有「本进程自己起的子进程退出」那一档才跑（那说明纪要真的在盘上）。web 分支交付即完事 —— 投递 accepted 就记 `delivered`，这四条一条都不跑，产物在 DSH GUI 那条会话里看得见。

1. 场次目录根的 `meeting_final.md` 在、去空白之后非空；
2. 文件里有 `## 说话人附录`；
3. 文件里有 `### 猜不出的`；
4. 附录那一节里有一张**真的写了东西**的表：表头（至少 3 格非空）+ 分隔行 + 至少一行数据行（同样至少 3 格非空）。

第 4 条只认「`## 说话人附录` 与 `### 猜不出的` 之间」的行，写在附录之前或之后的表格不算。**认表头只看「一行表格够不够宽」，不看第一格写的是哪个词**：早先按第一格的字面值（`路` / `source`）认表头，模型换个词（「路径」「说话人」）就会把一张只有表头、一条数据都没有的空附录判成合格。

## 6. 配置

五个键在启动时读一次（非热读，改了要重启），归 `pending_restart`：

| 键 | 默认值 | 合法范围 | 谁在用 |
|---|---|---|---|
| `minutes.hook` | `auto` | `auto` / `null` / `dev` / `module:Class`，或 JSON `null` | 解析层：`auto` 落 `meeting.backends.minutes:MinutesHook`；`null` 是**缺席**（「直接出纪要」按钮当场回 404 `action_unavailable`，这一场不出纪要） |
| `minutes.chunk_lines` | `100` | 整数 `10–1000` | 每块行数（与 V1 那套切块口径同值） |
| `minutes.max_segments` | `8` | 整数 `1–20` | 每条 source 的分段上限，契约校验与提示词都用它 |
| `minutes.timeout_seconds` | `14400` | 整数 `60–86400` | 一次生成的墙钟上限（默认四小时：会后时间宽裕换质量，但它仍然要有一个上限） |
| `minutes.dsh_url` | `http://127.0.0.1:3080` | 任意非空字符串 | 拉起之前探的那台 DSH web 服务端的地址（与 `summary.base_url` 同一条口径：本层不校验 URL 形状，探不通就落回 headless 分支）。默认值就是 `dsh web` 的默认监听地址 |

`minutes.hook` 是一个**可空键**，而且 `null` 在它身上是一个**取值**：配置文件里写 JSON 的 `null` 就是「这台机器不接纪要这一层」，解析层看到它直接落缺席档，不会退回 `auto` —— 「直接出纪要」按钮在这一档当场回 404，`meeting post` 补纪要的那一路安静跳过。（写字符串 `"null"` 同解；空字符串不合法。）这一档与两个设备键的「没配」各走各的一支。

## 7. 手动跑一遍

三个只读盘、不加载 `meeting.config` 的子命令（一个后端子命令不必把家目录、日志那条配置链捆在自己身上）：

```powershell
python -m meeting.minutes chunk  <场次目录> [--chunk-lines N]   # 切块 + 写行号区间表
python -m meeting.minutes prompt <场次目录> [--mode …] [--max-segments N] [--template P]   # 渲染提示词打到 stdout
python -m meeting.minutes check  <segmentation.json> [--max-segments N] [--inputs P]      # 校验一份分段方案
```

- `chunk`：场次目录不存在退 `2`；一条转写都没有退 `1`（提示语里写出找的是哪两个文件名）。
- `prompt`：渲染不出来（模板读不动、占位符对不上、一条输入都没有）退 `1`。
- `check`：方案文件不在退 `2`；契约不过退 `1`，消息里点出是哪一条 source 的哪一段不对。它顺带在方案旁边找 `working/inputs.json`（先看方案自己那一层、再看 `working/` 那一层），**读得到就拿逐 source 的真实行数当判据**（分母跟「在场那份稿」走：会中稿在按会中稿，只有会后稿的那一路按会后稿），读不到才退回方案自己声明的 `total_lines`。

提示词正文里让子会话自己跑的那一条是 `python -m meeting.minutes check working/segmentation.json`（阶段 2 末尾的自检）。

## 8. 相关文档

- 产物清单、状态文件字段、附录口径：`docs/state.md` 第 3 章
- 钩子在哪个格子、怎么被解析到：`docs/backend-protocols.md` 第 1、3 章
- 配置键、`settings_update` 的两分：`docs/service.md` 第 4 章、`docs/api.md` 第 4 章
- 会后离线那一层（这一层的上游）：`docs/state.md` 第 3 章
- 需求锚（R12：两条转写一起交给生成纪要的模型）：`docs/requirements.md`
- 索引：`docs/spec.md`
