from __future__ import annotations



from agent_bus.reputation.database import Database
from agent_bus.reputation.endorsement import EndorsementTracker
from agent_bus.reputation.manager import MetricsUpdate, ReputationManager


async def test_initial_score(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    score = await rep.get_score("unknown_agent")
    assert score == 0.5


async def test_update_metrics(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    await rep.update("claude", MetricsUpdate(accuracy_delta=0.3, honesty_delta=0.1, energy_delta=0.2))
    score = await rep.get_score("claude")
    assert score > 0.5


async def test_score_formula(tmp_db: Database):
    rep = ReputationManager(tmp_db, accuracy_weight=0.5, honesty_weight=0.3, energy_weight=0.2)
    await rep.update("claude", MetricsUpdate(accuracy_delta=0.5))
    # accuracy=1.0, honesty=0.5, energy=0.5
    # score = 1.0*0.5 + 0.5*0.3 + 0.5*0.2 = 0.5 + 0.15 + 0.1 = 0.75
    score = await rep.get_score("claude")
    assert abs(score - 0.75) < 0.01


async def test_score_clamped(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    await rep.update("claude", MetricsUpdate(accuracy_delta=10.0, honesty_delta=10.0, energy_delta=10.0))
    score = await rep.get_score("claude")
    assert score <= 1.0


async def test_get_all_scores(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    await rep.update("a", MetricsUpdate(accuracy_delta=0.2))
    await rep.update("b", MetricsUpdate(accuracy_delta=0.4))
    scores = await rep.get_all_scores()
    assert "a" in scores
    assert "b" in scores
    assert scores["b"] > scores["a"]


async def test_leaderboard(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    await rep.update("low", MetricsUpdate(accuracy_delta=0.1))
    await rep.update("high", MetricsUpdate(accuracy_delta=0.5))
    await rep.update("mid", MetricsUpdate(accuracy_delta=0.3))
    lb = await rep.get_leaderboard()
    assert lb[0].agent_id == "high"
    assert lb[1].agent_id == "mid"
    assert lb[2].agent_id == "low"


async def test_endorsement_updates_both_agents(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    tracker = EndorsementTracker(tmp_db, rep)

    await rep.update("claude", MetricsUpdate())
    await rep.update("codex", MetricsUpdate())

    await tracker.endorse("claude", "codex", weight=1.0)

    endorsements = await tracker.get_endorsements("codex")
    assert len(endorsements) == 1
    assert endorsements[0]["from_agent"] == "claude"


async def test_endorsements_given(tmp_db: Database):
    rep = ReputationManager(tmp_db)
    tracker = EndorsementTracker(tmp_db, rep)

    await rep.update("claude", MetricsUpdate())
    await rep.update("codex", MetricsUpdate())

    await tracker.endorse("claude", "codex")
    given = await tracker.get_endorsements_given("claude")
    assert len(given) == 1
    assert given[0]["to_agent"] == "codex"
