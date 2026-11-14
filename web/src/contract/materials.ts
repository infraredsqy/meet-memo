import { z } from 'zod';
import { isoStringSchema, pathStringSchema } from './snapshot';

/**
 * 会议资料的四条端点。
 *
 * 资料层**独立于四态快照**：不提交、不涨 `revision`、不广播——信封里的 `revision`
 * 只是当前版本号。
 */

export const materialItemSchema = z
  .object({
    name: z.string(),
    size_bytes: z.number().int(),
    added_at: isoStringSchema,
  })
  .strict();

/** 清单按名字排序。 */
export const materialsListSchema = z
  .object({
    dir: pathStringSchema,
    materials_dir: pathStringSchema,
    items: z.array(materialItemSchema),
  })
  .strict();

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
 * `material_missing` **不在**动作那九个 `code` 表里（资料四条不走 `/api/actions`）。
 * `detail` 有哪个键显示哪个键，这里只收形状不收键集合。
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
