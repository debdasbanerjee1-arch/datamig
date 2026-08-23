"""Load environment from .env once, on import."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass  # dotenv optional; env vars may be set by the shell


def _azure_configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"))


def azure_ready() -> bool:  # kept for backward compatibility
    return _azure_configured()


def _sdk_installed() -> bool:
    """The `openai` package is an OPTIONAL dependency — the whole pipeline runs
    deterministically without it. Credentials alone are not readiness: with a
    key set but the SDK absent, `from openai import OpenAI` raised
    ModuleNotFoundError mid-run and took the whole mapping down. Readiness means
    credentials AND a usable client."""
    return importlib.util.find_spec("openai") is not None


def llm_ready() -> bool:
    """True if credentials are configured AND the openai SDK is importable."""
    return (_azure_configured() or bool(os.environ.get("OPENAI_API_KEY"))) \
        and _sdk_installed()


def credentials_present() -> bool:
    """Credentials configured, regardless of whether the SDK is installed —
    used to tell 'no key' apart from 'key but no SDK' in the UI."""
    return _azure_configured() or bool(os.environ.get("OPENAI_API_KEY"))


def llm_label() -> str:
    """Short label of the active provider/model, or '' when offline."""
    if not _sdk_installed():
        return "credentials set, but the `openai` package is not installed" \
            if credentials_present() else ""
    if _azure_configured():
        return "Azure · " + os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    if os.environ.get("OPENAI_API_KEY"):
        return "OpenAI · " + os.environ.get("OPENAI_MODEL", "gpt-4o")
    return ""


def llm_client():
    """Return (client, model) for whichever provider is configured, else (None, None).

    Standard OpenAI:  OPENAI_API_KEY  (+ optional OPENAI_MODEL, default gpt-4o)
    Azure OpenAI:     AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT (+ optional _API_VERSION)
    """
    if not _sdk_installed():
        return None, None
    if _azure_configured():
        from openai import AzureOpenAI
        client = AzureOpenAI(
            max_retries=3, timeout=60.0,
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        return client, os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        kwargs = {"api_key": os.environ["OPENAI_API_KEY"]}
        if os.environ.get("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        client = OpenAI(max_retries=3, timeout=60.0, **kwargs)
        return client, os.environ.get("OPENAI_MODEL", "gpt-4o")
    return None, None
