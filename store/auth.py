"""Auth: issues and verifies session tokens. No dependencies. Session lifetime
comes from hot-reloaded config."""

import hashlib
import hmac
import time

from fastapi import HTTPException

from common import create_app, get_config, log

app = create_app()
SECRET = b"dukkan-session-secret"
DEFAULT = {"version": 1, "token_ttl_s": 3600}


def _sign(user: str, issued: int) -> str:
    return hmac.new(SECRET, f"{user}.{issued}".encode(), hashlib.sha256).hexdigest()[:24]


@app.post("/login")
def login(body: dict):
    user = str(body.get("user", "guest"))[:40].replace(".", "")
    issued = int(time.time())
    log("info", f"issued session token for user {user}")
    return {"token": f"{user}.{issued}.{_sign(user, issued)}", "user": user}


@app.get("/verify")
def verify(token: str):
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit() or not hmac.compare_digest(parts[2], _sign(parts[0], int(parts[1]))):
        log("warn", "rejected invalid session token")
        raise HTTPException(401, "invalid token")
    user, issued = parts[0], int(parts[1])
    cfg = get_config("auth", DEFAULT)
    ttl = int(cfg.get("token_ttl_s", 3600))
    age = int(time.time()) - issued
    if age >= ttl:
        log("warn", f"rejected expired session for {user}: age {age}s is not under token_ttl_s {ttl} (config v{cfg.get('version')})")
        raise HTTPException(401, "session expired")
    return {"user": user}
