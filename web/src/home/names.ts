import type { SessionStatus, SpeakerIdentity } from '../contract';

/**
 * 工作台详情那一侧「说话人名字」的纯逻辑。
 *
 * 这一层只做「这一行怎么读、撞名怎么判」这两件事，判据全部来自服务给的
 * `GET /api/sessions/{id}` 那份读数的 `speakers` 一格——不落本地存储、不自己猜。
 */

export const SPEAKER_RENAME_ACTION = 'speaker_rename';

export interface NamesRowView {
  /** 限定键（`mic:S01` 这种带路前缀的限定名，或没跑过聚类时的路本身）。 */
  key: string;
  title: string;
  name: string | null;
}

export function namesRows(status: SessionStatus | null): NamesRowView[] {
  const identities = status?.speakers ?? [];
  return identities.map((identity) => ({
    key: identity.key,
    title: identityTitle(identity),
    name: identity.name,
  }));
}

export function identityTitle(identity: SpeakerIdentity): string {
  const local = identity.key === identity.source ? '' : identity.key.slice(identity.source.length + 1);
  return local ? `${identity.label} · ${local}` : identity.label;
}

/**
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
