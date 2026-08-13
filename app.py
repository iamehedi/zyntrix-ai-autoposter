import os
import sys
import json
import re
import logging
import argparse
import requests
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Configure logging — ALL logs go to stderr so stdout carries ONLY the JSON payload
# the calling application consumes (content-generation contract: return only valid JSON).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr
)
logger = logging.getLogger("ZyntrixContentEngine")

# Load local .env if present
load_dotenv()

# Environment Credentials & Settings
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v20.0")
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Content & Scheduling Constants
DEFAULT_SLOT_TIMES = ["08:00", "12:00", "17:00", "20:00", "22:00"]  # Asia/Dhaka
DEFAULT_BATCH_SIZE = 10        # algorithm default: 10 posts per batch
MONTHLY_DAYS = 30              # monthly plan = 30 days
DHAKA_TZ = ZoneInfo("Asia/Dhaka")
HISTORY_FILE = "post_history.json"
PLAN_FILE = "content_plan.json"

# Meta scheduling window (Graph API): publish date must be between 10 minutes
# and 30 days from the request time. We use a small safety margin.
META_MIN_LEAD_SECONDS = 10 * 60
META_MAX_LEAD_SECONDS = (30 * 24 * 60 * 60) - (2 * 60 * 60)


# ---------------------------------------------------------------- validation
def validate_environment(plan_only=False):
    """Ensures required credentials exist. Facebook creds are only needed when
    we are actually going to schedule/publish (not for --plan-only / DRY_RUN)."""
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")

    if not plan_only and not DRY_RUN:
        if not FACEBOOK_PAGE_ID:
            missing.append("FACEBOOK_PAGE_ID")
        if not FACEBOOK_PAGE_ACCESS_TOKEN:
            missing.append("FACEBOOK_PAGE_ACCESS_TOKEN")

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        logger.error("Please configure them in your environment or GitHub Secrets.")
        sys.exit(1)

    if not GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY not set — Gemini fallback is disabled (Groq primary only).")


# ---------------------------------------------------------------- history
def load_post_history():
    """Loads previously generated/scheduled posts from post_history.json."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning(f"Could not load post history ({e}). Starting fresh.")
    return []


def save_post_history(history):
    """Persists post history to post_history.json (scheduled posts only)."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def collect_used_titles(history):
    """All previously generated titles (new schema) or topics (legacy schema),
    used as the uniqueness feed for the content engine."""
    titles = []
    for h in history:
        title = h.get("internal_title") or h.get("topic")
        if title:
            titles.append(title.strip())
    return titles


def load_plan_titles():
    """If a --monthly plan file exists, its titles are also fed to the engine so
    daily runs never duplicate a planned post."""
    if not os.path.exists(PLAN_FILE):
        return []
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [p.get("internal_title", "") for p in data if p.get("internal_title")]
    except Exception:
        return []


def topic_history_summary(used_titles, limit=15):
    """Compact summary of recent titles for the agent prompt."""
    if not used_titles:
        return "No previous posts yet — this is the first post."
    return " | ".join(used_titles[-limit:])


# ---------------------------------------------------------------- schedule slots
def next_slots(count, history, start_date=None, times=None):
    """Returns the next `count` free publishing slots (Asia/Dhaka).

    Slots are drawn from the fixed daily times (default 08:00/12:00/17:00/20:00/22:00),
    starting from the next available day, skipping slots already used in history
    and slots that are already in the past.
    """
    times = times or DEFAULT_SLOT_TIMES
    used = set()
    for h in history:
        d, t = h.get("scheduled_date"), h.get("scheduled_time")
        if d and t:
            used.add((d, t))

    now = datetime.now(DHAKA_TZ)
    if isinstance(start_date, str):
        # --start-date arrives as a string from the CLI
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    day = start_date or now.date()
    if day < now.date():
        day = now.date()

    slots = []
    while len(slots) < count:
        for t_str in times:
            slot_dt = datetime.combine(day, time.fromisoformat(t_str), tzinfo=DHAKA_TZ)
            if slot_dt <= now:
                continue  # already past — skip today's earlier slots
            if (day.isoformat(), t_str) in used:
                continue  # already assigned to a previous batch
            slots.append({
                "scheduled_date": day.isoformat(),
                "scheduled_time": t_str,
                "timezone": "Asia/Dhaka",
                "unix_ts": int(slot_dt.timestamp()),
            })
            if len(slots) == count:
                break
        day += timedelta(days=1)
    return slots


# ---------------------------------------------------------------- JSON parsing helpers
def _find_json_objects(text: str):
    """Yield all top-level JSON objects found in text via balanced-brace scan."""
    candidates = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start:i + 1]
                try:
                    candidates.append(json.loads(candidate))
                except Exception:
                    pass
                start = -1
    return candidates


def _escape_raw_newlines_in_strings(text: str) -> str:
    """Replace literal newlines inside JSON string literals with escaped \\n so
    json.loads accepts Qwen-style multi-line string values."""
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch == "\n":
                out.append("\\n")
                continue
            elif ch == "\r":
                out.append("\\r")
                continue
        else:
            if ch == '"':
                in_string = True
        out.append(ch)
    return "".join(out)


def parse_json_from_text(text: str) -> dict:
    """Extracts and parses the JSON object from an LLM response string."""
    try:
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        cleaned = _escape_raw_newlines_in_strings(cleaned)

        # Template-echo markers: if the LLM parrots the JSON format example
        # instead of writing real content, that object must never be selected.
        PLACEHOLDER_MARKERS = (
            "Final validated Facebook post",
            "Selected topic",
            "Technology category",
            "Content mode used",
            "Complete Facebook post text",
            "Short internal title",
        )

        def _is_template_echo(obj):
            blob = json.dumps(obj, ensure_ascii=False)
            return any(m in blob for m in PLACEHOLDER_MARKERS)

        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                if not _is_template_echo(parsed):
                    return parsed
            except Exception:
                pass

        candidates = _find_json_objects(cleaned)
        candidates = [c for c in candidates if not _is_template_echo(c)]
        if candidates:
            def _score(obj):
                return sum(k in obj for k in ("internal_title", "category", "caption", "hashtags", "scores", "status", "error_code"))
            scored = sorted(candidates, key=_score, reverse=True)
            if scored[0]:
                return scored[0]

        parsed = json.loads(cleaned)
        if not _is_template_echo(parsed):
            return parsed
        return {}
    except Exception as e:
        logger.error(f"Failed to parse JSON output from agent text: {e}")
        escaped_raw = text[:2000].replace(chr(10), "\\n")
        logger.warning(f"Raw text output: {escaped_raw}")
        return {}


# ---------------------------------------------------------------- content generation
def generate_post(history_summary: str, provider: str = "groq") -> dict:
    """Runs a single self-reviewing CrewAI agent that writes ONE history-story
    post (the algorithm's batch of 10 is assembled by the application from
    individual calls so each post stays inside the model's output budget).

    Returns the parsed JSON dict or {} on failure.
    """
    try:
        from crewai import Agent, Task, Crew, Process, LLM
    except ImportError as e:
        logger.error(f"CrewAI initialization failed. Ensure dependencies are installed: {e}")
        sys.exit(1)

    if provider == "gemini":
        if not GOOGLE_API_KEY:
            logger.error("Gemini fallback requested but GOOGLE_API_KEY is not set.")
            return {}
        logger.info(f"Initializing CrewAI with Gemini model: {GEMINI_MODEL}")
        llm = LLM(
            model=f"gemini/{GEMINI_MODEL}",
            api_key=GOOGLE_API_KEY,
            temperature=0.7,
            max_tokens=4096,
            extra_body={"thinking_config": {"thinking_budget": 0}}
        )
    else:
        logger.info(f"Initializing CrewAI with Groq model: {GROQ_MODEL}")
        llm = LLM(
            model=f"openai/{GROQ_MODEL}",
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
            temperature=0.7,
            max_tokens=4096
        )

    creator = Agent(
        role="Zyntrix History Storyteller — Facebook Content Engine",
        goal=(
            "Write natural, curiosity-driven Bengali storytelling posts about computer, "
            "programming, internet and AI history for The Zyntrix Studio Facebook page — "
            "posts that feel typed by a real Bangladeshi tech enthusiast, never AI-generated."
        ),
        backstory=(
            "You are the content-generation engine for a Facebook page called 'The Zyntrix Studio'.\n"
            "PAGE NICHE — the page focuses on interesting stories about: computer history, "
            "programming history, famous programmers and computer scientists, origins of programming "
            "languages, operating system history, internet and Web history, AI history, software "
            "development history, famous bugs / failures / inventions / accidents in computing, old "
            "computers and forgotten technologies, and important moments that changed modern technology.\n"
            "TARGET AUDIENCE — general Facebook users, especially people interested in technology who "
            "are NOT necessarily programmers. Content must be understandable to a normal Bangladeshi "
            "Facebook user.\n"
            "CORE CONTENT STYLE — write like a real Bangladeshi tech enthusiast telling an interesting "
            "story to a friend. The writing must NOT sound like: an AI-generated article, a Wikipedia "
            "article, a school textbook, a formal newspaper, or a corporate marketing post. Avoid "
            "excessive headings, excessive bullet points, excessive emojis, and artificial spacing. "
            "The writing should feel naturally typed by a human. Use natural Bangladeshi conversational "
            "language. Bengali is the PRIMARY language. English technical terms can remain in English "
            "when they are commonly used in Bangladesh (computer, programming, software, bug, code, "
            "internet, AI, Windows, Linux, etc.). Do not force a regional dialect into every sentence. "
            "CRITICAL: every Bengali word must be typed in proper Bengali Unicode script (বাংলা লিপি), "
            "NEVER in Romanized Bangla/Banglish (e.g. write 'সবচেয়ে বড়', NOT 'shobcheye boro').\n"
            "STORYTELLING FORMULA — whenever possible structure the story naturally like this (NEVER "
            "label these sections explicitly): 1) start with a strong curiosity-based opening; 2) "
            "introduce the situation or person; 3) slowly reveal what happened; 4) add surprising or "
            "lesser-known details; 5) explain why the event mattered; 6) connect the historical event "
            "to something people know today; 7) end with a memorable thought, question, or interesting "
            "observation. The first 1-3 sentences are extremely important because they determine "
            "whether someone stops scrolling. Example of the desired feeling: '১৯৪৭ সালে একটা বিশাল "
            "computer হঠাৎ কাজ করা বন্ধ করে দিল। Engineersরা অনেকক্ষণ ধরে কারণ খুঁজেও কিছু পাচ্ছিল না। "
            "শেষ পর্যন্ত তারা machine-এর ভেতর এমন একটা জিনিস পেল, যেটা আজও programming-এর একটা famous "
            "শব্দের সাথে জড়িয়ে আছে।' Then reveal the story naturally.\n"
            "LENGTH — normally generate 400-800 Bengali words per post. For simple topics, 300-500 "
            "words is acceptable. For particularly interesting historical stories, 700-1000 words is "
            "acceptable. Do not add unnecessary sentences just to increase word count.\n"
            "FACTUAL ACCURACY — historical accuracy is extremely important. NEVER invent: dates, names, "
            "quotes, locations, technical details, historical events, or claims about who invented "
            "something. If a fact is uncertain, do not present it as certain. If multiple historical "
            "sources disagree, mention the uncertainty naturally. Do not turn myths or popular internet "
            "stories into facts. If a topic cannot be written reliably without factual verification, "
            "return {\"status\": \"error\", \"error_code\": \"FACT_UNCERTAIN\", \"message\": \"explain "
            "which fact needs verification\"} instead of inventing details.\n"
            "CONTENT VARIETY — do not generate repetitive stories. Across posts rotate between: strange "
            "incidents, forgotten inventions, programmer stories, programming language origins, computer "
            "failures, famous bugs, historical rivalries, technology that failed, technology that "
            "unexpectedly succeeded, old hardware, internet history, software history, AI history, and "
            "interesting technical concepts explained through stories. Avoid starting every post with "
            "'আপনি কি জানেন...', 'কল্পনা করুন...' or 'আজ আমরা জানবো...'. Vary the hooks naturally and "
            "never use the same ending repeatedly.\n"
            "EMOJI POLICY — use emojis sparingly: normally 0-3 emojis per post, never fill the post "
            "with emojis.\n"
            "HASHTAGS — use 3-6 relevant hashtags at the end. Prefer hashtags such as #ComputerHistory, "
            "#Programming, #TechHistory, #Technology, #TheZyntrixStudio. Do not use irrelevant trending "
            "hashtags.\n"
            "SCHEDULING RULE — you are a content engine only. You NEVER claim a post has been scheduled "
            "or published. You only return content data (text + metadata); the Python application "
            "handles Meta authentication, scheduling, retries, errors and confirmation. Never fabricate "
            "a successful Meta API response.\n"
            "IMAGE WORKFLOW — do NOT generate or require an image for the post. Every post must work "
            "perfectly as text-only content.\n"
            "SELF-REVIEW before answering — silently check: 1) does the opening create curiosity? "
            "2) does the story feel human-written? 3) is the Bengali natural? 4) is it understandable "
            "to a non-programmer? 5) are the historical claims reasonable and not invented? 6) is the "
            "story sufficiently detailed? 7) is it different from previous posts (avoid the provided "
            "titles)? 8) are emojis limited (0-3)? 9) are hashtags relevant (3-6)? 10) is the JSON "
            "valid? Assign honest scores (usefulness>=8, uniqueness>=8, human_feel>=8, "
            "technical_accuracy>=9, promotional_feel<=3, ai_like_feel<=3). If any check fails, fix the "
            "post before returning it."
        ),
        verbose=False,
        allow_delegation=False,
        llm=llm
    )

    task = Task(
        description=(
            f"Previous generated titles (NEVER repeat these, and keep every post meaningfully "
            f"different): {history_summary}\n"
            "Write ONE complete history-storytelling Facebook post in natural conversational Bangla — "
            "in proper Bengali Unicode script (বাংলা লিপি), NEVER Romanized Bangla/Banglish. Follow your "
            "role rules: curiosity-based opening, natural story reveal, 400-800 Bengali words, 0-3 "
            "emojis, 3-6 relevant hashtags, text-only (no image), never claim scheduling/publishing. "
            "Pick a fresh topic from the page niche that is clearly different from every title above.\n"
            "Format your final answer as valid JSON with exactly these keys:\n"
            "{\n"
            '  "internal_title": "Short internal title",\n'
            '  "category": "Category (e.g. Computer History / Programming History / Famous Programmers / Internet History / AI History / Famous Bugs)",\n'
            '  "caption": "Complete Facebook post text",\n'
            '  "hashtags": ["#ComputerHistory", "#Programming"],\n'
            '  "scores": {"usefulness": 9, "uniqueness": 9, "human_feel": 9, "technical_accuracy": 10, "promotional_feel": 1, "ai_like_feel": 1}\n'
            "}\n"
            "Do not include any explanation outside the JSON. Never echo the format template back — the "
            "JSON above is only a structure example; every value must be your real content."
        ),
        expected_output="A JSON object containing internal_title, category, caption, hashtags, and scores.",
        agent=creator
    )

    crew = Crew(
        agents=[creator],
        tasks=[task],
        process=Process.sequential,
        verbose=False
    )

    logger.info("Executing CrewAI content engine (story generation + self-review)...")
    try:
        result = crew.kickoff()
        raw_result_str = str(result)
    except Exception as e:
        logger.error(f"Provider call failed ({type(e).__name__}): {str(e)[:400]}")
        return {}

    parsed_output = parse_json_from_text(raw_result_str)

    caption = parsed_output.get("caption", "") if parsed_output else ""
    if caption:
        logger.info(f"Generated post ({parsed_output.get('internal_title', 'untitled')}):")
        for line in caption.splitlines():
            logger.info(line)
        hashtags = parsed_output.get("hashtags")
        if hashtags:
            logger.info(" " + " ".join(hashtags))
    else:
        logger.warning("No usable 'caption' content in agent output.")

    return parsed_output


def _content_usable(content_data, used_titles, word_min=300, word_max=1000) -> bool:
    """Deterministic quality gate for ONE generated post."""
    if not isinstance(content_data, dict) or not content_data:
        return False

    # Algorithm error responses (MISSING_INPUT / FACT_UNCERTAIN) are never usable.
    if content_data.get("status") == "error":
        logger.warning(f"Content engine returned an error: {content_data.get('error_code')} — "
                       f"{content_data.get('message', '')[:200]}")
        return False

    caption = content_data.get("caption", "")
    if not caption or any(m in caption for m in ("Final validated Facebook post", "Selected topic", "Complete Facebook post text")):
        return False

    word_count = len(caption.split())
    if not (word_min <= word_count <= word_max):
        logger.warning(f"Content failed quality gate: caption is {word_count} words "
                       f"(allowed {word_min}-{word_max}).")
        return False

    title = (content_data.get("internal_title") or "").strip()
    if not title:
        logger.warning("Content failed quality gate: missing internal_title.")
        return False
    if title in used_titles:
        logger.warning(f"Content failed quality gate: duplicate title '{title}'.")
        return False

    hashtags = content_data.get("hashtags") or []
    if not (3 <= len(hashtags) <= 6):
        logger.warning(f"Content failed quality gate: {len(hashtags)} hashtags (allowed 3-6).")
        return False
    if not all(isinstance(h, str) and h.startswith("#") for h in hashtags):
        logger.warning("Content failed quality gate: invalid hashtag format.")
        return False

    # Soft emoji check (warning only — emoji detection regexes are not exhaustive).
    emoji_count = len(re.findall(
        r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]",
        caption
    ))
    if emoji_count > 3:
        logger.warning(f"Emoji count {emoji_count} exceeds the algorithm's 0-3 guideline (warning only).")

    scores = content_data.get("scores") or {}

    def _num(key, default):
        try:
            return float(scores.get(key, default))
        except (TypeError, ValueError):
            return default

    # Lenient defaults: a missing score is treated as passing.
    failures = [
        ("usefulness", _num("usefulness", 8) < 8),
        ("uniqueness", _num("uniqueness", 8) < 8),
        ("human_feel", _num("human_feel", 8) < 8),
        ("technical_accuracy", _num("technical_accuracy", 9) < 9),
        ("promotional_feel", _num("promotional_feel", 0) > 3),
        ("ai_like_feel", _num("ai_like_feel", 0) > 3),
    ]
    failed = [k for k, bad in failures if bad]
    if failed:
        logger.warning(f"Content failed quality gate: {failed}")
        return False
    return True


def generate_usable_post(used_titles, max_attempts=2) -> dict:
    """Generates ONE post via Groq (primary, retried) then Gemini (fallback).
    Returns {} if nothing usable was produced."""
    history_summary = topic_history_summary(used_titles)

    for attempt in range(1, max_attempts + 1):
        logger.info(f"Attempt {attempt}/{max_attempts} — primary provider: Groq ({GROQ_MODEL})")
        content = generate_post(history_summary, provider="groq")
        if _content_usable(content, used_titles):
            return content
        logger.warning(f"Groq returned unusable output (attempt {attempt}/{max_attempts}). Retrying...")

    if GOOGLE_API_KEY:
        for attempt in range(1, max_attempts + 1):
            logger.info(f"Attempt {attempt}/{max_attempts} — fallback provider: Gemini ({GEMINI_MODEL})")
            content = generate_post(history_summary, provider="gemini")
            if _content_usable(content, used_titles):
                return content
            logger.warning(f"Gemini returned unusable output (attempt {attempt}/{max_attempts}). Retrying...")
    else:
        logger.warning("No GOOGLE_API_KEY configured — skipping Gemini fallback.")

    return {}


# ---------------------------------------------------------------- Meta scheduling
def _token_binding_check() -> dict:
    """Confirms the configured token is a Page Access Token bound to the
    intended Page, without ever logging the token. Returns {} on success."""
    try:
        r = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/me",
            params={"fields": "id,name", "access_token": FACEBOOK_PAGE_ACCESS_TOKEN},
            timeout=30
        )
        data = r.json()
    except Exception as e:
        return {"http_status": 0, "code": None, "type": "TokenCheckException",
                "message": f"Token binding check request failed: {e}"}

    if r.status_code != 200 or not data.get("id"):
        err = data.get("error", {}) if isinstance(data, dict) else {}
        return {"http_status": r.status_code, "code": err.get("code"), "type": err.get("type"),
                "message": err.get("message", str(data)[:500])}

    if str(data.get("id")) != str(FACEBOOK_PAGE_ID):
        logger.error(f"Token belongs to '{data.get('name')}' (id {data.get('id')}), NOT the configured "
                     f"FACEBOOK_PAGE_ID ({FACEBOOK_PAGE_ID}). Scheduling aborted.")
        return {"http_status": 200, "code": None, "type": "TokenPageMismatch",
                "message": "Token is not bound to the configured Page."}

    logger.info(f"Token binding OK: Page Access Token for '{data.get('name')}' (id {data.get('id')}).")
    return {}


def schedule_post(caption: str, hashtags: list, unix_ts: int) -> dict:
    """Schedules ONE text-only post via Meta Graph API.

    Meta scheduling: POST /{page-id}/feed with published=false and
    scheduled_publish_time=<unix ts>. Never treated as success from HTTP 200
    alone — the created object is re-fetched and must confirm it is scheduled
    (is_published=false and scheduled_publish_time matches).

    Returns {"status": "SCHEDULED_VERIFIED", "post_id": ...} on success, or a
    status in {FAILED, OUT_OF_SCHEDULE_WINDOW} with a structured error.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    lead = unix_ts - now_ts
    if lead < META_MIN_LEAD_SECONDS:
        return {"status": "OUT_OF_SCHEDULE_WINDOW", "post_id": "", "error": {
            "type": "ScheduleTimeTooSoon",
            "message": f"Requested slot is only {int(lead // 60)} minutes away; Meta requires at least 10 minutes."}}
    if lead > META_MAX_LEAD_SECONDS:
        return {"status": "OUT_OF_SCHEDULE_WINDOW", "post_id": "", "error": {
            "type": "ScheduleTimeTooFar",
            "message": f"Requested slot is {lead / 86400:.1f} days away; Meta allows scheduling at most 30 days in advance."}}

    binding_error = _token_binding_check()
    if binding_error:
        logger.error(f"Facebook Scheduling Error | HTTP Status: {binding_error.get('http_status')} | "
                     f"Error Type: {binding_error.get('type')} | Message: {binding_error.get('message')}")
        return {"status": "FAILED", "post_id": "", "error": binding_error}

    hashtags_formatted = " ".join(hashtags).strip() if hashtags else ""
    full_text = caption.strip()
    if hashtags_formatted and hashtags_formatted not in full_text:
        full_text += f"\n\n{hashtags_formatted}"

    api_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{FACEBOOK_PAGE_ID}/feed"

    # 1. Schedule the post.
    try:
        response = requests.post(
            api_url,
            data={
                "message": full_text,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
                "published": "false",
                "scheduled_publish_time": str(unix_ts)
            },
            timeout=60
        )
        res_json = response.json()
    except Exception as e:
        logger.error(f"Facebook Scheduling Error | HTTP Status: N/A | Error Type: RequestException | "
                     f"Message: {e} | Endpoint: {api_url}")
        return {"status": "FAILED", "post_id": "",
                "error": {"http_status": None, "code": None, "type": "RequestException",
                           "message": str(e)[:500], "endpoint": api_url}}

    if response.status_code != 200 or not isinstance(res_json, dict) or not res_json.get("id"):
        err = res_json.get("error", {}) if isinstance(res_json, dict) else {}
        detail = {"http_status": response.status_code, "code": err.get("code"), "type": err.get("type"),
                  "message": err.get("message", response.text[:500]), "endpoint": api_url}
        logger.error(f"Facebook Scheduling Error | HTTP Status: {detail['http_status']} | "
                     f"Error Code: {detail['code']} | Error Type: {detail['type']} | "
                     f"Message: {detail['message']} | Endpoint: {detail['endpoint']}")
        return {"status": "FAILED", "post_id": "", "error": detail}

    post_id = res_json["id"]
    logger.info(f"Facebook accepted scheduled post (Post ID: {post_id} | scheduled_publish_time: {unix_ts})")

    # 2. Verify: the object must exist, be unpublished and scheduled for the exact time.
    verify_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{post_id}"
    try:
        v = requests.get(verify_url, params={"fields": "id,is_published,scheduled_publish_time,status_type",
                                             "access_token": FACEBOOK_PAGE_ACCESS_TOKEN}, timeout=60)
        v_json = v.json()
    except Exception as e:
        logger.error(f"Scheduled post verification request failed: {e}")
        return {"status": "FAILED", "post_id": post_id,
                "error": {"http_status": None, "code": None, "type": "VerificationException",
                           "message": str(e)[:500], "endpoint": verify_url}}

    if v.status_code != 200 or not isinstance(v_json, dict) or not v_json.get("id"):
        err = v_json.get("error", {}) if isinstance(v_json, dict) else {}
        logger.error(f"Scheduled post verification FAILED — object not readable via API: "
                     f"{err.get('message', str(v_json)[:500])}")
        return {"status": "FAILED", "post_id": post_id,
                "error": {"http_status": v.status_code, "code": err.get("code"), "type": err.get("type"),
                           "message": err.get("message", str(v_json)[:500]), "endpoint": verify_url}}

    actual_ts = v_json.get("scheduled_publish_time")
    if v_json.get("is_published") is not False or actual_ts != unix_ts:
        logger.error(f"Scheduled post verification FAILED — expected unpublished post at {unix_ts}, "
                     f"got is_published={v_json.get('is_published')} scheduled_publish_time={actual_ts}")
        return {"status": "FAILED", "post_id": post_id,
                "error": {"http_status": 200, "code": None, "type": "ScheduleMismatch",
                           "message": "Post exists but is not scheduled for the requested time.",
                           "endpoint": verify_url}}

    logger.info(f"POST STATUS: Scheduled and verified. | POST ID: {post_id} | "
                f"Publish time: {datetime.fromtimestamp(unix_ts, tz=DHAKA_TZ).isoformat()} Asia/Dhaka")
    return {"status": "SCHEDULED_VERIFIED", "post_id": post_id, "error": {}}


# ---------------------------------------------------------------- output shaping
def build_post_payload(content: dict, slot: dict, scheduling_status: str, meta_post_id: str = "") -> dict:
    """Shapes one generated post into the algorithm's output format."""
    payload = {
        "post_id": f"{slot['scheduled_date'].replace('-', '')}-{slot['scheduled_time'].replace(':', '')}",
        "internal_title": content.get("internal_title", ""),
        "category": content.get("category", ""),
        "scheduled_date": slot["scheduled_date"],
        "scheduled_time": slot["scheduled_time"],
        "timezone": slot["timezone"],
        "caption": content.get("caption", ""),
        "hashtags": content.get("hashtags", []),
        "image_required": False,
        "scheduling_status": scheduling_status,
    }
    if meta_post_id:
        payload["meta_post_id"] = meta_post_id
    return payload


def failed_post_payload(slot: dict, stage: str) -> dict:
    """Placeholder payload when generation or scheduling failed for a slot."""
    return {
        "post_id": f"{slot['scheduled_date'].replace('-', '')}-{slot['scheduled_time'].replace(':', '')}",
        "internal_title": "",
        "category": "",
        "scheduled_date": slot["scheduled_date"],
        "scheduled_time": slot["scheduled_time"],
        "timezone": slot["timezone"],
        "caption": "",
        "hashtags": [],
        "image_required": False,
        "scheduling_status": stage,
    }


# ---------------------------------------------------------------- monthly plan
def run_monthly_plan(args, history, used_titles):
    """Generates a full 30-day plan (150 posts) in sub-batches of `--batch`
    (default 10). Each sub-batch is printed as one JSON object as soon as it is
    generated. With --schedule, posts within Meta's window are scheduled and
    verified; otherwise everything is marked PLANNED (plan-only preview)."""
    total = MONTHLY_DAYS * len(args.times)
    slots = next_slots(total, history, start_date=args.start_date, times=args.times)
    plan = []
    batch_number = 0
    any_failed = False

    for i in range(0, len(slots), args.batch):
        batch_number += 1
        chunk = slots[i:i + args.batch]
        posts = []
        for slot in chunk:
            content = generate_usable_post(used_titles, args.attempts)
            if not content:
                posts.append(failed_post_payload(slot, "GENERATION_FAILED"))
                any_failed = True
                continue
            used_titles.append(content["internal_title"])
            payload = build_post_payload(content, slot, "PLANNED")
            if args.schedule:
                result = schedule_post(content["caption"], content.get("hashtags", []), slot["unix_ts"])
                payload["scheduling_status"] = result["status"]
                if result["status"] == "SCHEDULED_VERIFIED":
                    payload["meta_post_id"] = result["post_id"]
                    history.append(history_entry(content, slot, result["post_id"]))
                else:
                    any_failed = True
                    payload["error"] = result.get("error", {})
            posts.append(payload)
            plan.append(payload)

        batch = {"status": "success", "batch_number": batch_number, "posts": posts}
        print(json.dumps(batch, ensure_ascii=False, indent=2), flush=True)

    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    logger.info(f"Monthly plan saved to {PLAN_FILE} ({len(plan)} posts).")
    if history:
        save_post_history(history)
    logger.info("Monthly plan generation complete.")
    sys.exit(1 if any_failed else 0)


def history_entry(content: dict, slot: dict, meta_post_id: str) -> dict:
    return {
        "internal_title": content.get("internal_title", ""),
        "category": content.get("category", ""),
        "scheduled_date": slot["scheduled_date"],
        "scheduled_time": slot["scheduled_time"],
        "timezone": slot["timezone"],
        "status": "SCHEDULED_VERIFIED",
        "meta_post_id": meta_post_id,
        "generated_at": datetime.now(timezone.utc).isoformat()
    }


# ---------------------------------------------------------------- CLI
def parse_args():
    parser = argparse.ArgumentParser(
        description="Zyntrix Studio — Facebook history-storytelling content engine & Meta scheduler."
    )
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Number of posts per batch (default {DEFAULT_BATCH_SIZE}).")
    parser.add_argument("--plan-only", action="store_true",
                        help="Generate the batch and print the JSON plan WITHOUT calling Meta.")
    parser.add_argument("--monthly", action="store_true",
                        help="Generate a full 30-day plan (150 posts) in sub-batches of --batch.")
    parser.add_argument("--schedule", action="store_true",
                        help="With --monthly: schedule posts via Meta instead of plan-only preview.")
    parser.add_argument("--start-date", type=str, default=None,
                        help="First slot date as YYYY-MM-DD (default: next free slot from today, Asia/Dhaka).")
    parser.add_argument("--times", type=str, default=",".join(DEFAULT_SLOT_TIMES),
                        help=f"Comma-separated daily publish times HH:MM (default: {','.join(DEFAULT_SLOT_TIMES)}).")
    parser.add_argument("--attempts", type=int, default=2,
                        help="Generation attempts per post per provider (default 2).")
    return parser.parse_args()


def main():
    args = parse_args()
    args.times = [t.strip() for t in args.times.split(",") if t.strip()]
    if not args.times:
        args.times = DEFAULT_SLOT_TIMES
    if args.start_date:
        try:
            datetime.strptime(args.start_date, "%Y-%m-%d")
        except ValueError:
            logger.error("--start-date must be YYYY-MM-DD.")
            sys.exit(1)

    plan_only = args.plan_only or DRY_RUN
    logger.info("Starting Zyntrix Studio Content & Scheduling Engine (History Storytelling Mode)")
    validate_environment(plan_only=plan_only)

    history = load_post_history()
    used_titles = collect_used_titles(history) + load_plan_titles()

    if args.monthly:
        run_monthly_plan(args, history, used_titles)
        return  # exits inside

    # --- Default: generate the next batch and schedule it via Meta ---
    slots = next_slots(args.batch, history, start_date=args.start_date, times=args.times)
    logger.info(f"Next {len(slots)} publishing slots assigned "
                f"({slots[0]['scheduled_date']} {slots[0]['scheduled_time']} → "
                f"{slots[-1]['scheduled_date']} {slots[-1]['scheduled_time']}, Asia/Dhaka).")

    posts = []
    any_failed = False

    for slot in slots:
        content = generate_usable_post(used_titles, args.attempts)
        if not content:
            posts.append(failed_post_payload(slot, "GENERATION_FAILED"))
            any_failed = True
            logger.error(f"Content generation failed for slot {slot['scheduled_date']} "
                         f"{slot['scheduled_time']} — slot left unassigned.")
            continue

        used_titles.append(content["internal_title"])

        if plan_only:
            # Preview mode: never touch Meta, never claim anything was scheduled.
            payload = build_post_payload(content, slot, "PLANNED")
            posts.append(payload)
            continue

        result = schedule_post(content["caption"], content.get("hashtags", []), slot["unix_ts"])
        if result["status"] == "SCHEDULED_VERIFIED":
            payload = build_post_payload(content, slot, "SCHEDULED_VERIFIED", result["post_id"])
            history.append(history_entry(content, slot, result["post_id"]))
        else:
            any_failed = True
            payload = build_post_payload(content, slot, result["status"])
            payload["error"] = result.get("error", {})
            logger.error(f"Scheduling failed for slot {slot['scheduled_date']} {slot['scheduled_time']}: "
                         f"{result.get('error', {}).get('message', result['status'])}")
        posts.append(payload)

    batch = {"status": "success", "batch_number": 1, "posts": posts}
    print(json.dumps(batch, ensure_ascii=False, indent=2))

    if plan_only:
        logger.info(f"PLAN-ONLY preview complete: {len(posts)} post(s) generated, nothing scheduled, "
                    "no Meta API calls made.")
        sys.exit(0 if not any_failed else 1)

    save_post_history(history)
    logger.info(f"Post history updated ({len(history)} scheduled posts recorded).")
    scheduled_count = sum(1 for p in posts if p.get("scheduling_status") == "SCHEDULED_VERIFIED")
    logger.info(f"Batch complete: {scheduled_count}/{len(posts)} post(s) scheduled and verified via Meta.")

    if any_failed:
        logger.error("One or more posts could not be scheduled. Unverified posts are NOT recorded "
                     "in history and will be retried on the next run.")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
