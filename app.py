import os
import sys
import json
import re
import random
import logging
import urllib.parse
import requests
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
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "")

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
    """Reads topics.txt and returns the first available non-empty topic and remaining list."""
    if not os.path.exists(file_path):
        logger.error(f"Topic file '{file_path}' not found.")
        sys.exit(1)

    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]

    valid_topics = [line for line in lines if line and not line.startswith("#")]
    
    if not valid_topics:
        logger.warning("Topic queue is empty. Nothing to post today.")
        sys.exit(0)

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


def parse_json_from_text(text: str) -> dict:
    """Extracts and parses JSON object from LLM response string."""
    try:
        # Strip reasoning/thinking blocks (e.g. Qwen <think>...</think>) that may
        # contain braces and break naive JSON extraction below.
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

        # 1. Match json codeblock if present
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except Exception:
                pass  # fall through to full scan below

        # 2. Balanced-brace scan: pick the most likely payload object.
        #    Prefer objects carrying expected content keys (approved/hook/etc).
        candidates = _find_json_objects(cleaned)
        if candidates:
            def _score(obj):
                return sum(k in obj for k in ("approved", "hook", "caption", "hashtags", "image_prompt", "reason"))
            scored = sorted(candidates, key=_score, reverse=True)
            if scored[0]:
                return scored[0]

        # 3. Last resort: whole-text parse
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"Failed to parse JSON output from agent text: {e}")
        logger.warning(f"Raw text output: {text[:2000]}")
        return {}


# CrewAI AI Content Pipeline
def generate_and_validate_content(topic: str) -> dict:
    """Runs CrewAI 3-Agent pipeline to write, design, and validate content."""
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
        # Qwen 3.6 is a reasoning model: it spends tokens on <think> blocks
        # before the final JSON. max_tokens=2048 is the default but stated
        # explicitly; a higher value would exceed the free-tier 8000 TPM limit
        # once agent context accumulates (prompt+output > 8000). Truncation is
        # handled by a retry loop in main() + a robust JSON parser.
        max_tokens=2048
    )

    # 1. Writer Agent
    writer = Agent(
        role="Bengali Technology Content Writer",
        goal="Create engaging, natural Bengali technology posts for Zyntrix Studio targeting business owners and developers.",
        backstory=(
            "You are a senior technology writer for Zyntrix Studio, a premier software, web development, and AI solutions agency. "
            "You write in clear, natural Bengali. You keep technical terms (like AI, API, Cloud, Frontend, Backend, SaaS, Automation, Database) "
            "in natural English script. You craft strong hooks, valuable practical insights, natural call-to-actions, and 3-5 relevant hashtags "
            "including #ZyntrixStudio. You never use textbook/robotic Bengali or spammy promises."
        ),
        verbose=False,
        allow_delegation=False,
        llm=llm
    )

    # 2. Designer Agent
    designer = Agent(
        role="Zyntrix Social Media Visual Designer",
        goal="Formulate detailed visual prompts for 1080x1080 social media graphics following Zyntrix brand aesthetics.",
        backstory=(
            "You are the Lead Visual Designer at Zyntrix Studio. You design dark-mode tech visuals. "
            "Zyntrix Brand Direction: Dark slate/charcoal background (#1E1E1E), subtle green/cyan/blue accents, modern geometric shapes, "
            "clean developer aesthetics, abstract software architecture, digital UI elements, controlled lighting, minimal clutter. "
            "Avoid generic AI robots, excessive neon glow, stock photos, competitor logos, and text clutter."
        ),
        verbose=False,
        allow_delegation=False,
        llm=llm
    )

    # 3. Manager Agent
    manager = Agent(
        role="Zyntrix Content Quality Manager",
        goal="Review writer and designer outputs for Zyntrix tone, Bengali naturalness, factual safety, brand consistency, and Facebook policy.",
        backstory=(
            "You are the Editorial Director and Brand Guardian at Zyntrix Studio. "
            "You strictly enforce: 70% educational/value, 20% industry insight, 10% promotional ratio. "
            "You ensure no false claims ('100% guarantee', fake reviews/stats), natural non-robotic Bengali, "
            "professional tone, concise structure, and brand alignment. You reject substandard posts."
        ),
        verbose=False,
        allow_delegation=False,
        llm=llm
    )

    # Define Tasks
    writer_task = Task(
        description=(
            f"Topic: '{topic}'\n"
            "Write a Facebook post in natural Bengali with appropriate English tech terms for Zyntrix Studio.\n"
            "Format your final answer as JSON with these keys:\n"
            "{\n"
            '  "hook": "Attention-grabbing hook line in Bengali",\n'
            '  "caption": "Short explanation, practical insight, takeaway, and concise CTA",\n'
            '  "hashtags": ["#ZyntrixStudio", "#WebDevelopment", "#AI"]\n'
            "}"
        ),
        expected_output="A JSON object containing hook, caption, and hashtags.",
        agent=writer
    )

    designer_task = Task(
        description=(
            f"Topic: '{topic}'\n"
            "Create a detailed 1080x1080 visual prompt for Pollinations AI matching Zyntrix brand aesthetics.\n"
            "Prompt must incorporate: dark slate background (#1E1E1E), geometric forms, subtle green/blue/cyan accents, modern software concept.\n"
            "Format your final answer as JSON:\n"
            "{\n"
            '  "image_prompt": "Professional 1080x1080 social media visual for Zyntrix Studio..."\n'
            "}"
        ),
        expected_output="A JSON object containing image_prompt.",
        agent=designer
    )

    manager_task = Task(
        description=(
            "Review the writer's post and designer's image prompt.\n"
            "Verify natural Bengali, Zyntrix dark tech visual style, non-spam policy, factual claims, and concise structure.\n"
            "If approved, output structured JSON:\n"
            "{\n"
            '  "approved": true,\n'
            '  "hook": "...",\n'
            '  "caption": "...",\n'
            '  "hashtags": ["#ZyntrixStudio", "..."],\n'
            '  "image_prompt": "..."\n'
            "}\n"
            "If rejected, output:\n"
            "{\n"
            '  "approved": false,\n'
            '  "reason": "Specific explanation of rejection"\n'
            "}"
        ),
        expected_output="A JSON object indicating approval status and final validated post fields.",
        agent=manager
    )

    # Form Crew and Execute
    crew = Crew(
        agents=[writer, designer, manager],
        tasks=[writer_task, designer_task, manager_task],
        process=Process.sequential,
        verbose=False
    )

    logger.info("Executing CrewAI agents workflow...")
    result = crew.kickoff()
    raw_result_str = str(result)
    
    logger.info(f"CrewAI raw output (first 1500 chars): {raw_result_str[:1500]}")
    parsed_output = parse_json_from_text(raw_result_str)
    logger.info(f"Parsed content data: {json.dumps(parsed_output, ensure_ascii=False)[:1000]}")
    return parsed_output


# Pollinations AI Image Generation
def generate_image_pollinations(image_prompt: str, output_file="temp_post_image.jpg") -> str:
    """Generates 1080x1080 image using Pollinations API and saves to disk."""
    logger.info("Generating 1080x1080 visual using Pollinations API...")
    
    # Ensure brand direction keywords are present in prompt
    brand_suffix = (
        ", dark slate charcoal background #1E1E1E, modern geometric technology style, "
        "subtle cyan green accents, ultra clean, minimal clutter, square 1080x1080"
    )
    final_prompt = image_prompt + brand_suffix
    encoded_prompt = urllib.parse.quote(final_prompt)
    seed = random.randint(10000, 99999)
    
    # Pollinations image generation endpoint
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1080&nologo=true&seed={seed}"
    
    headers = {}
    if POLLINATIONS_API_KEY:
        headers["Authorization"] = f"Bearer {POLLINATIONS_API_KEY}"

    try:
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code == 200 and len(response.content) > 5000:
            with open(output_file, "wb") as f:
                f.write(response.content)
            logger.info(f"Image successfully generated and saved to '{output_file}' ({len(response.content)} bytes)")
            return output_file
        else:
            logger.error(f"Pollinations API failed with status {response.status_code}. Response length: {len(response.content)}")
            return ""
    except Exception as e:
        logger.error(f"Exception during image generation: {e}")
        return ""


# Facebook Meta Graph API Publishing
def publish_to_facebook(hook: str, caption: str, hashtags: list, image_path: str) -> bool:
    """Publishes photo with text caption to Facebook Page via Meta Graph API."""
    logger.info("Publishing post to Facebook Page via Meta Graph API...")
    
    hashtags_formatted = " ".join(hashtags) if hashtags else "#ZyntrixStudio"
    full_text = f"{hook}\n\n{caption}\n\n{hashtags_formatted}"
    
    api_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{FACEBOOK_PAGE_ID}/photos"
    
    try:
        with open(image_path, "rb") as image_file:
            payload = {
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
                "caption": full_text,
                "published": "true"
            }
            files = {
                "source": ("image.jpg", image_file, "image/jpeg")
            }
            
            response = requests.post(api_url, data=payload, files=files, timeout=60)
            res_json = response.json()
            
            if response.status_code == 200 and ("id" in res_json or "post_id" in res_json):
                post_id = res_json.get("id") or res_json.get("post_id")
                logger.info(f"Successfully published post to Facebook Page! Facebook Post ID: {post_id}")
                return True
            else:
                error_msg = res_json.get("error", {}).get("message", response.text)
                logger.error(f"Facebook Graph API Error (HTTP {response.status_code}): {error_msg}")
                return False
    except Exception as e:
        logger.error(f"Exception while posting to Facebook API: {e}")
        return False


# Main Orchestration Loop
def main():
    logger.info("Starting Zyntrix Studio AI Autoposter Engine")
    validate_environment()

    # 1. Fetch next topic from queue
    current_topic, queue = get_next_topic("topics.txt")
    logger.info(f"Selected Queue Topic: '{current_topic}'")

    # 2. Execute CrewAI agent pipeline.
    #    Qwen 3.6 can truncate mid-<think> under the 2048 token cap, producing
    #    unparsable output ({}). Retry once before giving up.
    content_data = {}
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        content_data = generate_and_validate_content(current_topic)
        if content_data:
            break
        logger.warning(f"Content generation returned unparsable output (attempt {attempt}/{max_attempts}). Retrying...")

    if not content_data or not content_data.get("approved"):
        reason = content_data.get("reason", "Manager agent rejected content or failed to parse agent response.")
        logger.error(f"Content generation rejected by Quality Manager: {reason}")
        logger.error("Aborting posting process. Topic remains in queue.")
        sys.exit(1)

    logger.info("Content approved by Zyntrix Quality Manager!")
    hook = content_data.get("hook", "")
    caption = content_data.get("caption", "")
    hashtags = content_data.get("hashtags", ["#ZyntrixStudio"])
    image_prompt = content_data.get("image_prompt", "")

    # 3. Generate image via Pollinations
    image_file = generate_image_pollinations(image_prompt, "temp_post_image.jpg")
    if not image_file or not os.path.exists(image_file):
        logger.error("Image generation failed. Aborting Facebook post. Topic remains in queue.")
        sys.exit(1)

    # 4. Publish to Facebook Page
    posted_successfully = publish_to_facebook(hook, caption, hashtags, image_file)
    
    # Clean up temporary image
    if os.path.exists(image_file):
        try:
            os.remove(image_file)
        except Exception:
            pass

    if posted_successfully:
        # 5. Remove processed topic from queue only on successful post
        remove_topic_from_queue(current_topic, "topics.txt")
        logger.info("Autoposter pipeline execution completed successfully.")
        sys.exit(0)
    else:
        logger.error("Facebook posting failed. Topic remains in queue for future retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
