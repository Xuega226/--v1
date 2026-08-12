import json
from pathlib import Path
import tempfile
import unittest

from worldbook import WorldBookManager


class FakeEmbedder:
    dimension = 3

    def __init__(self):
        self.batch_sizes = []

    def encode(self, texts, batch_size=12):
        self.batch_sizes.append(len(texts))
        return [[float(len(text) % 7), 1.0, 0.5] for text in texts]


class FakeVectorStore:
    def __init__(self):
        self.points = {}
        self.deleted = []
        self.ready = False

    def healthy(self):
        return True

    def ensure_collection(self, dimension):
        self.ready = dimension == 3

    def upsert(self, points):
        for point in points:
            self.points[point["id"]] = point

    def delete(self, point_ids):
        self.deleted.extend(point_ids)
        for point_id in point_ids:
            self.points.pop(point_id, None)

    def query(self, vector, book_ids, limit):
        hits = []
        for point in self.points.values():
            if point["payload"]["book_id"] in book_ids:
                hits.append({"score": 0.8, "payload": point["payload"]})
        return hits[:limit]


class WorldBookManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.books = root / "books"
        self.books.mkdir()
        self.store = FakeVectorStore()
        self.embedder = FakeEmbedder()
        self.manager = WorldBookManager(
            db_path=str(root / "worldbooks.db"),
            books_dir=str(self.books),
            qdrant_url="http://unused",
            collection="test",
            embed_model="unused",
            embedder=self.embedder,
            vector_store=self.store,
            top_k=8,
            context_tokens=1200,
            rule_tokens=400,
        )
        self.source = self.books / "strict.json"
        self._write_book()

    def tearDown(self):
        self.temp.cleanup()

    def _write_book(self, city_content="蒙德是自由之城。"):
        self.source.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "uid": 1,
                            "comment": "世界基本法",
                            "content": "任何角色都不能突破力量上限。",
                            "constant": True,
                            "priority": 100,
                        },
                        {
                            "uid": 2,
                            "comment": "蒙德",
                            "content": city_content,
                            "key": ["蒙德"],
                            "priority": 30,
                        },
                        {
                            "uid": 3,
                            "comment": "禁术",
                            "content": "禁术会反噬施术者。",
                            "key": ["魔法"],
                            "keysecondary": ["禁术"],
                            "priority": 50,
                        },
                        {
                            "uid": 4,
                            "comment": "风神历史",
                            "content": "风神曾参与古代战争。",
                        },
                        {
                            "uid": 5,
                            "comment": "限定角色",
                            "content": "琴是代理团长。",
                            "key": ["代理团长"],
                            "scope": {"characters": ["琴"]},
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_import_bind_and_hybrid_retrieve(self):
        report = self.manager.import_source("strict.json", "提瓦特")
        self.assertEqual(report["entries"], 5)
        self.assertEqual(report["indexed"], 5)
        self.assertTrue(self.store.ready)
        self.manager.bind("10001", "提瓦特")

        result = self.manager.retrieve("10001", "蒙德有什么？")
        self.assertIn("力量上限", result.prompt)
        self.assertIn("蒙德是自由之城", result.prompt)
        self.assertNotIn("琴是代理团长", result.prompt)
        self.assertIn("风神曾参与古代战争", result.prompt)

        scoped = self.manager.retrieve("10001", "琴这位代理团长是谁？")
        self.assertIn("琴是代理团长", scoped.prompt)

    def test_secondary_keyword_is_required(self):
        self.manager.import_source("strict.json", "提瓦特")
        self.manager.bind("10001", "提瓦特")
        without_secondary = self.manager.retrieve("10001", "这里能使用魔法吗？")
        self.assertNotIn("精确触发｜禁术", without_secondary.prompt)
        with_secondary = self.manager.retrieve("10001", "这个魔法属于禁术吗？")
        self.assertIn("精确触发｜禁术", with_secondary.prompt)

    def test_global_binding_applies_to_every_group(self):
        self.manager.import_source("strict.json", "提瓦特")
        self.manager.bind_global("提瓦特")

        first_group = self.manager.retrieve("10001", "蒙德有什么？")
        other_group = self.manager.retrieve("20002", "蒙德有什么？")

        self.assertIn("力量上限", first_group.prompt)
        self.assertIn("蒙德是自由之城", first_group.prompt)
        self.assertIn("力量上限", other_group.prompt)
        self.assertIn("蒙德是自由之城", other_group.prompt)

    def test_incremental_reload_only_indexes_changes(self):
        self.manager.import_source("strict.json", "提瓦特")
        unchanged = self.manager.reload("提瓦特")[0]
        self.assertEqual(unchanged["indexed"], 0)
        self.assertEqual(unchanged["unchanged"], 5)

        self._write_book("蒙德是一座崇尚自由的城市。")
        changed = self.manager.reload("提瓦特")[0]
        self.assertEqual(changed["indexed"], 1)
        self.assertEqual(changed["unchanged"], 4)

    def test_commands_and_owner_permissions(self):
        handled, response = self.manager.handle_command("10001", "/world import strict.json 提瓦特", False)
        self.assertTrue(handled)
        self.assertIn("只有主人", response)
        _, response = self.manager.handle_command("10001", "/world import strict.json 提瓦特", True)
        self.assertIn("已导入", response)
        _, response = self.manager.handle_command("10001", "/world use 提瓦特", True)
        self.assertIn("已启用", response)
        _, response = self.manager.handle_command("10001", "/world status", False)
        self.assertIn("提瓦特", response)

    def test_markdown_and_directory_import(self):
        folder = self.books / "lore"
        folder.mkdir()
        (folder / "places.md").write_text("# 北境\n北境终年积雪。\n\n# 南境\n南境气候温暖。", encoding="utf-8")
        (folder / "notes.txt").write_text("第一段资料。\n\n第二段资料。", encoding="utf-8")
        report = self.manager.import_source("lore", "大陆设定")
        self.assertEqual(report["entries"], 3)

    def test_large_import_is_indexed_in_bounded_batches(self):
        entries = [
            {"id": index, "title": f"条目 {index}", "content": f"大型世界书资料 {index}"}
            for index in range(130)
        ]
        (self.books / "large.json").write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        report = self.manager.import_source("large.json", "大型设定")
        self.assertEqual(report["indexed"], 130)
        self.assertEqual(self.embedder.batch_sizes, [64, 64, 2])

    def test_startup_warmup_eagerly_loads_embedding_and_reports_ready(self):
        status = self.manager.start_warmup(background=False)
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["model_ready"])
        self.assertEqual(self.embedder.batch_sizes, [1])
        self.assertTrue(self.store.ready)
        self.assertEqual(self.manager.status("10001")["warmup"]["state"], "ready")

    def test_background_warmup_can_be_waited_for(self):
        status = self.manager.start_warmup(background=True)
        self.assertIn(status["state"], ("warming", "ready"))
        finished = self.manager.wait_warmup(timeout=2)
        self.assertEqual(finished["state"], "ready")


if __name__ == "__main__":
    unittest.main()
