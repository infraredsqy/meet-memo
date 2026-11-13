import type { SummaryWindowChoice } from '../contract';
import {
  SUMMARY_CLIENT_TIMEOUT,
  SUMMARY_CLIENT_TIMEOUT_SECONDS,
  initialSummaryState,
  runSummaryExchange,
  summaryLogView,
  summaryResultView,
  type SummaryFailure,
  type SummaryLogView,
  type SummaryState,
} from './summary';

/**
 * 总结区那个**请求状态机**的宿主。
 *
 * 三件事必须有地方落，否则它们会散在组件里、也没法用 node 用例验：
 *
 * 1. **这一次请求的状态**（闲 / 跑 / 有结果 / 失败）与**当前选中的档位**；
 * 2. **客户端那条 30 秒兜底线** —— 它是一条墙钟承诺，跑起来之后要靠定时器落地；
 * 3. **迟到的应答**：用户等了 30 秒（兜底已经画成超时）之后，服务那一趟可能过一会儿才回来。
 *    那一次的结果**不覆盖**已经画出来的东西（画面上那句「再按一次」说的是现在的实情），
 *    也不写任何记录缓存。
 *
 * 它**不持有权威状态**：会议状态、转写健康、能不能按，全在服务推来的那份快照上，
 * 由调用方在渲染时现算（`summaryAvailable()` / `summaryWhy()`）。这里只有这一次动作的
 * 应答原文与本场记录那一次 GET 的原文 —— 两样都是服务给的，不派生。
 */

/** 这一次请求的应答原文（服务给的 `summary` 子对象）。 */
export interface SummaryAnswer {
  window: string;
  window_label: string;
  at: string;
  elapsed_seconds: number;
  lines: number;
  what: string[];
  where: string[];
  lost: string[];
}

/** 记录端点应答里的一条（`GET /api/summaries`，比应答体少一格 `lines`）。 */
export interface SummaryRecord {
  window: string;
  window_label: string;
  at: string;
  elapsed_seconds: number;
  what: string[];
  where: string[];
  lost: string[];
}

export interface SummaryControllerDeps {
  /**
   * 打那一次动作；成功给应答原文（`summary` 子对象），失败抛（带 `code` / `message` /
   * `detail`）。**面板要摆的样子由这一层去算**（`summaryResultView`），注入方只管把
   * 服务给的原文交过来。
   */
  request: (window: SummaryWindowChoice) => Promise<SummaryAnswer>;
  /** 取本场记录那一次 GET。 */
  loadLog: () => Promise<SummaryRecord[]>;
  /** 现取一个毫秒时钟（默认 `Date.now`）。 */
  now?: () => number;
  /** 兜底秒数（默认 30；用例给一个小值把这一档压到墙钟上量得出来）。 */
  timeoutSeconds?: number;
}

export interface SummaryController {
  /** 现在的状态（进视图的就是它）。 */
  state(): SummaryState;
  /** 状态一变就叫一次（组件据此重画）。 */
  subscribe(listener: () => void): () => void;
  /** 换一个档位。 */
  select(window: SummaryWindowChoice): void;
  /**
   * 把服务给的缺省档铺到预选上（：默认预选随快照走）。
   *
   * **只在没有被人显式挑过档**的这一场里生效 —— 用户自己挑过的档位不被动改；
   * 已经在跑 / 有结果 / 有失败的那几档同样不碰：那些档位说的是「这一次」用哪个窗口，
   * 缺省档管的是开场时预选落在哪一格，两件事各走各的。
   */
  applyDefaultWindow(window: SummaryWindowChoice): void;
  /** 按一下（正跑着的那一次不受影响：按钮此时已经按不动了）。 */
  run(): Promise<void>;
  /** 关掉结果面板 / 失败面板（记录仍在）。 */
  close(): void;
  /** 记录那一块现在的样子（没取过、取失败时就给上一次取到的那一份）。 */
  log(): SummaryLogView | null;
  /** 这一场按过几次（**服务给的条目数**）；还没取过就是 `null`。 */
  logCount(): number | null;
  /** 取一次本场记录；已经在取就不重复发。 */
  loadRecords(): Promise<void>;
  /** 记录那一块取不到时给的那句话；取到了就是 `null`。 */
  logError(): string | null;
  /** 面板开着没有（组件把显隐告诉这一层：它决定「手上这一份」算不算用户正看着的东西）。 */
  setLogOpen(open: boolean): void;
}

export function createSummaryController(deps: SummaryControllerDeps): SummaryController {
  const now = deps.now ?? Date.now;
  const listeners = new Set<() => void>();

  let state = initialSummaryState();
  let log: SummaryLogView | null = null;
  let logError: string | null = null;
  let logInFlight = false;
  let logOpen = false;
  /** 用户在本页显式挑过档吗？挑过之后，快照带来的缺省档不再覆盖他的选择。 */
  let userPickedWindow = false;
  /** 请求代号：只有当下这一号的结果进状态（旧号的应答一律丢掉）。 */
  let generation = 0;

  const emit = (): void => {
    for (const listener of listeners) listener();
  };

  const setState = (next: SummaryState): void => {
    state = next;
    emit();
  };

  const controller: SummaryController = {
    state: () => state,

    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },

    select(window: SummaryWindowChoice): void {
      if (state.window === window) return;
      userPickedWindow = true;
      setState({ ...state, window });
    },

    applyDefaultWindow(window: SummaryWindowChoice): void {
      if (userPickedWindow) return;
      if (state.phase !== 'idle') return;
      if (state.window === window) return;
      setState({ ...state, window });
    },

    async run(): Promise<void> {
      generation += 1;
      const mine = generation;
      const window = state.window;

      setState({ ...state, phase: 'running', result: null, failure: null, elapsedSeconds: null });

      const outcome = await runSummaryExchange(window, {
        // 窗口标签、三小段、时刻都从服务给的原文上读，这一层不造
        request: async (choice) => summaryResultView(await deps.request(choice)),
        now,
        timeoutSeconds: deps.timeoutSeconds ?? SUMMARY_CLIENT_TIMEOUT_SECONDS,
      });

      // 迟到的应答：这一号已经不是当下那一号了（用户又按了一次，或者换了档位重按）
      if (mine !== generation) return;

      if (outcome.kind === 'ok') {
        setState({
          ...state,
          phase: 'done',
          result: outcome.result,
          failure: null,
          elapsedSeconds: outcome.elapsedSeconds,
        });
        // 成功了就顺手把本场记录取一次：入口那一行写的次数**永远是服务给的条目数**，
        // 界面自己不数按了几下。面板开着时更要取 —— 新那一条要当场进列表；
        // 面板关着时这一下也是必要的（否则入口那一行还停在按之前的数）。
        void controller.loadRecords();
        return;
      }

      const failure: SummaryFailure =
        outcome.kind === 'timeout'
          ? {
              code: SUMMARY_CLIENT_TIMEOUT,
              message: '',
              // 兜底那一档没有服务的 detail；秒数由界面自己量出来的这条数说
              fields: { elapsed_seconds: outcome.elapsedSeconds },
            }
          : outcome.failure;

      setState({
        ...state,
        phase: 'failure',
        result: null,
        failure,
        elapsedSeconds: outcome.elapsedSeconds,
      });
    },

    close(): void {
      if (state.phase !== 'done' && state.phase !== 'failure') return;
      setState({ ...state, phase: 'idle', result: null, failure: null, elapsedSeconds: null });
    },

    log: () => log,

    logCount: () => (log === null ? null : log.count),

    async loadRecords(): Promise<void> {
      if (logInFlight) return;
      logInFlight = true;
      try {
        const records = await deps.loadLog();
        log = summaryLogView(records);
        logError = null;
      } catch (error) {
        // 取不到就保留上一次取到的那一份（与设计章 5.2「刷新这一场」同一条立场：
        // 手上这份旧的就地留着，另加一行说明），**不冒充成「这一场还没总结过」**
        logError = `取不到本场总结记录：${error instanceof Error ? error.message : String(error)}`;
      } finally {
        logInFlight = false;
        emit();
      }
    },

    logError: () => logError,

    setLogOpen(open: boolean): void {
      // 面板的显隐归组件；这一层记住它，是为了**只在面板真开着的时候**把「当前这一份」
      // 当成用户正在看的东西（关着时它只是入口那一行要用的计数，没什么要看的样子）。
      // 判据：状态真的换了才算一次变化，同一个值反复设不惊动订阅者。
      if (logOpen === open) return;
      logOpen = open;
      emit();
    },
  };

  return controller;
}