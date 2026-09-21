"""Tests for the xP providers: loading, validating and choosing the artifact.

No database. The HTTP case runs a throwaway local server on 127.0.0.1.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from pathlib import Path

import pytest

from optimizer.pool import (
    SEED_XP_PATH,
    ArtifactError,
    ArtifactXPProvider,
    FixtureXPProvider,
    SeasonMismatchError,
    provider_from_env,
    validate_artifact,
)


def artifact(**overrides) -> dict:
    """A minimal valid artifact: two players, two target gameweeks."""
    doc = {
        "format": 1,
        "season": "2026-27",
        "model_version": "test-0.1",
        "generated_at": "2026-09-20T05:00:00Z",
        "as_of_gw": 5,
        "target_gws": [6, 7],
        "ingest_run_id": 8,
        "players": [
            {
                "element_id": 430,
                "web_name": "Haaland",
                "team_fpl_id": 13,
                "element_type": 4,
                "now_cost_tenths": 156,
                "selected_by_percent": 61.2,
                "status": "a",
                "xp": {"6": 6.1, "7": 11.8},
                "xmins": {"6": 84.0, "7": 84.0},
                "n_fixtures": {"6": 1, "7": 2},
            },
            {
                "element_id": 12,
                "web_name": "Saka",
                "team_fpl_id": 1,
                "element_type": 3,
                "now_cost_tenths": 95,
                "selected_by_percent": 12.7,
                "status": "d",
                "xp": {"6": 0.0, "7": 5.2},
                "xmins": {"6": 0.0, "7": 80.0},
                "n_fixtures": {"6": 0, "7": 1},
            },
        ],
    }
    doc.update(overrides)
    return doc


def write(tmp_path: Path, doc: dict, name: str = "xp.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


# --- validate_artifact ----------------------------------------------------


def test_a_well_formed_artifact_has_no_problems():
    assert validate_artifact(artifact(), expected_season="2026-27") == []


def test_wrong_season_is_a_problem():
    problems = validate_artifact(artifact(season="2025-26"), expected_season="2026-27")
    assert any("2025-26" in p and "2026-27" in p for p in problems)


def test_a_float_price_is_refused():
    # 15.6 where 156 was meant is off by a factor of ten -- CLAUDE.md invariant 1.
    doc = artifact()
    doc["players"][0]["now_cost_tenths"] = 15.6
    problems = validate_artifact(doc)
    assert any("now_cost_tenths" in p for p in problems)


def test_missing_top_level_fields_are_named():
    doc = artifact()
    del doc["generated_at"]
    assert validate_artifact(doc) == ["missing top-level field 'generated_at'"]


def test_xp_must_cover_the_target_gameweeks():
    doc = artifact()
    del doc["players"][1]["xp"]["7"]
    problems = validate_artifact(doc)
    assert any("players[1].xp" in p for p in problems)


def test_non_finite_xp_is_refused():
    doc = artifact()
    doc["players"][0]["xp"]["6"] = float("nan")
    assert any("finite" in p for p in validate_artifact(doc))


def test_duplicate_element_ids_are_refused():
    doc = artifact()
    doc["players"][1]["element_id"] = 430
    assert any("duplicate element_id 430" in p for p in validate_artifact(doc))


# --- ArtifactXPProvider -----------------------------------------------------


def test_provider_loads_every_field_from_a_path(tmp_path):
    snap = ArtifactXPProvider(write(tmp_path, artifact()), expected_season="2026-27").snapshot(
        "2026-27"
    )
    assert snap.season == "2026-27"
    assert snap.model_version == "test-0.1"
    assert snap.generated_at == "2026-09-20T05:00:00Z"
    assert snap.as_of_gw == 5
    assert snap.target_gws == (6, 7)
    assert snap.ingest_run_id == 8
    assert snap.synthetic is False

    haaland = snap.players[430]
    assert haaland.price == 156 and isinstance(haaland.price, int)
    assert haaland.xp_by_gw == {6: 6.1, 7: 11.8}
    assert haaland.fixtures_by_gw == {6: 1, 7: 2}
    assert snap.players[12].status == "d"
    assert snap.fixtures_matrix([430, 12]) == {430: {6: 1, 7: 2}, 12: {6: 0, 7: 1}}


def test_provider_refuses_a_wrong_season_artifact(tmp_path):
    provider = ArtifactXPProvider(
        write(tmp_path, artifact(season="2025-26")), expected_season="2026-27"
    )
    with pytest.raises(ArtifactError, match="2025-26"):
        provider.snapshot("2026-27")


def test_provider_refuses_a_request_for_another_season(tmp_path):
    provider = ArtifactXPProvider(write(tmp_path, artifact()))
    with pytest.raises(SeasonMismatchError):
        provider.snapshot("2027-28")


def test_provider_refuses_a_malformed_artifact(tmp_path):
    path = tmp_path / "xp.json"
    path.write_text("{not json")
    with pytest.raises(ArtifactError, match="not valid JSON"):
        ArtifactXPProvider(path).snapshot("")


def test_provider_caches_and_does_not_reread(tmp_path):
    path = write(tmp_path, artifact())
    provider = ArtifactXPProvider(path)
    first = provider.snapshot("2026-27")
    path.unlink()  # held for the container's life -- the file is not read again
    assert provider.snapshot("2026-27") is first


def test_a_failed_load_is_not_cached(tmp_path):
    path = tmp_path / "late.json"
    provider = ArtifactXPProvider(path)
    with pytest.raises(ArtifactError):
        provider.snapshot("")
    write(tmp_path, artifact(), name="late.json")
    assert provider.snapshot("").model_version == "test-0.1"


def test_provider_loads_from_an_http_url(tmp_path):
    body = json.dumps(artifact()).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/xp.json"
        snap = ArtifactXPProvider(url, expected_season="2026-27").snapshot("2026-27")
        assert snap.source == url
        assert snap.players[430].web_name == "Haaland"
    finally:
        server.shutdown()


# --- provider_from_env --------------------------------------------------------


def test_url_wins_over_path(tmp_path):
    p = provider_from_env(
        {"OPTIMIZER_XP_URL": "https://example.invalid/xp.json", "OPTIMIZER_XP_PATH": "/x.json"},
        default_artifact=tmp_path / "none.json",
    )
    assert isinstance(p, ArtifactXPProvider)
    assert p.source == "https://example.invalid/xp.json"


def test_a_non_http_url_is_refused(tmp_path):
    with pytest.raises(ArtifactError):
        provider_from_env(
            {"OPTIMIZER_XP_URL": "file:///etc/passwd"}, default_artifact=tmp_path / "none.json"
        )


def test_explicit_path_is_an_artifact(tmp_path):
    path = write(tmp_path, artifact())
    p = provider_from_env({"OPTIMIZER_XP_PATH": str(path)}, default_artifact=tmp_path / "none.json")
    assert isinstance(p, ArtifactXPProvider)
    assert p.snapshot("2026-27").synthetic is False


def test_a_locally_published_artifact_beats_the_seed(tmp_path):
    default = write(tmp_path, artifact(), name="xp_artifact.local.json")
    p = provider_from_env({}, default_artifact=default)
    assert isinstance(p, ArtifactXPProvider)
    assert p.source == str(default)


def test_the_seed_is_the_last_resort_and_says_so(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="optimizer.pool"):
        p = provider_from_env({}, default_artifact=tmp_path / "none.json")
    assert isinstance(p, FixtureXPProvider)
    assert p.synthetic is True
    assert "SYNTHETIC SEED" in caplog.text
    snap = p.snapshot("2026-27")
    assert snap.synthetic is True
    assert "SYNTHETIC SEED" in snap.source


def test_pointing_the_path_at_the_seed_is_still_synthetic(tmp_path):
    p = provider_from_env(
        {"OPTIMIZER_XP_PATH": str(SEED_XP_PATH)}, default_artifact=tmp_path / "none.json"
    )
    assert isinstance(p, FixtureXPProvider)
    assert p.snapshot("").synthetic is True


def test_expected_season_comes_from_the_environment(tmp_path):
    path = write(tmp_path, artifact())
    p = provider_from_env(
        {"OPTIMIZER_XP_PATH": str(path), "OPTIMIZER_SEASON": "2027-28"},
        default_artifact=tmp_path / "none.json",
    )
    with pytest.raises(ArtifactError, match="2027-28"):
        p.snapshot("")
