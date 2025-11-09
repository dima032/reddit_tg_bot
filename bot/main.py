import os
import time
import json
import logging
import requests
import functools
import asyncio
import html
from typing import List, Dict, Set
from telegram import Bot
from telegram.constants import ParseMode

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration via environment
TOKEN = os.getenv('TELEGRAM_REDDIT_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_REDDIT_BOT_CHAT_ID')  # target chat id (string or int)
SUBREDDITS = os.getenv('SUBREDDIT_LIST', '')  # comma-separated, each item optionally "subreddit:threshold"
SCORE_THRESHOLD = int(os.getenv('SCORE_THRESHOLD', '1000'))
SEEN_FILE = os.getenv('SEEN_FILE', '/data/seen_posts.json')  # ensure this volume is writable in k3s
POSTS_LIMIT = int(os.getenv('POSTS_LIMIT', '50'))
USER_AGENT = os.getenv('REDDIT_USER_AGENT', 'telegram-reddit-bot/0.1')

if not TOKEN or not CHAT_ID or not SUBREDDITS:
    logger.error("Missing required env vars. Ensure TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and SUBREDDITS are set.")
    raise SystemExit(1)

def parse_subreddit_config(spec: str, default_threshold: int) -> Dict[str, int]:
    """
    Parse SUBREDDITS env var into a mapping {subreddit: threshold}.
    Format: "cats:500,aww:1000,EarthPorn"  (EarthPorn uses default_threshold)
    """
    mapping: Dict[str, int] = {}
    for part in spec.split(','):
        p = part.strip()
        if not p:
            continue
        if ':' in p:
            name, val = p.split(':', 1)
            name = name.strip()
            try:
                thr = int(val.strip())
            except ValueError:
                thr = default_threshold
        else:
            name = p
            thr = default_threshold
        if name:
            mapping[name] = thr
    return mapping

def load_seen(path: str) -> Set[str]:
    try:
        with open(path, 'r') as f:
            data = json.load(f)
            return set(data if isinstance(data, list) else [])
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning("Failed to load seen file %s: %s", path, e)
        return set()

def save_seen(path: str, seen: Set[str]):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(list(seen), f)
    except Exception as e:
        logger.error("Failed to save seen file %s: %s", path, e)


def retry_with_backoff(retries=3, backoff_in_seconds=1):
    """Decorator that retries a function (sync or async) with exponential backoff.

    Works for both sync and async functions. Uses time.sleep for waiting.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper_async(*args, **kwargs):
            for i in range(retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if i == retries - 1:  # Last attempt
                        raise
                    wait_time = (backoff_in_seconds * (2 ** i))
                    logger.warning(
                        "Attempt %d/%d failed: %s. Retrying in %ds...",
                        i + 1, retries, str(e), wait_time
                    )
                    time.sleep(wait_time)
            return None

        @functools.wraps(func)
        def wrapper_sync(*args, **kwargs):
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if i == retries - 1:
                        raise
                    wait_time = (backoff_in_seconds * (2 ** i))
                    logger.warning(
                        "Attempt %d/%d failed: %s. Retrying in %ds...",
                        i + 1, retries, str(e), wait_time
                    )
                    time.sleep(wait_time)
            return None

        return wrapper_async if asyncio.iscoroutinefunction(func) else wrapper_sync
    return decorator

@retry_with_backoff(retries=3, backoff_in_seconds=1)
def get_top_posts(subreddit: str, limit: int = 50) -> List[Dict]:
    # Use the newer .json API with raw_json=1 to get unescaped data and include ups/downs
    url = f"https://www.reddit.com/r/{subreddit}/top.json?limit={limit}&t=day&raw_json=1"
    headers = {
        "User-Agent": USER_AGENT,
        # Add a more detailed user agent to help with rate limiting
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        logger.warning("Reddit returned status %s for /r/%s", r.status_code, subreddit)
        return []
        
    data = r.json()
    posts = data.get("data", {}).get("children", [])
    result = []
    for p in posts:
        d = p.get("data", {})
        if not d:
            continue
        # determine if post has video, gallery, or single image content
        image_url = None
        is_video = False
        is_gallery = False
        url_dest = d.get("url_overridden_by_dest") or d.get("url")

        # Check if it's a gallery post
        if d.get("is_gallery", False) or \
            (url_dest and isinstance(url_dest, str) and \
            ('gallery' in url_dest or '/gallery/' in url_dest)):
            is_gallery = True
        # Check if it's a video post
        elif d.get("is_video", False) or \
            (url_dest and isinstance(url_dest, str) and \
            (url_dest.lower().endswith(('.mp4', '.mov', '.webm')) or \
            'v.redd.it' in url_dest or \
            'youtube.com' in url_dest or \
            'youtu.be' in url_dest)):
            is_video = True
        # Check for single image if not a video or gallery
        elif url_dest and isinstance(url_dest, str) and url_dest.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            image_url = url_dest
        else:
            # try preview images
            images = d.get("preview", {}).get("images", [])
            if images and isinstance(images, list):
                src = images[0].get("source", {}).get("url")
                if src:
                    image_url = src.replace('&amp;', '&')

        # Get more accurate vote counts
        ups = d.get("ups", 0)
        ratio = d.get("upvote_ratio", 1.0)
        # Calculate total votes and actual score
        total_votes = int(ups / ratio) if ratio > 0 else ups
        downs = total_votes - ups if total_votes > ups else 0
        score = ups - downs

        # Get post flair if available
        flair_text = None
        if d.get("link_flair_text"):
            flair_text = d["link_flair_text"]
        # Some subreddits use custom flair templates
        elif d.get("link_flair_richtext"):
            rich_flair = d["link_flair_richtext"]
            if isinstance(rich_flair, list):
                flair_parts = []
                for part in rich_flair:
                    if part.get("t"):  # text part
                        flair_parts.append(part["t"])
                    elif part.get("e"):  # emoji part
                        flair_parts.append(f':{part["e"]}:')
                if flair_parts:
                    flair_text = ''.join(flair_parts).strip()

        result.append({
            "id": d.get("id"),
            "title": d.get("title", "No Title"),
            "score": score,
            "ups": ups,
            "downs": downs,
            "ratio": ratio,
            "permalink": d.get("permalink", ""),
            "image_url": image_url,
            "is_video": is_video,
            "is_gallery": is_gallery,
            "flair": flair_text,
            "created_utc": d.get("created_utc", 0)
        })
    return result
def is_within_last_24h(created_utc: float) -> bool:
    now = time.time()
    return (now - created_utc) <= 24 * 3600

def make_post_url(permalink: str) -> str:
    return f"https://reddit.com{permalink}"

def build_caption(title: str, score: int, post_url: str, ups: int = None, ratio: float = None, flair: str = None) -> str:
    safe_title = html.escape(title)
    # Keep title as plain text and add hyperlink below and detailed stats
    caption = f'{safe_title}'
    
    # Add flair if available
    if flair:
        safe_flair = html.escape(flair)
        caption += f'\n🏷️ {safe_flair}'  # Tag emoji
        
    caption += f'\n<a href="{post_url}">🔗 Link to post</a>'
    
    # Add vote details if available
    if ups is not None and ratio is not None:
        upvote_percent = int(ratio * 100)
        caption += f'\n⬆️ {score} ({upvote_percent}% upvoted)'
    else:
        caption += f'\n⬆️ {score}'
    
    return caption

@retry_with_backoff(retries=3, backoff_in_seconds=1)
async def send_post(bot: Bot, chat_id: str, post: Dict):
    post_url = make_post_url(post.get("permalink", ""))
    score = post.get("score", 0)
    ups = post.get("ups")
    ratio = post.get("ratio")
    title = post.get("title", "")
    flair = post.get("flair")
    
    # Add appropriate icons for different types of posts
    if post.get("is_video", False):
        title = "🎥 " + title
    elif post.get("is_gallery", False):
        title = "📷 " + title  # Camera with multiple images icon

    caption = build_caption(title, score, post_url, ups, ratio, flair)
    
    if post.get("is_video", False) or post.get("is_gallery", False):
        # For video and gallery posts, just send the message with the link and icon
        await bot.send_message(chat_id=chat_id, text=caption, 
                            parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    elif post.get("image_url"):
        await bot.send_photo(chat_id=chat_id, photo=post["image_url"], 
                            caption=caption, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id=chat_id, text=caption, 
                            parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    logger.info("Sent post %s to chat %s", post.get("id"), chat_id)



async def main():
    bot = Bot(token=TOKEN)
    seen = load_seen(SEEN_FILE)
    logger.info("Loaded %d seen ids from %s", len(seen), SEEN_FILE)
    sub_config = parse_subreddit_config(SUBREDDITS, SCORE_THRESHOLD)
    new_seen = set(seen)

    for sub, threshold in sub_config.items():
        logger.info("Checking /r/%s with threshold %d", sub, threshold)
        posts = get_top_posts(sub, limit=POSTS_LIMIT)
        logger.info("Fetched %d posts from /r/%s", len(posts), sub)
        for p in posts:
            pid = p.get("id")
            if not pid:
                logger.info("Skipping post with no id: %s", p.get("title"))
                continue
            if pid in seen:
                logger.info("Skipping %s: already seen", pid)
                continue
            if not is_within_last_24h(p.get("created_utc", 0)):
                logger.info("Skipping %s: older than 24h (created=%s)", pid, p.get("created_utc"))
                continue
            score = p.get("score", 0)
            if score < threshold:
                logger.info("Skipping %s: score %d < threshold %d", pid, score, threshold)
                continue
            logger.info("Sending %s (score=%d) from /r/%s", pid, score, sub)
            await send_post(bot, CHAT_ID, p)  # Add await here
            new_seen.add(pid)

    if new_seen != seen:
        save_seen(SEEN_FILE, new_seen)
    logger.info("Finished run.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
