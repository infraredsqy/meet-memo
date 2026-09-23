/**
 * 契约层：服务与界面之间那份契约的 TS 侧落点。
 *
 * 正文在 `docs/contract.md`，可机读基准在 `contract/fixtures/`。
 * 这边只做两件事：把形状写成 zod schema，把 schema 变成静态类型。
 *
 * 一句话分工：**客户端不持有权威状态**。这里没有「当前状态」这种成员，
 * 只有「怎么读服务推过来的那一份」。
 */

export * from './primitives';
export * from './enums';
export * from './snapshot';
export * from './sse';
export * from './actions';
export * from './summaries';
export * from './heartbeat';
