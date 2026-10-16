def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "UP"}
    assert client.get("/ready").json() == {"status": "READY"}


def test_create_room_validates_name(client):
    r = client.post("/api/rooms", json={"name": "   "})
    assert r.status_code == 400 and "name" in r.json()["error"]


def test_unknown_room_is_404(client):
    r = client.post("/api/rooms/ZZZZZ/join", json={"name": "bob"})
    assert (r.status_code, r.json()) == (404, {"error": "room not found"})


def test_join_waits_for_host_approval(table):
    bob = table.ask("bob")
    assert table.view(bob) == {"status": "pending"}
    assert table.post("action", token=bob, action="fold").status_code == 403, "pending players can't act"
    reqs = table.view(table.host)["requests"]
    assert [(r["kind"], r["name"]) for r in reqs] == [("join", "bob")]
    assert "token" not in reqs[0], "the host never sees the guest's secret token"
    assert table.decide(bob, reqs[0]["id"], True).status_code == 403, "only the host decides"

    assert table.decide(table.host, reqs[0]["id"], True).status_code == 204
    assert table.view(bob)["status"] == "seated"
    assert [p["name"] for p in table.view(bob)["players"]] == ["host", "bob"]


def test_declined_join_is_rejected(table):
    eve = table.ask("eve")
    req = table.view(table.host)["requests"][0]
    table.decide(table.host, req["id"], False)
    assert table.view(eve) == {"status": "rejected"}


def test_cancelled_join_disappears_from_host_list(table):
    bob = table.ask("bob")
    assert table.delete("/seat", bob).status_code == 204
    assert table.view(table.host)["requests"] == []


def test_play_a_hand_and_record_it(table):
    bob = table.seat("bob")
    assert table.post("start", token=table.host).status_code == 204
    v = table.view(table.host)
    assert v["street"] == "preflop" and len(v["hole"]) == 2
    assert v["players"][1].get("hole") is None, "other players' cards stay hidden"

    first = table.host if v["toAct"] == v["you"] else bob
    second = bob if first == table.host else table.host
    assert table.post("action", token=second, action="fold").json() == {"error": "not your turn"}
    assert table.post("action", token=first, action="fold").status_code == 204

    v = table.view(table.host)
    assert v["street"] == "waiting" and sum(p["chips"] for p in v["players"]) == 2000
    hands = table.c.get(f"/api/rooms/{table.code}/hands", params={"token": bob}).json()
    assert len(hands) == 1 and hands[0]["number"] == 1 and hands[0]["pot"] == 30


def test_rebuy_needs_host_approval_except_for_host(table, client):
    bob = table.seat("bob")
    from app.db import SessionLocal
    from app.models import RoomRow
    from app.rooms import Room

    def bust(player_index: int):
        with SessionLocal() as db:
            row = db.get(RoomRow, table.code)
            room = Room.from_dict(row.state)
            room.table.players[player_index].chips = 0
            row.state = room.to_dict()
            db.commit()

    bust(1)
    assert table.post("rebuy", token=bob).status_code == 204
    assert table.view(bob)["rebuyPending"] is True
    assert table.view(bob)["players"][1]["chips"] == 0
    req = table.view(table.host)["requests"][0]
    assert req["kind"] == "rebuy"
    table.decide(table.host, req["id"], True)
    assert (table.view(bob)["players"][1]["chips"], table.view(bob)["players"][1]["buyins"]) == (1000, 2)

    bust(0)
    table.post("rebuy", token=table.host)
    assert table.view(table.host)["players"][0]["chips"] == 1000, "the host's own rebuy is immediate"


def test_leave_between_hands(table):
    bob = table.seat("bob")
    assert table.delete("/seat", bob).status_code == 204
    assert [p["name"] for p in table.view(table.host)["players"]] == ["host"]
    assert table.delete("/seat", table.host).status_code == 409, "the host closes the table instead"


def test_only_host_closes_room(table, client):
    bob = table.seat("bob")
    assert table.delete("", bob).status_code == 403
    assert table.delete("", table.host).status_code == 204
    assert client.get(f"/api/rooms/{table.code}", params={"token": table.host}).status_code == 404


def test_metrics_endpoint(client, table):
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "devoker_hands_played_total" in body


def test_events_rejects_unknown_token_and_missing_room(table):
    assert table.c.get(f"/api/rooms/{table.code}/events", params={"token": "nope"}).status_code == 403
    assert table.c.get("/api/rooms/ZZZZZ/events", params={"token": "nope"}).status_code == 404


def test_events_holds_no_request_scoped_db_session():
    """A Depends(get_db) session would stay open (idle in transaction) for the whole stream and
    exhaust the connection pool once enough players connect, which starves /ready."""
    from app.db import get_db
    from app.main import app

    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/rooms/{code}/events")
    assert get_db not in [d.call for d in route.dependant.dependencies]
