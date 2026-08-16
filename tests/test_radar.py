import json
import pathlib
from datetime import datetime, timezone

import pytest

import venue_radar as vr

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures/invitations_sample.json").read_text())
NOW = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)


def _v(due_offset_days):
    return {"invitations": [{"id": "a/-/S", "kind": "S",
            "duedate": int((NOW.timestamp() + 86400 * due_offset_days) * 1000)}]}


# --- Task 2: grouping + deadline math ---

def test_group_merges_same_venue():
    g = vr.group_by_venue(FIX["invitations"])
    v = g["Test.org/2026/Workshop/X"]
    assert {i["kind"] for i in v["invitations"]} == {"Submission", "Abstract_Submission"}


def test_group_skips_undated():
    g = vr.group_by_venue([{"id": "A/-/Submission"}])
    assert g == {}


def test_days_left_ceils():
    due = int((NOW.timestamp() + 36 * 3600) * 1000)
    assert vr.days_left(due, NOW) == 2


def test_soonest_due_ignores_past():
    v = {"invitations": [
        {"id": "a/-/S", "kind": "S", "duedate": int((NOW.timestamp() - 100) * 1000)},
        {"id": "a/-/T", "kind": "T", "duedate": int((NOW.timestamp() + 86400 * 3) * 1000)}]}
    assert vr.days_left(vr.soonest_due(v, NOW), NOW) == 3


def test_prettify():
    assert vr.prettify("IEEE.org/IROS/2026/Workshop/SurgTwin") == "IEEE.org IROS 2026 Workshop SurgTwin"


# --- Task 3: diff engine ---

def test_diff_new_extended_expired():
    state = {"OLD/V": {**_v(5), "archived": False},
             "GONE/V": {**_v(5), "archived": False},
             "DONE/V": {**_v(-5), "archived": True}}
    current = {"OLD/V": _v(9), "NEW/V": _v(3)}
    ch = vr.compute_changes(state, current, NOW)
    assert ch["new"] == ["NEW/V"]
    assert ch["extended"][0][0] == "OLD/V" and vr.days_left(ch["extended"][0][2], NOW) == 9
    assert ch["expired"] == ["GONE/V"]  # DONE/V already archived: not re-expired


# --- Task 4: classification parse ---

def test_parse_verdicts_strips_fences():
    text = '```json\n[{"venue": "A/B", "verdict": "core", "why": "alignment"}]\n```'
    out = vr.parse_verdicts(text, {"A/B"})
    assert out["A/B"]["verdict"] == "core"


def test_parse_verdicts_rejects_bad_verdict_and_missing():
    with pytest.raises(ValueError):
        vr.parse_verdicts('[{"venue": "A/B", "verdict": "maybe", "why": "x"}]', {"A/B"})
    with pytest.raises(ValueError):
        vr.parse_verdicts('[]', {"A/B"})


# --- Task 6: rendering ---

def _entry(due_days, verdict, title, issue=None):
    return {**_v(due_days), "title": title, "subtitle": "", "website": "",
            "verdict": verdict, "why": "w", "issue": issue, "archived": False,
            "first_seen": "2026-08-15"}


def test_render_sections_and_sort():
    sv = {"B/V": _entry(9, "core", "Beta"), "A/V": _entry(3, "core", "Alpha", issue=4),
          "C/V": _entry(5, "adjacent", "Gamma"), "D/V": _entry(-2, "core", "Old")}
    sv["D/V"]["archived"] = True
    md = vr.render(sv, {"new": ["B/V"], "extended": [], "expired": []}, NOW, "harryila/venue-radar")
    core = md[md.index("## 🎯 Core"):]
    assert core.index("Alpha") < core.index("Beta")  # core sorted by deadline
    assert "Old" not in md                            # archived hidden
    assert "🆕" in md and "Gamma" in md and "#4" in md


def test_render_unclassified_listed():
    sv = {"U/V": {**_entry(4, None, "Mystery"), "verdict": None}}
    md = vr.render(sv, {"new": [], "extended": [], "expired": []}, NOW, "r/r")
    assert "Unclassified" in md and "Mystery" in md


# --- Task 7: notifications ---

def test_subjects():
    due = int((NOW.timestamp() + 86400 * 36) * 1000)
    old = int((NOW.timestamp() + 86400 * 27) * 1000)
    assert vr.subject_new("AAAI Alignment", due, NOW) == "🎯 Open: AAAI Alignment — due Sep 20 (36d)"
    assert vr.subject_reminder("X", due, NOW) == "⏰ 36 days left: X (due Sep 20)"
    assert vr.subject_extended("X", old, due, NOW) == "📅 Extended +9d: X → Sep 20"


def test_email_html_contains_pieces():
    h = vr.email_html("T", "why", [("Due", "Sep 20")], "https://x", "Open CFP", "")
    assert "T" in h and "why" in h and "Sep 20" in h and "https://x" in h
