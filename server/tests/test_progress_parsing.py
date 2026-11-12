"""``utils.execute._parse_progress`` 对 rich 进度行的解析与回落行为。

pdf2zh 2.x 的 rich 进度条输出形如 ``阶段名 ━━━━━ 12/40``，纯文本经 ANSI
剥离后即可解析；不认识的行必须继续走既有 tqdm / main / legacy 回落规则。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from utils import execute


class ParseProgressTest(unittest.TestCase):
    def _updates(self, text):
        with patch.object(execute.task_manager, "update_task") as update:
            execute._parse_progress(text, "task-1")
        return update

    def test_rich_line_reports_stage_and_percent(self):
        update = self._updates("  Parsing PDF ━━━━━━━━━━━ 12/40 \r\n")

        update.assert_called_once_with("task-1", {
            "progress": 30,
            "status": "running",
            "message": "Parsing PDF",
        })

    def test_rich_line_uses_last_match_and_caps_at_99(self):
        text = (
            "  Parse 1/4 ━━━ 1/4 \n"
            "  Translate 4/4 ━━━ 4/4 \n"
        )

        update = self._updates(text)

        update.assert_called_once_with("task-1", {
            "progress": 99,
            "status": "running",
            "message": "Translate 4/4",
        })

    def test_rich_line_with_zero_total_is_skipped(self):
        update = self._updates("  Pending ━━━ 0/0 \n")

        update.assert_not_called()

    def test_non_rich_line_falls_back_to_main_progress(self):
        update = self._updates("translate 5/10\n")

        update.assert_called_once_with("task-1", {
            "progress": 50,
            "status": "running",
            "message": "translate 5/10",
        })

    def test_unmatched_text_does_not_update_task(self):
        update = self._updates("starting up, please wait\n")

        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()