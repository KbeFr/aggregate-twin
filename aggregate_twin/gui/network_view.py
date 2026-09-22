"""
Network sheet: linked pairs, the unlinked pool, pairing controls and message flow.

Pool agent states, as the console draws them:

    review      known, but its discovery waits for the operator
    waiting     complete, no instance asked yet
    chosen      the operator picked an instance that is still busy
    requesting  asked every instance at once (pooled)
    offered     asked one instance directly
    bidding     free instances are bidding for it
    releasing   its handshake is being cancelled
"""
from __future__ import annotations

import time
from typing import Any

from aggregate_twin.core.comm_managers.link_manager import LinkingMode
from aggregate_twin.gui.comm_monitor import CommMonitor
from aggregate_twin.gui.discovery_view import review_state
from aggregate_twin.gui.view_helpers import enum_value, header

LOG_TAIL = 120
IN_HANDSHAKE = ("requesting", "offered", "bidding")


def network_state(twin: Any, monitor: CommMonitor, gateway: Any) -> dict[str, Any]:
    links = twin.link_manager
    stale_after = getattr(twin.fleet, "stale_after", 3.0)
    top = header(twin, monitor)

    pairs = _pairs(twin, monitor, stale_after)
    pool_agents = _pool_agents(twin, monitor)
    pool_instances = _pool_instances(twin, monitor)
    rows = _history(twin, monitor, stale_after)

    return {
        "twin": top,
        "review": review_state(twin),
        "graph": {
            "aggregate": {"id": top["name"], "detail": f"{top['loop_hz']} Hz"},
            "pairs": pairs,
            "pool": {"agents": pool_agents, "instances": pool_instances},
        },
        "pairing": {
            "mode": links.linking_mode.value,
            "modes": [{"id": m.value, "available": links.mode_available(m)} for m in LinkingMode],
            "instance_discovery": bool(links.instance_discovery),
            "pool_timeout": links.pool_timeout,
            "linkable": [a["id"] for a in pool_agents if a["state"] in ("waiting", "chosen")],
            "free": [i["id"] for i in pool_instances if i["state"] == "free"],
        },
        "rows": rows,
        "counters": {
            "linked": len(pairs),
            "pooled": len(pool_agents) + len(pool_instances),
            "degraded": sum(1 for p in pairs if p["state"] in ("stale", "silent")),
            "msgs_in": monitor.msgs_in,
            "msgs_out": monitor.msgs_out,
            "handshakes": (sum(1 for a in pool_agents if a["state"] in IN_HANDSHAKE)
                           + len(twin.missions_in_flight)),
            "stale_after": stale_after,
            "planner_error": gateway.last_error,
        },
        "events": list(monitor.events)[-LOG_TAIL:],
        "telemetry_events": list(monitor.telemetry_events)[-LOG_TAIL:],
    }


def _pairs(twin: Any, monitor: CommMonitor, stale_after: float) -> list[dict[str, Any]]:
    links = twin.link_manager
    pairs = []
    for entry in sorted(twin.fleet.all(include_stale=True), key=lambda e: str(e.name)):
        peer = monitor.find(entry.name)
        telemetry = peer.ch["twin_state"].hz if peer else 0.0
        out = peer.ch["instantiate_out"].count if peer else 0
        back = peer.ch["instantiate_in"].count if peer else 0
        if links.releasing(entry.name):
            state = "releasing"
        elif entry.age <= stale_after:
            state = "live"
        else:
            state = "stale" if entry.age <= 2 * stale_after else "silent"
        pairs.append({
            "agent": entry.name,
            "instance": entry.instance_name,
            "kind": enum_value(entry.kind, "ugv"),
            "state": state,
            "age": round(entry.age, 1),
            "telemetry": f"{telemetry:.1f} Hz",
            "handshake": f"{out}↑ {back}↓",
        })
    return pairs


def _pool_agents(twin: Any, monitor: CommMonitor) -> list[dict[str, Any]]:
    links = twin.link_manager
    review = twin.discoveries_gui
    agents = []
    for name, discovery in list(links.unlinked_agents):
        election = links.elections.get(name)
        handshake = links.handshakes.get(name)
        choice = links.instance_choice.get(name)
        targets: list[str] = []
        if discovery is None:
            state = "review"
        elif election is not None:
            state, targets = "bidding", sorted(election.engaged)
        elif handshake is not None:
            targets = sorted(handshake.engaged)
            state = ("releasing" if links.releasing(name)
                     else "offered" if targets else "requesting")
        elif choice:
            state = "chosen"
        else:
            state = "waiting"
        kind, agent_type = _identity(discovery, review.get(name))
        agents.append({
            "id": name, "kind": kind, "agent_type": agent_type, "state": state,
            "targets": targets, "choice": choice,
            "hz": round(monitor.heartbeat_hz(name), 1),
            **_liveness(links, links.unlinked_agents, name),
        })
    return agents


def _pool_instances(twin: Any, monitor: CommMonitor) -> list[dict[str, Any]]:
    links = twin.link_manager
    engaged = links.engaged_instances()
    instances = []
    for name in links.unlinked_instances.names():
        busy = name in engaged
        instances.append({
            "id": name,
            "state": "engaged" if busy else "free",
            "engaged_with": links.subjects_on(name) if busy else [],
            "hz": round(monitor.heartbeat_hz(name), 1),
            **_liveness(links, links.unlinked_instances, name),
        })
    return instances


def _history(twin: Any, monitor: CommMonitor, stale_after: float) -> list[dict[str, Any]]:
    rows = []
    for name, peer in sorted(monitor.snapshot(), key=lambda kv: str(kv[0])):
        rows.append({
            "agent": name,
            "instance": peer.instance or twin.fleet.instance_of(name),
            "kind": peer.kind,
            "phase": peer.phase,
            "state": monitor.link_state(peer, stale_after),
            "since": round(time.time() - peer.since, 1),
            "discovery": peer.ch["discovery"].as_dict(),
            "instantiate": {"out": peer.ch["instantiate_out"].count,
                            "in": peer.ch["instantiate_in"].count},
            "mission": {"out": peer.ch["mission_out"].count, "in": peer.ch["mission_in"].count},
            "twin_state": peer.ch["twin_state"].as_dict(),
            "obstacle": peer.ch["obstacle"].as_dict(),
        })
    return rows


def _liveness(links: Any, registry: Any, name: str) -> dict[str, float]:
    age = registry.age(name) or 0.0
    return {"age": round(age, 1), "expires_in": round(max(0.0, links.pool_timeout - age), 1)}


def _identity(discovery: Any, layers: dict | None) -> tuple[str | None, str | None]:
    if discovery is not None:
        return (enum_value(getattr(discovery, "kind", None)),
                enum_value(getattr(discovery, "agent_type", None)))
    reported = (layers or {}).get("reported")
    if reported is None:
        return None, None
    kind = reported.get("kind") if "kind" in reported else None
    agent_type = reported.get("agent_type") if "agent_type" in reported else None
    return enum_value(kind), enum_value(agent_type)
