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


MODEL_PROFILES: dict[str, ModelProfile] = {
    "deepseek-chat": ModelProfile(
        id="deepseek-chat",
        provider="deepseek",
        model="deepseek-v4-flash",
    ),
}

_GLM_5_3_FLASH_ROUTES = {
    "openrouter": "openrouter/z-ai/glm-5.3-flash",
    "tokenhub": "tokenhub/glm-5.3-flash",
    "zhipu": "glm-5.3-flash",
}


def resolve_model_profile(profile_id: str) -> ModelProfile:
    """Resolve a trusted gateway profile without accepting raw model IDs."""
    if profile_id == "glm-5.3-flash":
        provider = os.environ.get("GLM_5_3_FLASH_PROVIDER", "zhipu").strip().lower()
        try:
            model = _GLM_5_3_FLASH_ROUTES[provider]
        except KeyError as error:
            raise ValueError(
                f"Unsupported GLM-5.3-Flash provider: {provider!r}"
            ) from error
        return ModelProfile(id=profile_id, provider=provider, model=model)
    try:
        return MODEL_PROFILES[profile_id]
    except KeyError as error:
        raise ValueError(f"Unsupported Analyst Runtime model profile: {profile_id!r}") from error
