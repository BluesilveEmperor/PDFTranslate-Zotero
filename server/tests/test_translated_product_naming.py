"""产物命名与 staging 清理的单元测试。

对齐 PDFMathTranslate main 分支的产物约定：
- 最终产物命名 ``Traned_<variant>_<stem>.pdf``；
- 中间产物（上传源副本、内核输出、裁剪结果）写入
  ``translated/.staging/<task_id>/``，成功后才原子移到 ``translated`` 根；
- 失败 / 取消同样要清理 staging，源副本不得残留在输出根。
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server as server_module
from server import (
    match_traned_product,
    STAGING_DIRNAME,
    PDFTranslator,
    cleanup_staging,
    move_products_to_output,
    semantic_stem,
    strip_traned_prefix,
    task_staging_dir,
    translated_filename,
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class NamingTests(unittest.TestCase):
    def test_strip_longest_prefix_first(self):
        cases = {
            "Traned_crop_compare_paper": "paper",
            "Traned_double_cut_paper": "paper",
            "Traned_single_cut_paper": "paper",
            "Traned_origin_cut_paper": "paper",
            "Traned_compare_paper": "paper",
            "Traned_double_paper": "paper",
            "Traned_single_paper": "paper",
        }
        for stem, expected in cases.items():
            with self.subTest(stem=stem):
                self.assertEqual(strip_traned_prefix(stem), expected)

    def test_strip_is_a_noop_for_plain_or_empty_stems(self):
        for stem in ("paper", "Traned_", "Traned_single_", "Traned_unknown_paper"):
            with self.subTest(stem=stem):
                self.assertEqual(strip_traned_prefix(stem), stem)

    def test_strip_is_case_insensitive_for_new_prefixes(self):
        # 与插件 classifyPdfFileName 一致：Windows 上文件名大小写可能被改写。
        cases = {
            "traned_single_paper": "paper",
            "TRANED_DOUBLE_PAPER": "PAPER",
            "Traned_Double_Cut_paper": "paper",
            "traned_crop_compare_paper": "paper",
            "TRANED_ORIGIN_CUT_paper": "paper",
            "traned_compare_paper": "paper",
        }
        for stem, expected in cases.items():
            with self.subTest(stem=stem):
                self.assertEqual(strip_traned_prefix(stem), expected)

    def test_strip_keeps_legacy_names_case_sensitive(self):
        # 旧命名兼容保持大小写敏感（与 TS legacy 分支一致）。
        self.assertEqual(strip_traned_prefix("PAPER-MONO"), "PAPER-MONO")

    def test_semantic_stem_drops_extension_and_prefix(self):
        self.assertEqual(
            semantic_stem(os.path.join("translated", "Traned_double_paper.pdf")),
            "paper",
        )
        self.assertEqual(semantic_stem("Traned_single_cut_课程 2024.pdf"), "课程 2024")
        self.assertEqual(semantic_stem("paper.PDF"), "paper")
        self.assertEqual(semantic_stem("no-extension"), "no-extension")
        self.assertEqual(semantic_stem("traned_single_paper.pdf"), "paper")
        self.assertEqual(semantic_stem("TRANED_DOUBLE_PAPER.PDF"), "PAPER")

    def test_semantic_stem_drops_layout_and_intermediate_markers(self):
        # pdf2zh_next 的内核产物自带布局 / 类型标记；不清洗会让最终文件名
        # 变成 Traned_compare_paper.LR_dual.pdf 并破坏 stem 过滤。
        cases = {
            "paper.LR_dual.pdf": "paper",
            "paper.TB_dual.pdf": "paper",
            "paper.dual.pdf": "paper",
            "paper.mono.pdf": "paper",
            "paper.dual-cut.pdf": "paper",
            "paper.mono-cut.pdf": "paper",
            "paper.crop-compare.pdf": "paper",
            "paper.cut.pdf": "paper",
            "paper.compare.pdf": "paper",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(semantic_stem(name), expected)

    def test_semantic_stem_keeps_user_versions_and_pure_markers(self):
        self.assertEqual(semantic_stem("paper.v2.final.pdf"), "paper.v2.final")
        self.assertEqual(semantic_stem(".dual.pdf"), ".dual")
        self.assertEqual(semantic_stem("Traned_double_paper.LR_dual.pdf"), "paper")

    def test_translated_filename_covers_every_product_type(self):
        cases = {
            "mono": "Traned_single_paper.pdf",
            "dual": "Traned_double_paper.pdf",
            "mono-cut": "Traned_single_cut_paper.pdf",
            "dual-cut": "Traned_double_cut_paper.pdf",
            "compare": "Traned_compare_paper.pdf",
            "crop-compare": "Traned_crop_compare_paper.pdf",
            "origin-cut": "Traned_origin_cut_paper.pdf",
        }
        for outtype, expected in cases.items():
            with self.subTest(outtype=outtype):
                self.assertEqual(translated_filename("paper", outtype), expected)

    def test_translated_filename_never_stacks_prefixes(self):
        # 二次加工（cut / compare）的输入本身就是 Traned_* 产物。
        self.assertEqual(
            translated_filename("Traned_double_paper", "mono-cut"),
            "Traned_single_cut_paper.pdf",
        )
        self.assertEqual(
            translated_filename("Traned_single_cut_paper", "compare"),
            "Traned_compare_paper.pdf",
        )

    def test_unknown_product_type_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            translated_filename("paper", "mystery")
        self.assertIn("mystery", str(ctx.exception))


class GetFileTypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )

    def test_new_prefixes_map_to_product_types(self):
        cases = {
            "Traned_single_paper.pdf": "mono",
            "Traned_double_paper.pdf": "dual",
            "Traned_single_cut_paper.pdf": "mono-cut",
            "Traned_double_cut_paper.pdf": "dual-cut",
            "Traned_compare_paper.pdf": "compare",
            "Traned_crop_compare_paper.pdf": "crop-compare",
            "Traned_origin_cut_paper.pdf": "origin-cut",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.translator.get_filetype(name), expected)

    def test_long_prefixes_win_over_short_prefixes(self):
        # 若先判 mono / dual，cut / compare 变体会被误判。
        self.assertEqual(
            self.translator.get_filetype("Traned_double_cut_paper.pdf"), "dual-cut"
        )
        self.assertEqual(
            self.translator.get_filetype("Traned_single_cut_paper.pdf"), "mono-cut"
        )
        self.assertEqual(
            self.translator.get_filetype("Traned_crop_compare_paper.pdf"),
            "crop-compare",
        )
        self.assertEqual(
            self.translator.get_filetype("Traned_origin_cut_paper.pdf"), "origin-cut"
        )

    def test_works_with_absolute_paths(self):
        self.assertEqual(
            self.translator.get_filetype(
                os.path.join("translated", "Traned_single_cut_paper.pdf")
            ),
            "mono-cut",
        )

    def test_legacy_names_still_recognised(self):
        cases = {
            "paper mono.pdf": "mono",
            "paper dual.pdf": "dual",
            "paper mono-cut.pdf": "mono-cut",
            "paper dual-cut.pdf": "dual-cut",
            "paper crop-compare.pdf": "crop-compare",
            "paper compare.pdf": "compare",
            "paper cut.pdf": "origin-cut",
            "paper.LR_dual.pdf": "dual",
            "paper.TB_dual.pdf": "dual",
            "paper.pdf": "origin",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.translator.get_filetype(name), expected)

    def test_new_prefix_without_stem_is_not_a_product(self):
        # 前缀后必须有 stem，否则视为原文，避免误判残缺命名。
        self.assertEqual(self.translator.get_filetype("Traned_single_.pdf"), "origin")

    def test_new_prefixes_are_case_insensitive(self):
        # Windows 可能把文件名改成全小写/全大写，插件已按小写判定。
        cases = {
            "traned_single_x.pdf": "mono",
            "traned_double_x.pdf": "dual",
            "traned_single_cut_x.pdf": "mono-cut",
            "traned_double_cut_x.pdf": "dual-cut",
            "traned_compare_x.pdf": "compare",
            "traned_crop_compare_x.pdf": "crop-compare",
            "traned_origin_cut_x.pdf": "origin-cut",
            "TRANED_DOUBLE_X.PDF": "dual",
            "TRANED_DOUBLE_CUT_X.PDF": "dual-cut",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.translator.get_filetype(name), expected)

    def test_legacy_markers_stay_case_sensitive(self):
        # 旧命名兼容分支必须保持大小写敏感，避免扩大历史识别范围。
        self.assertEqual(self.translator.get_filetype("PAPER-MONO.PDF"), "origin")
        self.assertEqual(self.translator.get_filetype("PAPER-DUAL.PDF"), "origin")


class MatchTranedProductTests(unittest.TestCase):
    def test_returns_product_type_for_complete_names(self):
        cases = {
            "Traned_single_paper.pdf": "mono",
            "Traned_double_paper.pdf": "dual",
            "Traned_single_cut_paper.pdf": "mono-cut",
            "Traned_double_cut_paper.pdf": "dual-cut",
            "Traned_compare_paper.pdf": "compare",
            "Traned_crop_compare_paper.pdf": "crop-compare",
            "Traned_origin_cut_paper.pdf": "origin-cut",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(match_traned_product(name), expected)

    def test_matches_new_prefixes_case_insensitively(self):
        cases = {
            "traned_single_x.pdf": "mono",
            "TRANED_DOUBLE_X.PDF": "dual",
            "traned_double_cut_x.PDF": "dual-cut",
            "Traned_Crop_Compare_x.Pdf": "crop-compare",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(match_traned_product(name), expected)

    def test_rejects_incomplete_legacy_and_non_pdf_names(self):
        for name in (
            "traned_single_.pdf",
            "TRANED_DOUBLE_CUT_.PDF",
            "paper-mono.pdf",
            "PAPER-DUAL.PDF",
            "Traned_single_paper.txt",
        ):
            with self.subTest(name=name):
                self.assertIsNone(match_traned_product(name))

    def test_rejects_incomplete_or_non_pdf_names(self):
        for name in (
            "Traned_single_.pdf",
            "Traned_double_cut_.pdf",
            "Traned_single_paper.txt",
            "paper.pdf",
            "",
        ):
            with self.subTest(name=name):
                self.assertIsNone(match_traned_product(name))


class CrossLanguageNamingContractTests(unittest.TestCase):
    """服务端前缀表必须与插件 ``pdfFileName.ts`` 保持一致。

    两侧各有一份命名规则（Python 生成产物名，TS 回传附件时判定类型）。
    任何一侧新增变体而另一侧未同步，Zotero 就会把新产物识别成原文。
    """

    @classmethod
    def setUpClass(cls) -> None:
        plugin_file = (
            Path(__file__).resolve().parents[2]
            / "plugin"
            / "src"
            / "utils"
            / "pdfFileName.ts"
        )
        cls.plugin_rules = cls._parse_plugin_rules(plugin_file)

    @staticmethod
    def _parse_plugin_rules(path: Path):
        import re

        text = path.read_text(encoding="utf-8")
        block = text.split("const TRANSLATED_NAME_RULES", 1)[1].split("];", 1)[0]
        rules = re.findall(
            r'\[\s*"([^"]+)"\s*,\s*PDFType\.([A-Z_]+)\s*\]', block
        )
        mapping = {
            "MONO": "mono",
            "DUAL": "dual",
            "MONO_CUT": "mono-cut",
            "DUAL_CUT": "dual-cut",
            "CROP_COMPARE": "crop-compare",
            "COMPARE": "compare",
            "ORIGIN_CUT": "origin-cut",
        }
        return [(prefix.replace("traned_", "Traned_"), mapping[type_name]) for prefix, type_name in rules]

    def test_plugin_and_server_cover_the_same_prefixes(self):
        self.assertEqual(dict(self.plugin_rules), dict(server_module._TRANED_PREFIX_RULES))

    def test_longer_prefixes_are_matched_before_shorter_ones_on_both_sides(self):
        for label, rules in (
            ("plugin", self.plugin_rules),
            ("server", server_module._TRANED_PREFIX_RULES),
        ):
            for longer, _ in rules:
                for shorter, _ in rules:
                    if longer == shorter or not longer.startswith(shorter):
                        continue
                    with self.subTest(side=label, longer=longer, shorter=shorter):
                        self.assertLess(
                            [prefix for prefix, _ in rules].index(longer),
                            [prefix for prefix, _ in rules].index(shorter),
                            f"{label}: {longer} 必须先于 {shorter} 判定",
                        )

    def test_generated_names_round_trip_through_the_classifier(self):
        for outtype, prefix in server_module._PRODUCT_PREFIXES.items():
            with self.subTest(outtype=outtype):
                name = translated_filename("课程 2024", outtype)
                self.assertTrue(name.startswith(prefix))
                self.assertEqual(match_traned_product(name), outtype)

    def test_generated_names_round_trip_case_insensitively(self):
        # 插件用 toLowerCase() 判定；server 必须接受同样的小写/大写形式。
        for outtype in server_module._PRODUCT_PREFIXES:
            with self.subTest(outtype=outtype):
                name = translated_filename("paper", outtype)
                self.assertEqual(match_traned_product(name.lower()), outtype)
                self.assertEqual(match_traned_product(name.upper()), outtype)


class IntermediateFilenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )

    def test_intermediate_stays_inside_staging_and_drops_prefix(self):
        path = self.translator.get_filename_after_process(
            os.path.join("translated", ".staging", "t1", "Traned_double_paper.pdf"),
            "mono-cut",
            "pdf2zh_next",
        )

        self.assertEqual(
            path,
            os.path.join("translated", ".staging", "t1", "paper.mono-cut.pdf"),
        )

    def test_explicit_stem_is_used_as_is(self):
        path = self.translator.get_filename_after_process(
            os.path.join("staging", "paper.pdf"), "compare", None, stem="课程 2024"
        )

        self.assertEqual(
            path, os.path.join("staging", "课程 2024.compare.pdf")
        )

    def test_origin_cut_uses_cut_suffix(self):
        path = self.translator.get_filename_after_process(
            os.path.join("staging", "paper.pdf"), "origin-cut"
        )

        self.assertEqual(path, os.path.join("staging", "paper.cut.pdf"))

    def test_never_returns_the_input_path(self):
        # 旧实现按 "{intype}.pdf" 做替换，遇到 Traned_* 输入会直接返回自身。
        input_path = os.path.join("staging", "Traned_single_paper.pdf")
        for outtype in ("mono-cut", "compare", "crop-compare", "dual-cut"):
            with self.subTest(outtype=outtype):
                output = self.translator.get_filename_after_process(
                    input_path, outtype
                )
                self.assertNotEqual(os.path.abspath(output), os.path.abspath(input_path))


class StagingDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="staging-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_directory_layout_is_output_staging_task(self):
        path = task_staging_dir("task-1")

        self.assertEqual(
            Path(path), self.output / STAGING_DIRNAME / "task-1"
        )
        self.assertTrue(Path(path).is_dir())

    def test_task_id_is_sanitised_into_a_single_directory_name(self):
        path = Path(task_staging_dir("../../evil/id"))

        self.assertEqual(path.parent, self.output / STAGING_DIRNAME)
        self.assertEqual(path.name, "evil_id")
        self.assertTrue(path.is_dir())

    def test_empty_task_id_falls_back_to_task(self):
        self.assertEqual(Path(task_staging_dir("")).name, "task")
        self.assertEqual(Path(task_staging_dir(None)).name, "task")

    def test_long_task_id_is_truncated(self):
        path = Path(task_staging_dir("x" * 200))

        self.assertEqual(len(path.name), 64)

    def test_staging_container_is_hidden(self):
        with patch.object(server_module, "ensure_hidden") as hidden:
            task_staging_dir("task-2")

        hidden.assert_called_once_with(
            os.path.join(str(self.output), STAGING_DIRNAME)
        )


class CleanupStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="cleanup-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.staging_root = self.output / STAGING_DIRNAME
        self.staging_root.mkdir(parents=True)
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_removes_task_directory_with_files(self):
        task_dir = self.staging_root / "task-1"
        (task_dir / "nested").mkdir(parents=True)
        (task_dir / "a.pdf").write_bytes(PDF_BYTES)
        (task_dir / "nested" / "b.pdf").write_bytes(PDF_BYTES)

        cleanup_staging(str(task_dir))

        self.assertFalse(task_dir.exists())
        self.assertTrue(self.staging_root.is_dir())

    def test_missing_directory_is_a_noop(self):
        cleanup_staging(str(self.staging_root / "ghost"))
        self.assertTrue(self.staging_root.is_dir())

    def test_refuses_to_delete_outside_the_staging_container(self):
        keep = self.output / "keep-me"
        keep.mkdir()
        (keep / "a.pdf").write_bytes(PDF_BYTES)

        cleanup_staging(str(keep))

        self.assertTrue((keep / "a.pdf").is_file())

    def test_refuses_to_delete_the_staging_container_itself(self):
        (self.staging_root / "task-1").mkdir()

        cleanup_staging(str(self.staging_root))

        self.assertTrue((self.staging_root / "task-1").is_dir())

    def test_falls_back_to_manual_removal_when_rmtree_gives_up(self):
        task_dir = self.staging_root / "task-1"
        task_dir.mkdir()
        (task_dir / "a.pdf").write_bytes(PDF_BYTES)

        with patch.object(server_module.shutil, "rmtree", return_value=None):
            cleanup_staging(str(task_dir))

        self.assertFalse(task_dir.exists())

    def test_fallback_removes_nested_directories_before_rmdir(self):
        task_dir = self.staging_root / "task-1"
        nested = task_dir / "nested"
        nested.mkdir(parents=True)
        (task_dir / "a.pdf").write_bytes(PDF_BYTES)
        (nested / "b.pdf").write_bytes(PDF_BYTES)

        real_rmtree = server_module.shutil.rmtree

        def fake_rmtree(path, **kwargs):
            # 第一次（任务目录）放弃，第二次（嵌套目录）真正删除。
            if Path(path) == task_dir:
                return None
            return real_rmtree(path, **kwargs)

        with patch.object(server_module.shutil, "rmtree", side_effect=fake_rmtree):
            cleanup_staging(str(task_dir))

        self.assertFalse(task_dir.exists())

    def test_cleanup_failure_never_raises(self):
        task_dir = self.staging_root / "task-1"
        task_dir.mkdir()
        (task_dir / "a.pdf").write_bytes(PDF_BYTES)

        # rmtree 放弃（Windows 文件锁）且兜底遍历也失败：仍然不得抛异常。
        with patch.object(
            server_module.shutil, "rmtree", return_value=None
        ), patch.object(server_module.os, "listdir", side_effect=OSError("locked")):
            cleanup_staging(str(task_dir))

        self.assertTrue(task_dir.is_dir())


class MoveProductsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="move-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self.staging = Path(tempfile.mkdtemp(prefix="move-staging-"))
        self.addCleanup(shutil.rmtree, self.staging, ignore_errors=True)
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _staged(self, name: str) -> str:
        path = self.staging / name
        path.write_bytes(PDF_BYTES)
        return str(path)

    def test_moves_products_to_output_root_with_new_names(self):
        mono = self._staged("paper-mono.pdf")
        dual = self._staged("paper-dual.pdf")

        moved = move_products_to_output(
            [(mono, "mono"), (dual, "dual"), (str(self.staging / "ghost.pdf"), "compare")],
            "paper",
        )

        self.assertEqual(
            sorted(Path(path).name for path in moved),
            ["Traned_double_paper.pdf", "Traned_single_paper.pdf"],
        )
        for path in moved:
            self.assertEqual(Path(path).parent, self.output)
            self.assertTrue(Path(path).is_file())
        self.assertFalse(Path(mono).exists())
        self.assertFalse(Path(dual).exists())

    def test_overwrites_previous_product_atomically(self):
        stale = self.output / "Traned_single_paper.pdf"
        stale.write_bytes(b"stale")
        mono = self._staged("paper-mono.pdf")

        move_products_to_output([(mono, "mono")], "paper")

        self.assertEqual(stale.read_bytes(), PDF_BYTES)

    def test_stem_with_prefix_does_not_stack(self):
        mono = self._staged("paper-mono.pdf")

        moved = move_products_to_output(
            [(mono, "mono-cut")], "Traned_double_paper"
        )

        self.assertEqual(Path(moved[0]).name, "Traned_single_cut_paper.pdf")

    def test_move_falls_back_to_copy_when_replace_fails(self):
        # Windows 上目标可能被占用：os.replace 失败后 copy2 兜底仍应成功归档。
        mono = self._staged("paper-mono.pdf")
        dual = self._staged("paper-dual.pdf")
        real_replace = os.replace

        def flaky_replace(source, target):
            if Path(source).name == "paper-dual.pdf":
                raise OSError("target locked")
            return real_replace(source, target)

        with patch.object(server_module.os, "replace", side_effect=flaky_replace):
            moved = move_products_to_output(
                [(mono, "mono"), (dual, "dual")], "paper"
            )

        self.assertEqual(
            sorted(Path(path).name for path in moved),
            ["Traned_double_paper.pdf", "Traned_single_paper.pdf"],
        )
        self.assertEqual(
            (self.output / "Traned_double_paper.pdf").read_bytes(), PDF_BYTES
        )
        # 兜底复制不删除源文件，由调用方 finally 统一清理 staging。
        self.assertTrue(Path(dual).is_file())
        self.assertFalse((self.output / "Traned_double_paper.pdf.tmp").exists())

    def test_move_failure_raises_instead_of_silently_skipping(self):
        mono = self._staged("paper-mono.pdf")
        dual = self._staged("paper-dual.pdf")
        real_replace = os.replace
        target = self.output / "Traned_double_paper.pdf"

        def flaky_replace(source, target_path):
            if Path(source).name == "paper-dual.pdf":
                raise OSError("target locked")
            return real_replace(source, target_path)

        with patch.object(
            server_module.os, "replace", side_effect=flaky_replace
        ), patch.object(
            server_module.shutil, "copy2", side_effect=OSError("copy failed")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                move_products_to_output(
                    [(mono, "mono"), (dual, "dual")], "paper"
                )

        self.assertIn("产物归档失败", str(ctx.exception))
        self.assertIn(str(target), str(ctx.exception))
        # 不允许“失败但返回成功列表”：异常已上抛，调用方必须走失败路径。
        self.assertFalse(target.exists())
        self.assertTrue(Path(dual).is_file(), "源产物留在 staging 由调用方清理")

    def test_copy_fallback_failure_keeps_existing_target_and_removes_tmp(self):
        # os.replace 与兜底路径的最终替换都失败时，旧产物不能被截断，也不能留 .tmp。
        dual = self._staged("paper-dual.pdf")
        target = self.output / "Traned_double_paper.pdf"
        target.write_bytes(b"old")

        with patch.object(server_module.os, "replace", side_effect=OSError("locked")):
            with self.assertRaises(RuntimeError):
                move_products_to_output([(dual, "dual")], "paper")

        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse((self.output / "Traned_double_paper.pdf.tmp").exists())
        self.assertTrue(Path(dual).is_file())


class TranslatedInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="translated-info-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )

    def _touch(self, name: str) -> None:
        (self.output / name).write_bytes(PDF_BYTES)

    def test_stem_filter_matches_new_naming(self):
        for name in (
            "Traned_single_paper.pdf",
            "Traned_double_paper.pdf",
            "Traned_single_cut_paper.pdf",
            "paper-mono.pdf",
            "Traned_single_other.pdf",
        ):
            self._touch(name)

        with self.translator.app.test_request_context("/translatedInfo?stem=paper"):
            response, status = self.translator.translated_info()

        self.assertEqual(status, 200)
        payload = response.get_json()
        self.assertEqual(
            sorted(item["fileName"] for item in payload["files"]),
            [
                "Traned_double_paper.pdf",
                "Traned_single_cut_paper.pdf",
                "Traned_single_paper.pdf",
                "paper-mono.pdf",
            ],
        )

    def test_empty_stem_lists_every_pdf(self):
        self._touch("Traned_single_paper.pdf")
        self._touch("other.pdf")
        (self.output / "notes.txt").write_text("x", encoding="utf-8")

        with self.translator.app.test_request_context("/translatedInfo"):
            response, _ = self.translator.translated_info()

        names = {item["fileName"] for item in response.get_json()["files"]}
        self.assertEqual(names, {"Traned_single_paper.pdf", "other.pdf"})


class UploadRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="upload-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )

    def _request(self, **overrides):
        payload = {
            "engine": "pdf2zh_next",
            "fileName": "paper.pdf",
            "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
        }
        payload.update(overrides)
        return self.translator.app.test_request_context(
            "/translate", method="POST", json=payload
        )

    def test_upload_copy_goes_to_task_staging_not_output_root(self):
        with self._request():
            input_path, _ = self.translator.process_request("task-1")

        self.assertEqual(
            Path(input_path),
            self.output / STAGING_DIRNAME / "task-1" / "paper.pdf",
        )
        self.assertEqual(Path(input_path).read_bytes(), PDF_BYTES)
        self.assertFalse((self.output / "paper.pdf").exists())

    def test_without_task_id_upload_keeps_legacy_output_root_behaviour(self):
        with self._request():
            input_path, _ = self.translator.process_request()

        self.assertEqual(Path(input_path), self.output / "paper.pdf")

    def test_data_url_prefix_is_accepted(self):
        content = "data:application/pdf;base64," + base64.b64encode(
            PDF_BYTES
        ).decode("ascii")

        with self._request(fileContent=content):
            input_path, _ = self.translator.process_request("task-2")

        self.assertEqual(Path(input_path).read_bytes(), PDF_BYTES)

    def test_traversal_and_bad_content_are_rejected_without_leaking_staging(self):
        bad_requests = (
            {"fileName": "../evil.pdf"},
            {"fileName": "paper.txt"},
            {"fileContent": "!!!not-base64!!!"},
        )
        for overrides in bad_requests:
            with self.subTest(overrides=overrides):
                with self._request(**overrides):
                    with self.assertRaises(ValueError):
                        self.translator.process_request("task-3")

        staging_root = self.output / STAGING_DIRNAME
        leftovers = (
            [p.name for p in staging_root.iterdir()] if staging_root.is_dir() else []
        )
        self.assertEqual(leftovers, [], "非法请求不应在 staging 留下任务目录")


class TranslateJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="job-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )
        self.staging = self.output / STAGING_DIRNAME / "task-1"
        self.staging.mkdir(parents=True)
        self.input_path = str(self.staging / "paper.pdf")
        Path(self.input_path).write_bytes(PDF_BYTES)

    def _stage(self, name: str) -> str:
        path = self.staging / name
        path.write_bytes(PDF_BYTES)
        return str(path)

    def test_success_archives_products_and_cleans_staging(self):
        mono = self._stage("paper-mono.pdf")
        dual = self._stage("paper-dual.pdf")

        with patch.object(
            self.translator,
            "_translate_products",
            return_value=[(mono, "mono"), (dual, "dual")],
        ), patch.object(server_module.task_manager, "complete_task") as complete:
            payload = self.translator._execute_translate_job(
                "task-1", self.input_path, SimpleNamespace(), "pdf2zh_next"
            )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(
            sorted(payload["fileList"]),
            ["Traned_double_paper.pdf", "Traned_single_paper.pdf"],
        )
        for name in payload["fileList"]:
            self.assertTrue((self.output / name).is_file())
        self.assertFalse(self.staging.exists(), "成功后 staging 必须清理")
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[1], "success")

    def test_no_products_marks_failure_and_cleans_staging(self):
        with patch.object(
            self.translator, "_translate_products", return_value=[]
        ), patch.object(server_module.task_manager, "complete_task") as complete:
            payload = self.translator._execute_translate_job(
                "task-1", self.input_path, SimpleNamespace(), "pdf2zh_next"
            )

        self.assertEqual(payload["status"], "error")
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[1], "failed")
        self.assertFalse(self.staging.exists())

    def test_exception_still_cleans_staging(self):
        with patch.object(
            self.translator,
            "_translate_products",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.translator._execute_translate_job(
                    "task-1", self.input_path, SimpleNamespace(), "pdf2zh_next"
                )

        self.assertFalse(self.staging.exists(), "失败也必须清理 staging")
        self.assertEqual(list(self.output.glob("Traned_*.pdf")), [])

    def test_archive_failure_is_not_reported_as_success(self):
        mono = self._stage("paper-mono.pdf")

        with patch.object(
            self.translator,
            "_translate_products",
            return_value=[(mono, "mono")],
        ), patch.object(
            server_module,
            "move_products_to_output",
            side_effect=RuntimeError("产物归档失败: locked"),
        ), patch.object(
            server_module.task_manager, "complete_task"
        ) as complete:
            with self.assertRaises(RuntimeError):
                self.translator._execute_translate_job(
                    "task-1", self.input_path, SimpleNamespace(), "pdf2zh_next"
                )

        # 归档失败由上层 _start_accepted_job 捕获并 complete failed；
        # 这里至少必须确认没有被标记为 success。
        for call in complete.call_args_list:
            self.assertNotEqual(call.args[1], "success")
        self.assertFalse(self.staging.exists(), "失败后 staging 仍要清理")
        self.assertEqual(list(self.output.glob("Traned_*.pdf")), [])


class TranslateEndpointCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="endpoint-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )

    def test_non_pdf_input_is_rejected_and_staging_is_cleaned(self):
        content = base64.b64encode(PDF_BYTES).decode("ascii")
        with self.translator.app.test_request_context(
            "/translate",
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": "Traned_single_paper.pdf",
                "fileContent": content,
            },
        ):
            response, status = self.translator.translate()

        self.assertEqual(status, 400)
        self.assertEqual(response.get_json()["status"], "error")
        staging_root = self.output / STAGING_DIRNAME
        leftovers = (
            [p.name for p in staging_root.iterdir()] if staging_root.is_dir() else []
        )
        self.assertEqual(leftovers, [], "被拒绝的上传也必须清理 staging")
        self.assertEqual(list(self.output.glob("Traned_*.pdf")), [])

    def test_invalid_json_does_not_create_staging_entries(self):
        with self.translator.app.test_request_context(
            "/translate", method="POST", json={"engine": "pdf2zh_next"}
        ):
            response, status = self.translator.translate()

        self.assertEqual(status, 500)
        self.assertIn("error", response.get_json()["status"])
        staging_root = self.output / STAGING_DIRNAME
        self.assertFalse(
            staging_root.is_dir() and any(staging_root.iterdir()),
            "非法请求不应在 staging 留下任务目录",
        )


if __name__ == "__main__":
    unittest.main()
