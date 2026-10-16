"""Devoker API: Texas Hold'em rooms over HTTP. Players get live table updates through Server-Sent
Events and act with plain JSON requests. All game state lives in PostgreSQL, so replicas are interchangeable."""

import asyncio
import json
import time

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from . import rooms
from .config import settings
from .db import SessionLocal, get_db
from .models import HandRow, RoomRow
from .poker import MAX_PLAYERS, WAITING, PokerError, Table
from .rooms import ApiError, Room
from .schemas import ActionIn, DecisionIn, Hand, Joined, NameIn, TokenIn

app = FastAPI(title="Devoker API")

# Event streams stay open for the whole game: counting them as requests would wreck the latency metrics.
Instrumentator(excluded_handlers=["/metrics", "/api/rooms/{code}/events"]).instrument(app).expose(app, include_in_schema=False)
HANDS_PLAYED = Counter("devoker_hands_played_total", "Poker hands played to the end")
OPEN_STREAMS = Gauge("devoker_open_event_streams", "Players currently connected to a table on this replica")

POLL_SECONDS = 0.5
PING_SECONDS = 25  # keeps proxies from closing an idle stream


@app.exception_handler(ApiError)
async def api_error(_: Request, e: ApiError) -> JSONResponse:
    return JSONResponse({"error": e.message}, status_code=e.status)


@app.exception_handler(PokerError)
async def poker_error(_: Request, e: PokerError) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=409)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
    err = e.errors()[0]
    return JSONResponse({"error": f"{err['loc'][-1]}: {err['msg']}"}, status_code=400)


@app.get("/health")
def health() -> dict:
    return {"status": "UP"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))  # not ready until the database answers
    return {"status": "READY"}


@app.post("/api/rooms", status_code=201)
def create_room(body: NameIn, db: Session = Depends(get_db)) -> Joined:
    table = Table(settings.starting_chips, settings.small_blind, settings.big_blind)
    host = table.add_player(body.name)
    token = rooms.new_token()
    room = Room(table=table, owner=host.id, tokens={token: host.id})
    while True:
        code = rooms.new_code()
        if db.get(RoomRow, code) is None:
            break
    db.add(RoomRow(code=code, state=room.to_dict()))
    db.commit()
    return Joined(room=code, token=token)


@app.post("/api/rooms/{code}/join", status_code=201)
def join(code: str, body: NameIn, db: Session = Depends(get_db)) -> Joined:
    """Asks the host for a seat. The player gets a token at once and watches their stream until the host decides."""
    with rooms.locked(db, code) as (row, room):
        if len(room.table.players) >= MAX_PLAYERS:
            raise ApiError(409, "table is full")
        token = rooms.new_token()
        room.requests.append({"id": rooms.new_token()[:8], "kind": "join", "name": body.name, "token": token})
    return Joined(room=row.code, token=token)


@app.get("/api/rooms/{code}")
def get_room(code: str, token: str, db: Session = Depends(get_db)) -> dict:
    """The table as this token may see it: the same document the event stream sends."""
    _, room = rooms.load(db, code)
    if not room.known(token):
        raise ApiError(403, "unknown player")
    return room.view(token)


@app.get("/api/rooms/{code}/hands")
def hands(code: str, token: str, db: Session = Depends(get_db)) -> list[Hand]:
    """The last 20 finished hands, newest first."""
    row, room = rooms.load(db, code)
    room.seated(token)
    found = db.scalars(select(HandRow).where(HandRow.room_code == row.code).order_by(HandRow.number.desc()).limit(20))
    return [Hand(number=h.number, pot=h.pot, board=h.board, winners=h.winners) for h in found]


@app.post("/api/rooms/{code}/start", status_code=204)
def start(code: str, body: TokenIn, db: Session = Depends(get_db)) -> None:
    with rooms.locked(db, code) as (_, room):
        room.seated(body.token)
        room.table.start_hand()


@app.post("/api/rooms/{code}/action", status_code=204)
def action(code: str, body: ActionIn, db: Session = Depends(get_db)) -> None:
    with rooms.locked(db, code) as (row, room):
        room.table.act(room.seated(body.token), body.action, body.amount)
        if room.table.street == WAITING:  # that action finished the hand
            room.hands_played += 1
            t = room.table
            db.add(HandRow(room_code=row.code, number=room.hands_played, pot=t.pot(),
                           board=[c.to_json() for c in t.board], winners=t.result["winners"]))
            HANDS_PLAYED.inc()


@app.post("/api/rooms/{code}/rebuy", status_code=204)
def rebuy(code: str, body: TokenIn, db: Session = Depends(get_db)) -> None:
    """Tops a busted player back up. The host's own rebuy goes straight through; anyone else's waits for the host."""
    with rooms.locked(db, code) as (_, room):
        pid = room.seated(body.token)
        room.table.check_rebuy(pid)
        if pid == room.owner:
            room.table.rebuy(pid)
        elif not any(r.get("playerId") == pid for r in room.requests):
            name = room.table.player(pid).name
            room.requests.append({"id": rooms.new_token()[:8], "kind": "rebuy", "name": name, "playerId": pid})


@app.put("/api/rooms/{code}/requests/{request_id}", status_code=204)
def decide(code: str, request_id: str, body: DecisionIn, db: Session = Depends(get_db)) -> None:
    """The host approves or declines a pending join or rebuy."""
    with rooms.locked(db, code) as (_, room):
        if room.seated(body.token) != room.owner:
            raise ApiError(403, "only the host can do that")
        req = next((r for r in room.requests if r["id"] == request_id), None)
        if req is None:
            raise ApiError(404, "request not found")
        room.requests.remove(req)
        if req["kind"] == "rebuy":
            if body.approve:
                room.table.rebuy(req["playerId"])
        elif body.approve and len(room.table.players) < MAX_PLAYERS:
            room.tokens[req["token"]] = room.table.add_player(req["name"]).id
        else:
            room.rejected.append(req["token"])


@app.delete("/api/rooms/{code}/seat", status_code=204)
def leave(code: str, body: TokenIn, db: Session = Depends(get_db)) -> None:
    """Cancels a pending join, or gives up a seat between hands. The host closes the room instead."""
    with rooms.locked(db, code) as (_, room):
        if body.token in room.tokens:
            pid = room.tokens[body.token]
            if pid == room.owner:
                raise ApiError(409, "the host can't leave; close the table instead")
            room.table.remove_player(pid)
            del room.tokens[body.token]
            room.requests = [r for r in room.requests if r.get("playerId") != pid]
        else:
            room.requests = [r for r in room.requests if r.get("token") != body.token]


@app.delete("/api/rooms/{code}", status_code=204)
def close_room(code: str, body: TokenIn, db: Session = Depends(get_db)) -> Response:
    row, room = rooms.load(db, code, lock=True)
    if room.seated(body.token) != room.owner:
        raise ApiError(403, "only the host can do that")
    db.execute(delete(HandRow).where(HandRow.room_code == row.code))
    db.delete(row)
    db.commit()
    return Response(status_code=204)


def snapshot(code: str, token: str) -> tuple[int, dict] | None:
    """The room's version and this token's view, or None once the room is gone."""
    with SessionLocal() as db:
        try:
            row, room = rooms.load(db, code)
        except ApiError:
            return None
        return row.version, room.view(token)


def current_version(code: str) -> int | None:
    with SessionLocal() as db:
        return db.scalar(select(RoomRow.version).where(RoomRow.code == code.upper()))


def is_known(code: str, token: str) -> bool:
    """404 if the room is gone, else whether this token belongs to it."""
    with SessionLocal() as db:
        _, room = rooms.load(db, code)
        return room.known(token)


@app.get("/api/rooms/{code}/events")
async def events(code: str, token: str, request: Request) -> StreamingResponse:
    """Streams the room as this token may see it every time it changes. The token is in the query
    string because the browser's EventSource can't send headers.

    No Depends(get_db) here: FastAPI would keep that session (and its pooled connection, idle in
    transaction) open for the whole stream, and a few dozen players would exhaust the pool and
    starve /ready. Every database touch below opens and closes its own short session."""
    if not await run_in_threadpool(is_known, code, token):
        raise ApiError(403, "unknown player")

    async def stream():
        OPEN_STREAMS.inc()
        try:
            version, last_ping = None, time.monotonic()
            while not await request.is_disconnected():
                # shortcut: each stream polls its room's version; switch to Postgres LISTEN/NOTIFY if
                # thousands of players connect at once.
                if await run_in_threadpool(current_version, code) != version:
                    snap = await run_in_threadpool(snapshot, code, token)
                    if snap is None:
                        yield f"data: {json.dumps({'status': 'closed'})}\n\n"
                        return
                    version, view = snap
                    yield f"data: {json.dumps(view)}\n\n"
                elif time.monotonic() - last_ping > PING_SECONDS:
                    last_ping = time.monotonic()
                    yield ": ping\n\n"
                await asyncio.sleep(POLL_SECONDS)
        finally:
            OPEN_STREAMS.dec()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # stop nginx buffering the stream
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)
