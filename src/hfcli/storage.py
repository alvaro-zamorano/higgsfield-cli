"""Outputs: extract URLs from a result, download them, mirror to Cloudinary, keep a JSONL history."""
from __future__ import annotations

import json
import mimetypes
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from hfcli.config import redact

_history_lock = threading.Lock()
SKIP_KEYS = {"status_url", "cancel_url", "request_id", "status"}


def extract_outputs(result: dict) -> list[dict]:
    """Collect every media URL in a completed result (images[], video, audio, *_url extras)."""
    found: list[dict] = []
    seen: set[str] = set()

    def add(url: str, kind: str | None, content_type: str | None = None) -> None:
        if url not in seen:
            seen.add(url)
            found.append({"url": url, "kind": kind, "content_type": content_type})

    def walk(obj, key=None):
        if isinstance(obj, dict):
            if isinstance(obj.get("url"), str) and obj["url"].startswith("http"):
                add(obj["url"], key, obj.get("content_type"))
                return
            for k, v in obj.items():
                if k in SKIP_KEYS:
                    continue
                if isinstance(v, str) and k.endswith("_url") and v.startswith("http"):
                    add(v, k)
                else:
                    walk(v, k)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, key)

    walk(result)
    return found


MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v", ".png", ".jpg", ".jpeg", ".webp", ".gif",
                  ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".zip", ".fbx", ".ply", ".jsx", ".glb"}


def _extension(url: str, *content_types: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in MEDIA_SUFFIXES:
        return suffix
    for content_type in content_types:
        guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return suffix if suffix and len(suffix) <= 6 else ".bin"


def download(url: str, dest_dir: Path, basename: str, content_type: str | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest_dir / f"{basename}.part"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0)) as resp:
            resp.raise_for_status()
            target = dest_dir / f"{basename}{_extension(url, resp.headers.get('content-type'), content_type)}"
            with partial.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        if partial.stat().st_size == 0:
            raise IOError(f"descarga vacía: {url}")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def cloudinary_upload(source: str | Path, *, folder: str, public_id: str, tags: list[str], context: dict) -> str:
    try:
        import cloudinary
        import cloudinary.uploader
    except ImportError as exc:
        raise RuntimeError("Falta el paquete cloudinary: pip install -e '.[cloudinary]'") from exc
    cloudinary.config(secure=True)  # reads CLOUDINARY_URL from the environment
    response = cloudinary.uploader.upload(
        str(source), resource_type="auto", folder=folder, public_id=public_id,
        tags=tags, context=context, overwrite=False,
    )
    return response["secure_url"]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_history(history_file: Path, record: dict) -> None:
    """Best effort: a history write failure is reported but never breaks a paid job."""
    line = redact(json.dumps({"ts": now_iso(), **record}, ensure_ascii=False)) + "\n"
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with _history_lock, history_file.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        try:
            print(f"AVISO: no se pudo escribir el historial ({exc}): {line.strip()}", file=sys.stderr)
        except OSError:
            pass


def read_history(history_file: Path) -> list[dict]:
    """Latest record per request_id, oldest first."""
    if not history_file.exists():
        return []
    latest: dict[str, dict] = {}
    for raw in history_file.read_text("utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        key = rec.get("request_id") or f"_{len(latest)}"
        previous = latest.pop(key, {})
        latest[key] = {**previous, **rec}
    return list(latest.values())
