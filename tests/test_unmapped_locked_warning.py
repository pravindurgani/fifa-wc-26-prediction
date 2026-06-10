"""
Tests for the unmapped-LOCKED-fixture warning emitted by fetch_results.py.

When the provider returns a fixture that is already FT/AET/PEN but does NOT
appear in data/live/provider_fixture_map.json (and the schedule fallback also
fails to resolve it), the fetcher must append an `unmapped_locked_fixture`
warning to the module-level `_FETCH_WARNINGS` list. The orchestrator
(scripts/live/run_live_update.py) reads results_2026.json's `warnings` array
via get_results_warnings() and merges entries into live_state.warnings, so
this turns a silent stall (locking stops at the last mapped match) into a
visible banner.

Tests assert:
  - LOCKED unmapped FT/AET/PEN fixtures produce a warning.
  - SCHEDULED / LIVE unmapped fixtures do NOT produce a warning.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import fetch_results  # noqa: E402


def _make_fixture(*, fixture_id: int, status_short: str, status_long: str,
                  home: str, away: str, date: str,
                  gh: int | None, ga: int | None) -> dict:
    """Build a single API-Football-shaped fixture dict."""
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "status": {"short": status_short, "long": status_long, "elapsed": 90},
        },
        "teams": {
            "home": {"name": home},
            "away": {"name": away},
        },
        "goals": {"home": gh, "away": ga},
    }


def _patch_http(monkeypatch, response: list[dict]) -> None:
    """Stub http_get_json so fetch_api_football returns our crafted payload."""
    def fake_http_get_json(url, headers, timeout=15, retries=3):
        return {"response": response, "errors": [], "results": len(response)}
    monkeypatch.setattr(fetch_results, "http_get_json", fake_http_get_json)


def _stub_fixture_map(monkeypatch) -> None:
    """Force load_fixture_map() to return {} so nothing resolves via the map."""
    monkeypatch.setattr(fetch_results, "load_fixture_map", lambda: {})


@pytest.fixture(autouse=True)
def _clear_fetch_warnings():
    """Each test starts with an empty _FETCH_WARNINGS list."""
    fetch_results._FETCH_WARNINGS.clear()
    yield
    fetch_results._FETCH_WARNINGS.clear()


def test_locked_unmapped_fixture_emits_warning(monkeypatch):
    """An FT fixture with no map entry and no schedule fallback → warning."""
    # France vs Spain on 2026-07-05 is intentionally NOT in the group stage
    # schedule (group stage finishes 2026-06-27), so the date+team fallback
    # will not resolve it either. With load_fixture_map stubbed to {}, this
    # is forced into the unmapped[] branch with a LOCKED status.
    payload = [
        _make_fixture(
            fixture_id=99999,
            status_short="FT", status_long="Match Finished",
            home="France", away="Spain",
            date="2026-07-05T20:00:00+00:00",
            gh=1, ga=2,
        ),
    ]
    _patch_http(monkeypatch, payload)
    _stub_fixture_map(monkeypatch)

    out = fetch_results.fetch_api_football(api_key="test", dry_run=False)

    # The fixture should NOT be in the returned matches (unmapped → skipped).
    assert out == [], f"unmapped fixture leaked into output: {out!r}"

    # …but it SHOULD have emitted exactly one unmapped_locked_fixture warning.
    warns = [w for w in fetch_results._FETCH_WARNINGS
             if w.get("type") == "unmapped_locked_fixture"]
    assert len(warns) == 1, f"expected 1 warning, got {len(warns)}: {fetch_results._FETCH_WARNINGS!r}"
    msg = warns[0]["message"]
    assert "99999" in msg, f"fixture id missing from message: {msg!r}"
    assert "France" in msg and "Spain" in msg, f"team names missing from message: {msg!r}"
    assert "provider_fixture_map.json" in msg, f"actionable hint missing: {msg!r}"


def test_scheduled_unmapped_fixture_does_not_warn(monkeypatch):
    """A SCHEDULED (NS) fixture with no map entry must NOT generate a warning."""
    payload = [
        _make_fixture(
            fixture_id=88888,
            status_short="NS", status_long="Not Started",
            home="France", away="Spain",
            date="2026-07-05T20:00:00+00:00",
            gh=None, ga=None,
        ),
    ]
    _patch_http(monkeypatch, payload)
    _stub_fixture_map(monkeypatch)

    fetch_results.fetch_api_football(api_key="test", dry_run=False)

    warns = [w for w in fetch_results._FETCH_WARNINGS
             if w.get("type") == "unmapped_locked_fixture"]
    assert warns == [], (
        "SCHEDULED unmapped fixture incorrectly emitted a warning: "
        f"{fetch_results._FETCH_WARNINGS!r}"
    )


def test_live_unmapped_fixture_does_not_warn(monkeypatch):
    """A LIVE (1H/HT/2H) fixture with no map entry must NOT generate a warning."""
    payload = [
        _make_fixture(
            fixture_id=77777,
            status_short="2H", status_long="Second Half",
            home="France", away="Spain",
            date="2026-07-05T20:00:00+00:00",
            gh=0, ga=0,
        ),
    ]
    _patch_http(monkeypatch, payload)
    _stub_fixture_map(monkeypatch)

    fetch_results.fetch_api_football(api_key="test", dry_run=False)

    warns = [w for w in fetch_results._FETCH_WARNINGS
             if w.get("type") == "unmapped_locked_fixture"]
    assert warns == [], (
        "LIVE unmapped fixture incorrectly emitted a warning: "
        f"{fetch_results._FETCH_WARNINGS!r}"
    )


def test_aet_and_pen_also_warn(monkeypatch):
    """AET and PEN — both LOCKED statuses — must also produce warnings when unmapped."""
    payload = [
        _make_fixture(
            fixture_id=66666,
            status_short="AET", status_long="After Extra Time",
            home="Argentina", away="Brazil",
            date="2026-07-10T20:00:00+00:00",
            gh=2, ga=2,
        ),
        _make_fixture(
            fixture_id=66667,
            status_short="PEN", status_long="Penalty Shootout",
            home="Germany", away="Italy",
            date="2026-07-11T20:00:00+00:00",
            gh=1, ga=1,
        ),
    ]
    _patch_http(monkeypatch, payload)
    _stub_fixture_map(monkeypatch)

    fetch_results.fetch_api_football(api_key="test", dry_run=False)

    warns = [w for w in fetch_results._FETCH_WARNINGS
             if w.get("type") == "unmapped_locked_fixture"]
    assert len(warns) == 2, (
        f"expected 2 warnings (AET + PEN), got {len(warns)}: "
        f"{fetch_results._FETCH_WARNINGS!r}"
    )
    messages = " ".join(w["message"] for w in warns)
    assert "66666" in messages and "66667" in messages
