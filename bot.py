#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чат-бот MAX: портфолио педагога.

Сценарий:
    /start -> ФИО -> меню (грамота / курс ПК) -> фото или PDF ->
    распознавание -> пошаговая проверка и редактирование полей ->
    сохранение в БД -> отчёт за текущий отчётный период.

Запуск:
    pip install -r requirements.txt
    python bot.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

import db as database
import imgToFilds
import report
import storage

# По требованию проекта проверка TLS для запросов к MAX отключена.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "documents.sqlite3"
MARKER_PATH = DATA_DIR / "marker.json"

POLL_TIMEOUT = 30
MAX_FILE_BYTES = 50 * 1024 * 1024
SUMMARY_BUTTON_LIMIT = 20

LEVELS = ("Муниципальный", "Региональный", "Федеральный", "Международный")
RESULTS = ("победитель", "призер", "участник")

KIND_LABELS = {
    "конкурс": "грамоту за конкурс",
    "курс": "курс повышения квалификации",
}

PHOTO_PROMPT = (
    "📷 Пришлите фото документа или PDF.\n\n"
    "Нажмите скрепку 📎 рядом с полем ввода → «Камера» или «Файл», "
    "сделайте снимок / выберите файл и отправьте сюда.\n\n"
    "Поддерживаются изображения JPG, PNG, WebP и PDF. Чтобы прервать, "
    "отправьте /cancel или нажмите «❌ Отмена»."
)

log = logging.getLogger("max-docs-bot")


def load_dotenv(path: Path) -> None:
    """Крошечная замена python-dotenv: читает KEY=VALUE из .env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
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


load_dotenv(BASE_DIR / ".env")

MAX_API_URL = os.getenv("MAX_API_URL", "https://platform-api2.max.ru").strip().rstrip("/")
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# Поля анкеты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    short: str
    kind: str = "text"  # text | enum | hours | date
    options: tuple[str, ...] = ()
    optional: bool = False


FIELDS: dict[str, tuple[Field, ...]] = {
    "конкурс": (
        Field("title", "Название конкурса", "Название"),
        Field("organizer", "Организатор", "Организатор"),
        Field("level", "Уровень конкурса", "Уровень", kind="enum", options=LEVELS),
        Field("dates", "Даты проведения", "Даты", kind="date"),
        Field("result", "Результат", "Результат", kind="enum", options=RESULTS),
    ),
    "курс": (
        Field("title", "Название курса", "Название"),
        Field("organizer", "Организатор", "Организатор"),
        Field("dates", "Дата выдачи", "Дата выдачи", kind="date"),
        Field("hours", "Количество часов", "Часы", kind="hours"),
        Field(
            "registration_number",
            "Регистрационный номер",
            "Рег. номер",
            optional=True,
        ),
    ),
}


# ---------------------------------------------------------------------------
# Состояние диалога (в памяти, без третьей таблицы)
# ---------------------------------------------------------------------------


@dataclass
class Draft:
    user_id: int
    kind: str
    photo_path: Path
    values: dict[str, Any]
    index: int = 0
    return_to_summary: bool = False


@dataclass
class Session:
    state: str = "new"  # new | menu | await_photo | review | edit | summary
    pending_kind: Optional[str] = None
    draft: Optional[Draft] = None


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def inline_keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def callback_button(text: str, payload: str) -> dict[str, str]:
    return {"type": "callback", "text": text, "payload": payload}


def menu_keyboard() -> list[list[dict[str, str]]]:
    return [
        [callback_button("🏅 Добавить грамоту", "add:конкурс")],
        [callback_button("🎓 Добавить курс ПК", "add:курс")],
    ]


def options_keyboard(field: Field) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for option in field.options:
        row.append(callback_button(option, f"option:{field.key}:{option}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def cancel_row() -> list[dict[str, str]]:
    return [callback_button("❌ Отмена", "cancel")]


def with_cancel(rows: list[list[dict[str, str]]]) -> list[list[dict[str, str]]]:
    return list(rows) + [cancel_row()]


# ---------------------------------------------------------------------------
# Клиент MAX Bot API
# ---------------------------------------------------------------------------


def _retry_after(response: requests.Response) -> float:
    try:
        return max(0.5, min(10.0, float(response.headers.get("Retry-After", "1"))))
    except (TypeError, ValueError):
        return 1.0


class MaxApi:
    """Тонкая обёртка над platform-api2.max.ru."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"Authorization": token})
        self._last_send: dict[int, float] = {}

    def _throttle(self, user_id: int) -> None:
        # MAX разрешает не больше 2 сообщений в секунду в один диалог.
        now = time.monotonic()
        wait = 0.55 - (now - self._last_send.get(user_id, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_send[user_id] = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 20,
        retries: int = 2,
    ) -> dict[str, Any]:
        url = f"{MAX_API_URL}{path}"
        last_response: Optional[requests.Response] = None
        for attempt in range(retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    timeout=timeout,
                    verify=False,
                )
            except requests.RequestException:
                # POST не повторяем, чтобы не отправить сообщение дважды.
                if method == "GET" and attempt + 1 < retries:
                    time.sleep(1)
                    continue
                raise

            last_response = response
            if response.status_code == 429:
                delay = _retry_after(response)
                log.warning("Лимит запросов MAX, пауза %.1f с", delay)
                time.sleep(delay)
                continue
            if response.status_code >= 500 and method == "GET" and attempt + 1 < retries:
                time.sleep(2)
                continue
            if not response.ok:
                log.error(
                    "MAX API %s %s -> %s: %s",
                    method,
                    path,
                    response.status_code,
                    response.text[:500],
                )
                response.raise_for_status()
            return response.json() if response.content else {}

        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError(f"Не удалось выполнить {method} {path}")

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def updates(self, marker: Optional[int]) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        return self._request(
            "GET", "/updates", params=params, timeout=POLL_TIMEOUT + 15
        )

    def create_upload(self, upload_type: str) -> dict[str, Any]:
        return self._request("POST", "/uploads", params={"type": upload_type})

    def send(
        self,
        user_id: int,
        text: str,
        keyboard: Optional[list[list[dict[str, str]]]] = None,
        attachment: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._throttle(user_id)
        body: dict[str, Any] = {"text": text[:4000]}
        attachments: list[dict[str, Any]] = []
        if attachment is not None:
            attachments.append(attachment)
        if keyboard is not None:
            attachments.append(inline_keyboard(keyboard))
        if attachments:
            body["attachments"] = attachments
        return self._request(
            "POST", "/messages", params={"user_id": user_id}, payload=body
        )

    def send_photo(
        self,
        user_id: int,
        text: str,
        token: str,
        keyboard: Optional[list[list[dict[str, str]]]] = None,
    ) -> dict[str, Any]:
        attachment = {"type": "image", "payload": {"token": token}}
        for attempt in range(5):
            try:
                return self.send(user_id, text, keyboard=keyboard, attachment=attachment)
            except requests.HTTPError as exc:
                if self._attachment_not_ready(exc):
                    time.sleep(1 + attempt)
                    continue
                raise
        raise RuntimeError("MAX не успел обработать фото")

    @staticmethod
    def _attachment_not_ready(exc: requests.HTTPError) -> bool:
        try:
            return ((exc.response.json() or {}).get("code") == "attachment.not.ready")
        except Exception:
            return False

    def answer(
        self,
        callback_id: str,
        *,
        text: Optional[str] = None,
        notification: Optional[str] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if text is not None:
            # Пустой attachments очищает клавиатуру у отредактированного сообщения.
            body["message"] = {"text": text[:4000], "attachments": []}
        if notification:
            body["notification"] = notification[:128]
        if not body:
            body["notification"] = "Готово"
        return self._request(
            "POST", "/answers", params={"callback_id": callback_id}, payload=body
        )

    def download(self, url: str) -> requests.Response:
        response = requests.get(url, timeout=60, verify=False)
        if response.status_code in (401, 403):
            response = requests.get(
                url,
                timeout=60,
                verify=False,
                headers={"Authorization": self.token},
            )
        response.raise_for_status()
        return response

    def upload_image(self, path: Path) -> str:
        info = self.create_upload("image")
        url = info.get("url")
        if not url:
            raise RuntimeError("MAX не вернул URL для загрузки")

        mime = storage.guess_image_mime(path)
        with path.open("rb") as handle:
            response = requests.post(
                url,
                files={"data": (path.name, handle, mime)},
                timeout=120,
                verify=False,
            )
            if response.status_code in (401, 403):
                handle.seek(0)
                response = requests.post(
                    url,
                    files={"data": (path.name, handle, mime)},
                    headers={"Authorization": self.token},
                    timeout=120,
                    verify=False,
                )
        response.raise_for_status()

        token = info.get("token")
        if not token and response.content:
            try:
                payload = response.json() or {}
            except ValueError:
                payload = {}
            token = payload.get("token")
            if not token:
                photos = payload.get("photos") or {}
                first = photos.get("0") or photos.get(0) or {}
                token = first.get("token")
        if not token:
            raise RuntimeError("MAX не вернул токен загруженного фото")
        return str(token)


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------


class Bot:
    def __init__(self, api: MaxApi, db: database.Database) -> None:
        self.api = api
        self.db = db
        self.sessions: dict[int, Session] = {}
        self._photo_tokens: dict[tuple[str, int, int], str] = {}

    # --- сессии ---

    def session(self, user_id: int) -> Session:
        session = self.sessions.get(user_id)
        if session is None:
            user = self.db.get_user(user_id)
            session = Session(state="menu" if user and user.fio else "new")
            self.sessions[user_id] = session
        return session

    def cancel_draft(self, session: Session) -> None:
        if session.draft is not None:
            storage.remove_file(session.draft.photo_path)
            session.draft = None
        session.pending_kind = None

    # --- маршрутизация ---

    def handle_update(self, update: dict[str, Any]) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            user_id = (update.get("user") or {}).get("user_id")
            if user_id:
                self.on_start(int(user_id))
        elif kind == "message_created":
            message = update.get("message") or {}
            if self.chat_type(message) not in (None, "dialog"):
                return
            self.on_message(message)
        elif kind == "message_callback":
            message = update.get("message") or {}
            if message and self.chat_type(message) not in (None, "dialog"):
                return
            self.on_callback(update)

    @staticmethod
    def chat_type(message: dict[str, Any]) -> Optional[str]:
        return (message.get("recipient") or {}).get("chat_type")

    # --- входящие сообщения ---

    def on_message(self, message: dict[str, Any]) -> None:
        sender = message.get("sender") or {}
        if sender.get("is_bot"):
            return
        user_id = sender.get("user_id") or (message.get("recipient") or {}).get("user_id")
        if not user_id:
            return
        user_id = int(user_id)

        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments") or []

        command = text.split()[0].lower() if text else ""
        command = command.split("@", 1)[0]

        if command == "/start":
            self.on_start(user_id)
            return
        if command == "/cancel":
            self.on_cancel(user_id)
            return
        if command == "/my":
            user = self.db.ensure_user(user_id)
            if not user.fio:
                session = self.session(user_id)
                self.change_state(session, "new")
                self.api.send(
                    user_id,
                    "Сначала введите ФИО, пожалуйста. Например: Иванова Мария Петровна",
                )
            else:
                self.send_summary(user_id)
            return

        user = self.db.ensure_user(user_id)
        session = self.session(user_id)

        if attachments:
            if not user.fio:
                self.change_state(session, "new")
                self.api.send(user_id, "Сначала введите ФИО, пожалуйста.")
                return
            if session.state == "await_photo":
                attachment = next(
                    (
                        item
                        for item in attachments
                        if item.get("type") in ("image", "photo", "file")
                    ),
                    None,
                )
                if attachment is None:
                    self.api.send(user_id, PHOTO_PROMPT, [cancel_row()])
                    return
                self.on_document(user_id, session, attachment)
            elif session.state in ("review", "edit", "summary"):
                self.api.send(
                    user_id,
                    "Сначала завершите текущий документ или нажмите «❌ Отмена».",
                    [cancel_row()],
                )
            else:
                self.api.send(
                    user_id,
                    "Чтобы добавить документ, нажмите кнопку ниже.",
                    menu_keyboard(),
                )
            return

        if not text:
            return

        if session.state == "new":
            self.on_fio(user_id, text)
        elif session.state == "menu":
            self.api.send(
                user_id,
                "Выберите действие на клавиатуре ниже или отправьте /my.",
                menu_keyboard(),
            )
        elif session.state == "await_photo":
            self.api.send(user_id, PHOTO_PROMPT, [cancel_row()])
        elif session.state in ("review", "edit"):
            self.on_text_answer(user_id, session, text)
        elif session.state == "summary":
            self.api.send(
                user_id,
                "Нажмите «💾 Сохранить» или «✏️ …», чтобы изменить поле.",
                [callback_button("💾 Сохранить", "save"), callback_button("❌ Отменить", "cancel")],
            )
        else:
            self.on_start(user_id)

    # --- команды ---

    def change_state(self, session: Session, state: str) -> None:
        session.state = state

    def on_start(self, user_id: int) -> None:
        user = self.db.ensure_user(user_id)
        session = self.session(user_id)
        self.cancel_draft(session)

        if user.fio:
            self.change_state(session, "menu")
            self.send_summary(user_id, header=f"Здравствуйте, {user.fio}!")
        else:
            self.change_state(session, "new")
            self.api.send(
                user_id,
                "Здравствуйте! Я помогу собрать портфолио: грамоты за конкурсы "
                "и курсы повышения квалификации.\n\n"
                "Как вас зовут? Введите ФИО полностью, например:\n"
                "Иванова Мария Петровна",
            )

    def on_cancel(self, user_id: int) -> None:
        user = self.db.ensure_user(user_id)
        session = self.session(user_id)
        self.cancel_draft(session)

        if user.fio:
            self.change_state(session, "menu")
            self.api.send(user_id, "Действие отменено. Что добавляем?", menu_keyboard())
        else:
            self.change_state(session, "new")
            self.api.send(user_id, "Действие отменено. Введите ФИО, пожалуйста:")

    def on_fio(self, user_id: int, text: str) -> None:
        fio = re.sub(r"\s+", " ", text).strip()
        if not (5 <= len(fio) <= 200) or len(fio.split()) < 2:
            self.api.send(
                user_id,
                "Похоже, это не ФИО. Введите фамилию, имя и отчество через пробел, "
                "например: Иванова Мария Петровна",
            )
            return

        self.db.set_fio(user_id, fio)
        session = self.session(user_id)
        self.change_state(session, "menu")
        self.send_summary(user_id, header=f"Спасибо, {fio}!")

    # --- кнопки ---

    def answer(
        self,
        callback_id: str,
        *,
        text: Optional[str] = None,
        notification: Optional[str] = None,
    ) -> None:
        """Ответ на callback не должен ломать сценарий, если сообщение удалено."""
        try:
            self.api.answer(callback_id, text=text, notification=notification)
        except requests.RequestException:
            log.warning("Не удалось ответить на callback %s", callback_id, exc_info=True)

    def on_callback(self, update: dict[str, Any]) -> None:
        callback = update.get("callback") or {}
        user_id = (callback.get("user") or {}).get("user_id")
        callback_id = callback.get("callback_id")
        payload = callback.get("payload") or ""
        if not user_id or not callback_id:
            return

        user_id = int(user_id)
        user = self.db.ensure_user(user_id)
        session = self.session(user_id)

        if payload.startswith("add:"):
            self.on_add(user_id, user, session, callback_id, payload[4:])
            return
        if payload.startswith("keep:"):
            self.on_keep(user_id, session, callback_id, payload[5:])
            return
        if payload.startswith("edit:"):
            self.on_edit(user_id, session, callback_id, payload[5:])
            return
        if payload.startswith("skip:"):
            self.on_skip(user_id, session, callback_id, payload[5:])
            return
        if payload.startswith("option:"):
            _, _, rest = payload.partition(":")
            key, _, value = rest.partition(":")
            self.on_option(user_id, session, callback_id, key, value)
            return
        if payload == "save":
            self.on_save(user_id, session, callback_id)
            return
        if payload == "cancel":
            self.on_cancel_callback(user_id, session, callback_id)
            return
        if payload == "back":
            self.on_back(user_id, session, callback_id)
            return
        if payload == "menu":
            self.answer(callback_id, notification="К отчёту")
            self.send_summary(user_id)
            return
        if payload.startswith("view:"):
            self.on_view(user_id, callback_id, payload[5:])
            return

        self.answer(callback_id, notification="Кнопка устарела. Отправьте /start")

    def on_add(
        self,
        user_id: int,
        user: database.User,
        session: Session,
        callback_id: str,
        kind: str,
    ) -> None:
        if kind not in FIELDS:
            self.answer(callback_id, notification="Неизвестный тип документа")
            return
        if not user.fio:
            self.answer(
                callback_id,
                text="Сначала введите ФИО. Отправьте его одним сообщением.",
                notification="Сначала введите ФИО",
            )
            self.change_state(session, "new")
            return
        if session.state in ("await_photo", "review", "edit", "summary"):
            self.answer(
                callback_id,
                notification="Сначала завершите текущий документ или отправьте /cancel",
            )
            return

        self.cancel_draft(session)
        session.pending_kind = kind
        self.change_state(session, "await_photo")
        self.answer(
            callback_id,
            text=f"➕ Добавляем {KIND_LABELS[kind]}",
            notification="Теперь пришлите фото или PDF",
        )
        self.api.send(user_id, PHOTO_PROMPT, [cancel_row()])

    # --- фото и распознавание ---

    def fetch_attachment(
        self, attachment: dict[str, Any]
    ) -> tuple[bytes, str, Optional[str]]:
        payload = attachment.get("payload") or {}
        url = payload.get("url")
        if not url:
            raise ValueError("во вложении нет url")
        response = self.api.download(url)
        mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        filename = payload.get("filename")
        return response.content, mime, filename

    def on_document(
        self,
        user_id: int,
        session: Session,
        attachment: dict[str, Any],
    ) -> None:
        kind = session.pending_kind
        if kind not in FIELDS:
            self.cancel_draft(session)
            self.change_state(session, "menu")
            self.api.send(user_id, "Сначала выберите тип документа:", menu_keyboard())
            return

        try:
            data, mime, filename = self.fetch_attachment(attachment)
        except Exception as exc:
            log.warning("Не удалось скачать вложение: %s", exc)
            self.api.send(
                user_id,
                "Не удалось получить файл 😔 Попробуйте отправить ещё раз.",
                [cancel_row()],
            )
            return

        log.info(
            "Вложение: type=%r filename=%r mime=%r size=%s",
            attachment.get("type"),
            filename,
            mime,
            len(data),
        )

        if not data:
            self.api.send(
                user_id,
                "Файл пришёл пустым. Попробуйте отправить его ещё раз.",
                [cancel_row()],
            )
            return
        if len(data) > MAX_FILE_BYTES:
            self.api.send(
                user_id,
                "Файл больше 50 МБ. Сфотографируйте документ ещё раз — камера "
                "мессенджера обычно сжимает снимок автоматически.",
                [cancel_row()],
            )
            return

        document_type = storage.detect_document_type(filename, mime, data)
        if document_type is None:
            log.warning(
                "Неподдерживаемое вложение: type=%r filename=%r mime=%r size=%s",
                attachment.get("type"),
                filename,
                mime,
                len(data),
            )
            self.api.send(
                user_id,
                "Не удалось определить формат файла. Поддерживаются изображения "
                "JPG, PNG, WebP и PDF. Пришлите документ ещё раз.",
                [cancel_row()],
            )
            return

        note = ""
        photo_path: Optional[Path] = None
        pdf_path: Optional[Path] = None

        try:
            if document_type == "pdf":
                pdf_path = storage.save_temp(data, ".pdf", base_dir=DATA_DIR)
                png_path = storage.photo_dir(DATA_DIR, user_id) / f"{os.urandom(16).hex()}.png"
                storage.pdf_first_page_to_png(pdf_path, png_path)
                photo_path = png_path

                try:
                    text = storage.pdf_first_page_text(pdf_path)
                except Exception:
                    text = ""

                if text:
                    values = imgToFilds.extract_info(text, kind)
                else:
                    values = imgToFilds.recognize_image(photo_path, kind)
            else:
                # Картинку не конвертируем: сохраняем как есть.
                # Уменьшение до лимитов OCR делает imgToFilds.recognize_image.
                photo_path = storage.save_photo(
                    data,
                    user_id,
                    base_dir=DATA_DIR,
                    mime=mime,
                    filename=filename,
                )
                values = imgToFilds.recognize_image(photo_path, kind)
        except Exception as exc:
            log.exception("Ошибка распознавания документа: %s", exc)
            values = imgToFilds.empty_info(kind)
            note = (
                "\n\n⚠️ Не удалось распознать текст. Заполним поля вручную — "
                "отправляйте значения сообщениями."
            )
        finally:
            if pdf_path is not None:
                storage.remove_file(pdf_path)

        if photo_path is None or not photo_path.exists():
            self.api.send(
                user_id,
                "Не удалось подготовить документ. Попробуйте отправить другой файл.",
                [cancel_row()],
            )
            return

        if not isinstance(values, dict):
            values = imgToFilds.empty_info(kind)

        values = {key: values.get(key) for key in imgToFilds.FIELDS}
        values["kind"] = kind
        if kind == "курс":
            values["level"] = None
            values["result"] = None
        else:
            values["hours"] = None
            values["registration_number"] = None

        self.cancel_draft(session)
        session.draft = Draft(
            user_id=user_id,
            kind=kind,
            photo_path=photo_path,
            values=values,
        )
        session.pending_kind = None
        self.change_state(session, "review")

        self.api.send(
            user_id,
            "Фото сохранено ✅\n"
            "Проверьте распознанные данные. Каждое поле можно оставить или изменить."
            + note,
        )
        self.ask_field(user_id, session.draft, FIELDS[kind][0])

    # --- пошаговое редактирование ---

    @staticmethod
    def current_field(draft: Draft) -> Field:
        return FIELDS[draft.kind][draft.index]

    def format_value(self, field: Field, value: Any) -> str:
        if value is None or value == "":
            return "не распознано"
        if field.kind == "hours":
            return f"{value} ч"
        return str(value)

    def ask_field(self, user_id: int, draft: Draft, field: Field) -> None:
        fields = FIELDS[draft.kind]
        index = draft.index + 1
        value = draft.values.get(field.key)

        lines = [f"{index}/{len(fields)}. {field.label}"]
        lines.append(f"Сейчас: {self.format_value(field, value)}")

        keyboard: list[list[dict[str, str]]] = []

        if field.kind == "enum":
            if value not in (None, ""):
                keyboard.append(
                    [
                        callback_button("✅ Оставить", f"keep:{field.key}"),
                        callback_button("✏️ Изменить", f"edit:{field.key}"),
                    ]
                )
            else:
                keyboard = options_keyboard(field)
                lines.append("Выберите вариант кнопкой ниже.")
        else:
            if value not in (None, ""):
                keyboard.append(
                    [
                        callback_button("✅ Оставить", f"keep:{field.key}"),
                        callback_button("✏️ Изменить", f"edit:{field.key}"),
                    ]
                )
            else:
                lines.append("Отправьте значение одним сообщением.")
                if field.kind == "date":
                    lines.append("Например: 17.08.2026 или 12.05.2025 – 15.05.2025")
                elif field.kind == "hours":
                    lines.append("Например: 72")
                if field.optional:
                    keyboard.append(
                        [callback_button("⏭ Пропустить", f"skip:{field.key}")]
                    )

        keyboard.append(cancel_row())
        self.api.send(user_id, "\n".join(lines), keyboard)

    def ask_edit_field(self, user_id: int, draft: Draft, field: Field) -> None:
        keyboard: list[list[dict[str, str]]] = []

        if field.kind == "enum":
            keyboard = options_keyboard(field)
            hint = "Выберите вариант кнопкой ниже."
        else:
            hint = "Отправьте новое значение одним сообщением."
            if field.kind == "hours":
                hint += "\nНапример: 72"
            elif field.kind == "date":
                hint += "\nНапример: 17.08.2026 или 12.05.2025 – 15.05.2025"

        keyboard.append([callback_button("↩️ Назад", "back")])
        keyboard.append(cancel_row())
        self.api.send(
            user_id,
            f"✏️ {field.label}\n{hint}",
            keyboard,
        )

    def on_keep(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
        key: str,
    ) -> None:
        if session.state != "review" or session.draft is None:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft
        field = self.current_field(draft)
        if field.key != key:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        if draft.values.get(key) in (None, ""):
            self.answer(callback_id, notification="Значение не распознано — введите его текстом")
            return

        self.answer(callback_id, text=f"✅ {field.short}: оставлено")
        self.finish_field(user_id, session, draft)

    def on_skip(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
        key: str,
    ) -> None:
        if session.state != "review" or session.draft is None:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft
        field = self.current_field(draft)
        if field.key != key or not field.optional:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return

        draft.values[key] = None
        self.answer(callback_id, text=f"⏭ {field.short}: пропущено")
        self.finish_field(user_id, session, draft)

    def on_edit(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
        key: str,
    ) -> None:
        if session.draft is None or session.state not in ("review", "summary"):
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft
        fields = FIELDS[draft.kind]
        keys = [field.key for field in fields]
        if key not in keys:
            self.answer(callback_id, notification="Неизвестное поле")
            return

        if session.state == "review" and self.current_field(draft).key != key:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return

        draft.index = keys.index(key)
        draft.return_to_summary = session.state == "summary"
        self.change_state(session, "edit")

        field = fields[draft.index]
        self.answer(callback_id, text=f"✏️ {field.short}: изменить")
        self.ask_edit_field(user_id, draft, field)

    def on_back(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
    ) -> None:
        if session.draft is None or session.state != "edit":
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft
        if draft.return_to_summary:
            draft.return_to_summary = False
            self.change_state(session, "summary")
            self.answer(callback_id, notification="К сводке")
            self.send_summary_card(user_id, draft)
        else:
            self.change_state(session, "review")
            self.answer(callback_id, notification="Назад")
            self.ask_field(user_id, draft, self.current_field(draft))

    def on_option(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
        key: str,
        value: str,
    ) -> None:
        if session.draft is None or session.state not in ("review", "edit"):
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft
        field = self.current_field(draft)
        if field.key != key or field.kind != "enum":
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return

        chosen = self.match_option(field, value)
        if chosen is None:
            self.answer(callback_id, notification="Неизвестный вариант")
            return

        draft.values[key] = chosen
        self.answer(callback_id, text=f"✅ {field.short}: {chosen}")
        self.finish_field(user_id, session, draft)

    def on_text_answer(self, user_id: int, session: Session, text: str) -> None:
        if session.draft is None:
            self.change_state(session, "menu")
            self.api.send(
                user_id,
                "Черновик не найден. Начнём заново — выберите документ:",
                menu_keyboard(),
            )
            return

        draft = session.draft
        field = self.current_field(draft)
        raw = re.sub(r"\s+", " ", text).strip()

        if raw.lower() in ("/skip", "пропустить") and field.optional:
            draft.values[field.key] = None
            self.finish_field(user_id, session, draft)
            return

        error = ""
        value: Any = None

        if field.kind == "enum":
            chosen = self.match_option(field, raw)
            if chosen is None:
                self.api.send(
                    user_id,
                    "Не понял вариант. Пожалуйста, выберите кнопкой:",
                    with_cancel(options_keyboard(field)),
                )
                return
            value = chosen
        elif field.kind == "hours":
            match = re.search(r"\d{1,4}", raw)
            if not match or not (1 <= int(match.group(0)) <= 10000):
                error = "Количество часов — целое число от 1 до 10000. Попробуйте ещё раз:"
            else:
                value = int(match.group(0))
        elif field.kind == "date":
            normalized = report.normalize_dates(raw, draft.kind)
            if normalized is None:
                error = (
                    "Не удалось понять дату. Напишите в формате 17.08.2026 "
                    "или 12.05.2025 – 15.05.2025:"
                )
            else:
                value = normalized
        else:
            if not (1 <= len(raw) <= 500):
                error = "Ответ должен быть текстом до 500 символов. Попробуйте ещё раз:"
            else:
                value = raw

        if error:
            self.api.send(user_id, error, [cancel_row()])
            return

        draft.values[field.key] = value
        self.finish_field(user_id, session, draft)

    def finish_field(self, user_id: int, session: Session, draft: Draft) -> None:
        if draft.return_to_summary:
            draft.return_to_summary = False
            self.change_state(session, "summary")
            self.send_summary_card(user_id, draft)
            return

        draft.index += 1
        fields = FIELDS[draft.kind]
        if draft.index >= len(fields):
            self.change_state(session, "summary")
            self.send_summary_card(user_id, draft)
        else:
            self.change_state(session, "review")
            self.ask_field(user_id, draft, fields[draft.index])

    @staticmethod
    def match_option(field: Field, value: str) -> Optional[str]:
        needle = value.strip().lower()
        for option in field.options:
            normalized = option.lower()
            if needle == normalized or needle in normalized or normalized in needle:
                return option
        return None

    # --- сводка перед сохранением ---

    def send_summary_card(self, user_id: int, draft: Draft) -> None:
        lines = ["📄 Проверьте данные документа:", ""]
        for field in FIELDS[draft.kind]:
            lines.append(
                f"• {field.short}: {self.format_value(field, draft.values.get(field.key))}"
            )
        lines.append("")
        lines.append("Если всё верно — сохраните документ.")

        keyboard: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for field in FIELDS[draft.kind]:
            row.append(callback_button(f"✏️ {field.short}", f"edit:{field.key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([callback_button("💾 Сохранить", "save")])
        keyboard.append([callback_button("❌ Отменить", "cancel")])

        self.api.send(user_id, "\n".join(lines), keyboard)

    def on_save(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
    ) -> None:
        if session.state != "summary" or session.draft is None:
            self.answer(callback_id, notification="Этот шаг уже неактуален")
            return
        draft = session.draft

        for index, field in enumerate(FIELDS[draft.kind]):
            if field.optional:
                continue
            if draft.values.get(field.key) in (None, ""):
                draft.index = index
                draft.return_to_summary = False
                self.change_state(session, "review")
                self.answer(
                    callback_id,
                    notification=f"Заполните: {field.short}",
                )
                self.ask_field(user_id, draft, field)
                return

        try:
            # Всегда храним путь в POSIX-виде (data/photos/...) —
            # так он одинаково читается на Windows и Linux.
            photo_path = draft.photo_path.relative_to(BASE_DIR).as_posix()
        except ValueError:
            photo_path = draft.photo_path.as_posix()

        try:
            document = self.db.add_document(
                user_id=user_id,
                kind=draft.kind,
                title=draft.values.get("title"),
                organizer=draft.values.get("organizer"),
                level=draft.values.get("level"),
                dates=draft.values.get("dates"),
                result=draft.values.get("result"),
                hours=draft.values.get("hours"),
                registration_number=draft.values.get("registration_number"),
                photo_path=photo_path,
            )
        except Exception as exc:
            log.exception("Не удалось сохранить документ: %s", exc)
            self.answer(callback_id, notification="Ошибка сохранения, попробуйте ещё раз")
            return

        session.draft = None
        session.pending_kind = None
        self.change_state(session, "menu")

        self.answer(callback_id, text="✅ Документ сохранён")
        header = f"✅ «{document.title or 'Документ'}» добавлен в портфолио."
        start, end = report.current_period(date.today())
        if not report.document_in_period(document.dates or "", document.kind, start, end):
            header += (
                "\n\nℹ️ Дата документа не входит в текущий отчётный период "
                f"({report.format_period(start, end)}), поэтому в списке ниже его нет."
            )
        self.send_summary(user_id, header=header)

    def on_cancel_callback(
        self,
        user_id: int,
        session: Session,
        callback_id: str,
    ) -> None:
        self.cancel_draft(session)
        self.change_state(session, "menu")
        self.answer(callback_id, text="❌ Добавление отменено")
        self.send_summary(user_id)

    # --- просмотр документа ---

    def resolve_photo_path(self, photo_path: Optional[str]) -> Optional[Path]:
        if not photo_path:
            return None
        path = Path(photo_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    def on_view(self, user_id: int, callback_id: str, document_id: str) -> None:
        try:
            document = self.db.get_document(int(document_id))
        except (TypeError, ValueError):
            document = None
        if document is None or document.user_id != user_id:
            self.answer(callback_id, notification="Документ не найден")
            return

        self.answer(callback_id, notification="Открываю документ")
        text = self.format_document(document)
        photo_path = self.resolve_photo_path(document.photo_path)

        if photo_path is None or not photo_path.exists():
            self.api.send(
                user_id,
                text + "\n\n⚠️ Файл фото не найден.",
                [[callback_button("📊 К отчёту", "menu")]],
            )
            return

        try:
            token = self.photo_token(photo_path)
            self.api.send_photo(
                user_id,
                text,
                token,
                keyboard=[[callback_button("📊 К отчёту", "menu")]],
            )
        except Exception as exc:
            log.warning("Не удалось отправить фото документа: %s", exc)
            self.api.send(
                user_id,
                text + "\n\n⚠️ Не удалось отправить фото, но данные сохранены.",
                [[callback_button("📊 К отчёту", "menu")]],
            )

    def photo_token(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        token = self._photo_tokens.get(key)
        if token is None:
            token = self.api.upload_image(path)
            self._photo_tokens[key] = token
        return token

    # --- отчёт ---

    def send_summary(self, user_id: int, header: Optional[str] = None) -> None:
        documents = self.db.list_documents(user_id)
        start, end = report.current_period(date.today())
        period_documents = report.filter_period(documents, start, end)
        period_documents = report.sort_documents_by_date(period_documents)

        competitions = [doc for doc in period_documents if doc.kind == "конкурс"]
        courses = [doc for doc in period_documents if doc.kind == "курс"]
        hours = sum(int(doc.hours or 0) for doc in courses)

        lines: list[str] = []
        if header:
            lines.extend([header, ""])
        lines.append(f"📊 Отчётный период: {report.format_period(start, end)}")
        lines.append(f"🏅 Конкурсы: {len(competitions)}")
        lines.append(
            f"🎓 Курсы ПК: {len(courses)}"
            + (f" ({hours} ч)" if hours else "")
        )
        lines.append("")

        if not period_documents:
            lines.append("За этот период документов пока нет.")
            lines.append("")
        else:
            for number, document in enumerate(period_documents, 1):
                lines.append(self.format_document(document, number))
                lines.append("")
            if len(period_documents) > SUMMARY_BUTTON_LIMIT:
                lines.append(
                    f"…и ещё {len(period_documents) - SUMMARY_BUTTON_LIMIT} док. "
                    "Показаны первые."
                )
                lines.append("")

        lines.append("Добавить новый документ:")
        text = "\n".join(lines)

        keyboard: list[list[dict[str, str]]] = []
        for number, document in enumerate(
            period_documents[:SUMMARY_BUTTON_LIMIT], 1
        ):
            title = re.sub(r"\s+", " ", document.title or "Документ").strip()
            label = f"{number}. {title[:38]}"
            keyboard.append([callback_button(label, f"view:{document.id}")])
        keyboard.extend(menu_keyboard())

        self.api.send(user_id, text[:4000], keyboard)

    def format_document(self, document: database.Document, number: Optional[int] = None) -> str:
        prefix = f"{number}. " if number is not None else ""
        if document.kind == "конкурс":
            lines = [f"{prefix}🏅 {document.title or 'Без названия'}"]
            meta: list[str] = []
            if document.organizer:
                meta.append(document.organizer)
            if document.level:
                meta.append(document.level)
            if meta:
                lines.append("   " + " · ".join(meta))
            details: list[str] = []
            if document.dates:
                details.append(document.dates)
            if document.result:
                details.append(f"Результат: {document.result}")
            if details:
                lines.append("   " + " · ".join(details))
            return "\n".join(lines)

        lines = [f"{prefix}🎓 {document.title or 'Без названия'}"]
        if document.organizer:
            lines.append(f"   {document.organizer}")
        details = []
        if document.dates:
            details.append(document.dates)
        if document.hours:
            details.append(f"{document.hours} ч")
        if document.registration_number:
            details.append(f"Рег. № {document.registration_number}")
        if details:
            lines.append("   " + " · ".join(details))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def load_marker() -> Optional[int]:
    try:
        data = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        marker = data.get("marker")
        return int(marker) if marker is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def save_marker(marker: Optional[int]) -> None:
    try:
        if marker is None:
            MARKER_PATH.unlink(missing_ok=True)
        else:
            MARKER_PATH.write_text(json.dumps({"marker": marker}), encoding="utf-8")
    except OSError as exc:
        log.warning("Не удалось сохранить marker: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not BOT_TOKEN:
        log.error(
            "Не задан MAX_BOT_TOKEN. Скопируйте .env.example в .env и вставьте "
            "токен бота (business.max.ru → Чат-боты → ⋮ → Настройки)."
        )
        return 1

    api = MaxApi(BOT_TOKEN)
    db = database.Database(DB_PATH)

    try:
        info = api.me()
    except requests.RequestException as exc:
        log.error("Не удалось подключиться к MAX API: %s", exc)
        log.error("Проверьте токен и доступ к %s.", MAX_API_URL)
        db.close()
        return 1

    log.info(
        "Бот запущен: %s (@%s), user_id=%s",
        info.get("name"),
        info.get("username"),
        info.get("user_id"),
    )
    log.info("База данных: %s", DB_PATH)
    log.info("Long polling запущен, Ctrl+C — остановить")

    bot = Bot(api, db)
    marker = load_marker()

    try:
        while True:
            try:
                result = api.updates(marker)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    log.error(
                        "MAX API отклонил токен (%s). Проверьте MAX_BOT_TOKEN.", status
                    )
                    return 1
                if status == 400:
                    log.warning("Сервер отклонил marker, начинаю с последнего события")
                    marker = None
                    save_marker(None)
                else:
                    log.warning("Ошибка long polling: %s", exc)
                    time.sleep(3)
                continue
            except requests.RequestException as exc:
                log.warning("Сеть недоступна: %s", exc)
                time.sleep(3)
                continue
            except Exception as exc:  # чтобы один сбой не останавливал бота
                log.exception("Неожиданный сбой long polling: %s", exc)
                time.sleep(3)
                continue

            for update in result.get("updates") or []:
                try:
                    bot.handle_update(update)
                except Exception:
                    log.exception("Ошибка обработки %s", update.get("update_type"))

            new_marker = result.get("marker")
            if new_marker is not None and new_marker != marker:
                marker = new_marker
                save_marker(marker)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
