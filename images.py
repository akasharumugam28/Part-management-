"""Image upload validation, storage and cleanup.

Uploaded files are never trusted: the extension is checked, the bytes are
decoded with Pillow, and the image is re-encoded under a random filename.
Re-encoding also strips metadata (e.g. GPS EXIF) and caps the dimensions.

The re-encoded file is stored by one of two backends, and the database keeps
only its object name (``<uuid>.<ext>``) in ``image_filename``:

* ``SupabaseStorage``: a private Supabase Storage bucket (production). The
  browser gets short-lived signed URLs; the service-role key never leaves
  the server.
* ``LocalStorage``: the ``UPLOAD_FOLDER`` directory (local development).
"""
import io
import logging
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import current_app, g
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

INVALID_FORMAT = "Invalid image format."
UPLOAD_FAILED = "The image could not be saved. Please try again."
# Pillow format -> stored extension
_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
MIME_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
# Object names are generated here (uuid hex + extension). Anything else that
# reaches the storage layer, e.g. from a URL, must at least look like a plain
# file name: no slashes, no "..", no leading dot.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
# Stored images are only shrunk below their size limit down to this many px.
MIN_STORED_DIMENSION = 640
# Guard against decompression bombs (~40 megapixels is plenty for a photo).
Image.MAX_IMAGE_PIXELS = 40_000_000


class ImageError(ValueError):
    pass


class StorageError(ImageError):
    """The storage backend failed (network, permissions, ...)."""


class ObjectExists(StorageError):
    pass


def is_safe_name(name):
    return bool(name) and _SAFE_NAME.fullmatch(name) is not None and ".." not in name


# ------------------------------------------------------------------ backends

class LocalStorage:
    def __init__(self, folder):
        self.folder = Path(folder)

    def save(self, name, data, content_type):
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.folder / name, "xb") as f:  # never overwrite
                f.write(data)
        except FileExistsError:
            raise ObjectExists(UPLOAD_FAILED) from None
        except OSError as exc:
            log.warning("Could not write image file %s: %s", name, exc)
            raise StorageError(UPLOAD_FAILED) from exc

    def read(self, name):
        if not is_safe_name(name):
            return None
        try:
            return (self.folder / name).read_bytes()
        except OSError:
            return None

    def delete(self, names):
        for name in names:
            # Only ever delete plain filenames inside the upload folder.
            if not name or Path(name).name != name:
                continue
            try:
                (self.folder / name).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete image file %s", name)

    def list_names(self):
        if not self.folder.is_dir():
            return []
        return [f.name for f in self.folder.iterdir() if f.is_file()]


class SupabaseStorage:
    """Minimal client for the Supabase Storage REST API (/storage/v1)."""

    def __init__(self, url, key, bucket, timeout=15, signed_url_ttl=3600,
                 max_object_bytes=20 * 1024 * 1024, session=None):
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.timeout = timeout
        self.signed_url_ttl = signed_url_ttl
        self.max_object_bytes = max_object_bytes
        self.session = session or requests.Session()
        self._auth = {"Authorization": f"Bearer {key}", "apikey": key}

    def __repr__(self):  # never show the key
        return f"SupabaseStorage(base={self.base!r}, bucket={self.bucket!r})"

    def _request(self, method, path, headers=None, **kwargs):
        try:
            return self.session.request(
                method, self.base + path, headers={**self._auth, **(headers or {})},
                timeout=self.timeout, **kwargs,
            )
        except requests.RequestException as exc:
            # Log the exception type only: messages can echo request details.
            log.warning("Supabase Storage %s %s failed: %s", method, path, type(exc).__name__)
            raise StorageError(UPLOAD_FAILED) from exc

    @staticmethod
    def _is_duplicate(resp):
        if resp.status_code == 409:
            return True
        return resp.status_code == 400 and ("Duplicate" in resp.text or "already exists" in resp.text)

    @staticmethod
    def _is_missing(resp):
        return resp.status_code == 404 or (resp.status_code == 400 and "not found" in resp.text.lower())

    def save(self, name, data, content_type):
        resp = self._request(
            "POST", f"/object/{self.bucket}/{name}", data=data,
            headers={"Content-Type": content_type, "x-upsert": "false",
                     # Names are never reused, so the content never changes.
                     "Cache-Control": "max-age=31536000"},
        )
        if self._is_duplicate(resp):
            raise ObjectExists(UPLOAD_FAILED)
        if not resp.ok:
            log.warning("Supabase Storage upload of %s failed: HTTP %s %s",
                        name, resp.status_code, resp.text[:200])
            raise StorageError(UPLOAD_FAILED)

    def read(self, name):
        if not is_safe_name(name):
            return None
        try:
            resp = self._request("GET", f"/object/authenticated/{self.bucket}/{name}")
        except StorageError:
            return None
        if not resp.ok:
            if not self._is_missing(resp):
                log.warning("Supabase Storage download of %s failed: HTTP %s", name, resp.status_code)
            return None
        return resp.content

    def delete(self, names):
        names = [n for n in names if is_safe_name(n)]
        if not names:
            return
        try:
            resp = self._request("DELETE", f"/object/{self.bucket}", json={"prefixes": names})
        except StorageError:
            log.warning("Could not delete image file(s) %s", ", ".join(names))
            return
        if not resp.ok:
            log.warning("Could not delete image file(s) %s: HTTP %s", ", ".join(names), resp.status_code)

    def list_names(self):
        names, offset, page = [], 0, 1000
        while True:
            resp = self._request("POST", f"/object/list/{self.bucket}", json={
                "prefix": "", "limit": page, "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            })
            if not resp.ok:
                raise StorageError(f"Could not list bucket '{self.bucket}': HTTP {resp.status_code}")
            items = resp.json()
            names += [i["name"] for i in items if i.get("id")]  # id is null for folders
            if len(items) < page:
                return names
            offset += page

    def _absolute(self, signed):
        return signed if signed.startswith("http") else self.base + "/" + signed.lstrip("/")

    def signed_url(self, name):
        if not is_safe_name(name):
            return None
        try:
            resp = self._request("POST", f"/object/sign/{self.bucket}/{name}",
                                 json={"expiresIn": self.signed_url_ttl})
        except StorageError:
            return None
        if not resp.ok:
            if not self._is_missing(resp):
                log.warning("Could not sign image URL %s: HTTP %s", name, resp.status_code)
            return None
        return self._absolute(resp.json()["signedURL"])

    def signed_urls(self, names):
        """Sign many object names in one request. Returns {name: url}."""
        names = sorted({n for n in names if is_safe_name(n)})
        if not names:
            return {}
        try:
            resp = self._request("POST", f"/object/sign/{self.bucket}",
                                 json={"expiresIn": self.signed_url_ttl, "paths": names})
        except StorageError:
            return {}
        if not resp.ok:
            log.warning("Could not sign %d image URLs: HTTP %s", len(names), resp.status_code)
            return {}
        return {i["path"]: self._absolute(i["signedURL"])
                for i in resp.json() if i.get("signedURL") and not i.get("error")}

    def ensure_bucket(self):
        """Create the bucket as private if it does not exist. Returns True if created."""
        resp = self._request("GET", f"/bucket/{self.bucket}")
        if resp.ok:
            if resp.json().get("public"):
                raise StorageError(
                    f"Bucket '{self.bucket}' is PUBLIC. Make it private in the Supabase dashboard "
                    "(Storage > bucket > Edit) - images must only be visible to the logged-in admin."
                )
            return False
        resp = self._request("POST", "/bucket", json={
            "id": self.bucket, "name": self.bucket, "public": False,
            "file_size_limit": self.max_object_bytes,
            "allowed_mime_types": sorted(set(MIME_TYPES.values())),
        })
        if self._is_duplicate(resp):
            return False
        if not resp.ok:
            raise StorageError(f"Could not create bucket '{self.bucket}': HTTP {resp.status_code} "
                               f"{resp.text[:200]}")
        return True


def storage():
    """The image storage backend for the current app."""
    cfg = current_app.config
    if cfg["STORAGE_BACKEND"] != "supabase":
        return LocalStorage(cfg["UPLOAD_FOLDER"])
    store = current_app.extensions.get("image_storage")
    if store is None:
        store = current_app.extensions["image_storage"] = SupabaseStorage(
            cfg["SUPABASE_URL"], cfg["SUPABASE_SERVICE_ROLE_KEY"], cfg["SUPABASE_BUCKET"],
            timeout=cfg["STORAGE_TIMEOUT"], signed_url_ttl=cfg["SUPABASE_SIGNED_URL_TTL"],
        )
    return store


# ------------------------------------------------------------------- uploads

def has_file(storage):
    return storage is not None and bool(storage.filename)


def encode_within(img, fmt, max_bytes):
    """Encode ``img`` as ``fmt``, keeping the stored file at most ``max_bytes``.

    Tries quality 85 first (the normal case for a photo). If the file is too
    big, JPEG/WebP drop to quality 75, then the image is scaled down step by
    step, never below MIN_STORED_DIMENSION px. Below that it is returned as is.
    """
    qualities = (85, 75) if fmt in ("JPEG", "WEBP") else (None,)
    while True:
        for quality in qualities:
            buf = io.BytesIO()
            img.save(buf, fmt, optimize=True, **({"quality": quality} if quality else {}))
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data
        if max(img.size) <= MIN_STORED_DIMENSION:
            return data
        # File size grows roughly with pixel count, so scale by sqrt of the ratio.
        scale = min(0.9, (max_bytes / len(data)) ** 0.5 * 0.95)
        scale = max(scale, MIN_STORED_DIMENSION / max(img.size))
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)


def validate_and_save(storage_file):
    """Validate an uploaded FileStorage and save it. Returns the new filename."""
    cfg = current_app.config
    name = storage_file.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in cfg["ALLOWED_IMAGE_EXTENSIONS"]:
        raise ImageError(INVALID_FORMAT)

    stream = storage_file.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        raise ImageError(INVALID_FORMAT)
    if size > cfg["MAX_IMAGE_BYTES"]:
        mb = cfg["MAX_IMAGE_BYTES"] // (1024 * 1024)
        raise ImageError(f"Image is too large. The maximum size is {mb} MB.")

    try:
        with Image.open(stream) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in _FORMATS:
            raise ImageError(INVALID_FORMAT)
        stream.seek(0)
        with Image.open(stream) as img:
            img = ImageOps.exif_transpose(img)
            limit = cfg["MAX_IMAGE_DIMENSION"]
            img.thumbnail((limit, limit))
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            data = encode_within(img, fmt, cfg["MAX_STORED_IMAGE_BYTES"])
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise ImageError(INVALID_FORMAT)

    ext = _FORMATS[fmt]
    store = storage()
    for _ in range(3):  # a uuid4 collision is practically impossible, but never overwrite
        filename = f"{uuid.uuid4().hex}.{ext}"
        try:
            store.save(filename, data, MIME_TYPES[ext])
            return filename
        except ObjectExists:
            continue
    raise StorageError(UPLOAD_FAILED)


def resolve_replacement(current, storage_file, remove):
    """Work out an edit form's image change.

    Returns (filename_to_store, newly_saved_file_or_None, obsolete_file_or_None).
    The caller deletes ``obsolete`` after the database commit succeeds, or
    ``newly_saved`` if it fails.
    """
    if has_file(storage_file):
        new = validate_and_save(storage_file)
        return new, new, current
    if remove and current:
        return None, None, current
    return current, None, None


def delete_files(filenames):
    """Remove stored images. Missing files are ignored; failures are logged."""
    names = [n for n in filenames if n]
    if names:
        storage().delete(names)


def read_many(names):
    """Fetch several stored images. Returns {name: bytes or None}."""
    names = sorted({n for n in names if n})
    store = storage()
    if len(names) <= 1 or isinstance(store, LocalStorage):
        return {n: store.read(n) for n in names}
    with ThreadPoolExecutor(max_workers=8) as pool:  # network-bound
        return dict(zip(names, pool.map(store.read, names)))


# --------------------------------------------------------------- display URLs

def prefetch_urls(names):
    """Sign the URLs of images a page is about to show, in one request.

    Without this each <img> goes through /uploads/<name>, which signs and
    redirects one image at a time. It still works, just slower.
    """
    store = storage()
    if not hasattr(store, "signed_urls"):
        return
    cache = g.setdefault("signed_image_urls", {})
    missing = [n for n in names if n and n not in cache]
    if missing:
        cache.update(store.signed_urls(missing))


def cached_url(name):
    return g.get("signed_image_urls", {}).get(name)
