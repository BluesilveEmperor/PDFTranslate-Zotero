import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { classifyPdfFileName, PDFType } from "../src/utils/pdfFileName.ts";

// 跨语言一致性校验所需的源码位置（plugin 与 server 在同一仓库）。
const REPO_ROOT = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../..",
);
const SERVER_PY = path.join(REPO_ROOT, "server", "server.py");
const HELPER_TS = path.join(
    REPO_ROOT,
    "plugin",
    "src",
    "modules",
    "pdf2zhHelper.ts",
);

// 新版服务端产物命名：Traned_<variant>_<stem>.pdf
const translatedCases: Array<[fileName: string, expected: PDFType]> = [
    ["Traned_single_paper.pdf", PDFType.MONO],
    ["Traned_double_paper.pdf", PDFType.DUAL],
    ["Traned_single_cut_paper.pdf", PDFType.MONO_CUT],
    ["Traned_double_cut_paper.pdf", PDFType.DUAL_CUT],
    ["Traned_compare_paper.pdf", PDFType.COMPARE],
    ["Traned_crop_compare_paper.pdf", PDFType.CROP_COMPARE],
    ["Traned_origin_cut_paper.pdf", PDFType.ORIGIN_CUT],
];

// 旧版命名：历史产物必须仍能识别。
const legacyCases: Array<[fileName: string, expected: PDFType]> = [
    ["paper mono.pdf", PDFType.MONO],
    ["paper dual.pdf", PDFType.DUAL],
    ["paper mono-cut.pdf", PDFType.MONO_CUT],
    ["paper dual-cut.pdf", PDFType.DUAL_CUT],
    ["paper crop-compare.pdf", PDFType.CROP_COMPARE],
    ["paper compare.pdf", PDFType.COMPARE],
    ["paper cut.pdf", PDFType.ORIGIN_CUT],
];

// 非产物 / 边界文件名。
const originCases: Array<[fileName: string, expected: PDFType]> = [
    ["paper.pdf", PDFType.ORIGIN],
    ["Some Paper (2024).pdf", PDFType.ORIGIN],
    ["Traned_.pdf", PDFType.ORIGIN],
    // 前缀之后没有 stem，不是合法的翻译产物命名。
    ["Traned_single_.pdf", PDFType.ORIGIN],
    ["Traned_double_cut_.pdf", PDFType.ORIGIN],
    ["", PDFType.ORIGIN],
];

test("recognizes new Traned_* translated file names", () => {
    for (const [fileName, expected] of translatedCases) {
        assert.equal(
            classifyPdfFileName(fileName),
            expected,
            `${fileName} 应判定为 ${expected}`,
        );
    }
});

test("keeps recognizing legacy translated file names", () => {
    for (const [fileName, expected] of legacyCases) {
        assert.equal(
            classifyPdfFileName(fileName),
            expected,
            `${fileName} 应判定为 ${expected}`,
        );
    }
});

test("falls back to origin for names that are not translated outputs", () => {
    for (const [fileName, expected] of originCases) {
        assert.equal(
            classifyPdfFileName(fileName),
            expected,
            `${fileName} 应判定为 ${expected}`,
        );
    }
});

test("matches longer Traned_ prefix before shorter ones", () => {
    // 若先匹配 Traned_single_ / Traned_double_，这两个文件会被误判。
    assert.equal(
        classifyPdfFileName("Traned_single_cut_课程 2024.pdf"),
        PDFType.MONO_CUT,
    );
    assert.equal(
        classifyPdfFileName("Traned_double_cut_课程 2024.pdf"),
        PDFType.DUAL_CUT,
    );
    assert.equal(
        classifyPdfFileName("Traned_crop_compare_课程 2024.pdf"),
        PDFType.CROP_COMPARE,
    );
    assert.equal(
        classifyPdfFileName("Traned_origin_cut_课程 2024.pdf"),
        PDFType.ORIGIN_CUT,
    );
});

test("is case insensitive for new names and tolerates directory paths", () => {
    assert.equal(classifyPdfFileName("traned_single_Paper.PDF"), PDFType.MONO);
    assert.equal(
        classifyPdfFileName("TRANED_DOUBLE_CUT_PAPER.pdf"),
        PDFType.DUAL_CUT,
    );
    assert.equal(
        classifyPdfFileName("translated/Traned_double_paper.pdf"),
        PDFType.DUAL,
    );
    assert.equal(
        classifyPdfFileName("C:\\tmp\\translated\\Traned_compare_paper.pdf"),
        PDFType.COMPARE,
    );
});

test("prefers new naming over legacy substrings inside the stem", () => {
    // stem 里恰好含旧命名子串时，仍应按新前缀判定。
    assert.equal(
        classifyPdfFileName("Traned_single_paper mono.pdf"),
        PDFType.MONO,
    );
    assert.equal(
        classifyPdfFileName("Traned_double_paper cut.pdf"),
        PDFType.DUAL,
    );
});

test("rejects new-looking names that are not PDF files", () => {
    assert.equal(
        classifyPdfFileName("Traned_single_paper.txt"),
        PDFType.ORIGIN,
    );
    assert.equal(
        classifyPdfFileName("Traned_double_cut_paper.docx"),
        PDFType.ORIGIN,
    );
    assert.equal(classifyPdfFileName("Traned_compare_paper"), PDFType.ORIGIN);
});

test("PDFType strings match the values returned by the server", () => {
    assert.deepEqual(PDFType, {
        MONO: "mono",
        DUAL: "dual",
        MONO_CUT: "mono-cut",
        DUAL_CUT: "dual-cut",
        CROP_COMPARE: "crop-compare",
        COMPARE: "compare",
        ORIGIN_CUT: "origin-cut",
        ORIGIN: "origin",
        UNKNOWN: "unknown",
    });
});

/** 从服务端 server.py 解析 `_TRANED_PREFIX_RULES`，用于跨语言一致性校验。 */
function parseServerPrefixRules(): Array<[string, PDFType]> {
    const source = fs.readFileSync(SERVER_PY, "utf-8");
    const block = source.split("_TRANED_PREFIX_RULES = (");
    assert.ok(block.length > 1, "server.py 中应存在 _TRANED_PREFIX_RULES");
    const body = block[1].split("\n)")[0];
    const rules: Array<[string, PDFType]> = [];
    for (const match of body.matchAll(/\('([^']+)',\s*'([^']+)'\)/g)) {
        rules.push([match[1], match[2] as PDFType]);
    }
    return rules;
}

test("server and plugin agree on every Traned_ prefix", () => {
    const serverRules = parseServerPrefixRules();
    // 服务端与插件各自维护一份规则，任一侧新增变体而另一侧未同步都会漏识别。
    assert.ok(
        serverRules.length >= 7,
        `应至少解析出 7 条服务端规则，实际 ${serverRules.length}`,
    );

    for (const [prefix, expected] of serverRules) {
        assert.equal(
            classifyPdfFileName(`${prefix}paper.pdf`),
            expected,
            `服务端前缀 ${prefix} 应被插件判定为 ${expected}`,
        );
    }

    // 大小写契约：server 的 strip/match 已按 lower() 比较，插件也必须同样
    // 宽容，否则 Windows 改写大小写后两侧判定会分叉。
    for (const [prefix, expected] of serverRules) {
        for (const variant of [prefix.toLowerCase(), prefix.toUpperCase()]) {
            assert.equal(
                classifyPdfFileName(`${variant}paper.pdf`),
                expected,
                `${variant} 是 ${prefix} 的大小写变体，应判定为 ${expected}`,
            );
        }
    }

    const covered = new Set(serverRules.map(([, type]) => type));
    for (const expected of [
        PDFType.MONO,
        PDFType.DUAL,
        PDFType.MONO_CUT,
        PDFType.DUAL_CUT,
        PDFType.COMPARE,
        PDFType.CROP_COMPARE,
        PDFType.ORIGIN_CUT,
    ]) {
        assert.ok(covered.has(expected), `插件类型 ${expected} 缺少服务端前缀`);
    }
});

test("pdf2zhHelper.getFileType delegates to the pure classifier", () => {
    const source = fs.readFileSync(HELPER_TS, "utf-8");
    const afterSignature = source.split(
        "static getFileType(fileName: string): string {",
    );
    assert.ok(
        afterSignature.length > 1,
        "pdf2zhHelper.ts 中应存在 getFileType",
    );
    const body = afterSignature[1].split("\n    }")[0];

    assert.match(
        body,
        /classifyPdfFileName\s*\(/,
        "getFileType 应委托纯函数判定",
    );
    assert.doesNotMatch(
        body,
        /indexOf\s*\(/,
        "getFileType 不应再内联旧的 indexOf 子串判定",
    );
});
