from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = (
    ROOT
    / "analyst_runtime"
    / "workspace"
    / "skills"
    / "linghui-manufacturing-data-analyst"
    / "scripts"
    / "fetch_monthly_reconciliation.py"
)

pytestmark = pytest.mark.skipif(
    not HELPER.is_file(),
    reason="optional workspace skills are not tracked in the minimal checkout",
)


class _SealedHandler(BaseHTTPRequestHandler):
    artifact_sha = "925eff63c622" + ("0" * 52)
    payload = {
        "artifact_id": "monthly-event-reconciliation:2025-07:925eff63c622",
        "artifact_sha256": artifact_sha,
        "release_gate": {
            "passed": True,
            "checks": [
                {
                    "name": name,
                    "passed": True,
                    "expected_sha256": "a" * 64,
                    "actual_sha256": "a" * 64,
                }
                for name in (
                    "duckdb_sha256",
                    "projection_manifest_sha256",
                    "ocr_release_manifest_sha256",
                    "spreadsheet_release_manifest_sha256",
                )
            ],
        },
        "result": {
            "metric_contract": {"event_month": "2025-07"},
            "review_packet": [{"observed": "10"}],
        },
    }
    requested_path = ""

    def do_GET(self) -> None:  # noqa: N802
        type(self).requested_path = self.path
        body = json.dumps(self.payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def test_helper_emits_one_complete_release_gated_json_result() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SealedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--month",
                "2025-07",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 0
    assert result.stderr == ""
    expected = json.dumps(_SealedHandler.payload, separators=(",", ":")) + "\n"
    assert result.stdout == expected
    assert json.loads(result.stdout) == _SealedHandler.payload
    assert _SealedHandler.requested_path == (
        "/api/analysis/monthly-event-reconciliation?month=2025-07"
    )


def test_helper_rejects_invalid_month_before_network_access() -> None:
    result = subprocess.run(
        [sys.executable, str(HELPER), "--month", "July"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "month must use YYYY-MM" in result.stderr


def test_helper_rejects_artifact_without_passed_release_gate() -> None:
    original_payload = _SealedHandler.payload
    _SealedHandler.payload = {
        "artifact_id": "monthly-event-reconciliation:2025-07:925eff63c622",
        "artifact_sha256": "925eff63c622" + ("0" * 52),
        "release_gate": {"passed": False},
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SealedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--month",
                "2025-07",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        _SealedHandler.payload = original_payload
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "release gate did not pass" in result.stderr


def test_helper_rejects_artifact_for_a_different_month() -> None:
    original_payload = _SealedHandler.payload
    _SealedHandler.payload = {
        **original_payload,
        "artifact_id": "monthly-event-reconciliation:2024-01:925eff63c622",
        "result": {
            **original_payload["result"],
            "metric_contract": {"event_month": "2024-01"},
        },
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SealedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--month",
                "2025-07",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        _SealedHandler.payload = original_payload
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "artifact identity does not match request" in result.stderr


def test_helper_rejects_incomplete_release_checks() -> None:
    original_payload = _SealedHandler.payload
    _SealedHandler.payload = {
        **original_payload,
        "release_gate": {
            "passed": True,
            "checks": original_payload["release_gate"]["checks"][:-1],
        },
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SealedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--month",
                "2025-07",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        _SealedHandler.payload = original_payload
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "release checks are incomplete" in result.stderr
