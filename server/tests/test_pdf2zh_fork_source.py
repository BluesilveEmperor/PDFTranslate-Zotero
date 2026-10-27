"""pdf2zh fork 安装源（PEP 508 direct reference + gh-proxy）单元测试。

覆盖三件事：
1. venv.json 模板 / 常量声明的是 fork 源，而不是上游 PyPI pin；
2. git 子模块代理环境只为「代理源」注入，直连 / PyPI 回退不被污染；
3. 运行期 venv.json 与状态文件落在隐藏的 ``config/pdf-cache``，且配置迁移
   不会悄悄把用户回退到 PyPI 的意图覆盖掉（环境变量优先）。

这些用例全部是纯函数 / 子进程边界测试，不会真的执行 git 或 pip。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils import app_paths
from utils import environment_lifecycle as env_life
from utils.config_migration import _merge_managed_requirements

SERVER_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = SERVER_ROOT / "config" / "venv.json.example"


class RequirementParsingTests(unittest.TestCase):
    def test_normalises_version_pins_and_direct_references(self):
        cases = {
            "pdf2zh==1.9.12": "pdf2zh",
            "pdf2zh>=1.9,<2.0": "pdf2zh",
            "PDF2Zh @ git+https://example.com/pdf2zh.git@main": "pdf2zh",
            "pdf2zh_next[extra]>=2.9.0": "pdf2zh-next",
            "  pdf2zh ; python_version >= '3.10'": "pdf2zh",
        }
        for requirement, expected in cases.items():
            with self.subTest(requirement=requirement):
                self.assertEqual(
                    env_life.requirement_package_name(requirement), expected
                )

    def test_direct_reference_detection(self):
        self.assertTrue(
            env_life.is_direct_reference(
                "pdf2zh @ git+https://github.com/x/y.git@main"
            )
        )
        self.assertFalse(env_life.is_direct_reference("pdf2zh==1.9.12"))
        self.assertFalse(env_life.is_direct_reference("pypdf>=4"))


class RedactRequirementTests(unittest.TestCase):
    def test_credentials_are_hidden_but_scheme_and_host_are_kept(self):
        text = env_life._redact_requirement(
            "git+https://user:token@example.com/fork.git@main"
        )

        self.assertEqual(text, "git+https://***@example.com/fork.git@main")
        self.assertNotIn("token", text)

    def test_proxy_fork_spec_and_revision_are_untouched(self):
        spec = env_life.PDF2ZH_FORK_SPEC

        self.assertEqual(env_life._redact_requirement(spec), spec)

    def test_plain_requirement_is_untouched(self):
        self.assertEqual(
            env_life._redact_requirement("pdf2zh==1.9.12"), "pdf2zh==1.9.12"
        )


class ForkSourceClassificationTests(unittest.TestCase):
    def test_proxy_requirement_is_the_fork(self):
        spec = env_life.PDF2ZH_FORK_SPEC

        self.assertTrue(env_life.is_pdf2zh_proxy_requirement(spec))
        self.assertTrue(env_life.is_pdf2zh_fork_requirement(spec))
        self.assertIn("gh-proxy.com", spec)
        self.assertIn("@main", spec)

    def test_direct_github_requirement_is_a_fork_but_not_proxied(self):
        spec = env_life.PDF2ZH_FORK_DIRECT_SPEC

        self.assertFalse(env_life.is_pdf2zh_proxy_requirement(spec))
        self.assertTrue(env_life.is_pdf2zh_fork_requirement(spec))
        self.assertIn("github.com", spec)

    def test_upstream_pypi_pin_is_neither_fork_nor_proxy(self):
        spec = env_life.PDF2ZH_PYPI_FALLBACK_SPEC

        self.assertFalse(env_life.is_pdf2zh_proxy_requirement(spec))
        self.assertFalse(env_life.is_pdf2zh_fork_requirement(spec))

    def test_other_packages_never_count_as_pdf2zh_fork(self):
        self.assertFalse(
            env_life.is_pdf2zh_fork_requirement(
                f"pypdf @ git+{env_life.PDF2ZH_FORK_URL}@main"
            )
        )


class TemplateDeclarationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    def test_both_env_tools_install_pdf2zh_from_the_fork(self):
        for tool in ("uv", "conda"):
            with self.subTest(tool=tool):
                packages = self.template["pdf2zh"][tool]["packages"]
                pdf2zh_specs = [
                    item
                    for item in packages
                    if env_life.requirement_package_name(item) == "pdf2zh"
                ]
                self.assertEqual(len(pdf2zh_specs), 1)
                spec = pdf2zh_specs[0]
                self.assertTrue(env_life.is_pdf2zh_proxy_requirement(spec))
                self.assertIn(env_life.PDF2ZH_FORK_URL, spec)
                self.assertIn(f"@{env_life.PDF2ZH_FORK_REVISION}", spec)

    def test_template_no_longer_pins_upstream_pypi_version(self):
        raw = TEMPLATE.read_text(encoding="utf-8")

        self.assertNotIn("pdf2zh==1.9.11", raw)
        self.assertNotIn("pdf2zh==1.9.12", raw)

    def test_pdf2zh_next_still_comes_from_pypi(self):
        for tool in ("uv", "conda"):
            with self.subTest(tool=tool):
                packages = self.template["pdf2zh_next"][tool]["packages"]
                next_specs = [
                    item
                    for item in packages
                    if env_life.requirement_package_name(item) == "pdf2zh-next"
                ]
                self.assertEqual(len(next_specs), 1)
                self.assertFalse(env_life.is_direct_reference(next_specs[0]))

    def test_module_constants_match_the_shipped_template(self):
        for tool in ("uv", "conda"):
            packages = self.template["pdf2zh"][tool]["packages"]
            self.assertIn(env_life.PDF2ZH_FORK_SPEC, packages)
            self.assertNotIn(env_life.PDF2ZH_PYPI_FALLBACK_SPEC, packages)


class GitProxyEnvTests(unittest.TestCase):
    def test_injects_instead_of_only_for_the_proxy_spec(self):
        source = {"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "0"}

        injected = env_life._pdf2zh_git_proxy_env(
            [env_life.PDF2ZH_FORK_SPEC, "pypdf"], source
        )

        self.assertIsNotNone(injected)
        self.assertEqual(injected["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(injected["GIT_CONFIG_KEY_0"], env_life.PDF2ZH_GIT_PROXY_KEY)
        self.assertEqual(injected["GIT_CONFIG_VALUE_0"], "https://github.com/")
        self.assertEqual(injected["PATH"], "/usr/bin")
        # 调用方的 mapping 不得被就地修改。
        self.assertEqual(source, {"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "0"})

    def test_direct_or_pypi_requirements_do_not_get_the_proxy(self):
        for requirement in (
            env_life.PDF2ZH_FORK_DIRECT_SPEC,
            env_life.PDF2ZH_PYPI_FALLBACK_SPEC,
            "pypdf",
        ):
            with self.subTest(requirement=requirement):
                self.assertIsNone(
                    env_life._pdf2zh_git_proxy_env([requirement], {"PATH": "x"})
                )

    def test_does_not_duplicate_an_existing_insteadof_entry(self):
        source = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": env_life.PDF2ZH_GIT_PROXY_KEY,
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }

        returned = env_life._pdf2zh_git_proxy_env(
            [env_life.PDF2ZH_FORK_SPEC], source
        )

        self.assertEqual(returned["GIT_CONFIG_COUNT"], "1")
        self.assertNotIn("GIT_CONFIG_KEY_1", returned)

    def test_appends_after_user_configured_git_settings(self):
        source = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "someone@example.com",
        }

        returned = env_life._pdf2zh_git_proxy_env(
            [env_life.PDF2ZH_FORK_SPEC], source
        )

        self.assertEqual(returned["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(returned["GIT_CONFIG_KEY_0"], "user.name")
        self.assertEqual(returned["GIT_CONFIG_KEY_1"], "user.email")
        self.assertEqual(returned["GIT_CONFIG_KEY_2"], env_life.PDF2ZH_GIT_PROXY_KEY)

    def test_invalid_git_config_count_keeps_user_settings(self):
        source = {
            "GIT_CONFIG_COUNT": "not-a-number",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone",
        }
        stream = io.StringIO()
        with redirect_stdout(stream):
            returned = env_life._pdf2zh_git_proxy_env(
                [env_life.PDF2ZH_FORK_SPEC], source
            )

        # 非法 count 无法安全追加：宁可不注入，也不能覆盖用户 git 配置。
        self.assertIsNone(returned)
        self.assertIn("跳过", stream.getvalue())
        self.assertEqual(source["GIT_CONFIG_COUNT"], "not-a-number")
        self.assertEqual(source["GIT_CONFIG_KEY_0"], "user.name")

    def test_incomplete_git_config_count_keeps_user_settings(self):
        source = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone",
        }
        stream = io.StringIO()
        with redirect_stdout(stream):
            returned = env_life._pdf2zh_git_proxy_env(
                [env_life.PDF2ZH_FORK_SPEC], source
            )

        self.assertIsNone(returned)
        self.assertIn("跳过", stream.getvalue())

    def test_git_config_entries_without_count_keep_user_settings(self):
        source = {"GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "someone"}
        stream = io.StringIO()
        with redirect_stdout(stream):
            returned = env_life._pdf2zh_git_proxy_env(
                [env_life.PDF2ZH_FORK_SPEC], source
            )

        self.assertIsNone(returned)
        self.assertIn("跳过", stream.getvalue())


class InstallSubprocessEnvTests(unittest.TestCase):
    def test_proxy_spec_injects_git_rewrite_on_top_of_manager_env(self):
        with patch.object(
            env_life,
            "_package_manager_env",
            return_value={"PATH": "managed", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        ):
            env = env_life._install_subprocess_env([env_life.PDF2ZH_FORK_SPEC])

        self.assertEqual(env["PATH"], "managed")
        self.assertEqual(env["PIP_DISABLE_PIP_VERSION_CHECK"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], env_life.PDF2ZH_GIT_PROXY_KEY)

    def test_pypi_spec_keeps_manager_env_untouched(self):
        with patch.object(
            env_life, "_package_manager_env", return_value={"PATH": "managed"}
        ):
            env = env_life._install_subprocess_env([env_life.PDF2ZH_PYPI_FALLBACK_SPEC])

        self.assertEqual(env, {"PATH": "managed"})


class SpecOverrideTests(unittest.TestCase):
    def _apply(self, packages, override=None):
        env = {} if override is None else {env_life.PDF2ZH_SPEC_ENV: override}
        stream = io.StringIO()
        with patch.dict(os.environ, env, clear=False), redirect_stdout(stream):
            if override is None:
                os.environ.pop(env_life.PDF2ZH_SPEC_ENV, None)
            result = env_life._apply_pdf2zh_spec_override(list(packages))
        return result, stream.getvalue()

    def test_no_override_returns_packages_unchanged(self):
        packages = [env_life.PDF2ZH_FORK_SPEC, "pypdf"]

        result, output = self._apply(packages)

        self.assertEqual(result, packages)
        self.assertEqual(output, "")

    def test_override_replaces_only_pdf2zh_and_keeps_order(self):
        packages = [env_life.PDF2ZH_FORK_SPEC, "pypdf", "PyMuPDF"]

        result, output = self._apply(packages, env_life.PDF2ZH_PYPI_FALLBACK_SPEC)

        self.assertEqual(
            result,
            [env_life.PDF2ZH_PYPI_FALLBACK_SPEC, "pypdf", "PyMuPDF"],
        )
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)

    def test_override_is_inserted_when_pdf2zh_is_absent(self):
        result, _ = self._apply(["pypdf"], "pdf2zh==1.9.12")

        self.assertEqual(result, ["pdf2zh==1.9.12", "pypdf"])

    def test_override_for_another_package_is_ignored_with_warning(self):
        packages = [env_life.PDF2ZH_FORK_SPEC, "pypdf"]

        result, output = self._apply(packages, "pypdf==5.0")

        self.assertEqual(result, packages)
        self.assertIn("已忽略", output)

    def test_duplicate_pdf2zh_entries_are_collapsed_into_one(self):
        packages = ["pdf2zh==1.9.11", "pypdf", "pdf2zh==1.9.10"]

        result, _ = self._apply(packages, "pdf2zh==1.9.13")

        self.assertEqual(result, ["pdf2zh==1.9.13", "pypdf"])


class RuntimeConfigPathTests(unittest.TestCase):
    def setUp(self) -> None:
        app_paths._cache_dir_cache.clear()
        self.addCleanup(app_paths._cache_dir_cache.clear)
        self._home = tempfile.mkdtemp(prefix="fork-home-")
        self.addCleanup(shutil.rmtree, self._home, ignore_errors=True)
        patcher = patch.dict(
            os.environ, {app_paths.ENV_APP_HOME: self._home}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_venv_config_lives_in_hidden_cache_dir(self):
        path = env_life.runtime_venv_config_path()

        self.assertEqual(
            path, Path(self._home).resolve() / "config" / "pdf-cache" / "venv.json"
        )
        self.assertTrue(path.parent.is_dir())

    def test_package_update_state_lives_in_hidden_cache_dir(self):
        path = env_life.package_update_state_path()

        self.assertEqual(path.parent, env_life.cache_dir())
        self.assertEqual(path.name, "package_update_state.json")

    def test_load_requirements_prefers_runtime_config(self):
        runtime = env_life.runtime_venv_config_path()
        runtime.write_text(
            json.dumps(
                {
                    "pdf2zh": {
                        "uv": {
                            "python_version": "3.11",
                            "packages": ["pdf2zh==1.9.13", "pypdf"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        version, packages = env_life.load_requirements("pdf2zh", "uv")

        self.assertEqual(version, "3.11")
        self.assertEqual(packages, ["pdf2zh==1.9.13", "pypdf"])

    def test_load_requirements_falls_back_to_shipped_template(self):
        self.assertFalse(env_life.runtime_venv_config_path().exists())

        _, packages = env_life.load_requirements("pdf2zh", "uv")

        self.assertIn(env_life.PDF2ZH_FORK_SPEC, packages)

    def test_env_override_wins_over_runtime_config_file(self):
        runtime = env_life.runtime_venv_config_path()
        runtime.write_text(
            json.dumps(
                {
                    "pdf2zh": {
                        "uv": {
                            "python_version": "3.12",
                            "packages": [env_life.PDF2ZH_FORK_SPEC, "pypdf"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {env_life.PDF2ZH_SPEC_ENV: env_life.PDF2ZH_PYPI_FALLBACK_SPEC},
            clear=False,
        ), redirect_stdout(io.StringIO()):
            _, packages = env_life.load_requirements("pdf2zh", "uv")

        self.assertEqual(packages[0], env_life.PDF2ZH_PYPI_FALLBACK_SPEC)
        self.assertNotIn(env_life.PDF2ZH_FORK_SPEC, packages)

    def test_pdf2zh_next_ignores_pdf2zh_spec_override(self):
        runtime = env_life.runtime_venv_config_path()
        runtime.write_text(
            json.dumps(
                {
                    "pdf2zh_next": {
                        "uv": {
                            "python_version": "3.12",
                            "packages": ["pdf2zh-next>=2.9.0,<3.0.0"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {env_life.PDF2ZH_SPEC_ENV: env_life.PDF2ZH_PYPI_FALLBACK_SPEC},
            clear=False,
        ):
            _, packages = env_life.load_requirements("pdf2zh_next", "uv")

        self.assertEqual(packages, ["pdf2zh-next>=2.9.0,<3.0.0"])


class ManagedRequirementMigrationTests(unittest.TestCase):
    """模板迁移与「回退到 PyPI」的关系。

    ``_merge_managed_requirements`` 会用发布模板里的托管项覆盖用户文件中的
    同名项（这是防止用户被旧 pin 卡住的既有行为）。因此「长期回退」只能靠
    环境变量表达：迁移之后 ``load_requirements`` 仍会用环境变量覆盖。
    """

    def test_template_managed_entry_replaces_user_pinned_pdf2zh(self):
        defaults = [env_life.PDF2ZH_FORK_SPEC, "pypdf"]
        current = ["pdf2zh==1.9.11", "pypdf", "user-extra-pkg"]

        merged = _merge_managed_requirements(defaults, current)

        self.assertEqual(
            merged, [env_life.PDF2ZH_FORK_SPEC, "pypdf", "user-extra-pkg"]
        )

    def test_env_override_still_wins_after_template_migration(self):
        merged = _merge_managed_requirements(
            [env_life.PDF2ZH_FORK_SPEC, "pypdf"], ["pdf2zh==1.9.11", "pypdf"]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "venv.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pdf2zh": {
                            "uv": {"python_version": "3.12", "packages": merged}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {env_life.PDF2ZH_SPEC_ENV: env_life.PDF2ZH_FORK_DIRECT_SPEC},
                clear=False,
            ), redirect_stdout(io.StringIO()):
                _, packages = env_life.load_requirements(
                    "pdf2zh", "uv", config_path
                )

        self.assertEqual(packages[0], env_life.PDF2ZH_FORK_DIRECT_SPEC)
        self.assertNotIn(env_life.PDF2ZH_FORK_SPEC, packages)


class DescribeSourceTests(unittest.TestCase):
    def _describe(self, packages, tool="uv"):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "venv.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pdf2zh": {
                            tool: {"python_version": "3.12", "packages": packages}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(env_life.PDF2ZH_SPEC_ENV, None)
                return env_life.describe_pdf2zh_source(tool, config_path)

    def test_reports_proxy_fork_source(self):
        text = self._describe([env_life.PDF2ZH_FORK_SPEC])

        self.assertIn("fork 源", text)
        self.assertIn("gh-proxy", text)
        self.assertIn("@main", text)

    def test_reports_direct_fork_source(self):
        text = self._describe([env_life.PDF2ZH_FORK_DIRECT_SPEC])

        self.assertIn("直连", text)
        self.assertIn("github.com/BluesilveEmperor/PDFtranslate", text)

    def test_reports_pypi_source_for_a_version_pin(self):
        text = self._describe(["pdf2zh==1.9.12"])

        self.assertEqual(text, "PyPI（pdf2zh==1.9.12）")

    def test_reports_custom_direct_reference(self):
        text = self._describe(["pdf2zh @ git+https://example.com/fork.git@dev"])

        self.assertIn("自定义 direct reference", text)

    def test_reports_missing_declaration(self):
        self.assertEqual(self._describe(["pypdf"]), "未声明 pdf2zh 依赖")

    def test_unknown_tool_name_falls_back_to_uv_requirements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "venv.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pdf2zh": {
                            "uv": {
                                "python_version": "3.12",
                                "packages": [env_life.PDF2ZH_FORK_SPEC],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            text = env_life.describe_pdf2zh_source("unknown-tool", config_path)

        self.assertIn("fork 源", text)

    def test_reports_read_failure_without_raising(self):
        missing = Path(tempfile.mkdtemp(prefix="broken-")) / "venv.json"
        missing.write_text("{not-json", encoding="utf-8")
        self.addCleanup(shutil.rmtree, missing.parent, ignore_errors=True)

        text = env_life.describe_pdf2zh_source("uv", missing)

        self.assertIn("未知", text)


class InstallSourceSelectionTests(unittest.TestCase):
    def _choose(self, requirements, reachable_sources):
        results = [
            SimpleNamespace(
                name=f"m{index}",
                url=url,
                reachable=True,
                total_latency_ms=index,
            )
            for index, url in enumerate(["https://a", "https://b", "https://c"])
        ]
        calls = []

        def fake_preflight(env_tool, python_path, reqs, url):
            calls.append(url)
            return url in reachable_sources

        with (
            patch.object(env_life, "probe_indexes", return_value=results),
            patch.object(env_life, "print_probe_report"),
            patch.object(env_life, "usable_indexes", return_value=results),
            patch.object(
                env_life, "_resolver_preflight", side_effect=fake_preflight
            ),
        ):
            sources = env_life.choose_install_sources(
                "pdf2zh", "uv", Path("python"), requirements
            )
        return sources, calls

    def test_git_requirements_keep_at_most_two_sources(self):
        sources, calls = self._choose(
            [env_life.PDF2ZH_FORK_SPEC],
            {"https://a", "https://b", "https://c"},
        )

        self.assertEqual(sources, ["https://a", "https://b"])
        self.assertEqual(calls, ["https://a", "https://b"])

    def test_git_requirements_keep_a_second_source_for_install_fallback(self):
        sources, _ = self._choose(
            [env_life.PDF2ZH_FORK_SPEC], {"https://a", "https://b"}
        )

        self.assertEqual(len(sources), 2)

    def test_plain_packages_keep_all_usable_sources(self):
        sources, calls = self._choose(
            ["pdf2zh-next>=2.9.0"], {"https://a", "https://b", "https://c"}
        )

        self.assertEqual(sources, ["https://a", "https://b", "https://c"])
        self.assertEqual(calls, ["https://a", "https://b", "https://c"])

    def test_git_short_circuit_does_not_consume_budget_on_failed_sources(self):
        # 第一源预检失败不占「保留 2 个」的配额，仍能拿到 b / c 用于回退。
        sources, calls = self._choose(
            [env_life.PDF2ZH_FORK_SPEC], {"https://b", "https://c"}
        )

        self.assertEqual(sources, ["https://b", "https://c"])
        self.assertEqual(calls, ["https://a", "https://b", "https://c"])

    def test_git_short_circuit_returns_empty_when_every_source_fails(self):
        sources, calls = self._choose([env_life.PDF2ZH_FORK_SPEC], set())

        self.assertEqual(sources, [])
        self.assertEqual(calls, ["https://a", "https://b", "https://c"])

    def test_git_requirements_keep_a_single_passable_source(self):
        sources, calls = self._choose(
            [env_life.PDF2ZH_FORK_SPEC], {"https://b"}
        )

        self.assertEqual(sources, ["https://b"])
        # 只有 1 个通过预检时无法凑满 2 个，必须遍历到最后一个候选。
        self.assertEqual(calls, ["https://a", "https://b", "https://c"])

    def test_plain_packages_skip_unreachable_sources_without_short_circuit(self):
        sources, calls = self._choose(["pdf2zh-next>=2.9.0"], {"https://b"})

        self.assertEqual(sources, ["https://b"])
        self.assertEqual(calls, ["https://a", "https://b", "https://c"])

    def test_empty_usable_index_list_returns_no_sources(self):
        with (
            patch.object(env_life, "probe_indexes", return_value=[]),
            patch.object(env_life, "print_probe_report"),
            patch.object(env_life, "usable_indexes", return_value=[]),
            patch.object(env_life, "_resolver_preflight") as preflight,
        ):
            sources = env_life.choose_install_sources(
                "pdf2zh", "uv", Path("python"), [env_life.PDF2ZH_FORK_SPEC]
            )

        self.assertEqual(sources, [])
        preflight.assert_not_called()

    def test_preferred_index_and_timeout_are_forwarded_to_probe(self):
        with (
            patch.object(env_life, "probe_indexes", return_value=[]) as probe,
            patch.object(env_life, "print_probe_report"),
            patch.object(env_life, "usable_indexes", return_value=[]),
        ):
            env_life.choose_install_sources(
                "pdf2zh",
                "uv",
                Path("python"),
                [env_life.PDF2ZH_FORK_SPEC],
                preferred_index="https://mirror.example/simple",
                network_timeout=7.5,
            )

        probe.assert_called_once_with(
            env_life.PRIMARY_PACKAGES["pdf2zh"],
            preferred_index="https://mirror.example/simple",
            timeout=7.5,
        )


class InstallTimeoutTests(unittest.TestCase):
    def test_resolver_preflight_without_install_command_only_trusts_conda(self):
        # 没有 uv / pip 可用时无法预检：conda 由自己的 solver 负责，uv 视为不可用。
        with patch.object(env_life, "_build_install_command", return_value=None):
            self.assertTrue(
                env_life._resolver_preflight(
                    "conda", Path("python"), [env_life.PDF2ZH_FORK_SPEC], "https://a"
                )
            )
            self.assertFalse(
                env_life._resolver_preflight(
                    "uv", Path("python"), [env_life.PDF2ZH_FORK_SPEC], "https://a"
                )
            )
    def test_resolver_preflight_uses_git_timeout_and_proxy_env(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(env_life.subprocess, "run", side_effect=fake_run):
            ok = env_life._resolver_preflight(
                "uv", Path("python"), [env_life.PDF2ZH_FORK_SPEC], "https://pypi.org/simple"
            )

        self.assertTrue(ok)
        self.assertEqual(captured["timeout"], env_life.PDF2ZH_GIT_PREFLIGHT_TIMEOUT)
        self.assertEqual(
            captured["env"]["GIT_CONFIG_KEY_0"], env_life.PDF2ZH_GIT_PROXY_KEY
        )

    def test_resolver_preflight_keeps_short_timeout_for_plain_packages(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(env_life.subprocess, "run", side_effect=fake_run):
            env_life._resolver_preflight(
                "uv", Path("python"), ["pdf2zh-next"], "https://pypi.org/simple"
            )

        self.assertEqual(captured["timeout"], 180)
        self.assertNotIn("GIT_CONFIG_COUNT", captured["env"])

    def test_preflight_failure_is_reported_as_false(self):
        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(
            env_life.subprocess, "run", side_effect=RuntimeError("network down")
        ):
            self.assertFalse(
                env_life._resolver_preflight(
                    "uv",
                    Path("python"),
                    [env_life.PDF2ZH_FORK_SPEC],
                    "https://pypi.org/simple",
                )
            )

    def test_run_install_uses_git_timeout_and_proxy_env(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0)

        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(env_life.subprocess, "run", side_effect=fake_run), redirect_stdout(
            io.StringIO()
        ):
            ok = env_life._run_install(
                "uv",
                Path("python"),
                [env_life.PDF2ZH_FORK_SPEC],
                ["https://pypi.org/simple"],
            )

        self.assertTrue(ok)
        self.assertEqual(captured["timeout"], env_life.PDF2ZH_GIT_INSTALL_TIMEOUT)
        self.assertEqual(
            captured["env"]["GIT_CONFIG_VALUE_0"], "https://github.com/"
        )
        self.assertIs(captured["check"], True)

    def test_run_install_falls_back_to_next_source_after_failure(self):
        import subprocess

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(env_life.subprocess, "run", side_effect=fake_run), redirect_stdout(
            io.StringIO()
        ):
            ok = env_life._run_install(
                "uv",
                Path("python"),
                ["pdf2zh-next"],
                ["https://mirror-a", "https://mirror-b"],
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["timeout"], 1200)

    def test_run_install_retries_the_second_git_source_with_proxy_env(self):
        import subprocess

        calls = []

        def fake_run(command, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(returncode=0)

        with (
            patch.object(
                env_life, "_build_install_command", return_value=["uv"]
            ),
            patch.object(env_life.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
        ):
            ok = env_life._run_install(
                "uv",
                Path("python"),
                [env_life.PDF2ZH_FORK_SPEC],
                ["https://mirror-a", "https://mirror-b"],
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["timeout"], env_life.PDF2ZH_GIT_INSTALL_TIMEOUT)
        self.assertEqual(
            calls[1]["env"]["GIT_CONFIG_KEY_0"], env_life.PDF2ZH_GIT_PROXY_KEY
        )

    def test_run_install_returns_false_when_no_source_works(self):
        import subprocess

        with patch.object(
            env_life, "_build_install_command", return_value=["uv", "pip", "install"]
        ), patch.object(
            env_life.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["uv"]),
        ), redirect_stdout(io.StringIO()):
            ok = env_life._run_install(
                "uv", Path("python"), ["pdf2zh-next"], ["https://only-source"]
            )

        self.assertFalse(ok)


class ForkInstallHintTests(unittest.TestCase):
    def test_proxy_fork_hint_explains_both_fallbacks(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            env_life._report_pdf2zh_fork_install_hint(
                "pdf2zh", [env_life.PDF2ZH_FORK_SPEC]
            )
        output = stream.getvalue()

        self.assertIn("fork 源", output)
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)
        self.assertIn(env_life.PDF2ZH_FORK_DIRECT_SPEC, output)
        self.assertIn(env_life.PDF2ZH_PYPI_FALLBACK_SPEC, output)
        self.assertIn("gh-proxy", output)

    def test_direct_fork_hint_does_not_mention_proxy(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            env_life._report_pdf2zh_fork_install_hint(
                "pdf2zh", [env_life.PDF2ZH_FORK_DIRECT_SPEC]
            )
        output = stream.getvalue()

        self.assertIn("当前直连 GitHub", output)
        self.assertNotIn("代理克隆主仓库", output)
        # 直连场景只有两档回退（PyPI / 改 venv.json），不应出现代理专属的第 3 档。
        self.assertIn(env_life.PDF2ZH_PYPI_FALLBACK_SPEC, output)
        self.assertIn("\n   1) 临时设置环境变量", output)
        self.assertIn("\n   2) 也可把 venv.json", output)
        self.assertNotIn("\n   3) 也可把 venv.json", output)

    def test_pypi_requirement_prints_no_fork_hint(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            env_life._report_pdf2zh_fork_install_hint(
                "pdf2zh", [env_life.PDF2ZH_PYPI_FALLBACK_SPEC]
            )

        self.assertEqual(stream.getvalue(), "")

    def test_other_engines_print_no_hint(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            env_life._report_pdf2zh_fork_install_hint(
                "pdf2zh_next", [env_life.PDF2ZH_FORK_SPEC]
            )

        self.assertEqual(stream.getvalue(), "")


class ForkInstallFailureTests(unittest.TestCase):
    """fork 安装失败时必须给出中文回退提示（而不是只报一句失败）。

    ``transactional_install_or_update`` 有三处失败出口（无可用下载源 / 安装失败 /
    校验失败）和一个异常出口，本次改动全部接上了同一份提示。
    """

    def setUp(self) -> None:
        self.python_path = Path(sys.executable)
        self.env_dir = Path(tempfile.mkdtemp(prefix="fork-env-"))
        self.addCleanup(shutil.rmtree, self.env_dir, ignore_errors=True)

    def _run(self, **overrides):
        stubs = {
            "_cleanup_legacy_sidecar_environments": lambda *a, **k: None,
            "_ensure_canonical_environment": lambda *a, **k: (
                self.env_dir,
                self.python_path,
            ),
            "load_requirements": lambda *a, **k: (
                "3.12",
                [env_life.PDF2ZH_FORK_SPEC, "pypdf"],
            ),
            "managed_runtime_health": lambda *a, **k: (True, "ok"),
            "read_versions": lambda *a, **k: {},
            "choose_install_sources": lambda *a, **k: ["https://pypi.org/simple"],
            "_run_install": lambda *a, **k: True,
            "validate_environment": lambda *a, **k: True,
        }
        stubs.update(overrides)
        stream = io.StringIO()
        patchers = [
            patch.object(env_life, name, value) for name, value in stubs.items()
        ]
        # 本机不一定装 uv/conda；只影响环境探测，不执行真实安装。
        patchers.append(
            patch.object(env_life.shutil, "which", return_value="/usr/bin/uv")
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        with redirect_stdout(stream):
            result = env_life.transactional_install_or_update(
                "pdf2zh", env_tool="uv"
            )
        return result, stream.getvalue()

    def test_no_usable_source_prints_fork_fallback_hint(self):
        result, output = self._run(choose_install_sources=lambda *a, **k: [])

        self.assertFalse(result[0])
        self.assertIn("没有可验证且能解析完整依赖的下载源", output)
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)
        self.assertIn(env_life.PDF2ZH_PYPI_FALLBACK_SPEC, output)

    def test_install_failure_prints_fork_fallback_hint(self):
        result, output = self._run(_run_install=lambda *a, **k: False)

        self.assertFalse(result[0])
        self.assertIn("fork 源", output)
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)

    def test_validation_failure_prints_fork_fallback_hint(self):
        result, output = self._run(validate_environment=lambda *a, **k: False)

        self.assertFalse(result[0])
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)

    def test_unexpected_exception_prints_hint_and_reports_no_environment(self):
        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        result, output = self._run(_cleanup_legacy_sidecar_environments=boom)

        self.assertFalse(result[0])
        self.assertIsNone(result[2])
        self.assertIn("disk full", output)
        self.assertIn("当前没有可用翻译环境", output)
        self.assertIn(env_life.PDF2ZH_SPEC_ENV, output)

    def test_pypi_fallback_failure_prints_no_fork_hint(self):
        result, output = self._run(
            load_requirements=lambda *a, **k: (
                "3.12",
                [env_life.PDF2ZH_PYPI_FALLBACK_SPEC],
            ),
            choose_install_sources=lambda *a, **k: [],
        )

        self.assertFalse(result[0])
        self.assertNotIn("fork 源", output)


if __name__ == "__main__":
    unittest.main()