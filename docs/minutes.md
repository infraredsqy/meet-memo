# meeting · 会后纪要生成

> 这份文档写会后纪要那一层**已经落地**的那一块（`41465462`）：它吃什么、经哪几步出一份纪要、产物校验是哪四条、跑砸了会怎么样、怎么手动跑一遍。
>
> 产物长什么样（文件清单、状态字段、附录口径）写在 `docs/state.md` 第 3 章「会后纪要生成」；钩子怎么被 `minutes.hook` 解析到、在哪个协议的格子里，写在 `docs/backend-protocols.md` 第 1 章末。配置键与默认值见 `docs/service.md` 第 4 章。

## 1. 谁在什么时候拉起它

离线那一趟跑完之后，`meeting/offline/runner.py` 的 `run_session()` 在**最后**通知一次这一层（`_announce_minutes()`）。顺序不能反：离线那条转写就是这一层的输入，先出纪要就永远只有降级档。

钩子住在 `meeting/backends/minutes.py` 的 `MinutesHook`：`launch(session_dir)` 起一条 daemon 线程跑 `meeting/minutes/runner.py` 的 `run()`，**返回即算启动成功**。离线那一趟不等它跑完，它跑多久都不占收尾预算；它炸了也不带走离线那三份产物、不改 `meeting_offline_status.json`（全程 `try/except BaseException` 兜住，只在主 log 记一条）。

一次 `run()` 按这个顺序做五件事：

| 步 | 做什么 | 落什么 |
|---|---|---|
| 0 | 再入闸 → 先落一份 `state: "running"` 的状态 | `meeting_minutes_status.json` |
| 1 | 找两条转写、判档位 | `working/inputs.json` |
| 2 | 按行切块 | `working/<source>/chunks/chunk_NN.md` + `working/<source>/00_index.md` |
| 3 | 渲染编排提示词 → 拉起 `dsh --profile headless` | `working/run_prompt.md`、`working/minutes-run.log` |
| 4 | 校验产物 → 覆盖成终态 | `meeting_minutes_status.json` |

**先落 `running` 再拉起 DSH**：一次生成最长 `minutes.timeout_seconds` 秒（默认 14400），这期间读方（`meeting post`、界面）要分得出「这一场还没跑」与「这一场正在跑」。进程被杀时钩子把那一份补成 `failed`，状态不会永远停在 `running`。

**再入闸放在 `run()` 的最前面**：环境里有 `MEETING_MINUTES_CHILD=1`（启动器给子进程加的那一格）说明这一趟是 DSH 子会话自己，它当场返回 `skipped`、**不写状态文件** —— 子会话的场次目录里那份状态文件归父进程那一趟所有。提示词里另有一道嘱咐（「不要再拉起任何会后流程」），两道闸各管一头。

## 2. 输入

输入只有**场次目录**，两条转写按名字找（`meeting/minutes/chunk.py` 的两个模板），`<source>` 只有 `mic` / `loopback` 两个值：

| 稿 | 文件名 | 行形 |
|---|---|---|
| 会中临时稿（`meeting/asr/pipeline.py` 写） | `meeting_transcript_<source>.md` | `[HH:MM] 正文`，无说话人标签 |
| 会后权威稿（`meeting/offline/artifacts.py` 写） | `meeting_offline_transcript_<source>.md` | `[HH:MM] **S01**: 正文` |

两种行形由 `meeting/minutes/lines.py` 的同一个正则一起吃掉（`[HH:MM:SS]` 也认）。

- **档位逐 source 判、整体取公共档位**：每一路自己有哪几条稿是 `both` / `live_only` / `offline_only`；两路不同时整体取 `both`（至少有一条真·离线稿在场上，不把整场说成降级）。逐 source 那一层事实由提示词的说明段逐条写出来。
- **切块读的是会后那份权威稿**，它缺席时退回会中稿。
- **行数一律 `lines.count_lines()`**（`splitlines()`，空行计入）：行号区间表与分段契约的分母都是这一个数。
- **两个来源一条都没有** → 不启动 DSH、不渲染提示词，状态落 `failed` + 原因。一份没有输入的纪要会以「这场会什么都没定」的样子交出去，这一档不许静默出空产物。

## 3. 提示词与五个阶段

模板是仓库根的 `prompts/meeting_final.md`，渲染器是 `meeting/minutes/prompt.py`。渲染器只做两件事：把路径与档位填进模板、把输入清单拼出来（人话一段 + 机器读的 JSON 一份）。

- 给模型看的路径**一律正斜杠、一律相对场次目录**（`{{SESSION_DIR}}` 填的是 `.`）；绝对路径只出现在清单那段 JSON 里。反斜杠在一段文本里容易被当成转义。
- 模板缺席、有对不上号的占位符、某条输入的路径为空 —— 一律抛 `PromptTemplateError`，**不拿半份提示词去启动子会话**。认识的占位符是 `SESSION_DIR` / `MODE` / `MODE_NOTE` / `INPUTS` / `MAX_SEGMENTS` / `PHASE_BODY`；前五处必须逐处写在模板里，`PHASE_BODY` 可以省略（两块 `PHASE_BODY` 注释标记之间的正文本身就是落点，渲染时整块就地替换）。

模板正文规定五阶段，主 agent 只编排、不读块、不读段总结、不读原始转写：

| 阶段 | 谁做 | 读什么 | 产出 |
|---|---|---|---|
| 1 分割 | **不用做** —— 块已经切好了 | `working/<source>/00_index.md` | 认下盘上的分块产物 |
| 2 分割方案 | sub agent A | 每一条输入的区间表与全部块 | `working/segmentation.json` |
| 3 逐段总结 | sub agent B1…Bn（每段一个） | 这一段覆盖的块，两条稿都读 | `working/segment-<id>.md` |
| 4 合并 | sub agent C | `segmentation.json` + 全部 `segment-*.md` | 场次目录根的 `meeting_final.md` |
| 5 只读检查 | 1–2 个只读 sub agent | 终稿与块 | 报告，主 agent 一次性改稿 |

两条转写的对账在流程内部完成：第 3 阶段每个 sub agent 把这一段在两条稿上并排读，按内容判哪一边可信；判不出哪边对的地方如实写「两条稿不一致」。

## 4. 拉起子会话

`meeting/minutes/launch.py` 拉起一个 headless DSH 子进程：`shutil.which("dsh")` 解析真实路径（Windows 上 npm 全局安装只生成 `.cmd` / `.ps1` shim，传裸名会 `WinError 2`），argv 是 `[exe, "--profile", "headless", <短指针>]`，`cwd` 是场次目录。

- **argv 里只放一段短指针**（「请读 `working/run_prompt.md` 并完整执行其中的流程」），提示词正文写进 `working/run_prompt.md`。`cmd.exe` 会在第一个换行处截断多行参数，正文走 argv 是把整份提示词赌在命令行解析上。
- **子进程环境显式加三格**：`DSH_PERMISSION_MODE=danger-full-access`（headless 没有界面应答审批）、`PYTHONIOENCODING=utf-8`（Windows 上中文输出的口径）、`MEETING_MINUTES_CHILD=1`（再入闸）。子进程不带窗口（Windows 上给 `CREATE_NO_WINDOW`）。
- **stdout / stderr 落 `working/minutes-run.log`**，头三行是命令、cwd、退出码与耗时，失败原因也写在头里。
- **四档失败各有各的名字**（`launch.py` 的 `LaunchResult.failure`）：`executable_missing`（解析不到可执行文件）、`exit_code`（退出码非 0）、`artifact_missing`（退出码 0 但产物没落盘 / 不合判据）、`timeout`（超时按失败处理，错误里写清超时秒数与提示词路径）。**不能只看退出码** —— V1 遇到过「进程正常退出但纪要不落盘」，退出码 0 只是必要条件。

`branch` 这一格记**实际走的那一条**：`auto` / `web` / `headless` 三种取值本轮都落 headless，配置里写 `web` 而实际走了 headless 时，状态文件里记的是 `headless`。

**本轮没有实现 web 分支**：V1 用的那套 web 路径在 DSH 0.1.5-rc.2 上已经失效（`workspace.list` 这个一元方法不存在、信封 payload 要包一层 `args`、`/api` 只认 HMAC 签名的 Cookie，密钥在 `%DSH_HOME%\.credentials.yaml`）。走它就得照抄一份会随版本漂的私有鉴权协议，归下游节点 `65dd3e4c`。

## 5. 产物校验：四条判据

子进程退出码为 0 之后，`launch.py` 的 `check_final_artifact()` 还要逐条核这四条，缺任何一条都算这一趟失败（`artifact_missing`）：

1. 场次目录根的 `meeting_final.md` 在、去空白之后非空；
2. 文件里有 `## 说话人附录`；
3. 文件里有 `### 猜不出的`；
4. 附录那一节里有一张**真的写了东西**的表：表头（至少 3 格非空）+ 分隔行 + 至少一行数据行（同样至少 3 格非空）。

第 4 条只认「`## 说话人附录` 与 `### 猜不出的` 之间」的行，写在附录之前或之后的表格不算。**认表头只看「一行表格够不够宽」，不看第一格写的是哪个词**：早先按第一格的字面值（`路` / `source`）认表头，模型换个词（「路径」「说话人」）就会把一张只有表头、一条数据都没有的空附录判成合格。

## 6. 配置

四个键在启动时读一次（非热读，改了要重启），归 `pending_restart`：

| 键 | 默认值 | 合法范围 | 谁在用 |
|---|---|---|---|
| `minutes.hook` | `auto` | `auto` / `null` / `dev` / `module:Class`，或 JSON `null` | 解析层：`auto` 落 `meeting.backends.minutes:MinutesHook`；`null` 是**缺席**（离线跑完不产生纪要，安静跳过） |
| `minutes.chunk_lines` | `100` | 整数 `10–1000` | 每块行数（与 V1 那套切块口径同值） |
| `minutes.max_segments` | `8` | 整数 `1–20` | 每条 source 的分段上限，契约校验与提示词都用它 |
| `minutes.timeout_seconds` | `14400` | 整数 `60–86400` | 一次生成的墙钟上限（默认四小时：会后时间宽裕换质量，但它仍然要有一个上限） |

`minutes.hook` 是一个**可空键**，而且 `null` 在它身上是一个**取值**：配置文件里写 JSON 的 `null` 就是「关掉自动跑」，解析层看到它直接落缺席档，不会退回 `auto`。（写字符串 `"null"` 同解；空字符串不合法。）这一档与两个设备键的「没配」各走各的一支。

## 7. 手动跑一遍

三个只读盘、不加载 `meeting.config` 的子命令（一个后端子命令不必把家目录、日志、输出根那条配置链捆在自己身上）：

```powershell
python -m meeting.minutes chunk  <场次目录> [--chunk-lines N]   # 切块 + 写行号区间表
python -m meeting.minutes prompt <场次目录> [--mode …] [--max-segments N] [--template P]   # 渲染提示词打到 stdout
python -m meeting.minutes check  <segmentation.json> [--max-segments N] [--inputs P]      # 校验一份分段方案
```

- `chunk`：场次目录不存在退 `2`；一条转写都没有退 `1`（提示语里写出找的是哪两个文件名）。
- `prompt`：渲染不出来（模板读不动、占位符对不上、一条输入都没有）退 `1`。
- `check`：方案文件不在退 `2`；契约不过退 `1`，消息里点出是哪一条 source 的哪一段不对。它顺带在方案旁边找 `working/inputs.json`（先看方案自己那一层、再看 `working/` 那一层），**读得到就拿逐 source 的真实行数当判据**，读不到才退回方案自己声明的 `total_lines`。

提示词正文里让子会话自己跑的那一条是 `python -m meeting.minutes check working/segmentation.json`（阶段 2 末尾的自检）。

## 8. 相关文档

- 产物清单、状态文件字段、附录口径：`docs/state.md` 第 3 章
- 钩子在哪个格子、怎么被解析到：`docs/backend-protocols.md` 第 1、3 章
- 配置键、`settings_update` 的两分：`docs/service.md` 第 4 章、`docs/api.md` 第 4 章
- 会后离线那一层（这一层的上游）：`docs/state.md` 第 3 章
- 需求锚（R12：两条转写一起交给生成纪要的模型）：`docs/requirements.md`
- 索引：`docs/spec.md`
