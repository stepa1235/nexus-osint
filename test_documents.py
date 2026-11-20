import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from documents import WORD, extract


class DocumentsTests(unittest.TestCase):
    def test_html_extracts_visible_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.html"
            path.write_text("<h1>Source &amp; title</h1><script>ignored</script>", encoding="utf-8")
            self.assertEqual(extract(path), "Source & title")

    def test_docx_keeps_paragraph_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.docx"
            with ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD[1:-1]}">'
                                 "<w:p><w:r><w:t>Публичный источник</w:t></w:r></w:p></w:document>")
            self.assertEqual(extract(path), "Публичный источник")

    def test_pdf_extracts_numbered_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=200, height=200)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/F1"): DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    })
                })
            })
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 10 100 Td (Public report) Tj ET")
            page[NameObject("/Contents")] = stream
            writer.write(path)
            self.assertIn("Страница 1\nPublic report", extract(path))
