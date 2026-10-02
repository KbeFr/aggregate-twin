"""
handshake_coordinator.py

Aggregate-side owner of one KIND of subject: agents (pairing) or missions.

Everything the two kinds share lives here -- open a conversation, route replies,
tick timeouts, retire finished ones -- because with a single handshake they really
are the same bookkeeping. Everything they do NOT share (what a link means, which
topic it goes on, what happens when it is released) is a hook the subclass fills in.

Two dicts, both homogeneous:

    handshakes   subject -> HandshakeInitiator, the conversation that owns the subject
    elections    subject -> HandshakeElection, a bidding round in front of one

A subject is in at most one of them. An election is transient: as soon as it picks a
winner, that winner's initiator moves into `handshakes` and the election is dropped,
so everything past the award is the same object and the same code whether the subject
was pooled, directed or auctioned.

Sans-I/O: outbound envelopes are appended to `self.outbox` and the owner flushes
them. A coordinator can therefore be tested with no transport, no twin and no bus.
"""
from __future__ import annotations

import logging
import time
from math import inf
from typing import Any, Callable

from core_msgs.instance_aggregate.handshake import (
    HandshakeAction, HandshakeElection, HandshakeEnvelope, HandshakeInitiator,
    HandshakeResult, orphan_reply,
)
from core_msgs.instance_aggregate.handshake_shared import HandshakeStatus, InitiatorState

DEFAULT_BID_TIMEOUT = 2.0


class HandshakeCoordinator:
    """One conversation per subject, whichever way it started."""

    #: What this coordinator calls its subjects, for logs only.
    kind = "subject"

    #: The initiator this kind of subject talks through. A kind with statuses beyond
    #: the base flow subclasses HandshakeInitiator and names it here.
    initiator_cls: type[HandshakeInitiator] = HandshakeInitiator

    #: Extra answers for an instance talking about a subject we no longer track,
    #: on top of handshake.ORPHAN_REPLIES.
    orphan_replies: dict[HandshakeStatus, HandshakeStatus] = {}

    def __init__(self,
                 aggregate_name: str,
                 clock: Callable[[], float] = time.time,
                 timeout: float = DEFAULT_BID_TIMEOUT,
                 logger: logging.Logger | None = None,
                 ) -> None:
        self.aggregate_name = aggregate_name
        self.clock = clock
        self.timeout = timeout

        # cooldown between requests to same sender
        self.request_cooldown = 20
        self.request_register: dict[tuple[str, str], float] = {}

        self.logger = logger or logging.getLogger(self.__class__.__name__)

        self.handshakes: dict[str, HandshakeInitiator] = {}
        self.elections: dict[str, HandshakeElection] = {}
        self.epochs: dict[str, int] = {}        # subject -> last epoch used
        self.outbox: list[HandshakeEnvelope] = []

    # ------------------------------------------------------------------
    # Starting a conversation
    # ------------------------------------------------------------------

    def request(self, subject: str, payload: Any = None,
                target: str | None = None) -> bool:
        """Pooled (target=None) or directed (target=instance). False if the subject is
        already in flight or the target is spoken for, so callers may fire this on
        every tick."""
        if self.in_flight(subject):
            return False
        if target is not None and target in self.engaged_instances():
            self.logger.debug("%s=%s not requested: %s is engaged", self.kind, subject, target)
            return False

        # Cooldown for request so the agent doesnt jump between
        if target is not None:
            key = (subject, target)
            if self.clock() - self.request_register.get(key, 0.0) < self.request_cooldown:
                self.logger.debug("%s=%s not requested: %s on cooldown for this subject", self.kind, subject, target)
                return False
            self.request_register[key] = self.clock()

        handshake = self.initiator_cls(subject, self.aggregate_name, timeout=self.timeout,
                                       clock=self.clock, epoch=self.epochs.get(subject, 0))

        self.handshakes[subject] = handshake
        self.outbox.append(handshake.request(payload=payload, target=target))
        self.epochs[subject] = handshake.epoch
        self.logger.debug("%s=%s requested (target=%s)", self.kind, subject, target)
        return True

    def elect(self, subject: str, hints: dict[str, Any], get_winner_fn,
              payload: Any = None) -> bool:
        """Ask several instances to bid. Candidates already engaged elsewhere are
        dropped first: they would only answer NACK a round trip later."""
        if self.in_flight(subject):
            return False
        engaged = self.engaged_instances()
        hints = {inst: hint for inst, hint in hints.items() if inst not in engaged}
        if not hints:
            self.logger.debug("%s=%s not elected: no free candidate", self.kind, subject)
            return False

        election = HandshakeElection(subject, self.aggregate_name, hints, get_winner_fn,
                                     payload=payload, timeout=self.timeout, clock=self.clock,
                                     epoch=self.epochs.get(subject, 0),
                                     initiator_cls=self.initiator_cls)
        self.elections[subject] = election
        self._apply(election.open(), subject)
        self.epochs[subject] = election.epoch
        if election.resolved:                   # everyone refused on the spot
            self._settle(subject)
        return True

    def cancel(self, subject: str, reason: str = "cancelled by aggregate") -> None:
        election = self.elections.get(subject)
        if election is not None:
            self._apply(election.cancel(reason), subject)
            self._settle(subject)               # abandoned: nothing survives the round
            return

        handshake = self.handshakes.get(subject)
        if handshake is None:
            self.logger.warning("cannot cancel %s=%s: nothing in flight", self.kind, subject)
            return
        if self.releasing(subject):
            return                              # already on its way out
        self._apply(handshake.cancel(reason), subject)
        self._drop_if_unheld(subject, handshake)

    def forget(self, subject: str) -> None:
        """The subject itself is gone, not just this attempt at it. Drops the epoch
        that would otherwise be kept to recognise stale replies."""
        self.epochs.pop(subject, None)

    # ------------------------------------------------------------------
    # Inbound and time
    # ------------------------------------------------------------------

    def route(self, env: HandshakeEnvelope) -> None:
        if not isinstance(env, HandshakeEnvelope):
            self.logger.warning("non-envelope for %s: %r", self.kind, type(env).__name__)
            return
        if env.sender == self.aggregate_name:            # our own echo on an inout topic
            return

        election = self.elections.get(env.id)
        if election is not None:
            self._apply(election.handle(env), env.id)
            if election.resolved:
                self._settle(env.id)
            return

        handshake = self.handshakes.get(env.id)
        if handshake is None:
            # Nothing here for that subject: tell the instance to let go, or it stays
            # stuck on something we already forgot.
            reply = orphan_reply(env, self.aggregate_name, self.orphan_replies)
            if reply is not None:
                self.outbox.append(reply)
            return

        self._apply(handshake.handle(env), env.id)
        if handshake.done:
            self._retire(env.id)

    def tick(self) -> None:
        for subject, election in list(self.elections.items()):
            self._apply(election.tick(), subject)
            if election.resolved:
                self._settle(subject)

        for subject, handshake in list(self.handshakes.items()):
            self._apply(handshake.tick(), subject)
            if handshake.done:
                self._retire(subject)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def in_flight(self, subject: str) -> bool:
        return subject in self.handshakes or subject in self.elections

    def receiver_of(self, subject: str) -> str | None:
        handshake = self.handshakes.get(subject) or self.elections.get(subject)
        return handshake.receiver if handshake else None

    def hint_of(self, subject: str) -> Any:
        """What we planned for whoever ended up with the subject. None until the
        conversation settled on one instance, and None when there was no bidding."""
        handshake = self.handshakes.get(subject)
        return handshake.hint if handshake else None

    def committed_receivers(self) -> dict[str, str]:
        """{instance: subject} for everything awarded, active or being released."""
        return {hs.receiver: subject for subject, hs in self.handshakes.items()
                if hs.receiver}

    def engaged_instances(self) -> set[str]:
        """Every instance that may still be holding a reservation or a subject for us,
        bidders included. Wider than `committed_receivers`, which is why it is what
        the start-a-conversation guards use."""
        engaged: set[str] = set()
        for handshake in (*self.handshakes.values(), *self.elections.values()):
            engaged |= handshake.engaged
        return engaged

    def subjects_on(self, instance: str) -> list[str]:
        """What this instance is engaged on. For evicting a receiver that died."""
        return [subject for subject, hs in
                (*self.handshakes.items(), *self.elections.items())
                if instance in hs.engaged]

    def releasing(self, subject: str) -> bool:
        """Already on its way out. Cancelling again would keep resetting the retry
        clock, so a dead instance would never reach the give-up point."""
        handshake = self.handshakes.get(subject)
        return handshake is not None and handshake.state is InitiatorState.CANCELLED

    # ------------------------------------------------------------------
    # Hooks: what a subject actually IS
    # ------------------------------------------------------------------

    def on_link(self, subject: str, receiver: str) -> None:
        """The instance holds it now."""

    def on_release(self, subject: str, receiver: str | None) -> None:
        """It was given back, cancelled, or the holder died."""

    def on_complete(self, subject: str, receiver: str | None) -> None:
        """Finished successfully. Only kinds that add a completion leg do this; the
        default treats it as a release so a subclass cannot silently lose the event."""
        self.on_release(subject, receiver)

    def on_retire(self, subject: str) -> None:
        """The handshake object is being dropped. Last chance to reconcile domain
        state with the fact that nothing is in flight for this subject any more."""

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply(self, result: HandshakeResult, subject: str) -> None:
        self.outbox.extend(env for env in result.out if env is not None)
        if result.action is HandshakeAction.DO_NOTHING:
            return
        target = result.subject or subject
        if result.action is HandshakeAction.LINK_SUBJECT:
            self.on_link(target, result.receiver)
        elif result.action is HandshakeAction.COMPLETE_SUBJECT:
            self.on_complete(target, result.receiver)
        else:
            self.on_release(target, result.receiver)

    def _settle(self, subject: str) -> None:
        """The bidding round is over. A winner carries the subject on as a plain
        initiator; an abandoned round leaves nothing behind."""
        election = self.elections.pop(subject, None)
        if election is None or election.winner is None:
            self._retire(subject)
            return
        self.handshakes[subject] = election.winner
        self.logger.debug("%s=%s awarded to %s", self.kind, subject, election.winner.receiver)

    def _retire(self, subject: str) -> None:
        self.handshakes.pop(subject, None)
        self.elections.pop(subject, None)
        self.on_retire(subject)

    def _drop_if_unheld(self, subject: str, handshake) -> None:
        """Nobody holds the subject, so there is no CANCEL_ACK to wait for. Keeping
        the object would block the next attempt."""
        if handshake.done or handshake.receiver is None:
            self._retire(subject)