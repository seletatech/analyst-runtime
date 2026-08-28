from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_mvp_image_uses_an_immutable_minimal_runtime_base() -> None:
    dockerfile = (ROOT / "Dockerfile.mvp").read_text(encoding="utf-8")

    assert re.search(
        r"^ARG ANALYST_RUNTIME_BASE_IMAGE="
        r"ghcr\.io/astral-sh/uv:python3\.12-alpine@sha256:[0-9a-f]{64}$",
        dockerfile,
        re.MULTILINE,
    )
    assert "python3.12-bookworm-slim" not in dockerfile


def test_mvp_image_keeps_lock_and_hash_verification() -> None:
    dockerfile = (ROOT / "Dockerfile.mvp").read_text(encoding="utf-8")

    for required in (
        "COPY analyst-runtime/pyproject.toml analyst-runtime/uv.lock",
        "uv export",
        "--locked",
        "--require-hashes",
        "--offline",
        "--no-index",
    ):
        assert required in dockerfile
