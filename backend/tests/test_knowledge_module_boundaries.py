from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"


def test_rag_flow_stays_out_of_core_backend_modules() -> None:
    core_files = [
        ROOT / "main.py",
        ROOT / "models.py",
        ROOT / "voice" / "agent.py",
        ROOT / "voice" / "router.py",
        ROOT / "voice" / "session.py",
        ROOT / "voice" / "tools.py",
    ]
    forbidden = ("app.knowledge", "knowledge_service", "KNOWLEDGE_SYSTEM_PROMPT")

    for path in core_files:
        content = path.read_text()
        assert not any(marker in content for marker in forbidden), path


def test_knowledge_addon_owns_its_integrations() -> None:
    knowledge = ROOT / "knowledge"
    expected = [
        knowledge / "addon.py",
        knowledge / "models.py",
        knowledge / "voice.py",
    ]
    assert all(path.is_file() for path in expected)
