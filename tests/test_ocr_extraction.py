import unittest
from unittest.mock import patch

from app import google_vision, media_intake


class MediaInsightExtractionTests(unittest.TestCase):
    @patch("app.media_intake.analyze_image", return_value=(None, "vision_http_500"))
    @patch("app.media_intake.extract_text_ocr", return_value=("glucose 118 mg/dL", None))
    @patch("app.media_intake.is_vision_configured", return_value=True)
    def test_image_ocr_runs_even_when_label_detection_fails(self, _vision_enabled, _extract_ocr, _analyze):
        result = media_intake._extract_media_insights(
            data=b"fake-image",
            media_type="image",
            mime_type="image/jpeg",
            caption="meter reading",
        )

        self.assertEqual(result["ocr_text"], "glucose 118 mg/dL")
        self.assertIsNone(result["vision_summary"])
        self.assertEqual(result["processing_error"], "vision_http_500")

    @patch("app.media_intake.extract_document_text", return_value=("HbA1c 6.2%", None))
    @patch("app.media_intake.is_vision_configured", return_value=False)
    def test_pdf_document_extracts_text_without_vision_key(self, _vision_enabled, _extract_document):
        result = media_intake._extract_media_insights(
            data=b"%PDF-1.4 fake",
            media_type="document",
            mime_type="application/pdf",
            caption="lab result",
        )

        self.assertEqual(result["ocr_text"], "HbA1c 6.2%")
        self.assertEqual(result["media_category"], "document")
        self.assertIsNone(result["processing_error"])


class DocumentTextExtractionTests(unittest.TestCase):
    def test_extract_document_text_uses_pdf_reader_for_pdf(self):
        class _FakePage:
            def extract_text(self):
                return "Page one text"

        class _FakeReader:
            def __init__(self, _stream):
                self.pages = [_FakePage()]

        with patch.object(google_vision, "PdfReader", _FakeReader):
            text, err = google_vision.extract_document_text(b"%PDF-1.4 fake", mime_type="application/pdf")

        self.assertEqual(text, "Page one text")
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()