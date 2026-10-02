/**
 * 服务接口的这一侧：SSE、动作、心跳、本场总结记录、工作区注册表、目录浏览与设备清单。
 *
 * 六条都是**只读或单向下发**的形状 —— 这里没有「本地状态源」，
 * 拿到的每一份数据都是从服务推过来或取回来的副本。
 */

export * from './events';
export * from './actions';
export * from './summaries';
export * from './sessions';
export * from './devices';
export * from './heartbeat';
