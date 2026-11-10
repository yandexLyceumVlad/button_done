"""ORM-модели и общие константы слоя БД.

Две таблицы, как описано в README:
    users      — user_id, fio
    documents  — документы пользователя + путь к фото
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Поля, которые можно записать в documents. Служебные id/created_at
# выставляются автоматически.
DOCUMENT_FIELDS = (
    "kind",
    "title",
    "organizer",
    "level",
    "dates",
    "result",
    "hours",
    "registration_number",
    "photo_path",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fio: Mapped[Optional[str]] = mapped_column(Text)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - удобно в логах
        return f"<User {self.user_id} {self.fio!r}>"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    organizer: Mapped[Optional[str]] = mapped_column(Text)
    level: Mapped[Optional[str]] = mapped_column(Text)
    dates: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[Optional[str]] = mapped_column(Text)
    hours: Mapped[Optional[int]] = mapped_column(Integer)
    registration_number: Mapped[Optional[str]] = mapped_column(Text)
    photo_path: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)

    user: Mapped[User] = relationship(back_populates="documents")

    def __repr__(self) -> str:  # pragma: no cover - удобно в логах
        return f"<Document {self.id} {self.kind} {self.title!r}>"
