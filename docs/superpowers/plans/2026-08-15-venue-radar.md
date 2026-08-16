# venue-radar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the venue-radar engine, tests, and daily GitHub Actions workflow; create the private repo; verify with a live first run.

**Architecture:** One Python engine (`venue_radar.py`) of pure functions (group/diff/render, deterministic, tested with fixtures) around impure edges (OpenReview HTTP, `claude -p` subprocess, `gh` CLI, Gmail SMTP). State advances atomically via a single CI commit per run.

**Tech Stack:** Python 3.12 (stdlib + `requests`), pytest, GitHub Actions, `gh` CLI, Claude Code CLI (`claude -p`, subscription OAuth token), Gmail SMTP.

**Spec:** `docs/superpowers/specs/2026-08-15-venue-radar-design.md`

## Global Constraints

- Notifications are push-only; adjacent venues never generate issues or emails.
- A venue is classified once; re-classification only when `sha256(profile.md)` changes (non-archived venues) — verdicts otherwise immutable.
- Emails: from `GMAIL_ADDRESS` to `MAIL_TO` (default `harry_ila@berkeley.edu`), via `smtp.gmail.com:465`. Email failure never fails the run.
- Fetch/classify hard errors fail the run before any file write (atomic state).
- Backfill run (empty `state.json` venues) → issues without `@harryila` mention, one summary email instead of per-venue emails.
- All "now"-dependent functions take `now: datetime` (UTC, tz-aware) as a parameter.
- Batches of 40 venues per `claude -p` call; one retry on malformed output; then venues render as *unclassified*, retried next run.

---

### Task 1: Scaffold + fixtures

**Files:**
- Create: `.gitignore`, `requirements-dev.txt`, `tests/fixtures/invitations_sample.json`

**Interfaces:** Produces fixture JSON: a dict `{"invitations": [...]}` with ≥6 real invitations from api2 covering ≥2 invitations sharing one venue prefix if present, else synthetic pair added with ids `Test.org/2026/Workshop/X/-/Submission` and `.../-/Abstract_Submission`.

- [ ] **Step 1:** Write `.gitignore` (`__pycache__/`, `*.pyc`, `.pytest_cache/`) and `requirements-dev.txt` (`requests`, `pytest`).
- [ ] **Step 2:** Capture fixture: `curl -s 'https://api2.openreview.net/invitations?invitee=~&pastdue=false&limit=8' > tests/fixtures/invitations_sample.json`, then append two synthetic invitations sharing prefix `Test.org/2026/Workshop/X` with distinct kinds and known epoch dates (e.g. duedate `1790000000000`, cdate `1780000000000`).
- [ ] **Step 3:** `pip install -r requirements-dev.txt`; commit `chore: scaffold`.

### Task 2: Grouping + deadline math (pure core)

**Files:**
- Create: `venue_radar.py`, `tests/test_radar.py`

**Interfaces (produces, used by all later tasks):**
```python
group_by_venue(invitations: list[dict]) -> dict[str, dict]
# {venue_id: {"invitations": [{"id","kind","cdate","duedate","expdate"}...]}}
effective_due(inv: dict) -> int | None          # duedate or expdate (ms)
soonest_due(venue: dict, now: datetime) -> int | None  # min future effective due, ms
days_left(due_ms: int, now: datetime) -> int    # ceil days
fmt_pt(ms: int) -> str                          # "Sep 20, 2026" in America/Los_Angeles
prettify(venue_id: str) -> str                  # "EMNLP 2026 Workshop WaC-13"
```

- [ ] **Step 1:** Failing tests:

```python
import json, pathlib
from datetime import datetime, timezone
import venue_radar as vr

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures/invitations_sample.json").read_text())
NOW = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)

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
```

- [ ] **Step 2:** Run `pytest -x` → FAIL (no module attrs).
- [ ] **Step 3:** Implement in `venue_radar.py`:

```python
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

def effective_due(inv):
    return inv.get("duedate") or inv.get("expdate")

def group_by_venue(invitations):
    venues = {}
    for inv in invitations:
        iid = inv.get("id", "")
        if "/-/" not in iid or effective_due(inv) is None:
            continue
        vid, kind = iid.split("/-/", 1)
        entry = {"id": iid, "kind": kind, "cdate": inv.get("cdate"),
                 "duedate": inv.get("duedate"), "expdate": inv.get("expdate")}
        venues.setdefault(vid, {"invitations": []})["invitations"].append(entry)
    return venues

def soonest_due(venue, now):
    now_ms = int(now.timestamp() * 1000)
    dues = [effective_due(i) for i in venue["invitations"]]
    future = [d for d in dues if d and d > now_ms]
    return min(future) if future else None

def days_left(due_ms, now):
    return math.ceil((due_ms / 1000 - now.timestamp()) / 86400)

def fmt_pt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=PT).strftime("%b %-d, %Y")

def prettify(venue_id):
    return venue_id.replace("_", " ").replace("/", " ")
```

- [ ] **Step 4:** `pytest -x` → PASS. Commit `feat: invitation grouping and deadline math`.

### Task 3: Diff engine

**Files:** Modify `venue_radar.py`, `tests/test_radar.py`

**Interfaces:**
```python
compute_changes(state_venues: dict, current: dict, now: datetime) -> dict
# {"new": [vid], "extended": [(vid, old_ms, new_ms)], "expired": [vid]}
# expired = active (not archived) in state but absent from current fetch
# extended = soonest_due changed for a non-archived known venue (either direction)
```

- [ ] **Step 1:** Failing tests:

```python
def _v(due_offset_days):
    return {"invitations": [{"id": "a/-/S", "kind": "S",
            "duedate": int((NOW.timestamp() + 86400 * due_offset_days) * 1000)}]}

def test_diff_new_extended_expired():
    state = {"OLD/V": {**_v(5), "archived": False},
             "GONE/V": {**_v(5), "archived": False},
             "DONE/V": {**_v(-5), "archived": True}}
    current = {"OLD/V": _v(9), "NEW/V": _v(3)}
    ch = vr.compute_changes(state, current, NOW)
    assert ch["new"] == ["NEW/V"]
    assert ch["extended"][0][0] == "OLD/V" and vr.days_left(ch["extended"][0][2], NOW) == 9
    assert ch["expired"] == ["GONE/V"]  # DONE/V already archived: not re-expired
```

- [ ] **Step 2:** `pytest -x` → FAIL.
- [ ] **Step 3:** Implement:

```python
def compute_changes(state_venues, current, now):
    new = [vid for vid in current if vid not in state_venues]
    extended, expired = [], []
    for vid, sv in state_venues.items():
        if sv.get("archived"):
            continue
        if vid not in current:
            expired.append(vid)
            continue
        old, newd = soonest_due(sv, now), soonest_due(current[vid], now)
        if old and newd and old != newd:
            extended.append((vid, old, newd))
    return {"new": sorted(new), "extended": extended, "expired": sorted(expired)}
```

- [ ] **Step 4:** `pytest -x` → PASS. Commit `feat: state diff engine`.

### Task 4: Classification (prompt build + output parse + subprocess)

**Files:** Modify `venue_radar.py`, `tests/test_radar.py`

**Interfaces:**
```python
build_prompt(profile: str, batch: list[dict]) -> str   # batch: [{"venue","title","subtitle","website","deadlines"}]
parse_verdicts(text: str, expected_ids: set[str]) -> dict[str, dict]  # {vid: {"verdict","why"}}; raises ValueError
classify_batch(profile: str, batch: list[dict]) -> dict[str, dict]    # calls claude -p; 1 retry; raises RuntimeError
VERDICTS = {"core", "adjacent", "no"}
```

- [ ] **Step 1:** Failing tests (parse only — subprocess is not unit-tested):

```python
def test_parse_verdicts_strips_fences():
    text = '```json\n[{"venue": "A/B", "verdict": "core", "why": "alignment"}]\n```'
    out = vr.parse_verdicts(text, {"A/B"})
    assert out["A/B"]["verdict"] == "core"

def test_parse_verdicts_rejects_bad_verdict_and_missing():
    import pytest
    with pytest.raises(ValueError):
        vr.parse_verdicts('[{"venue": "A/B", "verdict": "maybe", "why": "x"}]', {"A/B"})
    with pytest.raises(ValueError):
        vr.parse_verdicts('[]', {"A/B"})
```

- [ ] **Step 2:** `pytest -x` → FAIL.
- [ ] **Step 3:** Implement:

```python
import json, subprocess

VERDICTS = {"core", "adjacent", "no"}

def build_prompt(profile, batch):
    return (
        "You are screening academic venues for Harry. His profile and rules are below, "
        "followed by venues currently open for submissions on OpenReview.\n"
        "For EVERY venue output {\"venue\": \"<id>\", \"verdict\": \"core\"|\"adjacent\"|\"no\", "
        "\"why\": \"<one short sentence>\"}.\n"
        "core = Harry should seriously consider submitting; adjacent = plausibly relevant, worth a glance; "
        "no = clearly outside his fields. Apply the profile's explicit rules over vibes.\n"
        "Output ONLY a JSON array covering every venue id exactly once. No prose, no code fences.\n\n"
        f"## PROFILE\n{profile}\n\n## VENUES\n{json.dumps(batch, indent=1)}"
    )

def parse_verdicts(text, expected_ids):
    t = text.strip()
    if "```" in t:
        t = t.split("```")[1]
        t = t[4:] if t.startswith("json") else t
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array found")
    items = json.loads(t[start:end + 1])
    out = {}
    for it in items:
        vid, verdict = it.get("venue"), it.get("verdict")
        if vid in expected_ids and verdict in VERDICTS:
            out[vid] = {"verdict": verdict, "why": str(it.get("why", ""))[:300]}
        else:
            raise ValueError(f"bad item: {it}")
    missing = expected_ids - set(out)
    if missing:
        raise ValueError(f"missing verdicts: {missing}")
    return out

def classify_batch(profile, batch):
    prompt = build_prompt(profile, batch)
    expected = {b["venue"] for b in batch}
    last = None
    for _ in range(2):
        r = subprocess.run(["claude", "-p", "--output-format", "json"],
                           input=prompt, capture_output=True, text=True, timeout=900)
        try:
            if r.returncode != 0:
                raise ValueError(f"claude exit {r.returncode}: {r.stderr[:500]}")
            return parse_verdicts(json.loads(r.stdout)["result"], expected)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            last = e
    raise RuntimeError(f"classification failed after retry: {last}")
```

- [ ] **Step 4:** `pytest -x` → PASS. Commit `feat: claude classification with strict parse`.

### Task 5: Fetch + venue metadata (impure edge)

**Files:** Modify `venue_radar.py`

**Interfaces:**
```python
fetch_invitations() -> list[dict]        # api2 + api (v1), paged, raises on HTTP error
fetch_venue_meta(vid: str) -> dict       # {"title","subtitle","website"}; falls back to prettify(vid)
```

- [ ] **Step 1:** Implement (no unit tests — exercised by `--dry-run` against live API in Task 9):

```python
import requests

HOSTS = ["https://api2.openreview.net", "https://api.openreview.net"]

def fetch_invitations():
    out = []
    for host in HOSTS:
        offset = 0
        while True:
            r = requests.get(f"{host}/invitations",
                             params={"invitee": "~", "pastdue": "false",
                                     "limit": 1000, "offset": offset}, timeout=60)
            r.raise_for_status()
            page = r.json().get("invitations", [])
            out.extend(page)
            if len(page) < 1000:
                break
            offset += 1000
    return out

def fetch_venue_meta(vid):
    meta = {"title": prettify(vid), "subtitle": "", "website": ""}
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/groups", params={"id": vid}, timeout=30)
            if r.status_code != 200:
                continue
            groups = r.json().get("groups", [])
            if not groups:
                continue
            c = groups[0].get("content") or {}
            def val(k):
                v = c.get(k)
                return (v.get("value") if isinstance(v, dict) else v) or ""
            meta["title"] = val("title") or meta["title"]
            meta["subtitle"] = val("subtitle")
            meta["website"] = val("website")
            return meta
        except requests.RequestException:
            continue
    return meta
```

- [ ] **Step 2:** `pytest -x` still green. Commit `feat: openreview fetch + venue metadata`.

### Task 6: Rendering venues.md

**Files:** Modify `venue_radar.py`, `tests/test_radar.py`

**Interfaces:**
```python
render(state_venues: dict, changes: dict, now: datetime, repo: str) -> str
# venue entry fields used: title, website, verdict, why, invitations, issue, archived
# CFP link: https://openreview.net/group?id=<vid>; sections: New / Core / Adjacent / Unclassified
```

- [ ] **Step 1:** Failing test:

```python
def _entry(due_days, verdict, title, issue=None):
    return {**_v(due_days), "title": title, "subtitle": "", "website": "",
            "verdict": verdict, "why": "w", "issue": issue, "archived": False,
            "first_seen": "2026-08-15"}

def test_render_sections_and_sort():
    sv = {"B/V": _entry(9, "core", "Beta"), "A/V": _entry(3, "core", "Alpha", issue=4),
          "C/V": _entry(5, "adjacent", "Gamma"), "D/V": _entry(-2, "core", "Old")}
    sv["D/V"]["archived"] = True
    md = vr.render(sv, {"new": ["B/V"], "extended": [], "expired": []}, NOW, "harryila/venue-radar")
    assert md.index("Alpha") < md.index("Beta")      # core sorted by deadline
    assert "Old" not in md                            # archived hidden
    assert "🆕" in md and "Gamma" in md and "#4" in md
```

- [ ] **Step 2:** `pytest -x` → FAIL.
- [ ] **Step 3:** Implement:

```python
def _cfp(vid):
    return f"https://openreview.net/group?id={vid}"

def _deadline_lines(v):
    return " · ".join(f"{i['kind'].replace('_', ' ')}: {fmt_pt(effective_due(i))}"
                      for i in sorted(v["invitations"], key=lambda i: effective_due(i) or 0))

def render(state_venues, changes, now, repo):
    active = {vid: v for vid, v in state_venues.items() if not v.get("archived")}
    def dleft(v):
        d = soonest_due(v, now)
        return days_left(d, now) if d else 9999
    lines = [f"# 📡 Venue Radar", "",
             f"_Updated {now.strftime('%Y-%m-%d %H:%M UTC')} · "
             f"{len(active)} open venues tracked_", ""]
    if changes["new"]:
        lines += ["## 🆕 New since last run", ""]
        for vid in changes["new"]:
            v = active.get(vid)
            if v:
                lines.append(f"- **[{v['title']}]({_cfp(vid)})** ({v.get('verdict', '?')}) — "
                             f"due {fmt_pt(soonest_due(v, now))} ({dleft(v)}d)")
        lines.append("")
    for section, verdict, icon in [("Core", "core", "🎯"), ("Adjacent", "adjacent", "🔭")]:
        vs = sorted([(vid, v) for vid, v in active.items() if v.get("verdict") == verdict],
                    key=lambda t: dleft(t[1]))
        lines += [f"## {icon} {section} ({len(vs)})", ""]
        if verdict == "core":
            for vid, v in vs:
                issue = f" · [#{v['issue']}](https://github.com/{repo}/issues/{v['issue']})" if v.get("issue") else ""
                lines += [f"### [{v['title']}]({_cfp(vid)}) — {dleft(v)} days left",
                          f"{v['why']}", "",
                          f"Opened {v.get('first_seen', '?')} · {_deadline_lines(v)}{issue}", ""]
        else:
            lines += ["| Venue | Due | Days | Why |", "|---|---|---|---|"]
            lines += [f"| [{v['title']}]({_cfp(vid)}) | {fmt_pt(soonest_due(v, now))} "
                      f"| {dleft(v)} | {v['why']} |" for vid, v in vs]
            lines.append("")
    unc = sorted([(vid, v) for vid, v in active.items() if not v.get("verdict")],
                 key=lambda t: dleft(t[1]))
    if unc:
        lines += ["## ❓ Unclassified (retrying next run)", ""]
        lines += [f"- [{v['title']}]({_cfp(vid)}) — due {fmt_pt(soonest_due(v, now))}"
                  for vid, v in unc]
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4:** `pytest -x` → PASS. Commit `feat: venues.md renderer`.

### Task 7: Notifications (subjects, email HTML, gh issues)

**Files:** Modify `venue_radar.py`, `tests/test_radar.py`

**Interfaces:**
```python
subject_new(title, due_ms, now) -> str      # "🎯 Open: <t> — due Sep 20 (36d)"
subject_reminder(title, due_ms, now) -> str # "⏰ 5 days left: <t> (due Sep 20)"
subject_extended(title, old_ms, new_ms, now) -> str  # "📅 Extended +9d: <t> → Sep 30"
email_html(heading, why, detail_rows, cta_url, cta_label, footer_html) -> str
send_email(subject, html, plain) -> None    # no-op unless GMAIL_ADDRESS+GMAIL_APP_PASSWORD set; never raises
gh(args: list[str]) -> str                  # runs `gh <args>`, returns stdout; raises on error
open_issue(repo, title, body) -> int
issue_comment(repo, number, body) / close_issue(repo, number)
open_issue_numbers(repo) -> set[int]
```

- [ ] **Step 1:** Failing tests:

```python
def test_subjects():
    due = int((NOW.timestamp() + 86400 * 36) * 1000)
    old = int((NOW.timestamp() + 86400 * 27) * 1000)
    assert vr.subject_new("AAAI Alignment", due, NOW) == "🎯 Open: AAAI Alignment — due Sep 20 (36d)"
    assert vr.subject_reminder("X", due, NOW) == "⏰ 36 days left: X (due Sep 20)"
    assert vr.subject_extended("X", old, due, NOW) == "📅 Extended +9d: X → Sep 20"

def test_email_html_contains_pieces():
    h = vr.email_html("T", "why", [("Due", "Sep 20")], "https://x", "Open CFP", "")
    assert "T" in h and "why" in h and "Sep 20" in h and "https://x" in h
```

- [ ] **Step 2:** `pytest -x` → FAIL.
- [ ] **Step 3:** Implement:

```python
import os, smtplib
from email.message import EmailMessage

def _md(ms):
    return datetime.fromtimestamp(ms / 1000, tz=PT).strftime("%b %-d")

def subject_new(title, due_ms, now):
    return f"🎯 Open: {title} — due {_md(due_ms)} ({days_left(due_ms, now)}d)"

def subject_reminder(title, due_ms, now):
    return f"⏰ {days_left(due_ms, now)} days left: {title} (due {_md(due_ms)})"

def subject_extended(title, old_ms, new_ms, now):
    delta = days_left(new_ms, now) - days_left(old_ms, now)
    return f"📅 Extended {'+' if delta >= 0 else ''}{delta}d: {title} → {_md(new_ms)}"

def email_html(heading, why, detail_rows, cta_url, cta_label, footer_html):
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#777;font-size:13px;white-space:nowrap;'>{k}</td>"
        f"<td style='padding:4px 0;color:#222;font-size:13px;'>{v}</td></tr>" for k, v in detail_rows)
    return f"""<div style="background:#f5f5f4;padding:24px 12px;">
<table role="presentation" style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e4e4e4;border-radius:12px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;" width="100%">
<tr><td style="padding:28px;">
<p style="margin:0;font-size:11px;letter-spacing:1.5px;color:#b08900;text-transform:uppercase;">📡 venue radar</p>
<h2 style="margin:10px 0 6px;font-size:20px;color:#111;">{heading}</h2>
<p style="margin:0 0 16px;color:#444;font-size:14px;line-height:1.5;">{why}</p>
<table role="presentation" style="margin:0 0 20px;">{rows}</table>
<a href="{cta_url}" style="display:inline-block;background:#1f6feb;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;">{cta_label}</a>
<p style="margin:18px 0 0;font-size:12px;color:#999;">{footer_html}</p>
</td></tr></table></div>"""

def send_email(subject, html, plain):
    user, pw = os.environ.get("GMAIL_ADDRESS"), os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO", "harry_ila@berkeley.edu")
    if not user or not pw:
        print(f"[email skipped] {subject}")
        return
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, f"Venue Radar <{user}>", to
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        print(f"[email sent] {subject}")
    except Exception as e:
        print(f"[email FAILED] {subject}: {e}")

def gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh {args[:2]} failed: {r.stderr[:300]}")
    return r.stdout.strip()

def open_issue(repo, title, body):
    url = gh(["issue", "create", "-R", repo, "-t", title, "-b", body])
    return int(url.rstrip("/").rsplit("/", 1)[-1])

def issue_comment(repo, number, body):
    gh(["issue", "comment", str(number), "-R", repo, "-b", body])

def close_issue(repo, number, body):
    gh(["issue", "close", str(number), "-R", repo, "-c", body])

def open_issue_numbers(repo):
    out = gh(["issue", "list", "-R", repo, "--state", "open", "--limit", "500",
              "--json", "number", "-q", ".[].number"])
    return {int(n) for n in out.split()} if out else set()
```

- [ ] **Step 4:** `pytest -x` → PASS. Commit `feat: notification layer (subjects, email card, gh issues)`.

### Task 8: Orchestration — main(), state I/O, backfill, reminders, digest, --dry-run

**Files:** Modify `venue_radar.py`

**Interfaces (consumes everything above; CLI):** `python venue_radar.py [--dry-run] [--skip-classify]`

- [ ] **Step 1:** Implement:

```python
import argparse, hashlib, sys

STATE_FILE, VENUES_FILE, PROFILE_FILE = "state.json", "venues.md", "profile.md"
REMINDER_DAYS = [7, 2]
BATCH = 40

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"profile_hash": "", "last_run": "", "venues": {}}

def issue_body(vid, v, now):
    due = soonest_due(v, now)
    return (f"**[{v['title']}]({_cfp(vid)})** — due {fmt_pt(due)} ({days_left(due, now)}d left)\n\n"
            f"> {v['why']}\n\n{_deadline_lines(v)}\n\n"
            f"**Close this issue once you've decided** (submitting or skipping) — "
            f"reminders at T-7 and T-2 days stop when it's closed.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-classify", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run
    now = datetime.now(timezone.utc)
    repo = os.environ.get("GITHUB_REPOSITORY", "harryila/venue-radar")
    state = load_state()
    sv = state["venues"]
    backfill = not sv
    profile = open(PROFILE_FILE).read()
    phash = hashlib.sha256(profile.encode()).hexdigest()

    current = group_by_venue(fetch_invitations())
    print(f"fetched {sum(len(v['invitations']) for v in current.values())} invitations "
          f"across {len(current)} venues")
    changes = compute_changes(sv, current, now)
    print(f"new={len(changes['new'])} extended={len(changes['extended'])} "
          f"expired={len(changes['expired'])}")

    for vid in changes["new"]:
        meta = fetch_venue_meta(vid)
        sv[vid] = {**meta, "invitations": current[vid]["invitations"],
                   "first_seen": now.strftime("%Y-%m-%d"), "verdict": None, "why": "",
                   "issue": None, "reminded": [], "archived": False}
    for vid, v in sv.items():
        if vid in current:
            v["invitations"] = current[vid]["invitations"]

    to_judge = [vid for vid, v in sv.items() if not v.get("archived") and not v.get("verdict")]
    if state["profile_hash"] and state["profile_hash"] != phash:
        print("profile.md changed — re-judging all active venues")
        to_judge = [vid for vid, v in sv.items() if not v.get("archived")]
    if args.skip_classify:
        to_judge = []
    newly_core = []
    for i in range(0, len(to_judge), BATCH):
        batch_ids = to_judge[i:i + BATCH]
        batch = [{"venue": vid, "title": sv[vid]["title"], "subtitle": sv[vid]["subtitle"],
                  "website": sv[vid]["website"],
                  "deadlines": _deadline_lines(sv[vid])} for vid in batch_ids]
        try:
            verdicts = classify_batch(profile, batch)
        except RuntimeError as e:
            print(f"[warn] batch unclassified: {e}")
            continue
        for vid, verdict in verdicts.items():
            was = sv[vid].get("verdict")
            sv[vid]["verdict"], sv[vid]["why"] = verdict["verdict"], verdict["why"]
            if verdict["verdict"] == "core" and was != "core" and not sv[vid].get("issue"):
                newly_core.append(vid)
        print(f"classified {min(i + BATCH, len(to_judge))}/{len(to_judge)}")

    open_nums = set()
    if not dry and (newly_core or changes["extended"] or changes["expired"] or True):
        try:
            open_nums = open_issue_numbers(repo)
        except RuntimeError as e:
            print(f"[warn] gh unavailable: {e}")

    for vid in newly_core:
        v = sv[vid]
        due = soonest_due(v, now)
        mention = "" if backfill else "@harryila "
        if dry:
            print(f"[dry] would open issue + email: {subject_new(v['title'], due, now)}")
            continue
        try:
            v["issue"] = open_issue(repo, f"🎯 {v['title']} — due {fmt_pt(due)}",
                                    mention + issue_body(vid, v, now))
            open_nums.add(v["issue"])
        except RuntimeError as e:
            print(f"[warn] issue create failed for {vid}: {e}")
        if not backfill:
            issue_url = f"https://github.com/{repo}/issues/{v['issue']}"
            send_email(subject_new(v["title"], due, now),
                       email_html(v["title"], v["why"],
                                  [("Due", f"{fmt_pt(due)} ({days_left(due, now)}d left)"),
                                   ("Deadlines", _deadline_lines(v)),
                                   ("Opened", v["first_seen"])],
                                  _cfp(vid), "Open CFP →",
                                  f"Decide in <a href='{issue_url}'>issue #{v['issue']}</a> — closing it silences reminders."),
                       f"{v['title']} — core match. Due {fmt_pt(due)}. {_cfp(vid)}")

    if backfill and newly_core and not dry:
        cores = [(vid, sv[vid]) for vid in newly_core]
        cores.sort(key=lambda t: soonest_due(t[1], now) or 0)
        rows = [(v["title"], f"due {fmt_pt(soonest_due(v, now))} ({days_left(soonest_due(v, now), now)}d)")
                for _, v in cores]
        send_email(f"📡 Venue radar is live: {len(cores)} core venues open now",
                   email_html("Backfill complete", "Every currently-open venue judged core for you:",
                              rows, f"https://github.com/{repo}/blob/main/venues.md",
                              "See the full radar →", "Each has a GitHub issue — close ones you're skipping."),
                   "\n".join(f"{t}: {d}" for t, d in rows))

    for vid, old_ms, new_ms in changes["extended"]:
        v = sv[vid]
        if v.get("verdict") != "core" or not v.get("issue"):
            continue
        subj = subject_extended(v["title"], old_ms, new_ms, now)
        if dry:
            print(f"[dry] would comment+email: {subj}")
            continue
        if v["issue"] in open_nums:
            try:
                issue_comment(repo, v["issue"], f"📅 Deadline changed: {fmt_pt(old_ms)} → "
                              f"**{fmt_pt(new_ms)}** ({days_left(new_ms, now)}d left)")
            except RuntimeError as e:
                print(f"[warn] comment failed: {e}")
            send_email(subj, email_html(v["title"], v["why"],
                       [("Was", fmt_pt(old_ms)), ("Now", f"{fmt_pt(new_ms)} ({days_left(new_ms, now)}d left)")],
                       _cfp(vid), "Open CFP →", ""), subj)
            v["reminded"] = []

    for vid, v in sv.items():
        if v.get("archived") or v.get("verdict") != "core" or not v.get("issue"):
            continue
        if not dry and v["issue"] not in open_nums:
            continue
        due = soonest_due(v, now)
        if not due:
            continue
        dl = days_left(due, now)
        for t in REMINDER_DAYS:
            tag = f"T-{t}"
            if dl <= t and tag not in v.get("reminded", []):
                v.setdefault("reminded", []).append(tag)
                subj = subject_reminder(v["title"], due, now)
                if dry:
                    print(f"[dry] would remind: {subj}")
                    break
                try:
                    issue_comment(repo, v["issue"], f"⏰ **{dl} days left** (due {fmt_pt(due)}). "
                                  "Close this issue to stop reminders.")
                except RuntimeError as e:
                    print(f"[warn] reminder comment failed: {e}")
                send_email(subj, email_html(v["title"], v["why"],
                           [("Due", f"{fmt_pt(due)} ({dl}d left)")], _cfp(vid), "Open CFP →",
                           "Closing the GitHub issue silences reminders."), subj)
                break

    for vid in changes["expired"]:
        v = sv[vid]
        v["archived"] = True
        if v.get("issue") and not dry and v["issue"] in open_nums:
            try:
                close_issue(repo, v["issue"], "Deadline passed — archiving. 👋")
            except RuntimeError as e:
                print(f"[warn] close failed: {e}")

    if datetime.now(PT).weekday() == 0 or os.environ.get("FORCE_DIGEST"):
        week_new = [(vid, v) for vid, v in sv.items() if not v.get("archived")
                    and (now - datetime.strptime(v["first_seen"], "%Y-%m-%d").replace(tzinfo=timezone.utc)).days < 7]
        soon = sorted([(vid, v) for vid, v in sv.items() if not v.get("archived")
                       and v.get("verdict") in ("core", "adjacent") and soonest_due(v, now)
                       and days_left(soonest_due(v, now), now) <= 14],
                      key=lambda t: soonest_due(t[1], now))
        if (week_new or soon) and not backfill:
            n_core = sum(1 for _, v in week_new if v.get("verdict") == "core")
            subj = f"📬 Weekly: {n_core} new core, {len(soon)} deadlines in 14d"
            rows = ([("New this week", ", ".join(v["title"] for _, v in week_new) or "—")] +
                    [(v["title"], f"{v.get('verdict')} · due {fmt_pt(soonest_due(v, now))} "
                      f"({days_left(soonest_due(v, now), now)}d)") for _, v in soon])
            if dry:
                print(f"[dry] would send digest: {subj}")
            else:
                send_email(subj, email_html("Weekly digest",
                           "Everything new this week, plus deadlines in the next 14 days.",
                           rows, f"https://github.com/{repo}/blob/main/venues.md",
                           "Open the radar →", ""), subj)

    state["profile_hash"] = phash
    state["last_run"] = now.isoformat()
    md = render(sv, changes, now, repo)
    if dry:
        print("[dry] state/venues.md not written")
        return
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    with open(VENUES_FILE, "w") as f:
        f.write(md)
    print("state.json + venues.md written")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2:** `pytest -x` green; commit `feat: orchestration, backfill, reminders, digest`.

### Task 9: profile.md + README + local live verification

**Files:** Create `profile.md`, `README.md`

- [ ] **Step 1:** Write `profile.md` — sections: About Harry (Berkeley; AI alignment focus; ML/NLP; geospatial/data-infra background), Core rules (anything alignment/safety/interpretability; general ML/NLP main tracks and workshops where a small-team paper is feasible; LLM agents/evaluation), Adjacent rules (geospatial, data infra, robotics-ML crossover, domain applications of ML), No rules (pure theory outside ML, hardware, medicine-only, non-AI systems). Explicit instruction: "When unsure between adjacent and no, prefer adjacent; between core and adjacent, prefer core only if a submission is realistically feasible for a small team."
- [ ] **Step 2:** Write short `README.md`: what it is, the pipeline, how to tune `profile.md`, secrets table, `--dry-run` usage.
- [ ] **Step 3:** Verify live: `python venue_radar.py --dry-run --skip-classify` → expect ~340 invitations fetched, all-new diff, no writes.
- [ ] **Step 4:** Verify classification end-to-end on a tiny slice: temporary script or `--dry-run` with state seeded so only ~5 venues are new; confirm verdicts parse. (Simplest: run `classify_batch` directly in `python -c` with 3 real venues.)
- [ ] **Step 5:** Verify secrets work locally: `CLAUDE_CODE_OAUTH_TOKEN=<token> claude -p 'reply ok'` and `smtplib` login test with the app password (login only, no send).
- [ ] **Step 6:** Commit `docs: profile and readme`.

### Task 10: GitHub Actions workflow

**Files:** Create `.github/workflows/daily.yml`

- [ ] **Step 1:**

```yaml
name: venue-radar
on:
  schedule:
    - cron: "0 13 * * *"   # ~6am PT daily
  workflow_dispatch: {}
permissions:
  contents: write
  issues: write
concurrency:
  group: venue-radar
  cancel-in-progress: false
jobs:
  radar:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests
      - run: npm install -g @anthropic-ai/claude-code
      - name: Run radar
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          MAIL_TO: harry_ila@berkeley.edu
          GH_TOKEN: ${{ github.token }}
        run: python venue_radar.py
      - name: Commit state
        run: |
          git config user.name "venue-radar[bot]"
          git config user.email "actions@github.com"
          git add state.json venues.md
          git diff --cached --quiet || git commit -m "radar: $(date -u +%F)"
          git push
```

- [ ] **Step 2:** Commit `ci: daily radar workflow`.

### Task 11: Repo creation, secrets, adversarial review, first live run

- [ ] **Step 1:** `gh repo create harryila/venue-radar --private --source . --push`
- [ ] **Step 2:** Set secrets via `gh secret set` (token rejoined across the paste line-break; app password with spaces stripped).
- [ ] **Step 3:** Adversarial multi-agent review (Workflow tool) of the full code: correctness lenses (diff/date math, state mutation ordering, backfill semantics), robustness (API drift, malformed claude output), CI (yaml validity, token env, push race), secrets hygiene. Fix confirmed findings, re-run tests, push.
- [ ] **Step 4:** `gh workflow run venue-radar` → watch with `gh run watch`; verify: run green, state.json + venues.md committed, core issues exist, backfill summary email sent (check Actions log for `[email sent]`).
- [ ] **Step 5:** Pull, sanity-read `venues.md`, report to Harry with what's left (check inbox, tune profile.md, optionally install GitHub mobile app).

## Self-Review

- **Spec coverage:** fetch both hosts ✓ (T5), grouping ✓ (T2), diff incl. extensions/expiry ✓ (T3), classify once + re-judge on profile hash ✓ (T4/T8), render ✓ (T6), issues as tracker with T-7/T-2 + extension comments + auto-close ✓ (T7/T8), custom email with spec'd subjects ✓ (T7/T8), backfill single summary ✓ (T8), Monday digest ✓ (T8), atomic commit ✓ (T10), dry-run ✓ (T8), unclassified-not-dropped ✓ (T8/T6), secrets ✓ (T11).
- **Placeholder scan:** none — all code inline.
- **Type consistency:** state venue fields (`title, subtitle, website, invitations, first_seen, verdict, why, issue, reminded, archived`) match across T6/T7/T8; `changes` dict keys match T3↔T6↔T8. `email_html(heading, why, detail_rows, cta_url, cta_label, footer_html)` signature consistent at all call sites.
