"""Product-owned workspace configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
        )
