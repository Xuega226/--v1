"""Serialize GPU-heavy work initiated by the QQ bot."""

from contextlib import contextmanager
import csv
import io
import subprocess
import threading
import time


_GPU_LOCK = threading.Lock()


def get_free_vram_mb() -> int | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        row = next(csv.reader(io.StringIO(completed.stdout)))
        return int(row[0].strip())
    except Exception:
        return None


@contextmanager
def gpu_task(name: str, min_free_mb: int = 0, wait_seconds: int = 180):
    acquired = _GPU_LOCK.acquire(timeout=max(1, wait_seconds))
    if not acquired:
        raise TimeoutError(f"等待 GPU 任务队列超时：{name}")
    try:
        deadline = time.time() + max(1, wait_seconds)
        while min_free_mb > 0:
            free = get_free_vram_mb()
            if free is None or free >= min_free_mb:
                break
            if time.time() >= deadline:
                raise RuntimeError(f"可用显存不足：需要 {min_free_mb} MB，目前约 {free} MB")
            time.sleep(1)
        yield
    finally:
        _GPU_LOCK.release()
