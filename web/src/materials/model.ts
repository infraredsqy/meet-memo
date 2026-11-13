import {
  MaterialsForbiddenError,
  MaterialsHttpError,
  MaterialsUnavailableError,
} from '../api/materials';

/**
 * 会议资料区那份纯逻辑（节点）：类型判读、大小口径、提交文字的落盘名、
 * 粘贴与拖拽那两把筛子、逐文件失败的那几句人话。组件里不夹这些判断 —— 手法照
 * `live/alarm.ts` 与 `home/names.ts` 的先例：纯函数、逐档可验。
 */

/** 单文件的大小口径：超过它要弹一句「确认要放进资料吗」，确认后照传（spec 的 100M）。 */
export const MATERIALS_MAX_BYTES = 100 * 1024 * 1024;

/** 资料的类型档（按扩展名判；图片走缩略图，其余走类型标签）。 */
export type MaterialKind = 'image' | 'audio' | 'video' | 'text' | 'other';

const KIND_BY_EXT: Readonly<Record<string, MaterialKind>> = {
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  webp: 'image',
  bmp: 'image',
  svg: 'image',
  avif: 'image',
  mp3: 'audio',
  wav: 'audio',
  flac: 'audio',
  m4a: 'audio',
  ogg: 'audio',
  aac: 'audio',
  pcm: 'audio',
  mp4: 'video',
  mov: 'video',
  mkv: 'video',
  webm: 'video',
  avi: 'video',
  md: 'text',
  txt: 'text',
  csv: 'text',
  json: 'text',
  log: 'text',
};

const KIND_LABELS: Readonly<Record<MaterialKind, string>> = {
  image: '图片',
  audio: '音频',
  video: '视频',
  text: '文本',
  other: '文件',
};

/** 扩展名 → 类型档；没有扩展名或认不出都归「文件」。大小写不敏感。 */
export function materialKindOf(name: string): MaterialKind {
  const dot = name.lastIndexOf('.');
  if (dot <= 0 || dot === name.length - 1) return 'other';
  return KIND_BY_EXT[name.slice(dot + 1).toLowerCase()] ?? 'other';
}

/** 图片类资料：给缩略图 + 点开看原图（`img src` 直接指到取原文件端点）。 */
export function isImageMaterial(name: string): boolean {
  return materialKindOf(name) === 'image';
}

/** 类型标签那一格的字（非图片行用它，不开缩略图）。 */
export function materialKindLabel(name: string): string {
  return KIND_LABELS[materialKindOf(name)];
}

/** 字节数的读法：B 整数、KB 起一位小数（满 100 进到整数位）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** 单份是否超过大小口径（选择器文件模式手上是字节数，不拿 `File`）。 */
export function sizeOverLimit(sizeBytes: number): boolean {
  return sizeBytes > MATERIALS_MAX_BYTES;
}

/** 一批文件里超过大小口径的那几份（确认框点名用；File 按结构兼容）。 */
export function filesOverLimit(
  files: readonly { name: string; size: number }[],
): { name: string; size: number }[] {
  return files.filter((file) => sizeOverLimit(file.size));
}

/**
 * 提交文字的落盘名：标题填了就用它（交原始名，清洗归服务），没填用本地时间戳
 * `YYYYMMDD-HHMMSS.md`。扩展名固定 `.md`。
 */
export function textMaterialName(title: string, at: Date): string {
  const trimmed = title.trim();
  if (trimmed !== '') return `${trimmed}.md`;
  const pad = (value: number): string => String(value).padStart(2, '0');
  return (
    `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}` +
    `-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}.md`
);
}

/** 剪贴板里的一项（结构式：`DataTransferItemList` 的成员长这样）。 */
export interface ClipboardFileItem {
  kind: string;
  type: string;
  getAsFile(): File | null;
}

/**
 * 剪贴板里能收的图片文件：**只有图片项才收**，纯文本不收（spec 口径）。
 * 顺序保持剪贴板给的次序；取不出 `File` 的项（Rare，但可能）跳过。
 */
export function pasteImageFiles(
  items: readonly ClipboardFileItem[] | null | undefined,
): File[] {
  if (items == null) return [];
  const files: File[] = [];
  for (const item of items) {
    if (item.kind !== 'file') continue;
    if (!item.type.startsWith('image/')) continue;
    const file = item.getAsFile();
    if (file !== null) files.push(file);
  }
  return files;
}

/** 拖进来的那批文件（`dataTransfer.files`；没有就空批）。 */
export function dragFiles(
  dt: { files?: FileList | null } | null | undefined,
): File[] {
  return Array.from(dt?.files ?? []);
}

/** 粘贴该不该让位的判据：焦点落在输入框 / 文本域 / 可编辑元素里时不抢。 */
export const EDITABLE_SELECTOR =
  'input, textarea, select, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]';

/**
 * 事件目标是否落在可编辑元素里（`closest` 沿祖先找，覆盖「contenteditable 容器里的
 * 光标」那一档）。鸭子读法：node 挂载用例没有 DOM，假目标给不出 `closest` 就算不可编辑。
 */
export function eventTargetInEditable(target: unknown): boolean {
  if (target === null || typeof target !== 'object') return false;
  const closest = (target as { closest?: unknown }).closest;
  if (typeof closest !== 'function') return false;
  const hit = (closest as (selector: string) => unknown).call(target, EDITABLE_SELECTOR);
  return hit !== null && hit !== undefined;
}

/** 一条上传任务的读数（进行中 / 完成 / 失败三档；失败写明原因）。 */
export interface UploadTask {
  key: string;
  name: string;
  size: number;
  status: 'uploading' | 'done' | 'failed';
  /** 失败档那句人话（网络 / 403 头哨 / 404 目录 / 其他服务错误）。 */
  note: string | null;
}

/**
 * 一次资料失败的读法：按错误类分档，不按 `message` 做字符串匹配（契约 6.7 的同一姿态）。
 *
 * - 网络层（服务没应答）→ 点明「没传上去」；
 * - 403（头哨被拦）→ 点明「被拦下 + 没有落盘」——这一档没有失败信封；
 * - HTTP 失败信封 → HTTP 状态 + `code` + 服务回的那句原话；
 * - 其余 → 原话如实（不编码）。
 */
export function materialsFailureText(error: unknown): string {
  if (error instanceof MaterialsUnavailableError) {
    return `网络不通，东西没有放上去：${error.message}`;
  }
  if (error instanceof MaterialsForbiddenError) {
    return `请求被服务拦下（HTTP ${error.status}，缺少必需请求头），没有落盘。`;
  }
  if (error instanceof MaterialsHttpError) {
    const base = error.note ?? `服务没有说原因`;
    return error.code !== null
      ? `HTTP ${error.status} · ${error.code}：${base}`
      : `HTTP ${error.status}：${base}`;
  }
  return error instanceof Error ? error.message : String(error);
}

/** 贴上时刻的读法：本地时区的 `YYYY-MM-DD HH:MM`。读不出来把原话摆上去。 */
export function materialTimeLabel(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (value: number): string => String(value).padStart(2, '0');
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
    `${pad(at.getHours())}:${pad(at.getMinutes())}`
);
}
