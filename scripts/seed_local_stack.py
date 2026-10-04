#!/usr/bin/env python3
"""Seed a local Agent Realm stack with a dev account, API key, and sandbox readiness.

Uses only the public HTTP API (Manual §4, §13) plus optional ``docker compose logs``
to read the email-verification token when SMTP is not configured locally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace").strip()


def extract_verify_token(text: str) -> str | None:
    """Pull the verification token from front-tier log lines."""
    for pattern in VERIFY_TOKEN_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


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


def docker_compose_logs(stack_dir: str, service: str, since: str = "2m") -> str:
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
        text = docker_compose_logs(stack_dir, log_service)
        token = extract_verify_token(text)
        if token:
            return token
        time.sleep(2.0)
    raise SystemExit(
        f"no verification token in {log_service} logs under {stack_dir}; "
        "create the account first or pass --verify-token"
    )


def create_account(http: Http, email: str) -> dict:
    try:
        return http.request("POST", "/accounts", {"email": email})
    except ApiError as e:
        if e.status == 409 and e.code == "email_taken":
            raise SystemExit(
                f"account {email} already exists; set AGENTREALM_API_KEY to an existing key "
                "or use a fresh --email"
            ) from e
        raise


def mint_first_key(http: Http, token: str) -> str:
    try:
        out = http.request("POST", "/accounts/me/first-api-key", {"token": token})
    except ApiError as e:
        if e.status == 409 and e.code == "key_already_exists":
            raise SystemExit(
                "this account already has an API key; set AGENTREALM_API_KEY to that secret "
                "(secrets are shown only once at mint time)"
            ) from e
        raise
    secret = out.get("secret") if isinstance(out, dict) else None
    if not secret:
        raise SystemExit("first-api-key response missing secret")
    return str(secret)


def wait_for_sandbox_create(http: Http, api_key: str, timeout_s: float) -> None:
    """Poll character create until the sandbox sim has loaded its map."""
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
            if e.status == 409 and e.code in ("name_taken", "identity_reuse"):
                return
            if e.status == 409 and e.code == "world_not_ready":
                time.sleep(2.0)
                continue
            raise
    raise SystemExit(
        f"sandbox world still not ready after {timeout_s:.0f}s (world_not_ready); "
        "is sandbox-sim running?"
    )


def write_env_file(path: str, base_url: str, api_key: str, email: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"AGENTREALM_BASE_URL={base_url}\n")
        f.write(f"AGENTREALM_API_KEY={api_key}\n")
        f.write(f"# account {email}\n")


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
        default=os.environ.get("AGENTREALM_WRITE_ENV"),
        help="write AGENTREALM_* lines to this file (default: unset; try python/.state/local.env)",
    )
    ap.add_argument(
        "--print-env",
        action="store_true",
        help="print export lines for AGENTREALM_BASE_URL and AGENTREALM_API_KEY",
    )
    ap.add_argument(
        "--skip-probe",
        action="store_true",
        help="do not POST a probe character to confirm sandbox readiness",
    )
    args = ap.parse_args(argv)

    http = Http(args.base_url)
    wait_for_health(http, args.health_timeout)

    email = args.email
    if email == DEFAULT_EMAIL and "@" in email:
        # Allow parallel runs without colliding on the default address.
        local, domain = email.split("@", 1)
        email = f"{local}+{uuid.uuid4().hex[:8]}@{domain}"

    create_account(http, email)

    token = resolve_verify_token(
        explicit=args.verify_token,
        stack_dir=args.stack_dir,
        log_service=args.log_service,
        wait_s=args.token_wait,
    )
    api_key = mint_first_key(http, token)

    if not args.skip_probe:
        wait_for_sandbox_create(http, api_key, args.world_timeout)

    env_path = args.write_env
    if env_path is None:
        env_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "python", ".state", "local.env")
        )

    write_env_file(env_path, args.base_url, api_key, email)

    if args.print_env:
        print(f"export AGENTREALM_BASE_URL={args.base_url}")
        print(f"export AGENTREALM_API_KEY={api_key}")
    else:
        print(f"AGENTREALM_BASE_URL={args.base_url}")
        print(f"AGENTREALM_API_KEY={api_key}")
        print(f"# wrote {env_path}", file=sys.stderr)
    print(f"account {email} ready; sandbox create verified", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
