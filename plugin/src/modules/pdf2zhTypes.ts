// PDFType 的常量定义放在 utils/pdfFileName.ts：文件名判定函数需要与类型
// 常量处于同一个零依赖模块，才能被 node:test 直接加载（Node 的类型剥离
// 不支持 enum）。此处重导出，保持既有 import 路径不变。
export { PDFType } from "../utils/pdfFileName";

export interface ServerConfig {
    serverUrl: string;
    threadNum: string;
    qps: string;
    poolSize: string;
    engine: string;
    service: string;
    next_service: string;

    skipLastPages: string;
    sourceLang: string;
    targetLang: string;
    // generate
    mono: string;
    mono_cut: string;
    dual: string;
    dual_cut: string;
    crop_compare: string;
    compare: string;

    // pdf1x专用配置
    babeldoc: string;
    skipSubsetFonts: string;
    fontFile: string;

    // pdf2x专用配置
    ocr: string;
    autoOcr: string;
    transFirst: string;
    noWatermark: string;
    fontFamily: string;
    dualMode: string;
    saveGlossary: string;
    disableGlossary: string;
    noDual: string;
    noMono: string;
    skipClean: string;
    disableRichTextTranslate: string;
    enhanceCompatibility: string;
    translateTableText: string;
    onlyIncludeTranslatedPage: string;
}

export interface PDFOperationOptions {
    rename: boolean;
    openAfterProcess: boolean;
}

export type JobProgressPhase =
    | "submitting"
    | "accepted"
    | "running"
    | "importing"
    | "file-done"
    | "file-failed";

export interface JobProgressUpdate {
    phase: JobProgressPhase;
    fileName: string;
    endpoint: string;
    current: number;
    total: number;
    percent?: number;
    detail?: string;
    error?: string;
}
