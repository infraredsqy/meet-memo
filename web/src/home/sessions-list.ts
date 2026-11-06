import { fetchSessions } from '../api/sessions';
import { initialSelection } from './model';
import type { SessionsResponse } from '../contract';

/**
 * 工作区列表的取数与选区落座（web-sessions-list-polling-duplicated）：LiveView 与
 * WorkspaceHome 两屏共用的那一份接线 —— `GET /api/sessions` 的应答整块交给调用方，
 * 选区还空着时按服务给的 `recent` 落座（判据在 `model.ts` 的 `initialSelection()`，
 * 兜底第一行）。
 *
 * 轮询节奏归两屏自己（会中屏按心跳节奏 `setInterval`、工作台按动作与活动闸重取）；
 * 失败那一跳两屏也各有各的处置（会中屏静默等下一跳、工作台画失败块），由 `onFail`
 * 给 —— 不给就静默。
 */
export function loadSessionList(io: {
  /** 应答原文的去处（两屏各自的列表 setter）。 */
  onList: (data: SessionsResponse) => void;
  /** 选区落座：拿到的是「空着才坐进去」的那只 updater，调用方原样交给自己的 setter。 */
  onSelection: (seat: (current: string | null) => string | null) => void;
  /** 取数失败那一跳（网络层 / 形状不对都算）；省略即静默。 */
  onFail?: (error: unknown) => void;
}): Promise<SessionsResponse | null> {
  return fetchSessions().then(
    (data) => {
      io.onList(data);
      io.onSelection((current) => current ?? initialSelection(data.sessions, data.recent));
      return data;
    },
    (error) => {
      io.onFail?.(error);
      return null;
    },
  );
}
