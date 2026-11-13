import { z } from 'zod';

/**
 * 设备清单端点（`docs/contract.md` 4.10，设置抽屉节点）。
 *
 * `GET /api/devices`：两路各自的候选端点表。**只读、纯枚举、不开设备**
 * （`probe()` 会短暂试开，清单不走它）；不涨 `revision`、不广播，任何状态下都 200。
 * 界面只在设置抽屉打开时取它，取到的就是服务给的原文 —— 不派生、不缓存、不落本地存储。
 */

/** 端点表里的一条：`DeviceEndpoint` 原样六格（render 端点没有 host API，`host_api` 为空串）。 */
export const deviceEndpointSchema = z
  .object({
    name: z.string(),
    host_api: z.string(),
    role: z.enum(['input', 'render']),
    index: z.number().int().nullable(),
    channels: z.number().int(),
    is_default: z.boolean(),
  })
  .strict();

/** `GET /api/devices` 的应答体：两路各一份，键就是两路的 id。 */
export const devicesResponseSchema = z
  .object({
    mic: z.array(deviceEndpointSchema),
    loopback: z.array(deviceEndpointSchema),
  })
  .strict();

export type DeviceEndpoint = z.infer<typeof deviceEndpointSchema>;
export type DevicesResponse = z.infer<typeof devicesResponseSchema>;
