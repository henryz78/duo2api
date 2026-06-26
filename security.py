"""Security and reliability helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


AUTH_CACHE_TTL_SECONDS = 5.0
_auth_cache: tuple[float, Path, tuple[str, ...]] | None = None


def redact_secret(value: str | None) -> str:
    text = (value or "").strip()
    if not text or text == "需填":
        return ""
    if len(text) <= 8:
        return "********"
    return f"{text[:4]}...{text[-4:]}"


def looks_like_redacted_secret(value: str | None) -> bool:
    text = (value or "").strip()
    return text == "********" or (len(text) == 11 and text[4:7] == "...")


def _config_cookies(cfg: dict[str, Any]) -> dict[str, Any]:
    gitlab = cfg.get("gitlab", {})
    if not isinstance(gitlab, dict):
        return {}
    cookies = gitlab.get("cookies")
    if isinstance(cookies, dict):
        return cookies
    session = str(gitlab.get("session") or "").strip()
    remember = str(gitlab.get("remember_user_token") or "").strip()
    return {"_gitlab_session": session, "remember_user_token": remember}


def _config_api_keys(cfg: dict[str, Any]) -> list[Any]:
    server = cfg.get("server", {})
    if isinstance(server, dict) and isinstance(server.get("api_keys"), list):
        return server["api_keys"]
    legacy = cfg.get("api_keys")
    return legacy if isinstance(legacy, list) else []


def public_config_status(cfg: dict[str, Any], available_models: int) -> dict[str, Any]:
    gitlab = cfg.get("gitlab", {})
    cookies = _config_cookies(cfg)
    api_keys = _config_api_keys(cfg)
    return {
        "namespace_id": gitlab.get("namespace_id", ""),
        "model": gitlab.get("model", ""),
        "has_session_cookie": bool(str(cookies.get("_gitlab_session", "")).strip()),
        "has_remember_token": bool(str(cookies.get("remember_user_token", "")).strip()),
        "api_keys_count": len([key for key in api_keys if key]),
        "available_models": available_models,
    }


def apply_config_update(
    cfg: dict[str, Any],
    *,
    gitlab_session: str = "",
    remember_token: str = "",
    namespace_id: str | None = None,
    model: str | None = None,
    api_keys: list[str] | None = None,
) -> None:
    gitlab = cfg.setdefault("gitlab", {})
    cookies = gitlab.setdefault("cookies", {})
    server = cfg.setdefault("server", {})

    if looks_like_redacted_secret(gitlab_session) or looks_like_redacted_secret(remember_token):
        raise ValueError("Submit full cookie values or leave cookie fields empty.")
    if any(looks_like_redacted_secret(key) for key in (api_keys or [])):
        raise ValueError("Submit full API keys or leave the API Keys field empty.")

    if gitlab_session:
        cookies["_gitlab_session"] = gitlab_session
    if remember_token:
        cookies["remember_user_token"] = remember_token
    if namespace_id:
        gitlab["namespace_id"] = namespace_id
    if model is not None:
        gitlab["model"] = model

    cleaned_api_keys = [key.strip() for key in (api_keys or []) if key.strip()]
    if cleaned_api_keys:
        server["api_keys"] = cleaned_api_keys


def estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4)


def public_upstream_error_message(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "csrf token" in lowered or "cookies still valid" in lowered:
        return "GitLab authentication failed. Update the GitLab cookies in config."
    if "create workflow" in lowered:
        return "GitLab Duo upstream request failed while creating a chat workflow."
    if "workflow failed" in lowered:
        return "GitLab Duo upstream workflow failed."
    if "server error" in lowered:
        return "GitLab Duo upstream returned an error."
    return "GitLab Duo upstream request failed."


def auth_keys_from_config(config_path: Path, *, now: float | None = None) -> set[str]:
    global _auth_cache
    current = time.monotonic() if now is None else now
    resolved = config_path.resolve()
    if _auth_cache is not None:
        cached_at, cached_path, cached_keys = _auth_cache
        if cached_path == resolved and current - cached_at < AUTH_CACHE_TTL_SECONDS:
            return set(cached_keys)

    with open(resolved) as f:
        cfg: dict[str, Any] = json.load(f)
    keys = tuple(str(k) for k in _config_api_keys(cfg) if k)
    _auth_cache = (current, resolved, keys)
    return set(keys)


def clear_auth_cache() -> None:
    global _auth_cache
    _auth_cache = None
