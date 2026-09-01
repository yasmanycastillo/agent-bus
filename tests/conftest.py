from __future__ import annotations

import os
import tempfile

import pytest

from agent_bus.reputation.database import Database


@pytest.fixture
async def tmp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, "test.db"))
        await db.initialize()
        yield db
        await db.close()
