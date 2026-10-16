"""Private session state with shared Firestore leases and a local-memory adapter."""

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import RLock
from uuid import uuid4

from app.config import get_settings


class SessionSeatMismatch(ValueError):
    pass


class SessionBusy(RuntimeError):
    pass


def new_session(slug):
    return {"constituency_id": slug, "history": [], "pending_state": None,
            "voter_context": {}, "diagnostics": []}


class MemorySessions:
    """Local adapter, with the same commit-on-success behavior as shared storage."""
    def __init__(self):
        self.records = {}
        self.locks = {}
        self.guard = RLock()

    def clear(self):
        self.records.clear()
        self.locks.clear()

    @contextmanager
    def turn(self, uid, sid, slug):
        key = (uid, sid)
        with self.guard:
            lock = self.locks.setdefault(key, RLock())
        if not lock.acquire(blocking=False):
            raise SessionBusy("A previous turn is still running")
        try:
            state = self.read(uid, sid) or new_session(slug)
            if state["constituency_id"] != slug:
                raise SessionSeatMismatch("Session belongs to another constituency")
            yield state
            self.records[key] = copy.deepcopy(state)
        finally:
            lock.release()

    def read(self, uid, sid):
        return copy.deepcopy(self.records.get((uid, sid)))


class FirestoreSessions:
    """Keep slow model calls outside transactions; leases protect concurrent turns."""
    def __init__(self, client):
        self.client = client

    def document(self, uid, sid):
        key = hashlib.sha256(json.dumps([uid, sid], separators=(",", ":")).encode()).hexdigest()
        return self.client.collection("booth_agent_sessions").document(key)

    @staticmethod
    def decode(value, now):
        if not value or value.get("expires_at", now) <= now:
            return None
        return json.loads(value["state_json"])

    def read(self, uid, sid):
        value = self.document(uid, sid).get().to_dict()
        return self.decode(value, datetime.now(timezone.utc))

    @contextmanager
    def turn(self, uid, sid, slug):
        from google.cloud import firestore

        document = self.document(uid, sid)
        owner = uuid4().hex

        @firestore.transactional
        def acquire(transaction):
            now = datetime.now(timezone.utc)
            value = document.get(transaction=transaction).to_dict() or {}
            state = self.decode(value, now) or new_session(slug)
            if state["constituency_id"] != slug:
                raise SessionSeatMismatch("Session belongs to another constituency")
            if value.get("lease_until", now) > now:
                raise SessionBusy("A previous turn is still running")
            transaction.set(document, {"state_json": json.dumps(state, ensure_ascii=False),
                            "lease_owner": owner, "lease_until": now + timedelta(minutes=15),
                            "expires_at": now + timedelta(hours=get_settings().session_ttl_hours)})
            return state

        state = acquire(self.client.transaction())

        @firestore.transactional
        def release(transaction, save):
            value = document.get(transaction=transaction).to_dict() or {}
            if value.get("lease_owner") != owner:
                if save:
                    raise SessionBusy("Session lease was replaced; retry the turn")
                return
            now = datetime.now(timezone.utc)
            updates = {"lease_owner": None, "lease_until": now}
            if save:
                encoded = json.dumps(state, ensure_ascii=False, default=str)
                if len(encoded.encode()) > 800000:
                    raise ValueError("Session state exceeds its storage budget")
                updates.update(state_json=encoded, expires_at=now + timedelta(hours=get_settings().session_ttl_hours))
            transaction.update(document, updates)

        try:
            yield state
            release(self.client.transaction(), True)
        except Exception:
            # Do not replace a useful turn error if releasing the lease also fails.
            try:
                release(self.client.transaction(), False)
            except Exception:
                pass
            raise


@lru_cache
def get_store():
    settings = get_settings()
    mode = settings.session_store
    if mode == "auto":
        mode = "firestore" if os.environ.get("K_SERVICE") else "memory"
    if mode == "memory":
        return MemorySessions()
    from google.cloud import firestore

    return FirestoreSessions(firestore.Client(project=settings.session_project, database=settings.session_firestore_database))
