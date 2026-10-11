import { describe, expect, it } from 'vitest';
import {
  EDITABLE_SELECTOR,
  MATERIALS_MAX_BYTES,
  dragFiles,
  eventTargetInEditable,
  filesOverLimit,
  formatBytes,
  isImageMaterial,
  materialKindLabel,
  materialKindOf,
  materialTimeLabel,
  materialsFailureText,
  pasteImageFiles,
  textMaterialName,
} from '../src/materials/model';
import {
  MaterialsForbiddenError,
  MaterialsHttpError,
  MaterialsUnavailableError,
} from '../src/api/materials';

/**
 * 资料区那份纯逻辑的用例（160d1a78）。
 *
 * 完成判定点名的六样里，**粘贴收图**这一条由这里承住：node 环境没有 `document`、
 * 没有真 `ClipboardEvent`（仓库不引 jsdom），所以粘贴的**筛子**（只有图片项才收、
 * 纯文本不收）与**让位判据**（焦点在输入框 / 文本域 / 可编辑元素里不抢）按结构式
 * 假件逐档验 —— 组件那一侧的接线只有「把 `event.clipboardData.items` 递给筛子、
 * 判据说了算就 `preventDefault`」五行，由挂载用例顺带盖住。
 */

describe('大小口径与格式化', () => {
  it('MATERIALS_MAX_BYTES 就是 100 * 1024 * 1024 字节；超线的挑得出来，批里没有就空', () => {
    expect(MATERIALS_MAX_BYTES).toBe(104857600);
    const small = { name: '小.pdf', size: MATERIALS_MAX_BYTES };
    const big = { name: '大.mov', size: MATERIALS_MAX_BYTES + 1 };
    expect(filesOverLimit([small, big])).toEqual([big]);
    expect(filesOverLimit([small])).toEqual([]);
  });

  it('字节数的读法：B 整数、KB 起一位小数、满 100 进到整数位', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1023)).toBe('1023 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(15)).toBe('15 B');
    expect(formatBytes(32)).toBe('32 B');
    expect(formatBytes(100 * 1024 * 1024)).toBe('100 MB');
  });
});

describe('类型判读（缩略图 vs 类型标签）', () => {
  it('图片扩展名走缩略图那半边，大小写不敏感', () => {
    for (const name of ['a.png', 'b.JPG', 'c.jpeg', 'd.gif', 'e.webp', '带空格 f.SVG']) {
      expect(isImageMaterial(name), name).toBe(true);
      expect(materialKindOf(name), name).toBe('image');
    }
  });

  it('非图片各归各档；没扩展名、点在头上、认不出的都归「文件」', () => {
    expect(materialKindLabel('会议录音.mp3')).toBe('音频');
    expect(materialKindLabel('录像.mp4')).toBe('视频');
    expect(materialKindLabel('纪要.md')).toBe('文本');
    expect(materialKindLabel('noext')).toBe('文件');
    expect(materialKindLabel('.hidden')).toBe('文件');
    expect(materialKindLabel('readme.')).toBe('文件');
    expect(materialKindLabel('报告.xyz')).toBe('文件');
  });
});

describe('提交文字的落盘名', () => {
  it('标题填了就用它（交原始名，清洗归服务），固定 .md 收尾', () => {
    expect(textMaterialName('季度复盘', new Date(0))).toBe('季度复盘.md');
    expect(textMaterialName('  两侧空白进去  ', new Date(0))).toBe('两侧空白进去.md');
  });

  it('没填标题用本地时间戳 YYYYMMDD-HHMMSS.md；空串与纯空白同档', () => {
    const at = new Date(2026, 9, 11, 8, 5, 9); // 本地 2026-10-11 08:05:09
    const name = textMaterialName('', at);
    expect(name).toBe('20261011-080509.md');
    expect(textMaterialName('   ', at)).toBe(name);
    expect(textMaterialName('标题', at)).toBe('标题.md');
  });
});

describe('粘贴收图的筛子（只有图片项才收，纯文本不收）', () => {
  /** 结构式假件：`DataTransferItem` 长这样。 */
  function item(kind: string, type: string, file: File | null = null) {
    return { kind, type, getAsFile: () => file };
  }

  const png = new File(['bytes'], '贴图.png', { type: 'image/png' });

  it('图片文件项照收，顺序保持剪贴板给的次序', () => {
    const second = new File(['x'], '第二张.gif', { type: 'image/gif' });
    expect(pasteImageFiles([item('file', 'image/png', png), item('file', 'image/gif', second)])).toEqual([
      png,
      second,
    ]);
  });

  it('纯文本、别的 kind、取不出 File 的图片项都不收', () => {
    expect(pasteImageFiles([item('string', 'text/plain')])).toEqual([]);
    expect(pasteImageFiles([item('string', 'text/html')])).toEqual([]);
    expect(pasteImageFiles([item('file', 'application/pdf', new File(['x'], 'a.pdf'))])).toEqual([]);
    expect(pasteImageFiles([item('file', 'image/png', null)])).toEqual([]);
    expect(pasteImageFiles([])).toEqual([]);
    expect(pasteImageFiles(null)).toEqual([]);
    expect(pasteImageFiles(undefined)).toEqual([]);
  });
});

describe('拖拽的筛子与粘贴的让位判据', () => {
  it('dragFiles 收 dataTransfer.files，没有就空批', () => {
    const file = new File(['x'], '拖进来的.md');
    expect(dragFiles({ files: [file] as unknown as FileList })).toEqual([file]);
    expect(dragFiles({ files: null })).toEqual([]);
    expect(dragFiles(null)).toEqual([]);
    expect(dragFiles(undefined)).toEqual([]);
  });

  it('eventTargetInEditable：输入框 / 文本域 / contenteditable（含祖先）都让位，别的目标不让位', () => {
    const editable = {
      closest: (selector: string) => (selector === EDITABLE_SELECTOR ? { tag: 'input' } : null),
    };
    const plain = { closest: () => null };
    expect(eventTargetInEditable(editable)).toBe(true);
    expect(eventTargetInEditable(plain)).toBe(false);
    // document / 窗口 / null 都没有 closest：按不可编辑算（不抢）
    expect(eventTargetInEditable({})).toBe(false);
    expect(eventTargetInEditable(null)).toBe(false);
    expect(eventTargetInEditable(undefined)).toBe(false);
    expect(eventTargetInEditable('input')).toBe(false);
  });
});

describe('逐文件失败的读法（网络 / 403 头哨 / 服务错误，分档不猜）', () => {
  it('网络层失败：点明「没有放上去」并带上原话', () => {
    const text = materialsFailureText(
      new MaterialsUnavailableError('资料上传', new Error('Failed to fetch')),
    );
    expect(text).toContain('没有放上去');
    expect(text).toContain('Failed to fetch');
  });

  it('403 头哨：点明被拦下、没有落盘（这一档没有失败信封）', () => {
    const text = materialsFailureText(new MaterialsForbiddenError(403, 'missing header'));
    expect(text).toContain('403');
    expect(text).toContain('拦下');
    expect(text).toContain('没有落盘');
  });

  it('HTTP 失败信封：状态 + code + 服务回的那句原话；读不出信封就只有状态', () => {
    const missing = materialsFailureText(
      new MaterialsHttpError(404, 'material_missing', '资料里没有这一份：报告.pdf', '{}'),
    );
    expect(missing).toContain('404');
    expect(missing).toContain('material_missing');
    expect(missing).toContain('资料里没有这一份：报告.pdf');

    const bare = materialsFailureText(new MaterialsHttpError(500, null, null, ''));
    expect(bare).toContain('500');
  });

  it('其余错误原话如实（不编码）', () => {
    expect(materialsFailureText(new Error('盘满了'))).toBe('盘满了');
    expect(materialsFailureText('一串话')).toBe('一串话');
  });
});

describe('贴上时刻的读法', () => {
  it('ISO 时间戳读成本地 YYYY-MM-DD HH:MM；读不出的原话摆上去', () => {
    const label = materialTimeLabel('2026-09-21T21:03:00+08:00');
    expect(label).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(materialTimeLabel('不是时刻')).toBe('不是时刻');
  });
});
