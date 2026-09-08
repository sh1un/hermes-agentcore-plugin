"""Single-process connection lifecycle. Stores metadata only, never OAuth data.

The caller must authenticate the adapter before constructing SlackIdentity.
This type validates ID syntax, not Slack authenticity. One Store owns one DB.
"""

from dataclasses import dataclass
from pathlib import Path
import os
import fcntl
import re
import sqlite3
import threading
from collections.abc import Callable


class ConnectionRejected(Exception):
    """Fixed, non-sensitive lifecycle failure."""


@dataclass(frozen=True)
class SlackIdentity:
    workspace: str
    member: str

    def __post_init__(self):
        if not re.fullmatch(r"T[A-Z0-9]+", self.workspace):
            raise ValueError("Invalid Slack workspace ID")
        if not re.fullmatch(r"[UW][A-Z0-9]+", self.member):
            raise ValueError("Invalid Slack member ID")

    @property
    def subject(self):
        return f"slack:{self.workspace}:{self.member}"


@dataclass(frozen=True)
class Connection:
    state: str
    generation: int


class Store:
    def __init__(self, path: Path, namespace: str, connections: frozenset[str]):
        if not namespace or not connections:
            raise ValueError("Namespace and connection allowlist are required")
        self.namespace = namespace
        self.connections = frozenset(connections)
        self._lock = threading.RLock()
        # Caller supplies a dedicated, trusted directory. Never store tokens here.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._owner_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._owner_fd)
            raise RuntimeError("Connection store already has an owner") from None
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS connections (
            namespace TEXT NOT NULL, subject TEXT NOT NULL, name TEXT NOT NULL,
            state TEXT NOT NULL, generation INTEGER NOT NULL,
            PRIMARY KEY(namespace, subject, name))""")
        # In-memory OAuth sessions cannot survive a restart.
        with self.db:
            self.db.execute("""UPDATE connections SET state='disconnected',
                generation=generation+1 WHERE namespace=? AND state='connecting'""",
                (namespace,))

    def _key(self, identity: SlackIdentity, name: str):
        if not isinstance(identity, SlackIdentity) or name not in self.connections:
            raise ConnectionRejected("Unknown identity or connection")
        return (self.namespace, identity.subject, name)

    def status(self, identity: SlackIdentity, name: str) -> Connection:
        with self._lock:
            row = self.db.execute(
                "SELECT state,generation FROM connections WHERE namespace=? AND subject=? AND name=?",
                self._key(identity, name),
            ).fetchone()
            return Connection(*row) if row else Connection("disconnected", 0)

    def _transition(self, identity, name, state):
        with self._lock, self.db:
            generation = self.status(identity, name).generation + 1
            self.db.execute("""INSERT INTO connections VALUES (?,?,?,?,?)
                ON CONFLICT(namespace,subject,name) DO UPDATE SET
                state=excluded.state,generation=excluded.generation""",
                (*self._key(identity, name), state, generation))
            return Connection(state, generation)

    def begin(self, identity: SlackIdentity, name: str) -> Connection:
        return self._transition(identity, name, "connecting")

    def complete_verified(self, identity: SlackIdentity, name: str, generation: int):
        """Only a trusted callback/confirmation service may call this method.

        This is not OAuth verification and must never be exposed as a model tool.
        """
        with self._lock, self.db:
            changed = self.db.execute("""UPDATE connections SET state='connected'
                WHERE namespace=? AND subject=? AND name=?
                AND generation=? AND state='connecting'""",
                (*self._key(identity, name), generation)).rowcount
            if changed != 1:
                raise ConnectionRejected("Stale or invalid authorization session")

    def disconnect(self, identity: SlackIdentity, name: str) -> Connection:
        """Local dispatch gate only. Does not claim provider token revocation."""
        return self._transition(identity, name, "disconnected")

    def dispatch(self, identity: SlackIdentity, name: str, generation: int,
                 operation: Callable[[], object]):
        """Serialize admission and operation with disconnect in this process.

        Operations must have bounded timeouts. Disconnect waits for an admitted
        operation, which cannot be recalled. No new operation starts after it returns.
        """
        with self._lock:
            current = self.status(identity, name)
            if current != Connection("connected", generation):
                raise ConnectionRejected("Connection required")
            return operation()

    def close(self):
        with self._lock:
            self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
