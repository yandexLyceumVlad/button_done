import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import imgToFilds
from PIL import Image


COURSE_ANSWER = """```json
{
  "title": "Искусственный интеллект в школе. Модуль 1",
  "organizer": "АНО ВО «Центральный университет»",
  "issue_date": "17 августа 2026",
  "hours": 32,
  "registration_number": "260548"
}
```"""

COMPETITION_ANSWER = """```json
{
  "title": "Всероссийский конкурс «Педагог года — 2025»",
  "organizer": "Министерство просвещения Российской Федерации",
  "level": "Федеральный",
  "event_dates": "12.05.2025 – 15.05.2025",
  "result": "победитель"
}
```"""


class ExtractInfoTest(unittest.TestCase):
    def setUp(self):
        self.original = imgToFilds._call_llm
        self.calls = []

    def tearDown(self):
        imgToFilds._call_llm = self.original

    def answer(self, response):
        def fake(text, kind):
            self.calls.append((text, kind))
            return response

        imgToFilds._call_llm = fake

    def test_course_fields(self):
        self.answer(COURSE_ANSWER)
        info = imgToFilds.extract_info("текст удостоверения", "курс")

        self.assertEqual(info["kind"], "курс")
        self.assertEqual(info["title"], "Искусственный интеллект в школе. Модуль 1")
        self.assertEqual(info["organizer"], "АНО ВО «Центральный университет»")
        self.assertEqual(info["dates"], "17.08.2026")
        self.assertEqual(info["hours"], 32)
        self.assertEqual(info["registration_number"], "260548")
        self.assertIsNone(info["level"])
        self.assertIsNone(info["result"])

    def test_competition_fields(self):
        self.answer(COMPETITION_ANSWER)
        info = imgToFilds.extract_info("текст диплома", "конкурс")

        self.assertEqual(info["kind"], "конкурс")
        self.assertEqual(
            info["title"], "Всероссийский конкурс «Педагог года — 2025»"
        )
        self.assertEqual(
            info["organizer"], "Министерство просвещения Российской Федерации"
        )
        self.assertEqual(info["level"], "Федеральный")
        self.assertEqual(info["dates"], "12.05.2025 – 15.05.2025")
        self.assertEqual(info["result"], "победитель")
        self.assertIsNone(info["hours"])
        self.assertIsNone(info["registration_number"])

    def test_kind_is_taken_from_expected(self):
        self.answer('{"kind": "конкурс", "title": "Курс"}')
        info = imgToFilds.extract_info("текст", "курс")
        self.assertEqual(info["kind"], "курс")
        self.assertEqual(info["title"], "Курс")

    def test_invalid_answer_is_retried(self):
        responses = iter(["это не JSON", COMPETITION_ANSWER])
        self.answer(None)

        def fake(text, kind):
            self.calls.append((text, kind))
            return next(responses)

        imgToFilds._call_llm = fake
        info = imgToFilds.extract_info("текст", "конкурс")
        self.assertEqual(info["result"], "победитель")
        self.assertEqual(len(self.calls), 2)

    def test_unknown_values_become_none(self):
        self.answer(
            '{"level": "Школьный", "result": "лауреат", "hours": 0}'
        )
        competition = imgToFilds.extract_info("текст", "конкурс")
        self.assertIsNone(competition["level"])
        self.assertIsNone(competition["result"])

        course = imgToFilds.extract_info("текст", "курс")
        self.assertIsNone(course["hours"])

    def test_empty_text_does_not_call_llm(self):
        self.answer(COMPETITION_ANSWER)
        info = imgToFilds.extract_info("   ", "конкурс")
        self.assertEqual(info["kind"], "конкурс")
        self.assertIsNone(info["title"])
        self.assertEqual(self.calls, [])


class PrepareImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_image(self, fmt: str, size=(200, 100), mode="RGB") -> tuple[Path, bytes]:
        buffer = io.BytesIO()
        Image.new(mode, size, "white").save(buffer, format=fmt)
        data = buffer.getvalue()
        path = self.dir / f"document.{fmt.lower()}"
        path.write_bytes(data)
        return path, data

    def test_small_png_is_sent_unchanged(self):
        path, data = self.make_image("PNG")
        prepared, mime = imgToFilds._prepare_image_for_ocr(path)

        self.assertEqual(prepared, data)
        self.assertEqual(mime, "image/png")

    def test_webp_is_converted_only_for_ocr(self):
        path, data = self.make_image("WEBP")
        prepared, mime = imgToFilds._prepare_image_for_ocr(path)

        self.assertNotEqual(prepared, data)
        self.assertIn(mime, ("image/jpeg", "image/png"))
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertEqual(image.size, (200, 100))

    def test_large_jpeg_is_scaled(self):
        path, _ = self.make_image("JPEG", size=(3200, 1000))
        prepared, mime = imgToFilds._prepare_image_for_ocr(path)

        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertLessEqual(max(image.size), imgToFilds.OCR_MAX_SIDE)


if __name__ == "__main__":
    unittest.main()
