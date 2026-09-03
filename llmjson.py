"""Shared LLM JSON plumbing.

`_llm_json` and its escape-repair helper were living in legacy_expert.py, which
meant mapping_agent.py had to import from the COBOL agent to make an LLM call
that has nothing to do with COBOL. Extracted here so the two agents share the
plumbing without depending on each other.
"""
from __future__ import annotations

import json
import re
import time


_VALID_ESC = set('"\\/bfnrtu')


def _repair_json(s: str) -> str:
    r"""LLMs occasionally emit malformed escapes (a backslash-u without 4 hex
    digits, or a stray backslash) even in json_object mode — repair instead of
    failing: any invalid escape gets its backslash doubled into a literal."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    out, i, n = [], 0, len(s)
    BS = chr(92)
    while i < n:
        ch = s[i]
        if ch == BS and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "u" and not re.match(r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
                out.append(BS + BS); i += 1; continue
            if nxt != "u" and nxt not in _VALID_ESC:
                out.append(BS + BS); i += 1; continue
            out.append(ch); out.append(nxt); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


def _llm_json(llm, messages, attempts: int = 3,
              max_tokens: int = 4000) -> dict:
    """One call returning parsed JSON, with (a) escape-repairing parse and
    (b) retries with backoff for transient failures.

    `llm` is a LangChain chat model from config.llm_client(); `messages` is the
    plain OpenAI-style list of {"role", "content"} dicts, which LangChain chat
    models accept directly. No deployment argument: the chat model already
    carries it (deployment_name), and a second copy could only disagree with the
    one actually used.
    """
    last = None
    for attempt in range(attempts):
        try:
            resp = llm.invoke(messages, max_tokens=max_tokens, temperature=0,
                              response_format={"type": "json_object"})
            # getattr, not attribute access: a provider that omits
            # response_metadata would otherwise raise AttributeError inside this
            # try, get judged non-transient, and surface instead of the
            # truncation message it was meant to produce
            meta = getattr(resp, "response_metadata", None) or {}
            if meta.get("finish_reason") == "length":
                raise ValueError("response truncated (finish_reason=length) — "
                                 "payload/batch too large for max_tokens")
            raw = resp.content
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return json.loads(_repair_json(raw))
        except Exception as e:                                # noqa: BLE001
            last = e
            # Match the MESSAGE as well as the class name. LangChain and
            # gateways wrap provider errors in their own types, so a 429 can
            # arrive under a name containing none of these words — and raising
            # immediately turns a recoverable blip into a failed tier.
            haystack = f"{type(e).__name__} {e}"
            transient = any(k in haystack.lower() for k in
                            ("ratelimit", "rate limit", "429", "connection",
                             "timeout", "internalserver", "500", "503",
                             "jsondecode", "overloaded"))
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1) ** 2)                # 2s, 8s
    raise last  # pragma: no cover
