"""Photographs returned by upstream systems.

DLS returns ``applicant_image``; SBVS returns ``data.image``. Neither format is
documented, so images are found by shape rather than by a fixed path: a base64 or
data-URI string (or an image URL) sitting under a key that names an image, confirmed
by the decoded bytes' magic number. A string that merely *looks* like base64 is not an
image until its header says so.

Images are stored content-addressed and the raw payload keeps only a reference, so a
graph of fifty people does not carry fifty photographs inside its JSON.

Two things make a photo the browser cannot show, and both are fixed here, once, when the
record is read:

* **A link instead of bytes.** A police server's image URL is usually reachable from
  this server but not from an officer's browser (internal address, no CORS, http on an
  https page). The disk store downloads it and serves it like any other stored photo.
* **A format browsers do not draw.** NADRA-style JPEG 2000 and scanned TIFFs are
  converted to JPEG.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import re
import threading
from pathlib import Path
from typing import Any, Protocol

_IMAGE_KEY = re.compile(r"image|photo|pic|picture|avatar|thumb|img|snap|face", re.IGNORECASE)
_URL_IMAGE = re.compile(r"^https?://\S+\.(?:jpe?g|png|gif|webp|bmp)(?:\?\S*)?$", re.IGNORECASE)
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MAX_REMOTE_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)
_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"RIFF", "image/webp", "webp"),
    (b"BM", "image/bmp", "bmp"),
    # Not drawable by browsers - converted to JPEG by ``normalise``.
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "image/jp2", "jp2"),
    (b"\xffO\xffQ", "image/jp2", "j2k"),
    (b"II*\x00", "image/tiff", "tif"),
    (b"MM\x00*", "image/tiff", "tif"),
]
_CONVERT = {"jp2", "j2k", "tif"}
_MIN_B64_LEN = 200


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime, ext
    return None


def normalise(data: bytes) -> tuple[bytes, str, str] | None:
    """``(bytes, mime, ext)`` a browser can draw, or ``None`` if ``data`` is no image."""
    kind = sniff(data)
    if kind is None:
        return None
    if kind[1] not in _CONVERT:
        return data, *kind
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=90)
        return out.getvalue(), "image/jpeg", "jpg"
    except Exception:  # noqa: BLE001 - an unreadable photo is no photo, not a failed run
        logger.info("Could not convert a %s image", kind[1])
        return None


def fetch_remote_image(url: str, timeout: int = 10) -> tuple[bytes, str, str] | None:
    """Download an image link server-side. Police servers often use self-signed
    certificates, so a certificate failure is retried unverified - the bytes are then
    checked to be an image, and nothing else is ever done with the answer."""
    import requests

    def get(verify: bool) -> bytes | None:
        with requests.get(url, timeout=timeout, stream=True, verify=verify) as resp:
            if resp.status_code != 200:
                return None
            data = resp.raw.read(_MAX_REMOTE_BYTES + 1, decode_content=True)
            return data if len(data) <= _MAX_REMOTE_BYTES else None

    try:
        try:
            data = get(True)
        except requests.exceptions.SSLError:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            data = get(False)
    except Exception as exc:  # noqa: BLE001 - an unreachable photo keeps its link
        logger.info("Image %s not fetched: %s", url[:120], exc)
        return None
    return normalise(data) if data else None


def decode_image(value: str) -> tuple[bytes, str, str] | None:
    """Decode a base64 / data-URI string to ``(bytes, mime, ext)`` if it is an image."""
    text = value.strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    if len(text) < _MIN_B64_LEN:
        return None
    compact = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"[A-Za-z0-9+/_-]+=*", compact[:4000]):
        return None
    try:
        data = base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=b"-_" if "-" in compact or "_" in compact else None)
    except (binascii.Error, ValueError):
        return None
    return normalise(data)


class ImageStore(Protocol):
    def put(self, data: bytes, ext: str) -> str: ...

    def get(self, image_id: str) -> tuple[bytes, str] | None: ...


class MemoryImageStore:
    # Tests and --no-db runs stay offline: image links are kept as links.
    fetch_remote = False

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str]] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes, ext: str) -> str:
        image_id = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
        with self._lock:
            self._items[image_id] = (data, ext)
        return image_id

    def get(self, image_id: str) -> tuple[bytes, str] | None:
        return self._items.get(image_id)


class DiskImageStore:
    """``<dir>/<sha256>.<ext>``. Same photo from two systems is stored once."""

    # Image links are downloaded and stored, so the browser never has to reach them.
    fetch_remote = True

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, ext: str) -> str:
        image_id = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
        path = self.directory / image_id
        if not path.exists():
            path.write_bytes(data)
        return image_id

    def get(self, image_id: str) -> tuple[bytes, str] | None:
        if not re.fullmatch(r"[0-9a-f]{32}\.[a-z]{3,4}", image_id):
            return None
        path = self.directory / image_id
        if not path.exists():
            return None
        return path.read_bytes(), image_id.rsplit(".", 1)[1]


def extract_images(raw: Any, store: ImageStore) -> tuple[list[str], Any]:
    """Pull every image out of ``raw``.

    Returns ``(image_refs, raw_without_images)``. An image ref is a stored image id, or
    an ``http(s)`` URL when the system returned a link rather than bytes.
    """
    found: list[str] = []
    fetch = bool(getattr(store, "fetch_remote", False))

    def keep(ref: str) -> None:
        if ref not in found:
            found.append(ref)

    def walk(value: Any, under_image_key: bool) -> Any:
        if isinstance(value, dict):
            return {
                key: walk(item, under_image_key or bool(_IMAGE_KEY.search(str(key))))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item, under_image_key) for item in value]
        if isinstance(value, str) and under_image_key:
            url = value.strip()
            # A link under a photo field is a photo even without an extension
            # (getImage?id=12); the downloaded bytes decide.
            if _URL.match(url):
                got = fetch_remote_image(url) if fetch else None
                if got:
                    image_id = store.put(got[0], got[2])
                    keep(image_id)
                    return f"<image {image_id} from {url}>"
                if _URL_IMAGE.match(url):
                    keep(url)
                return value
            decoded = decode_image(value)
            if decoded:
                image_id = store.put(decoded[0], decoded[2])
                if image_id not in found:
                    found.append(image_id)
                return f"<image {image_id}>"
        if isinstance(value, str) and len(value) > 5000:
            # Unlabelled blobs are kept out of the stored payload either way.
            decoded = decode_image(value)
            if decoded:
                image_id = store.put(decoded[0], decoded[2])
                if image_id not in found:
                    found.append(image_id)
                return f"<image {image_id}>"
            return value[:2000] + f"... <{len(value)} chars>"
        return value

    return found, walk(raw, False)
