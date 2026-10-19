import os
import re
import json
import time
import html
import hashlib
import calendar
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, quote_plus

import requests
import feedparser
from deep_translator import GoogleTranslator

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")

REQUEST_TIMEOUT = 20
NEWS_MAX_AGE_HOURS = 72
MAX_SEEN_ITEMS = 3000
MAX_POSTS_PER_RUN = 1
HEADERS = {"User-Agent": "AI-News-Telegram-Agent/3.0 (+RSS reader)"}

# Надёжные прямые ленты. Дополнительное покрытие обеспечивают Google News RSS-запросы ниже.
DIRECT_FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Anthropic", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Google Research", "https://research.google/blog/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("NVIDIA", "https://blogs.nvidia.com/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("AWS Machine Learning", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("The Decoder", "https://the-decoder.com/feed/"),
    ("arXiv AI", "https://rss.arxiv.org/rss/cs.AI"),
    ("arXiv Machine Learning", "https://rss.arxiv.org/rss/cs.LG"),
    ("arXiv Computation and Language", "https://rss.arxiv.org/rss/cs.CL"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("MIT News AI", "https://news.mit.edu/rss/topic/artificial-intelligence2"),
    ("BAIR", "https://bair.berkeley.edu/blog/feed.xml"),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("MarkTechPost", "https://www.marktechpost.com/feed/"),
]

SEARCH_QUERIES = [
    '"artificial intelligence" news',
    '"generative AI" new model',
    '"AI model" launch OR release',
    '"neural network" research',
    '"machine learning" breakthrough',
    'ChatGPT OpenAI news',
    'Anthropic Claude AI news',
    'Google Gemini AI news',
    'Meta AI model news',
    'Microsoft Copilot AI news',
    'NVIDIA AI news',
    'Hugging Face models',
    '"AI agent" technology',
    'text to video AI model',
    'AI image generation model',
    'open source large language model',
    'multimodal AI research',
    'robotics artificial intelligence',
    'AI coding assistant release',
    'AI startup funding technology',
    'AI safety research',
    'AI scientific discovery',
    'computer vision deep learning',
    'LLM benchmark research',
    'Mistral AI model news',
    'Chinese AI model release',
    'AI tools product launch',
    'AI regulation technology',
    'AI hardware accelerator',
    'arXiv large language model',
]

def build_feeds():
    feeds = list(DIRECT_FEEDS)
    for query in SEARCH_QUERIES:
        encoded = quote_plus(f"{query} when:1d")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        feeds.append((f"Google News: {query}", url))
    return feeds

FEEDS = build_feeds()

GOOD_TERMS = [
    "artificial intelligence", "generative ai", "large language model",
    "language model", "machine learning", "deep learning", "neural network",
    "ai model", "ai agent", "robotics", "openai", "chatgpt", "claude",
    "gemini", "deepmind", "copilot", "hugging face", "computer vision",
    "text-to-image", "text-to-video", "reasoning model", "ai research",
    "ai system", "ai tool", "ai technology", "llm", "gpt", "multimodal",
    "diffusion model", "mistral", "nvidia", "generative model",
]
BAD_TERMS = [
    "sponsored", "paid partnership", "advertisement", "coupon code",
    "discount code", "giveaway", "sign up for our newsletter",
]

def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

def load_state():
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    defaults = {
        "paused": False, "seen": {}, "telegram_offset": 0,
        "last_scan": None, "last_success": None, "last_error_notice": None,
        "published_count": 0,
    }
    if not os.path.exists(STATE_PATH):
        return defaults
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return defaults
        for key, value in defaults.items():
            state.setdefault(key, value)
        if not isinstance(state.get("seen"), dict):
            state["seen"] = {}
        return state
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось прочитать состояние: {exc}")
        return defaults

def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    temp = STATE_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp, STATE_PATH)

def telegram_api(method, payload=None, timeout=REQUEST_TIMEOUT):
    if not BOT_TOKEN:
        raise RuntimeError("Не задан GitHub Secret BOT_TOKEN")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"Telegram вернул HTTP {response.status_code}: {response.text[:300]}")
    if not response.ok or not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: HTTP {response.status_code}: {data.get('description', response.text[:300])}")
    return data.get("result")

def send_message(chat_id, text):
    if not text or not text.strip():
        raise ValueError("Нельзя отправить пустое сообщение")
    return telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

def notify_admin(message):
    if not ADMIN_CHAT_ID:
        print(f"Уведомление администратору не отправлено (ADMIN_CHAT_ID не задан): {message}")
        return
    try:
        send_message(ADMIN_CHAT_ID, "🤖 <b>AI News Agent</b>\n" + html.escape(message)[:3500])
    except Exception as exc:
        print(f"Не удалось уведомить администратора: {exc}")

def handle_commands(state):
    if not ADMIN_CHAT_ID:
        return
    try:
        updates = telegram_api("getUpdates", {
            "offset": int(state.get("telegram_offset", 0) or 0),
            "timeout": 0,
            "allowed_updates": ["message"],
        }, timeout=10)
    except Exception as exc:
        print(f"Ошибка проверки команд Telegram: {exc}")
        return
    for update in updates or []:
        state["telegram_offset"] = update.get("update_id", 0) + 1
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        command_text = (message.get("text") or "").strip()
        if str(chat.get("id", "")) != str(ADMIN_CHAT_ID):
            continue
        command = command_text.split()[0].split("@")[0].lower() if command_text else ""
        try:
            if command == "/pause":
                state["paused"] = True
                send_message(ADMIN_CHAT_ID, "⏸ Агент приостановлен.")
            elif command == "/resume":
                state["paused"] = False
                send_message(ADMIN_CHAT_ID, "▶️ Агент возобновлён.")
            elif command == "/status":
                status = "приостановлен" if state["paused"] else "активен"
                send_message(ADMIN_CHAT_ID,
                    "<b>Статус AI News Agent</b>\n"
                    f"Состояние: {status}\n"
                    f"Источников/поисковых лент: {len(FEEDS)}\n"
                    f"Опубликовано: {state.get('published_count', 0)}\n"
                    f"Последнее сканирование: {html.escape(str(state.get('last_scan') or 'ещё не было'))}\n"
                    f"Последняя публикация: {html.escape(str(state.get('last_success') or 'ещё не было'))}")
        except Exception as exc:
            print(f"Ошибка команды {command}: {exc}")
    save_state(state)

def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def valid_http_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except Exception:
        return False

def entry_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("created_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None

def stable_id(entry, url, title):
    # URL + normalized title prevents repeat posts when the same article appears in multiple feeds.
    normalized = re.sub(r"\W+", " ", title.lower()).strip()
    value = f"{url.split('?')[0]}|{normalized}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]

def score_item(title, description):
    text = f"{title} {description}".lower()
    if any(term in text for term in BAD_TERMS):
        return 0
    score = sum(1 for term in GOOD_TERMS if term in text)
    # Short titles frequently say "AI" without spelling out "artificial intelligence".
    if re.search(r"\b(ai|llm|gpt)\b", text):
        score += 1
    return score

def fetch_candidates(state):
    candidates, errors = [], []
    cutoff = now_utc() - timedelta(hours=NEWS_MAX_AGE_HOURS)
    seen = state.get("seen", {})
    print(f"Начинаю проверку {len(FEEDS)} RSS-лент (прямые источники и агрегированный поиск).")
    for source_name, feed_url in FEEDS:
        try:
            response = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError("RSS пустой или не удалось разобрать")
            if not parsed.entries:
                print(f"Пустая лента: {source_name}")
                continue
            added_from_feed = 0
            for entry in parsed.entries:
                title = clean_text(entry.get("title", ""))
                url = (entry.get("link") or "").strip()
                if not title or not url or not valid_http_url(url):
                    continue
                description = clean_text(entry.get("summary", "") or entry.get("description", ""))
                score = score_item(title, description)
                is_search_feed = source_name.startswith("Google News:")
                # Search feeds are already topical; direct feeds must pass a broad relevance check.
                if score <= 0 and not is_search_feed:
                    continue
                published = entry_date(entry)
                if published is not None and published < cutoff:
                    continue
                # Google News search is constrained to the last day; accept missing dates there.
                if published is None and not is_search_feed:
                    print(f"Пропуск материала без даты: {source_name}: {title[:100]}")
                    continue
                if published is None:
                    published = now_utc()
                item_id = stable_id(entry, url, title)
                if item_id in seen:
                    continue
                candidates.append({
                    "id": item_id, "source": source_name, "title": title,
                    "url": url, "published": published, "score": score,
                })
                added_from_feed += 1
            print(f"Источник {source_name}: новых кандидатов {added_from_feed}")
        except Exception as exc:
            message = f"{source_name}: {type(exc).__name__}: {exc}"
            print(f"ОШИБКА RSS — {message}")
            errors.append(message)
    candidates.sort(key=lambda item: (item["published"], item["score"]), reverse=True)
    print(f"ИТОГО: кандидатов {len(candidates)}; ошибок лент {len(errors)} из {len(FEEDS)}.")
    return candidates, errors

def translate_title_to_russian(title):
    title = clean_text(title)
    if not title:
        return None
    if re.search(r"[А-Яа-яЁё]{3,}", title):
        return title
    for attempt in range(2):
        try:
            translated = GoogleTranslator(source="auto", target="ru").translate(title)
            translated = clean_text(translated)
            if translated and re.search(r"[А-Яа-яЁё]{3,}", translated):
                return translated
        except Exception as exc:
            print(f"Перевод: попытка {attempt + 1} не удалась: {exc}")
            time.sleep(1)
    # Translation failure must not silently block every otherwise valid news item.
    print(f"ПРЕДУПРЕЖДЕНИЕ: перевод недоступен, публикую исходный заголовок: {title}")
    return title

def make_post(russian_title, source_url, source_name):
    return (
        f"<b>{html.escape(russian_title)}</b>\n\n"
        f"📰 <i>{html.escape(source_name)}</i>\n"
        f'<a href="{html.escape(source_url, quote=True)}">Источник</a>'
    )

def prune_seen(state):
    seen = state.get("seen", {})
    if len(seen) > MAX_SEEN_ITEMS:
        ordered = sorted(seen.items(), key=lambda pair: pair[1] or "", reverse=True)
        state["seen"] = dict(ordered[:MAX_SEEN_ITEMS])

def notify_rss_errors(state, errors):
    if len(errors) < 5:
        return
    last_notice = state.get("last_error_notice")
    if last_notice:
        try:
            last_time = datetime.fromisoformat(last_notice)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            if now_utc() - last_time < timedelta(hours=6):
                return
        except (ValueError, TypeError):
            pass
    notify_admin("Много ошибок RSS (" + str(len(errors)) + "):\n" + "\n".join(errors[:6]))
    state["last_error_notice"] = now_iso()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("В GitHub Secrets отсутствует BOT_TOKEN")
    if not CHANNEL_ID:
        raise RuntimeError("В GitHub Secrets отсутствует CHANNEL_ID")

    state = load_state()
    handle_commands(state)
    if state.get("paused"):
        print("Агент приостановлен командой /pause.")
        state["last_scan"] = now_iso()
        save_state(state)
        return

    # Fail fast if channel ID is wrong or the bot cannot access the channel.
    try:
        chat = telegram_api("getChat", {"chat_id": CHANNEL_ID}, timeout=15)
        print(f"Telegram destination OK: {chat.get('title') or chat.get('username') or CHANNEL_ID}")
    except Exception as exc:
        raise RuntimeError(
            f"Бот не может обратиться к каналу CHANNEL_ID={CHANNEL_ID}. "
            f"Проверь, что бот добавлен в канал администратором и CHANNEL_ID верный. Детали: {exc}"
        ) from exc

    state["last_scan"] = now_iso()
    candidates, errors = fetch_candidates(state)
    notify_rss_errors(state, errors)

    for item in candidates:
        title_ru = translate_title_to_russian(item["title"])
        if not title_ru:
            continue
        post = make_post(title_ru, item["url"], item["source"])
        # Do not mark an item as seen until Telegram confirms successful publication.
        sent = send_message(CHANNEL_ID, post)
        state.setdefault("seen", {})[item["id"]] = now_iso()
        state["last_success"] = now_iso()
        state["published_count"] = int(state.get("published_count", 0)) + 1
        prune_seen(state)
        save_state(state)
        print(f"ОПУБЛИКОВАНО в Telegram: {title_ru} | message_id={sent.get('message_id')} | {item['source']}")
        return

    print("Новых подходящих публикаций сейчас нет. Следующий запуск проверит ленты снова.")
    save_state(state)

if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()

