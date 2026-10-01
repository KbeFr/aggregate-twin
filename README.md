# Aggregate Twin

The top level of the hierarchy, with one aggregate per namespace. It owns the shared world model,
decides which instance twin runs which agent, and plans, auctions and tracks missions across the
fleet. It never reads agent sensors directly; it only sees what instance twins report.

## Responsibilities

- **Pairing:** discover agents and instance twins, complete agent specs, and link each agent to one instance.
- **World model:** world bounds, static obstacles, and live obstacle reports combined into an occupancy grid.
- **Missions:** A* planning per candidate agent, bidding rounds, award, progress and completion.
- **Liveness:** evict silent agents or instances and return their missions to the pool.
- **Operator console:** a web UI for the map, network graph, pairing and mission dispatch.

## Architecture

```
AggregateNetworkNode ──decode──► inbox ──► AggregateTwin.step()  (10 Hz)
  flexNode · MQTT                            ├── LinkManager      pairing · pools · liveness
        ▲                                    ├── MissionManager   catalogue · planning · elections
        │                                    ├── World model      ObstacleRegistry → GlobalGridMap → A*
        │                                    ├── FleetRegistry    agent state · agent↔instance routing
        └──────────── outbox flush ──────────┘
```

Transport callbacks only decode and enqueue; all state changes happen on the step thread.
Operator actions from the console are queued with `submit_command()`.

| Module | Role |
|---|---|
| `core/aggregate_twin.py` | Owns the loop, inbox and transport; dispatches inbound messages with `@handles` |
| `core/comm_managers/handshake_coordinator.py` | Shared bookkeeping for one kind of subject: open, route, tick and retire conversations |
| `core/comm_managers/link_manager.py` | Agent ↔ instance pairing, unlinked pools, liveness sweep |
| `core/comm_managers/mission_manager.py` | Mission catalogue; the only writer of `MissionStatus` |
| `core/fleet/` | `FleetRegistry` (linked agents, state, routing maps) and `UnlinkedRegistry` (waiting pools) |
| `core/discovery_config.py` | Layered completion of agent discoveries (OmegaConf) |
| `core/functions/` | `GlobalGridMap` (occupancy, clearance, risk, coverage layers), `AStarPlannerCustom`, `MissionPlanner` |
| `core/obstacle_registry.py` | Obstacle reports per reporting agent: pruning, clustering, disagreement detection |
| `comms/aggregate_comms.py` | flexNode transport: decode into the inbox, publish, per-node subscriptions, flexCloud variables |
| `gui/` | Operator console, see [`gui/README.md`](aggregate_twin/gui/README.md) |

## Step loop

Each tick runs these steps in order:
1. Drain the inbox.
2. Run queued operator commands.
3. Every 20 steps: rebuild the grid map from obstacle reports and run a planning pass.
4. Tick handshake timeouts.
5. Run the liveness sweep.
6. Link waiting agents.
7. Flush the outboxes.

## Pairing modes

| Mode | Behaviour | Requires instance discovery |
|---|---|---|
| `pooled` | Broadcast on `instantiate`; the first ACK wins | no |
| `first_free` *(default)* | Directed at the free instance that has waited longest | yes |
| `gui` | Directed at the instance the operator picked | yes |
| `auction` | Every free instance bids; the lightest load wins | yes |

## Discovery completion

An agent's self-report is merged with config layers from `config/agent_configs/`. Higher layers win:

| Priority | Layer | Source |
|---|---|---|
| 1 | reported | the `AgentDiscoveryMessage` itself |
| 2 | agent | `agent_specific/<agent_name>__config.yaml` |
| 3 | type | `<kind>_configs/<agent_type>__config.yaml` |
| 4 | kind | `<kind>_configs/default__<kind>_config.yaml` |

With `autocomplete` on, the layers merge automatically. Otherwise the operator resolves each field in the console.

## Missions

- **Types:** `GOTO_WAYPOINT`, `COVERAGE_PATROL` (ordered waypoints), `TIME_GATED_GOTO` (unlocks at a set time), `TRACK_TARGET` (not implemented yet).
- **Postures** weight the planner across distance, energy, time, uncertainty and risk: `COVERAGE`, `CONSERVE`, `URGENT`, `SAFE`, plus `ASTAR` for plain shortest path.
- **Flow:** plan A* for every available UGV → send the best *k = 3* routes as `MissionPlanHint`s → instances reply with a `MissionBidding` (time, battery spend, remaining margin) → the winner is the lowest weighted time + energy + plan cost above the battery floor → `ACTIVE` → the instance reports `COMPLETE`.

## Configuration

| File | Contents |
|---|---|
| `config/config.yaml` | flexNode config (logger, MQTT/Redis, application id) |
| `config/empty_world.yaml` | World `width`, `height`, `origin_x/y`, grid `resolution`, `static_obstacles` |
| `config/global_topic_config.yaml` | Global topics (`discovery`, `instantiate`) |
| `config/specific_topic_config.yaml` | Topics subscribed per linked instance |
| `config/agent_configs/` | Discovery completion layers |

| Env var | Default | Purpose |
|---|---|---|
| `TWIN_NAME` | `aggregate_twin` | Node id |
| `TWIN_NAMESPACE` | `default-ns` | Topic namespace |
| `TWIN_TICK_HZ` | `10` | Loop rate |
| `MQTT_BROKER_HOST` / `REDIS_HOST` | `localhost` | Transport |
| `WORLD_CONFIG_FILE_NAME` | `empty_world.yaml` | World file in `config/` |
| `TWIN_GUI_PORT` | `8082` | Console port |

## Run

```bash
pip install -e core_msgs -e flexCommunicator -e aggregate-twin
aggregate-twin                     # console at http://localhost:8082
pytest aggregate-twin/tests
```