"""Locate the tool's home, load .env.local and report credential state without revealing values."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class UsageError(Exception):
    """Bad arguments or configuration: exit code 2, nothing billable happened."""


def find_home() -> Path:
    """Config (.env.local) and outputs live outside the code, in the same place on every machine."""
    env = os.getenv("HFCLI_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / ".higgsfield-cli"


@dataclass
class Settings:
    home: Path
    output_dir: Path
    history_file: Path
    cloudinary_folder: str

    @property
    def cloudinary_configured(self) -> bool:
        return bool(os.getenv("CLOUDINARY_URL"))


def load_settings() -> Settings:
    home = find_home()
    load_dotenv(home / ".env.local", override=False)  # real env vars win over the file
    output = Path(os.getenv("HFCLI_OUTPUT") or home / "output").expanduser()
    return Settings(
        home=home,
        output_dir=output,
        history_file=output / "history.jsonl",
        cloudinary_folder=os.getenv("HFCLI_CLOUDINARY_FOLDER", "higgsfield"),
    )


def credential_state() -> tuple[bool, str]:
    """Return (usable, description). Never includes the secret itself."""
    key = os.getenv("HF_KEY", "").strip()
    if key:
        key_id, sep, secret = key.partition(":")
        if sep and key_id and secret:
            return True, "HF_KEY definida con formato key-id:key-secret"
        return False, "HF_KEY definida pero sin formato key-id:key-secret"
    if os.getenv("HF_API_KEY") and os.getenv("HF_API_SECRET"):
        return True, "HF_API_KEY y HF_API_SECRET definidas"
    return False, "HF_KEY no definida"


def _secrets() -> list[str]:
    values = []
    for name in ("HF_KEY", "HF_API_KEY", "HF_API_SECRET", "CLOUDINARY_URL"):
        raw = os.getenv(name) or ""
        values += [raw, raw.strip()]
        if name == "HF_KEY" and ":" in raw:
            values.append(raw.split(":", 1)[1].strip())
        if name == "CLOUDINARY_URL" and "@" in raw:
            values.append(raw.split("@", 1)[0].rsplit(":", 1)[-1])
    return sorted({v for v in values if len(v) >= 6}, key=len, reverse=True)


def redact(text: str) -> str:
    """Remove any credential value from text before it is printed or stored."""
    for secret in _secrets():
        text = text.replace(secret, "***")
    return text


def auth_header() -> str:
    usable, detail = credential_state()
    if not usable:
        raise UsageError(f"Credenciales no válidas: {detail}. Revisa .env.local (valor no mostrado).")
    key = os.getenv("HF_KEY", "").strip() or f"{os.getenv('HF_API_KEY')}:{os.getenv('HF_API_SECRET')}"
    return f"Key {key}"
