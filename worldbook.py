"""World-book import, deterministic rules, and Qdrant hybrid retrieval.

SQLite owns book metadata, bindings and exact-trigger rules. Qdrant only owns
semantic vectors, so the bot can still apply hard rules when Qdrant is down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import threading
import time
from typing import Any, Iterable, Iterator
from urllib.parse import quote
import uuid

import requests

try:
    import yaml
except ImportError:  # pragma: no cover - reported clearly by importer
    yaml = None


SUPPORTED_SUFFIXES = {".json", ".yaml", ".yml", ".md", ".markdown", ".txt"}
GLOBAL_GROUP_ID = "__global__"
_WORD_SPLIT_RE = re.compile(r"[,，;；|\n]+")
_MARKDOWN_HEADER_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


def _now() -> int:
    return int(time.time())


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in _WORD_SPLIT_RE.split(value) if item.strip()]
    if isinstance(value, dict):
        return [str(key).strip() for key, enabled in value.items() if enabled and str(key).strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "常驻"}
    return bool(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    # Chinese is usually close to one token per character; Latin text is denser.
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    return chinese + max(1, (len(text) - chinese) // 4)


def _clip_to_token_budget(parts: Iterable[str], budget: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for part in parts:
        cost = _estimate_tokens(part)
        if used + cost <= budget:
            kept.append(part)
            used += cost
            continue
        remaining = budget - used
        if remaining > 80:
            kept.append(part[: max(80, remaining)])
        break
    return kept


@dataclass
class WorldEntry:
    source_id: str
    title: str
    content: str
    keywords: list[str] = field(default_factory=list)
    secondary_keywords: list[str] = field(default_factory=list)
    regexes: list[str] = field(default_factory=list)
    constant: bool = False
    enabled: bool = True
    priority: int = 0
    insertion_order: int = 100
    probability: float = 100.0
    scope_characters: list[str] = field(default_factory=list)
    scope_regions: list[str] = field(default_factory=list)
    scope_groups: list[str] = field(default_factory=list)
    recursive: bool = False
    max_depth: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def entry_hash(self) -> str:
        payload = {
            "title": self.title,
            "content": self.content,
            "keywords": self.keywords,
            "secondary_keywords": self.secondary_keywords,
            "regexes": self.regexes,
            "constant": self.constant,
            "enabled": self.enabled,
            "priority": self.priority,
            "insertion_order": self.insertion_order,
            "probability": self.probability,
            "scope_characters": self.scope_characters,
            "scope_regions": self.scope_regions,
            "scope_groups": self.scope_groups,
            "recursive": self.recursive,
            "max_depth": self.max_depth,
        }
        return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@dataclass
class RetrievalResult:
    prompt: str = ""
    hard_rules: list[str] = field(default_factory=list)
    exact_entries: list[str] = field(default_factory=list)
    semantic_entries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class LocalChineseEmbedder:
    """Lazy local embedding model backed by the project's torch/transformers."""

    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        cache_dir: str | None = None,
        model_source: str = "huggingface",
        modelscope_model: str = "AI-ModelScope/bge-small-zh-v1.5",
    ):
        self.model_name = model_name
        self.requested_device = device
        self.cache_dir = os.path.abspath(cache_dir) if cache_dir else None
        self.model_source = model_source.strip().lower()
        self.modelscope_model = modelscope_model
        self._model = None
        self._tokenizer = None
        self._device = "cpu"
        self._dimension: int | None = None
        self._lock = threading.Lock()
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            import torch
            from transformers import AutoModel, AutoTokenizer

            if self.requested_device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            else:
                device = self.requested_device
            if self.cache_dir:
                os.makedirs(self.cache_dir, exist_ok=True)
            model_path = self._resolve_model_path()
            print(f"[WorldBook] 加载本地向量模型 {model_path} ({device})…")
            tokenizer = AutoTokenizer.from_pretrained(
                model_path, trust_remote_code=False, cache_dir=self.cache_dir
            )
            model = AutoModel.from_pretrained(
                model_path, trust_remote_code=False, cache_dir=self.cache_dir
            )
            model.eval().to(device)
            dimension = int(getattr(model.config, "hidden_size"))
            # Publish initialized objects only after the entire load has
            # completed. A concurrent first message will wait on the lock.
            self._tokenizer = tokenizer
            self._model = model
            self._device = device
            self._dimension = dimension
            self._loaded = True
            print(f"[WorldBook] 向量模型已就绪，维度 {self._dimension}")

    def _resolve_model_path(self) -> str:
        if os.path.isdir(self.model_name):
            return self.model_name
        if self.model_source != "modelscope":
            return self.model_name
        if not self.cache_dir:
            raise RuntimeError("使用 ModelScope 下载源时必须配置 WORLD_BOOK_MODEL_CACHE")
        local_dir = os.path.join(
            self.cache_dir,
            "modelscope",
            self.modelscope_model.replace("/", "--"),
        )
        if os.path.isfile(os.path.join(local_dir, "config.json")):
            return local_dir
        print(f"[WorldBook] 首次从 ModelScope 下载 {self.modelscope_model}…")
        from modelscope import snapshot_download

        resolved = snapshot_download(
            model_id=self.modelscope_model,
            local_dir=local_dir,
        )
        return str(resolved)

    @property
    def dimension(self) -> int:
        self._load()
        return int(self._dimension)

    def encode(self, texts: list[str], batch_size: int = 12) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        import torch
        import torch.nn.functional as functional

        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with torch.inference_mode():
                hidden = self._model(**encoded).last_hidden_state
                mask = encoded["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                pooled = functional.normalize(pooled, p=2, dim=1)
            vectors.extend(pooled.cpu().tolist())
        return vectors


class QdrantStore:
    def __init__(self, base_url: str, collection: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def healthy(self) -> bool:
        try:
            response = requests.get(self._url("/healthz"), timeout=2)
            return response.ok
        except requests.RequestException:
            return False

    def ensure_collection(self, dimension: int):
        encoded = quote(self.collection, safe="")
        response = requests.get(self._url(f"/collections/{encoded}"), timeout=self.timeout)
        if response.status_code == 404:
            response = requests.put(
                self._url(f"/collections/{encoded}"),
                json={"vectors": {"size": dimension, "distance": "Cosine"}},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return
        response.raise_for_status()
        data = response.json().get("result", {})
        configured = data.get("config", {}).get("params", {}).get("vectors", {}).get("size")
        if configured and int(configured) != int(dimension):
            raise RuntimeError(
                f"Qdrant collection {self.collection} 的维度是 {configured}，当前模型需要 {dimension}；"
                "请更换 WORLD_BOOK_QDRANT_COLLECTION 名称后重新索引。"
            )

    def upsert(self, points: list[dict[str, Any]]):
        if not points:
            return
        encoded = quote(self.collection, safe="")
        response = requests.put(
            self._url(f"/collections/{encoded}/points?wait=true"),
            json={"points": points},
            timeout=max(self.timeout, 60),
        )
        response.raise_for_status()

    def delete(self, point_ids: list[str]):
        if not point_ids:
            return
        encoded = quote(self.collection, safe="")
        for start in range(0, len(point_ids), 256):
            response = requests.post(
                self._url(f"/collections/{encoded}/points/delete?wait=true"),
                json={"points": point_ids[start : start + 256]},
                timeout=self.timeout,
            )
            if response.status_code != 404:
                response.raise_for_status()

    def query(self, vector: list[float], book_ids: list[int], limit: int) -> list[dict[str, Any]]:
        if not book_ids:
            return []
        encoded = quote(self.collection, safe="")
        body = {
            "query": vector,
            "filter": {"must": [{"key": "book_id", "match": {"any": book_ids}}]},
            "limit": limit,
            "with_payload": True,
        }
        response = requests.post(
            self._url(f"/collections/{encoded}/points/query"),
            json=body,
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json().get("result", {}).get("points", [])


class WorldBookManager:
    def __init__(
        self,
        db_path: str,
        books_dir: str,
        qdrant_url: str,
        collection: str,
        embed_model: str,
        embed_device: str = "auto",
        model_cache: str | None = None,
        model_source: str = "huggingface",
        modelscope_model: str = "AI-ModelScope/bge-small-zh-v1.5",
        enabled: bool = True,
        top_k: int = 8,
        context_tokens: int = 1600,
        rule_tokens: int = 800,
        recursion_depth: int = 3,
        embedder: Any | None = None,
        vector_store: Any | None = None,
    ):
        self.enabled = enabled
        self.db_path = os.path.abspath(db_path)
        self.books_dir = os.path.abspath(books_dir)
        self.top_k = max(1, top_k)
        self.context_tokens = max(200, context_tokens)
        self.rule_tokens = max(100, rule_tokens)
        self.recursion_depth = max(1, recursion_depth)
        self.embedder = embedder or LocalChineseEmbedder(
            embed_model,
            embed_device,
            model_cache,
            model_source,
            modelscope_model,
        )
        self.vector_store = vector_store or QdrantStore(qdrant_url, collection)
        self._lock = threading.RLock()
        self._warmup_lock = threading.RLock()
        self._warmup_thread: threading.Thread | None = None
        self._warmup_state = "disabled" if not self.enabled else "idle"
        self._warmup_error = ""
        self._warmup_started_at = 0.0
        self._warmup_finished_at = 0.0
        self._warmup_model_ready = False
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.books_dir, exist_ok=True)
        self._init_db()

    def start_warmup(self, *, background: bool = True, force: bool = False) -> dict[str, Any]:
        """Eagerly load the embedding model and verify its Qdrant collection."""
        if not self.enabled:
            return self.warmup_status()
        with self._warmup_lock:
            if self._warmup_state == "warming":
                return self.warmup_status()
            if self._warmup_state == "ready" and not force:
                return self.warmup_status()
            self._warmup_state = "warming"
            self._warmup_error = ""
            self._warmup_started_at = time.time()
            self._warmup_finished_at = 0.0
            if background:
                self._warmup_thread = threading.Thread(
                    target=self._run_warmup,
                    name="worldbook-warmup",
                    daemon=True,
                )
                self._warmup_thread.start()
                return self.warmup_status()
        self._run_warmup()
        return self.warmup_status()

    def _run_warmup(self):
        print("[WorldBook] 正在预热向量模型与 Qdrant…")
        try:
            vector = self.embedder.encode(["未名子 世界书 启动预热"])[0]
            with self._warmup_lock:
                self._warmup_model_ready = True
            self.vector_store.ensure_collection(len(vector))
        except Exception as exc:
            with self._warmup_lock:
                self._warmup_state = "degraded" if self._warmup_model_ready else "failed"
                self._warmup_error = f"{type(exc).__name__}: {exc}"
                self._warmup_finished_at = time.time()
                elapsed = self._warmup_finished_at - self._warmup_started_at
            print(
                f"[WorldBook] 预热未完全成功（{elapsed:.1f}s），"
                f"将降级并在检索时重试：{self._warmup_error}"
            )
            return
        with self._warmup_lock:
            self._warmup_state = "ready"
            self._warmup_error = ""
            self._warmup_finished_at = time.time()
            elapsed = self._warmup_finished_at - self._warmup_started_at
        print(f"[WorldBook] 向量检索预热完成（{elapsed:.1f}s），首条消息无需再加载模型")

    def wait_warmup(self, timeout: float | None = None) -> dict[str, Any]:
        thread = self._warmup_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        return self.warmup_status()

    def warmup_status(self) -> dict[str, Any]:
        with self._warmup_lock:
            finished = self._warmup_finished_at
            started = self._warmup_started_at
            return {
                "state": self._warmup_state,
                "model_ready": self._warmup_model_ready,
                "error": self._warmup_error,
                "elapsed": max(0.0, (finished or time.time()) - started) if started else 0.0,
            }

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    source_path TEXT NOT NULL,
                    source_format TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    secondary_json TEXT NOT NULL DEFAULT '[]',
                    regexes_json TEXT NOT NULL DEFAULT '[]',
                    constant INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 0,
                    insertion_order INTEGER NOT NULL DEFAULT 100,
                    probability REAL NOT NULL DEFAULT 100,
                    scope_characters_json TEXT NOT NULL DEFAULT '[]',
                    scope_regions_json TEXT NOT NULL DEFAULT '[]',
                    scope_groups_json TEXT NOT NULL DEFAULT '[]',
                    recursive INTEGER NOT NULL DEFAULT 0,
                    max_depth INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    entry_hash TEXT NOT NULL,
                    indexed_hash TEXT,
                    vector_point_id TEXT NOT NULL,
                    UNIQUE(book_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_entries_book ON entries(book_id, enabled);
                CREATE TABLE IF NOT EXISTS bindings (
                    group_id TEXT NOT NULL,
                    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    trpg_only INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(group_id, book_id)
                );
                """
            )

    def list_books(self, group_id: str | int | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if group_id is None:
                rows = db.execute(
                    "SELECT b.*, COUNT(e.id) AS entry_count FROM books b "
                    "LEFT JOIN entries e ON e.book_id=b.id GROUP BY b.id ORDER BY b.name"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT b.*, COUNT(e.id) AS entry_count, "
                    "COALESCE(MAX(CASE WHEN x.group_id=? THEN x.enabled ELSE 0 END),0) AS bound "
                    "FROM books b LEFT JOIN entries e ON e.book_id=b.id "
                    "LEFT JOIN bindings x ON x.book_id=b.id GROUP BY b.id ORDER BY b.name",
                    (str(group_id),),
                ).fetchall()
        return [dict(row) for row in rows]

    def _resolve_source(self, source: str) -> Path:
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.books_dir) / candidate
        candidate = candidate.resolve()
        root = Path(self.books_dir).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"只允许导入世界书目录内的文件：{self.books_dir}")
        if not candidate.exists():
            raise FileNotFoundError(str(candidate))
        return candidate

    def import_source(self, source: str, name: str | None = None) -> dict[str, Any]:
        path = self._resolve_source(source)
        entries, source_hash, source_format = self._parse_source(path)
        book_name = (name or path.stem or path.name).strip()
        if not book_name:
            raise ValueError("世界书名称不能为空")
        if not entries:
            raise ValueError("没有从该文件中解析出有效条目")

        with self._lock, self._connect() as db:
            existing = db.execute("SELECT * FROM books WHERE name=?", (book_name,)).fetchone()
            if existing:
                book_id = int(existing["id"])
                version = int(existing["version"]) + (existing["content_hash"] != source_hash)
                db.execute(
                    "UPDATE books SET source_path=?,source_format=?,content_hash=?,enabled=1,version=?,updated_at=? WHERE id=?",
                    (str(path), source_format, source_hash, version, _now(), book_id),
                )
            else:
                cursor = db.execute(
                    "INSERT INTO books(name,source_path,source_format,content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (book_name, str(path), source_format, source_hash, _now()),
                )
                book_id = int(cursor.lastrowid)
                version = 1

            old_rows = {
                row["source_id"]: row
                for row in db.execute("SELECT * FROM entries WHERE book_id=?", (book_id,)).fetchall()
            }
            incoming_ids = {entry.source_id for entry in entries}
            removed_points = [
                row["vector_point_id"] for key, row in old_rows.items() if key not in incoming_ids
            ]
            if removed_points:
                db.executemany(
                    "DELETE FROM entries WHERE book_id=? AND source_id=?",
                    [(book_id, key) for key in old_rows if key not in incoming_ids],
                )

            changed: list[tuple[int, WorldEntry, str]] = []
            unchanged = 0
            for entry in entries:
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"worldbook:{book_name}:{entry.source_id}"))
                old = old_rows.get(entry.source_id)
                values = self._entry_values(book_id, entry, point_id)
                if old:
                    db.execute(
                        """UPDATE entries SET title=?,content=?,keywords_json=?,secondary_json=?,regexes_json=?,
                        constant=?,enabled=?,priority=?,insertion_order=?,probability=?,scope_characters_json=?,
                        scope_regions_json=?,scope_groups_json=?,recursive=?,max_depth=?,metadata_json=?,entry_hash=?,
                        vector_point_id=? WHERE book_id=? AND source_id=?""",
                        values[2:] + (book_id, entry.source_id),
                    )
                    entry_id = int(old["id"])
                    if old["indexed_hash"] == entry.entry_hash:
                        unchanged += 1
                    else:
                        changed.append((entry_id, entry, point_id))
                else:
                    cursor = db.execute(
                        """INSERT INTO entries(book_id,source_id,title,content,keywords_json,secondary_json,
                        regexes_json,constant,enabled,priority,insertion_order,probability,scope_characters_json,
                        scope_regions_json,scope_groups_json,recursive,max_depth,metadata_json,entry_hash,vector_point_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        values,
                    )
                    changed.append((int(cursor.lastrowid), entry, point_id))

        warnings: list[str] = []
        indexed = 0
        if removed_points:
            try:
                self.vector_store.delete(removed_points)
            except Exception as exc:
                warnings.append(f"删除旧向量失败：{exc}")
        if changed:
            try:
                self.vector_store.ensure_collection(self.embedder.dimension)
                for start in range(0, len(changed), 64):
                    batch = changed[start : start + 64]
                    vectors = self.embedder.encode(
                        [self._embedding_text(entry) for _, entry, _ in batch]
                    )
                    points = []
                    for (entry_id, entry, point_id), vector in zip(batch, vectors):
                        points.append({
                            "id": point_id,
                            "vector": vector,
                            "payload": {
                                "book_id": book_id,
                                "entry_id": entry_id,
                                "source_id": entry.source_id,
                                "title": entry.title,
                                "priority": entry.priority,
                            },
                        })
                    self.vector_store.upsert(points)
                    with self._connect() as db:
                        db.executemany(
                            "UPDATE entries SET indexed_hash=? WHERE id=?",
                            [(entry.entry_hash, entry_id) for entry_id, entry, _ in batch],
                        )
                    indexed += len(batch)
            except Exception as exc:
                warnings.append(f"向量索引暂不可用，关键词规则仍可使用：{exc}")

        return {
            "name": book_name,
            "book_id": book_id,
            "entries": len(entries),
            "indexed": indexed,
            "unchanged": unchanged,
            "removed": len(removed_points),
            "version": version,
            "warnings": warnings,
        }

    @staticmethod
    def _entry_values(book_id: int, entry: WorldEntry, point_id: str) -> tuple[Any, ...]:
        return (
            book_id,
            entry.source_id,
            entry.title,
            entry.content,
            json.dumps(entry.keywords, ensure_ascii=False),
            json.dumps(entry.secondary_keywords, ensure_ascii=False),
            json.dumps(entry.regexes, ensure_ascii=False),
            int(entry.constant),
            int(entry.enabled),
            entry.priority,
            entry.insertion_order,
            entry.probability,
            json.dumps(entry.scope_characters, ensure_ascii=False),
            json.dumps(entry.scope_regions, ensure_ascii=False),
            json.dumps(entry.scope_groups, ensure_ascii=False),
            int(entry.recursive),
            entry.max_depth,
            json.dumps(entry.metadata, ensure_ascii=False),
            entry.entry_hash,
            point_id,
        )

    @staticmethod
    def _embedding_text(entry: WorldEntry) -> str:
        keys = " ".join(entry.keywords + entry.secondary_keywords)
        return f"{entry.title}\n关键词：{keys}\n{entry.content}".strip()

    def reload(self, name: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if name:
                rows = db.execute("SELECT name,source_path FROM books WHERE name=?", (name,)).fetchall()
            else:
                rows = db.execute("SELECT name,source_path FROM books ORDER BY name").fetchall()
        if not rows:
            raise ValueError(f"找不到世界书：{name}" if name else "目前没有可重载的世界书")
        return [self.import_source(row["source_path"], row["name"]) for row in rows]

    def bind(self, group_id: str | int, name: str, trpg_only: bool = False):
        with self._connect() as db:
            book = db.execute("SELECT id FROM books WHERE name=? AND enabled=1", (name,)).fetchone()
            if not book:
                raise ValueError(f"找不到已启用的世界书：{name}")
            db.execute(
                "INSERT INTO bindings(group_id,book_id,enabled,trpg_only) VALUES(?,?,1,?) "
                "ON CONFLICT(group_id,book_id) DO UPDATE SET enabled=1,trpg_only=excluded.trpg_only",
                (str(group_id), int(book["id"]), int(trpg_only)),
            )

    def bind_global(self, name: str):
        """Make a world book active for every group without per-group setup."""
        self.bind(GLOBAL_GROUP_ID, name, trpg_only=False)

    def disable_binding(self, group_id: str | int, name: str | None = None) -> int:
        with self._connect() as db:
            if name:
                cursor = db.execute(
                    "UPDATE bindings SET enabled=0 WHERE group_id=? AND book_id IN (SELECT id FROM books WHERE name=?)",
                    (str(group_id), name),
                )
            else:
                cursor = db.execute("UPDATE bindings SET enabled=0 WHERE group_id=?", (str(group_id),))
            return cursor.rowcount

    def status(self, group_id: str | int) -> dict[str, Any]:
        with self._connect() as db:
            books = db.execute(
                """SELECT b.id,b.name,b.version,b.source_format,x.trpg_only,COUNT(e.id) AS entry_count,
                SUM(CASE WHEN e.indexed_hash=e.entry_hash THEN 1 ELSE 0 END) AS indexed_count
                FROM bindings x JOIN books b ON b.id=x.book_id
                LEFT JOIN entries e ON e.book_id=b.id
                WHERE x.group_id IN (?,?) AND x.enabled=1 AND b.enabled=1
                GROUP BY b.id ORDER BY b.name""",
                (str(group_id), GLOBAL_GROUP_ID),
            ).fetchall()
        return {
            "enabled": self.enabled,
            "qdrant": self.vector_store.healthy() if self.enabled else False,
            "books": [dict(row) for row in books],
            "warmup": self.warmup_status(),
        }

    def retrieve(
        self,
        group_id: str | int,
        text: str,
        recent_context: str = "",
        trpg_active: bool = False,
        character: str = "",
        region: str = "",
    ) -> RetrievalResult:
        result = RetrievalResult()
        if not self.enabled or not text.strip():
            return result
        with self._connect() as db:
            book_rows = db.execute(
                """SELECT DISTINCT b.id,b.name FROM bindings x JOIN books b ON b.id=x.book_id
                WHERE x.group_id IN (?,?) AND x.enabled=1 AND b.enabled=1
                AND (x.trpg_only=0 OR ?=1)""",
                (str(group_id), GLOBAL_GROUP_ID, int(trpg_active)),
            ).fetchall()
            book_ids = [int(row["id"]) for row in book_rows]
            if not book_ids:
                return result
            placeholders = ",".join("?" for _ in book_ids)
            rows = db.execute(
                f"SELECT * FROM entries WHERE enabled=1 AND book_id IN ({placeholders}) "
                "ORDER BY priority DESC,insertion_order ASC,id ASC",
                book_ids,
            ).fetchall()

        scope_text = f"{text}\n{recent_context}"
        scoped = [
            row for row in rows
            if self._scope_matches(row, group_id, character, region, scope_text)
        ]
        hard_rows = [row for row in scoped if row["constant"]]
        result.hard_rules = _clip_to_token_budget(
            [self._format_entry(row, "永久规则") for row in hard_rows], self.rule_tokens
        )

        exact_rows = self._recursive_exact_matches(scoped, text, group_id)
        result.exact_entries = _clip_to_token_budget(
            [self._format_entry(row, "精确触发") for row in exact_rows if not row["constant"]],
            self.context_tokens,
        )

        used_ids = {int(row["id"]) for row in hard_rows + exact_rows}
        query_text = f"{text}\n{recent_context}".strip()
        semantic_rows: list[tuple[float, sqlite3.Row]] = []
        try:
            self.vector_store.ensure_collection(self.embedder.dimension)
            vector = self.embedder.encode([query_text])[0]
            hits = self.vector_store.query(vector, book_ids, self.top_k * 2)
            by_id = {int(row["id"]): row for row in scoped}
            for hit in hits:
                payload = hit.get("payload") or {}
                entry_id = _int(payload.get("entry_id"), -1)
                row = by_id.get(entry_id)
                if not row or entry_id in used_ids or row["constant"]:
                    continue
                score = _float(hit.get("score")) + min(0.25, int(row["priority"]) / 400.0)
                if self._keyword_matches(row, text):
                    score += 0.35
                semantic_rows.append((score, row))
            semantic_rows.sort(key=lambda item: (-item[0], -int(item[1]["priority"]), int(item[1]["insertion_order"])))
            semantic_rows = semantic_rows[: self.top_k]
        except Exception as exc:
            result.warnings.append(f"向量检索降级为关键词模式：{exc}")
            fallback = [
                row for row in scoped
                if int(row["id"]) not in used_ids and not row["constant"] and self._keyword_matches(row, text)
            ][: self.top_k]
            semantic_rows = [(0.0, row) for row in fallback]

        remaining_budget = max(
            200,
            self.context_tokens - sum(_estimate_tokens(item) for item in result.exact_entries),
        )
        result.semantic_entries = _clip_to_token_budget(
            [self._format_entry(row, f"语义检索 {score:.3f}") for score, row in semantic_rows],
            remaining_budget,
        )
        result.prompt = self._build_prompt(result, trpg_active)
        return result

    @staticmethod
    def _scope_matches(
        row: sqlite3.Row,
        group_id: str | int,
        character: str,
        region: str,
        scope_text: str,
    ) -> bool:
        groups = json.loads(row["scope_groups_json"] or "[]")
        characters = json.loads(row["scope_characters_json"] or "[]")
        regions = json.loads(row["scope_regions_json"] or "[]")
        if groups and str(group_id) not in {str(item) for item in groups}:
            return False
        lowered = scope_text.casefold()
        if characters:
            character_match = character and character.casefold() in {str(item).casefold() for item in characters}
            if not character_match and not any(str(item).casefold() in lowered for item in characters):
                return False
        if regions:
            region_match = region and region.casefold() in {str(item).casefold() for item in regions}
            if not region_match and not any(str(item).casefold() in lowered for item in regions):
                return False
        return True

    def _recursive_exact_matches(self, rows: list[sqlite3.Row], text: str, group_id: str | int) -> list[sqlite3.Row]:
        matched: list[sqlite3.Row] = []
        matched_ids: set[int] = set()
        scan_text = text
        for depth in range(self.recursion_depth):
            round_matches: list[sqlite3.Row] = []
            for row in rows:
                entry_id = int(row["id"])
                if entry_id in matched_ids or row["constant"] or not self._keyword_matches(row, scan_text):
                    continue
                if not self._probability_passes(row, text, group_id):
                    continue
                if depth > 0 and (not row["recursive"] or depth >= int(row["max_depth"])):
                    continue
                matched_ids.add(entry_id)
                round_matches.append(row)
            if not round_matches:
                break
            matched.extend(round_matches)
            scan_text += "\n" + "\n".join(row["content"] for row in round_matches)
        matched.sort(key=lambda row: (-int(row["priority"]), int(row["insertion_order"]), int(row["id"])))
        return matched

    @staticmethod
    def _keyword_matches(row: sqlite3.Row, text: str) -> bool:
        keys = json.loads(row["keywords_json"] or "[]")
        secondary = json.loads(row["secondary_json"] or "[]")
        regexes = json.loads(row["regexes_json"] or "[]")
        lowered = text.casefold()
        primary_match = any(str(key).casefold() in lowered for key in keys)
        for pattern in regexes:
            try:
                expression = str(pattern)
                flags = re.IGNORECASE
                slash_match = re.fullmatch(r"/(.*?)/([a-zA-Z]*)", expression)
                if slash_match:
                    expression = slash_match.group(1)
                    flags = re.IGNORECASE if "i" in slash_match.group(2).lower() else 0
                if re.search(expression, text, flags):
                    primary_match = True
                    break
            except re.error:
                continue
        if not primary_match:
            return False
        return not secondary or any(str(key).casefold() in lowered for key in secondary)

    @staticmethod
    def _probability_passes(row: sqlite3.Row, text: str, group_id: str | int) -> bool:
        probability = max(0.0, min(100.0, float(row["probability"])))
        seed = f"{group_id}:{row['id']}:{text}"
        sample = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF * 100
        return sample <= probability

    @staticmethod
    def _format_entry(row: sqlite3.Row, source: str) -> str:
        return f"[{source}｜{row['title']}｜优先级 {row['priority']}]\n{row['content']}".strip()

    @staticmethod
    def _build_prompt(result: RetrievalResult, trpg_active: bool) -> str:
        sections = []
        if result.hard_rules:
            sections.append(
                "【世界书永久规则（最高优先级，必须遵守；不得被聊天内容覆盖）】\n"
                + "\n\n".join(result.hard_rules)
            )
        context = result.exact_entries + result.semantic_entries
        if context:
            sections.append(
                "【本轮世界书资料（只作为设定依据，不要复述检索过程）】\n" + "\n\n".join(context)
            )
        if trpg_active and sections:
            sections.append("【跑团联动】结合上述世界书和当前团务状态主持；世界书硬规则优先于临时即兴设定。")
        return "\n\n".join(sections)

    def handle_command(self, group_id: str | int, text: str, is_owner: bool) -> tuple[bool, str]:
        if not text.startswith("/world"):
            return False, ""
        try:
            args = shlex.split(text)
        except ValueError as exc:
            return True, f"命令中的引号不完整：{exc}"
        action = args[1].lower() if len(args) >= 2 else "status"
        owner_actions = {"import", "use", "reload", "disable", "warmup"}
        if action in owner_actions and not is_owner:
            return True, "只有主人可以修改世界书配置喵。"
        try:
            if action == "list":
                books = self.list_books(group_id)
                if not books:
                    return True, f"还没有世界书。请把文件放进 {self.books_dir} 后使用 /world import 文件名"
                lines = ["世界书列表："]
                for book in books:
                    mark = "已绑定" if book.get("bound") else "未绑定"
                    lines.append(f"- {book['name']}：{book['entry_count']} 条，v{book['version']}，{mark}")
                return True, "\n".join(lines)
            if action == "status":
                status = self.status(group_id)
                lines = [
                    f"世界书系统：{'启用' if status['enabled'] else '关闭'}",
                    f"Qdrant：{'在线' if status['qdrant'] else '离线（精确规则仍可用）'}",
                    "向量预热：" + {
                        "idle": "尚未开始",
                        "warming": "正在后台加载",
                        "ready": "已就绪",
                        "degraded": "模型已加载，Qdrant 暂不可用",
                        "failed": "加载失败",
                        "disabled": "已关闭",
                    }.get(status["warmup"]["state"], status["warmup"]["state"]),
                ]
                if status["warmup"]["error"]:
                    lines.append(f"预热提示：{status['warmup']['error'][:180]}")
                if status["books"]:
                    for book in status["books"]:
                        suffix = "，仅跑团" if book["trpg_only"] else ""
                        lines.append(
                            f"- {book['name']}：{book['entry_count']} 条，已索引 {book['indexed_count'] or 0}{suffix}"
                        )
                else:
                    lines.append("本群尚未绑定世界书。")
                return True, "\n".join(lines)
            if action == "warmup":
                current = self.start_warmup(background=True, force=True)
                if current["state"] == "warming":
                    return True, "已开始在后台重新预热世界书向量模型喵。"
                return True, f"世界书预热状态：{current['state']}"
            if action == "import":
                if len(args) < 3:
                    return True, "用法：/world import 文件或目录 [世界书名称]"
                report = self.import_source(args[2], " ".join(args[3:]) or None)
                warning = "\n" + "\n".join(report["warnings"]) if report["warnings"] else ""
                return True, (
                    f"已导入「{report['name']}」：{report['entries']} 条，新增/更新索引 {report['indexed']} 条，"
                    f"未变化 {report['unchanged']} 条，版本 v{report['version']}。{warning}"
                )
            if action == "use":
                if len(args) < 3:
                    return True, "用法：/world use 世界书名称 [trpg]"
                trpg_only = args[-1].lower() == "trpg"
                name = " ".join(args[2:-1] if trpg_only else args[2:])
                self.bind(group_id, name, trpg_only=trpg_only)
                return True, f"本群已启用世界书「{name}」{'（仅跑团）' if trpg_only else ''}喵。"
            if action == "reload":
                reports = self.reload(" ".join(args[2:]) or None)
                indexed = sum(item["indexed"] for item in reports)
                warnings = [warning for item in reports for warning in item["warnings"]]
                response = f"已重载 {len(reports)} 本世界书，本次更新索引 {indexed} 条。"
                if warnings:
                    response += "\n" + "\n".join(warnings)
                return True, response
            if action == "disable":
                name = " ".join(args[2:]) or None
                count = self.disable_binding(group_id, name)
                return True, f"已停用 {count} 个世界书绑定喵。"
            return True, (
                "世界书命令：\n"
                "/world list\n/world status\n/world import 文件或目录 [名称]\n"
                "/world use 名称 [trpg]\n/world reload [名称]\n/world disable [名称]"
            )
        except Exception as exc:
            return True, f"世界书操作失败：{exc}"

    def _parse_source(self, path: Path) -> tuple[list[WorldEntry], str, str]:
        if path.is_dir():
            files = sorted(item for item in path.rglob("*") if item.suffix.lower() in SUPPORTED_SUFFIXES)
            entries: list[WorldEntry] = []
            hashes = []
            for file in files:
                parsed, digest, _ = self._parse_source(file)
                relative = file.relative_to(path).as_posix()
                for entry in parsed:
                    entry.source_id = f"{relative}:{entry.source_id}"
                entries.extend(parsed)
                hashes.append(f"{relative}:{digest}")
            return entries, _hash_text("\n".join(hashes)), "directory"

        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig", errors="replace")
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(text)
            return self._parse_structured(data, path.stem), digest, "json"
        if suffix in {".yaml", ".yml"}:
            if yaml is None:
                raise RuntimeError("缺少 PyYAML，请执行 pip install PyYAML")
            return self._parse_structured(yaml.safe_load(text), path.stem), digest, "yaml"
        if suffix in {".md", ".markdown"}:
            return self._parse_markdown(text, path.stem), digest, "markdown"
        return self._parse_text(text, path.stem), digest, "text"

    def _parse_structured(self, data: Any, fallback_title: str) -> list[WorldEntry]:
        if isinstance(data, dict) and isinstance(data.get("character_book"), dict):
            raw_entries = data["character_book"].get("entries", [])
        elif isinstance(data, dict) and "entries" in data:
            raw_entries = data["entries"]
        elif isinstance(data, dict) and isinstance(data.get("data"), dict) and "entries" in data["data"]:
            raw_entries = data["data"]["entries"]
        elif isinstance(data, list):
            raw_entries = data
        elif isinstance(data, dict) and any(key in data for key in ("content", "text", "rules")):
            raw_entries = [data]
        elif isinstance(data, dict):
            raw_entries = [dict(value, _source_key=key) if isinstance(value, dict) else {
                "_source_key": key, "content": value
            } for key, value in data.items()]
        else:
            raw_entries = [{"content": str(data or "")}]

        if isinstance(raw_entries, dict):
            items = []
            for key, value in raw_entries.items():
                if isinstance(value, dict):
                    value = dict(value)
                    value.setdefault("_source_key", key)
                else:
                    value = {"_source_key": key, "content": value}
                items.append(value)
            raw_entries = items

        entries = []
        for index, item in enumerate(raw_entries or []):
            if not isinstance(item, dict):
                item = {"content": str(item)}
            entry = self._normalize_entry(item, index, fallback_title)
            if entry.content.strip():
                entries.append(entry)
        return entries

    def _normalize_entry(self, item: dict[str, Any], index: int, fallback_title: str) -> WorldEntry:
        extensions = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}
        source_id = str(item.get("uid", item.get("id", item.get("_source_key", index))))
        title = str(item.get("comment") or item.get("name") or item.get("title") or f"{fallback_title}-{index + 1}")
        content = str(item.get("content", item.get("text", item.get("value", item.get("rules", "")))))
        keys = item.get("key", item.get("keys", item.get("keywords", item.get("primary_keywords", []))))
        secondary = item.get(
            "keysecondary", item.get("secondary_keys", item.get("secondary_keywords", []))
        )
        regexes = item.get("regex", item.get("regexes", item.get("patterns", [])))
        constant = _bool(item.get("constant", item.get("always_active", item.get("permanent", False))))
        enabled = not _bool(item.get("disable", item.get("disabled", False))) and _bool(item.get("enabled"), True)
        priority = _int(item.get("priority", extensions.get("priority", item.get("order", 0))))
        insertion_order = _int(item.get("insertion_order", item.get("order", 100)), 100)
        probability = _float(item.get("probability", 100), 100)
        if not _bool(item.get("useProbability", item.get("use_probability", True)), True):
            probability = 100
        scopes = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        recursive = _bool(item.get("recursive", item.get("allow_recursion", False)))
        max_depth = max(1, _int(item.get("max_depth", item.get("scanDepth", 1)), 1))
        return WorldEntry(
            source_id=source_id,
            title=title,
            content=content.strip(),
            keywords=_json_list(keys),
            secondary_keywords=_json_list(secondary),
            regexes=_json_list(regexes),
            constant=constant,
            enabled=enabled,
            priority=priority,
            insertion_order=insertion_order,
            probability=probability,
            scope_characters=_json_list(scopes.get("characters", item.get("characters", []))),
            scope_regions=_json_list(scopes.get("regions", item.get("regions", []))),
            scope_groups=_json_list(scopes.get("groups", item.get("groups", item.get("group_ids", [])))),
            recursive=recursive,
            max_depth=max_depth,
            metadata={key: value for key, value in item.items() if key not in {"content", "text", "value"}},
        )

    def _parse_markdown(self, text: str, fallback_title: str) -> list[WorldEntry]:
        matches = list(_MARKDOWN_HEADER_RE.finditer(text))
        if not matches:
            return self._parse_text(text, fallback_title)
        entries = []
        prefix = text[: matches[0].start()].strip()
        if prefix:
            entries.append(WorldEntry("preamble", fallback_title, prefix))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            content = text[match.end() : end].strip()
            if content:
                entries.append(WorldEntry(str(index), match.group(2).strip(), content))
        return entries

    @staticmethod
    def _parse_text(text: str, fallback_title: str) -> list[WorldEntry]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        entries = []
        buffer = ""
        index = 0
        for paragraph in paragraphs:
            if buffer and len(buffer) + len(paragraph) + 2 > 1400:
                entries.append(WorldEntry(str(index), f"{fallback_title}-{index + 1}", buffer))
                index += 1
                buffer = paragraph
            else:
                buffer = f"{buffer}\n\n{paragraph}".strip()
        if buffer:
            entries.append(WorldEntry(str(index), f"{fallback_title}-{index + 1}", buffer))
        return entries
