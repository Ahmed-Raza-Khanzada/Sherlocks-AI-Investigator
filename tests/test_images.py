"""Photos the browser can always draw: links downloaded server-side, JPEG 2000 / TIFF
converted, and nothing broken left on a node."""

from __future__ import annotations

import base64
import io

from PIL import Image

from sherlocks.linkgraph import images
from sherlocks.linkgraph.images import DiskImageStore, MemoryImageStore, extract_images, normalise


def _img(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 30, 30)).save(out, format=fmt)
    return out.getvalue()


def test_jpeg2000_and_tiff_become_jpeg_and_jpeg_stays_as_is():
    for fmt in ("JPEG2000", "TIFF"):
        data, mime, ext = normalise(_img(fmt))
        assert (mime, ext) == ("image/jpeg", "jpg") and data.startswith(b"\xff\xd8\xff")
    jpeg = _img("JPEG")
    assert normalise(jpeg) == (jpeg, "image/jpeg", "jpg")
    assert normalise(b"not an image at all") is None


def test_a_base64_jpeg2000_photo_is_stored_as_jpeg():
    raw = {"applicant_image": base64.b64encode(_img("JPEG2000")).decode()}
    refs, clean = extract_images(raw, MemoryImageStore())
    assert refs[0].endswith(".jpg") and clean["applicant_image"].startswith("<image ")


class _Resp:
    def __init__(self, status: int, data: bytes) -> None:
        self.status_code = status
        self.raw = io.BytesIO(data)
        self.raw.read = lambda n, decode_content=True, _r=self.raw: io.BytesIO.read(_r, n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_disk_store_downloads_photo_links_even_without_an_extension(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kw: _Resp(200, _img("PNG")))
    store = DiskImageStore(tmp_path)
    refs, clean = extract_images({"photo": "http://10.0.0.5/dls/getImage?id=12"}, store)
    assert len(refs) == 1 and refs[0].endswith(".png") and store.get(refs[0]) is not None
    assert "getImage" in clean["photo"]                       # the source link is kept for the record


def test_an_unreachable_link_is_kept_only_if_it_looks_like_an_image(tmp_path, monkeypatch):
    import requests

    def down(url, **kw):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", down)
    store = DiskImageStore(tmp_path)
    assert extract_images({"photo": "http://10.0.0.5/p/7.jpg"}, store)[0] == ["http://10.0.0.5/p/7.jpg"]
    assert extract_images({"photo": "http://10.0.0.5/getImage?id=7"}, store)[0] == []


def test_memory_store_never_touches_the_network(monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(requests, "get", boom)
    assert extract_images({"photo": "http://x/p.jpg"}, MemoryImageStore())[0] == ["http://x/p.jpg"]
    assert images.MemoryImageStore.fetch_remote is False and images.DiskImageStore.fetch_remote is True
