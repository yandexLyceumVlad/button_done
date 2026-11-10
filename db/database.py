"""Создание файла БД и операции с таблицами users/documents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import DOCUMENT_FIELDS, Base, Document, User, utc_now


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    """Включает ON DELETE CASCADE для SQLite."""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
    except Exception:
        # Для не-SQLite подключений (например, в тестах) просто пропускаем.
        pass


class Database:
    """Подключение к SQLite, создание таблиц и операции с данными."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        # Создаёт таблицы users и documents, если их ещё нет.
        Base.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(
            self.engine,
            expire_on_commit=False,
            future=True,
        )

    # --- пользователи ---

    def get_user(self, user_id: int) -> Optional[User]:
        with self._session_factory() as session:
            return session.get(User, int(user_id))

    def ensure_user(self, user_id: int) -> User:
        with self._session_factory() as session:
            user = session.get(User, int(user_id))
            if user is None:
                user = User(user_id=int(user_id))
                session.add(user)
                session.commit()
            return user

    def set_fio(self, user_id: int, fio: str) -> User:
        with self._session_factory() as session:
            user = session.get(User, int(user_id))
            if user is None:
                user = User(user_id=int(user_id))
                session.add(user)
            user.fio = fio
            session.commit()
            return user

    # --- документы ---

    def add_document(self, user_id: int, **fields: Any) -> Document:
        unknown = set(fields) - set(DOCUMENT_FIELDS)
        if unknown:
            raise ValueError(f"Неизвестные поля документа: {sorted(unknown)}")
        if not fields.get("kind"):
            raise ValueError("Не указан тип документа")

        with self._session_factory() as session:
            document = Document(
                user_id=int(user_id),
                created_at=utc_now(),
                **fields,
            )
            session.add(document)
            session.commit()
            session.refresh(document)
            return document

    def get_document(self, document_id: int) -> Optional[Document]:
        with self._session_factory() as session:
            return session.get(Document, int(document_id))

    def list_documents(self, user_id: int) -> list[Document]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(Document)
                .where(Document.user_id == int(user_id))
                .order_by(Document.created_at.desc(), Document.id.desc())
            ).all()
            return list(rows)

    def close(self) -> None:
        self.engine.dispose()


def create_database(path: Path) -> Database:
    """Создаёт файл БД и таблицы users/documents, если их ещё нет."""
    return Database(path)
