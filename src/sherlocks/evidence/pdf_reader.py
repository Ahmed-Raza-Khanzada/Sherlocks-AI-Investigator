"""Read a PDF report: its text, and the pictures in it.

Lab reports and CRO dossiers arrive as PDFs. Some carry a text layer; many are scans
(a photo of a signed report). Per page:

1. the text layer (pypdf);
2. a page with (almost) no text is a scan: the vision-language model on the LAN (the
   office Qwen server, ``llm.vision``) reads the page image - the scan embedded in the
   page, or the page rendered with ``pdftoppm`` - Urdu and English alike. No separate
   OCR engine.

A page none of these could read is reported as unread - never guessed. Embedded images
(CRO poses, fingerprints, the scan itself) are returned so they can be shown and put in
the case report.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A page with fewer characters than this in its text layer is treated as a scan.
_MIN_TEXT = 40
# Embedded images smaller than this (logos, rules, QR codes) are not evidence.
_MIN_IMAGE_BYTES = 1_000
_MIN_IMAGE_SIDE = 60
_MAX_IMAGES = 24
_MAX_PAGES = 30
# Scanned pages read by the vision model per PDF (each is one model call).
_MAX_VISION_PAGES = 10
# An embedded image this large on a text-less page is the scan of that page.
_MIN_SCAN_SIDE = 500


@dataclass
class PdfImage:
    data: bytes
    ext: str
    page: int
    width: int = 0
    height: int = 0


@dataclass
class PdfPage:
    number: int
    text: str
    method: str  # "text" | "ocr" | "vision" | "unread"


@dataclass
class PdfContent:
    pages: list[PdfPage] = field(default_factory=list)
    images: list[PdfImage] = field(default_factory=list)
    error: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(f"[page {p.number}] {p.text}" for p in self.pages if p.text)

    @property
    def unread_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.method == "unread"]

    def summary(self) -> str:
        methods: dict[str, int] = {}
        for p in self.pages:
            methods[p.method] = methods.get(p.method, 0) + 1
        how = ", ".join(f"{n} by {m}" for m, n in methods.items())
        return f"{len(self.pages)} page(s) ({how}), {len(self.images)} picture(s)"


def _render_page(pdf: bytes, number: int, dpi: int = 150) -> bytes | None:
    """One page as PNG, via poppler's pdftoppm."""
    if not shutil.which("pdftoppm"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(pdf)
        try:
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(number), "-l", str(number),
                            "-singlefile", str(src), str(Path(tmp) / "page")],
                           check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.info("pdftoppm failed on page %s: %s", number, exc)
            return None
        out = Path(tmp) / "page.png"
        return out.read_bytes() if out.exists() else None


_VISION_PROMPT = ("Transcribe all text on this page of a police / forensic laboratory document (English "
                  "and/or Urdu), exactly as written, line by line, in its own script. Keep numbers, names, dates and results verbatim. Do not summarise "
                  "or add anything.")


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001 - an undecodable image is simply not sized
        return 0, 0


def _as_web_image(data: bytes, name: str) -> tuple[bytes, str] | None:
    """Embedded images come as JPEG, JPEG2000, PNG or raw bitmaps; browsers and the
    report want JPEG/PNG."""
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext in ("jpg", "jpeg") or data[:3] == b"\xff\xd8\xff":
        return data, "jpg"
    if ext == "png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "png"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=88)
            return buf.getvalue(), "jpg"
    except Exception:  # noqa: BLE001
        return None


def _read_scan(vision: Any, image: bytes, mime: str, page: int) -> str:
    try:
        return (vision.read_image(image, _VISION_PROMPT, mime=mime) or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unread page is reported, not guessed
        logger.info("Vision model could not read page %s: %s", page, exc)
        return ""


def read_pdf(pdf: bytes, *, vision: Any = None, max_pages: int = _MAX_PAGES,
             max_vision_pages: int = _MAX_VISION_PAGES) -> PdfContent:
    """Text and pictures of ``pdf``. ``vision`` is an LLM client with ``read_image``
    (or None: scanned pages are then reported unread)."""
    from pypdf import PdfReader

    out = PdfContent()
    try:
        reader = PdfReader(io.BytesIO(pdf))
    except Exception as exc:  # noqa: BLE001 - a corrupt file is reported, not raised
        out.error = f"Not a readable PDF: {type(exc).__name__}: {exc}"
        return out

    vision_left = max_vision_pages
    for index, page in enumerate(reader.pages[:max_pages], 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        pictures: list[PdfImage] = []
        try:
            images = list(page.images)
        except Exception:  # noqa: BLE001 - an image filter pypdf cannot decode
            images = []
        for image in images:
            data = getattr(image, "data", b"") or b""
            if len(data) < _MIN_IMAGE_BYTES:
                continue
            web = _as_web_image(data, getattr(image, "name", "") or "")
            if web is None:
                continue
            w, h = _image_size(web[0])
            # Rules, icons and letterhead logos are not evidence.
            if w and h and (min(w, h) < _MIN_IMAGE_SIDE or max(w, h) < 160):
                continue
            pictures.append(PdfImage(data=web[0], ext=web[1], page=index, width=w, height=h))

        method = "text"
        if len(text) < _MIN_TEXT:
            method = "unread"
            if vision is not None and vision_left > 0:
                vision_left -= 1
                # A scanned page is usually one big embedded image; send that, else render the page.
                scan = max(pictures, key=lambda p: p.width * p.height, default=None)
                if scan is not None and min(scan.width, scan.height) >= _MIN_SCAN_SIDE:
                    read = _read_scan(vision, scan.data, "image/png" if scan.ext == "png" else "image/jpeg", index)
                else:
                    png = _render_page(pdf, index)
                    read = _read_scan(vision, png, "image/png", index) if png else ""
                if len(read) >= _MIN_TEXT:
                    text, method = read, "vision"
            if method == "unread" and text:
                method = "text"
        out.pages.append(PdfPage(number=index, text=text, method=method))
        room = _MAX_IMAGES - len(out.images)
        out.images.extend(pictures[:max(0, room)])
    return out
