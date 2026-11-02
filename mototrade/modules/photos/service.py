"""Carga real de fotografías.

- Valida que el archivo sea una imagen (JPEG/PNG/WebP por firma de bytes y decodificación con Pillow).
- Recomprime a JPEG de hasta `max_side` px y calidad 85 (fotos de celular de 3–8 MB → 150–400 KB) para
  caber en cuotas gratuitas (Supabase Storage 1 GB) y abaratar la visión por IA (tokens ∝ píxeles).
- Deduplica por SHA-256 del contenido original: la misma foto cargada dos veces no se guarda dos veces.
- Almacenamiento: disco local (VPS/Docker, por defecto) o Supabase Storage (REST, bucket privado) si hay credenciales.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from mototrade.core import audit
from mototrade.core.enums import ActorType
from mototrade.db.models import Photo

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_SIGNATURES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png", b"RIFF": "image/webp"}


class PhotoError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredBlob:
    path: str
    url: str | None


class PhotoStorage(Protocol):
    name: str

    def put(self, key: str, data: bytes, mime: str) -> StoredBlob: ...
    def get(self, key: str) -> bytes: ...


class LocalPhotoStorage:
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, mime: str) -> StoredBlob:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return StoredBlob(path=str(p), url=None)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class SupabasePhotoStorage:
    """Supabase Storage vía REST (bucket privado). Requiere URL del proyecto y service key (secreto).
    Límite del plan gratuito: 1 GB total, 50 MB por archivo; las fotos recomprimidas pesan ~200–400 KB."""

    name = "supabase"

    def __init__(self, project_url: str, service_key: str, bucket: str = "fotos", transport: httpx.BaseTransport | None = None):
        if not project_url or not service_key:
            raise PhotoError("Supabase Storage requiere project_url y service_key.")
        self.base = project_url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self._client = httpx.Client(transport=transport, timeout=30.0,
                                    headers={"Authorization": f"Bearer {service_key}", "apikey": service_key})

    def put(self, key: str, data: bytes, mime: str) -> StoredBlob:
        r = self._client.post(f"{self.base}/object/{self.bucket}/{key}", content=data,
                              headers={"Content-Type": mime, "x-upsert": "true"})
        if r.status_code not in (200, 201):
            raise PhotoError(f"Supabase Storage respondió {r.status_code}: {r.text[:200]}")
        return StoredBlob(path=f"supabase://{self.bucket}/{key}", url=None)

    def get(self, key: str) -> bytes:
        r = self._client.get(f"{self.base}/object/authenticated/{self.bucket}/{key}")
        if r.status_code != 200:
            raise PhotoError(f"No se pudo leer la foto ({r.status_code}).")
        return r.content


def get_storage() -> PhotoStorage:
    from mototrade.config import get_settings

    s = get_settings()
    url, key = getattr(s, "supabase_url", ""), getattr(s, "supabase_service_key", None)
    key = key.get_secret_value() if key is not None and hasattr(key, "get_secret_value") else (key or "")
    if url and key:
        return SupabasePhotoStorage(url, key, getattr(s, "supabase_bucket", "fotos"))
    return LocalPhotoStorage(s.photos_dir)


def sniff_mime(data: bytes) -> str | None:
    for sig, mime in _SIGNATURES.items():
        if data.startswith(sig):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                return None
            return mime
    return None


def normalize_image(data: bytes, *, max_side: int = 1600, quality: int = 85) -> tuple[bytes, int, int]:
    """Decodifica, corrige orientación EXIF, reduce y recomprime a JPEG. Devuelve (bytes, ancho, alto)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise PhotoError("El archivo no es una imagen válida.") from exc
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), img.size[0], img.size[1]


class PhotoService:
    def __init__(self, session: Session, storage: PhotoStorage | None = None):
        self.session = session
        self.storage = storage or get_storage()

    def store(self, *, entity_type: str, entity_id: int, data: bytes, filename: str = "", origin: str = "upload",
              caption: str = "", by: str = "operator") -> Photo:
        if not data:
            raise PhotoError("Archivo vacío.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise PhotoError("La foto supera 20 MB.")
        if sniff_mime(data) is None:
            raise PhotoError("Formato no soportado: usar JPEG, PNG o WebP.")
        sha = hashlib.sha256(data).hexdigest()
        existing = self.session.execute(
            select(Photo).where(Photo.sha256 == sha, Photo.entity_type == entity_type, Photo.entity_id == entity_id)
        ).scalars().first()
        if existing:
            return existing
        jpeg, w, h = normalize_image(data)
        key = f"{entity_type}/{entity_id}/{sha[:16]}.jpg"
        blob = self.storage.put(key, jpeg, "image/jpeg")
        photo = Photo(entity_type=entity_type, entity_id=entity_id, path=blob.path, url=blob.url, sha256=sha,
                      is_authentic=True, caption=caption or filename[:255], mime_type="image/jpeg", size_bytes=len(jpeg),
                      origin=origin)
        self.session.add(photo)
        self.session.flush()
        audit.record(self.session, action="photo.stored", entity_type="photo", entity_id=photo.id,
                     details={"target": f"{entity_type}:{entity_id}", "bytes": len(jpeg), "px": f"{w}x{h}",
                              "storage": self.storage.name, "origin": origin}, actor_type=ActorType.HUMAN, actor_id=by)
        return photo

    def read(self, photo: Photo) -> bytes:
        if photo.path.startswith("supabase://"):
            key = photo.path.split("/", 3)[3]
            return self.storage.get(key)
        return Path(photo.path).read_bytes()

    def for_entity(self, entity_type: str, entity_id: int) -> list[Photo]:
        return list(self.session.execute(
            select(Photo).where(Photo.entity_type == entity_type, Photo.entity_id == entity_id).order_by(Photo.id)
        ).scalars())
