"""Damage-photo evidence.

Each uploaded photo gets:
  * a SHA-256 fingerprint of the original file, recorded in the dispute file so anyone can check
    the photo wasn't swapped later
  * a compressed JPEG (longest side <= 1568 px, the size Claude reads images at), which is what
    Claude assesses and what is stored in Firestore when no Storage bucket is configured
  * the original itself, when a Cloud Storage bucket is configured
"""
import hashlib
import io
import time
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import store

MAX_SIDE = 1568
MAX_COMPRESSED = 900_000  # leaves room in a 1 MiB Firestore document
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


class EvidenceError(ValueError):
    pass


def compress(raw: bytes) -> tuple[bytes, int, int]:
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)  # phone photos store rotation in EXIF
    except (UnidentifiedImageError, OSError) as exc:
        raise EvidenceError("That file isn't a photo we can read. Use a JPEG, PNG or WebP image.") from exc
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    for quality in (82, 72, 62, 50, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_COMPRESSED:
            return buf.getvalue(), img.width, img.height
        if quality == 50:
            img.thumbnail((1024, 1024))
    raise EvidenceError("That photo is too detailed to store. Try a smaller one.")


def save_photos(order_id: str, photos: list[tuple[str, bytes]]) -> tuple[list[dict], list[tuple[str, bytes]]]:
    """Store photos. Returns (records for the order, compressed JPEGs to show Claude)."""
    records, for_claude = [], []
    for i, (media_type, raw) in enumerate(photos):
        compressed, w, h = compress(raw)
        record = {
            "id": f"ev_{uuid.uuid4().hex[:12]}",
            "order_id": order_id,
            "index": i,
            "media_type": media_type if media_type in EXT else "image/jpeg",
            "ext": EXT.get(media_type, ".jpg"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "original_bytes": len(raw),
            "width": w,
            "height": h,
            "uploaded_at": time.time(),
        }
        records.append(store.save_evidence(record, raw, compressed))
        for_claude.append(("image/jpeg", compressed))
    return records, for_claude
