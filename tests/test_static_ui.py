from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PaperActionMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.javascript = (ROOT / "paper_endnote" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        cls.styles = (ROOT / "paper_endnote" / "static" / "style.css").read_text(
            encoding="utf-8"
        )

    def test_more_actions_has_an_explicit_close_control(self) -> None:
        self.assertIn('class="paper-more-actions"', self.javascript)
        self.assertIn('class="paper-more-close"', self.javascript)
        self.assertIn('aria-label="关闭更多操作菜单"', self.javascript)
        self.assertIn('menu.querySelector("summary")?.focus()', self.javascript)

    def test_polling_preserves_an_open_paper_menu(self) -> None:
        self.assertIn("#task-detail .paper-more-actions[open]", self.javascript)
        self.assertIn(".paper-more-menu {", self.styles)


if __name__ == "__main__":
    unittest.main()
