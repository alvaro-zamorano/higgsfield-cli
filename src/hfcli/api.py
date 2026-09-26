"""HTTP layer. Built on the official SDK's transport, with retry rules chosen to avoid paying twice:
the generation POST is only retried on 429 (rejected, never queued); status GETs retry normally.
Uploads go through the SDK client unchanged.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import httpx
import higgsfield_client
from higgsfield_client.http.retry import ExponentialBackoffRetry, NoRetry, create_default_retry_strategy
from higgsfield_client.http.transport import HttpTransport

from hfcli.config import UsageError, auth_header

API_BASE = "https://api.higgsfield.ai"
PENDING = {"queued", "in_progress", "pending", "processing", "running", "starting"}
TERMINAL = {"completed", "failed", "nsfw", "canceled", "cancelled"}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_lock = threading.Lock()
_http_client: httpx.Client | None = None
_sdk_client: higgsfield_client.SyncClient | None = None


class PollTimeout(Exception):
    pass


class LostTrack(Exception):
    """Too many consecutive errors while polling; the request may still finish server-side."""


class Interrupted(Exception):
    """A stop was requested (batch Ctrl+C) while waiting."""


def _key() -> str:
    return auth_header().removeprefix("Key ")


def http() -> httpx.Client:
    global _http_client
    with _lock:
        if _http_client is None:
            _http_client = httpx.Client(base_url=API_BASE, timeout=httpx.Timeout(120.0, connect=15.0),
                                        headers={"Authorization": f"Key {_key()}", "User-Agent": "hfcli",
                                                 "Content-Type": "application/json"})
        return _http_client


def sdk() -> higgsfield_client.SyncClient:
    global _sdk_client
    with _lock:
        if _sdk_client is None:
            _sdk_client = higgsfield_client.SyncClient(timeout=120.0, api_key=_key())  # key already stripped
        return _sdk_client


def is_higgsfield_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return urlparse(url).scheme == "https" and (host == "higgsfield.ai" or host.endswith(".higgsfield.ai"))


def status_url_for(request_id: str) -> str:
    if not REQUEST_ID_RE.match(request_id):
        raise UsageError(f"request_id no válido: {request_id!r}")
    return f"{API_BASE}/requests/{request_id}/status"


def submit(endpoint: str, params: dict) -> dict:
    """POST the generation. Returns the API's JSON (request_id, status_url, ...)."""
    transport = HttpTransport(http(), ExponentialBackoffRetry(max_retries=4, initial_delay=2.0,
                                                              retryable_status_codes={429}))
    data = transport.request("POST", "/" + endpoint.lstrip("/"), json=params).json()
    if not isinstance(data, dict) or not data.get("request_id"):
        raise ValueError(f"respuesta de envío sin request_id: {str(data)[:200]}")
    return data


def get_json(url: str) -> dict:
    data = HttpTransport(http(), create_default_retry_strategy()).request("GET", url).json()
    if not isinstance(data, dict):
        raise ValueError("respuesta de estado no es un objeto JSON")
    return data


def status_of(request_id: str) -> str:
    return str(get_json(status_url_for(request_id)).get("status", "?"))


def wait(status_url: str, *, timeout_s: float, delay_s: float, on_status: Callable[[str], None] | None = None,
         stop: threading.Event | None = None, max_errors: int = 10) -> dict:
    """Poll until a terminal status and return that final JSON (it already contains the outputs).
    Unknown statuses are treated as still running, so a paid job is never abandoned early."""
    start, errors, last = time.monotonic(), 0, None
    while True:
        if stop is not None and stop.is_set():
            raise Interrupted(f"interrumpido (último estado: {last or 'desconocido'})")
        try:
            data = get_json(status_url)
            name = str(data.get("status", "")).lower()
            errors = 0
        except (httpx.HTTPError, higgsfield_client.HiggsfieldClientError, ValueError) as exc:
            errors += 1
            if errors >= max_errors:
                raise LostTrack(f"{errors} errores seguidos consultando el estado ({type(exc).__name__})") from exc
            time.sleep(delay_s)
            continue
        if name != last and on_status:
            on_status(name if name in PENDING | TERMINAL else f"{name} (desconocido, sigo esperando)")
        last = name
        if name in TERMINAL:
            data["status"] = "canceled" if name == "cancelled" else name
            return data
        if time.monotonic() - start > timeout_s:
            raise PollTimeout(f"Sin terminar tras {timeout_s / 60:.0f} min (último estado: {name})")
        time.sleep(delay_s)


def cancel(request_id: str) -> None:
    status_url_for(request_id)  # validates the id
    HttpTransport(http(), NoRetry()).request("POST", f"/requests/{request_id}/cancel")


def upload_file(path) -> str:
    return sdk().upload_file(path)


def raw_get(path: str, params: dict | None = None) -> httpx.Response:
    """Authenticated GET without raising on HTTP errors (presets, doctor probe)."""
    return http().get(path, params=params)
