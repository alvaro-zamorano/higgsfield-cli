"""Key frames of a generated video, so Claude can review what came out (needs ffmpeg + Pillow).

Optional: without ffmpeg nothing breaks, hf_wait just returns no video preview. The Claude desktop
app starts MCP servers with a minimal PATH, so the usual Homebrew locations are searched too.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
FRACTIONS = (0.08, 0.36, 0.64, 0.92)


def find_ffmpeg() -> str | None:
    custom = os.getenv("HFCLI_FFMPEG", "").strip()
    if custom:
        return custom if Path(custom).is_file() and os.access(custom, os.X_OK) else None
    found = shutil.which("ffmpeg")
    if found:
        return found
    for folder in SEARCH_DIRS:
        path = Path(folder) / "ffmpeg"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def duration_s(ffmpeg: str, path: Path, timeout: float) -> float | None:
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path.resolve())], capture_output=True, text=True,
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = DURATION_RE.search(proc.stderr or "")
    if not match:
        return None
    h, m, s = match.groups()
    total = int(h) * 3600 + int(m) * 60 + float(s)
    return total if total > 0 else None


def _frame(ffmpeg: str, path: Path, at: float, width: int, timeout: float) -> bytes | None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(path.resolve()),
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8" else None


def contact_sheet(path: Path, *, width: int = 384, budget_s: float = 8.0) -> tuple[bytes, list[float]] | None:
    """2x2 JPEG mosaic of four moments of the video, each labelled with its second. None if unavailable."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not path.is_file():
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    deadline = time.monotonic() + budget_s
    total = duration_s(ffmpeg, path, timeout=min(4.0, budget_s))
    if not total:
        return None
    frames, times = [], []
    for fraction in FRACTIONS:
        left = deadline - time.monotonic()
        if left <= 0.5:
            break
        at = round(total * fraction, 1)
        data = _frame(ffmpeg, path, at, width, timeout=min(4.0, left))
        if data:
            try:
                frames.append(Image.open(io.BytesIO(data)).convert("RGB"))
                times.append(at)
            except Exception:  # corrupt frame or decompression-bomb guard: skip that frame
                continue
    if not frames:
        return None
    w = max(f.width for f in frames)
    h = max(f.height for f in frames)
    cols = 2 if len(frames) > 1 else 1
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for i, (frame, at) in enumerate(zip(frames, times)):
        x, y = (i % cols) * w, (i // cols) * h
        sheet.paste(frame, (x, y))
        label = f"{at:.1f}s"
        draw.rectangle((x + 4, y + 4, x + 12 + 7 * len(label), y + 20), fill=(0, 0, 0))
        draw.text((x + 8, y + 6), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), times
