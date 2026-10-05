"""Product-owned workspace configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

WORKSPACE_CONFIG_FILE = "workspace.json"


@dataclass(frozen=True)
class TrustedGatewayConfiguration:
    """Authenticated gateway identity allowed to add a system instruction."""

    runtime: str
    project_id: str


@dataclass(frozen=True)
class WorkspaceConfiguration:
    """Configuration that travels with a product workspace."""

    trusted_gateway: TrustedGatewayConfiguration | None = None
    reference_files: tuple[str, ...] = ()

    @classmethod
    def load(cls, workspace: Path) -> WorkspaceConfiguration:
        path = workspace / WORKSPACE_CONFIG_FILE
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
        if data.get("schema_version") != 1:
            raise ValueError(f"{path} schema_version must be 1")
        references = data.get("reference_files", [])
        if not isinstance(references, list) or len(references) > 4:
            raise ValueError("reference_files must be a list of at most four Markdown paths")
        for reference in references:
            if (not isinstance(reference, str) or len(reference) > 240
                or "\\" in reference or not reference.endswith(".md")
                or any(ord(character) < 32 or ord(character) == 127 for character in reference)
                or PurePosixPath(reference).is_absolute()
                or any(not part or part.startswith(".") for part in reference.split("/"))):
                raise ValueError("reference_files must contain relative non-hidden Markdown paths")
        if len(set(references)) != len(references):
            raise ValueError("reference_files must not contain duplicates")
        trusted_gateway = data.get("trusted_gateway")
        parsed_gateway = None
        if trusted_gateway is not None:
            if not isinstance(trusted_gateway, dict):
                raise ValueError(f"{path} trusted_gateway must be an object")
            runtime = trusted_gateway.get("runtime")
            project_id = trusted_gateway.get("project_id")
            if not isinstance(runtime, str) or not runtime.strip():
                raise ValueError(f"{path} trusted_gateway.runtime must be a name")
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError(f"{path} trusted_gateway.project_id must be a name")
            parsed_gateway = TrustedGatewayConfiguration(
                runtime=runtime.strip(),
                project_id=project_id.strip(),
            )
        return cls(
            trusted_gateway=parsed_gateway,
            reference_files=tuple(references),
        )
