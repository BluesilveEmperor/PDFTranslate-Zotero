"""翻译 / 裁剪 / 对照任务流程中的 staging 与归档行为。

这些用例把内核（pdf2zh / pdf2zh_next）与 PDF 操作（Cropper）都替换成
「在指定路径写一个假 PDF」的桩，从而只验证 server 自己的编排逻辑：
中间产物必须留在任务 staging、最终产物必须按 ``Traned_*`` 命名落到输出根、
无论成功失败 staging 都要清理。
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
from server import STAGING_DIRNAME, PDFTranslator, prepare_path

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def _make_translator() -> PDFTranslator:
    return PDFTranslator(
        SimpleNamespace(
            enable_venv=False,
            env_tool="uv",
            enable_mirror=False,
            skip_install=False,
            mirror_source=None,
        )
    )


def _write(path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(PDF_BYTES)


class _StagingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="jobs-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(server_module, "output_folder", str(self.output))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = _make_translator()
        self.staging = self.output / STAGING_DIRNAME / "task-1"
        self.staging.mkdir(parents=True)
        self.input_path = str(self.staging / "paper.pdf")
        _write(self.input_path)

    def staged(self, name: str) -> str:
        path = self.staging / name
        _write(path)
        return str(path)

    def leftovers(self):
        return sorted(p.name for p in self.staging.iterdir()) if self.staging.is_dir() else []


class TranslateProductsTests(_StagingCase):
    def test_pdf2zh_engine_returns_all_requested_products(self):
        mono = self.staged("paper-mono.pdf")
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(
            mono_cut=True,
            dual_cut=True,
            crop_compare=True,
            compare=True,
            babeldoc=False,
        )

        def fake_crop(_config, _source, _intype, target, _outtype):
            _write(target)

        def fake_merge(_source, target):
            _write(target)

        with patch.object(
            self.translator, "translate_pdf", return_value=(mono, dual)
        ), patch.object(
            self.translator.cropper, "crop_pdf", side_effect=fake_crop
        ), patch.object(
            self.translator.cropper, "merge_pdf", side_effect=fake_merge
        ):
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh"
            )

        self.assertEqual(
            [(Path(path).name, outtype) for path, outtype in products],
            [
                ("paper-mono.pdf", "mono"),
                ("paper-dual.pdf", "dual"),
                ("paper.mono-cut.pdf", "mono-cut"),
                ("paper.dual-cut.pdf", "dual-cut"),
                ("paper.crop-compare.pdf", "crop-compare"),
                ("paper.compare.pdf", "compare"),
            ],
        )
        # 所有中间产物都必须在本任务的 staging 目录内。
        for path, _ in products:
            self.assertEqual(Path(path).parent, self.staging)

    def test_pdf2zh_babeldoc_skips_compare(self):
        mono = self.staged("paper-mono.pdf")
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(
            mono_cut=False,
            dual_cut=False,
            crop_compare=False,
            compare=True,
            babeldoc=True,
        )

        with patch.object(
            self.translator, "translate_pdf", return_value=(mono, dual)
        ), patch.object(self.translator.cropper, "merge_pdf") as merge:
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh"
            )

        self.assertEqual([outtype for _, outtype in products], ["mono", "dual"])
        merge.assert_not_called()

    def test_pdf2zh_next_lr_compare_copies_the_dual_layout(self):
        mono = self.staged("paper-mono.pdf")
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(
            mono=True,
            dual=True,
            mono_cut=False,
            dual_cut=False,
            crop_compare=False,
            compare=True,
            no_mono=False,
            no_dual=False,
            dual_mode="LR",
        )

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[mono, dual]
        ):
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh_next"
            )

        names = {Path(path).name: outtype for path, outtype in products}
        self.assertIn("paper-mono.pdf", names)
        self.assertTrue(
            any(Path(path).name.endswith(".LR_dual.pdf") for path in names),
            f"dual 产物应规范化为 LR_dual：{list(names)}",
        )
        compare_path = self.staging / "paper.compare.pdf"
        self.assertTrue(compare_path.is_file())
        self.assertEqual(compare_path.read_bytes(), PDF_BYTES)
        self.assertIn("compare", names.values())

    def test_pdf2zh_next_tb_compare_merges_pages(self):
        mono = self.staged("paper-mono.pdf")
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(
            mono=True,
            dual=True,
            mono_cut=False,
            dual_cut=False,
            crop_compare=False,
            compare=True,
            no_mono=False,
            no_dual=False,
            dual_mode="TB",
        )

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[mono, dual]
        ), patch.object(
            self.translator.cropper,
            "merge_pdf",
            side_effect=lambda _src, target: _write(target),
        ):
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh_next"
            )

        self.assertIn("compare", [outtype for _, outtype in products])
        self.assertTrue((self.staging / "paper.compare.pdf").is_file())

    def test_pdf2zh_next_dual_cut_uses_tb_layout(self):
        mono = self.staged("paper-mono.pdf")
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(
            mono=False,
            dual=False,
            mono_cut=False,
            dual_cut=True,
            crop_compare=False,
            compare=False,
            no_mono=False,
            no_dual=False,
            dual_mode="TB",
        )

        def fake_crop(_config, _source, _intype, target, _outtype):
            _write(target)

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[mono, dual]
        ), patch.object(self.translator.cropper, "crop_pdf", side_effect=fake_crop):
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh_next"
            )

        # pdf2zh_next 总会产出 mono，因此中间产物除 dual-cut 外还有 mono。
        self.assertEqual(
            [outtype for _, outtype in products], ["mono", "dual-cut"]
        )
        self.assertTrue((self.staging / "paper.dual-cut.pdf").is_file())

    def test_pdf2zh_next_no_dual_keeps_only_mono(self):
        mono = self.staged("paper-mono.pdf")
        config = SimpleNamespace(
            mono=True,
            dual=False,
            mono_cut=False,
            dual_cut=False,
            crop_compare=False,
            compare=False,
            no_mono=False,
            no_dual=True,
            dual_mode="TB",
        )

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[mono]
        ):
            products = self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh_next"
            )

        self.assertEqual([(Path(p).name, t) for p, t in products], [("paper-mono.pdf", "mono")])

    def test_pdf2zh_next_rejects_disabling_both_outputs(self):
        config = SimpleNamespace(
            mono=False,
            dual=False,
            mono_cut=False,
            dual_cut=False,
            crop_compare=False,
            compare=False,
            no_mono=True,
            no_dual=True,
        )

        with self.assertRaises(ValueError) as ctx:
            self.translator._translate_products(
                "task-1", self.input_path, config, "pdf2zh_next"
            )
        self.assertIn("至少需要生成 mono 或 dual", str(ctx.exception))

    def test_unknown_engine_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.translator._translate_products(
                "task-1", self.input_path, SimpleNamespace(), "not-an-engine"
            )
        self.assertIn("不支持的翻译引擎", str(ctx.exception))


class _EndpointCase(unittest.TestCase):
    """端点测试基类：独立输出目录 + 真实请求上下文。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="crop-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.output.mkdir()
        self._patcher = patch.object(
            server_module, "output_folder", str(self.output)
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.translator = _make_translator()

    def _request(self, route: str, file_name: str):
        return self.translator.app.test_request_context(
            route,
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": file_name,
                "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
            },
        )


class CropEndpointTests(_EndpointCase):
    def _post(self, file_name: str):
        return self.translator.app.test_request_context(
            "/crop",
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": file_name,
                "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
            },
        )

    def test_origin_crop_is_archived_with_new_prefix_and_staging_cleaned(self):
        def fake_crop(_config, source, _intype, target, _outtype):
            self.assertTrue(os.path.exists(source))
            _write(target)

        with self._post("paper.pdf"), patch.object(
            self.translator.cropper, "crop_pdf", side_effect=fake_crop
        ):
            response, status = self.translator.crop()

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["fileList"], ["Traned_origin_cut_paper.pdf"])
        self.assertTrue((self.output / "Traned_origin_cut_paper.pdf").is_file())
        self.assertFalse(
            any(self.output.glob(".staging/*/paper.pdf")),
            "上传源副本不得残留在 staging",
        )
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])

    def test_missing_crop_result_returns_500_and_cleans_staging(self):
        with self._post("paper.pdf"), patch.object(
            self.translator.cropper, "crop_pdf", return_value=None
        ) as crop:
            crop.side_effect = lambda *a, **k: None
            response, status = self.translator.crop()

        self.assertEqual(status, 500)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])

    def test_double_processed_input_is_rejected(self):
        with self._post("Traned_double_cut_paper.pdf"):
            response, status = self.translator.crop()

        self.assertEqual(status, 400)
        self.assertEqual(response.get_json()["errorType"], "InvalidPDFOperation")
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])


class CompareJobTests(_StagingCase):
    def _config(self, mode: str):
        return SimpleNamespace(dual_mode=mode)

    def test_tb_compare_merges_and_archives(self):
        dual = self.staged("paper.dual.pdf")

        with patch.object(
            self.translator.cropper,
            "merge_pdf",
            side_effect=lambda _src, target: _write(target),
        ), patch.object(server_module.task_manager, "complete_task") as complete:
            payload = self.translator._execute_compare_job(
                "task-1", dual, self._config("TB"), "pdf2zh_next", "dual"
            )

        self.assertEqual(payload["fileList"], ["Traned_compare_paper.pdf"])
        self.assertTrue((self.output / "Traned_compare_paper.pdf").is_file())
        self.assertFalse(self.staging.exists())
        complete.assert_called_once()

    def test_lr_compare_is_a_byte_copy_of_the_dual_file(self):
        dual = self.staged("paper.LR_dual.pdf")

        with patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_compare_job(
                "task-1", dual, self._config("LR"), "pdf2zh_next", "dual"
            )

        self.assertEqual(payload["fileList"], ["Traned_compare_paper.pdf"])
        self.assertEqual(
            (self.output / "Traned_compare_paper.pdf").read_bytes(), PDF_BYTES
        )
        self.assertFalse(self.staging.exists())

    def test_compare_rejects_non_dual_input_and_cleans_staging(self):
        with self.assertRaises(ValueError) as ctx:
            self.translator._execute_compare_job(
                "task-1", self.input_path, self._config("TB"), "pdf2zh_next", "mono"
            )

        self.assertIn("不能执行 compare", str(ctx.exception))
        self.assertFalse(self.staging.exists())

    def test_compare_translates_origin_first_when_engine_is_pdf2zh(self):
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(dual_mode="TB")

        with patch.object(
            self.translator, "translate_pdf", return_value=(self.input_path, dual)
        ) as translate, patch.object(
            self.translator.cropper,
            "merge_pdf",
            side_effect=lambda _src, target: _write(target),
        ), patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_compare_job(
                "task-1", self.input_path, config, "pdf2zh", "origin"
            )

        translate.assert_called_once()
        self.assertEqual(config.engine, "pdf2zh")
        self.assertEqual(payload["fileList"], ["Traned_compare_paper.pdf"])

    def test_compare_reports_failure_when_product_is_missing(self):
        dual = self.staged("paper.dual.pdf")

        with patch.object(self.translator.cropper, "merge_pdf"), patch.object(
            server_module.task_manager, "complete_task"
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.translator._execute_compare_job(
                    "task-1", dual, self._config("TB"), "pdf2zh_next", "dual"
                )

        self.assertIn("Compare failed", str(ctx.exception))
        self.assertFalse(self.staging.exists())


class CropCompareJobTests(_StagingCase):
    def test_dual_input_is_cropped_and_archived(self):
        dual = self.staged("paper.dual.pdf")

        with patch.object(
            self.translator.cropper,
            "crop_pdf",
            side_effect=lambda _c, _s, _i, target, _o: _write(target),
        ) as crop, patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_crop_compare_job(
                "task-1", dual, SimpleNamespace(dual_mode="TB"), "pdf2zh_next", "dual"
            )

        self.assertEqual(payload["fileList"], ["Traned_crop_compare_paper.pdf"])
        self.assertTrue((self.output / "Traned_crop_compare_paper.pdf").is_file())
        self.assertEqual(crop.call_args.args[4], "crop-compare")
        self.assertFalse(self.staging.exists())

    def test_lr_dual_is_converted_to_tb_before_cropping(self):
        dual = self.staged("paper.LR_dual.pdf")
        tb = str(self.staging / "paper.TB_dual.pdf")
        _write(tb)

        with patch.object(
            self.translator.cropper,
            "pdf_dual_mode",
            return_value=("lr", tb),
        ), patch.object(
            self.translator.cropper,
            "crop_pdf",
            side_effect=lambda _c, _s, _i, target, _o: _write(target),
        ) as crop, patch.object(server_module.task_manager, "complete_task"):
            self.translator._execute_crop_compare_job(
                "task-1", dual, SimpleNamespace(dual_mode="LR"), "pdf2zh_next", "dual"
            )

        self.assertEqual(crop.call_args.args[1], tb)

    def test_dual_cut_input_is_merged(self):
        dual_cut = self.staged("paper.dual-cut.pdf")

        with patch.object(
            self.translator.cropper,
            "merge_pdf",
            side_effect=lambda _src, target: _write(target),
        ), patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_crop_compare_job(
                "task-1",
                dual_cut,
                SimpleNamespace(dual_mode="TB"),
                "pdf2zh_next",
                "dual-cut",
            )

        self.assertEqual(payload["fileList"], ["Traned_crop_compare_paper.pdf"])

    def test_unsupported_input_type_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.translator._execute_crop_compare_job(
                "task-1",
                self.input_path,
                SimpleNamespace(dual_mode="TB"),
                "pdf2zh_next",
                "mono",
            )

        self.assertIn("不能执行 crop-compare", str(ctx.exception))
        self.assertFalse(self.staging.exists())


class CropLRLayoutTests(_EndpointCase):
    def _post(self, file_name: str):
        return self._request("/crop", file_name)

    def test_lr_dual_is_normalised_to_tb_before_cropping(self):
        tb_source = Path(self._temp_dir) / "tb.pdf"
        _write(tb_source)

        with self._post("paper.LR_dual.pdf"), patch.object(
            self.translator.cropper,
            "pdf_dual_mode",
            return_value=("lr.pdf", str(tb_source)),
        ) as dual_mode, patch.object(
            self.translator.cropper,
            "crop_pdf",
            side_effect=lambda _c, _s, _i, target, _o: _write(target),
        ) as crop:
            response, status = self.translator.crop()

        self.assertEqual(status, 200)
        dual_mode.assert_called_once()
        self.assertEqual(crop.call_args.args[1], str(tb_source))
        self.assertEqual(
            response.get_json()["fileList"], ["Traned_double_cut_paper.pdf"]
        )
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])


class CompareEndpointTests(_EndpointCase):
    def _post_compare(self, file_name: str):
        return self.translator.app.test_request_context(
            "/compare",
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": file_name,
                "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
            },
        )

    def test_existing_compare_result_is_rejected_with_409(self):
        with self._post_compare("Traned_compare_paper.pdf"):
            response, status = self.translator.compare()

        self.assertEqual(status, 409)
        self.assertEqual(response.get_json()["errorType"], "InvalidPDFOperation")
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])

    def test_mono_input_is_rejected_with_400(self):
        with self._post_compare("Traned_single_paper.pdf"):
            response, status = self.translator.compare()

        self.assertEqual(status, 400)
        self.assertIn("不能执行 compare", response.get_json()["message"])
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])


class CropCompareEndpointTests(_EndpointCase):
    def _post(self, file_name: str):
        return self.translator.app.test_request_context(
            "/crop-compare",
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": file_name,
                "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
            },
        )

    def test_existing_crop_compare_result_is_rejected_with_409(self):
        with self._post("Traned_crop_compare_paper.pdf"):
            response, status = self.translator.crop_compare()

        self.assertEqual(status, 409)
        self.assertEqual(response.get_json()["errorType"], "InvalidPDFOperation")
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])

    def test_mono_input_is_rejected_with_400(self):
        with self._post("Traned_single_paper.pdf"):
            response, status = self.translator.crop_compare()

        self.assertEqual(status, 400)
        self.assertIn("不能执行 crop-compare", response.get_json()["message"])
        self.assertEqual(list((self.output / STAGING_DIRNAME).iterdir()), [])


class OriginJobTests(_StagingCase):
    def test_compare_origin_with_pdf2zh_next_translates_lr_dual_first(self):
        dual = self.staged("paper.LR_dual.pdf")
        config = SimpleNamespace(dual_mode="TB", no_dual=True, no_mono=False)

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[dual]
        ) as translate, patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_compare_job(
                "task-1", self.input_path, config, "pdf2zh_next", "origin"
            )

        translate.assert_called_once()
        self.assertEqual(config.dual_mode, "LR")
        self.assertIs(config.no_dual, False)
        self.assertIs(config.no_mono, True)
        self.assertEqual(payload["fileList"], ["Traned_compare_paper.pdf"])
        self.assertTrue((self.output / "Traned_compare_paper.pdf").is_file())
        self.assertFalse(self.staging.exists())

    def test_compare_origin_with_pdf2zh_uses_dual_output(self):
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(dual_mode="TB")

        with patch.object(
            self.translator, "translate_pdf", return_value=(self.input_path, dual)
        ), patch.object(
            self.translator.cropper,
            "merge_pdf",
            side_effect=lambda _src, target: _write(target),
        ), patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_compare_job(
                "task-1", self.input_path, config, "pdf2zh", "origin"
            )

        self.assertEqual(payload["fileList"], ["Traned_compare_paper.pdf"])

    def test_compare_origin_raises_when_kernel_dual_is_missing(self):
        config = SimpleNamespace(dual_mode="TB")

        with patch.object(
            self.translator,
            "translate_pdf",
            return_value=(self.input_path, str(self.staging / "ghost-dual.pdf")),
        ):
            with self.assertRaises(FileNotFoundError) as ctx:
                self.translator._execute_compare_job(
                    "task-1", self.input_path, config, "pdf2zh", "origin"
                )

        self.assertIn("Dual file not found", str(ctx.exception))
        self.assertFalse(self.staging.exists())

    def test_crop_compare_origin_with_pdf2zh_next_translates_tb_dual(self):
        dual = self.staged("paper.TB_dual.pdf")
        config = SimpleNamespace(dual_mode="LR", no_dual=True, no_mono=False)

        with patch.object(
            self.translator, "translate_pdf_next", return_value=[dual]
        ) as translate, patch.object(
            self.translator.cropper,
            "crop_pdf",
            side_effect=lambda _c, _s, _i, target, _o: _write(target),
        ), patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_crop_compare_job(
                "task-1", self.input_path, config, "pdf2zh_next", "origin"
            )

        translate.assert_called_once()
        self.assertEqual(config.dual_mode, "TB")
        self.assertEqual(payload["fileList"], ["Traned_crop_compare_paper.pdf"])
        self.assertFalse(self.staging.exists())

    def test_crop_compare_origin_with_pdf2zh_translates_dual(self):
        dual = self.staged("paper-dual.pdf")
        config = SimpleNamespace(dual_mode="TB")

        with patch.object(
            self.translator, "translate_pdf", return_value=(self.input_path, dual)
        ), patch.object(
            self.translator.cropper,
            "crop_pdf",
            side_effect=lambda _c, _s, _i, target, _o: _write(target),
        ), patch.object(server_module.task_manager, "complete_task"):
            payload = self.translator._execute_crop_compare_job(
                "task-1", self.input_path, config, "pdf2zh", "origin"
            )

        self.assertEqual(config.engine, "pdf2zh")
        self.assertEqual(payload["fileList"], ["Traned_crop_compare_paper.pdf"])


class _InlineThread:
    """``threading.Thread`` 的确定性替身：``start()`` 立即执行 target。

    异步受理路径用 daemon 线程跑 job；单元测试不能依赖线程调度，否则断言
    「job 完成后 task 状态 / staging 清理」会随机失败。
    """

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self) -> None:
        assert self._target is not None
        self._target()


class AsyncAcceptedJobTests(_EndpointCase):
    def _post(self, **extra):
        payload = {
            "engine": "pdf2zh_next",
            "fileName": "paper.pdf",
            "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
        }
        payload.update(extra)
        return self.translator.app.test_request_context(
            "/translate", method="POST", json=payload
        )

    def _run_translate(self, worker, stream=None):
        from contextlib import redirect_stdout
        from io import StringIO

        stream = stream or StringIO()
        with self._post(asyncJob=True), patch.object(
            server_module, "threading", SimpleNamespace(Thread=_InlineThread)
        ), patch.object(
            self.translator, "_translate_products", side_effect=worker
        ), patch.object(
            server_module.task_manager, "add_task"
        ) as add, patch.object(
            server_module.task_manager, "complete_task"
        ) as complete, redirect_stdout(stream):
            response, status = self.translator.translate()
        return response, status, add, complete, stream.getvalue()

    def test_job_exception_marks_task_failed_and_cleans_staging(self):
        def boom(*args, **kwargs):
            raise RuntimeError("kernel crashed")

        response, status, add, complete, output = self._run_translate(boom)

        self.assertEqual(status, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "accepted")
        task_id = body["taskId"]
        self.assertTrue(task_id)

        add.assert_called_once()
        self.assertEqual(add.call_args.args[0], task_id)
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[0], task_id)
        self.assertEqual(complete.call_args.args[1], "failed")
        self.assertIn("kernel crashed", complete.call_args.args[2])
        self.assertIn("kernel crashed", output)

        staging = self.output / STAGING_DIRNAME
        self.assertFalse(
            (staging / task_id).exists(), "异步 job 失败后必须删除该任务的 staging"
        )
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])
        self.assertEqual(list(self.output.glob("Traned_*.pdf")), [])

    def test_job_success_archives_products_and_cleans_staging(self):
        def products(_task_id, input_path, _config, _engine):
            staging = Path(input_path).parent
            mono = staging / "paper-mono.pdf"
            _write(mono)
            return [(str(mono), "mono")]

        response, status, _, complete, _ = self._run_translate(products)

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertTrue((self.output / "Traned_single_paper.pdf").is_file())
        self.assertEqual(complete.call_args.args[1], "success")
        staging = self.output / STAGING_DIRNAME
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])

    def test_job_without_products_marks_task_failed(self):
        response, status, _, complete, _ = self._run_translate(lambda *a, **k: [])

        self.assertEqual(status, 200, "HTTP 已在 job 之前返回 accepted")
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertEqual(complete.call_args.args[1], "failed")
        self.assertEqual(complete.call_args.kwargs["error"], "无文件生成")
        staging = self.output / STAGING_DIRNAME
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])

    def test_async_flag_from_header_is_honoured(self):
        from contextlib import redirect_stdout
        from io import StringIO

        def boom(*args, **kwargs):
            raise RuntimeError("header path")

        with self.translator.app.test_request_context(
            "/translate",
            method="POST",
            json={
                "engine": "pdf2zh_next",
                "fileName": "paper.pdf",
                "fileContent": base64.b64encode(PDF_BYTES).decode("ascii"),
            },
            headers={"X-PDF2zh-Protocol": "accepted"},
        ), patch.object(
            server_module, "threading", SimpleNamespace(Thread=_InlineThread)
        ), patch.object(
            self.translator, "_translate_products", side_effect=boom
        ), patch.object(
            server_module.task_manager, "add_task"
        ), patch.object(
            server_module.task_manager, "complete_task"
        ) as complete, redirect_stdout(StringIO()):
            response, status = self.translator.translate()

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["status"], "accepted")
        self.assertEqual(complete.call_args.args[1], "failed")

    def test_legacy_client_still_gets_synchronous_success_and_cleanup(self):
        from contextlib import redirect_stdout
        from io import StringIO

        def products(_task_id, input_path, _config, _engine):
            staging = Path(input_path).parent
            mono = staging / "paper-mono.pdf"
            _write(mono)
            return [(str(mono), "mono")]

        with self._request("/translate", "paper.pdf") as _ctx, patch.object(
            self.translator, "_translate_products", side_effect=products
        ), patch.object(
            server_module.task_manager, "add_task"
        ), patch.object(
            server_module.task_manager, "complete_task"
        ), redirect_stdout(StringIO()):
            response, status = self.translator.translate()

        self.assertEqual(status, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(body["fileList"], ["Traned_single_paper.pdf"])
        staging = self.output / STAGING_DIRNAME
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])

    def test_legacy_client_sync_failure_returns_500_and_cleans_staging(self):
        from contextlib import redirect_stdout
        from io import StringIO

        def boom(*args, **kwargs):
            raise RuntimeError("sync crashed")

        with self._request("/translate", "paper.pdf"), patch.object(
            self.translator, "_translate_products", side_effect=boom
        ), patch.object(
            server_module.task_manager, "add_task"
        ), patch.object(
            server_module.task_manager, "complete_task"
        ) as complete, redirect_stdout(StringIO()):
            response, status = self.translator.translate()

        self.assertEqual(status, 500)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertEqual(complete.call_args.args[1], "failed")
        staging = self.output / STAGING_DIRNAME
        self.assertEqual(list(staging.iterdir()) if staging.is_dir() else [], [])

    def test_sync_job_with_unexpected_status_returns_500(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with self._request("/translate", "paper.pdf"), patch.object(
            self.translator,
            "_execute_translate_job",
            return_value={"status": "nonsense"},
        ), patch.object(
            server_module.task_manager, "add_task"
        ), patch.object(
            server_module.task_manager, "complete_task"
        ), redirect_stdout(StringIO()):
            response, status = self.translator.translate()

        self.assertEqual(status, 500)
        body = response.get_json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["message"], "操作失败，请查看详细日志。")

    def test_sync_job_error_payload_is_forwarded_with_500(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with self._request("/translate", "paper.pdf"), patch.object(
            self.translator,
            "_execute_translate_job",
            return_value={"status": "error", "message": "no files generated"},
        ), patch.object(
            server_module.task_manager, "add_task"
        ), patch.object(
            server_module.task_manager, "complete_task"
        ), redirect_stdout(StringIO()):
            response, status = self.translator.translate()

        self.assertEqual(status, 500)
        self.assertEqual(
            response.get_json(),
            {"status": "error", "message": "no files generated"},
        )

    def test_non_object_json_is_treated_as_legacy_client(self):
        # _client_wants_async_job 只接受 dict；数组/字符串必须走同步协议，
        # 否则旧插件会拿到 accepted 而挂不上附件。
        with self.translator.app.test_request_context(
            "/translate", method="POST", json=["asyncJob", True]
        ) as _ctx:
            self.assertFalse(self.translator._client_wants_async_job())

    def test_client_protocol_field_selects_async_mode(self):
        with self.translator.app.test_request_context(
            "/translate", method="POST", json={"clientProtocol": "accepted"}
        ):
            self.assertTrue(self.translator._client_wants_async_job())
        with self.translator.app.test_request_context(
            "/translate", method="POST", json={"clientProtocol": "legacy"}
        ):
            self.assertFalse(self.translator._client_wants_async_job())
        with self.translator.app.test_request_context(
            "/translate", method="POST", json={"asyncJob": "0"}
        ):
            self.assertFalse(
                self.translator._client_wants_async_job(),
                "asyncJob 为假值时仍应是旧协议",
            )


class ConcurrentStagingTests(_StagingCase):
    def _make_task(self, task_id: str, stem: str):
        staging = Path(server_module.task_staging_dir(task_id))
        input_path = staging / f"{stem}.pdf"
        _write(input_path)
        return staging, str(input_path)

    def _products_writer(self, payload: bytes, outtype: str = "mono"):
        def writer(_task_id, input_path, _config, _engine):
            target = Path(input_path).parent / f"{Path(input_path).stem}-{outtype}.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return [(str(target), outtype)]

        return writer

    def test_task_staging_directories_are_isolated(self):
        staging_a, input_a = self._make_task("task-a", "paper")
        staging_b, input_b = self._make_task("task-b", "other")

        self.assertNotEqual(staging_a, staging_b)
        self.assertEqual(staging_a.parent, staging_b.parent)
        self.assertTrue(Path(input_a).is_file())
        self.assertTrue(Path(input_b).is_file())

    def test_same_task_id_reuses_its_own_directory(self):
        first = server_module.task_staging_dir("task-a")
        second = server_module.task_staging_dir("task-a")

        self.assertEqual(first, second)

    def test_parallel_jobs_keep_products_apart(self):
        import threading

        staging_a, input_a = self._make_task("task-a", "paper")
        staging_b, input_b = self._make_task("task-b", "other")
        other = _make_translator()
        payload_a = PDF_BYTES + b"task-a"
        payload_b = PDF_BYTES + b"task-b"

        with patch.object(
            self.translator,
            "_translate_products",
            side_effect=self._products_writer(payload_a),
        ), patch.object(
            other, "_translate_products", side_effect=self._products_writer(payload_b)
        ), patch.object(server_module.task_manager, "complete_task") as complete:
            threads = [
                threading.Thread(
                    target=self.translator._execute_translate_job,
                    args=("task-a", input_a, SimpleNamespace(), "pdf2zh_next"),
                ),
                threading.Thread(
                    target=other._execute_translate_job,
                    args=("task-b", input_b, SimpleNamespace(), "pdf2zh_next"),
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "并发 job 不应阻塞")

        product_a = self.output / "Traned_single_paper.pdf"
        product_b = self.output / "Traned_single_other.pdf"
        self.assertTrue(product_a.is_file())
        self.assertTrue(product_b.is_file())
        self.assertEqual(product_a.read_bytes()[-6:], b"task-a")
        self.assertEqual(product_b.read_bytes()[-6:], b"task-b")
        self.assertFalse(staging_a.exists())
        self.assertFalse(staging_b.exists())
        self.assertEqual(
            sorted(Path(p).name for p in self.output.glob("*.pdf")),
            ["Traned_single_other.pdf", "Traned_single_paper.pdf"],
        )
        self.assertEqual(complete.call_count, 2)

    def test_failed_task_cleanup_does_not_touch_the_other_task(self):
        staging_a, input_a = self._make_task("task-a", "paper")
        staging_b, input_b = self._make_task("task-b", "other")
        other = _make_translator()

        def boom(*args, **kwargs):
            raise RuntimeError("first task failed")

        with patch.object(
            self.translator, "_translate_products", side_effect=boom
        ), patch.object(
            other,
            "_translate_products",
            side_effect=self._products_writer(PDF_BYTES + b"task-b"),
        ), patch.object(server_module.task_manager, "complete_task"):
            with self.assertRaises(RuntimeError):
                self.translator._execute_translate_job(
                    "task-a", input_a, SimpleNamespace(), "pdf2zh_next"
                )
            other._execute_translate_job(
                "task-b", input_b, SimpleNamespace(), "pdf2zh_next"
            )

        self.assertFalse(staging_a.exists(), "失败任务必须清理自己的 staging")
        self.assertFalse(staging_b.exists())
        self.assertFalse((self.output / "Traned_single_paper.pdf").exists())
        self.assertTrue((self.output / "Traned_single_other.pdf").is_file())

    def test_same_stem_second_job_overwrites_atomically(self):
        # 同一次上传被重复翻译（同 stem）时，后完成者原子覆盖，不留中间文件。
        staging_a, input_a = self._make_task("task-a", "paper")
        staging_b, input_b = self._make_task("task-b", "paper")

        with patch.object(
            self.translator,
            "_translate_products",
            side_effect=self._products_writer(PDF_BYTES + b"first"),
        ), patch.object(server_module.task_manager, "complete_task"):
            self.translator._execute_translate_job(
                "task-a", input_a, SimpleNamespace(), "pdf2zh_next"
            )
        with patch.object(
            self.translator,
            "_translate_products",
            side_effect=self._products_writer(PDF_BYTES + b"second"),
        ), patch.object(server_module.task_manager, "complete_task"):
            self.translator._execute_translate_job(
                "task-b", input_b, SimpleNamespace(), "pdf2zh_next"
            )

        product = self.output / "Traned_single_paper.pdf"
        self.assertEqual(product.read_bytes()[-6:], b"second")
        self.assertEqual(
            [p.name for p in self.output.iterdir() if p.is_file()],
            ["Traned_single_paper.pdf"],
            "原子覆盖不得留下临时文件",
        )
        self.assertFalse(staging_a.exists())
        self.assertFalse(staging_b.exists())


class PreparePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="prepare-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.output = Path(self._temp_dir) / "translated"
        self.config = Path(self._temp_dir) / "server-config"
        self.runtime = self.config / "pdf-cache"

    def test_creates_directories_hides_config_and_migrates_legacy_files(self):
        migrated = [self.runtime / "config.json"]
        with patch.object(
            server_module, "output_folder", str(self.output)
        ), patch.object(server_module, "config_folder", str(self.config)), patch.object(
            server_module, "runtime_config_folder", str(self.runtime)
        ), patch.object(
            server_module, "config_path", {}
        ), patch.object(
            server_module, "ensure_hidden"
        ) as hidden, patch.object(
            server_module, "migrate_legacy_configs", return_value=migrated
        ) as migrate, patch.object(
            server_module, "prepare_config_files"
        ) as prepare, patch.object(
            server_module.os, "name", "nt"
        ):
            prepare_path()

        self.assertTrue(self.output.is_dir())
        self.assertTrue(self.runtime.is_dir())
        migrate.assert_called_once_with(str(self.config), str(self.runtime))
        self.assertEqual(
            [call.args[0] for call in hidden.call_args_list],
            [str(self.config), str(self.runtime)],
        )
        prepare.assert_called_once_with({})

    def test_non_windows_does_not_touch_file_attributes(self):
        with patch.object(
            server_module, "output_folder", str(self.output)
        ), patch.object(server_module, "config_folder", str(self.config)), patch.object(
            server_module, "runtime_config_folder", str(self.runtime)
        ), patch.object(
            server_module, "config_path", {}
        ), patch.object(
            server_module, "ensure_hidden"
        ) as hidden, patch.object(
            server_module, "migrate_legacy_configs", return_value=[]
        ), patch.object(
            server_module, "prepare_config_files"
        ), patch.object(server_module.os, "name", "posix"):
            prepare_path()

        hidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()