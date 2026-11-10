import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import report


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = db.Database(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.database.close()
        self.tmp.cleanup()

    def test_user_and_document(self):
        user = self.database.ensure_user(42)
        self.assertIsNone(user.fio)

        self.database.set_fio(42, "Иванова Мария Петровна")
        self.assertEqual(self.database.get_user(42).fio, "Иванова Мария Петровна")

        document = self.database.add_document(
            user_id=42,
            kind="конкурс",
            title="Педагог года",
            organizer="Департамент образования",
            level="Региональный",
            dates="02.11.2026 – 03.11.2026",
            result="победитель",
            hours=None,
            registration_number=None,
            photo_path="data/photos/42/test.png",
        )
        self.assertIsNotNone(document.id)
        self.assertEqual(document.created_at[:4], "2026")

        stored = self.database.get_document(document.id)
        self.assertEqual(stored.title, "Педагог года")
        self.assertEqual(len(self.database.list_documents(42)), 1)

    def test_period_filter(self):
        self.database.ensure_user(1)
        self.database.add_document(
            user_id=1,
            kind="курс",
            title="Курс",
            organizer=None,
            level=None,
            dates="17.08.2026",
            result=None,
            hours=32,
            registration_number="260548",
            photo_path="data/photos/1/test.png",
        )
        self.database.add_document(
            user_id=1,
            kind="конкурс",
            title="Конкурс",
            organizer=None,
            level=None,
            dates="02.11.2026 – 03.11.2026",
            result=None,
            hours=None,
            registration_number=None,
            photo_path="data/photos/1/test2.png",
        )

        start, end = date(2026, 9, 1), date(2027, 1, 31)
        documents = report.filter_period(self.database.list_documents(1), start, end)
        self.assertEqual([document.title for document in documents], ["Конкурс"])


if __name__ == "__main__":
    unittest.main()
