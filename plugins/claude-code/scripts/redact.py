#!/usr/bin/env python3
"""Redaction helpers for Jieli sync."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

Replacement = str | Callable[[re.Match[str]], str]


def typed_redaction(rule_id: str) -> str:
    return f"[REDACTED:{rule_id}]"


@dataclass(frozen=True)
class RedactionRule:
    id: str
    pattern: re.Pattern[str]
    keywords: tuple[str, ...] = ()
    replacement: Replacement | None = None
    case_insensitive_keywords: bool = True

    def applies_to(self, value: str) -> bool:
        if not self.keywords:
            return True
        haystack = value.lower() if self.case_insensitive_keywords else value
        needles = (
            tuple(keyword.lower() for keyword in self.keywords)
            if self.case_insensitive_keywords
            else self.keywords
        )
        return any(keyword in haystack for keyword in needles)

    def apply(self, value: str) -> str:
        replacement = (
            self.replacement
            if self.replacement is not None
            else typed_redaction(self.id)
        )
        return self.pattern.sub(replacement, value)


INVISIBLE_TAG_CHARS_RE = re.compile(r"[\U000E0000-\U000E007F]")

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_\-.])(?:api[_\-.]?key|api[_\-.]?token|access[_\-.]?token|auth[_\-.]?token|ws[_\-.]?token|"
    r"rvt[_\-.]?token|token|secret|client[_\-.]?secret|password|passwd|pwd|jwt|sessionid|session|sid|"
    r"authorization|bearer|rediscli[_\-.]?auth|database[_\-.]?url|redis[_\-.]?url|mongo(?:db)?[_\-.]?(?:uri|url)|"
    r"connection[_\-.]?string|private[_\-.]?key)(?:$|[_\-.])"
)

SENSITIVE_QUERY_KEYWORDS = (
    "token",
    "key",
    "api_key",
    "apikey",
    "access_token",
    "secret",
    "password",
    "auth",
    "authorization",
    "bearer",
    "jwt",
    "session",
    "sessionid",
    "sid",
)

SECRET_FILE_BASENAME_ALLOWLIST = {
    ".env.example",
    ".env.sample",
    "env.example",
    "env.sample",
}
SECRET_FILE_SUFFIXES = (".env", ".secret", ".credentials", ".envrc")

URL_RE = re.compile(
    r"(?i)\b(?:https?|wss?|ftp|file|redis|rediss|mongodb(?:\+srv)?|postgres(?:ql)?|mysql|mariadb)://[^\s<>\")']+"
)


def sensitive_key_id(raw_key: str, prefix: str = "") -> str:
    key = re.sub(r"[^a-z0-9]+", "-", raw_key.lower()).strip("-")
    if not prefix:
        return key or "value"
    return f"{prefix}-{key or 'value'}"


SECRET_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        ("private key", "begin"),
    ),
    RedactionRule(
        "authorization-bearer",
        re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s\"']+"),
        ("authorization", "bearer"),
        lambda match: match.group(1) + typed_redaction("authorization-bearer"),
    ),
    RedactionRule(
        "openai-api-key",
        re.compile(
            r"(?i)\b(?:sk-ant-[A-Za-z0-9_-]+|sk-(?:proj|live|test)-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{20,})\b"
        ),
        ("sk-", "sk-ant"),
    ),
    RedactionRule(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA|A3T)[A-Z0-9]{16}\b"),
        ("AKIA", "ASIA", "A3T"),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "github-token",
        re.compile(
            r"\b(?:ghp_[0-9A-Za-z]{36}|gho_[0-9A-Za-z]{36}|(?:ghu|ghs)_[0-9A-Za-z]{36}|ghr_[0-9A-Za-z]{76}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}|gh[ps]_[A-Za-z0-9_]{20,})\b"
        ),
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    ),
    RedactionRule("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"), ("npm_",)),
    RedactionRule(
        "gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), ("glpat-",)
    ),
    RedactionRule(
        "bitbucket-token",
        re.compile(r"\bb(?:bpat|brat)-[A-Za-z0-9_-]{20,}\b"),
        ("bbpat-", "bbrat-"),
    ),
    RedactionRule(
        "huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), ("hf_",)
    ),
    RedactionRule(
        "slack-token",
        re.compile(r"\bx(?:ox[abprs]|app|wfp)-[A-Za-z0-9-]{10,}\b"),
        ("xox", "xapp-", "xwfp-"),
    ),
    RedactionRule(
        "slack-webhook",
        re.compile(
            r"https://hooks\.slack\.com/(?:services|triggers|workflows)/[A-Za-z0-9/_-]+"
        ),
        ("hooks.slack.com",),
    ),
    RedactionRule(
        "stripe-secret-key",
        re.compile(r"\bsk_(?:test|live)_[A-Za-z0-9]{16,}\b"),
        ("sk_test_", "sk_live_"),
    ),
    RedactionRule(
        "supabase-token", re.compile(r"\bsbp_[A-Za-z0-9_-]{20,}\b"), ("sbp_",)
    ),
    RedactionRule("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"), ("pypi-",)),
    RedactionRule(
        "cloudflare-token", re.compile(r"\bcfut_[A-Za-z0-9_-]{20,}\b"), ("cfut_",)
    ),
    RedactionRule("e2b-token", re.compile(r"\be2b_[A-Za-z0-9_-]{20,}\b"), ("e2b_",)),
    RedactionRule(
        "jieli-api-key", re.compile(r"\bjieli_[A-Za-z0-9_-]{20,}\b"), ("jieli_",)
    ),
    RedactionRule(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        ("AIza",),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "sendgrid-token",
        re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
        ("SG.",),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "linear-token", re.compile(r"\blin_api_[A-Za-z0-9_-]{20,}\b"), ("lin_api_",)
    ),
    RedactionRule(
        "postman-token",
        re.compile(r"\bPMAK-[A-Za-z0-9-]{20,}\b"),
        ("PMAK-",),
        case_insensitive_keywords=False,
    ),
    RedactionRule("pulumi-token", re.compile(r"\bpul-[A-Za-z0-9_-]{20,}\b"), ("pul-",)),
    RedactionRule(
        "databricks-token", re.compile(r"\bdapi[A-Za-z0-9]{20,}\b"), ("dapi",)
    ),
    RedactionRule(
        "grafana-token",
        re.compile(r"\beyJrIjoi[A-Za-z0-9_-]{20,}\b"),
        ("eyJrIjoi",),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "mapbox-token",
        re.compile(r"\bpk\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        ("pk.",),
    ),
    RedactionRule(
        "jwt-token",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        ("eyJ",),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "age-secret-key",
        re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]{20,}\b"),
        ("AGE-SECRET-KEY-1",),
        case_insensitive_keywords=False,
    ),
    RedactionRule(
        "sensitive-assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|REDISCLI_AUTH)[A-Z0-9_.-]*\s*(?:=|:=|:)\s*)([\"']?)(?!\[REDACTED:)([^\s,\"']+)([\"']?)"
        ),
        ("api", "key", "token", "secret", "password", "passwd", "pwd", "rediscli_auth"),
        lambda match: match.group(1)
        + match.group(2)
        + typed_redaction(sensitive_key_id(match.group(1)))
        + match.group(4),
    ),
    RedactionRule(
        "json-yaml-sensitive-key",
        re.compile(
            r"(?i)([\"']?[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|ws[_-]?token|rvt[_-]?token|token|secret|client[_-]?secret|password|passwd|pwd|jwt|sessionid|session|sid)[A-Za-z0-9_.-]*[\"']?\s*(?:=|:=|:)\s*)([\"']?)(?!\[REDACTED:)([^\s,\n\r\"'}\]]+)([\"']?)"
        ),
        ("api", "token", "secret", "password", "session", "jwt"),
        lambda match: match.group(1)
        + match.group(2)
        + typed_redaction(sensitive_key_id(match.group(1)))
        + match.group(4),
    ),
    RedactionRule(
        "redis-cli-password",
        re.compile(
            r"(?i)\b(redis-cli|redis-server)(\s+(?:-a|--pass|--requirepass)\s+)([^\s\"']+)"
        ),
        ("redis-cli", "redis-server", "--pass", "--requirepass"),
        lambda match: match.group(1)
        + match.group(2)
        + typed_redaction("redis-cli-password"),
    ),
    RedactionRule(
        "gcp-service-account",
        re.compile(r"(?i)([\"']type[\"']\s*:\s*[\"'])service_account([\"'])"),
        ("service_account",),
        lambda match: match.group(1)
        + typed_redaction("gcp-service-account")
        + match.group(2),
    ),
)


def strip_invisible_tag_chars(value: str) -> str:
    return INVISIBLE_TAG_CHARS_RE.sub("", value)


def redact_text(value: str) -> str:
    redacted = strip_invisible_tag_chars(value)
    redacted = redact_urls(redacted)
    for rule in SECRET_RULES:
        if rule.applies_to(redacted):
            redacted = rule.apply(redacted)
    return redacted


def redact_urls(value: str) -> str:
    return URL_RE.sub(lambda match: redact_url(match.group(0)), value)


def url_userinfo_suffix(parsed: SplitResult, netloc: str) -> str:
    fallback = netloc.split("@", 1)[1]
    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return fallback
    if port:
        host = f"{host}:{port}"
    if (
        parsed.hostname
        and parsed.hostname.startswith("[")
        and parsed.hostname.endswith("]")
    ):
        host = parsed.hostname
    return host or fallback


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        suffix = url_userinfo_suffix(parsed, netloc)
        netloc = f"{typed_redaction('url-userinfo')}@{suffix}"
    query = redact_query(parsed.query)
    return urlunsplit(SplitResult(parsed.scheme, netloc, parsed.path, query, ""))


def redact_query(query: str) -> str:
    if not query:
        return query
    parts = re.split(r"([&;])", query)
    redacted_parts: list[str] = []
    for part in parts:
        if part in {"&", ";"}:
            redacted_parts.append(part)
            continue
        key, separator, value = part.partition("=")
        if separator and is_sensitive_query_key(key):
            redacted_parts.append(
                f"{key}={typed_redaction(sensitive_key_id(key, 'url-query'))}"
            )
        else:
            redacted_parts.append(part)
    return "".join(redacted_parts)


def is_sensitive_query_key(key: str) -> bool:
    lowered = key.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return any(
        term in lowered or term.replace("_", "") in compact
        for term in SENSITIVE_QUERY_KEYWORDS
    )


def redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        if is_base64_payload(value):
            return value
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_json_key(key):
                redacted[key] = typed_redaction(sensitive_key_id(str(key)))
            elif is_image_content_key(value, key):
                redacted[key] = item
            else:
                redacted[key] = redact_json(item)
        return redacted
    return value


def is_sensitive_json_key(key: Any) -> bool:
    return isinstance(key, str) and SENSITIVE_KEY_RE.search(key) is not None


def is_base64_payload(value: dict[Any, Any]) -> bool:
    return value.get("type") in {"base64", "image"} and "data" in value


def is_image_content_key(container: dict[Any, Any], key: Any) -> bool:
    return (
        key == "content"
        and container.get("isImage") is True
        and isinstance(container.get("content"), str)
    )


def is_secret_file_path(path: str) -> bool:
    raw = path.strip()
    if not raw:
        return False
    name = PurePosixPath(raw).name
    if "\\" in raw:
        name = PureWindowsPath(raw).name
    lowered = name.lower()
    if lowered in SECRET_FILE_BASENAME_ALLOWLIST:
        return False
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered.startswith("env."):
        return True
    return lowered.endswith(SECRET_FILE_SUFFIXES)
