from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.database import configure, dispose, get_session, init_db
from portal.models import (
    DBBooth,
    DeveloperAccount,
    Event,
    EventMembership,
    OAuthClient,
    OAuthToken,
    Room,
    RoomMembership,
    User,
)


@pytest.fixture
async def rooms():
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as db:
        user = User(email="organizer@example.test", display_name="Organizer")
        own = Event(slug="owned", display_name="Owned")
        other = Event(slug="foreign", display_name="Foreign")
        db.add_all([user, own, other])
        await db.flush()
        developer = DeveloperAccount(user_id=user.id, status="approved")
        own_room = Room(event_id=own.id, display_name="Owned room")
        foreign_room = Room(event_id=other.id, display_name="Foreign room")
        db.add_all([developer, own_room, foreign_room])
        await db.flush()
        for event, room in [(own, own_room), (other, foreign_room)]:
            db.add(DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"))
        values = (user.id, own.id, other.id, developer.id, own_room.id, foreign_room.id)
    yield values
    await dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["owner", "coordinator", "confidential"])
@pytest.mark.parametrize("operation", ["list", "get", "create", "delete", "export"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_room_endpoints_enforce_event_scope(rooms, role, operation, foreign):
    user_id, event_id, foreign_event_id, developer_id, own_room, foreign_room = rooms
    async with get_session() as db:
        client = OAuthClient(
            developer_account_id=developer_id,
            client_id="scope-client",
            name="Test client",
            is_confidential=role == "confidential",
        )
        db.add(client)
        await db.flush()
        if role == "owner":
            db.add(EventMembership(user_id=user_id, event_id=event_id, role="event_owner"))
        if role == "coordinator":
            db.add(RoomMembership(user_id=user_id, room_id=own_room, role="room_coordinator"))
            db.add(RoomMembership(user_id=user_id, room_id=foreign_room, role="room_coordinator"))
        db.add(
            OAuthToken(
                client_id=client.id,
                user_id=user_id,
                event_id=event_id,
                scopes=["booths:read", "booths:write", "transcripts:read"],
                access_token_hash=hashlib.sha256(b"scope-token").hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    room_id = foreign_room if foreign else own_room
    path = f"/api/v1/events/owned/rooms/{room_id}/booths"
    method = "GET"
    if operation != "list":
        path += "/fr" if operation == "create" and foreign else "/en"
    if operation == "export":
        path += "/transcripts/export"
    if operation == "create":
        method = "POST"
    if operation == "delete":
        method = "DELETE"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.request(method, path, headers={"Authorization": "Bearer scope-token"})
    assert response.status_code == (404 if foreign else 409 if operation == "create" else 200), response.text
    if foreign:
        async with get_session() as db:
            booths = (await db.scalars(select(DBBooth).where(DBBooth.room_id == foreign_room))).all()
            assert [(b.event_id, b.language_code) for b in booths] == [(foreign_event_id, "en")]
