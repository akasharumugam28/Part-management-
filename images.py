"""Image upload validation, storage and cleanup.

Uploaded files are never trusted: the extension is checked, the bytes are
decoded with Pillow, and the image is re-encoded under a random filename.
Re-encoding also strips metadata (e.g. GPS EXIF) and caps the dimensions.
"""
import logging
import os
import uuid
from pathlib import Path

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

INVALID_FORMAT = "Invalid image format."
# Pillow format -> stored extension
_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
# Guard against decompression bombs (~40 megapixels is plenty for a photo).
Image.MAX_IMAGE_PIXELS = 40_000_000


class ImageError(ValueError):
    pass


def has_file(storage):
    return storage is not None and bool(storage.filename)


def _upload_dir():
    path = Path(current_app.config["UPLOAD_FOLDER"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_and_save(storage):
    """Validate an uploaded FileStorage and save it. Returns the new filename."""
    cfg = current_app.config
    ext = storage.filename.rsplit(".", 1)[-1].lower() if "." in storage.filename else ""
    if ext not in cfg["ALLOWED_IMAGE_EXTENSIONS"]:
        raise ImageError(INVALID_FORMAT)

    stream = storage.stream
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
            filename = f"{uuid.uuid4().hex}.{_FORMATS[fmt]}"
            save_kwargs = {"optimize": True}
            if fmt in ("JPEG", "WEBP"):
                save_kwargs["quality"] = 85
            img.save(_upload_dir() / filename, fmt, **save_kwargs)
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise ImageError(INVALID_FORMAT)
    return filename


def resolve_replacement(current, storage, remove):
    """Work out an edit form's image change.

    Returns (filename_to_store, newly_saved_file_or_None, obsolete_file_or_None).
    The caller deletes ``obsolete`` after the database commit succeeds, or
    ``newly_saved`` if it fails.
    """
    if has_file(storage):
        new = validate_and_save(storage)
        return new, new, current
    if remove and current:
        return None, None, current
    return current, None, None


def delete_files(filenames):
    """Remove stored images. Missing files are ignored; failures are logged."""
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    for name in filenames:
        if not name:
            continue
        # Only ever delete plain filenames inside the upload folder.
        if Path(name).name != name:
            continue
        try:
            (upload_dir / name).unlink(missing_ok=True)
        except OSError:
            log.warning("Could not delete image file %s", name)
