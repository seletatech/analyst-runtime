"""Product-facing model profiles owned by Analyst Runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    """Resolve one stable product choice to its runtime provider and model."""

    id: str
    provider: str
    model: str


MODEL_PROFILES: dict[str, ModelProfile] = {
    "glm-5.3-flash": ModelProfile(
        id="glm-5.3-flash",
        provider="zhipu",
        model="glm-5.3-flash",
    ),
    "deepseek-chat": ModelProfile(
        id="deepseek-chat",
        provider="deepseek",
        model="deepseek-v4-flash",
    ),
}


def resolve_model_profile(profile_id: str) -> ModelProfile:
    """Resolve a trusted gateway profile without accepting raw model IDs."""
    try:
        return MODEL_PROFILES[profile_id]
    except KeyError as error:
        raise ValueError(f"Unsupported Analyst Runtime model profile: {profile_id!r}") from error
