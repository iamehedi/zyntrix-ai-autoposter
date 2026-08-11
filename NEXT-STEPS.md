# 🚀 Zyntrix Studio AI Autoposter — What To Do Now

A step-by-step guide to get your AI-Powered Facebook Autoposter live.

## ✅ Already done

- **Repo is live**: [github.com/iamehedi/zyntrix-ai-autoposter](https://github.com/iamehedi/zyntrix-ai-autoposter) — public, `main` branch pushed
- **Workflow is active** on GitHub (confirmed it parses correctly)
- **Model switched** to `qwen/qwen3.6-27b` — Groq's official replacement for `llama-3.3-70b-versatile` (which shuts down Aug 16, 2026). Qwen has native Bengali support, giving better Bangla output than the previous `gpt-oss-120b`.
- **Concurrency guard added** — prevents duplicate posts when runs overlap
- **Write permissions enabled** — the workflow can commit the updated `topics.txt` back to the repo

## ⏳ Remaining — only 3 things

### 1. Add the GitHub secrets (the only real blocker)

The workflow is live but has **zero secrets** — a run right now would fail immediately at the credential check. You need 4:

| Secret | Where to get it |
| :--- | :--- |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) |
| `FACEBOOK_PAGE_ID` | **Section 1 of the README** — Meta Developer setup |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | **Section 1 of the README** — permanent Page token |
| `POLLINATIONS_API_KEY` | Optional — can skip |

You can add them in the GitHub UI (**Settings → Secrets and variables → Actions**), or paste the values to your assistant and set them via `gh secret set`.

### 2. Run a manual test

Once secrets are in, either:

- Click **Actions → Zyntrix AI Facebook Autoposter → Run workflow**, or
- Ask your assistant to trigger it with `gh workflow run`.

### 3. Watch for the first auto-post

- The daily cron fires at **6:00 PM BST** every day.
- After each successful post, `topics.txt` shrinks by one topic.
- **10 topics = 10 days of content.** When it's empty, just push more topics to the file.

## 🧪 Optional: Test locally first

> ⚠️ Note: a successful local run **will post to the real Facebook page**.

1. Copy the template: `cp .env.example .env`
2. Fill in your credentials in `.env`
3. Install dependencies: `pip install -r requirements.txt`
4. Run the engine: `python app.py`

## 📚 Reference

- Full setup, troubleshooting, and architecture docs: `README.md`
