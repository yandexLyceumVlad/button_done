import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 32


class DetectDocumentTypeTest(unittest.TestCase):
    def test_jpeg_without_filename_or_mime(self):
        self.assertEqual(
            storage.detect_document_type(None, "application/octet-stream", JPEG),
            "image",
        )

    def test_png_without_filename_or_mime(self):
        self.assertEqual(
            storage.detect_document_type("", "", PNG),
            "image",
        )

    def test_webp_without_filename_or_mime(self):
        self.assertEqual(
            storage.detect_document_type(None, "application/octet-stream", WEBP),
            "image",
        )

    def test_webp_by_extension(self):
        self.assertEqual(
            storage.detect_document_type("scan.webp", "image/webp", WEBP),
            "image",
        )

    def test_pdf_by_content(self):
        self.assertEqual(
            storage.detect_document_type("scan.bin", "application/octet-stream", b"%PDF-1.7"),
            "pdf",
        )

    def test_unknown_file_is_rejected(self):
        self.assertIsNone(
            storage.detect_document_type("scan.docx", "application/msword", b"PK\x03\x04")
        )

    def test_webp_saved_with_correct_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = storage.save_photo(
                WEBP,
                1,
                base_dir=Path(tmp),
                mime="application/octet-stream",
                filename=None,
            )
            self.assertEqual(path.suffix, ".webp")
            self.assertEqual(path.read_bytes(), WEBP)

    def test_jpeg_saved_with_correct_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = storage.save_photo(
                JPEG,
                1,
                base_dir=Path(tmp),
                mime="application/octet-stream",
                filename="photo.bin",
            )
            self.assertEqual(path.suffix, ".jpg")


if __name__ == "__main__":
    unittest.main()
