/**
 * PDF 产物文件名 -> 附件类型判定。
 *
 * 本模块刻意保持零运行时依赖：单元测试用 Node 的 TypeScript 类型剥离
 * 直接加载它，因此不能引用 Zotero 全局对象，也不能使用 enum / namespace
 * 等需要类型转换（transform）的语法。
 */

export const PDFType = {
    MONO: "mono",
    DUAL: "dual",
    MONO_CUT: "mono-cut",
    DUAL_CUT: "dual-cut",
    CROP_COMPARE: "crop-compare",
    COMPARE: "compare",
    ORIGIN_CUT: "origin-cut",
    ORIGIN: "origin",
    UNKNOWN: "unknown",
} as const;

export type PDFType = (typeof PDFType)[keyof typeof PDFType];

/**
 * 新版服务端产物命名：`Traned_<variant>_<stem>.pdf`。
 *
 * 顺序敏感：带 `_cut_` / `_compare_` 的长前缀必须先匹配，否则
 * `Traned_single_cut_x.pdf` 会被短前缀 `Traned_single_` 误判成单语文件。
 */
const TRANSLATED_NAME_RULES: ReadonlyArray<readonly [string, PDFType]> = [
    ["traned_single_cut_", PDFType.MONO_CUT],
    ["traned_double_cut_", PDFType.DUAL_CUT],
    ["traned_crop_compare_", PDFType.CROP_COMPARE],
    ["traned_origin_cut_", PDFType.ORIGIN_CUT],
    ["traned_compare_", PDFType.COMPARE],
    ["traned_single_", PDFType.MONO],
    ["traned_double_", PDFType.DUAL],
];

/**
 * 旧版命名兼容规则（历史文件仍需识别）。
 *
 * `cut.pdf` 语义最弱，必须放在最后兜底，否则会抢走 `mono-cut.pdf`、
 * `dual-cut.pdf`、`crop-compare.pdf` 等更具体的命名。
 */
const LEGACY_NAME_MARKERS: ReadonlyArray<readonly [string, PDFType]> = [
    ["mono.pdf", PDFType.MONO],
    ["dual.pdf", PDFType.DUAL],
    ["mono-cut.pdf", PDFType.MONO_CUT],
    ["dual-cut.pdf", PDFType.DUAL_CUT],
    ["crop-compare.pdf", PDFType.CROP_COMPARE],
    ["compare.pdf", PDFType.COMPARE],
    ["cut.pdf", PDFType.ORIGIN_CUT],
];

/**
 * 去掉目录部分，只保留文件名。
 *
 * server 的 fileList 可能返回相对路径（如 `translated/Traned_single_x.pdf`），
 * 而本地路径在 Windows 上可能是反斜杠分隔，两种都兼容。
 */
function toLeafName(fileName: string): string {
    return String(fileName || "").replace(/^.*[/\\]/, "");
}

/**
 * 匹配新版 `Traned_` 前缀命名。
 *
 * 一旦命中某个前缀就不再尝试更短的前缀（避免 `Traned_double_cut_.pdf`
 * 回落到 DUAL）；前缀之后必须还有非空 stem 且以 `.pdf` 结尾，
 * 因此 `Traned_single_.pdf` 这类残缺命名不算翻译产物。
 */
function matchTranslatedName(lowerLeaf: string): PDFType | null {
    for (const [prefix, type] of TRANSLATED_NAME_RULES) {
        if (!lowerLeaf.startsWith(prefix)) {
            continue;
        }
        if (!lowerLeaf.endsWith(".pdf")) {
            return null;
        }
        const stem = lowerLeaf.slice(prefix.length, -".pdf".length);
        return stem.length > 0 ? type : null;
    }
    return null;
}

/**
 * 根据 PDF 文件名判定附件类型。
 *
 * 先按新版 `Traned_` 前缀命名判定（大小写不敏感），未命中时回退到旧版
 * 子串命名（大小写敏感，保持与历史行为一致），两者都不匹配则视为原文。
 */
export function classifyPdfFileName(fileName: string): PDFType {
    const leaf = toLeafName(fileName);
    if (!leaf) {
        return PDFType.ORIGIN;
    }

    const translatedType = matchTranslatedName(leaf.toLowerCase());
    if (translatedType) {
        return translatedType;
    }

    const legacyRule = LEGACY_NAME_MARKERS.find(([marker]) =>
        leaf.includes(marker),
    );
    return legacyRule ? legacyRule[1] : PDFType.ORIGIN;
}
