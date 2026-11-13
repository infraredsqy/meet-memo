import type { SessionStatus, SpeakerIdentity } from '../contract';

/**
 * 工作台详情那一侧「说话人名字」的纯逻辑（`docs/requirements.md` R10 第四条 / R30，
 * 节点）。
 *
 * 入口一行（会后区里），点开一个小弹窗：一行一个身份（路标签 + 编号 + 现名/待确认），
 * 输入框 + 确认，动作回包即重问单场状态（当场生效）。这一层只做「这一行怎么读、
 * 撞名怎么判」这两件事，判据全部来自服务给的 `GET /api/sessions/{id}` 那份读数的
 * `speakers` 一格——不落本地存储、不自己猜（契约章 6 第 1、2 条）。
 */

/** 改名的动作名（与 `docs/contract.md` 4.11 逐字对齐）。 */
export const SPEAKER_RENAME_ACTION = 'speaker_rename';

/** 弹窗里的一行：服务给的身份原文 + 界面要画的读法。 */
export interface NamesRowView {
  /** 限定键（`mic:S01` 这种带路前缀的限定名，或没跑过聚类时的路本身）。 */
  key: string;
  /** 一行的读法：路标签；带编号时附上编号（`本地 · S01`）。 */
  title: string;
  /** 现名；没起过名时 `null`（界面画「待确认」）。 */
  name: string | null;
}

/**
 * 单场读数 → 弹窗的行。`status` 还没到手（404 / 网络瞬态）或这一场盘上没有材料时
 * 给空数组——空态由弹窗自己如实画一句，不编身份。
 */
export function namesRows(status: SessionStatus | null): NamesRowView[] {
  const identities = status?.speakers ?? [];
  return identities.map((identity) => ({
    key: identity.key,
    title: identityTitle(identity),
    name: identity.name,
  }));
}

/** 一行的读法：路标签打头；离线编号跟在后面（`本地 · S01`），路级身份只有标签。 */
export function identityTitle(identity: SpeakerIdentity): string {
  const local = identity.key === identity.source ? '' : identity.key.slice(identity.source.length + 1);
  return local ? `${identity.label} · ${local}` : identity.label;
}

/**
 * 撞名的那几行：与要确认的名字（去首尾空白后）同名的**其它**键。
 *
 * 两个编号叫同一个名合法（编号是分开的）——这一格只拿来提示一句
 * 「编号是分开的，可以重名」，提交照走、不拦。空名字不算撞名（空 = 不起名）。
 */
export function duplicateNameHolders(
  rows: readonly NamesRowView[],
  key: string,
  name: string,
): string[] {
  const wanted = name.trim();
  if (!wanted) return [];
  return rows
    .filter((row) => row.key !== key && row.name !== null && row.name === wanted)
    .map((row) => row.key);
}
