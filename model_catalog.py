"""Model list normalization helpers for GitLab Duo."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


Model = dict[str, Any]

_PROVIDER_SUFFIXES = ("vertex", "bedrock")
_DATE_TOKEN_RE = re.compile(r"^\d{8}$")


def _clean_display_name(name: str) -> str:
    return name.split(" - ", 1)[0].strip() or name.strip()


def _public_id_from_name(name: str, ref: str) -> str:
    base = _clean_display_name(name).lower()
    base = re.sub(r"[^a-z0-9.]+", "-", base).strip("-")
    suffix = _provider_suffix(ref)
    if suffix and base.startswith("claude-"):
        return f"{base}-{suffix}"
    return base


def _provider_suffix(ref: str) -> str:
    for suffix in _PROVIDER_SUFFIXES:
        if ref.endswith(f"_{suffix}"):
            return suffix
    return ""


def _strip_ref_version(ref: str, *, keep_provider: bool) -> str:
    tokens = ref.split("_")
    provider = tokens[-1] if tokens and tokens[-1] in _PROVIDER_SUFFIXES else ""
    core = tokens[:-1] if provider else tokens
    core = [token for token in core if not _DATE_TOKEN_RE.match(token)]
    if keep_provider and provider:
        core.append(provider)
    return "_".join(core)


def _snake_public_alias(public_id: str) -> str:
    return public_id.replace("-", "_").replace(".", "_")


def _owned_by(provider: str, ref: str) -> str:
    lowered = provider.lower()
    if "openai" in lowered or ref.startswith("gpt_"):
        return "openai"
    if "anthropic" in lowered or ref.startswith("claude_") and not _provider_suffix(ref):
        return "anthropic"
    if "bedrock" in lowered or ref.endswith("_bedrock"):
        return "bedrock"
    if "gemini" in lowered or "vertex" in lowered or ref.endswith("_vertex"):
        return "google"
    return lowered.replace(" ", "_") or "gitlab"


def _aliases(public_id: str, ref: str) -> list[str]:
    values = {
        public_id,
        ref,
        _snake_public_alias(public_id),
        _strip_ref_version(ref, keep_provider=True),
        _strip_ref_version(ref, keep_provider=False),
    }
    return sorted(value for value in values if value)


def normalize_graphql_models(result: Mapping[str, Any]) -> list[Model]:
    default_ref = ""
    default = result.get("defaultModel")
    if isinstance(default, Mapping):
        default_ref = str(default.get("ref", "")).strip()

    models: list[Model] = []
    seen_ids: set[str] = set()
    selectable = result.get("selectableModels", [])
    if not isinstance(selectable, Sequence):
        return models

    for raw in selectable:
        if not isinstance(raw, Mapping):
            continue
        ref = str(raw.get("ref", "")).strip()
        if not ref:
            continue
        provider = str(raw.get("modelProvider", "")).strip()
        name = _clean_display_name(str(raw.get("name", ref)).strip())
        public_id = _public_id_from_name(name, ref)
        if not public_id or public_id in seen_ids:
            continue
        seen_ids.add(public_id)
        models.append({
            "id": public_id,
            "gitlab_id": ref,
            "name": name,
            "owned_by": _owned_by(provider, ref),
            "model_provider": provider,
            "model_description": str(raw.get("modelDescription", "") or "").strip(),
            "cost_indicator": str(raw.get("costIndicator", "") or "").strip(),
            "aliases": _aliases(public_id, ref),
            "is_default": ref == default_ref,
        })
    return models


def resolve_model_id(model_id: str, models: Sequence[Mapping[str, Any]]) -> str:
    if not model_id:
        return model_id
    for model in models:
        aliases = model.get("aliases", [])
        values = {model.get("id"), model.get("gitlab_id")}
        if isinstance(aliases, Sequence) and not isinstance(aliases, str):
            values.update(aliases)
        if model_id in values:
            return str(model.get("gitlab_id") or model_id)
    return model_id
