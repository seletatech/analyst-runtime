"""Product-facing model profiles owned by Analyst Runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    """Resolve one stable product choice to its runtime provider and model."""

    id: str
    provider: str
    model: str


_DEEPSEEK_V4_FLASH_ROUTES = {
    "deepseek": "deepseek-v4-flash",
    "nebius": "deepseek-ai/DeepSeek-V4-Flash-0731",
    "nvidia": "deepseek-ai/deepseek-v4-flash-0731",
}

_GLM_5_3_FLASH_ROUTES = {
    "openrouter": "openrouter/z-ai/glm-5.3-flash",
    "tokenhub": "tokenhub/glm-5.3-flash",
    "zhipu": "glm-5.3-flash",
}

DEEPSEEK_V4_FLASH_PROFILE_ID = "deepseek-v4-flash-0731"
_LEGACY_DEEPSEEK_PROFILE_IDS = {"deepseek-chat"}


def resolve_model_profile(profile_id: str) -> ModelProfile:
    """Resolve a trusted gateway profile without accepting raw model IDs."""
    if profile_id == DEEPSEEK_V4_FLASH_PROFILE_ID or profile_id in _LEGACY_DEEPSEEK_PROFILE_IDS:
        provider = os.environ.get("DEEPSEEK_V4_FLASH_PROVIDER", "nebius").strip().lower()
        try:
            model = _DEEPSEEK_V4_FLASH_ROUTES[provider]
        except KeyError as error:
            raise ValueError(f"Unsupported DeepSeek-V4-Flash provider: {provider!r}") from error
        return ModelProfile(id=DEEPSEEK_V4_FLASH_PROFILE_ID, provider=provider, model=model)
    if profile_id == "glm-5.3-flash":
        provider = os.environ.get("GLM_5_3_FLASH_PROVIDER", "openrouter").strip().lower()
        try:
            model = _GLM_5_3_FLASH_ROUTES[provider]
        except KeyError as error:
            raise ValueError(f"Unsupported GLM-5.3-Flash provider: {provider!r}") from error
        return ModelProfile(id=profile_id, provider=provider, model=model)
    raise ValueError(f"Unsupported Analyst Runtime model profile: {profile_id!r}")
