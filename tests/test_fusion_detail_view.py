"""The request detail page must say what a Fusion task actually did.

A fusion task does not run on `tasks.model` — that column is the author's
candidate-1 pick. Rendering it alone (the old behaviour) claimed a two-model
arbitrated run was a single gemma call. `_fusion_view` builds the panel's view of
the configuration plus what the event log proves actually happened.
"""

from __future__ import annotations

import json

import pytest

from lucent.web.routes.requests_routes import _fusion_view

ARBITER = {
    "model": "nimble:latest",
    "questions": [
        {"name": "a", "type": "noul", "instructions": "Q1?"},
        {"name": "b", "type": "noul", "instructions": "Q2?"},
    ],
}


def _task(*, fusion_config=None, events=(), task_kind="fusion", model="model-a"):
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "task_kind": task_kind,
        "model": model,
        "fusion_config": fusion_config,
        "events": list(events),
    }


def _event(event_type, metadata=None):
    return {"event_type": event_type, "metadata": metadata or {}}


def _completed_metadata(
    *,
    mode="best_of_n",
    candidates,
    winner_index,
    all_candidates=None,
    arbiter_model="nimble:latest",
    totals=None,
    refine=None,
    synthesis=None,
    degraded=False,
    threshold_failed=False,
):
    payload = {
        "mode": mode,
        "candidates": candidates,
        "all_candidates": all_candidates
        if all_candidates is not None
        else [
            {"index": c["index"], "model": c["model"], "status": "completed"}
            for c in candidates
        ],
        "arbiter": {"model": arbiter_model, "input_tokens": 1, "output_tokens": 0},
        "winner": {
            "index": winner_index,
            "model": next(c["model"] for c in candidates if c["index"] == winner_index),
            "aggregate": 0.9,
        },
        "refine": refine or {"passes": 0, "taken": False},
        "degraded": degraded,
        "threshold_failed": threshold_failed,
        "totals": totals or {"total_tokens": 1000},
    }
    if synthesis:
        payload["synthesis"] = synthesis
    return payload


BEST_OF_N_CONFIG = {
    "mode": "best_of_n",
    "candidates": {"models": ["model-a", "model-b"]},
    "arbiter": ARBITER,
    "refine": {"model": "model-a", "max_refine_passes": 1},
}


# ── non-fusion tasks must be untouched ──────────────────────────────────


def test_plain_agent_task_gets_no_fusion_view():
    assert _fusion_view(_task(task_kind="agent", fusion_config=None)) is None


def test_explicit_fusion_kind_without_config_still_gets_a_view():
    """task_kind='fusion' is authoritative even if the config is unreadable."""
    view = _fusion_view(_task(fusion_config=None, events=[_event("fusion_dispatched")]))
    assert view is not None
    assert view["ran"] is True


# ── configuration is read, including the JSON-string form ───────────────


def test_reads_mode_models_and_arbiter():
    view = _fusion_view(_task(fusion_config=BEST_OF_N_CONFIG))
    assert view["mode"] == "best_of_n"
    assert view["mode_label"] == "Best of N"
    assert view["configured_models"] == ["model-a", "model-b"]
    assert view["arbiter_model"] == "nimble:latest"
    assert view["arbiter_question_count"] == 2
    assert view["refine_model"] == "model-a"


def test_tolerates_double_encoded_config():
    """The pool's jsonb codec double-encodes these columns; read either shape."""
    view = _fusion_view(_task(fusion_config=json.dumps(BEST_OF_N_CONFIG)))
    assert view["mode"] == "best_of_n"
    assert view["arbiter_question_count"] == 2


def test_falls_back_to_execution_config():
    view = _fusion_view(_task(fusion_config=None, events=[_event("fusion_dispatched")]))
    assert view is not None
    view = _fusion_view(
        {**_task(fusion_config=None), "execution_config": BEST_OF_N_CONFIG}
    )
    assert view["configured_models"] == ["model-a", "model-b"]


# ── what actually ran ───────────────────────────────────────────────────


def test_best_of_n_shows_every_candidate_and_the_winner():
    metadata = _completed_metadata(
        candidates=[
            {"index": 1, "model": "model-a", "aggregate": 0.97, "scores": {"a": 0.9}},
            {"index": 2, "model": "model-b", "aggregate": 0.93, "scores": {"a": 0.8}},
        ],
        winner_index=1,
    )
    view = _fusion_view(
        _task(
            fusion_config=BEST_OF_N_CONFIG,
            events=[_event("fusion_dispatched"), _event("fusion_completed", metadata)],
        )
    )
    assert view["ran"] is True
    assert view["configured"] is True
    assert view["candidate_count"] == 2
    models = [c["model"] for c in view["candidates"]]
    assert models == ["model-a", "model-b"]
    assert [c["winner"] for c in view["candidates"]] == [True, False]
    assert view["arbiter_model"] == "nimble:latest"
    assert view["total_tokens"] == 1000


def test_winner_matches_by_index_not_model_id():
    """A refine pass reuses a candidate's model id; only one row is the winner."""
    candidates = [
        {"index": 0, "model": "model-a", "aggregate": 0.99, "scores": {}},
        {"index": 1, "model": "model-a", "aggregate": 0.97, "scores": {}},
        {"index": 2, "model": "model-b", "aggregate": 0.95, "scores": {}},
    ]
    metadata = _completed_metadata(
        candidates=candidates, winner_index=0, refine={"passes": 1, "taken": True}
    )
    view = _fusion_view(
        _task(
            fusion_config=BEST_OF_N_CONFIG,
            events=[_event("fusion_completed", metadata)],
        )
    )
    winners = [c for c in view["candidates"] if c["winner"]]
    assert len(winners) == 1
    assert winners[0]["index"] == 0
    assert winners[0]["role"] == "refine"
    assert view["refine_taken"] is True
    # Only the authored candidates count as candidates, not the refine pass.
    assert view["candidate_count"] == 2


def test_synthesis_run_labels_the_derived_session():
    config = {
        "mode": "synthesize",
        "candidates": {"models": ["model-a", "model-b"]},
        "synthesis": {"model": "model-synth"},
    }
    metadata = _completed_metadata(
        mode="synthesize",
        candidates=[
            {"index": 0, "model": "model-synth", "aggregate": 0.99, "scores": {}},
            {"index": 1, "model": "model-a", "aggregate": 0.98, "scores": {}},
            {"index": 2, "model": "model-b", "aggregate": 0.97, "scores": {}},
        ],
        winner_index=0,
        synthesis={"model": "model-synth", "input_tokens": 10, "output_tokens": 5},
    )
    view = _fusion_view(
        _task(fusion_config=config, events=[_event("fusion_completed", metadata)])
    )
    assert view["mode_label"] == "Synthesis"
    assert view["synthesis_model"] == "model-synth"
    roles = {c["index"]: c["role"] for c in view["candidates"]}
    assert roles == {0: "synthesis", 1: "candidate", 2: "candidate"}
    assert view["candidate_count"] == 2


def test_failed_candidate_still_appears():
    """A candidate that errored is part of the story, not hidden."""
    metadata = _completed_metadata(
        candidates=[{"index": 1, "model": "model-a", "aggregate": 0.9, "scores": {}}],
        all_candidates=[
            {"index": 1, "model": "model-a", "status": "completed"},
            {"index": 2, "model": "model-b", "status": "failed", "error": "timeout"},
        ],
        winner_index=1,
        degraded=True,
    )
    view = _fusion_view(
        _task(fusion_config=BEST_OF_N_CONFIG, events=[_event("fusion_completed", metadata)])
    )
    statuses = {c["index"]: c["status"] for c in view["candidates"]}
    assert statuses == {1: "completed", 2: "failed"}
    assert view["degraded"] is True


# ── the latest attempt is the one described ─────────────────────────────


def test_latest_attempt_wins_over_earlier_fallback():
    """A retried task must not mix an old fallback with a new fusion result.

    Reproduces a real row: attempts 1-2 dispatched with the flag off
    (`fusion_disabled`), then a later attempt genuinely ran fusion. Showing the
    fallback banner next to a scored candidate table contradicted itself.
    """
    metadata = _completed_metadata(
        candidates=[
            {"index": 1, "model": "model-a", "aggregate": 0.99, "scores": {}},
            {"index": 2, "model": "model-b", "aggregate": 0.98, "scores": {}},
        ],
        winner_index=1,
    )
    view = _fusion_view(
        _task(
            fusion_config=BEST_OF_N_CONFIG,
            events=[
                _event("fusion_disabled"),
                _event("fusion_disabled"),
                _event("fusion_dispatched"),
                _event("fusion_completed", metadata),
            ],
        )
    )
    assert view["fallback"] is None
    assert view["fallback_label"] is None
    assert view["candidate_count"] == 2


def test_fallback_after_fusion_clears_the_old_result():
    """The reverse order: the last attempt fell back, so show only that."""
    metadata = _completed_metadata(
        candidates=[
            {"index": 1, "model": "model-a", "aggregate": 0.99, "scores": {}},
            {"index": 2, "model": "model-b", "aggregate": 0.98, "scores": {}},
        ],
        winner_index=1,
    )
    view = _fusion_view(
        _task(
            fusion_config=BEST_OF_N_CONFIG,
            events=[
                _event("fusion_dispatched"),
                _event("fusion_completed", metadata),
                _event("fusion_disabled"),
            ],
        )
    )
    assert view["ran"] is False
    assert view["fallback"] == "fusion_disabled"
    assert view["candidates"] == []
    assert view["winner_model"] is None


def test_fallback_is_explained_in_plain_language():
    view = _fusion_view(
        _task(fusion_config=BEST_OF_N_CONFIG, events=[_event("fusion_unsupported")])
    )
    assert view["fallback"] == "fusion_unsupported"
    assert "single model" in view["fallback_label"].lower() or "one model" in view[
        "fallback_label"
    ].lower()


# ── authored-but-never-run configurations ───────────────────────────────


def test_queued_task_lists_the_authored_models():
    """Before dispatch there is no event data, but the page can still say
    which models were meant to run."""
    view = _fusion_view(_task(fusion_config=BEST_OF_N_CONFIG))
    assert view["ran"] is False
    assert [c["model"] for c in view["candidates"]] == ["model-a", "model-b"]
    assert all(c["status"] == "not run" for c in view["candidates"])
    assert view["candidate_count"] == 2


def test_count_based_config_has_no_model_list_before_it_runs():
    config = {
        "mode": "best_of_n",
        "candidates": {"count": 2},
        "arbiter": ARBITER,
    }
    view = _fusion_view(_task(fusion_config=config))
    assert view["configured_models"] == []
    assert view["configured_count"] == 2
    assert view["candidates"] == []


def test_unparseable_config_does_not_crash():
    view = _fusion_view(_task(fusion_config="{not json"))
    # task_kind='fusion' keeps the panel, but it knows nothing about the config.
    assert view is not None
    assert view["configured"] is False
    assert view["mode_label"] == "Fusion"
