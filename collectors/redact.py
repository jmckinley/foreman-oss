"""Redact secrets and (optionally) PII from prompt text before it's stored or shown.

Two levels:
  - ``secrets(text)``  -- dependency-free regex: API keys, tokens, private keys, JWTs, emails,
    long hex/base64 blobs, and inline ``password=…`` style assignments. Fast enough for the render
    path; always applied.
  - ``full(text)``     -- ``secrets`` plus Microsoft Presidio PII detection (PERSON, PHONE_NUMBER,
    CREDIT_CARD, US_SSN, LOCATION, …) IF ``presidio-analyzer`` is installed. Presidio is an optional
    dependency: if it (or its spaCy model) isn't present, ``full`` degrades to ``secrets`` silently.
    Used at store time in the prompt collector, where the extra cost is fine.

Nothing here is a security boundary on its own — the store is already local-only and out-of-repo —
but it keeps a pasted key or a customer's name out of the on-disk prompt history.
"""

from __future__ import annotations

import math
import re

# Vendor patterns aligned to gitleaks' vetted ruleset (the de-facto OSS reference for secret
# scanning): github.com/gitleaks/gitleaks config/gitleaks.toml. Ordered specific-first.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S]{64,}?"
                r"-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"), "[PRIVATE_KEY]"),
    (re.compile(r"\bey[a-zA-Z0-9]{17,}\.ey[a-zA-Z0-9/_-]{17,}\.[a-zA-Z0-9/_-]{10,}={0,2}"), "[JWT]"),
    (re.compile(r"\bsk-ant-[a-z0-9]{2,}-[a-zA-Z0-9_-]{80,}"), "[API_KEY]"),   # Anthropic
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[API_KEY]"),                     # OpenAI-style
    (re.compile(r"\bgithub_pat_\w{82}\b"), "[TOKEN]"),                         # GitHub fine-grained
    (re.compile(r"\bgh[pousr]_[0-9a-zA-Z]{36}\b"), "[TOKEN]"),                 # GitHub PAT
    (re.compile(r"\bglpat-[\w-]{20}\b"), "[TOKEN]"),                           # GitLab PAT
    (re.compile(r"\bxox[baprs]-[0-9]{10,13}-[0-9A-Za-z-]{10,}"), "[TOKEN]"),   # Slack
    (re.compile(r"\bhooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}"),
     "[SLACK_WEBHOOK]"),
    (re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b"), "[AWS_KEY]"),   # AWS
    (re.compile(r"\bAIza[\w-]{35}\b"), "[GCP_KEY]"),                           # GCP
    (re.compile(r"\b(?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{10,99}\b"), "[STRIPE_KEY]"),
    (re.compile(r"\bnpm_[a-z0-9]{36}\b"), "[TOKEN]"),                          # npm
    (re.compile(r"\bpypi-AgEIcHlwaS5vcmc[\w-]{50,}"), "[TOKEN]"),              # PyPI
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"), "Bearer [TOKEN]"),
    (re.compile(r"://[^\s:/@]+:[^\s:/@]+@"), "://[CREDENTIALS]@"),             # user:pass@host
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|auth)\b"
                r"(\s*[=:]\s*)(\"[^\"]+\"|'[^']+'|\S+)"), r"\1\2[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[HEX]"),
]

# Entropy fallback, mirroring gitleaks' generic-api-key rule: an unknown long token with high
# Shannon entropy is almost certainly a secret, not prose. Catches keys no named pattern covers.
_ENTROPY_MIN_LEN = 20
_ENTROPY_THRESHOLD = 4.0        # bits/char; random base64 ~5-6, English prose ~3-4 but space-split
# no '/' in the charset: it keeps file paths ("collectors/scheduler.py") from reading as one
# long high-entropy token. Slash-free secrets (the common case) are still caught.
_TOKEN_RE = re.compile(r"[A-Za-z0-9+=_-]{%d,}" % _ENTROPY_MIN_LEN)


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _entropy_mask(text: str) -> str:
    def repl(m):
        tok = m.group(0)
        return "[SECRET]" if _shannon(tok) >= _ENTROPY_THRESHOLD else tok
    return _TOKEN_RE.sub(repl, text)


def secrets(text: str) -> str:
    """Regex redaction of credentials/emails/blobs (gitleaks-aligned) plus a Shannon-entropy pass
    for unknown high-entropy tokens. Dependency-free and fast."""
    if not text:
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return _entropy_mask(text)


# ---- optional Presidio -------------------------------------------------------------------------
_ENGINE = False        # False = not yet probed; None = unavailable; else the AnalyzerEngine
_PII_ENTITIES = ("PERSON", "PHONE_NUMBER", "CREDIT_CARD", "US_SSN", "IBAN_CODE",
                 "LOCATION", "IP_ADDRESS", "MEDICAL_LICENSE")


def _engine():
    global _ENGINE
    if _ENGINE is not False:
        return _ENGINE
    try:
        from presidio_analyzer import AnalyzerEngine
        _ENGINE = AnalyzerEngine()
    except Exception:
        _ENGINE = None                 # not installed, or model missing -> stay on regex only
    return _ENGINE


def has_presidio() -> bool:
    return _engine() is not None


def full(text: str) -> str:
    """``secrets`` plus Presidio PII masking when available; otherwise identical to ``secrets``."""
    text = secrets(text)
    eng = _engine()
    if not text or eng is None:
        return text
    try:
        results = eng.analyze(text=text, entities=list(_PII_ENTITIES), language="en")
    except Exception:
        return text
    # replace spans right-to-left so earlier offsets stay valid
    for r in sorted(results, key=lambda r: r.start, reverse=True):
        if r.score >= 0.5:
            text = text[:r.start] + f"[{r.entity_type}]" + text[r.end:]
    return text
