"""``utils.app_paths`` 单元测试。

覆盖运行期配置目录从 ``server/config`` 迁到 ``<程序根>/config/pdf-cache`` 后
的四条路径：
1. ``app_root`` 的 ``PDF2ZH_HOME`` 覆盖与默认（源码布局）推导；
2. ``template_dir`` 固定指向随包分发的 ``<server>/config``（不受 HOME 影响）；
3. ``cache_dir`` 的创建 / 进程内缓存 / 目录被外部删除后的重建；
4. ``ensure_hidden`` 在各平台的 best-effort 行为；
5. ``migrate_legacy_configs`` 的「只复制不覆盖、只复制不删除」语义。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from utils import app_paths
from utils.app_paths import (
    CONFIG_FILE_NAMES,
    ENV_APP_HOME,
    app_root,
    cache_dir,
    ensure_hidden,
    migrate_legacy_configs,
    template_dir,
)

SERVER_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _without_home_env():
    """临时移除 ``PDF2ZH_HOME``，模拟用户未设置覆盖变量的场景。"""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(ENV_APP_HOME, None)
        yield


class AppRootTests(unittest.TestCase):
    def setUp(self) -> None:
        app_paths._cache_dir_cache.clear()

    def tearDown(self) -> None:
        app_paths._cache_dir_cache.clear()

    def test_defaults_to_server_source_directory(self):
        with _without_home_env():
            self.assertEqual(app_root(), SERVER_ROOT)

    def test_env_override_wins_and_is_absolutised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {ENV_APP_HOME: temp_dir}, clear=False):
                self.assertEqual(app_root(), Path(temp_dir).resolve())

    def test_env_override_expands_user_home(self):
        fake_home = Path(tempfile.mkdtemp(prefix="fake-home-"))
        try:
            with patch.dict(
                os.environ,
                {ENV_APP_HOME: os.path.join("~", "portable-root")},
                clear=False,
            ), patch("utils.app_paths.Path.expanduser", autospec=True) as expand:
                expand.return_value = fake_home / "portable-root"
                root = app_root()
            self.assertEqual(root, (fake_home / "portable-root").resolve())
            expand.assert_called_once()
        finally:
            shutil.rmtree(fake_home, ignore_errors=True)

    def test_template_dir_stays_next_to_server_sources(self):
        # 模板随发布包分发，即使运行期配置被重定向也必须留在 <server>/config。
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {ENV_APP_HOME: temp_dir}, clear=False):
                self.assertEqual(template_dir(), SERVER_ROOT / "config")


class CacheDirTests(unittest.TestCase):
    def setUp(self) -> None:
        app_paths._cache_dir_cache.clear()
        self._temp_dir = tempfile.mkdtemp(prefix="app-paths-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.addCleanup(app_paths._cache_dir_cache.clear)
        patcher = patch.dict(os.environ, {ENV_APP_HOME: self._temp_dir}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_hidden_config_pdf_cache_directory(self):
        target = cache_dir()

        self.assertEqual(target, Path(self._temp_dir).resolve() / "config" / "pdf-cache")
        self.assertTrue(target.is_dir())
        self.assertTrue((target.parent).is_dir())

    def test_second_call_reuses_cached_path_without_recreating(self):
        first = cache_dir()
        with patch.object(app_paths, "ensure_hidden") as hidden, patch(
            "utils.app_paths.Path.mkdir"
        ) as mkdir:
            second = cache_dir()

        self.assertEqual(first, second)
        hidden.assert_not_called()
        mkdir.assert_not_called()

    def test_recreates_directory_deleted_at_runtime(self):
        target = cache_dir()
        shutil.rmtree(target)
        self.assertFalse(target.exists())

        again = cache_dir()

        self.assertEqual(again, target)
        self.assertTrue(target.is_dir())

    def test_sets_hidden_attribute_for_both_levels(self):
        with patch.object(app_paths, "ensure_hidden") as hidden:
            target = cache_dir()

        touched = {Path(call.args[0]) for call in hidden.call_args_list}
        self.assertEqual(touched, {target.parent, target})

    def test_different_roots_do_not_share_cached_paths(self):
        first = cache_dir()
        other = tempfile.mkdtemp(prefix="app-paths-other-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)

        with patch.dict(os.environ, {ENV_APP_HOME: other}, clear=False):
            second = cache_dir()

        self.assertNotEqual(first, second)
        self.assertEqual(second, Path(other).resolve() / "config" / "pdf-cache")
        self.assertTrue(second.is_dir())


class EnsureHiddenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="hidden-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.file = Path(self._temp_dir) / "config.json"
        self.file.write_text("{}", encoding="utf-8")

    def _fake_ctypes(self, attributes):
        kernel32 = MagicMock()
        kernel32.GetFileAttributesW.return_value = attributes
        module = MagicMock()
        module.windll.kernel32 = kernel32
        return module, kernel32

    def test_disabled_on_non_windows_platforms(self):
        fake, kernel32 = self._fake_ctypes(0x20)
        with patch.object(app_paths, "_is_windows", return_value=False), patch.dict(
            sys.modules, {"ctypes": fake}
        ):
            ensure_hidden(self.file)

        kernel32.GetFileAttributesW.assert_not_called()
        kernel32.SetFileAttributesW.assert_not_called()

    def test_adds_hidden_bit_without_dropping_existing_attributes(self):
        fake, kernel32 = self._fake_ctypes(0x20)
        with patch.object(app_paths, "_is_windows", return_value=True), patch.dict(
            sys.modules, {"ctypes": fake}
        ):
            ensure_hidden(self.file)

        kernel32.SetFileAttributesW.assert_called_once_with(str(self.file), 0x22)

    def test_skips_file_already_marked_hidden(self):
        fake, kernel32 = self._fake_ctypes(0x22)
        with patch.object(app_paths, "_is_windows", return_value=True), patch.dict(
            sys.modules, {"ctypes": fake}
        ):
            ensure_hidden(self.file)

        kernel32.SetFileAttributesW.assert_not_called()

    def test_skips_path_whose_attributes_cannot_be_read(self):
        fake, kernel32 = self._fake_ctypes(0xFFFFFFFF)
        with patch.object(app_paths, "_is_windows", return_value=True), patch.dict(
            sys.modules, {"ctypes": fake}
        ):
            ensure_hidden(self.file)

        kernel32.SetFileAttributesW.assert_not_called()

    def test_missing_path_is_a_noop(self):
        fake, kernel32 = self._fake_ctypes(0x20)
        with patch.object(app_paths, "_is_windows", return_value=True), patch.dict(
            sys.modules, {"ctypes": fake}
        ):
            ensure_hidden(Path(self._temp_dir) / "does-not-exist.json")

        kernel32.GetFileAttributesW.assert_not_called()

    def test_native_failure_never_propagates(self):
        fake, kernel32 = self._fake_ctypes(0x20)
        kernel32.GetFileAttributesW.side_effect = OSError("kernel32 unavailable")
        with patch.object(app_paths, "_is_windows", return_value=True), patch.dict(
            sys.modules, {"ctypes": fake}
        ), self.assertLogs("utils.app_paths", level="DEBUG") as logs:
            ensure_hidden(self.file)

        self.assertTrue(any("设置隐藏属性失败" in line for line in logs.output))


class MigrateLegacyConfigsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.legacy = Path(tempfile.mkdtemp(prefix="legacy-config-"))
        self.target = Path(tempfile.mkdtemp(prefix="target-config-"))
        self.addCleanup(shutil.rmtree, self.legacy, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.target, ignore_errors=True)

    def _write(self, root: Path, name: str, content: str) -> Path:
        path = root / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_copies_existing_configs_and_keeps_legacy_files(self):
        self._write(self.legacy, "config.json", "legacy-json")
        self._write(self.legacy, "venv.json", "legacy-venv")
        self._write(self.legacy, "notes.txt", "not-managed")

        migrated = migrate_legacy_configs(self.legacy, self.target)

        self.assertEqual(
            {path.name for path in migrated}, {"config.json", "venv.json"}
        )
        for name, expected in (
            ("config.json", "legacy-json"),
            ("venv.json", "legacy-venv"),
        ):
            self.assertEqual(
                (self.target / name).read_text(encoding="utf-8"), expected
            )
            # 只复制、不移动：旧版 Server 或用户回滚时仍能读到原配置。
            self.assertTrue((self.legacy / name).is_file())
        self.assertFalse((self.target / "notes.txt").exists())

    def test_never_overwrites_config_that_already_exists(self):
        self._write(self.legacy, "config.json", "legacy-json")
        self._write(self.target, "config.json", "current-json")

        migrated = migrate_legacy_configs(self.legacy, self.target)

        self.assertEqual(migrated, [])
        self.assertEqual(
            (self.target / "config.json").read_text(encoding="utf-8"), "current-json"
        )

    def test_restricts_migration_to_requested_names(self):
        self._write(self.legacy, "config.json", "legacy-json")
        self._write(self.legacy, "config.toml", "legacy-toml")

        migrated = migrate_legacy_configs(
            self.legacy, self.target, names=("config.toml",)
        )

        self.assertEqual([path.name for path in migrated], ["config.toml"])
        self.assertFalse((self.target / "config.json").exists())

    def test_missing_legacy_dir_yields_nothing(self):
        migrated = migrate_legacy_configs(
            self.legacy / "not-there", self.target
        )
        self.assertEqual(migrated, [])

    def test_same_directory_is_skipped(self):
        self._write(self.legacy, "config.json", "legacy-json")

        migrated = migrate_legacy_configs(self.legacy, self.legacy)

        self.assertEqual(migrated, [])

    def test_copy_error_is_logged_and_other_files_still_migrate(self):
        self._write(self.legacy, "config.json", "legacy-json")
        self._write(self.legacy, "venv.json", "legacy-venv")
        real_copy = shutil.copy2

        def flaky_copy(source, destination, *args, **kwargs):
            if Path(source).name == "config.json":
                raise OSError("locked by another process")
            return real_copy(source, destination, *args, **kwargs)

        with patch("utils.app_paths.shutil.copy2", side_effect=flaky_copy), self.assertLogs(
            "utils.app_paths", level="WARNING"
        ):
            migrated = migrate_legacy_configs(self.legacy, self.target)

        self.assertEqual([path.name for path in migrated], ["venv.json"])
        self.assertFalse((self.target / "config.json").exists())
        self.assertTrue((self.target / "venv.json").is_file())

    def test_default_target_is_runtime_cache_dir(self):
        self._write(self.legacy, "config.json", "legacy-json")
        temp_home = tempfile.mkdtemp(prefix="home-")
        self.addCleanup(shutil.rmtree, temp_home, ignore_errors=True)
        app_paths._cache_dir_cache.clear()
        self.addCleanup(app_paths._cache_dir_cache.clear)

        with patch.dict(os.environ, {ENV_APP_HOME: temp_home}, clear=False):
            migrated = migrate_legacy_configs(self.legacy)

        self.assertEqual(len(migrated), 1)
        self.assertEqual(
            Path(migrated[0]).parent,
            Path(temp_home).resolve() / "config" / "pdf-cache",
        )

    def test_managed_config_names_match_runtime_contract(self):
        self.assertEqual(
            set(CONFIG_FILE_NAMES), {"config.json", "config.toml", "venv.json"}
        )


if __name__ == "__main__":
    unittest.main()