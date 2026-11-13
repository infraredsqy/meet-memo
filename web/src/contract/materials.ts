import { z } from 'zod';
import { isoStringSchema, pathStringSchema } from './snapshot';

/**
 * 会议资料的四条端点（`docs/contract.md` 4.12，资料节点）。
 *
 * - `GET /api/sessions/{id}/materials`：**目录就是清单**（没有 JSON 记录），每条三格
 *   ——名字、大小、贴上时刻（= 文件修改时间）；`meeting_materials/` 还不在时 `items`
 *   是空数组。任何状态下都 200（寻址失败 404）；
 * - `POST /api/sessions/{id}/materials?name=`：上传成功体 = 通用信封 + `material`
 *   三格；`?from=` 那一档（选择器文件模式的盘上拷入）成功体同形状；
 * - `DELETE /api/sessions/{id}/materials/{name}`：删除成功体 = 通用信封 + `material`
 *   一格（名字）。
 *
 * 资料层**独立于四态快照**：不提交、不涨 `revision`、不广播——信封里的 `revision`
 * 只是当前版本号（先例是 `summarize`）。
 */

/** 清单里的一条资料：名字、字节数、贴上时刻。 */
export const materialItemSchema = z
  .object({
    name: z.string(),
    size_bytes: z.number().int(),
    added_at: isoStringSchema,
  })
  .strict();

/** `GET /api/sessions/{id}/materials` 的应答体：两个路径 + 按名字排序的清单。 */
export const materialsListSchema = z
  .object({
    dir: pathStringSchema,
    materials_dir: pathStringSchema,
    items: z.array(materialItemSchema),
  })
  .strict();

/** 上传成功体：通用信封 + `material` 恰好三格。 */
export const materialUploadOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    material: z
      .object({
        name: z.string(),
        size_bytes: z.number().int(),
        added_at: isoStringSchema,
      })
      .strict(),
  })
  .strict();

/** 删除成功体：通用信封 + `material` 恰好一格（名字；大小与时刻随文件一起没了）。 */
export const materialDeleteOkSchema = z
  .object({
    ok: z.literal(true),
    revision: z.number().int(),
    material: z
      .object({
        name: z.string(),
      })
      .strict(),
  })
  .strict();

/**
 * 资料这一片自己的失败信封（`contract/fixtures/materials/missing.json` 那一档）。
 *
 * `material_missing` 是 4.12 那片自己的 `code`——**不在**动作那九个 `code` 表里
 * （资料四条不走 `/api/actions`）；`session_dir_missing` / `invalid_setting` /
 * `persist_failed` 与动作表同名同档。`detail` 照 6.7 同一姿态：有哪个键显示哪个键，
 * 这里只收形状不收键集合。
 */
export const MATERIAL_ERROR_CODES = [
  'material_missing',
  'session_dir_missing',
  'invalid_setting',
  'persist_failed',
] as const;

export const materialErrorSchema = z
  .object({
    ok: z.literal(false),
    code: z.enum(MATERIAL_ERROR_CODES),
    message: z.string(),
    detail: z.record(z.unknown()),
  })
  .strict();

export type MaterialItem = z.infer<typeof materialItemSchema>;
export type MaterialsList = z.infer<typeof materialsListSchema>;
export type MaterialUploadOk = z.infer<typeof materialUploadOkSchema>;
export type MaterialDeleteOk = z.infer<typeof materialDeleteOkSchema>;
export type MaterialErrorCode = (typeof MATERIAL_ERROR_CODES)[number];
