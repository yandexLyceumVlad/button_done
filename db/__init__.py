"""Слой работы с БД: ORM-модели и создание SQLite.

Публичный API:
    Database        — подключение, создание таблиц, операции с данными
    create_database — создать файл БД и таблицы users/documents
    User, Document  — ORM-модели
"""

from __future__ import annotations

from .database import Database, create_database
from .models import DOCUMENT_FIELDS, Base, Document, User, utc_now

__all__ = [
    "Base",
    "DOCUMENT_FIELDS",
    "Database",
    "Document",
    "User",
    "create_database",
    "utc_now",
]
