from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .inputs import normalize_doi, normalize_title, title_similarity
from .pdf_validation import validate_pdf


class EndNoteError(RuntimeError):
    pass


def _plain(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def write_enw(path: Path, metadata: dict[str, Any]) -> Path:
    reference_type = "Journal Article"
    if metadata.get("type") in {"book", "monograph"}:
        reference_type = "Book"
    lines = [f"%0 {reference_type}"]
    if metadata.get("title"):
        lines.append(f"%T {_plain(metadata['title'])}")
    for author in metadata.get("authors") or []:
        lines.append(f"%A {_plain(author)}")
    if metadata.get("year"):
        lines.append(f"%D {_plain(metadata['year'])}")
    if metadata.get("journal"):
        lines.append(f"%J {_plain(metadata['journal'])}")
    if metadata.get("volume"):
        lines.append(f"%V {_plain(metadata['volume'])}")
    if metadata.get("issue"):
        lines.append(f"%N {_plain(metadata['issue'])}")
    if metadata.get("pages"):
        lines.append(f"%P {_plain(metadata['pages'])}")
    if metadata.get("doi"):
        lines.append(f"%R {_plain(metadata['doi'])}")
    if metadata.get("url"):
        lines.append(f"%U {_plain(metadata['url'])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")
    return path


@dataclass(frozen=True)
class EndNoteRecord:
    record_number: str | None
    doi: str | None
    title: str
    year: int | None
    authors: list[str]
    attachments: list[str]


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def parse_endnote_xml(path: Path) -> list[EndNoteRecord]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    records: list[EndNoteRecord] = []
    for record in root.findall(".//record"):
        record_number = _text(record.find("./rec-number")) or None
        title = _text(record.find("./titles/title")) or _text(record.find("./title"))
        doi_text = _text(record.find("./electronic-resource-num"))
        doi = normalize_doi(doi_text)
        year_text = _text(record.find("./dates/year")) or _text(record.find("./year"))
        year_match = re.search(r"\b(?:18|19|20|21)\d{2}\b", year_text)
        authors = [_text(item) for item in record.findall("./contributors/authors/author") if _text(item)]
        attachments: list[str] = []
        for item in record.findall("./urls/pdf-urls/url") + record.findall("./urls/related-urls/url"):
            value = _text(item)
            if value:
                attachments.append(value)
        records.append(
            EndNoteRecord(
                record_number=record_number,
                doi=doi,
                title=title,
                year=int(year_match.group(0)) if year_match else None,
                authors=authors,
                attachments=attachments,
            )
        )
    return records


def match_records(records: list[EndNoteRecord], metadata: dict[str, Any]) -> list[EndNoteRecord]:
    doi = normalize_doi(metadata.get("doi"))
    if doi:
        exact = [record for record in records if record.doi == doi]
        if exact:
            return exact
    title = metadata.get("title") or ""
    year = metadata.get("year")
    candidates = []
    for record in records:
        score = title_similarity(title, record.title)
        if score >= 0.88 and (not year or not record.year or int(year) == record.year):
            candidates.append(record)
    return candidates


def resolve_internal_attachment(library_path: Path, attachment: str) -> Path | None:
    attachment = unquote(attachment)
    if not attachment.casefold().startswith("internal-pdf://"):
        value = attachment.replace("file:///", "")
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        candidate = Path(value)
        return candidate if candidate.is_absolute() else None
    relative = attachment[len("internal-pdf://") :].lstrip("/\\").replace("/", os.sep)
    return library_path.with_suffix(".Data") / "PDF" / relative


def verified_main_attachments(
    library_path: Path, record: EndNoteRecord, metadata: dict[str, Any]
) -> list[Path]:
    """Return existing attachments that can safely satisfy the full-text requirement."""
    result: list[Path] = []
    for attachment in record.attachments:
        resolved = resolve_internal_attachment(library_path, attachment)
        if not resolved or not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
            continue
        validation = validate_pdf(
            resolved,
            expected_doi=metadata.get("doi"),
            expected_title=metadata.get("title"),
        )
        if validation.valid_pdf and validation.identity == "verified" and validation.role == "main":
            result.append(resolved)
    return result


class EndNoteAdapter:
    """Serializes all EndNote UI writes and verifies through exported XML.

    EndNote does not expose a supported public automation API. This adapter uses
    the native import and attachment UI, scoped to one explicitly opened library.
    """

    def __init__(self, endnote_exe: Path, generated_dir: Path, backup_dir: Path):
        self.endnote_exe = Path(endnote_exe)
        self.generated_dir = Path(generated_dir)
        self.backup_dir = Path(backup_dir)
        self._lock = threading.Lock()

    def probe(self) -> dict[str, Any]:
        process_ids = self.process_ids()
        visible_windows = self.visible_window_titles()
        result: dict[str, Any] = {
            "platform": os.name,
            "endnote_exe": str(self.endnote_exe),
            "endnote_exists": self.endnote_exe.exists(),
            "pywinauto": False,
            "ready": False,
            "process_ids": process_ids,
            "visible_windows": visible_windows,
            "details": [],
        }
        try:
            import pywinauto  # noqa: F401

            result["pywinauto"] = True
        except Exception as exc:
            result["details"].append(f"pywinauto 不可用：{exc}")
        if os.name != "nt":
            result["details"].append("EndNote 自动化仅支持 Windows")
        if not self.endnote_exe.exists():
            result["details"].append("未找到 EndNote.exe")
        if process_ids and not visible_windows:
            result["details"].append("EndNote 正在后台运行，但没有可控制的窗口；请在任务管理器中结束该后台进程后重试")
        result["ready"] = bool(
            os.name == "nt"
            and result["endnote_exists"]
            and result["pywinauto"]
            and not (process_ids and not visible_windows)
        )
        return result

    @staticmethod
    def validate_library_path(path: str, mode: str) -> Path:
        library = Path(path).expanduser().resolve()
        if library.suffix.casefold() != ".enl":
            raise EndNoteError("目标库必须使用 .enl 扩展名")
        if mode == "existing" and not library.is_file():
            raise EndNoteError(f"EndNote 库不存在：{library}")
        if mode == "new" and (library.exists() or library.with_suffix(".Data").exists()):
            raise EndNoteError(f"目标库或 .Data 目录已经存在：{library}")
        if not library.parent.exists():
            raise EndNoteError(f"目标目录不存在：{library.parent}")
        return library

    def create_filesystem_backup(self, library: Path, batch_id: str) -> Path:
        """Create a recoverable snapshot while EndNote is closed.

        The caller must use `is_endnote_running` first. Copying an open EndNote
        database could produce an inconsistent snapshot.
        """
        if self.is_endnote_running():
            raise EndNoteError("创建备份前请关闭 EndNote；已打开的库不能安全复制")
        if not library.exists():
            raise EndNoteError(f"无法备份不存在的库：{library}")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.backup_dir / f"{library.stem}-{timestamp}-{batch_id[:8]}.zip"
        data_dir = library.with_suffix(".Data")
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(library, library.name)
            if data_dir.is_dir():
                for child in data_dir.rglob("*"):
                    if child.is_file():
                        archive.write(child, str(Path(data_dir.name) / child.relative_to(data_dir)))
        return destination

    @staticmethod
    def is_endnote_running() -> bool:
        return bool(EndNoteAdapter.process_ids())

    @staticmethod
    def process_ids() -> list[int]:
        try:
            import win32api
            import win32con
            import win32process

            result: list[int] = []
            for pid in win32process.EnumProcesses():
                try:
                    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    try:
                        executable = win32process.GetModuleFileNameEx(handle, 0)
                    finally:
                        handle.Close()
                    if Path(executable).name.casefold() == "endnote.exe":
                        result.append(pid)
                except Exception:
                    continue
            return result
        except Exception:
            return []

    @staticmethod
    def visible_window_titles() -> list[str]:
        try:
            from pywinauto import Desktop

            return [
                window.window_text() for window in Desktop(backend="win32").windows()
                if "endnote" in window.window_text().casefold()
            ]
        except Exception:
            return []

    def _desktop(self):
        from pywinauto import Desktop

        return Desktop(backend="win32")

    @staticmethod
    def _main_windows(desktop) -> list[Any]:
        result: list[Any] = []
        for window in desktop.windows():
            try:
                if (
                    window.is_visible()
                    and window.class_name() != "#32770"
                    and re.match(
                        r"^EndNote(?:\s+\d+)?(?:\s+-|$)",
                        window.window_text(),
                        re.IGNORECASE,
                    )
                ):
                    result.append(window)
            except Exception:
                # EndNote recreates its frame while a library is loading.  A
                # wrapper returned by EnumWindows may therefore already be
                # stale by the time its properties are inspected.
                continue
        return result

    def _target_window(self, library: Path, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        stem = library.stem.casefold()
        while time.monotonic() < deadline:
            windows = self._main_windows(self._desktop())
            candidates = [w for w in windows if stem in w.window_text().casefold()]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise EndNoteError(f"发现多个同名 EndNote 窗口：{library.stem}")
            time.sleep(0.5)
        raise EndNoteError(f"无法确认目标 EndNote 库窗口：{library}")

    def _focus_target_window(self, library: Path, timeout: float = 30.0):
        """Reacquire the EndNote frame until a live handle accepts focus."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                window = self._target_window(library, timeout=min(2.0, max(0.5, deadline - time.monotonic())))
                window.set_focus()
                return window
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        raise EndNoteError(f"无法聚焦目标 EndNote 库窗口：{library}；{last_error}")

    def open_library(self, library: Path) -> None:
        if not self.endnote_exe.exists():
            raise EndNoteError(f"未找到 EndNote：{self.endnote_exe}")
        desktop = self._desktop()
        stem = library.stem.casefold()
        existing = [
            window for window in self._main_windows(desktop)
            if stem in window.window_text().casefold()
        ]
        if len(existing) == 1:
            self._focus_target_window(library)
            return
        if len(existing) > 1:
            raise EndNoteError(f"发现多个同名 EndNote 窗口：{library.stem}")
        other_windows = self._main_windows(desktop)
        if other_windows:
            titles = ", ".join(window.window_text() for window in other_windows)
            raise EndNoteError(f"写入前请关闭其他 EndNote 库窗口：{titles}")
        subprocess.Popen([str(self.endnote_exe), str(library)])
        self._focus_target_window(library)

    def create_library(self, library: Path) -> None:
        from pywinauto import Desktop, keyboard

        if library.exists() or library.with_suffix(".Data").exists():
            raise EndNoteError(f"不会覆盖现有库：{library}")
        if self.process_ids() and not self.visible_window_titles():
            raise EndNoteError("EndNote 后台进程没有可见窗口；请先在任务管理器中结束 EndNote.exe")
        desktop = Desktop(backend="win32")
        windows = self._main_windows(desktop)
        if len(windows) > 1:
            raise EndNoteError("创建新库前请只保留一个 EndNote 主窗口")
        window = windows[0] if windows else None
        if window is None:
            subprocess.Popen([str(self.endnote_exe)])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                windows = self._main_windows(desktop)
                if len(windows) == 1:
                    window = windows[0]
                    break
                if len(windows) > 1:
                    raise EndNoteError("EndNote 启动后出现多个主窗口，无法安全选择")
                time.sleep(0.5)
        if window is None:
            raise EndNoteError("EndNote 未能启动")
        window.set_focus()
        try:
            window.menu_select("File->New...")
        except Exception:
            keyboard.send_keys("%fn")
        dialog = Desktop(backend="win32").window(class_name="#32770")
        dialog.wait("visible", timeout=15)
        self._fill_file_dialog(dialog, library)
        dialog.child_window(title_re="Save|保存", class_name="Button").click()
        self._target_window(library)

    @staticmethod
    def _fill_file_dialog(dialog, path: Path) -> None:
        from pywinauto import keyboard

        edit = dialog.child_window(class_name="Edit")
        try:
            edit.set_edit_text(str(path))
        except Exception:
            dialog.set_focus()
            keyboard.send_keys("^a")
            keyboard.send_keys(str(path), with_spaces=True)

    def export_xml(self, library: Path, output: Path) -> Path:
        from pywinauto import Desktop, keyboard

        output.parent.mkdir(parents=True, exist_ok=True)
        window = self._focus_target_window(library)
        try:
            window.menu_select("File->Export...")
        except Exception:
            keyboard.send_keys("^e")
        dialog = Desktop(backend="win32").window(class_name="#32770")
        dialog.wait("visible", timeout=15)
        self._fill_file_dialog(dialog, output)
        combos = dialog.descendants(class_name="ComboBox")
        selected_xml = False
        for combo in combos:
            try:
                choices = combo.item_texts()
                xml_choice = next((item for item in choices if "XML" in item.upper()), None)
                if xml_choice:
                    combo.select(xml_choice)
                    selected_xml = True
                    break
            except Exception:
                continue
        if not selected_xml:
            raise EndNoteError("导出对话框中未找到 XML 文件类型")
        button = dialog.child_window(title_re="Save|保存", class_name="Button")
        button.click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not output.exists():
            time.sleep(0.25)
        if not output.exists():
            raise EndNoteError("EndNote XML 导出未生成文件")
        return output

    def import_enw(self, library: Path, enw_path: Path) -> None:
        """Import one tagged record into the explicitly opened library."""
        self._focus_target_window(library)
        subprocess.Popen([str(self.endnote_exe), str(enw_path)])
        time.sleep(2.0)
        self._focus_target_window(library)

    def select_reference(self, library: Path, query: str) -> None:
        from pywinauto import keyboard

        window = self._focus_target_window(library)
        keyboard.send_keys("^f")
        time.sleep(0.3)
        keyboard.send_keys("^a")
        keyboard.send_keys(query, with_spaces=True)
        keyboard.send_keys("{ENTER}")
        time.sleep(1.0)
        keyboard.send_keys("^a")

    def attach_pdf(self, library: Path, query: str, pdf_path: Path) -> None:
        from pywinauto import Desktop, keyboard

        self.select_reference(library, query)
        window = self._focus_target_window(library)
        try:
            window.menu_select("References->File Attachments->Attach File...")
        except Exception as exc:
            raise EndNoteError("无法打开 EndNote 的 Attach File 对话框") from exc
        dialog = Desktop(backend="win32").window(class_name="#32770")
        dialog.wait("visible", timeout=15)
        self._fill_file_dialog(dialog, pdf_path)
        for checkbox in dialog.descendants(class_name="Button"):
            try:
                if "copy this file" in checkbox.window_text().casefold() and checkbox.get_check_state() == 0:
                    checkbox.click()
            except Exception:
                continue
        open_button = dialog.child_window(title_re="Open|打开", class_name="Button")
        open_button.click()
        time.sleep(1.0)

    def prepare_index(self, library: Path, batch_id: str) -> tuple[Path, list[EndNoteRecord]]:
        output = self.generated_dir / batch_id / f"index-{uuid.uuid4().hex}.xml"
        self.export_xml(library, output)
        return output, parse_endnote_xml(output)

    def commit_paper(self, library: Path, paper_id: str, metadata: dict[str, Any], pdf_path: Path | None) -> dict[str, Any]:
        """Commit one paper and return verified state.

        This method is intentionally serialized because EndNote UI operations
        and SQLite state cannot be committed atomically.
        """
        with self._lock:
            before_path, before = self.prepare_index(library, paper_id)
            existing = match_records(before, metadata)
            if len(existing) > 1:
                raise EndNoteError("目标库中有多个匹配题录，需要人工选择")
            created = False
            if not existing:
                enw = write_enw(self.generated_dir / paper_id / "record.enw", metadata)
                self.import_enw(library, enw)
                created = True
            after_meta_path, after_meta = self.prepare_index(library, paper_id)
            matched = match_records(after_meta, metadata)
            if len(matched) != 1:
                raise EndNoteError(f"导入后无法唯一定位题录（匹配数：{len(matched)}）")
            record = matched[0]
            existing_main = verified_main_attachments(library, record, metadata)
            attached = False
            if pdf_path and not existing_main:
                wanted_hash = _sha256(pdf_path)
                already = False
                for attachment in record.attachments:
                    resolved = resolve_internal_attachment(library, attachment)
                    if resolved and resolved.is_file() and _sha256(resolved) == wanted_hash:
                        already = True
                        break
                if not already:
                    self.attach_pdf(library, metadata.get("doi") or metadata.get("title") or "", pdf_path)
                    attached = True
            final_path, final_records = self.prepare_index(library, paper_id)
            final_match = match_records(final_records, metadata)
            if len(final_match) != 1:
                raise EndNoteError("最终核验无法唯一定位题录")
            final = final_match[0]
            if pdf_path and not existing_main:
                wanted_hash = _sha256(pdf_path)
                verified_attachment = False
                for attachment in final.attachments:
                    resolved = resolve_internal_attachment(library, attachment)
                    if resolved and resolved.is_file() and _sha256(resolved) == wanted_hash:
                        verified_attachment = True
                        break
                if not verified_attachment:
                    raise EndNoteError("最终核验未找到本次 PDF 的库内相对附件")
            return {
                "created": created,
                "attached": attached,
                "existing_full_text": bool(existing_main),
                "existing_full_text_paths": [str(path) for path in existing_main],
                "record_number": final.record_number,
                "attachments": final.attachments,
                "xml_snapshot": str(final_path),
            }


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
