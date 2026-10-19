from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app
from app.runtime import RuntimePolicy

ROOT = Path(__file__).resolve().parents[2]
PROXIES = {
    "container": ROOT / "frontend" / "nginx.conf",
    "host": ROOT / "deploy" / "nginx-talkstream.conf",
}


@pytest.fixture(scope="module")
def api_prefixes():
    # Every optional feature on, so a route an add-on mounts cannot be missed.
    app = create_app(
        Settings(
            _env_file=None, jwt_secret="x" * 32, knowledge_rag_enabled=True, openai_api_key="k"
        )
    )
    paths = {"/" + path.strip("/").split("/")[0] for path in app.openapi()["paths"]}
    return paths - {"/docs", "/openapi.json", "/redoc"} | {"/ws"}


def locations(conf: str) -> dict[str, str]:
    return dict(re.findall(r"location\s+([^{]+?)\s*\{([^}]*)\}", conf))


def proxied(conf: str, prefix: str) -> str | None:
    """The body of the proxy location that serves the prefix, if any."""
    for location, body in locations(conf).items():
        if "proxy_pass" not in body:
            continue
        if location.startswith("~"):
            if re.search(location.lstrip("~*").strip(), prefix):
                return body
        elif prefix.startswith(location.removeprefix("=").strip().rstrip("/") or "\0"):
            return body
    return None


@pytest.mark.parametrize("proxy", PROXIES)
def test_proxy_routes_include_optional_features(api_prefixes, proxy):
    conf = PROXIES[proxy].read_text()
    assert {"/capabilities", "/knowledge", "/auth", "/ws", "/operations"} <= api_prefixes
    assert [prefix for prefix in sorted(api_prefixes) if proxied(conf, prefix) is None] == []


def test_dev_server_proxies_every_api_prefix(api_prefixes):
    # Vite answers unforwarded paths with the app page, so a gap breaks only local dev.
    forwarded = set(
        re.findall(r'"(/[\w.-]+)":', (ROOT / "frontend" / "vite.config.ts").read_text())
    )
    assert sorted(api_prefixes - forwarded) == []


@pytest.mark.parametrize("proxy", PROXIES)
def test_proxy_admits_the_largest_accepted_upload(proxy):
    body = proxied(PROXIES[proxy].read_text(), "/knowledge")
    limit = re.search(r"client_max_body_size\s+(\d+)m;", body or "")
    assert limit and int(limit.group(1)) * 1024 * 1024 > RuntimePolicy().upload_bytes


@pytest.mark.parametrize("profile", ["owner_hosted", "limited_vps"])
def test_public_policy_is_bounded(profile):
    policy = RuntimePolicy(profile=profile)
    assert Settings(_env_file=None, jwt_secret="x" * 32).backend_workers == 1
    assert policy.global_active_calls == 2
    monthly = policy.hosting_month_micro_inr + policy.variable_month_micro_inr
    assert monthly + policy.reserve_micro_inr <= 2_000 * 1_000_000


def test_deployed_policy_file_matches_the_compose_profile():
    policy = RuntimePolicy.model_validate_json(
        (ROOT / "deploy" / "config" / "runtime-policy.json").read_text()
    )
    compose = (ROOT / "compose.yaml").read_text()
    assert f"RUNTIME_PROFILE: ${{TALKSTREAM_RUNTIME_PROFILE:-{policy.profile}}}" in compose
