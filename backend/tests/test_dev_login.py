from __future__ import annotations


def enable(client, **update):
    app = client._transport.app
    app.state.settings = app.state.settings.model_copy(update={"dev_login": True, **update})


async def test_dev_login_is_off_unless_explicitly_enabled(api_client):
    client, _ = api_client
    assert (await client.get("/capabilities")).json()["dev_login"] is False
    assert (await client.post("/auth/dev-login")).status_code == 404


async def test_dev_login_signs_in_one_local_user_without_email(api_client):
    client, sender = api_client
    enable(client)
    ids = []
    for _ in range(2):
        response = await client.post("/auth/dev-login")
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        me = (await client.get("/auth/me", headers=headers)).json()
        assert me["email"] == "user@example.com" and me["is_verified"] is True
        ids.append(me["id"])
    assert ids[0] == ids[1] and sender.deliveries == []


async def test_dev_login_is_absent_outside_local_environments(api_client):
    client, _ = api_client
    enable(client, app_env="production")
    assert (await client.post("/auth/dev-login")).status_code == 404


async def test_dev_login_refuses_remote_peers_even_with_forwarded_headers(api_client):
    client, _ = api_client
    enable(client)
    client._transport.client = ("203.0.113.9", 4000)
    forwarded = {"X-Forwarded-For": "127.0.0.1"}
    assert (await client.post("/auth/dev-login", headers=forwarded)).status_code == 404


def test_dev_login_never_wires_real_email_even_with_a_resend_key(tmp_path):
    from fastapi.testclient import TestClient

    from app.auth.email import ConsoleOtpSender, ResendOtpSender
    from app.config import Settings
    from app.main import create_app

    def sender_for(dev_login: bool):
        settings = Settings(
            _env_file=None,
            jwt_secret="test-secret-that-is-at-least-thirty-two-characters",
            database_url=f"sqlite+aiosqlite:///{tmp_path / f'{dev_login}.db'}",
            resend_api_key="re_test_key_never_used_for_a_real_request",
            dev_login=dev_login,
        )
        app = create_app(settings)
        with TestClient(app):
            return type(app.state.email_sender)

    assert sender_for(True) is ConsoleOtpSender
    assert sender_for(False) is ResendOtpSender
