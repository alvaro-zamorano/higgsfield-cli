"""Remote connector: the same hf_* tools over HTTPS, so any Claude chat (web, mobile) can use them.

The server listens on 127.0.0.1 only; a tunnel (Tailscale Funnel) publishes it. claude.ai custom
connectors speak OAuth, so this module is a tiny single-user OAuth 2.1 authorization server:
- Claude registers itself (Dynamic Client Registration). Only Claude's callback URLs and loopback
  addresses (Claude Code) are accepted as redirect URIs, so codes can't be sent anywhere else.
- Authorising means typing a passphrase on a page served by this machine. The user sets it locally
  with `hf remote passphrase`; only a PBKDF2 hash is stored. 3 wrong tries use up a login link, and 5 in
  15 min lock the page for 15 min. Unauthenticated POST bodies are capped at 16 KB.
- PKCE S256 is checked by the MCP SDK. Tokens are random, stored as SHA-256 hashes in remote/state.json
  (0600); access tokens last 1 h, refresh tokens 30 days and rotate on every use (a rotated one that comes
  back later drops every token of that client).
- `hf remote revoke` (or changing the passphrase) disconnects every chat at once.
- It refuses to start without a daily job cap (HFCLI_MCP_MAX_JOBS_PER_DAY). Remote calls only reach
  catalogued models, and @files only files this tool generated (mcp_server._check_remote_refs). The money
  guards of mcp_server (dry run by default, ledger, duplicates) still apply.
The Higgsfield key stays in .env.local; nothing here reads, logs or returns it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hfcli.config import Settings, UsageError, credential_state

SCOPE = "hf"
DEFAULT_PORT = 8765
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
CODE_TTL_S = 300
LOGIN_TTL_S = 600
MAX_PENDING = 20
MAX_CLIENTS = 50
MAX_CLIENT_NAME = 100
TRIES_PER_LOGIN = 3
BODY_LIMIT = 16 * 1024          # unauthenticated POST routes (OAuth endpoints and /hf/login)
LIMITED_PATHS = {"/register", "/authorize", "/token", "/revoke", "/hf/login"}
REFRESH_GRACE_S = 60             # a second refresh with the same token right after rotation: refuse, don't punish
UNUSED_CLIENT_TTL_S = 86400
USED_REFRESH_TTL_S = REFRESH_TTL_S
FAILS_BEFORE_LOCK = 5
LOCK_WINDOW_S = 900
MIN_PASSPHRASE = 12
PBKDF2_ITERATIONS = 600_000
CLAUDE_CALLBACKS = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
LAUNCHD_LABEL = "ai.higgsfield-cli.remote"


# ---------- files ----------

def remote_dir(settings: Settings) -> Path:
    return settings.home / "remote"


def _write_private(path: Path, data: dict) -> None:
    """Atomic write readable only by the user (0600), whatever the umask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- passphrase ----------

def passphrase_file(settings: Settings) -> Path:
    return remote_dir(settings) / "passphrase.json"


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)


def set_passphrase(settings: Settings, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise UsageError(f"La frase necesita al menos {MIN_PASSPHRASE} caracteres.")
    salt = secrets.token_bytes(16)
    _write_private(passphrase_file(settings), {
        "algorithm": "pbkdf2_sha256", "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_derive(passphrase, salt, PBKDF2_ITERATIONS)).decode(),
    })
    revoke_all(settings)  # a new passphrase disconnects every chat


def passphrase_set(settings: Settings) -> bool:
    try:
        data = json.loads(passphrase_file(settings).read_text("utf-8"))
        return bool(data.get("hash") and data.get("salt") and int(data.get("iterations", 0)) > 0)
    except (OSError, ValueError, TypeError):
        return False


def check_passphrase(settings: Settings, candidate: str) -> bool:
    try:
        data = json.loads(passphrase_file(settings).read_text("utf-8"))
        salt = base64.b64decode(data["salt"])
        expected = base64.b64decode(data["hash"])
        iterations = int(data["iterations"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(_derive(candidate, salt, iterations), expected)


# ---------- configuration ----------

@dataclass
class RemoteConfig:
    base_url: str      # https://mac.tailnet.ts.net (no trailing slash)
    host: str          # mac.tailnet.ts.net
    port: int          # local port the tunnel points at
    cap: int           # HFCLI_MCP_MAX_JOBS_PER_DAY

    @property
    def mcp_url(self) -> str:
        return self.base_url + "/mcp"


def check_config(settings: Settings, port: int | None = None) -> tuple[RemoteConfig | None, list[str]]:
    """Everything remote mode needs. Returns (config or None, problems). Never includes secret values."""
    problems: list[str] = []
    raw_url = os.getenv("HF_REMOTE_URL", "").strip().rstrip("/")
    parts = urlsplit(raw_url) if raw_url else None
    host = (parts.hostname or "") if parts else ""
    if not raw_url:
        problems.append("Falta HF_REMOTE_URL en .env.local (la URL pública del túnel, p. ej. "
                        "https://mi-mac.tu-tailnet.ts.net).")
    elif parts.scheme not in ("https", "http") or not host or parts.path or parts.query or parts.fragment \
            or parts.username or parts.password:
        problems.append("HF_REMOTE_URL debe ser solo esquema y dominio, sin ruta: https://mi-mac.tu-tailnet.ts.net")
    elif parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        problems.append("HF_REMOTE_URL tiene que ser https (http solo vale para pruebas en 127.0.0.1).")
    if port is None:
        raw_port = os.getenv("HF_REMOTE_PORT", "").strip()
        try:
            port = int(raw_port) if raw_port else DEFAULT_PORT
        except ValueError:
            problems.append("HF_REMOTE_PORT no es un número.")
            port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        problems.append("HF_REMOTE_PORT fuera de rango.")
    raw_cap = os.getenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "").strip()
    cap = 0
    try:
        cap = int(raw_cap) if raw_cap else 0
    except ValueError:
        pass
    if cap < 1:
        problems.append("En remoto el tope diario es obligatorio: pon HFCLI_MCP_MAX_JOBS_PER_DAY=<n> (1 o más) "
                        "en .env.local.")
    if not passphrase_set(settings):
        problems.append("Falta la frase de acceso: ejecuta `hf remote passphrase`.")
    usable, detail = credential_state()
    if not usable:
        problems.append(f"Credenciales de Higgsfield: {detail}.")
    output = settings.output_dir.expanduser().resolve()
    if output == Path.home().resolve() or settings.home.expanduser().resolve().is_relative_to(output):
        problems.append(f"La carpeta de salida ({output}) contiene la configuración o es tu carpeta personal: en "
                        "remoto solo se suben archivos de ahí, así que usa una carpeta propia (HFCLI_OUTPUT).")
    if problems:
        return None, problems
    return RemoteConfig(base_url=raw_url, host=host, port=port, cap=cap), []


# ---------- token store ----------

KINDS = ("clients", "access", "refresh", "used")


class Store:
    """Clients and hashed tokens in remote/state.json. Re-read when the file changes, so `hf remote revoke`
    run from another process takes effect immediately."""

    def __init__(self, settings: Settings):
        self.path = remote_dir(settings) / "state.json"
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._data: dict = {}

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime, self._data = None, {}
            return self._blank()
        if mtime != self._mtime:
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                self._data = {}
            self._mtime = mtime
        for key in KINDS:
            if not isinstance(self._data.get(key), dict):
                self._data[key] = {}
        return self._data

    @staticmethod
    def _blank() -> dict:
        return {kind: {} for kind in KINDS}

    def _save(self, data: dict) -> None:
        now = time.time()
        for kind in ("access", "refresh", "used"):
            data[kind] = {h: r for h, r in data[kind].items() if r.get("expires_at", 0) > now}
        _write_private(self.path, data)
        self._data, self._mtime = data, self.path.stat().st_mtime_ns

    @contextmanager
    def _exclusive(self):
        """In-process lock plus a file lock, so `hf remote revoke` and the server never overwrite each other."""
        with self._lock:
            try:
                import fcntl
            except ImportError:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path.with_name("state.lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def read(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._load()))

    def get(self, kind: str, key: str) -> dict | None:
        """Copy of one record (cheaper than read() on every request)."""
        with self._lock:
            rec = self._load()[kind].get(key)
            return dict(rec) if isinstance(rec, dict) else None

    def update(self, change) -> Any:
        """Apply change(data) and save; if change raises, nothing is kept. The exception is re-raised outside
        the lock: the SDK's errors are frozen dataclasses, which contextlib can't annotate on the way out."""
        error: Exception | None = None
        with self._exclusive():
            data = json.loads(json.dumps(self._load()))
            try:
                result = change(data)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                error = exc
            else:
                self._save(data)
        if error is not None:
            raise error
        return result


def revoke_all(settings: Settings) -> int:
    """Forget every client and token. Returns how many clients were registered."""
    def clear(data: dict) -> int:
        count = len(data["clients"])
        data.update({kind: {} for kind in KINDS})
        return count
    return Store(settings).update(clear)


def connected_clients(settings: Settings) -> list[str]:
    data = Store(settings).read()
    now = time.time()
    live = {r.get("client_id") for r in data["refresh"].values() if r.get("expires_at", 0) > now}
    return [str(c.get("client_name") or cid) for cid, c in data["clients"].items() if cid in live]


CLIENT_FIELDS = ("client_id", "client_secret", "client_id_issued_at", "client_secret_expires_at", "redirect_uris",
                 "token_endpoint_auth_method", "grant_types", "response_types", "scope", "client_name")


def _client_record(client_info) -> dict:
    """Only what the OAuth flow needs: attacker-supplied extras (jwks, contacts, URIs...) are not stored."""
    raw = client_info.model_dump(mode="json", exclude_none=True)
    rec = {k: raw[k] for k in CLIENT_FIELDS if k in raw}
    if "client_name" in rec:
        rec["client_name"] = " ".join(str(rec["client_name"]).split())[:MAX_CLIENT_NAME]
    return rec


def redirect_allowed(uri: str) -> bool:
    if uri in CLAUDE_CALLBACKS:
        return True
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS and not parts.username


# ---------- OAuth provider (mcp SDK protocol) ----------

def make_provider(settings: Settings, config: RemoteConfig):
    from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                          RegistrationError, TokenError)
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    class Provider:
        def __init__(self):
            self.settings, self.config = settings, config
            self.store = Store(settings)
            self.pending: dict[str, dict] = {}
            self.codes: dict[str, AuthorizationCode] = {}
            self.failures: deque[float] = deque()
            self._lock = threading.Lock()

        # clients
        async def get_client(self, client_id: str):
            raw = self.store.get("clients", client_id)
            return OAuthClientInformationFull.model_validate(raw) if raw else None

        async def register_client(self, client_info) -> None:
            bad = [str(u) for u in (client_info.redirect_uris or []) if not redirect_allowed(str(u))]
            if bad or not client_info.redirect_uris:
                raise RegistrationError("invalid_redirect_uri",
                                        "Este servidor solo acepta los callbacks de Claude o direcciones locales.")

            def add(data: dict) -> None:
                now = time.time()
                live = {r.get("client_id") for r in data["refresh"].values()}
                for cid, raw in list(data["clients"].items()):  # drop registrations never used
                    if cid not in live and now - float(raw.get("client_id_issued_at") or 0) > UNUSED_CLIENT_TTL_S:
                        del data["clients"][cid]
                if len(data["clients"]) >= MAX_CLIENTS:
                    unused = sorted((float(raw.get("client_id_issued_at") or 0), cid)
                                    for cid, raw in data["clients"].items() if cid not in live)
                    if not unused:
                        raise RegistrationError("invalid_client_metadata",
                                                "Demasiados clientes conectados: ejecuta `hf remote revoke`.")
                    del data["clients"][unused[0][1]]
                data["clients"][client_info.client_id] = _client_record(client_info)
            self.store.update(add)

        # authorization: send the browser to the passphrase page
        async def authorize(self, client, params: AuthorizationParams) -> str:
            now = time.time()
            with self._lock:
                for key in [k for k, v in self.pending.items() if v["expires_at"] < now]:
                    del self.pending[key]
                if len(self.pending) >= MAX_PENDING:
                    oldest = min(self.pending, key=lambda k: self.pending[k]["expires_at"])
                    del self.pending[oldest]
                req = secrets.token_urlsafe(24)
                self.pending[req] = {"client_id": client.client_id,
                                     "client_name": " ".join((client.client_name or "").split())[:MAX_CLIENT_NAME],
                                     "params": params, "expires_at": now + LOGIN_TTL_S, "tries": 0}
            return f"{self.config.base_url}/hf/login?req={req}"

        def pending_request(self, req: str) -> dict | None:
            with self._lock:
                item = self.pending.get(req)
                return item if item and item["expires_at"] >= time.time() else None

        def locked_for(self) -> float:
            now = time.time()
            with self._lock:
                while self.failures and now - self.failures[0] > LOCK_WINDOW_S:
                    self.failures.popleft()
                if len(self.failures) >= FAILS_BEFORE_LOCK:
                    return self.failures[0] + LOCK_WINDOW_S - now
                return 0.0

        def record_failure(self, req: str) -> bool:
            """Count a wrong passphrase. Returns False when this login link is used up."""
            with self._lock:
                self.failures.append(time.time())
                item = self.pending.get(req)
                if item is None:
                    return False
                item["tries"] += 1
                if item["tries"] >= TRIES_PER_LOGIN:
                    del self.pending[req]
                    return False
                return True

        def approve(self, req: str) -> str | None:
            """Consume the pending request and return the redirect with a fresh code."""
            from mcp.server.auth.provider import construct_redirect_uri
            with self._lock:
                item = self.pending.pop(req, None)
                if not item or item["expires_at"] < time.time():
                    return None
                params = item["params"]
                code = secrets.token_urlsafe(32)
                fields = dict(code=code, scopes=params.scopes or [SCOPE], expires_at=time.time() + CODE_TTL_S,
                              client_id=item["client_id"], code_challenge=params.code_challenge,
                              redirect_uri=params.redirect_uri,
                              redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly)
                if "resource" in AuthorizationCode.model_fields:
                    fields["resource"] = getattr(params, "resource", None)
                self.codes[code] = AuthorizationCode(**fields)
                self.failures.clear()
            return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

        def deny(self, req: str) -> str | None:
            from mcp.server.auth.provider import construct_redirect_uri
            with self._lock:
                item = self.pending.pop(req, None)
            if not item:
                return None
            params = item["params"]
            return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

        # codes and tokens
        async def load_authorization_code(self, client, authorization_code: str):
            with self._lock:
                code = self.codes.get(authorization_code)
                if code and code.expires_at < time.time():
                    del self.codes[authorization_code]
                    return None
                return code

        def _issue(self, client_id: str, scopes: list[str], resource: str | None, data: dict) -> OAuthToken:
            if client_id not in data["clients"]:  # revoked while the exchange was in flight
                raise TokenError("invalid_grant", "client no longer registered")
            access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = int(time.time())
            data["refresh"][_sha(refresh)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                              "expires_at": now + REFRESH_TTL_S}
            data["access"][_sha(access)] = {"client_id": client_id, "scopes": scopes, "resource": resource,
                                            "expires_at": now + ACCESS_TTL_S, "refresh": _sha(refresh)}
            return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S,
                              refresh_token=refresh, scope=" ".join(scopes))

        async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
            with self._lock:
                if self.codes.pop(authorization_code.code, None) is None:  # single use
                    raise TokenError("invalid_grant", "authorization code already used")
            resource = getattr(authorization_code, "resource", None)
            return self.store.update(lambda d: self._issue(client.client_id, authorization_code.scopes,
                                                           resource, d))

        async def load_refresh_token(self, client, refresh_token: str):
            h = _sha(refresh_token)
            used = self.store.get("used", h)
            if used and time.time() - float(used.get("rotated_at") or 0) < REFRESH_GRACE_S:
                return None  # likely a retry or two parallel refreshes: refuse without logging anyone out
            if used:  # a rotated token came back later: assume it was stolen
                def drop_client_tokens(data: dict) -> None:
                    owner = (data["used"].get(h) or {}).get("client_id")
                    for kind in ("access", "refresh"):
                        data[kind] = {k: r for k, r in data[kind].items() if r.get("client_id") != owner}
                self.store.update(drop_client_tokens)
                print("hf remote: token de renovación reutilizado; sesiones de ese cliente cerradas",
                      file=sys.stderr, flush=True)
                return None
            rec = self.store.get("refresh", h)
            if not rec or rec.get("client_id") != client.client_id or rec.get("expires_at", 0) < time.time():
                return None
            fields = dict(token=refresh_token, client_id=rec["client_id"], scopes=rec.get("scopes") or [SCOPE],
                          expires_at=int(rec["expires_at"]))
            if "resource" in RefreshToken.model_fields:
                fields["resource"] = rec.get("resource")
            return RefreshToken(**fields)

        async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
            old = _sha(refresh_token.token)

            def rotate(data: dict) -> OAuthToken:
                rec = data["refresh"].pop(old, None)
                if rec is None:  # used twice (or revoked meanwhile)
                    raise TokenError("invalid_grant", "refresh token no longer valid")
                data["used"][old] = {"client_id": rec.get("client_id"), "rotated_at": time.time(),
                                     "expires_at": time.time() + USED_REFRESH_TTL_S}
                data["access"] = {h: r for h, r in data["access"].items() if r.get("refresh") != old}
                return self._issue(client.client_id, scopes or rec.get("scopes") or [SCOPE],
                                   rec.get("resource"), data)
            return self.store.update(rotate)

        async def load_access_token(self, token: str):
            rec = self.store.get("access", _sha(token))
            if not rec or rec.get("expires_at", 0) < time.time() or not self.store.get("clients", rec["client_id"]):
                return None
            fields = dict(token=token, client_id=rec["client_id"], scopes=rec.get("scopes") or [SCOPE],
                          expires_at=int(rec["expires_at"]))
            if "resource" in AccessToken.model_fields:
                fields["resource"] = rec.get("resource")
            return AccessToken(**fields)

        async def revoke_token(self, token) -> None:
            h = _sha(token.token)

            def drop(data: dict) -> None:
                pair = (data["access"].pop(h, None) or {}).get("refresh") or h
                data["refresh"].pop(pair, None)
                data["access"] = {k: r for k, r in data["access"].items() if r.get("refresh") != pair}
            self.store.update(drop)

    return Provider()


# ---------- passphrase page ----------

PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar hf</title>
<style>
:root{{--bg:#0A0A0A;--fg:#F5F2EB;--muted:#9A9A9A;--signal:#34D399}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,
system-ui,sans-serif;padding:32px 16px}}main{{max-width:440px}}h1{{font-size:22px;margin:0 0 12px}}
p{{margin:0 0 12px}}.muted{{color:var(--muted);font-size:14px}}.err{{color:#F87171}}
input{{width:100%;font-size:16px;padding:12px;border:1px solid #444;border-radius:6px;background:#141414;
color:var(--fg);margin:8px 0 16px}}button{{font-size:16px;padding:12px 18px;border-radius:6px;border:0;
cursor:pointer;margin-right:8px}}.ok{{background:var(--signal);color:#0A0A0A;font-weight:600}}
.no{{background:#222;color:var(--fg)}}code{{font-size:14px}}
</style></head><body><main>
<h1>Conectar Claude con hf</h1>
{body}
</main></body></html>"""


def _page(body: str, status: int = 200):
    from starlette.responses import HTMLResponse
    return HTMLResponse(PAGE.format(body=body), status_code=status, headers={
        "Cache-Control": "no-store", "Pragma": "no-cache", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    })


def _form(item: dict, req: str, error: str = "") -> str:
    params = item["params"]
    target = urlsplit(str(params.redirect_uri)).netloc
    name = item.get("client_name") or "cliente sin nombre"
    esc = html.escape
    return (
        f"<p>Un cliente (<b>{esc(name)}</b>) pide usar el generador de este ordenador. "
        f"Si lo apruebas, la autorización vuelve a <code>{esc(target)}</code>.</p>"
        "<p class=\"muted\">Escribe la frase solo si acabas de pulsar Conectar en tu propia cuenta de Claude. "
        "Si no sabes de dónde viene esta página, cancela.</p>"
        + (f"<p class=\"err\">{esc(error)}</p>" if error else "") +
        "<form method=\"post\" action=\"/hf/login\">"
        f"<input type=\"hidden\" name=\"req\" value=\"{esc(req)}\">"
        "<label for=\"p\">Frase de acceso</label>"
        "<input id=\"p\" name=\"passphrase\" type=\"password\" autocomplete=\"current-password\" required autofocus>"
        "<button class=\"ok\" name=\"action\" value=\"allow\" type=\"submit\">Conectar</button>"
        "<button class=\"no\" name=\"action\" value=\"deny\" type=\"submit\" formnovalidate>Cancelar</button>"
        "</form>")


class BodyLimit:
    """ASGI middleware: unauthenticated POST routes get at most BODY_LIMIT bytes (older mcp SDKs set no limit).
    The body is read here, bounded, and replayed to the app."""

    def __init__(self, app, limit: int = BODY_LIMIT, paths: set[str] = LIMITED_PATHS):
        self.app, self.limit, self.paths = app, limit, paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in self.paths or scope.get("method") in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            return await _too_large(send)
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.limit:
                return await _too_large(send)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


async def _too_large(send) -> None:
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": b"Request too large"})


def add_login_routes(srv, provider) -> None:
    from urllib.parse import parse_qs

    import anyio
    from starlette.responses import JSONResponse, RedirectResponse

    expired = "<p>Este enlace ha caducado. Vuelve a Claude y pulsa Conectar otra vez.</p>"
    # One passphrase check at a time, in its own thread: parallel guesses can't skip the lockout or
    # starve the tool calls (which use anyio's default thread pool).
    guard = anyio.Lock()
    hashing = anyio.CapacityLimiter(1)

    @srv.custom_route("/hf/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @srv.custom_route("/hf/login", methods=["GET", "POST"])
    async def login(request):
        if request.method == "GET":
            req = request.query_params.get("req", "")
            item = provider.pending_request(req)
            return _page(_form(item, req)) if item else _page(expired, 400)
        try:
            form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace"),
                                                  max_num_fields=8).items()}
        except ValueError:
            return _page(expired, 400)
        req = form.get("req", "")
        item = provider.pending_request(req)
        if not item:
            return _page(expired, 400)
        if form.get("action") == "deny":
            target = provider.deny(req)
            return RedirectResponse(target, status_code=302) if target else _page(expired, 400)
        async with guard:
            wait = provider.locked_for()
            if wait > 0:
                return _page(f"<p class=\"err\">Demasiados intentos fallidos. Prueba dentro de "
                             f"{int(wait // 60) + 1} minutos.</p>", 429)
            if not provider.pending_request(req):  # used up while this request waited
                return _page(expired, 400)
            ok = await anyio.to_thread.run_sync(check_passphrase, provider.settings, form.get("passphrase", ""),
                                                limiter=hashing)
            if not ok:
                alive = provider.record_failure(req)
                print(f"hf remote: frase incorrecta desde {request.client.host if request.client else '?'}",
                      file=sys.stderr, flush=True)
                return _page(_form(item, req, "Frase incorrecta.") if alive else
                             "<p class=\"err\">Frase incorrecta demasiadas veces con este enlace.</p>" + expired, 401)
            target = provider.approve(req)
        if not target:
            return _page(expired, 400)
        print(f"hf remote: conectado '{item.get('client_name') or item['client_id']}'", file=sys.stderr, flush=True)
        return RedirectResponse(target, status_code=302)


# ---------- server ----------

def build_app(settings: Settings, config: RemoteConfig):
    """The Starlette app: MCP at /mcp behind OAuth, OAuth endpoints, the passphrase page and /hf/health."""
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings

    from hfcli import mcp_server

    mcp_server.set_remote_mode(settings.output_dir)
    provider = make_provider(settings, config)
    auth = dict(issuer_url=config.base_url, resource_server_url=config.mcp_url, required_scopes=[SCOPE],
                client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                      default_scopes=[SCOPE]),
                revocation_options=RevocationOptions(enabled=True))
    if "validate_token_resource" in AuthSettings.model_fields:
        auth["validate_token_resource"] = False  # single resource: every token here is for this server
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[config.host, f"{config.host}:443", "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[f"https://{config.host}", "https://claude.ai", "https://claude.com",
                         "http://127.0.0.1:*", "http://localhost:*"],
    )
    srv = mcp_server.build_server(remote=True, auth_server_provider=provider, auth=AuthSettings(**auth),
                                  host="127.0.0.1", port=config.port, stateless_http=True,
                                  transport_security=security)
    add_login_routes(srv, provider)
    return BodyLimit(srv.streamable_http_app()), provider


def serve(settings: Settings, port: int | None = None) -> None:
    config, problems = check_config(settings, port)
    if problems:
        raise UsageError("No arranco el modo remoto:\n- " + "\n- ".join(problems))
    import uvicorn
    app, _ = build_app(settings, config)
    print(f"hf remote: escuchando en 127.0.0.1:{config.port} · conector: {config.mcp_url} · "
          f"tope {config.cap} generaciones/24 h", file=sys.stderr, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=config.port, log_level="warning", access_log=False,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


# ---------- macOS service (launchd) ----------

def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(settings: Settings) -> bytes:
    import plistlib
    log = remote_dir(settings) / "server.log"
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.getenv("HFCLI_HOME"):
        env["HFCLI_HOME"] = str(settings.home)
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "hfcli", "remote", "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "EnvironmentVariables": env,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def install_service(settings: Settings) -> Path:
    if sys.platform != "darwin":
        raise UsageError("El servicio automático solo está hecho para macOS. En otro sistema, lanza "
                         "`hf remote serve` con tu gestor de servicios.")
    _, problems = check_config(settings)
    if problems:
        raise UsageError("Antes de instalar el servicio:\n- " + "\n- ".join(problems))
    remote_dir(settings).mkdir(parents=True, exist_ok=True)
    path = launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launchd_plist(settings))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LAUNCHD_LABEL}")
    res = _launchctl("bootstrap", domain, str(path))
    if res.returncode != 0:
        raise UsageError(f"launchctl no pudo cargar el servicio: {res.stderr.strip() or res.returncode}")
    return path


def uninstall_service() -> bool:
    if sys.platform != "darwin":
        raise UsageError("El servicio automático solo existe en macOS.")
    _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    path = launchd_plist_path()
    if path.exists():
        path.unlink()
        return True
    return False


def service_loaded() -> bool | None:
    if sys.platform != "darwin":
        return None
    return _launchctl("print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}").returncode == 0


def probe(url: str, timeout: float = 5.0) -> str:
    """GET <url>/hf/health. Returns 'ok' or a short reason."""
    import httpx
    try:
        res = httpx.get(url.rstrip("/") + "/hf/health", timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as exc:
        return f"sin respuesta ({type(exc).__name__})"
    if res.status_code == 200 and res.headers.get("content-type", "").startswith("application/json"):
        try:
            return "ok" if res.json().get("ok") is True else f"respuesta inesperada ({res.status_code})"
        except ValueError:
            pass
    return f"respuesta inesperada ({res.status_code})"
