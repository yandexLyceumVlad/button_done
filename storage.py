"""Файловое хранение фото документов и работа с PDF."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

import pymupdf

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "image/heif": ".heic",
}

# Форматы изображений, которые бот принимает. Оригинал сохраняется как есть;
# для OCR неподдерживаемые Yandex форматы (WebP/GIF/BMP/TIFF) конвертируются
# только во временную копию в памяти.
SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}

IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}


def sniff_image_extension(data: bytes) -> Optional[str]:
    """Определяет формат изображения по сигнатуре файла."""
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    return None


def image_extension(
    mime: Optional[str],
    filename: Optional[str],
    data: Optional[bytes] = None,
) -> str:
    # Сигнатура надёжнее расширения и Content-Type: MAX может отдать файл
    # без имени или с application/octet-stream.
    suffix = sniff_image_extension(data or b"")
    if suffix:
        return suffix

    mime = (mime or "").split(";")[0].strip().lower()
    if mime in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[mime]

    suffix = Path(filename or "").suffix.lower()
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def guess_image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".bmp":
        return "image/bmp"
    if suffix in (".tif", ".tiff"):
        return "image/tiff"
    if suffix in (".heic", ".heif"):
        return "image/heic"
    return "application/octet-stream"


def detect_document_type(
    filename: Optional[str],
    mime: Optional[str],
    data: bytes,
) -> Optional[str]:
    """Определяет тип вложения по содержимому, расширению и MIME.

    Возвращает "image", "pdf" или None, если формат не поддерживается.
    """
    suffix = Path(filename or "").suffix.lower()
    mime = (mime or "").split(";")[0].strip().lower()

    if suffix == ".pdf" or mime == "application/pdf" or data[:5] == b"%PDF-":
        return "pdf"

    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    if mime in IMAGE_MIME_TYPES:
        return "image"
    # MAX часто присылает фото без filename и с Content-Type
    # application/octet-stream — определяем по сигнатуре файла.
    if sniff_image_extension(data):
        return "image"
    return None


def photo_dir(base_dir: Path, user_id: int) -> Path:
    path = base_dir / "photos" / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_dir(base_dir: Path) -> Path:
    path = base_dir / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_photo(
    data: bytes,
    user_id: int,
    *,
    base_dir: Path,
    mime: Optional[str] = None,
    filename: Optional[str] = None,
) -> Path:
    """Сохраняет фото в data/photos/<user_id>/ без конвертации формата."""
    extension = image_extension(mime, filename, data)
    path = photo_dir(base_dir, user_id) / f"{uuid.uuid4().hex}{extension}"
    path.write_bytes(data)
    return path


def save_temp(data: bytes, suffix: str, *, base_dir: Path) -> Path:
    path = temp_dir(base_dir) / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return path


def pdf_first_page_text(pdf_path: Path) -> str:
    with pymupdf.open(pdf_path) as document:
        if document.page_count < 1:
            return ""
        return document.load_page(0).get_text("text").strip()


def pdf_first_page_to_png(pdf_path: Path, target: Path, dpi: int = 200) -> Path:
    with pymupdf.open(pdf_path) as document:
        if document.page_count < 1:
            raise ValueError("PDF не содержит страниц")
        page = document.load_page(0)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        pixmap.save(target)
    return target


def remove_file(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
