"""Fail-closed Linux child-process boundary for account-scoped exec."""

import json
import os
import runpy
import sys
from pathlib import Path


def main():
    # -I and direct standard-library loading avoid account-controlled startup hooks
    # and importing the agent/provider stack before the kernel boundary is active.
    tools = Path(__file__).resolve().parent
    scope_type = runpy.run_path(str(tools / "account_access.py"))["AccountWorkspace"]
    isolate = runpy.run_path(str(tools / "document_isolation.py"))["isolate_office"]
    snapshot = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    scope = scope_type(Path(sys.argv[1]), sys.argv[2], snapshot.get("run_id"), snapshot.get("manifest_sha256"))
    public = [Path(p).resolve() for p in ["/usr", "/lib", "/lib64", "/bin", "/etc/fonts", sys.prefix]]
    if any(scope.workspace.is_relative_to(path) for path in public):
        raise PermissionError("Account workspace overlaps public runtime libraries")
    readonly = [p for p in [*scope.shared, scope.uploads, Path(sys.prefix)] if p.exists()]
    isolate(str(scope.private), tuple(str(p) for p in readonly), local_sockets=False)
    os.execv("/bin/sh", ["/bin/sh", "-c", sys.argv[3]])


if __name__ == "__main__":
    main()
