# venue-radar — Design Spec

**Date:** 2026-08-15 · **Repo:** `harryila/venue-radar` (private) · **Status:** approved design, pre-implementation

## Purpose

Harry keeps discovering OpenReview "Open for Submissions" venues (COLM AIMs, AAAI AI Alignment) after their windows close. venue-radar is a daily, fully push-based monitor: it fetches every open-submission venue on OpenReview, judges relevance to Harry's work once per venue via Claude, keeps a sliding window of open opportunities, and pings him **only** when something worth his attention happens. He never has to check anything on a schedule.

## Data source (validated 2026-08-15)

- `GET https://api2.openreview.net/invitations?invitee=~&pastdue=false&limit=1000` — returned exactly 340 invitations, matching the openreview.net homepage list in content and order. Each invitation carries `cdate` (opened), `duedate`, `expdate`, and `id` like `EMNLP/2026/Workshop/WaC-13/-/Submission`.
- `GET https://api.openreview.net/invitations?invitee=~&pastdue=false` — legacy v1 host, 3 remaining venues. Merged with v2.
- Venue metadata: `GET /groups?id=<venue-prefix>` for the venue's real title, subtitle, and website (needed for cryptic names like "SurgTwin"). Fetched only when a venue is first seen.
- Some venues expose multiple invitations (e.g. abstract + full paper). Invitations are grouped by venue prefix (the id up to `/-/`) into one venue entry with all its deadlines.

## Architecture

```
venue_radar.py        # entire engine: fetch, diff, classify, render, notify (~250 lines)
profile.md            # Harry's research profile + core/adjacent/no rules — the classifier's spec
state.json            # every venue ever seen, verdicts, issue numbers (committed each run)
venues.md             # rendered dashboard, regenerated every run
.github/workflows/daily.yml   # cron + workflow_dispatch
tests/                # fixtures + unit tests for fetch-parsing, diff, render
```

No framework, no dependencies beyond `requests` (stdlib `smtplib` for email).

## Daily pipeline (cron 13:00 UTC ≈ 6am PT, plus manual `workflow_dispatch`)

1. **Fetch** both hosts, group invitations by venue prefix.
2. **Diff** against `state.json`:
   - *New venue* → fetch its group metadata, queue for classification.
   - *Deadline changed* → record; if venue is core with an open issue, comment on the issue ("extended to Sep 30, +9 days").
   - *All invitations expired/pastdue* → mark archived; drop from `venues.md`; close its issue if open.
3. **Classify** queued venues in batches (~40/call) via `claude -p --output-format json` using the subscription OAuth token. Prompt = `profile.md` + per-venue: title, subtitle, website, invitation id, deadlines. Output = strict JSON array of `{venue, verdict: core|adjacent|no, why}` (one line why). Verdict cached in state.
   - **Re-judge on profile change:** state stores `sha256(profile.md)`. When it differs, all *non-archived* venues are re-classified (one batched pass). Verdict changes flow through the normal notification path (newly-core → ping). This makes editing `profile.md` an actual steering wheel.
   - First run classifies all ~340 (~9 batched calls, one-time).
4. **Render** `venues.md`: 🆕 New since last run → 🎯 Core sorted by soonest deadline (title, days left, opened date, all deadlines, why, CFP link, issue link) → 🔭 Adjacent (compact table). Archived venues live only in state.
5. **Notify** (see below).
6. **Commit** `state.json` + `venues.md` in one commit. On any fetch/classify error, fail the run and commit nothing — state advances atomically; next day self-heals.

## Notifications — push-only, silence is the default

Channels activate by presence of their secrets; the engine calls one `notify()` layer.

**GitHub Issues (always on — the decision tracker):**
- New **core** venue → open an issue mentioning `@harryila`. Title: `🎯 <venue> — due <date>`.
- Issue **open** = "still considering": bot comments at **T-7** and **T-2** days before the soonest deadline (reminder), and when a deadline extends.
- Issue **closed** by Harry = decided (submitting or skipping) → no further pings for that venue, ever.
- Deadline passes → bot closes the issue with a farewell comment.
- Adjacent venues never open issues.

**Email (on when Gmail secrets present):** sent via Gmail SMTP (`smtp.gmail.com:465`, app password) from `hilanyan2004@gmail.com` **to `harry_ila@berkeley.edu`**. GitHub-native notification emails are not used for primary alerts (fixed subjects, global routing only); the Action sends its own so subject and body are fully controlled.
- Subjects, designed for the lock screen:
  - New core: `🎯 Open: <venue> — due <MMM D> (<N>d)`
  - Reminder: `⏰ <N> days left: <venue> (due <MMM D>)`
  - Extension: `📅 Extended +<N>d: <venue> → <MMM D>`
  - Digest: `📬 Weekly: <n> new core, <m> deadlines in 14d`
- Body: clean single-card HTML (inline styles, table layout) — venue name, the one-line why, all deadlines with days-left, prominent CFP link, small link to the GitHub issue.
- Email failure never fails the run (issue still exists); logged as a warning.

**Weekly digest (Mondays, same cron with a day check):** one email + one digest issue: everything new that week (core and adjacent) plus a table of all deadlines in the next 14 days. Backstop so nothing slips through.

Expected volume: 1–3 emails/week, most weeks fewer.

## State schema (`state.json`)

```json
{
  "profile_hash": "sha256...",
  "last_run": "2026-08-15T13:00:00Z",
  "venues": {
    "EMNLP/2026/Workshop/WaC-13": {
      "title": "...", "subtitle": "...", "website": "...",
      "invitations": [{"id": "...", "cdate": 0, "duedate": 0, "expdate": 0, "kind": "Submission"}],
      "first_seen": "...", "verdict": "core", "why": "...",
      "issue": 12, "reminded": ["T-7"], "archived": false
    }
  }
}
```

## Failure handling

- OpenReview or Claude hard-fails → run fails visibly (Actions email), nothing committed.
- `claude -p` returns malformed JSON → one retry; still bad → affected venues rendered in `venues.md` as *unclassified* (listed, never silently dropped) and retried next run.
- Email/issue API errors → warn, continue; the committed state is source of truth.

## Testing

- `tests/` with fixture JSON captured from the live API: unit tests for invitation grouping, diffing (new/changed/expired), days-left math, and `venues.md` rendering.
- `--dry-run` flag: full pipeline, prints would-be notifications, no commits/issues/emails. Also the loop for tuning `profile.md` locally.

## Secrets & Harry's manual steps

| Secret | Source |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` (run locally, one time) |
| `GMAIL_ADDRESS` | `hilanyan2004@gmail.com` |
| `GMAIL_APP_PASSWORD` | Google Account → Security → 2-Step Verification → App passwords |

`MAIL_TO` defaults to `harry_ila@berkeley.edu` in the workflow env (not a secret).

## Deliberately parked (v2)

- Non-OpenReview sources: CMT/EasyChair venues, aideadlin.es YAML for major-conference cycles before they open. The source layer is a function returning normalized venue dicts, so new sources slot in without touching diff/classify/notify.
- Richer per-venue extraction (page limits, archival status).
- GitHub Pages dashboard (needs paid plan on private repos; `venues.md` suffices).

## Cost envelope

- Actions: ~1–2 min/day ≈ 40 min/month of the 2,000 free private-repo minutes.
- Claude: ~3–10 new venues/day, one batched call — negligible against the subscription; first-run backfill ~9 calls, one time.

## Post-review hardening (2026-08-15)

An adversarial multi-agent review (20 confirmed findings) tightened the design:

- **Grace windows:** venues between `duedate` and `expdate` have no future due;
  every render path handles that ("past due (grace)") instead of crashing.
- **Profile-hash atomicity:** the stored hash only advances when classification
  fully succeeded, so an edit's re-judge pass retries after claude failures.
- **Revival:** an archived venue that reappears (extension after expiry, new
  cycle on the same id) is un-archived with a fresh issue and reminders.
- **Persistent issue creation:** every active core venue without an issue gets
  one each run (paced 1.5s for GitHub's secondary rate limit) — self-healing
  after transient gh failures.
- **Fail-fast on gh:** if listing open issues fails, the run aborts before any
  notification, keeping runs atomic.
- **Reminder tiers collapse** (late discovery ≠ two pings on consecutive days)
  and same-run double-notifications are suppressed.
- **Digest:** idempotent per day, catches up a missed Monday, excludes the
  backfill cohort from "new this week".
- **CI:** subprocess timeouts are caught; the state commit rebases onto any
  profile.md push that landed mid-run.
