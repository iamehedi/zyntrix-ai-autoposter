# 🚀 Zyntrix Studio — History Storytelling Facebook Scheduler: What To Do Now

A step-by-step guide to get the new engine live. It generates Bengali **computer/programming/internet/AI
history stories** and **schedules** them on the page via the Meta Graph API — one daily GitHub Actions
run covers the next 2 days (5 posts/day at 08:00/12:00/17:00/20:00/22:00 Asia/Dhaka).

## ✅ Already done

- **New content algorithm implemented** — `app.py` now generates history-storytelling posts (400–800
  Bengali words, curiosity hooks, 0–3 emojis, 3–6 hashtags) per `IMAGELESS-CONTENT-AGENT.md` and
  **schedules** them via Meta (`published=false` + `scheduled_publish_time`) with verification.
- **Batch + monthly plan support** — `--batch`, `--plan-only`, `--monthly`, `--schedule`, `--start-date`,
  `--times`.
- **Autonomous topic discovery** — the `topics.txt` queue is gone; uniqueness is enforced from
  `post_history.json` instead.
- **Workflow updated** — one daily run at 02:30 UTC (08:30 Dhaka) schedules the next batch; commits
  `post_history.json` back to the repo.

## ⚠️ Security note (important)

The previous version of this file contained a **real Facebook Page access token**. It has been removed
and replaced with a placeholder. If that old version was ever pushed to GitHub, the token is
compromised — regenerate it now from Meta Business Suite and update the GitHub secret.

## ⏳ Remaining — only 3 things

### 1. Add the GitHub secrets (the only real blocker)

The workflow is live but needs secrets — a run right now would fail at the credential check:

| Secret | Where to get it |
| :--- | :--- |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) |
| `FACEBOOK_PAGE_ID` | `1303242722866748` (The Zyntrix Studio) |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Permanent Page token from `/me/accounts` (README Section 1) |
| `GOOGLE_API_KEY` | Optional — Gemini fallback; can skip |

Set them in **Settings → Secrets and variables → Actions**, or paste the values here and set them via
`gh secret set`.

### 2. Run a manual test

Once secrets are in, either:

- Click **Actions → Zyntrix AI Facebook Autoposter → Run workflow**, or
- Ask your assistant to trigger it with `gh workflow run`.

The run generates 10 posts, schedules them via Meta, verifies each, and commits `post_history.json`.
Watch the log for `POST STATUS: Scheduled and verified` per post.

### 3. Watch the schedule fill up

- Each daily run schedules the next 2 days (10 slots).
- Verify slots under **Meta Business Suite → Scheduled posts**.
- To preview a batch locally first: `python app.py --plan-only --batch 2` (no Meta calls).

## 🧪 Optional: Test locally

> ⚠️ Note: a non-dry-run local run **will schedule real posts on the real Facebook page**.

1. Copy the template: `cp .env.example .env`
2. Fill in your credentials in `.env`
3. Install dependencies: `pip install -r requirements.txt`
4. Preview without posting: `python app.py --plan-only --batch 2`
5. Schedule for real: `python app.py --batch 10`

## 📚 Reference

- Content algorithm spec: `IMAGELESS-CONTENT-AGENT.md`
- Full setup, troubleshooting, and architecture docs: `README.md`
