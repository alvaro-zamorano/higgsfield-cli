"""Remote connector: passphrase, config gate, OAuth flow (DCR + PKCE + passphrase page), token rotation,
revocation, host checks, @file restriction and the launchd service definition. Nothing here spends money:
api.submit is patched to fail the test if anything tries to send a generation."""
from __future__ import annotations

import base64
import hashlib
import json
import plistlib
import secrets
import socket
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("mcp")

from hfcli import api, cli, remote, storage  # noqa: E402
from hfcli import mcp_server as m  # noqa: E402
from hfcli.config import UsageError, load_settings  # noqa: E402

PASSPHRASE = "correct horse battery staple"
RID = "f4017ce6-b83d-4d79-b7ec-46beb4b54a7d"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HFCLI_HOME", str(tmp_path))
    monkeypatch.setenv("HF_KEY", "test-id:test-secret-value")
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "3")
    monkeypatch.delenv("HF_REMOTE_URL", raising=False)
    monkeypatch.delenv("HF_REMOTE_PORT", raising=False)
    monkeypatch.delenv("CLOUDINARY_URL", raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    monkeypatch.setattr(api, "submit", lambda *a, **k: pytest.fail("nothing may be submitted in these tests"))
    yield tmp_path
    m.set_remote_mode(None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(home, monkeypatch):
    import uvicorn
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HF_REMOTE_URL", base)
    settings = load_settings()
    remote.set_passphrase(settings, PASSPHRASE)
    config, problems = remote.check_config(settings)
    assert not problems, problems
    app, provider = remote.build_app(settings, config)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started
    yield {"base": base, "settings": settings, "provider": provider}
    srv.should_exit = True
    thread.join(5)


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _register(base: str, redirect: str = CLAUDE_CB, name: str = "Claude") -> httpx.Response:
    return httpx.post(f"{base}/register", json={
        "redirect_uris": [redirect], "client_name": name, "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post", "scope": "hf"})


def _start_login(base: str, client: dict, challenge: str, state: str = "st4te") -> str:
    res = httpx.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0],
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "scope": "hf",
        "resource": f"{base}/mcp"})
    assert res.status_code == 302, res.text
    login = res.headers["location"]
    assert login.startswith(f"{base}/hf/login?req=")
    return parse_qs(urlsplit(login).query)["req"][0]


def _submit(base: str, req: str, passphrase: str, action: str = "allow") -> httpx.Response:
    return httpx.post(f"{base}/hf/login", data={"req": req, "passphrase": passphrase, "action": action})


def _token(base: str, client: dict, **form) -> httpx.Response:
    return httpx.post(f"{base}/token", data={"client_id": client["client_id"],
                                             "client_secret": client["client_secret"], **form})


def _code_flow(base: str, client: dict) -> dict:
    verifier, challenge = _pkce()
    req = _start_login(base, client, challenge)
    res = _submit(base, req, PASSPHRASE)
    code = parse_qs(urlsplit(res.headers["location"]).query)["code"][0]
    res = _token(base, client, grant_type="authorization_code", code=code, code_verifier=verifier,
                 redirect_uri=client["redirect_uris"][0])
    assert res.status_code == 200, res.text
    return res.json()


def _rpc(base: str, token: str | None, host: str | None = None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    return httpx.post(f"{base}/mcp", json=body, headers=headers)


# ---------- passphrase and configuration ----------

def test_passphrase_is_stored_hashed_and_private(home):
    settings = load_settings()
    with pytest.raises(UsageError, match="12"):
        remote.set_passphrase(settings, "short")
    remote.set_passphrase(settings, PASSPHRASE)
    path = remote.passphrase_file(settings)
    assert PASSPHRASE not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert remote.check_passphrase(settings, PASSPHRASE)
    assert not remote.check_passphrase(settings, PASSPHRASE + "x")


def test_config_gate_refuses_to_start_without_everything(home, monkeypatch):
    settings = load_settings()
    monkeypatch.delenv("HFCLI_MCP_MAX_JOBS_PER_DAY")
    config, problems = remote.check_config(settings)
    text = " ".join(problems)
    assert config is None and "HF_REMOTE_URL" in text and "tope diario" in text and "hf remote passphrase" in text
    for bad in ("http://mac.tailnet.ts.net", "https://mac.tailnet.ts.net/mcp", "ftp://x", "mac.ts.net"):
        monkeypatch.setenv("HF_REMOTE_URL", bad)
        assert any("HF_REMOTE_URL" in p for p in remote.check_config(settings)[1]), bad
    monkeypatch.setenv("HF_REMOTE_URL", "https://mac.tailnet.ts.net/")
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "0")
    remote.set_passphrase(settings, PASSPHRASE)
    assert any("tope diario" in p for p in remote.check_config(settings)[1])
    monkeypatch.setenv("HFCLI_MCP_MAX_JOBS_PER_DAY", "5")
    config, problems = remote.check_config(settings)
    assert not problems and config.mcp_url == "https://mac.tailnet.ts.net/mcp" and config.cap == 5
    monkeypatch.setenv("HFCLI_OUTPUT", str(home.parent))
    assert any("carpeta de salida" in p for p in remote.check_config(load_settings())[1])
    monkeypatch.delenv("HFCLI_OUTPUT")
    with pytest.raises(UsageError, match="No arranco"):
        monkeypatch.delenv("HF_KEY")
        remote.serve(settings)


def test_redirect_allowlist():
    ok = [CLAUDE_CB, "https://claude.com/api/mcp/auth_callback", "http://localhost:3118/callback",
          "http://127.0.0.1:55000/callback"]
    bad = ["https://evil.example/cb", "http://evil.example/cb", "https://claude.ai/other",
           "http://user@localhost:1/cb", "https://localhost.evil.example/cb", "javascript:alert(1)"]
    assert all(remote.redirect_allowed(u) for u in ok)
    assert not any(remote.redirect_allowed(u) for u in bad)


def test_status_never_prints_secrets(home, monkeypatch, capsys):
    monkeypatch.setenv("HF_REMOTE_URL", "http://127.0.0.1:9")
    settings = load_settings()
    remote.set_passphrase(settings, PASSPHRASE)
    code = cli.main(["remote", "status"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code != 0 and "Servidor local" in text and "FAIL" in text
    assert PASSPHRASE not in text and "test-secret-value" not in text


def test_launchd_plist(home):
    data = plistlib.loads(remote.launchd_plist(load_settings()))
    assert data["Label"] == remote.LAUNCHD_LABEL
    assert data["ProgramArguments"][-3:] == ["hfcli", "remote", "serve"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["StandardErrorPath"].startswith(str(home / "remote"))
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]


def test_remote_mode_only_uploads_generated_files(home, tmp_path_factory):
    settings = load_settings()
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.png"
    outside.write_bytes(b"x")
    inside = settings.output_dir / "2026-09-27" / "frame.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    stray = settings.output_dir / "2026-09-27" / "not-generated.png"  # in the folder, but not in the history
    stray.write_bytes(b"x")
    storage.append_history(settings.history_file, {"request_id": RID, "status": "completed", "ok": True,
                                                   "outputs": [{"file": str(inside)}]})
    m.set_remote_mode(settings.output_dir)
    with pytest.raises(UsageError, match="carpeta de salida"):
        m.generate_impl("seedance-2.5-i2v", "slow push-in", {"image_url": f"@{outside}"})
    link = settings.output_dir / "link.png"
    link.symlink_to(outside)
    with pytest.raises(UsageError, match="carpeta de salida"):
        m.generate_impl("seedance-2.5-i2v", "slow push-in", {"image_url": f"@{link}"})
    out = m.generate_impl("seedance-2.5-i2v", "slow push-in", {"image_url": f"@{inside}"})
    assert out["dry_run"] is True
    ledger = settings.output_dir / "mcp_ledger.jsonl"
    ledger.write_text("{}\n")
    refused = [
        ("seedance-2.5-i2v", {"image_url": f"@{ledger}"}),                          # not media
        ("seedance-2.5-i2v", {"image_url": "@/etc/nope.png"}),                      # doesn't exist (no oracle)
        ("seedance-2.5-i2v", {"image_url": "@2026-09-27/frame.png"}),               # relative
        ("seedance-2.5-i2v", {"image_url": f"@ {inside}"}),                         # checked path != uploaded path
        ("seedance-2.5-i2v", {"image_url": f"@{stray}"}),                           # not a generated file
        ("kling-3", {"multi_prompt": [{"prompt": f"@{inside}", "duration": 5}]}),  # nested, not a URL field
        ("vendor/unknown/endpoint", {"image_url": f"@{inside}"}),                   # not in the catalogue
        ("vendor/unknown/endpoint", {}),
    ]
    messages = set()
    for model, params in refused:
        with pytest.raises(UsageError) as err:
            m.generate_impl(model, "x", params)
        messages.add(str(err.value))
    assert len(messages) == 2 and any("catálogo" in msg for msg in messages)
    m.set_remote_mode(None)
    assert m.generate_impl("seedance-2.5-i2v", "slow push-in", {"image_url": f"@{outside}"})["dry_run"] is True


# ---------- OAuth over HTTP ----------

def test_discovery_and_unauthenticated_requests(server):
    base = server["base"]
    res = _rpc(base, None)
    assert res.status_code == 401
    assert "resource_metadata=" in res.headers["www-authenticate"]
    prm = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{base}/mcp" and prm["authorization_servers"][0].rstrip("/") == base
    asm = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    assert "S256" in asm["code_challenge_methods_supported"] and asm["registration_endpoint"]
    assert _rpc(base, "not-a-token").status_code == 401
    assert httpx.get(f"{base}/hf/health").json() == {"ok": True}


def test_registration_only_accepts_claude_or_loopback(server):
    res = _register(server["base"], "https://evil.example/cb")
    assert res.status_code == 400 and res.json()["error"] == "invalid_redirect_uri"
    assert _register(server["base"]).status_code == 201
    assert _register(server["base"], "http://localhost:3118/callback").status_code == 201


def test_passphrase_page_code_exchange_refresh_and_revoke(server, monkeypatch):
    base = server["base"]
    client = _register(base, name="<script>alert(1)</script>").json()
    verifier, challenge = _pkce()
    req = _start_login(base, client, challenge, state="abc")

    page = httpx.get(f"{base}/hf/login", params={"req": req})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text and "claude.ai" in page.text
    assert httpx.get(f"{base}/hf/login", params={"req": "bogus"}).status_code == 400

    assert _submit(base, req, "wrong passphrase!!").status_code == 401
    res = _submit(base, req, PASSPHRASE)
    assert res.status_code == 302
    target = urlsplit(res.headers["location"])
    query = parse_qs(target.query)
    assert f"{target.scheme}://{target.netloc}{target.path}" == CLAUDE_CB and query["state"] == ["abc"]
    code = query["code"][0]
    assert _submit(base, req, PASSPHRASE).status_code == 400  # the login link is single use

    bad = _token(base, client, grant_type="authorization_code", code=code, code_verifier="x" * 50,
                 redirect_uri=CLAUDE_CB)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    res = _token(base, client, grant_type="authorization_code", code=code, code_verifier=verifier,
                 redirect_uri=CLAUDE_CB)
    assert res.status_code == 200, res.text
    tokens = res.json()
    again = _token(base, client, grant_type="authorization_code", code=code, code_verifier=verifier,
                   redirect_uri=CLAUDE_CB)
    assert again.status_code == 400  # codes are single use

    state_file = remote.remote_dir(server["settings"]) / "state.json"
    raw = state_file.read_text()
    assert tokens["access_token"] not in raw and tokens["refresh_token"] not in raw
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

    assert _rpc(base, tokens["access_token"]).status_code == 200
    assert _rpc(base, tokens["access_token"], host="evil.example").status_code in (400, 421)
    assert remote.connected_clients(server["settings"]) == ["<script>alert(1)</script>"]

    res = _token(base, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert res.status_code == 200, res.text
    fresh = res.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert _rpc(base, tokens["access_token"]).status_code == 401  # rotated away
    assert _rpc(base, fresh["access_token"]).status_code == 200
    # the old refresh token comes back after the grace window (stolen?): refused, and every token of
    # that client is dropped
    monkeypatch.setattr(remote, "REFRESH_GRACE_S", 0)
    assert _token(base, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"]).status_code == 400
    assert _rpc(base, fresh["access_token"]).status_code == 401
    assert _token(base, client, grant_type="refresh_token", refresh_token=fresh["refresh_token"]).status_code == 400

    again = _code_flow(base, client)
    assert _rpc(base, again["access_token"]).status_code == 200
    assert remote.revoke_all(server["settings"]) == 1  # e.g. `hf remote revoke` from another terminal
    assert _rpc(base, again["access_token"]).status_code == 401
    assert _token(base, client, grant_type="refresh_token", refresh_token=again["refresh_token"]).status_code in (400, 401)


def test_wrong_passphrases_use_up_the_link_and_lock_the_page(server):
    base = server["base"]
    client = _register(base).json()
    _, challenge = _pkce()
    req = _start_login(base, client, challenge)
    for _ in range(remote.TRIES_PER_LOGIN):
        assert _submit(base, req, "not the passphrase").status_code == 401
    assert _submit(base, req, PASSPHRASE).status_code == 400  # this link is used up
    for _ in range(remote.FAILS_BEFORE_LOCK - remote.TRIES_PER_LOGIN):
        assert _submit(base, _start_login(base, client, challenge), "nope nope nope").status_code == 401
    res = _submit(base, _start_login(base, client, challenge), PASSPHRASE)
    assert res.status_code == 429 and "location" not in res.headers


def test_parallel_guesses_cannot_skip_the_lockout(server, monkeypatch):
    import anyio
    base = server["base"]
    client = _register(base).json()
    _, challenge = _pkce()
    reqs = [_start_login(base, client, challenge) for _ in range(12)]
    checks = []
    real = remote.check_passphrase
    monkeypatch.setattr(remote, "check_passphrase", lambda *a: (checks.append(1), real(*a))[1])

    async def post(http, req):
        await http.post(f"{base}/hf/login", data={"req": req, "passphrase": "guess guess guess", "action": "allow"})

    async def burst():
        async with httpx.AsyncClient(timeout=60) as http, anyio.create_task_group() as tg:
            for req in reqs:
                for _ in range(3):
                    tg.start_soon(post, http, req)
    anyio.run(burst)
    assert len(checks) == remote.FAILS_BEFORE_LOCK


def test_unauthenticated_bodies_are_bounded(server):
    base = server["base"]
    big = "x" * (remote.BODY_LIMIT + 1)
    assert httpx.post(f"{base}/hf/login", data={"req": big}).status_code == 413
    assert httpx.post(f"{base}/register", content=big, headers={"content-type": "application/json"}).status_code == 413

    def chunks():  # no Content-Length: the limit still applies while reading
        for _ in range(64):
            yield b"y" * 1024
    assert httpx.post(f"{base}/token", content=chunks(),
                      headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_registration_keeps_only_what_the_flow_needs(server):
    base = server["base"]
    res = httpx.post(f"{base}/register", json={
        "redirect_uris": [CLAUDE_CB], "client_name": "N" * 500, "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "contacts": ["a@b.c"], "jwks": {"keys": [{"k": "z" * 1000}]}})
    assert res.status_code == 201
    stored = json.loads((remote.remote_dir(server["settings"]) / "state.json").read_text())["clients"]
    rec = stored[res.json()["client_id"]]
    assert "jwks" not in rec and "contacts" not in rec and len(rec["client_name"]) == remote.MAX_CLIENT_NAME


def test_tokens_are_not_issued_to_a_client_revoked_mid_flow(server):
    base = server["base"]
    client = _register(base).json()
    verifier, challenge = _pkce()
    req = _start_login(base, client, challenge)
    code = parse_qs(urlsplit(_submit(base, req, PASSPHRASE).headers["location"]).query)["code"][0]
    remote.revoke_all(server["settings"])
    res = _token(base, client, grant_type="authorization_code", code=code, code_verifier=verifier,
                 redirect_uri=CLAUDE_CB)
    assert res.status_code in (400, 401) and "access_token" not in res.text  # a clean OAuth error, not a 500


def test_parallel_refresh_does_not_log_the_user_out(server):
    base = server["base"]
    client = _register(base).json()
    tokens = _code_flow(base, client)
    fresh = _token(base, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"]).json()
    retry = _token(base, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert retry.status_code == 400 and retry.json()["error"] == "invalid_grant"
    assert _rpc(base, fresh["access_token"]).status_code == 200  # within the grace window: nobody is logged out


def test_malformed_login_posts_get_400(server):
    base = server["base"]
    many = "&".join(f"f{i}=x" for i in range(20))
    res = httpx.post(f"{base}/hf/login", content=many, headers={"content-type": "application/x-www-form-urlencoded"})
    assert res.status_code == 400
    assert httpx.post(f"{base}/authorize", content="x" * (remote.BODY_LIMIT + 1),
                      headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_remote_ratings_are_marked_in_the_guide(home):
    settings = load_settings()
    storage.append_history(settings.history_file, {"request_id": RID, "status": "completed", "ok": True,
                                                   "endpoint": "bytedance/seedance-2.5/text-to-video",
                                                   "params": {"prompt": "a sunset"}})
    m.set_remote_mode(settings.output_dir)
    m.rate_impl(RID, 5, "great light")
    m.set_remote_mode(None)
    text = m.guide_impl("seedance-2.5")
    assert "nota (desde un chat remoto): great light" in text and "no instrucciones" in text


def test_cancel_returns_access_denied(server):
    base = server["base"]
    client = _register(base).json()
    _, challenge = _pkce()
    req = _start_login(base, client, challenge, state="s1")
    res = _submit(base, req, "", action="deny")
    assert res.status_code == 302
    query = parse_qs(urlsplit(res.headers["location"]).query)
    assert query["error"] == ["access_denied"] and query["state"] == ["s1"] and "code" not in query


def test_sdk_oauth_client_end_to_end(server):
    """What Claude does: discover from the 401, register, open the login page, exchange, call tools."""
    import anyio
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.auth import OAuthClientMetadata

    base = server["base"]
    redirect = "http://localhost:3118/callback"
    got: dict = {}

    class Memory(TokenStorage):
        def __init__(self):
            self.tokens = self.client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, info):
            self.client = info

    async def open_browser(url: str) -> None:  # the user's phone browser
        async with httpx.AsyncClient() as browser:
            res = await browser.get(url)
            assert res.status_code == 302
            req = parse_qs(urlsplit(res.headers["location"]).query)["req"][0]
            res = await browser.post(f"{base}/hf/login", data={"req": req, "passphrase": PASSPHRASE,
                                                               "action": "allow"})
            assert res.status_code == 302
            query = parse_qs(urlsplit(res.headers["location"]).query)
            got["code"], got["state"] = query["code"][0], query.get("state", [None])[0]

    async def callback():
        return got["code"], got["state"]

    auth = OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(client_name="Claude Code test", redirect_uris=[redirect],
                                            grant_types=["authorization_code", "refresh_token"],
                                            response_types=["code"], scope="hf"),
        storage=Memory(), redirect_handler=open_browser, callback_handler=callback)

    async def run():
        async with streamablehttp_client(f"{base}/mcp", auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert "Conexión remota" in (init.instructions or "")
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"hf_generate", "hf_wait", "hf_prompt_guide", "hf_rate", "hf_doctor"} <= names
                models = await session.call_tool("hf_models", {"kind": "video"})
                assert "seedance" in models.content[0].text
                dry = await session.call_tool("hf_generate", {"model": "seedance-2.5", "prompt": "a sunset",
                                                              "params": {"duration": 5}})
                assert json.loads(dry.content[0].text)["dry_run"] is True
                outside = await session.call_tool("hf_generate", {"model": "seedance-2.5-i2v", "prompt": "x",
                                                                  "params": {"image_url": "@/etc/hosts"}})
                assert "carpeta de salida" in outside.content[0].text

    anyio.run(run)
    assert remote.connected_clients(server["settings"]) == ["Claude Code test"]
