from __future__ import annotations

from fastapi import Depends

import app.auth.dependencies as dependencies


async def login(client, sender, email):
    assert (await client.post("/auth/request-otp", json={"email": email})).status_code == 202
    response = await client.post(
        "/auth/verify-otp", json={"email": email, "code": sender.deliveries[-1]["code"]}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def protect_route(api_client):
    client, sender = api_client
    app = client._transport.app
    guard = getattr(dependencies, "get_administrator", None)
    assert guard is not None, "No server-side administrator authorization"
    administrator = Depends(guard)

    @app.get("/test-admin")
    async def protected_operation(user=administrator):
        return {"id": user.id}

    app.state.settings.admin_emails = "OWNER@example.com"
    return client, sender, app


async def test_only_configured_verified_identity_is_administrator(api_client):
    client, sender, _ = protect_route(api_client)
    owner = await login(client, sender, "owner@example.com")
    regular = await login(client, sender, "person@example.com")
    assert (
        await client.get("/test-admin", headers={"Authorization": f"Bearer {owner}"})
    ).status_code == 200
    assert (
        await client.get("/test-admin", headers={"Authorization": f"Bearer {regular}"})
    ).status_code == 403
    assert (await client.get("/test-admin")).status_code == 401


async def test_signed_role_claim_cannot_grant_administrator_access(api_client):
    import jwt

    client, sender, app = protect_route(api_client)
    ordinary = await login(client, sender, "person@example.com")
    settings = app.state.settings
    claims = jwt.decode(ordinary, options={"verify_signature": False})
    claims["role"] = "admin"
    claims["is_admin"] = True
    forged = jwt.encode(claims, settings.jwt_secret_value, algorithm=settings.jwt_algorithm)
    assert (
        await client.get("/test-admin", headers={"Authorization": f"Bearer {forged}"})
    ).status_code == 403


async def test_administrator_is_rechecked_against_database(api_client):
    from sqlalchemy import text

    client, sender, app = protect_route(api_client)
    token = await login(client, sender, "owner@example.com")
    async with app.state.database.session_factory() as session:
        await session.execute(text("UPDATE users SET is_verified=0"))
        await session.commit()
    assert (
        await client.get("/test-admin", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 401
