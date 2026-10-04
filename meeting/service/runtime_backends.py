"""``ServiceRuntime`` 的「后端接线与探测」mixin：绑定 / 解析 / 探测 / 起停。

方法与方法间注释均自 ``meeting/service/runtime.py`` 第 644-1167 行一字不改复制；
本文件自写内容只有模块 docstring、import 与 ``_log`` 一行、类声明行。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable

from meeting import config as config_module, logging_setup
from meeting.backends.base import SOURCE_IDS, ReadOnlyConfig
from meeting.backends.registry import (
    capabilities_of,
    resolve_capture_backend,
    resolve_post_hook,
    resolve_transcriber_backend,
)
from meeting.config import ConfigValueError
from meeting.state import (
    PROBE_STATUS,
    SettingsView,
    now_iso,
    validate_health,
)

_log = logging.getLogger("meeting.service.runtime")


class RuntimeBackendsMixin:
    """「后端接线与探测」：``revision`` 等只读小接口、绑定地址、startup/shutdown、后端解析与设备探测。"""

    # -- 只读小接口 -------------------------------------------------------

    @property
    def revision(self) -> int:
        return int(self.snapshot.revision)

    @property
    def top_state(self) -> str:
        return self.snapshot.top_state

    @property
    def url(self) -> str:
        """由**监听 socket 的 `getsockname()` 读回值**拼出的 URL（绑定前取不到）。"""
        if self.bound_port is None:
            raise ConfigValueError(
                "service.url",
                None,
                "先绑监听 socket 并调 set_binding()，再取 URL"
                "（绑定前这个值取不到，不许拿配置值或字面量顶替）",
                "service.port / service.url 只能由监听 socket 的 getsockname() 产生",
            )
        return f"http://{self.bound_host}:{self.bound_port}"

    @property
    def log_path(self) -> str:
        return logging_setup.service_log_path()

    def _value(self, key: str) -> Any:
        if self.config is None:
            return config_module.default_for(key)
        return self.config.get(key)

    def _settings_view(self) -> SettingsView:
        """快照 `settings` 的**生效值**视图。

        口径（§3.5.5 + §7.1 高危点 7）：这里读的是**运行期生效值**。归 `pending_restart` 的那些键
        （`web.*` / `log.*` / `alert.*` 与三个后端选择键、会中总结端点与预算那几键）由
        `settings_update` 写盘成功之后，快照里的 `settings` **不会**立刻反映写进去的新值，
        要重启才可见；热读键才在写盘后立刻换成新值（那一处在 `settings_update()` 里按
        `applied` 重取一次本函数）—— `asr.*` 五个之外，`alarm.sound` 与设置抽屉那四项
        （`devices.mic` / `devices.loopback` / `heartbeat.interval_seconds` /
        `summary.default_window`）也走这一条：出声开关勾选即改运行期行为，设备改完
        紧接着的 `reprobe` 就按新值挑（会中仍不换设备，`docs/contract.md` 第 7 章），
        心跳周期与总结缺省档的快照格当场换新值。

        `settings.devices` 的两个子键逐一取 `devices.mic` / `devices.loopback` 的生效值
        （`dca4539b` 定名；没配时为 `null`，与配置层 `NULLABLE_KEYS` 的空值形状一致）。

        会中切模型的两格（本节点）：`summary_preset_names` 是预设的**名字清单**（只给名字，
        预设内容与密钥不进快照）；`summary_active_preset` 是当前用哪一套的**生效值** ——
        指针指向的名字不在清单里（没配、解析失败、名字已删）时，实际生效的是老四键
        平铺那一档，这里照实报空串。
        """
        preset_names = [
            item["name"]
            for item in config_module.summary_presets_of(self._value("summary.presets_json"))
        ]
        active_preset = str(self._value("summary.active_preset") or "").strip()
        if active_preset and active_preset not in preset_names:
            active_preset = ""
        return SettingsView(
            alarm_sound=bool(self._value("alarm.sound")),
            heartbeat_interval_seconds=int(self._value("heartbeat.interval_seconds")),
            # 快速总结的缺省档：四档取值（装载与写前校验都只放四档，这里原样带出）
            summary_default_window=str(self._value("summary.default_window")),
            summary_preset_names=preset_names,
            summary_active_preset=active_preset,
            # `settings.asr` 的五个子键与配置键 `asr.*` **逐字同名**（只去掉前缀），取生效值；
            # 嵌套写法只出现在状态 JSON 里，不进请求 / 应答 / config.json
            asr={name: self._value(f"asr.{name}") for name in config_module.ASR_SETTING_KEYS},
            # `settings.devices` 的两个子键同理（子键名与配置键的点号后半段逐字同名）
            devices=self._devices_view(),
        )

    def _devices_view(self) -> dict[str, Any]:
        """`settings.devices` 的两个子键：取两个设备键位的**生效值**（没配即 `None`）。"""
        return {
            name: self._value(f"devices.{name}") for name in config_module.DEVICE_SETTING_KEYS
        }

    # -- 绑定地址 ---------------------------------------------------------

    def set_binding(self, host: str, port: int) -> None:
        """从监听 socket 的 `getsockname()` 读回值写入（`port` 不许另外猜）。

        **已经绑到一个真实端口之后不许被改掉**（`port == 0` 是「还没绑」的同一档，
        仍可被真实端口覆盖）。这样「快照里的 `service.port` 就是本次真正监听的那个端口」
        这条判据在 `cli.py` 之外也成立。
        """
        if self.bound_port not in (None, 0):
            return
        self.bound_host = str(host)
        self.bound_port = int(port)

    def bind_from_scope(self, scope: dict[str, Any]) -> None:
        """ASGI 挂载路径上用请求的 `server` 信息补齐绑定（只补一次）。"""
        if self.bound_port not in (None, 0):
            return
        server = scope.get("server") or ("127.0.0.1", 0)
        host, _, port = str(server[0]), server[1], None
        try:
            port = int(server[1])
        except (TypeError, ValueError, IndexError):  # pragma: no cover - 非标准 server 元组
            port = 0
        self.set_binding(host or "127.0.0.1", port or 0)

    # -- 生命周期（由框架 lifespan 的 startup 钩子触发） -------------------

    async def startup(self) -> None:
        """启动序列：取事件循环 → 写关停目标 → 信号自检 → 解析后端 → 探测设备 →
        组装初始快照（**含 `transcript` 三格的初值**，见 ⑤b）→ 打开浏览器。
        **绝不自动开始录音，也不去读任何别的场次目录**（落点由工作区给，注册表归下一节点）。
        """
        if self._started:
            return
        self._started = True
        self.started_at = now_iso()

        # ① 取事件循环（`asyncio.get_running_loop()` 只有在这里才成立）
        self._loop = asyncio.get_running_loop()

        # ② 写关停目标：语义 = 「先跑收尾、再请求关停」（常驻 app.py 的安装点，形态不变）
        from meeting.service import app as service_app

        service_app.set_shutdown_target(
            self._loop, service_app.make_trigger_shutdown(self._loop, self)
        )

        # ③ 信号接管自检（服务器的信号捕获已被 ServiceServer.capture_signals() 屏蔽）
        self._signal_ok = service_app.check_signal_handlers()

        # ④ 解析三个后端（唯一入口是 registry；失败回退替身 + warning）
        self._resolve_backends()

        # ④b 会后在飞记录的对账（b1c6a58e 的 d5）：上一趟会后被进程死掉截断时，盘上停着
        # running、记录还写着 —— 这里把它补成 `interrupted`（治「杀了进程就永远停在在跑」
        # 那条病）。这是「启动序列不读场次目录」那条口径的**定向例外**：只读记录里点名的
        # 那一场，不扫任何目录；对账的任何意外只警告，不阻塞启动。
        self._reconcile_interrupted_post_jobs()

        # ⑤ 组装初始快照（`top_state: idle`、`session: null`、`sources` 取 probe() 的三态）
        self._probe_devices()

        # ⑤b `transcript` 三格的初值**取自后端**，不留给 dataclass 默认值
        #
        # **为什么要有这一步**（复审 m3）：`_refresh_transcript_snapshot()`
        # 只挂在节拍与「一行到来」上时，快照初值就只能是 `TranscriptState` 的 dataclass 默认
        # `health = "normal"`；而真实现的空闲态报的是 `stopped`（没起过场次 —— 见
        # `meeting/backends/transcribe.py` 的 `health()`），于是健康度节拍**第一拍**
        # （0.5 秒后）才把它刷成 `stopped` 并 `commit()`（`revision 1 -> 2`）。后果有两条：
        # ① `contract/fixtures/snapshot/idle*.json` 描述的只是「服务起来前半秒」那一瞬的取值，
        # ② 空闲场次的用例被迫抢在那一拍之前读快照 —— 一条**纯粹的时序依赖**
        # （机器被压住 >0.5 秒就红）。
        #
        # 初值在 `startup()` 里就地取一次之后：空闲档稳定成 `health = "stopped"`、
        # `revision` 停在 `REVISION_START`（初值不 `commit()`：`startup()` 里没有任何状态迁移），
        # 上面那两条一起消失。位置放在 ⑤ 之后是必须的 —— `_probe_devices()` 才把转写后端
        # `bind()` 上（`_config` 也在那里给），早于它调这一步会读到一份没接上配置的后端。
        # 返回值（= `health` 有没有迁移）在这里**有意丢掉**：`startup()` 不做提交。
        self._refresh_transcript_snapshot()
        # 空闲态不发提示级（`docs/ui-design.md` 章 2.5：空闲态告警区不出现）：快照这一格与
        # 判定器那份记账一起从零开始。`_refresh_transcript_snapshot()` 的收口判据是
        # `top_state ∈ {recording, alert}`，开机这一刻是 `idle`，所以它自己不会写进任何提示；
        # 这里再显式清一次，是为了让「快照 = 记账」这条不变量从第一帧就成立。
        self._clear_hints()

        # ⑥ 打开浏览器（尾部；`MEETING_NO_BROWSER=1` 时跳过并落一行日志）
        self._open_browser()

    def _reconcile_interrupted_post_jobs(self) -> None:
        """启动时对一次「在飞」记录（b1c6a58e 的 d5）：记录里点名的、盘上仍 `running` 的
        那一场 offline 会后，补成 `interrupted`，然后清记录。

        三档，逐档口径：

          - **offline 档 + 盘上 `running`** → 走 `runner.write_interrupted_status()`（与
            `run_session` 的打断终态同一形状：十二格、`state = "interrupted"`、缺口注记
            拼进 `error`，缺口从逐块账读出）。这一格写完，读方（4.9 的 `offline.state`、
            界面进度区、幂等闸）看到的都是「人停的、可以重按」，而「杀了服务就永远停在
            在跑」从此只剩不到一次服务生命周期的窗口。
          - **minutes 档** → 只清记录 + log：纪要那一层的 `running` 由它自己的钩子兜底
            （`docs/state.md`「进程被杀时钩子把它补成 failed」），这里再补一份就把两层
            的账搅在一起了。
          - **盘上不是 `running`**（那趟其实跑完了、或状态文件读不出）→ 只清记录 + log，
            一个字节都不写 —— 对账不许把已经落定的终态翻掉。

        范围**只有记录里点名的那一场**：记录文件里至多一条（服务单槽），这里读它、处置
        它、清它，不扫任何目录。CLI（`meeting post`）那一趟既不落记录也不被这一步碰 ——
        它活在另一个进程，服务分不清「另一进程还在跑」与「都死了」，如实不猜。
        """
        from meeting.service import postjobs

        records = postjobs.load()
        if not records:
            return
        try:
            postjobs.clear()
        except Exception:  # pragma: no cover - 清不上只警告：盘上状态处置照旧做
            _log.warning("启动对账：在飞记录清不掉", exc_info=True)
        for item in records:
            kind = str(item.get("kind") or "")
            target = str(item.get("dir") or "")
            if not target:
                continue
            if kind != "offline":
                _log.info(
                    "启动对账：上一趟会后的在飞记录（%s：%s）只清不碰"
                    "——纪要那一层有自己的钩子兜底", kind, target,
                )
                continue
            try:
                from meeting.offline import runner as offline_runner

                written = offline_runner.write_interrupted_status(
                    target,
                    reason="服务重启时这一趟还在跑（进程已结束），按「被打断」收场",
                )
            except Exception:
                _log.warning(
                    "启动对账：这一场补 interrupted 没写成（只警告，不阻塞启动）：%s",
                    target, exc_info=True,
                )
                continue
            if written is not None:
                _log.warning(
                    "启动对账：上一趟 offline 会后没跑完，盘上状态已补成 interrupted"
                    "（%s）；重按按钮可从头再跑", target,
                )
            else:
                _log.info(
                    "启动对账：在飞记录点名的那一场盘上不是 running（可能已跑完或没有状态"
                    "文件），只清记录：%s", target,
                )

    def _open_browser(self) -> None:
        """打开浏览器这一步：故障一律收成警告，**不阻塞启动**。"""
        from meeting.service import app as service_app

        try:
            service_app.launch_browser(self.url)
        except BaseException as exc:  # 兜底：这一步的任何意外都不许带崩启动钩子
            _log.warning(
                "打开浏览器这一步出意外（只警告、不阻塞）：%s: %s", type(exc).__name__, exc
            )

    async def shutdown(self) -> None:
        """关停动作：停 ticker、停采集、**停转写**、清关停目标（本 Phase 不做收尾，收尾归 phase3）。

        `transcriber_backend.stop()` 在关停路径上一直缺一个调用点：协议里这个方法从 phase1
        就冻结着，而在此之前没有任何一处调它 —— 于是转写的读线程与 worker 会一直挂到进程退出
        （`9e5db89c` phase2 §7 的那条接线）。它与采集一样**失败只记账、不抛**：关停路径上
        任何一步出事都不许把进程卡住。
        """
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._transcriber_ticker is not None:
            self._transcriber_ticker.cancel()
            self._transcriber_ticker = None
        try:
            if self.capture_backend is not None:
                self.capture_backend.stop()
        except Exception:
            _log.exception("关停时停采集失败（如实记账，不静默）")
        try:
            if self.transcriber_backend is not None:
                self.transcriber_backend.stop()
        except Exception:
            _log.exception("关停时停转写失败（如实记账，不静默）")
        # 收尾四步的执行器**不等**（工作线程是 daemon：第二次 `Ctrl+C` 之后进程不许被在飞的
        # 同步步拖住）——`wait=False` 顺带把已排队的收尾步丢掉，这正是「立即中止」要的形态。
        self._shutdown_drain_executor()
        # 会中总结那台执行器照同一口径收场：到点被放弃的那次调用仍在飞，关停不等它
        # （它同样是一条只读的模型调用，写不出半截现场）。
        self._shutdown_summary_executor()
        # 会中模型「收 / 放」那台同样不等：在飞的那次释放 / 拉起是模型层自己的事
        # （丢引用、清缓存、造识别器），进程退出时跟着一起消失。
        self._shutdown_model_executor()
        # 会中总结那两样一并清掉（缓冲与记录表走同一个入口）：关停之后这两笔内存不该留着
        self._clear_session_summary(reason="服务关停")
        from meeting.service import app as service_app

        service_app.clear_shutdown_target()

    def _shutdown_drain_executor(self) -> None:
        """收尾执行器收场（幂等）：**不等待**在飞的工作线程（daemon）。"""
        executor = self._drain_executor
        if executor is None:
            return
        self._drain_executor = None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 收场路径不掩盖别的结论
            _log.exception("收尾执行器收场失败（工作线程是 daemon，不影响进程退出）")

    def _shutdown_summary_executor(self) -> None:
        """会中总结执行器收场（幂等）：与收尾那台同一口径，**不等待**在飞的工作线程。"""
        executor = self._summary_executor
        if executor is None:
            return
        self._summary_executor = None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 收场路径不掩盖别的结论
            _log.exception("会中总结执行器收场失败（工作线程是 daemon，不影响进程退出）")

    def _shutdown_model_executor(self) -> None:
        """会中模型「收 / 放」执行器收场（幂等）：与上面两台同一口径，**不等待**在飞的工作线程。"""
        executor = self._model_executor
        if executor is None:
            return
        self._model_executor = None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover - 收场路径不掩盖别的结论
            _log.exception("会中模型生命周期执行器收场失败（工作线程是 daemon，不影响进程退出）")

    # -- 后端与探测 -------------------------------------------------------

    def _resolve_backends(self) -> None:
        """三个后端的解析入口（唯一入口是 `registry`；**判缺席只看解析结论**）。"""
        self.capture_resolution = resolve_capture_backend()
        self.capture_backend = self.capture_resolution.implementation
        self.capture_capabilities = capabilities_of(self.capture_backend)

        self.transcriber_resolution = resolve_transcriber_backend()
        self.transcriber_backend = self.transcriber_resolution.implementation

        self.post_resolution = resolve_post_hook()
        self.post_hook = self.post_resolution.implementation

        _log.info(
            "后端解析：capture=%s（在场=%s）/ transcriber=%s（在场=%s）/ post=%s（在场=%s）",
            self.capture_resolution.target or self.capture_resolution.selector,
            self.capture_resolution.has_implementation,
            self.transcriber_resolution.target or self.transcriber_resolution.selector,
            self.transcriber_resolution.has_implementation,
            self.post_resolution.target or self.post_resolution.selector,
            self.post_resolution.has_implementation,
        )

    def _config_view(self) -> ReadOnlyConfig:
        values = self.config.as_dict() if self.config is not None else config_module.as_dict()
        return ReadOnlyConfig(values)

    def bind_capture(self, backend: Any) -> Callable[[Any], None]:
        """给采集后端包一层线程安全的事实出口（`loop.call_soon_threadsafe`）。

        后端**不必知道事件循环在哪**，也不许自己碰状态 —— 它拿到的永远是包好的回调
        （`bind()` 里交给后端的正是这个 `sink`；本方法因此不读 `backend`，形参只为调用点可读）。
        """
        del backend  # 只用于调用点的可读性：出口属于 runtime，不属于某个后端实例

        def sink(fact: Any) -> None:
            """可从**任何线程**调的事实出口。"""
            loop = self._loop
            if loop is None or not loop.is_running():
                # 事件循环还没起来 / 已经关掉：同步处理（后端线程不要因为这里抛错而死）
                if loop is None:
                    self.apply_fact(fact)
                return
            try:
                loop.call_soon_threadsafe(self.apply_fact, fact)
            except RuntimeError:  # pragma: no cover - 循环正在关闭
                _log.debug("事件循环已在关闭，丢弃一条事实：%r", fact)

        return sink

    def bind_transcriber(self, backend: Any) -> Callable[[Any], None]:
        """转写后端的 `line_sink` 同样只经 `call_soon_threadsafe` 进循环。

        **顺路把这一行收进会中总结那条内存缓冲**（`1987cb20` phase2）：缓冲只在这一个 sink 里
        喂，`bind()` 被调两次也**只装一层**（两次都返回本方法包出来的 sink，收行的那一步在
        sink 内部、与装了几层无关，于是同一行不会进缓冲两遍）。收行**先于**
        `_call_soon_threadsafe`：缓冲自己是线程安全的，事件循环没起来 / 已经关掉时那一支
        （`loop is None` 时同步处理、其余丢弃）也照旧把这行收下 —— 「谁收行」与「谁推状态」
        是两件事。
        """
        del backend  # 同 `bind_capture()`：出口属于 runtime

        def sink(line: Any) -> None:
            try:
                self.summary_buffer.append(line)
            except Exception:  # noqa: BLE001 - 缓冲出意外不许把一行转写挡在门外
                _log.warning("转写行进会中总结缓冲失败（这一行照旧往下走）", exc_info=True)
            loop = self._loop
            if loop is None or not loop.is_running():
                if loop is None:
                    self._apply_transcript_line(line)
                return
            try:
                loop.call_soon_threadsafe(self._apply_transcript_line, line)
            except RuntimeError:  # pragma: no cover - 循环正在关闭
                _log.debug("事件循环已在关闭，丢弃一行转写：%r", line)

        return sink

    def _probe_devices(self) -> None:
        """调 capture 后端的 `probe()`，**在它之前先 `bind()`**，结果直取进初始快照。"""
        backend = self.capture_backend
        view = self._config_view()
        try:
            sink = self.bind_capture(backend)
            backend.bind(sink, config=view)
        except Exception:
            _log.exception("采集后端 bind() 失败（如实记账，不静默）")

        # 帧间隔的声明位 = 实例属性 `frame_interval`（读不到或非正数 → 回退并记 warning）
        raw_interval = getattr(backend, "frame_interval", None)
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            self._warn_frame_interval(raw_interval)
        elif not math.isfinite(float(raw_interval)) or float(raw_interval) <= 0:
            self._warn_frame_interval(raw_interval)
        else:
            self.frame_interval = float(raw_interval)

        try:
            if self.transcriber_backend is not None:
                self.transcriber_backend.bind(self.bind_transcriber(self.transcriber_backend),
                                              config=view)
        except Exception:
            _log.exception("转写后端 bind() 失败（如实记账、不阻塞录音）")

        self._probe_and_apply(
            reason="设备探测结果写入初始快照",
            threshold="探针三态（unprobed / ready / probe_failed），本档无阈值",
        )

    def rebind_and_probe(self) -> list[Any]:
        """重新 `bind()` 一次再 `probe()`（`reprobe` 动作走的就是这条路，幂等）。

        `probe()` 每次都用**当下**的配置与**当下**的系统端点表，于是「录到一半插上设备之后
        打一个动作重新探测」这条要求落在这里 —— 两路快照随即更新。
        """
        backend = self.capture_backend
        try:
            sink = self.bind_capture(backend)
            backend.bind(sink, config=self._config_view())
        except Exception:
            _log.exception("采集后端 bind() 失败（如实记账，不静默）")
        return self._probe_and_apply(
            reason="重新探测设备（reprobe）",
            threshold="探针三态（unprobed / ready / probe_failed），本档无阈值",
        )

    def reprobe(self) -> None:
        """`reprobe`：重新枚举并试开设备，更新快照两路（`dca4539b` phase1 §2）。

        **只在没有场次在跑的时候可用**：`recording` / `alert` 态下这两路设备是开场前定下来的，
        会中换设备属于明文不做的那一档（`docs/contract.md` 第 7 章），因此这两态一律拒绝，
        `code` 取既有的 `already_recording`（取值域冻结在 `ACTION_CODES` 里，不自造新码）。
        `ended`（含收尾停留档）态下采集已经停了，重新探测不干扰任何在飞的东西。
        """
        if self.snapshot.top_state in ("recording", "alert"):
            raise self._already_recording_error()
        _log.info("重新探测设备：按当下配置与当下系统端点表再探一遍")
        self.rebind_and_probe()
        self.commit()

    def _probe_and_apply(self, *, reason: str, threshold: str) -> list[Any]:
        """`probe()` 走一遍，把结果落进快照两路（启动与 `reprobe` 共用这一段）。"""
        backend = self.capture_backend
        try:
            probes = backend.probe()
        except Exception:
            _log.exception("采集后端 probe() 失败（如实记账，不静默）")
            probes = []
        self.probes = list(probes or [])
        self._apply_probe_results(reason=reason, threshold=threshold)
        return self.probes

    def _apply_probe_results(self, *, reason: str, threshold: str) -> None:
        """把 `self.probes` 按 `source` 落进快照两路（`dca4539b` phase1 §3 的落点）。

        越界取值（后端返回第四个探针状态这类）记 warning 并把该路落 `probe_failed`：
        服务照常起、`top_state` 不动。
        """
        by_source: dict[str, Any] = {}
        for probe in self.probes:
            source = getattr(probe, "source", None)
            if source in SOURCE_IDS:
                by_source[source] = probe
            else:
                _log.warning("设备探测：不认识的一路 %r，忽略", source)

        for item in self.snapshot.sources:
            probe = by_source.get(item.id)
            if probe is None:
                item.health = "unprobed"
                item.device_name = None
                _log.info("设备探测：%s 没有探测结果，按 unprobed 处理", item.id)
                continue
            status = getattr(probe, "status", None)
            if status not in PROBE_STATUS:
                # 后端违约（返回第四个探针状态这类）：记 warning、该路落 probe_failed、
                # top_state 不动、服务照常起（采集侧的异常不结束会话）
                _log.warning(
                    "设备探测：%s 返回了取值域外的状态 %r，按 probe_failed 处理", item.id, status
                )
            item.health = validate_health(status)
            if item.health == "probe_failed":
                _log.warning(
                    "设备探测：%s 这一路没有可用的设备（status=%s，试过哪几条见上面的 detail）",
                    item.id, status,
                )
            item.device_name = getattr(probe, "device_name", None)
            _log.info(
                "设备探测：%s -> status=%s device=%s（%s）",
                item.id, status, item.device_name, getattr(probe, "detail", ""),
            )
        # 探测结果落定即落一次「每一路健康迁移」（`unprobed` / `ready` / `probe_failed` 三档：
        # `unprobed` / `ready` 无阈值，`probe_failed` 的阈值口径见 `PROBE_STATUS` 的三档）
        self._log_health_migrations(reason=reason, threshold=threshold)

    def _warn_frame_interval(self, raw: Any) -> None:
        from meeting.state import DEFAULT_FRAME_INTERVAL_SECONDS

        self.frame_interval = DEFAULT_FRAME_INTERVAL_SECONDS
        _log.warning(
            "采集后端没有声明可用的 frame_interval（读到 %r），按 %ss 算帧时钟窗口"
            "（只影响窗口取值，不阻塞 start、不改 top_state）",
            raw, DEFAULT_FRAME_INTERVAL_SECONDS,
        )
