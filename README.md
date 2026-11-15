# meeting —— 本地会议录制服务

敲 `meeting web` 起一个只监听 `127.0.0.1` 的服务，界面从服务端口打开。开会时把两路声音完整
录下来、边录边出会中临时稿；会后按需拉起模型，出精细稿与纪要。

## 现在能做什么

- **录**：本地麦克风与系统内部声音各录各的，边采边把 16k / 单声道 / int16 裸 PCM 落进场次
  目录；收尾逐段转 MP3，PCM 照样留着。一路出事另一路照跑。
- **看**：会中主视图给四态、两路电平、会中临时稿与告警；空闲态是工作台，列工作区、起录音、
  看会后进度。
- **问**：会中按「总结一下」，十到十五秒拿回三句话——刚才在讲什么 / 讲到哪儿了 / 丢掉了什么。
- **会后**：结束录音不自动跑任何会后动作。工作台上两个按钮各拉自己那一趟：「生成会后精细稿」
  （离线模型，出带说话人编号的稿）与「生成纪要」（交给 DSH）。产物落在这一场的目录里，
  `meeting post <场次目录>` 也能重跑。

判据与边界在 `docs/`：能力清单与配置键见 `docs/service.md`，四态与盘上产物见 `docs/state.md`，
接口见 `docs/contract.md`，整份索引是 `docs/AGENTS.md`。

## 装

要 Python ≥ 3.13；`ffmpeg` 得在 `PATH` 上（转 MP3 靠它；缺席时照常录、转码那一步如实记
`failed`，PCM 一个字节不丢）。

```powershell
pip install -e .          # 服务本体，`meeting` 命令随之可用
# 会中转写与会后精细稿要模型依赖，另装一格；torch 那格是 CUDA 12.8 构建
pip install --extra-index-url https://download.pytorch.org/whl/cu128 -e ".[asr]"
```

不装 `-e` 也行：仓库根 `python -m meeting web` 等价于 `meeting web`。

## 起

```powershell
meeting web                        # 默认 http://127.0.0.1:8765，并打开浏览器
$env:MEETING_PORT=8766; meeting web   # 换端口；被占就拒绝启动，不静默换
$env:MEETING_NO_BROWSER=1; meeting web  # 测试与无头场景
```

只监听 `127.0.0.1`，这一条不可配。

**一场会落在哪由目录决定**：start 给一个已存在的目录，目录名就是这场会的名字——软件不建目录、
不按日期命名、没有默认落点。对录过的目录再按一次开始就是**续接**：新段接着编，旧的录音与稿子
一个字节不碰。

界面产物由服务直发（`web/dist`）；产物不在时 `GET /` 给一张说明页，服务照常起。

## 东西都在哪

| 东西 | 位置 |
|---|---|
| 配置 | `~/.config/meeting/config.json`（点号扁平键名的单层映射） |
| 场次 | start 给的那个目录：录音、转写、产物、`session.json` 都在里面 |
| 服务级 log | `~/.config/meeting/logs/meeting.log`（`log.dir` 可改） |
| 场次级 log | `<场次目录>/meeting.log`，按场查 |

## 三条命令

```powershell
meeting web                # 前台起服务，跑到 Ctrl+C
meeting post  <场次目录>   # 只跑会后那一套：归档补齐 → 会后转写 → 纪要，跑完就退
meeting replay <场次目录>  # 重放验证：素材截断复制出去、在拷贝上重跑离线链
```

后两条的开关、幂等闸与退出码见 `docs/service.md`。

## 开发

```powershell
python -m pytest -q                                   # 服务端全量（快档 + 慢档）
cd web; npm test; npm run typecheck; npm run build     # 界面
```

改界面要 Node ≥ 20，开会时不需要它。界面工程的命令与目录见 `web/README.md`。
动手之前先读 `docs/rules.md`（测试、文档、注释三条纪律）与 `docs/design-intent.md`（这项目
为什么长这样）。

## 联系与许可

跑起来撞到的坑、想加到自己的会上、能不能商用：写信到
[infraredsaw@gmail.com](mailto:infraredsaw@gmail.com)。缺陷与功能请求更适合开一条 issue。

[MIT](LICENSE)。
