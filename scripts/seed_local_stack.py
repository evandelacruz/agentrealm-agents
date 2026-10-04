#!/usr/bin/env python3
"""Seed a local Agent Realm stack with a dev account and API key.

Re-running with the same --email reuses the key saved in the env file. With
--probe it also waits until sandbox character create succeeds.

Uses only the public HTTP API (Manual §4, §13) plus optional ``docker compose logs``
to read the email-verification token when SMTP is not configured locally.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE = "http://localhost:8080"
DEFAULT_EMAIL = "dev-local@agentrealm.test"
PROBE_NAME = "_local_seed_probe"
VERIFY_TOKEN_PATTERNS = (
    re.compile(r"verify-email\?token=([A-Za-z0-9_-]+)"),
    re.compile(r"first-api-key[^\n\"']*[\"']token[\"']\s*:\s*[\"']([A-Za-z0-9_-]+)[\"']"),
)


@dataclass
class ApiError(Exception):
    status: int
    code: str
    body: Any = None

    def __str__(self) -> str:
        if self.status == 0:
            return f"network error: {self.code}"
        return f"HTTP {self.status} {self.code}"


class Http:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        bearer: str | None = None,
    ) -> Any:
        url = self.base + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            parsed: Any = None
            code = ""
            try:
                parsed = json.loads(e.read() or b"{}")
                if isinstance(parsed, dict):
                    code = str(parsed.get("code", ""))
            except (ValueError, OSError):
                pass
            raise ApiError(e.code, code or e.reason or "", parsed) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            # Refused, reset, or timed out: status 0, as in agentrealm_agent.client.
            raise ApiError(0, str(getattr(e, "reason", None) or e) or type(e).__name__) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace").strip()


def extract_verify_token(text: str, email: str | None = None) -> str | None:
    """Pull the newest verification token from front-tier log lines.

    Prefers a line that names ``email``, then falls back to the last token seen,
    so an older signup earlier in the window is never chosen over this one.
    """
    found: list[tuple[str, bool]] = []
    for line in text.splitlines():
        for pattern in VERIFY_TOKEN_PATTERNS:
            for m in pattern.finditer(line):
                found.append((m.group(1), bool(email) and email in line))
    for token, names_email in reversed(found):
        if names_email:
            return token
    return found[-1][0] if found else None


def wait_for_health(http: Http, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            body = http.request("GET", "/healthz")
            if isinstance(body, str) and body.strip().lower() == "ok":
                return
        except Exception as e:  # noqa: BLE001 — retry until deadline
            last_err = e
        time.sleep(1.0)
    msg = f"front tier at {http.base} did not answer /healthz within {timeout_s:.0f}s"
    if last_err:
        msg += f" (last error: {last_err})"
    raise SystemExit(msg)


def docker_compose_logs(stack_dir: str, service: str, since: str) -> str:
    cmd = ["docker", "compose", "logs", service, f"--since={since}", "--no-log-prefix"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=stack_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as e:
        raise SystemExit("docker not found; pass --verify-token or set AGENTREALM_VERIFY_TOKEN") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"docker compose logs failed in {stack_dir}: {err or proc.returncode}")
    return proc.stdout + proc.stderr


def resolve_verify_token(
    *,
    explicit: str | None,
    stack_dir: str | None,
    log_service: str,
    wait_s: float,
    since: str,
    email: str,
) -> str:
    if explicit:
        return explicit
    if not stack_dir:
        raise SystemExit(
            "need a verification token: set AGENTREALM_VERIFY_TOKEN, pass --verify-token, "
            "or set AGENTREALM_STACK_DIR to the compose project and re-run so logs can be read"
        )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        text = docker_compose_logs(stack_dir, log_service, since)
        token = extract_verify_token(text, email)
        if token:
            return token
        time.sleep(2.0)
    raise SystemExit(
        f"no verification token in {log_service} logs under {stack_dir}; "
        "create the account first or pass --verify-token"
    )


def create_account(http: Http, email: str) -> bool:
    """Sign up ``email``. False when the account already exists."""
    try:
        http.request("POST", "/accounts", {"email": email})
    except ApiError as e:
        if e.status == 409 and e.code == "email_taken":
            return False
        raise
    return True


def key_works(http: Http, api_key: str) -> bool:
    try:
        http.request("GET", "/accounts/me", bearer=api_key)
    except ApiError as e:
        if e.status in (401, 403):
            return False
        raise
    return True


def read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def mint_first_key(http: Http, token: str) -> str:
    out = http.request("POST", "/accounts/me/first-api-key", {"token": token})
    secret = out.get("secret") if isinstance(out, dict) else None
    if not secret:
        raise SystemExit("first-api-key response missing secret")
    return str(secret)


def wait_for_sandbox_create(http: Http, api_key: str, timeout_s: float) -> None:
    """Poll character create until the sandbox sim has loaded its map.

    The API has no character delete and no read-only readiness signal, so the
    probe character stays and holds one of the account's two sandbox slots until
    it ends (PLAN.md Server gaps). That is why the probe is opt-in.
    """
    deadline = time.monotonic() + timeout_s
    body = {
        "name": PROBE_NAME,
        "avatar": "default",
        "model_agent": "agentrealm-reference/seed-probe",
    }
    while time.monotonic() < deadline:
        try:
            http.request(
                "POST",
                f"/worlds/{urllib.parse.quote('sandbox')}/characters",
                body,
                bearer=api_key,
            )
            return
        except ApiError as e:
            if e.status == 409 and e.code == "name_taken" and probe_exists(http, api_key):
                return
            if e.status == 409 and e.code == "world_not_ready":
                time.sleep(2.0)
                continue
            raise
    raise SystemExit(
        f"sandbox world still not ready after {timeout_s:.0f}s (world_not_ready); "
        "is sandbox-sim running?"
    )


def probe_exists(http: Http, api_key: str) -> bool:
    """True when an earlier run's probe is already in the sandbox, so create worked then."""
    chars = http.request("GET", "/characters", bearer=api_key)
    if isinstance(chars, dict):
        chars = chars.get("characters", [])
    return any(
        isinstance(c, dict) and c.get("name") == PROBE_NAME and c.get("world_code") == "sandbox"
        for c in chars or []
    )


def write_env_file(path: str, base_url: str, api_key: str, email: str) -> None:
    """Write sourceable ``export`` lines, readable only by the owner."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"# account {email}\n")
        f.write(f"export AGENTREALM_BASE_URL={base_url}\n")
        f.write(f"export AGENTREALM_API_KEY={api_key}\n")


def default_env_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python", ".state", "local.env")
    )


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=os.environ.get("AGENTREALM_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--email", default=os.environ.get("AGENTREALM_DEV_EMAIL", DEFAULT_EMAIL))
    ap.add_argument(
        "--verify-token",
        default=os.environ.get("AGENTREALM_VERIFY_TOKEN"),
        help="verification token from the signup link (else read from compose logs)",
    )
    ap.add_argument(
        "--stack-dir",
        default=os.environ.get("AGENTREALM_STACK_DIR"),
        help="directory with docker-compose.yml for the game stack (for log scraping)",
    )
    ap.add_argument("--log-service", default=os.environ.get("AGENTREALM_LOG_SERVICE", "front"))
    ap.add_argument("--health-timeout", type=float, default=120.0)
    ap.add_argument("--token-wait", type=float, default=90.0, help="seconds to poll compose logs")
    ap.add_argument("--world-timeout", type=float, default=300.0, help="seconds to wait for sandbox map")
    ap.add_argument(
        "--write-env",
        metavar="PATH",
        default=os.environ.get("AGENTREALM_WRITE_ENV") or default_env_path(),
        help="file for the export lines, mode 0600 (default: python/.state/local.env)",
    )
    ap.add_argument(
        "--print-env",
        action="store_true",
        help="also print the export lines, API key included, to stdout",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help=f"create a {PROBE_NAME} sandbox character to confirm create works; "
        "it cannot be deleted and holds one of the account's two sandbox slots for 24h",
    )
    args = ap.parse_args(argv)
    try:
        return _seed(args)
    except ApiError as e:
        raise SystemExit(f"{e} from {args.base_url}") from None


def _seed(args: argparse.Namespace) -> int:
    http = Http(args.base_url)
    wait_for_health(http, args.health_timeout)

    email = args.email
    env_path = args.write_env
    since = _utc_now()
    if create_account(http, email):
        token = resolve_verify_token(
            explicit=args.verify_token,
            stack_dir=args.stack_dir,
            log_service=args.log_service,
            wait_s=args.token_wait,
            since=since,
            email=email,
        )
        api_key = mint_first_key(http, token)
        write_env_file(env_path, args.base_url, api_key, email)
        print(f"account {email} created; wrote {env_path}", file=sys.stderr)
    else:
        saved = read_env_file(env_path)
        api_key = saved.get("AGENTREALM_API_KEY", "")
        if (
            not api_key
            or saved.get("AGENTREALM_BASE_URL") != args.base_url
            or not key_works(http, api_key)
        ):
            raise SystemExit(
                f"account {email} already exists but {env_path} has no working key for "
                f"{args.base_url}; restore that file or pass a fresh --email"
            )
        print(f"account {email} already seeded; reusing {env_path}", file=sys.stderr)

    if args.probe:
        wait_for_sandbox_create(http, api_key, args.world_timeout)
        print("sandbox create verified", file=sys.stderr)

    if args.print_env:
        print(f"export AGENTREALM_BASE_URL={args.base_url}")
        print(f"export AGENTREALM_API_KEY={api_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
