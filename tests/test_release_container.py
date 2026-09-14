import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_litellm_stays_on_the_security_fixed_release_line() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert "litellm>=1.84.0,<1.85.0" in dependencies

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    litellm = next(package for package in lock["package"] if package["name"] == "litellm")
    version = tuple(int(part) for part in litellm["version"].split("."))
    assert (1, 84, 0) <= version < (1, 85, 0)


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
        "COPY pyproject.toml uv.lock",
        "uv export",
        "--locked",
        "--require-hashes",
        "--offline",
        "--no-index",
    ):
        assert required in dockerfile


def test_default_images_build_from_a_standalone_clone() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    mvp_dockerfile = (ROOT / "Dockerfile.mvp").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "COPY analyst-runtime/" not in dockerfile + mvp_dockerfile
    assert "COPY workspace/" not in dockerfile + mvp_dockerfile
    assert "context: ." in compose
