#!/usr/bin/env python3
"""venue-radar: push-only monitor for OpenReview venues open for submissions.

Daily pipeline: fetch open-submission invitations -> diff against state.json ->
classify new venues once via `claude -p` against profile.md -> render venues.md ->
notify (GitHub issues + Gmail). See docs/superpowers/specs/ for the design.
"""

import argparse
import hashlib
import json
import math
import os
import smtplib
import subprocess
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

PT = ZoneInfo("America/Los_Angeles")
HOSTS = ["https://api2.openreview.net", "https://api.openreview.net"]
STATE_FILE, VENUES_FILE, PROFILE_FILE = "state.json", "venues.md", "profile.md"
VERDICTS = {"core", "adjacent", "no"}
REMINDER_DAYS = [7, 2]
BATCH = 40


# --- pure core: grouping + deadline math ---

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


# --- diff engine ---

def compute_changes(state_venues, current, now):
    new, revived, extended, expired = [], [], [], []
    for vid in current:
        if vid not in state_venues:
            new.append(vid)
        elif state_venues[vid].get("archived"):
            revived.append(vid)  # deadline un-expired or a new cycle reusing the id
    for vid, sv in state_venues.items():
        if sv.get("archived"):
            continue
        if vid not in current:
            expired.append(vid)
            continue
        old, newd = soonest_due(sv, now), soonest_due(current[vid], now)
        if old and newd and old != newd:
            extended.append((vid, old, newd))
    return {"new": sorted(new), "revived": sorted(revived),
            "extended": extended, "expired": sorted(expired)}


# --- classification ---

def build_prompt(profile, batch):
    return (
        "You are screening academic venues for Harry. His profile and rules are below, "
        "followed by venues currently open for submissions on OpenReview.\n"
        "For EVERY venue output {\"venue\": \"<id>\", \"verdict\": \"core\"|\"adjacent\"|\"no\", "
        "\"why\": \"<one short sentence>\", \"prestige\": 1|2|3}.\n"
        "core = Harry should seriously consider submitting; adjacent = plausibly relevant, "
        "worth a glance; no = clearly outside his fields. "
        "Apply the profile's explicit rules over vibes.\n"
        "prestige: 1 = flagship family (NeurIPS/ICML/ICLR/ACL/EMNLP/NAACL/EACL/AACL/COLM/AAAI "
        "main tracks or their co-located workshops); 2 = established society or venue "
        "(IJCAI, AAMAS, COLING, CIKM, IEEE conferences, known ARR workshops); "
        "3 = regional, brand-new, or low-signal host.\n"
        "Output ONLY a JSON array covering every venue id exactly once. "
        "No prose, no code fences.\n\n"
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
        if not isinstance(it, dict):
            raise ValueError(f"non-object item: {it!r}")
        vid, verdict = it.get("venue"), it.get("verdict")
        if vid in expected_ids and verdict in VERDICTS:
            p = it.get("prestige")
            out[vid] = {"verdict": verdict, "why": str(it.get("why", ""))[:300],
                        "prestige": p if p in (1, 2, 3) else 2}
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
        try:
            r = subprocess.run(["claude", "-p", "--model", "claude-fable-5",
                                "--effort", "max", "--output-format", "json"],
                               input=prompt, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise ValueError(f"claude exit {r.returncode}: {r.stderr[:500]}")
            result = json.loads(r.stdout).get("result")
            if not isinstance(result, str):
                raise ValueError(f"claude envelope had no text result: {r.stdout[:200]}")
            return parse_verdicts(result, expected)
        except (ValueError, KeyError, json.JSONDecodeError,
                subprocess.TimeoutExpired, OSError) as e:
            last = e
    raise RuntimeError(f"classification failed after retry: {last}")


# --- fetch (impure edge) ---

def _get(url, params, timeout=60, attempts=4):
    # OpenReview rate-limits per IP; back off on 429/5xx, honoring Retry-After.
    r = None
    for a in range(attempts):
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "venue-radar (github.com/harryila)"})
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(int(r.headers.get("Retry-After") or 0) or 2 ** (a + 2), 90))
            continue
        return r
    return r


def fetch_invitations():
    # api2 rejects `offset` on this endpoint, so pagination is unavailable:
    # one maxed-out request per host, and a hard failure if we ever hit the cap.
    out = []
    for host in HOSTS:
        r = _get(f"{host}/invitations",
                 params={"invitee": "~", "pastdue": "false", "limit": 1000})
        r.raise_for_status()
        page = r.json().get("invitations", [])
        if len(page) >= 1000:
            raise RuntimeError(f"{host} returned {len(page)} invitations — result cap "
                               "hit, venues would be silently missed; needs pagination")
        out.extend(page)
    return out


def fetch_venue_meta(vid):
    meta = {"title": prettify(vid), "subtitle": "", "website": ""}
    time.sleep(0.25)  # polite pacing: backfill makes ~340 of these in a row
    for host in HOSTS:
        try:
            r = _get(f"{host}/groups", params={"id": vid}, timeout=30, attempts=3)
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


# --- rendering ---

def _cfp(vid):
    return f"https://openreview.net/group?id={vid}"


def stars(v):
    return {1: "★★★", 2: "★★", 3: "★"}.get(v.get("prestige", 2), "★★")


def _rank(v, now):
    # "most prestigious, then closest deadline" — prestige tiers group the list,
    # urgency orders inside each tier
    d = soonest_due(v, now)
    return (v.get("prestige", 2), days_left(d, now) if d else 9999)


def _deadline_lines(v):
    return " · ".join(f"{i['kind'].replace('_', ' ')}: {fmt_pt(effective_due(i))}"
                      for i in sorted(v["invitations"], key=lambda i: effective_due(i) or 0))


def render(state_venues, changes, now, repo):
    active = {vid: v for vid, v in state_venues.items() if not v.get("archived")}

    def dleft(v):
        d = soonest_due(v, now)
        return days_left(d, now) if d else 9999

    def due_days(v):
        # A venue can sit between duedate and expdate (grace window): no future
        # effective due, but the API still lists it. Never call fmt_pt(None).
        d = soonest_due(v, now)
        return (fmt_pt(d), f"{days_left(d, now)}d") if d else ("past due (grace)", "–")

    lines = ["# 📡 Venue Radar", "",
             f"_Updated {now.strftime('%Y-%m-%d %H:%M UTC')} · "
             f"{len(active)} open venues tracked_", ""]
    if changes["new"]:
        lines += ["## 🆕 New since last run", ""]
        for vid in changes["new"]:
            v = active.get(vid)
            if v:
                due, days = due_days(v)
                lines.append(f"- **[{v['title']}]({_cfp(vid)})** ({v.get('verdict') or '?'}) — "
                             f"due {due} ({days})")
        lines.append("")
    for section, verdict, icon in [("Core", "core", "🎯"), ("Adjacent", "adjacent", "🔭")]:
        vs = sorted([(vid, v) for vid, v in active.items() if v.get("verdict") == verdict],
                    key=lambda t: _rank(t[1], now))
        lines += [f"## {icon} {section} ({len(vs)})", ""]
        if verdict == "core":
            for vid, v in vs:
                issue = (f" · [#{v['issue']}](https://github.com/{repo}/issues/{v['issue']})"
                         if v.get("issue") else "")
                d = soonest_due(v, now)
                header_due = f"{days_left(d, now)} days left" if d else "past due (grace window)"
                lines += [f"### {stars(v)} [{v['title']}]({_cfp(vid)}) — {header_due}",
                          f"{v['why']}", "",
                          f"Opened {v.get('first_seen', '?')} · {_deadline_lines(v)}{issue}", ""]
        else:
            lines += ["| Venue | Due | Days | Why |", "|---|---|---|---|"]
            lines += [f"| {stars(v)} [{v['title']}]({_cfp(vid)}) | {due_days(v)[0]} "
                      f"| {due_days(v)[1]} | {v['why']} |" for vid, v in vs]
            lines.append("")
    unc = sorted([(vid, v) for vid, v in active.items() if not v.get("verdict")],
                 key=lambda t: dleft(t[1]))
    if unc:
        lines += ["## ❓ Unclassified (retrying next run)", ""]
        lines += [f"- [{v['title']}]({_cfp(vid)}) — due {due_days(v)[0]}"
                  for vid, v in unc]
        lines.append("")
    return "\n".join(lines)


# --- notifications ---

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
        f"<tr><td style='padding:4px 12px 4px 0;color:#777;font-size:13px;"
        f"white-space:nowrap;vertical-align:top;'>{k}</td>"
        f"<td style='padding:4px 0;color:#222;font-size:13px;'>{v}</td></tr>"
        for k, v in detail_rows)
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


def _list_blocks(items):
    # single-column stacked rows: two-column tables are unreadable on phones
    return "".join(
        f"<div style='padding:10px 0;border-bottom:1px solid #f0f0ee;'>"
        f"<a href='{it['url']}' style='color:#111;font-size:14px;font-weight:600;"
        f"text-decoration:none;line-height:1.4;'>{it['title']}</a>"
        f"<div style='margin-top:3px;color:#888;font-size:12px;line-height:1.4;'>{it['sub']}</div>"
        f"</div>" for it in items)


def email_list_html(heading, intro, sections, cta_url, cta_label, footer_html):
    """List email: sections = [(label_or_None, [{'title','sub','url'}...])]."""
    inner = ""
    for label, items in sections:
        if label:
            inner += (f"<p style='margin:16px 0 2px;font-size:11px;letter-spacing:1px;"
                      f"color:#999;text-transform:uppercase;'>{label}</p>")
        inner += _list_blocks(items)
    return f"""<div style="background:#f5f5f4;padding:24px 12px;">
<table role="presentation" style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e4e4e4;border-radius:12px;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;" width="100%">
<tr><td style="padding:28px;">
<p style="margin:0;font-size:11px;letter-spacing:1.5px;color:#b08900;text-transform:uppercase;">📡 venue radar</p>
<h2 style="margin:10px 0 6px;font-size:20px;color:#111;">{heading}</h2>
<p style="margin:0 0 8px;color:#444;font-size:14px;line-height:1.5;">{intro}</p>
{inner}
<a href="{cta_url}" style="display:inline-block;margin-top:20px;background:#1f6feb;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;">{cta_label}</a>
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
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"gh {args[:2]} failed: {e}") from e
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


def reminder_update(dl, reminded):
    """Which reminder to fire, collapsing tiers already passed.

    Returns (fire: bool, new_reminded: list). All tiers >= current days-left are
    marked at once so a venue first seen at T-2 doesn't get T-7 today and T-2
    tomorrow.
    """
    applicable = [f"T-{t}" for t in REMINDER_DAYS if dl <= t]
    fresh = [t for t in applicable if t not in reminded]
    return bool(fresh), list(reminded) + fresh


def should_send_digest(today_pt, last_digest):
    """Mondays once; catch up any day if the Monday run was lost. Idempotent per day."""
    if last_digest == today_pt.isoformat():
        return False
    if today_pt.weekday() == 0:
        return True
    if last_digest:
        try:
            return (today_pt - date.fromisoformat(last_digest)).days >= 8
        except ValueError:
            return False
    return False


def open_issue_numbers(repo):
    out = gh(["issue", "list", "-R", repo, "--state", "open", "--limit", "500",
              "--json", "number", "-q", ".[].number"])
    return {int(n) for n in out.split()} if out else set()


# --- orchestration ---

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
    for vid in changes["revived"]:
        # was archived (deadline passed / dropped from API) but is live again:
        # fresh issue, fresh reminders; verdict survives (same venue, same profile)
        sv[vid].update(archived=False, issue=None, reminded=[],
                       first_seen=now.strftime("%Y-%m-%d"))
    for vid, v in sv.items():
        if vid in current:
            v["invitations"] = current[vid]["invitations"]

    to_judge = [vid for vid, v in sv.items() if not v.get("archived") and not v.get("verdict")]
    rejudged = bool(state["profile_hash"]) and state["profile_hash"] != phash
    if rejudged:
        print("profile.md changed — re-judging all active venues")
        to_judge = [vid for vid, v in sv.items() if not v.get("archived")]
    classify_ok = True
    if args.skip_classify:
        classify_ok = not to_judge  # pending work skipped: don't advance profile hash
        to_judge = []
    newly_core = [vid for vid in changes["revived"] if sv[vid].get("verdict") == "core"]
    demoted = []
    for i in range(0, len(to_judge), BATCH):
        batch_ids = to_judge[i:i + BATCH]
        batch = [{"venue": vid, "title": sv[vid]["title"], "subtitle": sv[vid]["subtitle"],
                  "website": sv[vid]["website"],
                  "deadlines": _deadline_lines(sv[vid])} for vid in batch_ids]
        try:
            verdicts = classify_batch(profile, batch)
        except RuntimeError as e:
            print(f"[warn] batch unclassified: {e}")
            classify_ok = False
            continue
        for vid, verdict in verdicts.items():
            was = sv[vid].get("verdict")
            sv[vid]["verdict"], sv[vid]["why"] = verdict["verdict"], verdict["why"]
            sv[vid]["prestige"] = verdict.get("prestige", 2)
            if verdict["verdict"] == "core" and was != "core" and not sv[vid].get("issue"):
                newly_core.append(vid)
            elif was == "core" and verdict["verdict"] != "core" and sv[vid].get("issue"):
                demoted.append(vid)  # a profile re-judge downgraded it: retire its issue
        print(f"classified {min(i + BATCH, len(to_judge))}/{len(to_judge)}")

    # If gh itself is down, fail fast BEFORE any notification went out: nothing
    # is committed, nothing is sent, and tomorrow's run redoes everything cleanly.
    open_nums = open_issue_numbers(repo) if not dry else set()

    for vid in demoted:
        v = sv[vid]
        if not dry and v["issue"] in open_nums:
            try:
                close_issue(repo, v["issue"],
                            f"Re-judged as **{v['verdict']}** under the updated profile — "
                            "closing. It stays listed in venues.md.")
                open_nums.discard(v["issue"])
                time.sleep(1.0)
            except RuntimeError as e:
                print(f"[warn] demotion close failed for {vid}: {e}")
        elif dry:
            print(f"[dry] would close demoted issue #{v['issue']}: {v['title']}")
        v["issue"] = None
        v["reminded"] = []

    notified = set()
    promos = []
    # Every active core venue without an issue gets one — not just this run's
    # transitions. That makes issue creation self-healing after transient gh
    # failures/rate limits, and covers revived venues.
    need_issue = [vid for vid, v in sv.items()
                  if not v.get("archived") and v.get("verdict") == "core"
                  and not v.get("issue") and soonest_due(v, now)]
    need_issue.sort(key=lambda vid: soonest_due(sv[vid], now))
    for vid in need_issue:
        v = sv[vid]
        due = soonest_due(v, now)
        fresh = vid in newly_core
        mention = "" if backfill else "@harryila "
        if dry:
            print(f"[dry] would open issue{' + email' if fresh and not backfill else ''}: "
                  f"{subject_new(v['title'], due, now)}")
            continue
        try:
            v["issue"] = open_issue(repo, f"🎯 {v['title']} — due {fmt_pt(due)}",
                                    mention + issue_body(vid, v, now))
            open_nums.add(v["issue"])
            # discovered inside a reminder window: the creation notice already
            # says days-left, so pre-mark those tiers instead of pinging tomorrow
            v["reminded"] = reminder_update(days_left(due, now), [])[1]
            notified.add(vid)
            time.sleep(1.5)  # pace creates: GitHub secondary rate limit (backfill!)
        except RuntimeError as e:
            print(f"[warn] issue create failed for {vid} (retries next run): {e}")
        if fresh and not backfill:
            if rejudged:
                promos.append(vid)  # bulk promotions batch into ONE email below
                continue
            issue_ref = (f"Decide in <a href='https://github.com/{repo}/issues/{v['issue']}'>"
                         f"issue #{v['issue']}</a> — closing it silences reminders."
                         if v.get("issue") else "")
            send_email(subject_new(v["title"], due, now),
                       email_html(v["title"], v["why"],
                                  [("Due", f"{fmt_pt(due)} ({days_left(due, now)}d left)"),
                                   ("Deadlines", _deadline_lines(v)),
                                   ("Opened", v["first_seen"])],
                                  _cfp(vid), "Open CFP →", issue_ref),
                       f"{v['title']} — core match. Due {fmt_pt(due)}. {_cfp(vid)}")

    def core_items(pairs):
        return [{"title": f"{stars(v)} {v['title']}", "url": _cfp(vid),
                 "sub": f"due {fmt_pt(soonest_due(v, now))} "
                        f"({days_left(soonest_due(v, now), now)}d left) — {v['why'][:140]}"}
                for vid, v in pairs if soonest_due(v, now)]

    if promos and not dry:
        pairs = sorted([(vid, sv[vid]) for vid in promos], key=lambda t: _rank(t[1], now))
        items = core_items(pairs)
        send_email(f"🎯 {len(items)} newly core after your profile update",
                   email_list_html("Newly core",
                                   "Your profile edit promoted these to core:",
                                   [(None, items)],
                                   f"https://github.com/{repo}/blob/main/venues.md",
                                   "Open the radar →",
                                   "Each has a GitHub issue — close any you're skipping."),
                   "\n".join(f"{i['title']} — {i['sub']}" for i in items))

    if backfill and newly_core and not dry:
        cores = sorted([(vid, sv[vid]) for vid in newly_core], key=lambda t: _rank(t[1], now))
        items = core_items(cores)
        send_email(f"📡 Venue radar is live: {len(items)} core venues open now",
                   email_list_html("Backfill complete",
                                   "Every currently-open venue judged core for you — "
                                   "prestige-ranked, deadline-sorted:", [(None, items)],
                                   f"https://github.com/{repo}/blob/main/venues.md",
                                   "See the full radar →",
                                   "Each has a GitHub issue — close ones you're skipping."),
                   "\n".join(f"{i['title']} — {i['sub']}" for i in items))

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
                issue_comment(repo, v["issue"],
                              f"📅 Deadline changed: {fmt_pt(old_ms)} → "
                              f"**{fmt_pt(new_ms)}** ({days_left(new_ms, now)}d left)")
            except RuntimeError as e:
                print(f"[warn] comment failed: {e}")
            send_email(subj, email_html(v["title"], v["why"],
                       [("Was", fmt_pt(old_ms)),
                        ("Now", f"{fmt_pt(new_ms)} ({days_left(new_ms, now)}d left)")],
                       _cfp(vid), "Open CFP →", ""), subj)
            v["reminded"] = []
            notified.add(vid)

    for vid, v in sv.items():
        if (v.get("archived") or v.get("verdict") != "core" or not v.get("issue")
                or vid in notified):
            continue
        if not dry and v["issue"] not in open_nums:
            continue
        due = soonest_due(v, now)
        if not due:
            continue
        dl = days_left(due, now)
        fire, v["reminded"] = reminder_update(dl, v.get("reminded", []))
        if not fire:
            continue
        subj = subject_reminder(v["title"], due, now)
        if dry:
            print(f"[dry] would remind: {subj}")
            continue
        try:
            issue_comment(repo, v["issue"],
                          f"⏰ **{dl} days left** (due {fmt_pt(due)}). "
                          "Close this issue to stop reminders.")
        except RuntimeError as e:
            print(f"[warn] reminder comment failed: {e}")
        send_email(subj, email_html(v["title"], v["why"],
                   [("Due", f"{fmt_pt(due)} ({dl}d left)")], _cfp(vid), "Open CFP →",
                   "Closing the GitHub issue silences reminders."), subj)

    for vid in changes["expired"]:
        v = sv[vid]
        v["archived"] = True
        if v.get("issue") and not dry and v["issue"] in open_nums:
            try:
                close_issue(repo, v["issue"], "Deadline passed — archiving. 👋")
            except RuntimeError as e:
                print(f"[warn] close failed: {e}")

    today_pt = datetime.now(PT).date()
    if os.environ.get("FORCE_DIGEST") or should_send_digest(today_pt, state.get("last_digest", "")):
        bdate = state.get("backfill_date", "")
        week_new = [(vid, v) for vid, v in sv.items() if not v.get("archived")
                    and v.get("first_seen", "") != bdate  # backfill day isn't "new this week"
                    and (now - datetime.strptime(v["first_seen"], "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc)).days < 7]
        soon = sorted([(vid, v) for vid, v in sv.items() if not v.get("archived")
                       and v.get("verdict") in ("core", "adjacent") and soonest_due(v, now)
                       and days_left(soonest_due(v, now), now) <= 14],
                      key=lambda t: soonest_due(t[1], now))
        if (week_new or soon) and not backfill:
            n_core = sum(1 for _, v in week_new if v.get("verdict") == "core")
            subj = f"📬 Weekly: {n_core} new core, {len(soon)} deadlines in 14d"

            def digest_item(vid, v):
                d = soonest_due(v, now)
                when = (f"due {fmt_pt(d)} ({days_left(d, now)}d left)" if d
                        else "past due (grace)")
                return {"title": f"{stars(v)} {v['title']}", "url": _cfp(vid),
                        "sub": f"{v.get('verdict') or '?'} · {when}"}

            sections = []
            if week_new:
                wk = sorted(week_new, key=lambda t: _rank(t[1], now))
                label = (f"New this week ({len(wk)})" +
                         (" — top 20" if len(wk) > 20 else ""))
                sections.append((label, [digest_item(*t) for t in wk[:20]]))
            soon_core = [t for t in soon if t[1].get("verdict") == "core"]
            soon_adj = [t for t in soon if t[1].get("verdict") == "adjacent"]
            if soon_core:
                sections.append((f"Core deadlines ≤14d ({len(soon_core)})",
                                 [digest_item(*t) for t in soon_core]))
            if soon_adj:
                label = (f"Adjacent deadlines ≤14d ({len(soon_adj)})" +
                         (" — top 25" if len(soon_adj) > 25 else ""))
                sections.append((label, [digest_item(*t) for t in soon_adj[:25]]))
            if dry:
                print(f"[dry] would send digest: {subj}")
            else:
                send_email(subj, email_list_html("Weekly digest",
                           "Everything new this week, plus deadlines in the next 14 days.",
                           sections, f"https://github.com/{repo}/blob/main/venues.md",
                           "Open the radar →", ""), subj)
        if not dry:
            state["last_digest"] = today_pt.isoformat()

    if os.environ.get("RADAR_SUMMARY") and not dry:
        cores = sorted([(vid, v) for vid, v in sv.items() if not v.get("archived")
                        and v.get("verdict") == "core"], key=lambda t: _rank(t[1], now))
        items = core_items(cores)
        send_email(f"📡 Radar summary: {len(items)} core venues open",
                   email_list_html("Your radar right now",
                                   "All current core venues — prestige-ranked, "
                                   "deadline-sorted:", [(None, items)],
                                   f"https://github.com/{repo}/blob/main/venues.md",
                                   "Open the radar →",
                                   "Close a venue's GitHub issue to silence it."),
                   "\n".join(f"{i['title']} — {i['sub']}" for i in items))

    if classify_ok:
        state["profile_hash"] = phash
    elif state["profile_hash"] != phash:
        print("[warn] classification incomplete — keeping old profile hash so the "
              "re-judge retries next run")
    if backfill:
        state["backfill_date"] = now.strftime("%Y-%m-%d")
    state["last_run"] = now.isoformat()
    md = render(sv, {**changes, "new": sorted(changes["new"] + changes["revived"])},
                now, repo)
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
