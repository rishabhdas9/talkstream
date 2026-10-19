from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_compose_keeps_backend_and_qdrant_private(tmp_path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")

    environment = os.environ.copy()
    environment.update(
        {
            "TALKSTREAM_PUBLIC_ORIGIN": "https://talkstream.example",
            "TALKSTREAM_ADMIN_EMAILS": "owner@example.com",
            "TALKSTREAM_RESEND_FROM": "Talkstream <login@talkstream.example>",
            "TALKSTREAM_RECOVERY_DIR": str(tmp_path),
        }
    )
    completed = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )
    compose = json.loads(completed.stdout)
    services = compose["services"]

    assert set(services) == {"backend", "frontend", "qdrant"}
    assert services["backend"].get("ports") is None
    assert services["qdrant"].get("ports") is None
    assert services["frontend"]["ports"]
    assert services["backend"]["environment"]["KNOWLEDGE_RAG_ENABLED"] == "false"
    assert services["backend"]["environment"]["QDRANT_URL"] == "http://qdrant:6333"
    assert set(compose["volumes"]) == {"app_data", "qdrant_data"}
    backend = services["backend"]["environment"]
    assert backend["PUBLIC_ORIGIN"] == backend["CORS_ORIGINS"] == "https://talkstream.example"
    # Keys arrive as secret files, never as environment values visible to `docker inspect`.
    assert not [name for name in backend if name.endswith("_API_KEY")]
    assert {"SARVAM_API_KEY_FILE", "EXA_API_KEY_FILE", "RESEND_API_KEY_FILE"} <= set(backend)


def test_container_build_and_proxy_files_exist() -> None:
    expected = [
        ROOT / "backend" / "Dockerfile",
        ROOT / "frontend" / "Dockerfile",
        ROOT / "frontend" / "nginx.conf",
        ROOT / ".dockerignore",
        ROOT / "backend" / ".dockerignore",
        ROOT / "frontend" / ".dockerignore",
    ]
    assert all(path.is_file() for path in expected)

    nginx = (ROOT / "frontend" / "nginx.conf").read_text()
    assert "proxy_pass http://backend:8000" in nginx
    assert "proxy_set_header Upgrade $http_upgrade" in nginx
    assert "location /knowledge" in nginx
