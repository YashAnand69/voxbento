from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi_app import app
from portal.auth import create_listener_token, create_participant_token, create_user_token
from portal.config import settings
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room
from portal.websockets.manager import tts_manager


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "jwt_secret", "test-only-secret-with-at-least-32-bytes")
    configure("sqlite+aiosqlite://")
    anyio.run(init_db)

    async def seed():
        async with get_session() as db:
            event = Event(slug="conference", display_name="Conference", listener_join_code="JOIN42")
            db.add(event)
            await db.flush()
            db.add(Room(id=1, event_id=event.id, display_name="Main"))

    anyio.run(seed)
    yield
    anyio.run(dispose)


def connect(client, *, booth="conference-1-en", room=1, token=None, headers=None):
    path = f"/ws/tts/{room}/fr/{booth}"
    if token:
        path += f"?token={token}"
    return client.websocket_connect(path, headers=headers or {})


@pytest.mark.parametrize("access_token", ["", "configured"])
@pytest.mark.parametrize(
    "credential",
    [
        "missing",
        "invalid",
        "other-event",
        "prefix-event",
        "wrong-room",
        "wrong-language",
        "wrong-cookie",
        "invalid-join-code",
    ],
)
def test_tts_rejects_unauthorized_listener(monkeypatch, access_token, credential):
    monkeypatch.setattr(settings, "booth_access_token", access_token)
    token = None
    room = 1
    booth = "conference-1-en"
    if credential == "invalid":
        token = "invalid-token"
    elif credential == "other-event":
        token = create_listener_token(event_slug="other")
    elif credential == "prefix-event":
        token = create_listener_token(event_slug="conference")
        booth = "conference-private-1-en"
    elif credential == "wrong-room":
        token = create_listener_token(event_slug="conference")
        room = 2
    elif credential == "wrong-language":
        token = create_participant_token(
            booth_id=1, role="interpreter", event_slug="conference", room_id=1, language_code="de"
        )
    client = TestClient(app)
    if credential == "wrong-cookie":
        client.cookies.set("session_token", create_listener_token(event_slug="other"))
    elif credential == "invalid-join-code":
        client.cookies.set("listener_code_conference", "WRONG")
    with pytest.raises(WebSocketDisconnect):
        with connect(client, token=token, booth=booth, room=room):
            pass
    assert not tts_manager.has_listeners(room, "fr", booth)


@pytest.mark.parametrize("credential", ["listener-token", "listener-cookie", "join-code", "user", "admin"])
@pytest.mark.parametrize("source_language", ["en", "floor"])
@pytest.mark.parametrize("access_token", ["", "configured"])
def test_tts_allows_authorized_translated_audio(credential, source_language, access_token, monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", access_token)
    booth = f"conference-1-{source_language}"
    client = TestClient(app)
    token = None
    if credential == "listener-token":
        token = create_listener_token(event_slug="conference")
    elif credential == "listener-cookie":
        client.cookies.set("session_token", create_listener_token(event_slug="conference"))
    elif credential == "join-code":
        client.cookies.set("listener_code_conference", "JOIN42")
    else:
        client.cookies.set(
            "user_token", create_user_token(user_id=1, email="user@example.test", is_admin=credential == "admin")
        )
    with connect(client, token=token, booth=booth):
        assert tts_manager.has_listeners(1, "fr", booth)
    assert not tts_manager.has_listeners(1, "fr", booth)


def test_tts_rejects_foreign_origin_join_cookie():
    client = TestClient(app)
    client.cookies.set("listener_code_conference", "JOIN42")
    with pytest.raises(WebSocketDisconnect):
        with connect(client, headers={"origin": "https://unrelated.example"}):
            pass
