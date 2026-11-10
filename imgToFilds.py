"""Распознавание документов: Yandex Vision OCR + Yandex LLM.

Модуль отдаёт наружу словарь с полями документа:
    kind, title, organizer, level, dates, result, hours, registration_number
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import requests

import report
import storage

log = logging.getLogger("max-docs-bot.ocr")

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"
LLM_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"
DEFAULT_LLM_MODEL = "yandexgpt/latest"

FIELDS = (
    "kind",
    "title",
    "organizer",
    "level",
    "dates",
    "result",
    "hours",
    "registration_number",
)

LEVELS = ("Муниципальный", "Региональный", "Федеральный", "Международный")
RESULTS = ("победитель", "призер", "участник")

LEVEL_BY_LOWER = {value.lower(): value for value in LEVELS}
RESULT_BY_LOWER = {value.lower(): value for value in RESULTS}

# Yandex OCR принимает файл до 10 МБ. Оставляем запас на base64.
OCR_MAX_BYTES = 7 * 1024 * 1024
OCR_MAX_SIDE = 2500
OCR_SCALE_THRESHOLD = 3000

COURSE_PROMPT = """Ты извлекаешь данные из удостоверения о повышении квалификации.
Верни строго один JSON-объект без markdown и пояснений:
{"title": string|null, "organizer": string|null, "issue_date": string|null, "hours": number|null, "registration_number": string|null}
Правила:
- "title" — название программы обучения (например, после слова «программе»).
- "organizer" — организация, выдавшая документ.
- "issue_date" — дата выдачи (после слов «Дата выдачи»), формат ДД.ММ.ГГГГ. Это НЕ период обучения.
- "hours" — целое число академических часов или null.
- "registration_number" — значение после «Регистрационный номер», если есть; иначе номер документа.
- Не придумывай данные. Если поля нет — null."""

COMPETITION_PROMPT = """Ты извлекаешь данные из диплома или грамоты за конкурс.
Верни строго один JSON-объект без markdown и пояснений:
{"title": string|null, "organizer": string|null, "level": string|null, "event_dates": string|null, "result": string|null}
Правила:
- "title" — название конкурса.
- "organizer" — организатор конкурса.
- "level" — только "Муниципальный", "Региональный", "Федеральный", "Международный" или null.
- "event_dates" — даты проведения, диапазон в формате ДД.ММ.ГГГГ – ДД.ММ.ГГГГ или одна дата.
- "result" — только "победитель", "призер", "участник" или null.
- Не придумывай данные. Если поля нет — null."""


class RecognitionError(RuntimeError):
    """Ошибка OCR/LLM/разбора, которую бот показывает пользователю."""


def _load_env() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env()


def _api_key() -> str:
    return os.getenv("YANDEX_API_KEY", "").strip()


def _folder_id() -> str:
    return os.getenv("YANDEX_FOLDER_ID", "").strip()


def _llm_model() -> str:
    return os.getenv("YANDEX_LLM_MODEL", DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL


def empty_info(kind: str = "курс") -> dict[str, Any]:
    return {key: None for key in FIELDS} | {"kind": kind}


def _clean(value: Any, limit: int = 500) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = value.strip("\"'` ")
    value = value.rstrip(" .,;:")
    if not value:
        return None
    return value[:limit]


def _as_hours(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    match = re.search(r"\d{1,5}", str(value))
    if not match:
        return None
    hours = int(match.group(0))
    return hours if 1 <= hours <= 10000 else None


def _choice(value: Any, mapping: dict[str, str]) -> Optional[str]:
    cleaned = _clean(value, 100)
    if cleaned is None:
        return None
    return mapping.get(cleaned.lower())


def _strip_code_fences(value: str) -> str:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_json(value: str) -> dict[str, Any]:
    text = _strip_code_fences(value)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RecognitionError("LLM вернула ответ без JSON")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RecognitionError(f"LLM вернула некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RecognitionError("LLM вернула JSON не в виде объекта")
    return data


def _call_llm(text: str, kind: str) -> str:
    """Отправляет текст документа в Yandex LLM и возвращает ответ модели."""
    api_key = _api_key()
    folder_id = _folder_id()
    if not api_key or not folder_id:
        raise RecognitionError(
            "Не заданы YANDEX_API_KEY/YANDEX_FOLDER_ID в .env"
        )

    prompt = COMPETITION_PROMPT if kind == "конкурс" else COURSE_PROMPT
    body = {
        "model": f"gpt://{folder_id}/{_llm_model()}",
        "temperature": 0.0,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Текст документа:\n{text}"},
        ],
    }

    try:
        response = requests.post(
            LLM_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {api_key}",
            },
            json=body,
            timeout=90,
        )
    except requests.RequestException as exc:
        raise RecognitionError(f"Yandex LLM недоступна: {exc}") from exc

    if not response.ok:
        raise RecognitionError(
            f"Yandex LLM вернула {response.status_code}: {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RecognitionError("Yandex LLM вернула не JSON") from exc

    content = (
        ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
    )
    if not content:
        legacy = (
            ((payload.get("result") or {}).get("alternatives") or [{}])[0].get(
                "message"
            )
            or {}
        ).get("text")
        content = legacy
    if not content:
        raise RecognitionError("Yandex LLM вернула пустой ответ")
    return str(content)


def _normalize(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    info = empty_info(kind)

    title = _clean(raw.get("title"), 300)
    organizer = _clean(raw.get("organizer"), 300)
    info["title"] = title
    info["organizer"] = organizer

    if kind == "курс":
        issue_date = _clean(raw.get("issue_date"), 100)
        if issue_date:
            info["dates"] = report.normalize_dates(issue_date, "курс") or issue_date
        info["hours"] = _as_hours(raw.get("hours"))
        info["registration_number"] = _clean(
            raw.get("registration_number"), 100
        )
    else:
        event_dates = _clean(raw.get("event_dates"), 100)
        if event_dates:
            info["dates"] = (
                report.normalize_dates(event_dates, "конкурс") or event_dates
            )
        info["level"] = _choice(raw.get("level"), LEVEL_BY_LOWER)
        info["result"] = _choice(raw.get("result"), RESULT_BY_LOWER)

    return info


def extract_info(raw_text: str, kind: Optional[str] = None) -> dict[str, Any]:
    """Извлекает поля документа из распознанного текста через Yandex LLM."""
    expected = kind if kind in ("курс", "конкурс") else "курс"
    text = (raw_text or "").strip()
    if not text:
        return empty_info(expected)

    answer = _call_llm(text, expected)
    try:
        return _normalize(_parse_json(answer), expected)
    except RecognitionError:
        # Один повтор: модель иногда добавляет пояснения вокруг JSON.
        log.warning("LLM вернула неразборчивый ответ, повторяю запрос")
        answer = _call_llm(text, expected)
        return _normalize(_parse_json(answer), expected)


def _mime_type(mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in ("image/jpeg", "image/jpg", "jpeg", "jpg"):
        return "jpeg"
    if mime == "application/pdf":
        return "pdf"
    return "png"


def recognize_text(data: bytes, mime: str) -> str:
    """Отправляет изображение в Yandex Vision OCR и возвращает текст."""
    api_key = _api_key()
    folder_id = _folder_id()
    if not api_key or not folder_id:
        raise RecognitionError(
            "Не заданы YANDEX_API_KEY/YANDEX_FOLDER_ID в .env"
        )

    body = {
        "mimeType": _mime_type(mime),
        "languageCodes": ["*"],
        "model": "page",
        "content": base64.b64encode(data).decode("ascii"),
    }
    try:
        response = requests.post(
            OCR_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Api-Key {api_key}",
                "x-folder-id": folder_id,
                "x-data-logging-enabled": "true",
            },
            json=body,
            timeout=90,
        )
    except requests.RequestException as exc:
        raise RecognitionError(f"OCR недоступен: {exc}") from exc

    if not response.ok:
        raise RecognitionError(
            f"OCR вернул {response.status_code}: {response.text[:300]}"
        )

    payload = response.json()
    annotation = (payload.get("result") or {}).get("textAnnotation") or {}
    full_text = (annotation.get("fullText") or "").strip()
    if full_text:
        return full_text

    lines: list[str] = []
    for block in annotation.get("blocks") or []:
        for line in block.get("lines") or []:
            text = (line.get("text") or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines).strip()


def _prepare_image_for_ocr(path: Path) -> tuple[bytes, str]:
    """Возвращает изображение для OCR, при необходимости уменьшая его.

    Оригинал на диске не меняется. JPEG остаётся JPEG, PNG — PNG.
    WebP/GIF/BMP/TIFF конвертируются только в копию для OCR, потому что
    Yandex Vision OCR не принимает эти форматы.
    """
    path = Path(path)
    data = path.read_bytes()
    mime = storage.guess_image_mime(path)

    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover
        log.warning("Pillow не установлена, отправляю изображение как есть")
        return data, mime

    try:
        with Image.open(BytesIO(data)) as opened:
            source_format = (opened.format or "").upper()
            image = ImageOps.exif_transpose(opened)
            width, height = image.size

            needs_scale = (
                len(data) > OCR_MAX_BYTES
                or max(width, height) > OCR_SCALE_THRESHOLD
            )
            needs_conversion = source_format not in ("JPEG", "PNG")

            if not needs_scale and not needs_conversion:
                return data, mime

            if needs_scale:
                scale = min(1.0, OCR_MAX_SIDE / max(width, height))
                size = (max(1, int(width * scale)), max(1, int(height * scale)))
                image = image.resize(size, Image.LANCZOS)

            if source_format == "JPEG":
                output_format = "JPEG"
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
            elif source_format == "PNG":
                output_format = "PNG"
            else:
                # WebP/GIF/BMP/TIFF: для OCR нужна копия в JPEG или PNG,
                # сам сохранённый файл остаётся в исходном формате.
                has_alpha = image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                )
                if has_alpha:
                    output_format = "PNG"
                    image = image.convert("RGBA")
                else:
                    output_format = "JPEG"
                    image = image.convert("RGB")

            buffer = BytesIO()
            if output_format == "JPEG":
                image.save(buffer, format="JPEG", quality=90, optimize=True)
                result_mime = "image/jpeg"
            else:
                image.save(buffer, format="PNG", optimize=True)
                result_mime = "image/png"

            result = buffer.getvalue()

            # Если копия всё ещё больше лимита OCR, уменьшаем ещё раз.
            if len(result) > OCR_MAX_BYTES:
                scale = min(1.0, 1600 / max(image.size))
                image = image.resize(
                    (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    ),
                    Image.LANCZOS,
                )
                buffer = BytesIO()
                if output_format == "JPEG":
                    image.save(buffer, format="JPEG", quality=80, optimize=True)
                else:
                    image.save(buffer, format="PNG", optimize=True)
                result = buffer.getvalue()

            log.info(
                "Изображение подготовлено для OCR: %s -> %s байт (%s)",
                source_format or "?",
                len(result),
                result_mime,
            )
            return result, result_mime
    except Exception as exc:
        log.warning("Не удалось подготовить изображение %s: %s", path.name, exc)
        return data, mime


def recognize_image(path: Path, kind: Optional[str] = None) -> dict[str, Any]:
    """Распознаёт картинку и возвращает поля документа."""
    data, mime = _prepare_image_for_ocr(Path(path))
    text = recognize_text(data, mime)
    if not text:
        raise RecognitionError("Текст на изображении не найден")
    log.info("OCR распознал %s символов", len(text))
    return extract_info(text, kind)
