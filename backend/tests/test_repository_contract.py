from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_application_exposes_talkstream_identity(monkeypatch) -> None:
    assert (ROOT / "backend/app/main.py").is_file(), "Application is not imported"
    monkeypatch.setenv("JWT_SECRET", "isolated-test-secret-with-at-least-32-characters")
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(_env_file=None))
    assert app.openapi()["info"]["title"] == "Talkstream API"
    assert {"/auth/request-otp", "/wallet", "/capabilities"} <= set(app.openapi()["paths"])


def test_packages_have_standalone_names() -> None:
    backend = ROOT / "backend/pyproject.toml"
    frontend = ROOT / "frontend/package.json"
    assert backend.is_file() and frontend.is_file(), "Package manifests are not imported"
    assert tomllib.loads(backend.read_text())["project"]["name"] == "talkstream-backend"
    assert json.loads(frontend.read_text())["name"] == "talkstream-frontend"


def test_ci_runs_frontend_regression_tests() -> None:
    workflow = ROOT / ".github/workflows/ci.yml"
    assert workflow.is_file(), "Application CI is not imported"
    assert "- run: npm test" in workflow.read_text()


def test_uncleared_spoken_cue_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "isolated-test-secret-with-at-least-32-characters")
    monkeypatch.delenv("RESPONSE_CUES_ENABLED", raising=False)
    from app.config import Settings

    assert Settings(_env_file=None).response_cues_enabled is False
