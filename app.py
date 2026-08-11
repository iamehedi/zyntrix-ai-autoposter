import os
import sys
import json
import re
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ZyntrixAutoposter")

# Load local .env if present
load_dotenv()

# Environment Credentials & Settings
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v20.0")

HISTORY_FILE = "post_history.json"


# Validate Critical Environment Variables
def validate_environment():
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if not FACEBOOK_PAGE_ID:
        missing.append("FACEBOOK_PAGE_ID")
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        missing.append("FACEBOOK_PAGE_ACCESS_TOKEN")

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        logger.error("Please configure them in your environment or GitHub Secrets.")
        sys.exit(1)


# Queue Management Functions
def get_next_topic(file_path="topics.txt"):
    """Reads topics.txt and returns the first available non-empty topic and remaining list.

    Returns (None, []) when the queue is missing or empty, which switches the
    pipeline to autonomous topic discovery.
    """
    if not os.path.exists(file_path):
        logger.warning(f"Topic file '{file_path}' not found. Switching to autonomous topic discovery.")
        return None, []

    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]

    valid_topics = [line for line in lines if line and not line.startswith("#")]

    if not valid_topics:
        logger.warning("Topic queue is empty. Switching to autonomous topic discovery.")
        return None, []

    selected_topic = valid_topics[0]
    return selected_topic, valid_topics


def remove_topic_from_queue(topic_to_remove, file_path="topics.txt"):
    """Removes published topic from topics.txt after successful Facebook upload."""
    if not os.path.exists(file_path):
        return

    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    removed = False
    for line in lines:
        stripped = line.strip()
        if not removed and stripped == topic_to_remove:
            removed = True
            continue
        new_lines.append(line)

    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    logger.info(f"Topic successfully removed from queue: '{topic_to_remove}'")


# Post History Tracking
def load_post_history():
    """Loads previously published posts from post_history.json."""
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
    """Persists post history to post_history.json."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def topic_history_summary(history, limit=10):
    """Builds a compact text summary of recent post topics for the agent."""
    topics = [h.get("topic", "") for h in history if h.get("topic")]
    if not topics:
        return "No previous posts yet — this will be the first post."
    return " | ".join(topics[-limit:])


# Helper for Parsing JSON from Agent Outputs
def _find_json_objects(text: str):
    """Yield all top-level JSON objects found in text via balanced-brace scan.

    Handles nested braces and braces inside string literals correctly, so a
    JSON template echoed inside <think>/reasoning text won't confuse us.
    """
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
    """Replace literal newlines found inside JSON string literals with escaped
    \\n so json.loads accepts Qwen-style multi-line string values (Qwen often
    writes paragraph breaks in Bengali posts as raw newlines instead of \\n)."""
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
    """Extracts and parses JSON object from LLM response string."""
    try:
        # Strip reasoning/thinking blocks (e.g. Qwen <think>...</think>) that may
        # contain braces and break naive JSON extraction below.
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Qwen writes raw newlines inside JSON string values; escape them first.
        cleaned = _escape_raw_newlines_in_strings(cleaned)

        # Template-echo markers: LLMs sometimes parrot the JSON format example
        # from the prompt (placeholder values like "Final validated Facebook post
        # in Bengali") instead of writing real content. Such objects must never
        # be selected, or we would publish the template itself.
        PLACEHOLDER_MARKERS = (
            "Final validated Facebook post",
            "Selected topic",
            "Technology category",
            "Content mode used",
        )

        def _is_template_echo(obj):
            blob = json.dumps(obj, ensure_ascii=False)
            return any(m in blob for m in PLACEHOLDER_MARKERS)

        # 1. Match json codeblock if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                if not _is_template_echo(parsed):
                    return parsed
            except Exception:
                pass  # fall through to full scan below

        # 2. Balanced-brace scan: pick the most likely payload object.
        #    Prefer objects carrying expected content keys.
        candidates = _find_json_objects(cleaned)
        candidates = [c for c in candidates if not _is_template_echo(c)]
        if candidates:
            def _score(obj):
                return sum(k in obj for k in ("approved", "post", "topic", "hashtags", "scores", "reason", "content_mode"))
            scored = sorted(candidates, key=_score, reverse=True)
            if scored[0]:
                return scored[0]

        # 3. Last resort: whole-text parse (must not return a template echo)
        parsed = json.loads(cleaned)
        if not _is_template_echo(parsed):
            return parsed
        return {}
    except Exception as e:
        logger.error(f"Failed to parse JSON output from agent text: {e}")
        escaped_raw = text[:2000].replace(chr(10), "\\n")  # no backslash inside f-string (Py 3.11)
        logger.warning(f"Raw text output: {escaped_raw}")
        return {}


# CrewAI AI Content Pipeline (Imageless, single self-reviewing agent)
def generate_and_validate_content(topic, history_summary) -> dict:
    """Runs a single CrewAI agent that writes the post AND self-reviews it,
    returning JSON with a quality scorecard for the programmatic gate."""
    try:
        from crewai import Agent, Task, Crew, Process, LLM
    except ImportError as e:
        logger.error(f"CrewAI initialization failed. Ensure dependencies are installed: {e}")
        sys.exit(1)

    logger.info(f"Initializing CrewAI with Groq model: {GROQ_MODEL}")

    # Initialize Groq LLM (via its OpenAI-compatible endpoint)
    # Using the openai/ provider prefix avoids LiteLLM injecting unsupported
    # params (e.g. cache_breakpoint) that Groq rejects when using groq/ prefix.
    llm = LLM(
        model=f"openai/{GROQ_MODEL}",
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        temperature=0.7,
        # Single self-reviewing agent: prompt ~700 tokens + output <=3200 = ~3900
        # per call, so even the retry (x2) stays under the free-tier 8000 TPM
        # limit. Qwen 3.6 is a reasoning model that burns up to ~2500 tokens of
        # <think> reasoning before emitting JSON; 2048 was proven insufficient.
        max_tokens=3200
    )

    # Creator & Editor Agent — writes the post, then silently self-reviews it
    creator = Agent(
        role="Zyntrix Imaginative Tech Content Creator",
        goal="Create natural, human, occasionally funny Bengali Facebook posts that teach technology in a simple, entertaining way — never requiring an image.",
        backstory=(
            "You are the technology brain behind Zyntrix Studio's Facebook page, a software, web, "
            "mobile app, AI and automation development company. You write like a knowledgeable human "
            "developer explaining interesting things to a friend: useful, entertaining, technically "
            "accurate, and never like an AI, a textbook, or a sales bot.\n"
            "LANGUAGE: natural conversational Bangla. Keep common tech terms in English (Website, App, "
            "API, UI/UX, SEO, CRM, Database, Cloud, etc.) — never force awkward Bengali translations.\n"
            "STYLE: funny + tutorial + simple explanation. Use everyday situations, relatable "
            "experiences, small jokes and analogies to make concepts easy. The joke must always serve "
            "the explanation (70-90% useful info, 10-30% humor).\n"
            "CONTENT MODES — pick ONE each run: Funny Explanation, Mini Tutorial, ELI5 Technology, "
            "'What Actually Happens?', Tech Myth vs Reality, Developer Life, or Tech Story.\n"
            "RULES: 100-250 words; 0-1 emoji (max 2); 0-3 relevant hashtags; CTA optional and never "
            "forced; plain Facebook text (no headings, tables, heavy formatting); avoid AI-sounding "
            "phrases; never invent statistics, clients, benchmarks or company facts; branding must be "
            "subtle and only where it fits naturally.\n"
            "This is an IMAGELESS system: never mention, request, or describe images.\n"
            "Uniqueness: previous topics are provided — pick something meaningfully different and "
            "rotate between technology categories.\n"
            "SELF-REVIEW before answering: silently run a quality checklist — sounds human? natural "
            "Bangla? strong opening? 100-250 words? teaches something real? humor on-topic? technically "
            "accurate? non-promotional? different from previous posts? readable as text-only? Assign "
            "honest scores (usefulness>=8, uniqueness>=8, human_feel>=8, technical_accuracy>=9, "
            "promotional_feel<=3, ai_like_feel<=3). If any fails, rewrite the post until it passes."
        ),
        verbose=False,
        allow_delegation=False,
        llm=llm
    )

    # Define Task
    topic_line = f"Topic: '{topic}'" if topic else "Topic: none provided — discover your own interesting technology topic."
    creator_task = Task(
        description=(
            f"{topic_line}\n"
            f"Previous topics (avoid repeating these): {history_summary}\n"
            "Write a complete text-only Facebook post in natural conversational Bangla (tech terms in "
            "English) following your brand style, then self-review it. Format your final answer as JSON "
            "with exactly these keys:\n"
            "{\n"
            '  "topic": "Selected topic",\n'
            '  "category": "Technology category",\n'
            '  "content_mode": "One of: Funny Explanation / Mini Tutorial / ELI5 / What Actually Happens / Myth vs Reality / Developer Life / Tech Story",\n'
            '  "post": "Complete Facebook post in Bengali",\n'
            '  "hashtags": ["#Example"],\n'
            '  "scores": {"usefulness": 9, "uniqueness": 9, "human_feel": 9, "technical_accuracy": 10, "promotional_feel": 1, "ai_like_feel": 1}\n'
            "}\n"
            "Do not include any explanation outside the JSON. Never echo the format template back — "
            "the JSON above is only a structure example; every value must be your real content."
        ),
        expected_output="A JSON object containing topic, category, content_mode, post, hashtags, and scores.",
        agent=creator
    )

    # Form Crew and Execute
    crew = Crew(
        agents=[creator],
        tasks=[creator_task],
        process=Process.sequential,
        verbose=False
    )

    logger.info("Executing CrewAI agent workflow (Creator & self-review)...")
    result = crew.kickoff()
    raw_result_str = str(result)

    escaped_output = raw_result_str[:1500].replace(chr(10), "\\n")  # no backslash inside f-string (Py 3.11)
    logger.info(f"CrewAI raw output (first 1500 chars): {escaped_output}")
    raw_attr = getattr(result, "raw", None)
    if raw_attr is not None and str(raw_attr) != raw_result_str:
        escaped_raw_attr = str(raw_attr)[:1500].replace(chr(10), "\\n")
        logger.info(f"CrewAI result.raw (first 1500 chars): {escaped_raw_attr}")
    parsed_output = parse_json_from_text(raw_result_str)
    logger.info(f"Parsed content data: {json.dumps(parsed_output, ensure_ascii=False)[:1000]}")
    return parsed_output


def _content_usable(content_data) -> bool:
    """Deterministic quality gate: post text present, no template echo, and
    self-reported scores meet the brand guide minima (when scores exist)."""
    if not content_data:
        return False
    if content_data.get("approved") is False:
        return False
    post_text = content_data.get("post", "")
    if not post_text or any(m in post_text for m in ("Final validated Facebook post", "Selected topic")):
        return False
    scores = content_data.get("scores") or {}

    def _num(key, default):
        try:
            return float(scores.get(key, default))
        except (TypeError, ValueError):
            return default

    # Defaults are lenient: a missing score is treated as passing (Qwen
    # sometimes omits the scores block entirely).
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


# Facebook Meta Graph API Publishing (text-only)
def publish_text_post(post_text: str, hashtags: list) -> str:
    """Publishes a text-only post to the Facebook Page via the /feed endpoint."""
    logger.info("Publishing text-only post to Facebook Page via Meta Graph API...")

    hashtags_formatted = " ".join(hashtags).strip() if hashtags else ""
    full_text = post_text.strip()
    if hashtags_formatted and hashtags_formatted not in full_text:
        full_text += f"\n\n{hashtags_formatted}"

    api_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{FACEBOOK_PAGE_ID}/feed"

    try:
        response = requests.post(
            api_url,
            data={
                "message": full_text,
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN
            },
            timeout=60
        )
        res_json = response.json()

        if response.status_code == 200 and res_json.get("id"):
            post_id = res_json["id"]
            logger.info(f"Successfully published text post to Facebook Page! Facebook Post ID: {post_id}")
            return post_id
        else:
            error_msg = res_json.get("error", {}).get("message", response.text)
            logger.error(f"Facebook Graph API Error (HTTP {response.status_code}): {error_msg}")
            return ""
    except Exception as e:
        logger.error(f"Exception while posting to Facebook API: {e}")
        return ""


# Main Orchestration Loop
def main():
    logger.info("Starting Zyntrix Studio AI Autoposter Engine (Imageless Mode)")
    validate_environment()

    # 1. Hybrid topic source: use queue when available, otherwise autonomous
    history = load_post_history()
    history_summary = topic_history_summary(history)

    current_topic, queue = get_next_topic("topics.txt")
    if current_topic:
        logger.info(f"Selected Queue Topic: '{current_topic}'")
    else:
        logger.info("Topic queue empty — Creator agent will discover its own topic.")

    # 2. Execute the single-agent pipeline.
    #    Retry once if the output is unparsable, a template echo, or fails the
    #    quality score gate.
    content_data = {}
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        content_data = generate_and_validate_content(current_topic, history_summary)
        if content_data.get("approved") is False:
            logger.error(f"Content rejected by agent: {content_data.get('reason', 'no reason given')}")
        if _content_usable(content_data):
            break
        logger.warning(f"Content generation returned unusable output (attempt {attempt}/{max_attempts}). Retrying...")

    if not _content_usable(content_data):
        logger.error("Content generation returned no usable output after retries.")
        logger.error("Aborting posting process. Topic remains in queue.")
        sys.exit(1)

    logger.info("Content approved by Zyntrix quality gate (self-review scores OK)!")
    post_text = content_data.get("post", "")
    hashtags = content_data.get("hashtags", [])

    # 3. Publish text-only post to Facebook Page
    post_id = publish_text_post(post_text, hashtags)

    if post_id:
        # 4. Remove processed topic from queue only on successful post
        if current_topic:
            remove_topic_from_queue(current_topic, "topics.txt")

        # 5. Record post history for future uniqueness checks
        history.append({
            "topic": content_data.get("topic") or current_topic or "(autonomous topic)",
            "category": content_data.get("category", ""),
            "content_mode": content_data.get("content_mode", ""),
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "post_id": post_id
        })
        save_post_history(history)
        logger.info(f"Post history updated ({len(history)} posts recorded).")

        logger.info("Autoposter pipeline execution completed successfully.")
        sys.exit(0)
    else:
        logger.error("Facebook posting failed. Topic remains in queue for future retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
