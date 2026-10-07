from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import WSAuthError, resolve_booth_role, resolve_ws_auth
from portal.booth_identity import parse_booth_id
from portal.config import settings
from portal.database import get_event_by_slug, get_room_by_id, get_session
from portal.globals import booths
from portal.routers.listener import has_listener_access
from portal.websockets.manager import (
    Session,
    _handle_accept_handoff,
    _handle_cancel_handoff,
    _handle_chat,
    _handle_initiate_handoff,
    _handle_join,
    _handle_leave,
    _handle_set_active,
    _handle_set_broadcast_unlocked,
    _handle_update_state,
    listener_manager,
    manager,
    tts_manager,
)

_log = logging.getLogger(__name__)


router = APIRouter()


@router.websocket("/ws/booth/{booth_id}")
async def ws_booth(websocket: WebSocket, booth_id: str) -> None:
    try:
        payload = await resolve_ws_auth(websocket, booth_id)
    except WSAuthError:
        return

    if payload and payload.get("role") == "listener":
        await websocket.close(code=4003)
        return

    ws_granted_role = await resolve_booth_role(payload, booth_id)
    await websocket.accept()

    from portal.database import get_booth_language_name

    language_name = await get_booth_language_name(booth_id)

    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language=language_name,
        channel_id=f"{booth_id}-audio",
        granted_role=ws_granted_role,
    )
    manager.add(websocket, session)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "booth:error", "message": "Invalid JSON."}))
                continue

            msg_type = data.get("type", "")
            if msg_type == "booth:join":
                await _handle_join(websocket, session, data)
            elif msg_type == "booth:leave":
                await _handle_leave(session)
            elif msg_type == "booth:chat":
                await _handle_chat(websocket, session, data)
            elif msg_type == "booth:set-active":
                await _handle_set_active(websocket, session, data)
            elif msg_type == "booth:update-state":
                await _handle_update_state(websocket, session, data)
            elif msg_type == "booth:set-broadcast-unlocked":
                await _handle_set_broadcast_unlocked(websocket, session, data)
            elif msg_type == "booth:initiate-handoff":
                await _handle_initiate_handoff(websocket, session, data)
            elif msg_type == "booth:accept-handoff":
                await _handle_accept_handoff(websocket, session, data)
            elif msg_type == "booth:cancel-handoff":
                await _handle_cancel_handoff(websocket, session, data)
            else:
                await websocket.send_text(
                    json.dumps({"type": "booth:error", "message": f"Unknown message type: {msg_type}"})
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(websocket)
        if session.participant_id:
            state = await booths.leave_participant(
                session.booth_id,
                session.participant_id,
                session.language,
                session.channel_id,
            )
            await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


@router.websocket("/ws/captions/{booth_id}")
async def ws_captions(websocket: WebSocket, booth_id: str) -> None:
    """WebSocket endpoint for live captions. Listener tokens are limited to their own event"""
    try:
        await resolve_ws_auth(websocket, booth_id)
    except WSAuthError:
        return
    await websocket.accept()
    listener_manager.add(websocket, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listener_manager.remove(websocket, booth_id)


@router.websocket("/ws/tts/{room_id}/{language_code}/{booth_id}")
async def ws_tts(websocket: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
    try:
        event_slug, source_room, source_language = parse_booth_id(booth_id)
    except ValueError:
        await websocket.close(code=4003)
        return
    if source_room != room_id:
        await websocket.close(code=4003)
        return

    if not websocket.query_params.get("token"):
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc not in {urlparse(settings.public_base_url).netloc, websocket.url.netloc}:
            await websocket.close(code=4003)
            return

    # Listener pages use an event join-code cookie, not an invite session.
    join_cookie = websocket.cookies.get(f"listener_code_{event_slug}")
    if join_cookie and not websocket.query_params.get("token"):
        async with get_session() as db:
            event = await get_event_by_slug(db, event_slug)
            room = await get_room_by_id(db, room_id)
            allowed = (
                event is not None
                and room is not None
                and room.event_id == event.id
                and has_listener_access(websocket, event_slug, event.listener_join_code, None)
            )
        if not allowed:
            await websocket.close(code=4003)
            return
    else:
        try:
            payload = await resolve_ws_auth(websocket, booth_id)
        except WSAuthError:
            return
        if not payload:
            await websocket.close(code=4001)
            return
        if not (payload.get("is_admin") or payload.get("admin") or payload.get("user")):
            if payload.get("role") == "listener":
                allowed = payload.get("event_slug") == event_slug
            else:
                allowed = (
                    payload.get("event_slug") == event_slug
                    and payload.get("language_code") == source_language
                    and str(payload.get("room_id")) == str(room_id)
                )
            if not allowed:
                await websocket.close(code=4003)
                return

    await websocket.accept()
    tts_manager.add(websocket, room_id, language_code, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        tts_manager.remove(websocket, room_id, language_code, booth_id)
