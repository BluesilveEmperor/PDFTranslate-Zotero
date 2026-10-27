"""配置模板定位与迁移测试。

本次改动把运行期配置从 ``server/config`` 迁到 ``<程序根>/config/pdf-cache``，
模板 ``*.example`` 仍留在 ``server/config``。因此 ``_example_path`` 必须：
1. 优先使用与实际配置同目录的 ``*.example``（旧布局 / 单元测试）；
2. 找不到时回退到随包分发的模板目录；
3. 迁移只补充缺失默认值、保留用户值，非法文件先备份再恢复默认。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import toml

from utils import config_migration
from utils.config_migration import (
    _example_path,
    migrate_config_file,
    prepare_config_files,
)


class ExamplePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="cfg-template-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.root = Path(self._temp_dir)

    def test_prefers_sibling_template(self):
        active = self.root / "config.json"
        sibling = self.root / "config.json.example"
        sibling.write_text("{}", encoding="utf-8")

        self.assertEqual(_example_path(active), sibling)

    def test_falls_back_to_shipped_template_dir(self):
        active = self.root / "config.json"
        shipped = self.root / "templates" / "config.json.example"
        shipped.parent.mkdir()
        shipped.write_text("{}", encoding="utf-8")

        with patch.object(config_migration, "template_dir", return_value=shipped.parent):
            self.assertEqual(_example_path(active), shipped)

    def test_runtime_cache_path_resolves_to_server_template(self):
        # 运行期配置位于 <root>/config/pdf-cache，模板位于 <server>/config。
        active = Path(self._temp_dir) / "config" / "pdf-cache" / "venv.json"
        resolved = _example_path(active)

        self.assertEqual(resolved.name, "venv.json.example")
        self.assertEqual(resolved.parent, config_migration.template_dir())


class MigrateConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.mkdtemp(prefix="cfg-migrate-")
        self.addCleanup(shutil.rmtree, self._temp_dir, ignore_errors=True)
        self.root = Path(self._temp_dir)

    def _run(self, path: Path) -> str:
        stream = StringIO()
        with redirect_stdout(stream):
            migrate_config_file(path)
        return stream.getvalue()

    def test_missing_template_and_config_is_an_error(self):
        with patch.object(
            config_migration, "template_dir", return_value=self.root / "no-templates"
        ):
            with self.assertRaises(FileNotFoundError) as ctx:
                migrate_config_file(self.root / "config.json")
        self.assertIn("缺少配置文件及模板", str(ctx.exception))

    def test_missing_template_keeps_existing_config(self):
        active = self.root / "config.json"
        active.write_text('{"user": 1}', encoding="utf-8")

        with patch.object(
            config_migration, "template_dir", return_value=self.root / "no-templates"
        ):
            self._run(active)

        self.assertEqual(active.read_text(encoding="utf-8"), '{"user": 1}')

    def test_json_config_is_created_from_template(self):
        (self.root / "config.json.example").write_text(
            json.dumps({"engine": "pdf2zh"}), encoding="utf-8"
        )

        self._run(self.root / "config.json")

        self.assertEqual(
            json.loads((self.root / "config.json").read_text(encoding="utf-8")),
            {"engine": "pdf2zh"},
        )

    def test_json_migration_adds_missing_defaults_and_keeps_user_values(self):
        (self.root / "config.json.example").write_text(
            json.dumps({"engine": "pdf2zh", "newField": True}), encoding="utf-8"
        )
        (self.root / "config.json").write_text(
            json.dumps({"engine": "custom"}), encoding="utf-8"
        )

        output = self._run(self.root / "config.json")

        merged = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["engine"], "custom")
        self.assertIs(merged["newField"], True)
        self.assertIn("已迁移托管默认", output)

    def test_invalid_json_is_backed_up_and_reset_to_defaults(self):
        (self.root / "config.json.example").write_text(
            json.dumps({"engine": "pdf2zh"}), encoding="utf-8"
        )
        (self.root / "config.json").write_text("{not json", encoding="utf-8")

        output = self._run(self.root / "config.json")

        self.assertTrue((self.root / "config.json.invalid.bak").is_file())
        self.assertEqual(
            json.loads((self.root / "config.json").read_text(encoding="utf-8")),
            {"engine": "pdf2zh"},
        )
        self.assertIn("已备份", output)

    def test_toml_config_is_created_and_merged(self):
        (self.root / "config.toml.example").write_text(
            toml.dumps({"translation": {"lang": "en"}, "newField": 1}),
            encoding="utf-8",
        )

        self._run(self.root / "config.toml")
        self.assertEqual(toml.load(self.root / "config.toml")["newField"], 1)

        (self.root / "config.toml").write_text(
            toml.dumps({"translation": {"lang": "zh"}}), encoding="utf-8"
        )
        self._run(self.root / "config.toml")

        merged = toml.load(self.root / "config.toml")
        self.assertEqual(merged["translation"]["lang"], "zh")
        self.assertEqual(merged["newField"], 1)

    def test_invalid_toml_is_backed_up(self):
        (self.root / "config.toml.example").write_text(
            toml.dumps({"translation": {}}), encoding="utf-8"
        )
        (self.root / "config.toml").write_text("this is = = not toml", encoding="utf-8")

        self._run(self.root / "config.toml")

        self.assertTrue((self.root / "config.toml.invalid.bak").is_file())
        self.assertIn("translation", toml.load(self.root / "config.toml"))

    def test_unchanged_config_is_reported(self):
        (self.root / "config.json.example").write_text(
            json.dumps({"engine": "pdf2zh"}), encoding="utf-8"
        )
        (self.root / "config.json").write_text(
            json.dumps({"engine": "pdf2zh"}, indent=4) + "\n", encoding="utf-8"
        )

        output = self._run(self.root / "config.json")

        self.assertIn("无需修改", output)

    def test_prepare_config_files_migrates_every_entry(self):
        for name in ("config.json", "venv.json"):
            (self.root / f"{name}.example").write_text("{}", encoding="utf-8")

        with redirect_stdout(StringIO()):
            prepare_config_files(
                {
                    "pdf2zh": str(self.root / "config.json"),
                    "venv": str(self.root / "venv.json"),
                }
            )

        self.assertTrue((self.root / "config.json").is_file())
        self.assertTrue((self.root / "venv.json").is_file())


if __name__ == "__main__":
    unittest.main()