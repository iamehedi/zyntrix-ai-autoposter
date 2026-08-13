# 🚀 Zyntrix Studio — History Storytelling Facebook Scheduler

An autonomous, cloud-based content engine for **The Zyntrix Studio** Facebook page. It generates
natural **Bengali history-storytelling posts** (computer history, programming history, famous programmers,
internet/AI history, famous bugs, forgotten technologies…) and **schedules** them on the page via the
**Meta Graph API** — no images required, no manual posting.

The whole system runs on **GitHub Actions**: one run per day generates the next batch of posts and
schedules them with Meta (which publishes at the exact Asia/Dhaka slot times). Zero local machine uptime.

> 📖 **Content algorithm spec:** [`IMAGELESS-CONTENT-AGENT.md`](IMAGELESS-CONTENT-AGENT.md) is the
> canonical prompt/spec the content agent follows (niche, storytelling formula, length, accuracy,
> hashtag/emoji policy, output format). It is enforced both by the agent prompt and the programmatic
> quality gate in `app.py`.

---

## 🏗️ System Architecture

```text
┌───────────────────────────────┐
│  post_history.json            │  ← previously generated titles (uniqueness feed)
└──────────────┬────────────────┘
               ▼
┌───────────────────────────────┐
│  Content Engine (CrewAI+Groq) │  ← one self-reviewing agent per post
│  Bengali history-story post   │     (Qwen 3.6, 400–800 words, 3–6 hashtags)
└──────────────┬────────────────┘
               │   quality gate (_content_usable)
               ▼
┌───────────────────────────────┐
│  Slot Assigner (Asia/Dhaka)   │  ← next free slots: 08:00/12:00/17:00/20:00/22:00
└──────────────┬────────────────┘
               ▼
┌───────────────────────────────┐
│  Meta Graph API Scheduler     │  ← published=false + scheduled_publish_time
│  + verification (is_published │     Never claims success from HTTP 200 alone
│   = false & time matches)     │
└──────────────┬────────────────┘
               ▼
┌───────────────────────────────┐
│  Record in post_history.json  │  ← only SCHEDULED_VERIFIED posts are recorded,
│  & commit back to GitHub      │     unverified ones are retried next run
└───────────────────────────────┘
```

**Content engine and scheduling engine are strictly separated:**

| Content engine (AI) | Scheduling engine (Python app) |
| :--- | :--- |
| post text | Meta authentication / Page ID / access token |
| internal title, category | API requests (`published=false` + `scheduled_publish_time`) |
| hashtags | retries, error handling, verification, confirmation |

The AI **never claims** a post was scheduled — only the Python app reports it, and only after the Meta
API confirms (`is_published=false` and matching `scheduled_publish_time` on the created object).

---

## ✍️ Content Algorithm (summary)

- **Niche:** stories from computer / programming / internet / AI / software history — old computers,
  famous programmers, language origins, famous bugs, forgotten tech.
- **Style:** a Bangladeshi tech enthusiast telling a friend an interesting story — curiosity-based
  opening → reveal → surprising detail → why it mattered → connection to today → memorable ending.
  Bengali primary, common tech terms stay in English, proper Bengali Unicode (no Banglish).
- **Length:** 400–800 words (300–500 simple, 700–1000 for great stories).
- **Accuracy:** never invent dates, names, quotes or events; flag uncertainty naturally; return
  `FACT_UNCERTAIN` instead of guessing.
- **Emojis:** 0–3. **Hashtags:** 3–6, preferring `#ComputerHistory #Programming #TechHistory
  #Technology #TheZyntrixStudio`.
- **Images:** never required — every post works text-only (`image_required: false`).
- **Uniqueness:** previous titles from `post_history.json` (and `content_plan.json`) are fed back so
  no topic repeats.

Full rules: [`IMAGELESS-CONTENT-AGENT.md`](IMAGELESS-CONTENT-AGENT.md).

---

## 🔐 1. Facebook Page Credentials Setup

To let the app schedule posts on your Page you need a **Permanent Page Access Token** and the **Page ID**.

### Step 1: Create a Meta Developer Account & App
1. Go to [Meta for Developers Portal](https://developers.facebook.com/) and log in.
2. Click **My Apps** → **Create App** → select **Other** → **Business**.
3. Set an App Display Name (e.g., `Zyntrix Autoposter`) and finish creation.

### Step 2: Request Permissions in Graph API Explorer
1. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/).
2. Select your app; under **User or Page** choose **User Token**.
3. Add permissions: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`.
4. Click **Generate Access Token** and approve.

### Step 3: Get Page ID + Page Access Token
1. Run `GET /v20.0/me/accounts`.
2. Find **The Zyntrix Studio**:
   - `id` → `FACEBOOK_PAGE_ID`
   - `access_token` → temporary `FACEBOOK_PAGE_ACCESS_TOKEN`

### Step 4: Convert to a Permanent Page Access Token
1. Open the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/).
2. Paste the user token → **Debug** → **Extend Access Token** (60 days).
3. Use the 60-day token in the Explorer to run `GET /me/accounts` again — Page Access Tokens issued to a
   Business app remain valid indefinitely (unless password changes / permission revocations).

---

## ⚙️ 2. GitHub Secrets Setup

Add these as repository secrets (**Settings → Secrets and variables → Actions**):

| Secret | Description | Example / Required Format |
| :--- | :--- | :--- |
| `GROQ_API_KEY` | API key from [Groq Console](https://console.groq.com) | `gsk_...` |
| `FACEBOOK_PAGE_ID` | Numeric Facebook Page ID | `1303242722866748` |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Permanent Page Access Token | `EAAG...` |
| `GOOGLE_API_KEY` | (Optional) Gemini fallback key | `AIza...` |
| `GROQ_MODEL` | (Optional) Groq model override | `qwen/qwen3.6-27b` |
| `GRAPH_API_VERSION` | (Optional) Meta Graph API Version | `v20.0` |

---

## 💻 3. Local Setup & Testing

### Prerequisites
Python 3.10+ and Git.

### Steps

1. **Clone & enter the repo**, then create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   ```
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure environment:** copy `.env.example` to `.env` and fill in credentials.
4. **Preview a batch without touching Facebook:**
   ```bash
   python app.py --plan-only --batch 2
   ```
   This generates 2 posts for the next free slots, prints the JSON plan, and exits — **no Meta API calls**.
5. **Schedule for real:**
   ```bash
   python app.py --batch 10
   ```
   Generates 10 posts (2 days of the 5/day schedule), schedules each via Meta, verifies each, and records
   the verified ones in `post_history.json`. Exits `0` only if everything was scheduled; unverified posts
   are left unrecorded so the next run retries them.

> ⚠️ **A real (non-dry-run) local run schedules posts on the real Facebook page.**

### CLI Reference

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--batch N` | `10` | Posts per batch (algorithm default). |
| `--plan-only` | off | Generate + print the JSON plan, never call Meta. |
| `--monthly` | off | Generate a full **30-day / 150-post plan** in sub-batches of `--batch` (prints one JSON object per sub-batch, saves `content_plan.json`). |
| `--schedule` | off | With `--monthly`: schedule the plan via Meta instead of previewing. |
| `--start-date YYYY-MM-DD` | next free slot | First slot date (custom date/time support). |
| `--times 08:00,12:00,...` | `08:00,12:00,17:00,20:00,22:00` | Daily publish times (Asia/Dhaka). |
| `--attempts N` | `2` | Generation attempts per post per provider. |

`DRY_RUN=1` behaves like `--plan-only` (used by the benchmark workflow).

---

## 🤖 4. GitHub Actions Automation

### Daily automatic schedule
```yaml
schedule:
  - cron: "30 2 * * *"   # 02:30 UTC = 08:30 Asia/Dhaka
```
Each daily run:
1. Loads `post_history.json` (uniqueness feed).
2. Generates the next batch of **10 posts** and assigns the next free slots (2 days × 5 slots/day).
3. Schedules each post via Meta (`published=false` + `scheduled_publish_time`) and **verifies** it.
4. Commits updated `post_history.json` (and `content_plan.json` if a monthly plan exists) back to the repo.

A `concurrency` guard (`cancel-in-progress: false`) ensures manual + scheduled runs never overlap, so two
runs can never claim the same publishing slot.

> [!NOTE]
> Because Meta owns the actual publishing time, GitHub cron delays (10–30 min) no longer affect post
> timing at all. Run the workflow manually anytime via **Actions → Run workflow**.

### Scheduling rules enforced by the app
- Slots must be **≥ 10 minutes** in the future and **≤ 30 days** ahead (Meta's window).
- A post is only recorded as scheduled after **re-fetching the created object** and confirming
  `is_published=false` and `scheduled_publish_time` matches.
- The AI content engine never reports scheduling — only the verified API result is reported.

---

## 📋 5. Topic Lifecycle

- **No manual topic queue.** Topics are discovered autonomously by the content engine from the page
  niche (see `IMAGELESS-CONTENT-AGENT.md`).
- **Uniqueness:** every generated title is appended to `post_history.json`; the next run receives the
  recent titles and must pick something meaningfully different.
- If a post fails to schedule, it is **not** recorded; the run exits `1` and GitHub Actions alerts you,
  and the slot is retried on the next run.

---

## 🛠️ 6. Troubleshooting & Common Errors

### 1. `Facebook Scheduling Error | HTTP 400/401: Invalid OAuth access token`
- **Cause:** Page token expired, invalid, or missing `pages_manage_posts`.
- **Fix:** regenerate the token following Section 1.

### 2. `(#100) The specified scheduled publish time is invalid`
- **Cause:** slot outside Meta's 10-min–30-day window.
- **Fix:** slots are auto-assigned inside the window; this surfaces only with a custom `--start-date` far in the future.

### 3. `CrewAI initialization failed / Groq API error`
- **Cause:** invalid `GROQ_API_KEY` or deprecated `GROQ_MODEL`.
- **Fix:** verify the key and check [Groq Models](https://console.groq.com/docs/models).

### 4. Rate limits (429) during batch generation
- **Cause:** 10 long posts ≈ 10 LLM calls per batch; free-tier Groq TPM limits can be exceeded.
- **Fix:** use a smaller batch (`--batch 3`) or a paid Groq tier.

### 5. `GitHub Push Error in Workflow`
- **Cause:** workflow lacks write permissions to commit `post_history.json`.
- **Fix:** **Settings → Actions → General → Workflow permissions → Read and write permissions**.

### 6. Duplicate / missing posts
- Posting timing is handled by Meta. Verify a slot under **Meta Business Suite → Scheduled posts**.
- If a scheduled post disappears from the queue, check Meta's content policies — the app only reports
  what the API confirms.

---

## 🛡️ 7. Security & Anti-Spam Guidelines

- **Never commit secrets.** `.env` is in `.gitignore`; credentials live only in GitHub Secrets.
- **Never claim success without confirmation.** The app never reports a scheduled post from HTTP 200
  alone — the object is verified first.
- **No spam.** 5 posts/day is a controlled volume; Meta may throttle pages that post very frequently
  with low engagement. Consider 1–3/day for a newer page.

---

## 🔌 8. Modular Architecture & Future Extensions

`app.py` is organized into focused, replaceable functions:

- `generate_post()` / `generate_usable_post()` — content engine (CrewAI + Groq, Gemini fallback).
- `next_slots()` — Asia/Dhaka slot assignment (swap in custom calendars here).
- `schedule_post()` — Meta Graph API scheduling + verification (swap for other platforms).
- `run_monthly_plan()` — 30-day plan generation in batches.

Easy extensions: LinkedIn/X cross-posting, a Notion/Sheets slot source, or a comment-reply agent.

---

## 🔗 Official Documentation Links

- [Meta Graph API — Posts (scheduling)](https://developers.facebook.com/documentation/pages-api/posts)
- [Meta Graph API Explorer](https://developers.facebook.com/tools/explorer/)
- [Groq Developer Documentation](https://console.groq.com/docs)
- [CrewAI Framework Documentation](https://docs.crewai.com/)

---

*Built with ❤️ for **Zyntrix Studio** — Software, Web Development & AI Solutions.*
