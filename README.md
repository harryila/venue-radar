# 📡 venue-radar

Push-only monitor for OpenReview venues open for submissions. Runs daily via
GitHub Actions; you never check anything — it emails **harry_ila@berkeley.edu**
when something core appears, reminds you before deadlines, and keeps
[venues.md](venues.md) current for browsing.

## How it works

1. Fetches every open-submission invitation from OpenReview (api2 + legacy api).
2. Diffs against [state.json](state.json): new venues, deadline changes, expiries.
3. Judges **new** venues once against [profile.md](profile.md) via `claude -p`
   running **Fable 5 at max reasoning effort** (verdict: core / adjacent / no,
   plus a 1–3 prestige rating). Editing profile.md re-judges everything active.
4. Renders [venues.md](venues.md) and notifies:
   - New **core** venue → GitHub issue (+ email). **The issue is your decision
     tracker: close it when you've decided, and that venue goes silent forever.**
   - Open issues get reminder comments + emails at T-7 and T-2 days, and
     comments when deadlines are extended.
   - Monday digest email: new this week + all deadlines in the next 14 days.

## Local usage

```bash
pip install requests pytest
pytest                                        # unit tests
python venue_radar.py --dry-run --skip-classify   # fetch + diff, no side effects
python venue_radar.py --dry-run                   # + classification (needs claude CLI)
```

## Secrets (repo → Settings → Secrets → Actions)

| Secret | What |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` |
| `GMAIL_ADDRESS` | sender Gmail |
| `GMAIL_APP_PASSWORD` | Gmail app password for the sender |

Design: [docs/superpowers/specs/2026-08-15-venue-radar-design.md](docs/superpowers/specs/2026-08-15-venue-radar-design.md)
