"""llm_check — step-by-step diagnosis of LLM connectivity.

'Connection error.' from the OpenAI SDK hides the actual cause. This walks the
chain explicitly — config, DNS, TCP, TLS, live completion — and reports the
first failing step with a likely-cause hint (corporate proxy, TLS interception,
private endpoint, endpoint typo). Used by `python -m cli.llm_check` and
GET /api/llm/check.
"""
from __future__ import annotations

import os
import socket
import ssl
import time
from urllib.parse import urlparse

from .config import llm_client, llm_label, llm_ready


def _step(name: str, ok: bool, detail: str, hint: str = "") -> dict:
    return {"step": name, "ok": ok, "detail": detail, "hint": hint}


def diagnose(timeout: float = 8.0) -> list[dict]:
    steps: list[dict] = []

    # ---- 1. configuration
    if not llm_ready():
        steps.append(_step("config", False, "no LLM configured",
                           "fill .env (AZURE_OPENAI_ENDPOINT / _API_KEY / "
                           "_DEPLOYMENT, or OPENAI_API_KEY) and restart"))
        return steps
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or \
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    host = urlparse(endpoint).hostname or endpoint
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    steps.append(_step("config", True,
                       f"{llm_label()} -> {host}"
                       + (f" via proxy {proxy}" if proxy else " (no proxy env set)")))
    if not endpoint.startswith("https://"):
        steps.append(_step("endpoint-format", False, endpoint,
                           "AZURE_OPENAI_ENDPOINT must be the base URL: "
                           "https://<resource>.openai.azure.com"))
        return steps

    # ---- 2. DNS  (skipped when a proxy is set: the proxy resolves the name)
    if not proxy:
        try:
            ip = socket.gethostbyname(host)
            steps.append(_step("dns", True, f"{host} -> {ip}"))
        except OSError as e:
            steps.append(_step("dns", False, f"{host}: {e}",
                               "name does not resolve — typo in the endpoint, or "
                               "this network requires a proxy (set HTTPS_PROXY, "
                               "and NO_PROXY=localhost,127.0.0.1)"))
            return steps

        # ---- 3. TCP + TLS (direct connections only)
        try:
            with socket.create_connection((host, 443), timeout=timeout) as sock:
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    issuer = dict(x[0] for x in tls.getpeercert()["issuer"])
                    steps.append(_step("tcp+tls", True,
                                       f"TLS ok, issuer: {issuer.get('organizationName', '?')}"))
        except ssl.SSLError as e:
            steps.append(_step("tcp+tls", False, str(e),
                               "TLS failed — corporate SSL inspection is likely; "
                               "point SSL_CERT_FILE at the corporate CA bundle "
                               "(.pem) and restart"))
            return steps
        except OSError as e:
            steps.append(_step("tcp+tls", False, str(e),
                               "port 443 blocked or the Azure resource only "
                               "allows private endpoints — check firewall rules "
                               "and the resource's 'public network access' setting"))
            return steps

    # ---- 4. a real one-token completion
    client, model = llm_client()
    try:
        t0 = time.time()
        client.chat.completions.create(
            model=model, max_tokens=1,
            messages=[{"role": "user", "content": "ping"}], timeout=timeout)
        steps.append(_step("completion", True,
                           f"deployment '{model}' answered in {time.time()-t0:.1f}s"))
    except Exception as e:                                    # noqa: BLE001
        cause = e.__cause__ or getattr(e, "__context__", None)
        detail = f"{type(e).__name__}: {e}"
        if cause:
            detail += f" (cause: {type(cause).__name__}: {cause})"
        hint = ("404/DeploymentNotFound -> AZURE_OPENAI_DEPLOYMENT must be the "
                "DEPLOYMENT name from Azure OpenAI Studio, not 'gpt-4o'; "
                "401 -> wrong key; timeout/connect through a proxy -> the proxy "
                "may need credentials or the CA bundle (SSL_CERT_FILE)")
        steps.append(_step("completion", False, detail, hint))
    return steps


def summary(steps: list[dict]) -> str:
    lines = []
    for s in steps:
        lines.append(f"{'✓' if s['ok'] else '✗'} {s['step']}: {s['detail']}")
        if not s["ok"] and s["hint"]:
            lines.append(f"   → {s['hint']}")
    return "\n".join(lines)
