# meeting · 会后纪要生成

> 会后纪要那一层的流程规格与判据：谁拉起它、吃什么、经哪几步、产物四条判据、失败档位、手动入口。
>
> 产物清单、状态文件字段、在飞标记的盘上落点见 `docs/state.md`；`minutes.hook` 怎么解析到这一层见 `docs/backend-protocols.md`；配置键正本在 `docs/service.md`。

## 1. 拉起与再入

两个人工入口：`meeting post` 那条链上离线那一趟跑完之后 `run_session()` **最后**通知一次这一层（顺序不能反——离线那条转写就是这一层的输入之一）；工作台「生成纪要」按钮（`post_minutes`）。「生成会后精细稿」按钮走 `run_session(..., minutes=False)`，**不**通知这一层。

**整条链默认与 DSH 隔离，只有这两个人工入口放行**：`run_session()` 的 `announce_minutes` 与 `MinutesHook.launch()` 的 `deliver` 两个开关缺省假——测试、脚本、库调用不传就是「本次非人工发起」，安静跳过：不解析钩子、不落状态文件（绝不留卡在 `running` 的状态）、不切块、探针一次都不发。放行之后探针 / 交付 / headless 拉起三处各自还有一道**默认拒绝**的闸（不放行当场抛 `DeliveryForbidden`，一次网络 / 子进程动作都不发生）——绕过纪要层直接 import 这些函数的代码，也到不了 DSH。

钩子 `launch()` 起一条 daemon 线程跑 `meeting/minutes/runner.py` 的 `run()`，返回即算启动成功；它炸了也不带走离线那四份产物、不改 `meeting_offline_status.json`。一次 `run()` 按序做五件事：

| 步 | 做什么 | 落什么 |
|---|---|---|
| 0 | 再入闸 → 先落一份 `state: "running"` 的状态 | `meeting_minutes_status.json`（拉起方在建线程之前先落一版） |
| 1 | 找两条转写、判档位 | `working/inputs.json` |
| 2 | 按行切块 | `working/<source>/<kind>/chunks/chunk_NN.md` + `00_index.md` |
| 3 | 渲染编排提示词 → **探一次 DSH web 服务端**：在跑就交付（终态 `delivered`），没在跑就拉起 headless 子会话（终态 `done` / `failed`） | `working/run_prompt.md`、`working/minutes-run.log` |
| 4 | 覆盖成终态（headless 那趟先按四条判据校验产物，过了才记 `done`；web 分支交付成功即记 `delivered`，不跑那四条） | `meeting_minutes_status.json` |

- **再入闸**：环境里有 `MEETING_MINUTES_CHILD=1`（启动器给子进程加的）说明这一趟是 DSH 子会话自己，`run()` 当场返回、不写状态文件。提示词里另有一道嘱咐（「不要再拉起任何会后流程」），两道闸各管一头。
- **同一场挂不上两个子会话**：`launch()` 建线程之前同步调 `mark_running()`，把「读现状 + 写 running」用一把 `O_CREAT | O_EXCL` 的瞬态 claim 锁裹成跨进程原子（锁写完即删、不在产物清单里）；盘上已停着一版 `running` 就抛 `AlreadyRunningError`、**不覆盖另一趟的记录**。上一趟崩在半路留在盘上的锁不挡路：有界等待走完照常往下拉起。
- **`AlreadyRunningError` 一路交回调用方**：`meeting post` 按现成的 `running` 拒跑档收场（退出码 `2`、点名状态文件、给出 `--force`）；`--force` 是**唯一**出路——跳过在飞判据，把那一版 `running` 原子覆盖成自己的。
- **拉起 ≠ 落定**：`meeting post` 拉起成功之后**等这一层落终态**才报结论，上界取 `minutes.timeout_seconds`，判据是「盘上状态与拉起之前的基线不同、并且已是终态」；到上界没落定就如实报 `timeout`（不谎报成功，也不写成 `failed`）。

## 2. 输入

输入只有**场次目录**，两条转写按名字找，`<source>` 只有 `mic` / `loopback`；两种行形由同一个正则一起吃掉（`[HH:MM:SS]` 也认）：

| 稿 | 文件名 | 行形 |
|---|---|---|
| 会中稿 | `meeting_transcript_<source>.md` | `[HH:MM] 正文`，无说话人标签 |
| 会后稿 | `meeting_offline_transcript_<source>.md` | `[HH:MM] **S01**: 正文` |

- **档位逐 source 判、整体取公共档位**；两路不同时整体取 `both`（至少有一份真·会后稿在场上，不把整场说成降级）。
- **两份稿平级、一起切、一起喂**（R11 / R12）：两份都在时**各切一套块**（`working/<source>/live/` 与 `working/<source>/offline/`），提示词清单里逐份写清在不在——**谁也不叫「权威」，两路永不合并**。
- **行号区间表用自己那份稿的总行数**；分段契约的分母跟「在场那份稿」走（两份都在按会中稿，只有会后稿按会后稿）。
- **两个来源一条都没有** → 不启动 DSH、不渲染提示词，状态落 `failed` + 原因——不许静默出空产物。
- **人名表也是一份输入**：场次目录根的 `meeting_speaker_names.json`（机主确认过的 `(编号, 名字)` 对）。不进切块、不影响档位，只在渲染提示词时读一遍；缺席或读不出按「没有确认过的名字」渲染，**不挡出纪要**。

## 3. 提示词与编排

模板是仓库根的 `prompts/meeting_final.md`（编排流程的正本在模板正文），渲染器是 `meeting/minutes/prompt.py`：

- 给模型看的路径**一律正斜杠、一律相对场次目录**；绝对路径只出现在清单那段 JSON 里。
- 模板缺席、占位符对不上号、某条输入路径为空 → 抛 `PromptTemplateError`，**不拿半份提示词去启动子会话**。认识的占位符：`SESSION_DIR` / `MODE` / `MODE_NOTE` / `INPUTS` / `SPEAKER_NAMES` 必写，`MAX_SEGMENTS` 与 `PHASE_BODY` 可省略。
- 编排硬约束：**整场 sub agent 不超过 3 个**（短会 1 个写手直接写场次目录根的 `meeting_final.md`，中 / 长会 2–3 个各写一批 `working/part-<k>.md` 由主 agent 合并）；档位按**实质讨论量**判，时长与总行数只作参考。
- 两条转写的核对在写手那一层完成：两份都读、按内容判哪边可信，判不出就如实写「两条稿不一致」；**程序只校验场次目录根的 `meeting_final.md`**。
- **会议资料（`meeting_materials/`，若在）也是这一轮的输入**：写手当这场会的一部分消化。资料只进模型的 context：纪要正文的格式骨架与说话人附录一格不改、没有「参考资料」一节，模板对有料与没料的场次是同一份。

## 4. 拉起子会话

拉起之前**先探一次 DSH web 服务端**（`probe()`，地址取 `minutes.dsh_url`）：先一次 TCP，再发一次只读、无参数的 RPC 确认协议与鉴权。在跑 → **web 分支**，没在跑 → **headless 分支**；探针自己炸了也按「没在跑」算、落回 headless，理由进主 log。状态文件 `branch` 那格记**实际走的那一条**。

### web 分支：交付给 DSH web 服务端

`handoff()` 走 DSH 的 `/api`，三步**次序固定**（先建工作区是唯一能让这一场落进对应工作区的路——headless 建的会话不带工作区归属）：

1. `workspace/create{path=<场次目录>}` —— 幂等：同一个目录交回既有工作区；
2. `session/create{workspaceId}` —— 只带 `workspaceId`，不带 `cwd`；
3. `session/prompt{requestId, sessionId, mode: "queue", content: [{type: "text", text: <提示词全文>}]}`。

- **交付语义：投递 accepted 即算交付完成**。不等 DSH 写完纪要、**也不校验产物**（校验是 headless 那条路的事）；停不停 DSH 是用户的事。
- **五个失败档**：`credentials` / `workspace_failed` / `session_failed` / `prompt_failed` / `timeout`。任何一步失败都**如实记 `failed`，不偷偷改走 headless**。
- **权限不由这一层说了算**：走 `/api` 建的会话权限取 DSH 服务端自己的设置；DSH 若落在 `approval=ask`，无人值守的纪要会卡在等审批——这是走 web 分支要认下的一件事。
- **鉴权**：`/api` 只认 HMAC 签名的 browser-session cookie，用本地鉴权服务记录的持久签名密钥自签（详见该服务自身文档；有效期 30 天，跨重启有效）。
- **三样协议细节按 DSH 0.1.5-rc.2 实测校准**（正本 `meeting/minutes/dsh_web.py`），缺任何一个只会看到 401 / 404：端点写斜杠形式（点号写法回 not found；`workspace.list` 不存在——存活探针用只读的 `session/modelCatalog`）；信封是 `{type, rpcId, method, payload: {args: {…}}}`（少一层 `args` 回 `arguments-invalid`）；没有 Cookie 一律 401。

### headless 分支：本进程自己起子进程

`launch.py` 拉起 headless DSH 子进程：argv 是 `[exe, "--profile", "headless", <短指针>]`，`cwd` 是场次目录。

- **argv 里只放一段短指针**（「请读 `working/run_prompt.md` 并完整执行其中的流程」），提示词正文写进文件——`cmd.exe` 会在第一个换行处截断多行参数。
- **子进程环境显式加三格**：`DSH_PERMISSION_MODE=danger-full-access`（headless 没有界面应答审批）、`PYTHONIOENCODING=utf-8`、`MEETING_MINUTES_CHILD=1`（再入闸）。子进程不带窗口。
- **stdout / stderr 落 `working/minutes-run.log`**，头三行是命令、cwd、退出码与耗时。
- **四档失败**：`executable_missing` / `exit_code` / `artifact_missing` / `timeout`。**不能只看退出码**——V1 遇到过「进程正常退出但纪要不落盘」。

## 5. 产物校验：四条判据

子进程退出码 0 之后还要逐条核这四条，缺任何一条都算失败（`artifact_missing`）。**只跑在 headless 分支**——web 分支交付即记 `delivered`，一条都不跑：

1. 场次目录根的 `meeting_final.md` 在、去空白之后非空；
2. 文件里有 `## 说话人附录`；
3. 文件里有 `### 猜不出的`；
4. 附录那一节里有一张**真的写了东西**的表：表头 + 分隔行 + 至少一行数据行（各至少 3 格非空）。

第 4 条只认两个标题之间的行；**认表头只看「一行表格够不够宽」，不看第一格写的是哪个词**——模型换个词就会把空附录判成合格。附录里机主确认过的名字随提示词的 `{{SPEAKER_NAMES}}` 进来，模型照抄进表（把握「高」、依据「机主在界面上确认」），这几个编号不再推断；模型推断的逐行带「依据 / 把握」。

## 6. 配置

五个键（`minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url`）都非热读、改了要重启；键名、默认值与合法范围的正本在 `docs/service.md`，`minutes.hook` 的取值口径（`null` 是缺席档的取值）见 `docs/backend-protocols.md`。

## 7. 手动跑一遍

三个只读盘、不加载 `meeting.config` 的子命令（用法与退出码见 `python -m meeting.minutes --help` 与各子命令 `-h`）：

```powershell
python -m meeting.minutes chunk  <场次目录>             # 切块 + 写行号区间表
python -m meeting.minutes prompt <场次目录> [--mode …]  # 渲染提示词打到 stdout
python -m meeting.minutes check  <segmentation.json>    # 校验一份分段方案
```

`check` 顺带找 `working/inputs.json`：读得到就拿逐 source 的真实行数当判据，读不到才退回方案自己声明的 `total_lines`。提示词正文里**不让子会话跑任何 `python -m` 命令**（子会话的 cwd 在场次目录，`meeting` 包不在它的模块搜索路径上）：块由编排层提前切好，`check` 只留给手动跑。

## 8. 相关文档

- 产物清单、状态文件字段（含在飞标记与 claim 锁）、附录口径：`docs/state.md`
- 钩子成员与解析：`docs/backend-protocols.md`
- 配置键正本：`docs/service.md`
- 需求锚（R11 / R12）：`docs/requirements.md`
- 索引：`docs/AGENTS.md`
