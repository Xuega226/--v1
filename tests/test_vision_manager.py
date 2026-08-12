from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from qq_adapter import QQAdapter, extract_image_segments, extract_reply_id
from vision_manager import VisionManager


class FakeAdapter:
    def __init__(self, image_path: str):
        self.image_path = image_path
        self.get_image_calls = 0

    def get_image(self, file_id: str) -> dict:
        self.get_image_calls += 1
        return {"file": self.image_path} if file_id == "qq-file-id" else {}


class FakeVisionManager(VisionManager):
    def __init__(self, root: Path):
        super().__init__(
            cache_db=str(root / "vision.db"),
            cache_dir=str(root / "cache"),
            ollama_url="http://unused",
            model="test-vl",
            ocr_url="http://unused",
        )
        self.ocr_calls = 0
        self.vision_calls = 0

    def _run_ocr(self, image_bytes: bytes, warnings: list[str]) -> str:
        self.ocr_calls += 1
        return "测试文字"

    def _run_vision(self, image_bytes: bytes, ocr_text: str) -> str:
        self.vision_calls += 1
        return "一张用于测试缓存的蓝色图片。"


class VisionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image_path = self.root / "source.png"
        Image.new("RGB", (80, 40), "blue").save(self.image_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_onebot_segment_extractors(self):
        message = [
            {"type": "reply", "data": {"id": 123}},
            {"type": "text", "data": {"text": "看看"}},
            {"type": "image", "data": {"file": "qq-file-id", "url": "https://example/image"}},
        ]
        self.assertEqual(extract_reply_id(message), "123")
        self.assertEqual(extract_image_segments(message), [message[2]["data"]])

    def test_collects_and_deduplicates_replied_images(self):
        adapter = QQAdapter.__new__(QQAdapter)
        adapter.get_message = lambda message_id: {
            "message": [
                {"type": "image", "data": {"file": "current"}},
                {"type": "image", "data": {"file": "replied"}},
            ]
        }
        event = {
            "message": [
                {"type": "reply", "data": {"id": "42"}},
                {"type": "image", "data": {"file": "current"}},
            ]
        }

        images = adapter.collect_event_images(event, include_reply=True)

        self.assertEqual([item["file"] for item in images], ["current", "replied"])

    def test_analysis_is_cached_by_image_hash(self):
        manager = FakeVisionManager(self.root)
        adapter = FakeAdapter(str(self.image_path))
        segment = [{"file": "qq-file-id"}]

        first = manager.analyze(segment, adapter)
        second = manager.analyze(segment, adapter)

        self.assertIn("蓝色图片", first.prompt)
        self.assertIn("测试文字", first.prompt)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(manager.ocr_calls, 1)
        self.assertEqual(manager.vision_calls, 1)
        self.assertEqual(adapter.get_image_calls, 2)

    def test_normalize_resizes_large_edge(self):
        manager = FakeVisionManager(self.root)
        manager.max_edge = 128
        stream = BytesIO()
        Image.new("RGB", (640, 320), "white").save(stream, format="PNG")

        normalized, width, height, path = manager._normalize(stream.getvalue(), "resize")

        self.assertEqual((width, height), (128, 64))
        self.assertTrue(normalized.startswith(b"\xff\xd8"))
        self.assertTrue(Path(path).is_file())

    def test_rejects_invalid_image(self):
        manager = FakeVisionManager(self.root)
        with self.assertRaisesRegex(ValueError, "有效图片"):
            manager._normalize(b"not an image", "invalid")

    def test_rejects_loopback_image_url(self):
        with self.assertRaisesRegex(ValueError, "内网"):
            VisionManager._validate_public_url("http://127.0.0.1/private.png")


if __name__ == "__main__":
    unittest.main()
