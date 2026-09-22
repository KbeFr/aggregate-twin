"""The console's JSON API. One thin function per route; the work lives in the views
(reads) and in ConsoleGateway (writes). 202 means handed to the step thread."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aggregate_twin.gui.comm_monitor import CommMonitor
from aggregate_twin.gui.console_gateway import ConsoleGateway
from aggregate_twin.gui.discovery_view import discovery_form
from aggregate_twin.gui.http_router import Request, Router
from aggregate_twin.gui.network_view import network_state
from aggregate_twin.gui.view_helpers import header
from aggregate_twin.gui.world_view import world_state

ACCEPTED = 202

api = Router()


@dataclass(frozen=True)
class Console:
    twin: Any
    monitor: CommMonitor
    gateway: ConsoleGateway


# ------------------------------------------------------------------ reads
@api.get("/healthz")
def health(console: Console, request: Request):
    return {"status": "ok", **header(console.twin, console.monitor)}


@api.get("/api/world")
def world(console: Console, request: Request):
    return world_state(console.twin, console.monitor)


@api.get("/api/network")
def network(console: Console, request: Request):
    return network_state(console.twin, console.monitor, console.gateway)


@api.get("/api/discoveries/{agent}")
def discovery(console: Console, request: Request):
    return discovery_form(console.twin, request.params["agent"])


# ------------------------------------------------------------- missions
@api.post("/api/missions")
def dispatch_mission(console: Console, request: Request):
    mission_id = console.gateway.dispatch_mission(request.body)
    return {"ok": True, "mission_id": mission_id}, ACCEPTED


@api.post("/api/missions/cancel")
def cancel_mission(console: Console, request: Request):
    console.gateway.cancel_mission(request.body.get("mission_id", ""))
    return {"ok": True}, ACCEPTED


@api.post("/api/replan")
def replan(console: Console, request: Request):
    console.gateway.replan()
    return {"ok": True}, ACCEPTED


# -------------------------------------------------------------- pairing
@api.post("/api/pairing/mode")
def set_pairing_mode(console: Console, request: Request):
    mode = console.gateway.set_linking_mode(request.body.get("mode"))
    return {"ok": True, "mode": mode}, ACCEPTED


@api.post("/api/pairing/link")
def link_pair(console: Console, request: Request):
    agent, instance = request.body.get("agent", ""), request.body.get("instance", "")
    console.gateway.link_pair(agent, instance)
    return {"ok": True, "agent": agent, "instance": instance}, ACCEPTED


@api.post("/api/release")
def release(console: Console, request: Request):
    console.gateway.release(request.body.get("agent", ""))
    return {"ok": True}, ACCEPTED


# ------------------------------------------------------------ discovery
@api.post("/api/autocomplete")
def set_autocomplete(console: Console, request: Request):
    enabled = bool(request.body.get("enabled", True))
    completed = console.gateway.set_autocomplete(enabled)
    return {"ok": True, "autocomplete": enabled, "completed": completed}, ACCEPTED


@api.post("/api/discoveries/{agent}/resolve")
def resolve_discovery(console: Console, request: Request):
    agent = request.params["agent"]
    console.gateway.resolve_discovery(agent, request.body.get("fields") or {})
    return {"ok": True, "agent_name": agent}, ACCEPTED


@api.post("/api/discoveries/{agent}/reject")
def reject_discovery(console: Console, request: Request):
    console.gateway.reject_discovery(request.params["agent"])
    return {"ok": True}, ACCEPTED
