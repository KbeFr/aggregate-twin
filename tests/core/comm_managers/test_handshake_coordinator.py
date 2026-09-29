"""The user's test harness, driven through HandshakeCoordinator instead of a
hand-rolled FakeAggregate, plus the election-specific cases.

"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass

import pytest

from core_msgs.instance_aggregate.handshake_shared import (
    HandshakeEnvelope, HandshakeStatus as HS, InitiatorState as IS)
from core_msgs.instance_aggregate.handshake import (
    HandshakeAction as A, HandshakeElection, HandshakeInitiator, HandshakeResponder,
    Reservation, orphan_reply)
from core_msgs.instance_aggregate.mission_handshake import (
    MISSION_ORPHAN_REPLIES, MissionInitiator, MissionResponder)
from aggregate_twin.core.comm_managers.handshake_coordinator import HandshakeCoordinator

AGG = "agg"


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


@dataclass
class Bid:
    time_bidding: float


def lowest_time(payload, bids, hints):
    ok = {i: b for i, b in bids.items() if b is not None}
    return min(ok, key=lambda i: ok[i].time_bidding) if ok else None


class Agg(HandshakeCoordinator):
    kind = "mission"
    initiator_cls = MissionInitiator
    orphan_replies = MISSION_ORPHAN_REPLIES

    def __init__(self, clock, timeout=2.0):
        super().__init__(AGG, clock=clock, timeout=timeout)
        self.links, self.releases, self.completes = [], [], []

    def on_link(self, s, r): self.links.append((s, r))
    def on_release(self, s, r): self.releases.append((s, r))
    def on_complete(self, s, r): self.completes.append((s, r))


class Net:
    def __init__(self, agg, responders, rng=None, drop=0.0):
        self.agg, self.resp, self.rng, self.drop = agg, responders, rng, drop
        self.q = deque()
        self.actions = []
        self.log = []

    def pump(self):
        for env in self.agg.outbox:
            self.q.append(("inst", env))
        self.agg.outbox.clear()

    def from_instance(self, env):
        if env is not None:
            self.q.append(("agg", env))

    def run(self, limit=10_000):
        n = 0
        self.pump()
        while self.q:
            n += 1
            assert n < limit, "message storm"
            i = self.rng.randrange(len(self.q)) if self.rng else 0
            self.q.rotate(-i)
            side, env = self.q.popleft()
            if self.drop and self.rng.random() < self.drop:
                continue
            self.log.append((side, env.handshake_status.value, env.id, env.sender, env.target))
            if side == "inst":
                for name in (list(self.resp) if env.target is None else [env.target]):
                    if name not in self.resp:
                        continue
                    r = self.resp[name].handle(env)
                    if r.outbound is not A.DO_NOTHING:
                        self.actions.append((name, r.outbound, r.subject))
                    self.from_instance(r.reply)
            else:
                self.agg.route(env)
                self.pump()


def world(n=3, **rkw):
    clk = Clock()
    agg = Agg(clk)
    resp = {f"i{k}": MissionResponder(f"i{k}", clock=clk, **rkw) for k in range(1, n + 1)}
    return clk, agg, resp, Net(agg, resp)


# ══ POOLED ═════════════════════════════════════════════════════════════════

def test_pooled_first_ack_wins_and_the_rest_are_cancelled():
    clk, agg, resp, net = world(3, max_reserved=1)
    agg.request("agentA", payload="disc")
    net.run()
    assert agg.links == [("agentA", "i1")]
    assert resp["i1"].active == "agentA"
    assert all(not r.reserved for r in resp.values())
    assert [l[1] for l in net.log].count("bid") == 0


def test_pooled_release_frees_the_instance_and_drops_the_handshake():
    clk, agg, resp, net = world(2, max_reserved=1)
    agg.request("agentA", payload={"kind": "ugv"})
    net.run()
    winner = agg.links[0][1]
    agg.cancel("agentA")
    net.run()
    assert agg.releases == [("agentA", winner)]
    assert resp[winner].active is None and not agg.in_flight("agentA")


def test_pooled_instance_does_not_promise_itself_to_two_agents():
    clk, agg, resp, net = world(1, max_reserved=1)
    agg.request("A", payload=1)
    agg.request("B", payload=2)
    net.run()
    assert resp["i1"].active == "A" and not resp["i1"].reserved
    assert [s for s, _ in agg.links] == ["A"]


def test_pooled_late_ack_is_cancelled_not_linked():
    clk, agg, resp, net = world(2, max_reserved=1)
    agg.request("A", payload=1)
    req = agg.outbox[0]
    net.run()
    net.from_instance(resp["i2"].handle(req).reply)
    net.run()
    assert resp["i2"].active is None and not resp["i2"].reserved
    assert len(agg.links) == 1


def test_pooled_nobody_answers_times_out_and_can_be_retried():
    clk, agg, resp, net = world(0)
    agg.request("A", payload=1)
    net.run()
    clk.advance(2.5)
    agg.tick()
    assert not agg.in_flight("A") and agg.links == []
    assert agg.request("A", payload=1)
    assert agg.in_flight("A")


def test_epoch_continues_across_attempts_on_the_same_subject():
    """A reply to the attempt we gave up on must not look current."""
    clk, agg, resp, net = world(1)
    agg.request("A", payload=1, target="i1")
    first = agg.outbox[0]
    agg.outbox.clear()
    clk.advance(2.5); agg.tick()
    assert not agg.in_flight("A")
    agg.request("A", payload=1, target="i1")
    assert agg.outbox[0].epoch > first.epoch


# ══ DIRECTED ═══════════════════════════════════════════════════════════════

def test_directed_acks_and_links_without_bidding():
    clk, agg, resp, net = world(3, max_reserved=1)
    agg.request("agentA", payload="disc", target="i2")
    net.run()
    assert agg.links == [("agentA", "i2")]
    assert resp["i2"].active == "agentA"
    assert resp["i1"].active is None and not resp["i1"].reserved


def test_directed_to_an_engaged_instance_is_refused_locally():
    """New: the coordinator will not even ask an instance it already committed."""
    clk, agg, resp, net = world(1)
    agg.request("m1", payload="mission", target="i1")
    net.run()
    assert agg.request("m2", payload="mission", target="i1") is False
    assert not agg.in_flight("m2")


def test_directed_bind_failure_revokes_and_releases():
    clk, agg, resp, net = world(1)
    agg.request("agentA", payload=1, target="i1")
    net.run()
    net.from_instance(resp["i1"].get_revoked("agentA"))
    net.run()
    assert agg.releases == [("agentA", "i1")]
    assert resp["i1"].active is None and not agg.in_flight("agentA")


# ══ ELECTION ═══════════════════════════════════════════════════════════════

def test_election_lowest_bid_wins_losers_released():
    clk, agg, resp, net = world(3, bid_fn=lambda h: Bid(h["t"]))
    agg.elect("m1", {"i1": {"t": 30}, "i2": {"t": 10}, "i3": {"t": 20}},
              lowest_time, payload="mission")
    net.run()
    assert agg.links == [("m1", "i2")]
    assert resp["i2"].active == "m1"
    assert all(not r.reserved for r in resp.values())
    assert agg.receiver_of("m1") == "i2"


def test_election_hands_back_a_plain_initiator():
    clk, agg, resp, net = world(2, bid_fn=lambda h: Bid(h["t"]))
    agg.elect("m1", {"i1": {"t": 5}, "i2": {"t": 1}}, lowest_time)
    net.run()
    assert agg.elections == {}
    assert isinstance(agg.handshakes["m1"], MissionInitiator)
    assert agg.handshakes["m1"].state is IS.CONFIRMED
    assert agg.hint_of("m1") == {"t": 1}


def test_election_waits_for_every_bid():
    clk, agg, resp, net = world(2, bid_fn=lambda h: Bid(h["t"]))
    agg.elect("m1", {"i1": {"t": 5}, "i2": {"t": 1}}, lowest_time)
    first = [e for e in agg.outbox if e.target == "i1"]
    second = [e for e in agg.outbox if e.target == "i2"]
    agg.outbox = first
    net.run()
    assert "m1" in agg.elections and not agg.elections["m1"].resolved
    agg.outbox = second
    net.run()
    assert agg.links == [("m1", "i2")]


def test_election_deadline_awards_among_the_instances_that_answered():
    clk, agg, resp, net = world(3, bid_fn=lambda h: Bid(h["t"]))
    agg.elect("m1", {"i1": {"t": 9}, "i2": {"t": 3}, "i3": {"t": 1}}, lowest_time)
    deaf = [e for e in agg.outbox if e.target == "i3"]
    agg.outbox = [e for e in agg.outbox if e.target != "i3"]
    net.run()
    assert "m1" in agg.elections
    clk.advance(2.5)
    agg.tick(); net.run()
    assert agg.links == [("m1", "i2")]
    agg.outbox = deaf; net.run()
    assert resp["i3"].active is None and not resp["i3"].reserved
    assert len(agg.links) == 1


def test_election_late_bid_from_a_loser_is_cancelled_by_the_winner():
    """The losers' initiators are gone. The winner answers for them."""
    clk, agg, resp, net = world(2, bid_fn=lambda h: Bid(h["t"]))
    agg.elect("m1", {"i1": {"t": 9}, "i2": {"t": 1}}, lowest_time)
    late = [e for e in agg.outbox if e.target == "i1"]
    agg.outbox = [e for e in agg.outbox if e.target != "i1"]
    net.run()
    clk.advance(2.5); agg.tick(); net.run()
    assert agg.links == [("m1", "i2")]
    agg.outbox = late; net.run()
    assert resp["i1"].active is None and not resp["i1"].reserved
    assert resp["i2"].active == "m1"


def test_election_all_refuse_fails_and_is_dropped_for_replanning():
    clk, agg, resp, net = world(2, bid_fn=lambda h: None)
    agg.elect("m1", {"i1": {}, "i2": {}}, lowest_time)
    net.run()
    assert agg.links == [] and not agg.in_flight("m1")


def test_election_winner_never_confirms_is_cancelled_not_left_holding():
    """The initiator's own retry path now owns this, not the election."""
    clk, agg, resp, net = world(1, bid_fn=lambda h: Bid(1))
    agg.elect("m1", {"i1": {}}, lowest_time)
    net.pump()
    while net.q:                                # deliver the award, drop the confirm
        side, env = net.q.popleft()
        if side == "inst":
            r = resp["i1"].handle(env)
            if r.reply is not None and r.reply.handshake_status is not HS.ACK:
                net.from_instance(r.reply)
            elif r.reply is not None and r.reply.handshake_status is HS.BID:
                net.from_instance(r.reply)
        else:
            agg.route(env); net.pump()
    assert resp["i1"].active == "m1"            # it linked, the confirm was lost
    assert agg.links == []
    clk.advance(2.5)
    agg.tick(); net.run()
    assert resp["i1"].active is None
    assert agg.releases == [("m1", "i1")]


def test_election_cancel_mid_bidding_releases_reservations():
    clk, agg, resp, net = world(2, bid_fn=lambda h: Bid(1))
    agg.elect("m1", {"i1": {}, "i2": {}}, lowest_time)
    late = [e for e in agg.outbox if e.target == "i2"]
    agg.outbox = [e for e in agg.outbox if e.target == "i1"]
    net.run()
    assert resp["i1"].reserved
    agg.cancel("m1"); net.run()
    assert not resp["i1"].reserved
    agg.outbox = late; net.run()
    assert not resp["i2"].reserved


def test_election_get_winner_exception_fails_cleanly():
    clk, agg, resp, net = world(1, bid_fn=lambda h: Bid(1))
    agg.elect("m1", {"i1": {}}, lambda p, b, h: 1 / 0)
    net.run()
    assert agg.links == [] and not resp["i1"].reserved


def test_election_skips_candidates_already_engaged():
    clk, agg, resp, net = world(2, bid_fn=lambda h: Bid(1))
    agg.request("m1", payload="mission", target="i1")
    net.run()
    agg.elect("m2", {"i1": {}, "i2": {}}, lowest_time)
    assert set(agg.elections["m2"].hints) == {"i2"}
    net.run()
    assert agg.links == [("m1", "i1"), ("m2", "i2")]


def test_election_with_no_free_candidate_is_refused():
    clk, agg, resp, net = world(1, bid_fn=lambda h: Bid(1))
    agg.request("m1", payload="mission", target="i1")
    net.run()
    assert agg.elect("m2", {"i1": {}}, lowest_time) is False
    assert not agg.in_flight("m2")


# ══ after linking: identical whatever assigned it ══════════════════════════

@pytest.mark.parametrize("mode", ["pooled", "directed", "election"])
def test_complete_is_the_same_for_every_mode(mode):
    clk, agg, resp, net = world(1, bid_fn=lambda h: Bid(1))
    if mode == "pooled":
        agg.request("m1", payload="mission")
    elif mode == "directed":
        agg.request("m1", payload="mission", target="i1")
    else:
        agg.elect("m1", {"i1": {"t": 1}}, lowest_time, payload="mission")
    net.run()
    assert agg.links == [("m1", "i1")]

    net.from_instance(resp["i1"].get_completed("m1")); net.run()
    assert agg.completes == [("m1", "i1")] and agg.releases == []
    assert resp["i1"].active is None
    assert not agg.in_flight("m1")


@pytest.mark.parametrize("mode", ["pooled", "directed", "election"])
def test_cancel_after_linking_is_the_same_for_every_mode(mode):
    clk, agg, resp, net = world(1, bid_fn=lambda h: Bid(1))
    if mode == "pooled":
        agg.request("m1", payload="mission")
    elif mode == "directed":
        agg.request("m1", payload="mission", target="i1")
    else:
        agg.elect("m1", {"i1": {"t": 1}}, lowest_time, payload="mission")
    net.run()
    agg.cancel("m1"); net.run()
    assert agg.releases == [("m1", "i1")] and agg.completes == []
    assert resp["i1"].active is None and not agg.in_flight("m1")


def test_release_that_is_never_acknowledged_is_given_up_on():
    clk, agg, resp, net = world(1)
    agg.request("A", payload=1, target="i1"); net.run()
    agg.cancel("A")
    agg.outbox.clear()
    for _ in range(5):
        clk.advance(2.5)
        agg.tick()
        agg.outbox.clear()
    assert agg.releases == [("A", "i1")] and not agg.in_flight("A")


def test_lost_complete_ack_is_repaired_by_the_orphan_rule():
    clk, agg, resp, net = world(1)
    agg.request("m1", payload=1, target="i1"); net.run()
    net.from_instance(resp["i1"].get_completed("m1")); net.run()
    assert not agg.in_flight("m1")
    resp["i1"].active, resp["i1"].active_epoch = "m1", 1
    net.from_instance(resp["i1"].get_completed("m1")); net.run()
    assert resp["i1"].active is None


def test_base_initiator_has_no_completion_leg():
    """COMPLETE belongs to the mission extension, not to the base flow."""
    initiator = HandshakeInitiator("A", AGG, receiver="i1")
    initiator.request()
    initiator.handle(HandshakeEnvelope("A", HS.ACK, "i1", epoch=1))
    res = initiator.handle(HandshakeEnvelope("A", HS.COMPLETE, "i1", epoch=1))
    assert res.action is A.DO_NOTHING and initiator.illegal == 1

    mission = MissionInitiator("A", AGG, receiver="i1")
    mission.request()
    mission.handle(HandshakeEnvelope("A", HS.ACK, "i1", epoch=1))
    res = mission.handle(HandshakeEnvelope("A", HS.COMPLETE, "i1", epoch=1))
    assert res.action is A.COMPLETE_SUBJECT and mission.illegal == 0


def test_orphan_table_is_generic_until_a_kind_extends_it():
    ghost = HandshakeEnvelope("ghost", HS.COMPLETE, "i1", epoch=1)
    assert orphan_reply(ghost, AGG) is None
    assert orphan_reply(ghost, AGG, MISSION_ORPHAN_REPLIES).handshake_status is HS.COMPLETE_ACK
    for status in (HS.BID, HS.ACK):
        reply = orphan_reply(HandshakeEnvelope("g", status, "i1", epoch=1), AGG)
        assert reply.handshake_status is HS.CANCEL and reply.target == "i1"


# ══ fuzz ═══════════════════════════════════════════════════════════════════

def invariants(agg, resp):
    holders = {}
    for name, r in resp.items():
        if r.active:
            holders.setdefault(r.active, []).append(name)
    for subject, names in holders.items():
        assert len(names) == 1, f"{subject} held by {names}"
    # one subject per receiver, aggregate side
    receivers = [hs.receiver for hs in agg.handshakes.values() if hs.receiver]
    assert len(receivers) == len(set(receivers)), f"receiver committed twice: {receivers}"
    assert not (set(agg.handshakes) & set(agg.elections)), "subject in both dicts"


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_mixed_modes_random_order(seed):
    rng = random.Random(seed)
    clk = Clock()
    agg = Agg(clk)
    resp = {f"i{k}": MissionResponder(f"i{k}", clock=clk,
                                      bid_fn=lambda h: Bid(h["t"])) for k in range(4)}
    net = Net(agg, resp, rng=rng)
    subjects = [f"s{k}" for k in range(6)]

    for _ in range(30):
        for subject in subjects:
            mode = rng.choice(["pooled", "directed", "election"])
            if mode == "pooled":
                agg.request(subject, payload=subject)
            elif mode == "directed":
                agg.request(subject, payload=subject, target=rng.choice(list(resp)))
            else:
                names = rng.sample(list(resp), rng.randint(1, 4))
                agg.elect(subject, {n: {"t": rng.randint(1, 9)} for n in names},
                          lowest_time, payload=subject)
        net.run()
        invariants(agg, resp)
        clk.advance(rng.choice([0, 0.5, 1, 3]))
        agg.tick(); net.run()
        if rng.random() < 0.4:
            held = [s for s, h in agg.handshakes.items() if h.receiver]
            if held:
                subject = rng.choice(held)
                receiver = agg.receiver_of(subject)
                if rng.random() < 0.5:
                    net.from_instance(resp[receiver].get_completed(subject))
                else:
                    agg.cancel(subject)
                net.run()
    net.run()
    invariants(agg, resp)

    clk.advance(60)
    for _ in range(5):
        agg.tick(); net.run()
    for r in resp.values():
        assert not r.reserved, f"leaked reservation: {list(r.reserved)}"
    assert agg.elections == {}, "election left open"


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_pooled_pairing_never_double_binds(seed):
    rng = random.Random(1000 + seed)
    clk = Clock()
    agg = Agg(clk)
    resp = {f"i{k}": MissionResponder(f"i{k}", clock=clk, max_reserved=1) for k in range(3)}
    net = Net(agg, resp, rng=rng)
    agents = [f"a{k}" for k in range(5)]

    for _ in range(25):
        for agent in agents:
            agg.request(agent, payload=agent)
        net.run()
        invariants(agg, resp)
        clk.advance(rng.choice([0.5, 1, 3]))
        agg.tick(); net.run()
    invariants(agg, resp)
    bound = [r.active for r in resp.values() if r.active]
    assert len(bound) == len(set(bound)) == 3


@pytest.mark.parametrize("seed", range(20))
def test_fuzz_with_message_loss_never_crashes_or_leaks(seed):
    rng = random.Random(5000 + seed)
    clk = Clock()
    agg = Agg(clk)
    resp = {f"i{k}": MissionResponder(f"i{k}", clock=clk, timeout=4.0,
                                      bid_fn=lambda h: Bid(h["t"])) for k in range(3)}
    net = Net(agg, resp, rng=rng, drop=0.15)
    for _ in range(30):
        subject = f"s{rng.randint(0, 4)}"
        names = rng.sample(list(resp), rng.randint(1, 3))
        agg.elect(subject, {n: {"t": rng.randint(1, 9)} for n in names},
                  lowest_time, payload=subject)
        net.run()
        invariants(agg, resp)
        clk.advance(rng.choice([0.5, 3]))
        agg.tick(); net.run()

    net.drop = 0.0
    clk.advance(60)
    for _ in range(8):
        agg.tick(); net.run()
        clk.advance(10)
    invariants(agg, resp)
    assert agg.elections == {}
