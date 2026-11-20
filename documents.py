"""Extract searchable text from local documents."""
from html.parser import HTMLParser
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

LIMIT = 8_000_000
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def extract(path):
    with path.open("rb") as file:
        data = file.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Файл превышает 8 МБ")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("PDF защищён паролем")
        if len(reader.pages) > 500:
            raise ValueError("PDF превышает 500 страниц")
        parts = []
        metadata = reader.metadata
        if metadata:
            for key in ("/Title", "/Author", "/CreationDate"):
                if metadata.get(key):
                    parts.append(f"{key}: {metadata[key]}")
        for index, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(f"Страница {index}\n{text}")
        if not any(part.startswith("Страница ") for part in parts):
            raise ValueError("PDF не содержит текстового слоя")
        return "\n\n".join(parts)
    if suffix == ".docx":
        with ZipFile(BytesIO(data)) as archive:
            name = "word/document.xml"
            if archive.getinfo(name).file_size > LIMIT:
                raise ValueError("Развёрнутый текст DOCX превышает 8 МБ")
            root = ElementTree.fromstring(archive.read(name))
        return "\n".join(
            "".join(node.text or "" for node in paragraph.iter(WORD + "t"))
            for paragraph in root.iter(WORD + "p")
        )
    text = data.decode("utf-8-sig")
    if suffix == ".html":
        parser = HTMLText()
        parser.feed(text)
        parser.close()
        return "\n".join(parser.parts)
    return text
