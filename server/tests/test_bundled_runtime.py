"""便携内置运行时（runtime/ + site-packages/）的探测与执行路径。

覆盖：
1. ``app_paths`` 的 ``install_root`` / ``bundled_*`` 探测（不受 ``PDF2ZH_HOME``
   影响，Windows / Unix 解释器路径不同）；
2. ``VirtualEnvManager`` 命中便携包后不探测 uv/conda、不安装依赖，命令使用
   内置 Python 且注入 ``PYTHONPATH=site-packages``；
3. 非便携目录仍走原有 uv/conda 逻辑；
4. ``environment_lifecycle`` 的便携短路与环境变量构造；
5. ``server.print_runtime_status`` 的启动打印与 site-packages 缺失告警。
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server as server_module
import utils.environment_lifecycle as env_life
import utils.venv as venv_module
from utils import app_paths
from utils.app_paths import (
    bundled_python,
    bundled_runtime_dir,
    bundled_site_packages,
    install_root,
)
from utils.environment_lifecycle import ENV_BUNDLED_SITE_PACKAGES, bundled_python_env
from utils.venv import VirtualEnvManager

SERVER_ROOT = Path(__file__).resolve().parents[1]


def _make_bundle(root: Path, *, windows: bool = True, with_site: bool = True):
    """在 ``root`` 下伪造便携包布局，返回 (python, site_packages)。"""
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    if windows:
        python = runtime / "python.exe"
        # 同时准备 Unix 路径：部分用例不 patch `_is_windows`，需要在
        # Linux runner 上也能探测到内置 Python（跨平台测试）。
        unix_python = runtime / "bin" / "python3"
        unix_python.parent.mkdir(parents=True, exist_ok=True)
        unix_python.write_bytes(b"")
    else:
        python = runtime / "bin" / "python3"
        python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    site = root / "site-packages"
    if with_site:
        site.mkdir()
    return python, site


def _manager(skip_install: bool = False) -> VirtualEnvManager:
    return VirtualEnvManager(
        "config/venv.json",
        {},
        "auto",
        enable_mirror=False,
        skip_install=skip_install,
        mirror_source=None,
    )


class BundledPathProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="bundled-paths-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.root = Path(self._temp_dir)

    def _root_patch(self):
        return patch.object(app_paths, "install_root", return_value=self.root)

    def test_install_root_ignores_pdf2zh_home(self):
        with patch.dict(
            os.environ, {app_paths.ENV_APP_HOME: str(self.root / "home")}
        ):
            self.assertEqual(install_root(), SERVER_ROOT)

    def test_missing_runtime_is_not_bundled(self):
        with self._root_patch():
            self.assertIsNone(bundled_runtime_dir())
            self.assertIsNone(bundled_python())
            self.assertIsNone(bundled_site_packages())

    def test_windows_uses_runtime_python_exe(self):
        python, site = _make_bundle(self.root, windows=True)
        with self._root_patch(), patch.object(
            app_paths, "_is_windows", return_value=True
        ):
            self.assertEqual(bundled_runtime_dir(), self.root / "runtime")
            self.assertEqual(bundled_python(), python)
            self.assertEqual(bundled_site_packages(), site)

    def test_unix_prefers_bin_python3_then_python(self):
        python, _ = _make_bundle(self.root, windows=False)
        with self._root_patch(), patch.object(
            app_paths, "_is_windows", return_value=False
        ):
            self.assertEqual(bundled_python(), python)
            python.unlink()
            (self.root / "runtime" / "bin" / "python").write_bytes(b"")
            self.assertEqual(
                bundled_python(), self.root / "runtime" / "bin" / "python"
            )

    def test_runtime_without_site_packages_still_detected(self):
        _make_bundle(self.root, with_site=False)
        with self._root_patch():
            self.assertIsNotNone(bundled_python())
            self.assertIsNone(bundled_site_packages())


class BundledVirtualEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="bundled-venv-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.root = Path(self._temp_dir)
        self.python, self.site = _make_bundle(self.root)

    def _patchers(self):
        return (
            patch.object(venv_module, "bundled_python", return_value=self.python),
            patch.object(venv_module, "bundled_site_packages", return_value=self.site),
            patch.object(venv_module, "install_root", return_value=self.root),
        )

    def test_bundled_manager_skips_uv_conda_and_install(self):
        p1, p2, p3 = self._patchers()
        with p1, p2, p3, patch.object(
            venv_module, "maybe_prompt_existing_user_update"
        ) as prompt, patch.object(
            venv_module, "transactional_install_or_update"
        ) as install, patch.object(
            venv_module.shutil, "which"
        ) as which:
            manager = _manager()
            self.assertTrue(manager.bundled)
            self.assertIn("便携内置运行时", manager.describe_runtime())
            self.assertTrue(manager.ensure_env("pdf2zh_next"))
            self.assertTrue(manager.check_env("pdf2zh_next", "auto"))
            self.assertTrue(manager.check_packages("pdf2zh_next", "auto"))
            self.assertTrue(manager.install_packages("pdf2zh_next", "auto"))
            self.assertTrue(manager.check_envtool("uv"))

        prompt.assert_not_called()
        install.assert_not_called()
        which.assert_not_called()

    def test_bundled_command_uses_internal_python_and_pythonpath(self):
        p1, p2, p3 = self._patchers()
        user_pythonpath = str(self.root / "user-pkgs")
        with p1, p2, p3, patch.dict(
            os.environ, {"PYTHONPATH": user_pythonpath}, clear=False
        ):
            manager = _manager()
            final_cmd, env = manager._resolved_command(
                ["pdf2zh_next", "--lang-in", "en"]
            )

        self.assertEqual(
            final_cmd,
            [str(self.python), "-u", "-m", "pdf2zh_next", "--lang-in", "en"],
        )
        self.assertEqual(
            env["PYTHONPATH"],
            os.pathsep.join([str(self.site), user_pythonpath]),
        )
        self.assertEqual(env[ENV_BUNDLED_SITE_PACKAGES], str(self.site))
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    def test_bundled_command_falls_back_to_plain_python_for_other_tools(self):
        p1, p2, p3 = self._patchers()
        with p1, p2, p3:
            manager = _manager()
            final_cmd, _ = manager._resolved_command(["python", "-V"])

        self.assertEqual(final_cmd, [str(self.python), "-u", "python", "-V"])

    def test_bundled_without_site_packages_does_not_set_pythonpath(self):
        p1, p2, p3 = self._patchers()
        with p1, patch.object(
            venv_module, "bundled_site_packages", return_value=None
        ), p3, patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
            manager = _manager()
            _, env = manager._resolved_command(["pdf2zh", "paper.pdf"])

        self.assertNotIn(ENV_BUNDLED_SITE_PACKAGES, env)
        self.assertNotIn("PYTHONPATH", env)

    def test_bundled_missing_python_is_not_ready(self):
        p1, p2, p3 = self._patchers()
        with p1, p2, p3:
            manager = _manager()
            manager.bundled_python_path = self.root / "runtime" / "missing.exe"
            self.assertFalse(manager.ensure_env("pdf2zh"))
            with self.assertRaises(RuntimeError):
                manager._resolved_command(["pdf2zh_next"])

    def test_bundled_env_survives_execute_sanitising(self):
        # execute_with_progress 会对 venv 返回的环境再调用 managed_python_env；
        # 内置 site-packages 的 PYTHONPATH 必须保留。
        p1, p2, p3 = self._patchers()
        with p1, p2, p3:
            manager = _manager()
            _, env = manager._resolved_command(["pdf2zh_next"])

        cleaned = env_life.managed_python_env(env)
        self.assertEqual(cleaned["PYTHONPATH"], env["PYTHONPATH"])
        self.assertEqual(cleaned[ENV_BUNDLED_SITE_PACKAGES], str(self.site))

    def test_non_bundled_directory_keeps_uv_conda_logic(self):
        with patch.object(
            venv_module, "bundled_python", return_value=None
        ), patch.object(
            venv_module, "bundled_site_packages", return_value=None
        ), patch.object(
            venv_module, "maybe_prompt_existing_user_update"
        ) as prompt, patch.object(
            venv_module, "transactional_install_or_update"
        ) as install:
            manager = _manager()

        prompt.assert_called_once()
        self.assertFalse(manager.bundled)
        self.assertIn("托管环境", manager.describe_runtime())

        with patch.object(manager, "_preferred_tools", return_value=[]), patch.object(
            manager, "_install_tools", return_value=[]
        ), patch.object(venv_module.shutil, "which", return_value=None):
            self.assertFalse(manager.ensure_env("pdf2zh"))
        install.assert_not_called()


class BundledEnvironmentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="bundled-lifecycle-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.root = Path(self._temp_dir)
        self.python, self.site = _make_bundle(self.root)

    def test_bundled_python_env_merges_user_pythonpath(self):
        env = bundled_python_env(
            self.site,
            {
                "PYTHONPATH": "/user/pkgs",
                "PYTHONHOME": "/wrong-home",
                "PATH": "/usr/bin",
            },
        )

        self.assertEqual(
            env["PYTHONPATH"], os.pathsep.join([str(self.site), "/user/pkgs"])
        )
        self.assertEqual(env[ENV_BUNDLED_SITE_PACKAGES], str(self.site))
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")

    def test_managed_python_env_keeps_pythonpath_only_when_marked(self):
        marked = env_life.managed_python_env(
            {
                "PYTHONPATH": "/bundled",
                "PYTHONHOME": "/wrong-home",
                ENV_BUNDLED_SITE_PACKAGES: str(self.site),
            }
        )
        self.assertEqual(marked["PYTHONPATH"], "/bundled")
        self.assertNotIn("PYTHONHOME", marked)

        plain = env_life.managed_python_env({"PYTHONPATH": "/ambient"})
        self.assertNotIn("PYTHONPATH", plain)
        self.assertEqual(plain["PYTHONNOUSERSITE"], "1")

    def test_validate_environment_short_circuits_for_bundled_python(self):
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(env_life.subprocess, "run") as run:
            healthy = env_life.validate_environment("pdf2zh_next", self.python, ["flask"])

        self.assertTrue(healthy)
        run.assert_not_called()

    def test_transactional_install_short_circuits_for_bundled_python(self):
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(
            env_life, "bundled_site_packages", return_value=self.site
        ), patch.object(
            env_life, "install_root", return_value=self.root
        ), patch.object(
            env_life, "find_existing_environment"
        ) as find, patch.object(
            env_life.shutil, "which"
        ) as which:
            success, tool, env_dir = env_life.transactional_install_or_update("pdf2zh")

        self.assertTrue(success)
        self.assertEqual(tool, "bundled")
        self.assertEqual(env_dir, self.root)
        find.assert_not_called()
        which.assert_not_called()

    def test_update_prompt_is_skipped_for_bundled_python(self):
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(
            env_life, "find_existing_environment"
        ) as find:
            env_life.maybe_prompt_existing_user_update(env_tool="auto")

        find.assert_not_called()


    def test_managed_runtime_health_short_circuits_for_bundled_python(self):
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(env_life.subprocess, "run") as run:
            healthy, reason = env_life.managed_runtime_health(
                "pdf2zh_next", self.python
            )

        self.assertTrue(healthy)
        self.assertEqual(reason, "ok")
        run.assert_not_called()

    def test_read_versions_uses_bundled_env_without_isolated_mode(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"pdf2zh-next": "2.9.0"}',
            stderr="",
        )
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(
            env_life, "bundled_site_packages", return_value=self.site
        ), patch.object(env_life.subprocess, "run", return_value=completed) as run:
            versions = env_life.read_versions(self.python, "pdf2zh_next")

        self.assertEqual(versions, {"pdf2zh-next": "2.9.0"})
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertNotIn("-I", command)
        self.assertEqual(env[ENV_BUNDLED_SITE_PACKAGES], str(self.site))

    def test_deepseek_probe_uses_bundled_env_without_isolated_mode(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"supported": true}',
            stderr="",
        )
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(
            env_life, "bundled_site_packages", return_value=self.site
        ), patch.object(env_life.subprocess, "run", return_value=completed) as run:
            supported = env_life.runtime_supports_deepseek_thinking(
                self.python, isolated=True
            )

        self.assertTrue(supported)
        command = run.call_args.args[0]
        # 便携模式的依赖在 site-packages，-I 会忽略 PYTHONPATH。
        self.assertNotIn("-I", command)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env[ENV_BUNDLED_SITE_PACKAGES], str(self.site))

    def test_find_existing_environment_reports_bundled(self):
        with patch.object(
            env_life, "bundled_python", return_value=self.python
        ), patch.object(env_life, "install_root", return_value=self.root):
            existing = env_life.find_existing_environment("pdf2zh", "auto")

        self.assertEqual(existing, ("bundled", self.root, self.python))


class BundledStartupPrintTests(unittest.TestCase):
    def test_prints_bundled_runtime_paths(self):
        python = Path("C:/bundle/runtime/python.exe")
        site = Path("C:/bundle/site-packages")
        with patch.object(
            server_module, "bundled_python", return_value=python
        ), patch.object(
            server_module, "bundled_site_packages", return_value=site
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                hit = server_module.print_runtime_status()

        output = buffer.getvalue()
        self.assertTrue(hit)
        self.assertIn("便携模式", output)
        self.assertIn(str(python), output)
        self.assertIn(str(site), output)
        self.assertNotIn("缺少 site-packages", output)

    def test_warns_when_site_packages_missing(self):
        python = Path("C:/bundle/runtime/python.exe")
        with patch.object(
            server_module, "bundled_python", return_value=python
        ), patch.object(
            server_module, "bundled_site_packages", return_value=None
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                hit = server_module.print_runtime_status()

        output = buffer.getvalue()
        self.assertTrue(hit)
        self.assertIn("缺少 site-packages", output)

    def test_non_bundled_prints_nothing(self):
        with patch.object(server_module, "bundled_python", return_value=None):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                hit = server_module.print_runtime_status()

        self.assertFalse(hit)
        self.assertEqual(buffer.getvalue(), "")


class ConfigEndpointRuntimeTests(unittest.TestCase):
    def test_config_reports_bundled_flag_and_runtime(self):
        fake_args = SimpleNamespace(
            port=8890,
            enable_venv=False,
            env_tool="auto",
            enable_mirror=False,
            mirror_source=None,
            skip_install=False,
            enable_winexe=False,
        )
        translator = server_module.PDFTranslator(
            SimpleNamespace(
                enable_venv=False,
                env_tool="uv",
                enable_mirror=False,
                skip_install=False,
                mirror_source=None,
            )
        )
        with patch.object(
            server_module, "args", fake_args, create=True
        ), translator.app.test_request_context(
            "/api/config"
        ):
            response = translator.get_config()

        payload = response.get_json()["config"]
        self.assertFalse(payload["bundled"])
        self.assertIn("系统环境", payload["runtime"])


if __name__ == "__main__":
    unittest.main()