"""Cooperative local Unix exclusion for automatic executors of one participant."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from agent_bus.config import load_config
from agent_bus.security import validate_id


class ExecutionBusy(RuntimeError):
    pass


class ExecutionGuard:
    """Hold one flock inode for a project/database/agent until execution ends.

    The file deliberately survives release; removing it allows simultaneous locks
    on different inodes. OS process death releases the lock, not the filename.
    """
    def __init__(self, agent_id: str, *, kind: str = "worker"):
        validate_id(agent_id)
        config = load_config()
        database = Path(config.database_path).resolve()
        identity = [config.bus.project_id, str(database), agent_id]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        self.path = database.parent / ".executor-locks" / f"{key}.lock"
        self.agent_id = agent_id
        self.kind = kind
        self._fd: int | None = None

    def __enter__(self):
        if self._fd is not None:
            raise RuntimeError("Execution guard already held")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("Executor lock must be a regular file owned by this user with mode 0600")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExecutionBusy(f"Automatic executor already active for agent '{self.agent_id}'") from exc
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "kind": self.kind, "agent_id": self.agent_id}).encode())
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, *_):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
