import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as bot_module
import db
import imgToFilds

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
    b"\xc0\xf0\x1f\x00\x05\x05\x02\x00_\xc4\xe2\xf2\x00\x00\x00\x00IEND"
    b"\xaeB`\x82"
)

WEBP_HEADER = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 32


class FakeResponse:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeApi:
    def __init__(self, image_bytes: bytes, content_type: str = "image/png") -> None:
        self.image_bytes = image_bytes
        self.content_type = content_type
        self.sent = []
        self.answers = []
        self.photos = []

    def send(self, user_id, text, keyboard=None, attachment=None):
        self.sent.append(
            {
                "user_id": user_id,
                "text": text,
                "keyboard": keyboard,
                "attachment": attachment,
            }
        )
        return {}

    def send_photo(self, user_id, text, token, keyboard=None):
        self.photos.append(
            {"user_id": user_id, "text": text, "token": token, "keyboard": keyboard}
        )
        return {}

    def answer(self, callback_id, *, text=None, notification=None):
        self.answers.append(
            {"callback_id": callback_id, "text": text, "notification": notification}
        )
        return {}

    def download(self, url):
        return FakeResponse(self.image_bytes, self.content_type)

    def upload_image(self, path):
        return "photo-token"


def message(user_id: int, text: str = "", attachments=None) -> dict:
    return {
        "sender": {"user_id": user_id},
        "recipient": {"chat_type": "dialog"},
        "body": {"text": text, "attachments": attachments or []},
    }


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.database = db.Database(self.tmp_path / "test.sqlite3")
        self.old_data_dir = bot_module.DATA_DIR
        bot_module.DATA_DIR = self.tmp_path / "data"
        bot_module.DATA_DIR.mkdir(parents=True, exist_ok=True)

        image_bytes = PNG_1X1
        self.api = FakeApi(image_bytes)
        self.bot = bot_module.Bot(self.api, self.database)

        self.old_recognize = imgToFilds.recognize_image
        imgToFilds.recognize_image = lambda path, kind=None: {
            "kind": "конкурс",
            "title": "Педагог года",
            "organizer": "Департамент образования",
            "level": "Региональный",
            "dates": "02.11.2026 – 03.11.2026",
            "result": "победитель",
            "hours": None,
            "registration_number": None,
        }

    def tearDown(self):
        imgToFilds.recognize_image = self.old_recognize
        bot_module.DATA_DIR = self.old_data_dir
        self.database.close()
        self.tmp.cleanup()

    def callback(self, payload: str) -> None:
        self.bot.on_callback(
            {
                "callback": {
                    "user": {"user_id": 1},
                    "callback_id": f"cb-{payload}",
                    "payload": payload,
                }
            }
        )

    @staticmethod
    def has_cancel(keyboard) -> bool:
        if not keyboard:
            return False
        return any(
            button.get("payload") == "cancel"
            for row in keyboard
            for button in row
        )

    def test_full_flow(self):
        self.bot.on_start(1)
        self.assertIn("Как вас зовут", self.api.sent[-1]["text"])

        self.bot.on_message(message(1, "Иванова Мария Петровна"))
        self.assertIn("Отчётный период", self.api.sent[-1]["text"])

        self.callback("add:конкурс")
        self.assertIn("Пришлите фото", self.api.sent[-1]["text"])
        self.assertTrue(self.has_cancel(self.api.sent[-1]["keyboard"]))

        self.bot.on_message(
            message(
                1,
                attachments=[{"type": "image", "payload": {"url": "https://example.invalid/photo"}}],
            )
        )
        self.assertTrue(
            any(
                "Проверьте распознанные данные" in item["text"]
                for item in self.api.sent
            )
        )
        self.assertTrue(self.has_cancel(self.api.sent[-1]["keyboard"]))

        for key in ("title", "organizer", "level", "dates", "result"):
            self.callback(f"keep:{key}")

        self.assertIn("Проверьте данные документа", self.api.sent[-1]["text"])
        self.assertTrue(self.has_cancel(self.api.sent[-1]["keyboard"]))

        self.callback("save")
        documents = self.database.list_documents(1)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].title, "Педагог года")
        self.assertIn("photos/1/", documents[0].photo_path)
        self.assertNotIn("\\", documents[0].photo_path)
        self.assertTrue(
            any("Отчётный период" in item["text"] for item in self.api.sent)
        )

        self.callback(f"view:{documents[0].id}")
        self.assertEqual(len(self.api.photos), 1)
        self.assertEqual(self.api.photos[0]["token"], "photo-token")
        self.assertIn("Педагог года", self.api.photos[0]["text"])

    def test_cancel_does_not_save_document(self):
        self.bot.on_start(1)
        self.bot.on_message(message(1, "Иванова Мария Петровна"))
        self.callback("add:конкурс")
        self.bot.on_message(
            message(
                1,
                attachments=[{"type": "image", "payload": {"url": "https://example.invalid/photo"}}],
            )
        )

        photo_dir = bot_module.DATA_DIR / "photos" / "1"
        self.assertTrue(any(photo_dir.iterdir()))

        self.callback("cancel")

        self.assertEqual(self.database.list_documents(1), [])
        self.assertEqual(list(photo_dir.iterdir()), [])
        self.assertTrue(
            any("Добавление отменено" in item["text"] for item in self.api.answers)
        )
        self.assertIn("Добавить новый документ", self.api.sent[-1]["text"])

    def test_webp_without_filename_is_accepted(self):
        self.api.image_bytes = WEBP_HEADER
        self.api.content_type = "application/octet-stream"

        self.bot.on_start(1)
        self.bot.on_message(message(1, "Иванова Мария Петровна"))
        self.callback("add:конкурс")
        self.bot.on_message(
            message(
                1,
                attachments=[
                    {"type": "image", "payload": {"url": "https://example.invalid/photo"}}
                ],
            )
        )

        self.assertTrue(
            any(
                "Проверьте распознанные данные" in item["text"]
                for item in self.api.sent
            )
        )
        files = list((bot_module.DATA_DIR / "photos" / "1").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".webp")


if __name__ == "__main__":
    unittest.main()
