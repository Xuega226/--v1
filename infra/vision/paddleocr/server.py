"""Small CPU-only PaddleOCR HTTP service for the QQ bot."""

from __future__ import annotations

from io import BytesIO
import json
import threading
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
import numpy as np
from PIL import Image, UnidentifiedImageError


app = FastAPI(title="Unnameko PaddleOCR", docs_url=None, redoc_url=None)
_engine = None
_engine_lock = threading.Lock()
_predict_lock = threading.Lock()
_MIN_SCORE = 0.45


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from paddleocr import PaddleOCR

                _engine = PaddleOCR(
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    device="cpu",
                )
    return _engine


def _as_plain(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    payload = getattr(value, "json", None)
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _find_recognition(value: Any) -> tuple[list[str], list[float]]:
    if isinstance(value, dict):
        texts = value.get("rec_texts")
        if isinstance(texts, list):
            scores = value.get("rec_scores") or []
            return [str(item) for item in texts], [float(item) for item in scores]
        for nested in value.values():
            found_texts, found_scores = _find_recognition(nested)
            if found_texts:
                return found_texts, found_scores
    elif isinstance(value, list):
        for nested in value:
            found_texts, found_scores = _find_recognition(nested)
            if found_texts:
                return found_texts, found_scores
    return [], []


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _engine is not None, "device": "cpu"}


@app.post("/ocr")
def recognize(file: UploadFile = File(...)):
    raw = file.file.read(12 * 1024 * 1024 + 1)
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="empty image or image too large")
    try:
        with Image.open(BytesIO(raw)) as source:
            image = np.asarray(source.convert("RGB"))
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=415, detail="invalid image") from exc

    try:
        with _predict_lock:
            results = _get_engine().predict(input=image)
        texts: list[str] = []
        scores: list[float] = []
        for result in results:
            page_texts, page_scores = _find_recognition(_as_plain(result))
            texts.extend(page_texts)
            scores.extend(page_scores)
        items = [
            {"text": text, "score": scores[index] if index < len(scores) else None}
            for index, text in enumerate(texts)
            if text.strip() and (index >= len(scores) or scores[index] >= _MIN_SCORE)
        ]
        return {
            "status": "ok",
            "text": "\n".join(item["text"] for item in items),
            "items": items,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR inference failed: {exc}") from exc
