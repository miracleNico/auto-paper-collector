from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PaperActionMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "paper_endnote" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
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

    def test_institution_access_modes_have_dynamic_settings(self) -> None:
        self.assertIn('id="institution-access-type"', self.html)
        self.assertIn('value="ezproxy"', self.html)
        self.assertIn('value="carsi_saml"', self.html)
        self.assertIn('value="manual_browser"', self.html)
        self.assertIn('id="carsi-experimental"', self.html)
        self.assertIn('id="institution-publisher-login-urls"', self.html)
        self.assertIn("syncInstitutionFields", self.javascript)

    def test_institution_actions_use_resume_and_probe_endpoints(self) -> None:
        self.assertIn('paper.needs_action === "manual_institution"', self.javascript)
        self.assertIn('/continue-institution`', self.javascript)
        self.assertIn('"/api/institution/test-access"', self.javascript)
        self.assertIn(
            'publisher_login_urls: accessType === "carsi_saml" ? parsePublisherLoginUrls',
            self.javascript,
        )
        self.assertIn("{batch_id: batch.id}", self.javascript)

    def test_pdf_export_has_a_selectable_candidate_picker(self) -> None:
        self.assertIn('id="export-picker"', self.html)
        self.assertIn('id="export-select-all"', self.html)
        self.assertIn('id="export-selection-count"', self.html)
        self.assertIn('id="export-items"', self.html)
        self.assertIn('aria-label="选择要导出的论文"', self.html)
        self.assertIn("function renderExportItems(items)", self.javascript)
        self.assertIn("function selectedExportItemIds()", self.javascript)
        self.assertIn('data-export-item value="${esc(item.id)}"', self.javascript)
        self.assertIn('"/api/tools/export-pdfs/candidates"', self.javascript)
        self.assertIn("item_ids: itemIds", self.javascript)

    def test_pdf_tools_can_select_endnote_and_zotero_libraries(self) -> None:
        for prefix in ("rename", "export"):
            self.assertIn(f'id="{prefix}-zotero-library"', self.html)
            self.assertIn(f'id="{prefix}-endnote-library"', self.html)
            self.assertIn(f'id="pick-{prefix}-endnote-library"', self.html)
        self.assertNotIn('id="rename-endnote-library" readonly', self.html)
        self.assertIn('id="export-endnote-wrap"', self.html)
        self.assertIn("function selectedZoteroScope(prefix)", self.javascript)
        self.assertIn("function loadZoteroCollections(prefix, supplied = null)", self.javascript)
        self.assertIn('"/api/tools/pick-endnote-library"', self.javascript)
        self.assertIn("zotero_library_id", self.javascript)
        self.assertIn("collection_key", self.javascript)
        self.assertIn("whole_library", self.javascript)
        self.assertIn("endnote_library", self.javascript)
        self.assertIn('data-scope-placeholder="true"', self.javascript)
        self.assertIn('data-whole-library="true"', self.javascript)
        self.assertIn('id="rename-collection" disabled', self.html)
        self.assertIn('type="submit" disabled>开始重命名', self.html)
        self.assertIn('id="rename-scheme"', self.html)
        self.assertIn('value="year_author_title"', self.html)
        self.assertIn('value="title_only"', self.html)
        self.assertIn('rename_scheme: $("#rename-scheme").value', self.javascript)

    def test_pdf_export_ignores_stale_candidate_requests(self) -> None:
        self.assertIn("exportRequestToken: 0", self.javascript)
        self.assertIn("exportController: null", self.javascript)
        self.assertIn("state.exportController?.abort()", self.javascript)
        self.assertIn("const controller = new AbortController()", self.javascript)
        self.assertIn("signal: controller.signal", self.javascript)
        self.assertIn("token !== state.exportRequestToken", self.javascript)

    def test_tool_sources_ignore_stale_responses(self) -> None:
        self.assertIn("toolSourcesRequestToken: 0", self.javascript)
        self.assertIn("toolSourcesController: null", self.javascript)
        self.assertIn("state.toolSourcesController?.abort()", self.javascript)
        self.assertIn("token !== state.toolSourcesRequestToken", self.javascript)

    def test_header_has_confirmed_local_service_shutdown(self) -> None:
        self.assertIn('id="shutdown-service"', self.html)
        self.assertIn('class="danger header-shutdown"', self.html)
        self.assertIn('window.confirm("关闭本地论文收藏夹服务？', self.javascript)
        self.assertIn('"/api/system/shutdown"', self.javascript)

    def test_endnote_to_zotero_sync_form_uses_sync_endpoint(self) -> None:
        self.assertIn('id="sync-endnote-zotero-form"', self.html)
        self.assertIn("EndNote → Zotero", self.html)
        self.assertIn('id="sync-endnote-library"', self.html)
        self.assertIn('id="sync-zotero-collection"', self.html)
        self.assertIn('id="sync-endnote-report"', self.html)
        self.assertIn(
            '$("#sync-endnote-zotero-form").addEventListener("submit"',
            self.javascript,
        )
        self.assertIn('"/api/tools/sync-endnote-zotero"', self.javascript)
        self.assertIn("body: JSON.stringify({collection})", self.javascript)
        self.assertIn("其他题录类型会列为跳过", self.html)
        self.assertIn('row.status === "skipped"', self.javascript)
        self.assertIn("skippedDetails", self.javascript)


if __name__ == "__main__":
    unittest.main()
