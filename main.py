
import os
import re
import json
import time
import html
import hashlib
import calendar
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import feedparser
from deep_translator import GoogleTranslator


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

STATE_PATH = os.getenv("STATE_PATH", "data/state.json")

HEADERS = {
    "User-Agent": "AI-News-Telegram-Agent/2.0"
}

REQUEST_TIMEOUT = 20
NEWS_MAX_AGE_HOURS = 72
MAX_SEEN_ITEMS = 2000

FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    (
        "Anthropic",
        "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml"
    ),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Google Research", "https://research.google/blog/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    (
        "TechCrunch AI",
        "https://techcrunch.com/category/artificial-intelligence/feed/"
    ),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    (
        "The Verge AI",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"
    ),
    ("NVIDIA AI", "https://blogs.nvidia.com/feed/"),
    (
        "Microsoft Research",
        "https://www.microsoft.com/en-us/research/feed/"
    ),
    ("Microsoft AI", "https://blogs.microsoft.com/ai/feed/"),
    (
        "MIT Technology Review",
        "https://www.technologyreview.com/topic/artificial-intelligence/feed/"
    ),
    (
        "Ars Technica",
        "https://feeds.arstechnica.com/arstechnica/technology-lab"
    ),
    (
        "AWS Machine Learning",
        "https://aws.amazon.com/blogs/machine-learning/feed/"
    ),
    ("The Decoder", "https://the-decoder.com/feed/"),
    ("arXiv AI", "https://rss.arxiv.org/rss/cs.AI"),
]

TRUSTED_DOMAINS = {
    "openai.com",
    "anthropic.com",
    "deepmind.google",
    "google.com",
    "research.google",
    "blog.google",
    "huggingface.co",
    "techcrunch.com",
    "venturebeat.com",
    "theverge.com",
    "nvidia.com",
    "blogs.nvidia.com",
    "microsoft.com",
    "technologyreview.com",
    "arstechnica.com",
    "aws.amazon.com",
    "the-decoder.com",
    "arxiv.org",
}

GOOD_TERMS = [
    "artificial intelligence",
    "generative ai",
    "large language model",
    "language model",
    "machine learning",
    "deep learning",
    "neural network",
    "AI model",
    "AI agent",
    "AI agents",
    "robotics",
    "OpenAI",
    "ChatGPT",
    "Claude",
    "Gemini",
    "DeepMind",
    "Copilot",
    "Hugging Face",
    "computer vision",
    "text-to-image",
    "text-to-video",
    "reasoning model",
    "AI research",
    "AI system",
    "AI tool",
    "AI technology",
    "artificial intelligence research",
    "LLM",
    "GPT",
    "multimodal",
    "diffusion model",
]

BAD_TERMS = [
    "sponsored",
    "paid partnership",
    "advertisement",
    "coupon code",
    "discount code",
    "giveaway",
    "sign up for our newsletter",
]


# ============================================================
# STATE
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def load_state():
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)

    default_state = {
        "paused": False,
        "seen": {},
        "telegram_offset": 0,
        "last_scan": None,
        "last_success": None,
        "last_error_notice": None,
        "published_count": 0,
    }

    if not os.path.exists(STATE_PATH):
        return default_state

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as file:
            loaded = json.load(file)

        if not isinstance(loaded, dict):
            return default_state

        for key, value in default_state.items():
            loaded.setdefault(key, value)

        if not isinstance(loaded.get("seen"), dict):
            loaded["seen"] = {}

        return loaded

    except (OSError, json.JSONDecodeError) as error:
        print(f"Не удалось прочитать состояние: {error}")
        return default_state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)

    temporary_path = STATE_PATH + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temporary_path, STATE_PATH)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method, payload=None, timeout=REQUEST_TIMEOUT):
    if not BOT_TOKEN:
        raise RuntimeError("Не задан секрет BOT_TOKEN")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    response = requests.post(
        url,
        json=payload or {},
        timeout=timeout
    )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Telegram вернул HTTP {response.status_code}"
        )

    if not response.ok or not data.get("ok"):
        description = data.get("description", response.text[:500])
        raise RuntimeError(
            f"Telegram API, метод {method}: "
            f"HTTP {response.status_code}: {description}"
        )

    return data.get("result")


def send_message(chat_id, text):
    if not text or not text.strip():
        raise ValueError("Нельзя отправить пустое сообщение")

    # Отключаем предпросмотр ссылки, чтобы Telegram
    # не показывал изображение или карточку сайта.
    return telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    )


def notify_admin(message):
    if not ADMIN_CHAT_ID:
        print(f"Уведомление администратору не отправлено: {message}")
        return

    try:
        send_message(
            ADMIN_CHAT_ID,
            "🤖 <b>AI News Agent</b>\n" + html.escape(message)
        )
    except Exception as error:
        print(f"Не удалось уведомить администратора: {error}")


def handle_commands(state):
    """
    Обрабатывает команды администратора:
    /status
    /pause
    /resume
    """

    if not ADMIN_CHAT_ID:
        return

    try:
        offset = int(state.get("telegram_offset", 0) or 0)

        result = telegram_api(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
            timeout=10,
        )

    except Exception as error:
        print(f"Ошибка проверки команд Telegram: {error}")
        return

    for update in result or []:
        update_id = update.get("update_id", 0)
        state["telegram_offset"] = update_id + 1

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()

        # Принимаем команды только от указанного администратора.
        if str(chat.get("id", "")) != str(ADMIN_CHAT_ID):
            continue

        command = text.split()[0].split("@")[0].lower() if text else ""

        try:
            if command == "/pause":
                state["paused"] = True
                save_state(state)
                send_message(
                    ADMIN_CHAT_ID,
                    "⏸ Агент приостановлен. Новые публикации остановлены."
                )

            elif command == "/resume":
                state["paused"] = False
                save_state(state)
                send_message(
                    ADMIN_CHAT_ID,
                    "▶️ Агент возобновлён."
                )

            elif command == "/status":
                status = "приостановлен" if state["paused"] else "активен"

                send_message(
                    ADMIN_CHAT_ID,
                    "<b>Статус AI News Agent</b>\n"
                    f"Состояние: {status}\n"
                    f"Опубликовано: {state.get('published_count', 0)}\n"
                    f"Последнее сканирование: "
                    f"{html.escape(str(state.get('last_scan') or 'ещё не было'))}\n"
                    f"Последняя публикация: "
                    f"{html.escape(str(state.get('last_success') or 'ещё не было'))}"
                )

        except Exception as error:
            print(f"Ошибка обработки команды {command}: {error}")

    save_state(state)


# ============================================================
# TEXT AND SOURCE VALIDATION
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def domain_ok(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        return any(
            host == domain or host.endswith("." + domain)
            for domain in TRUSTED_DOMAINS
        )

    except Exception:
        return False


def entry_date(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
        or entry.get("created_parsed")
    )

    if not parsed:
        return None

    try:
        timestamp = calendar.timegm(parsed)
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def stable_id(entry, url, title):
    original_id = entry.get("id") or entry.get("guid") or url

    value = f"{original_id}|{title}".encode("utf-8")

    return hashlib.sha256(value).hexdigest()[:24]


def is_russian(text):
    """
    Не позволяет опубликовать заголовок,
    который остался полностью на английском.
    """
    cyrillic_count = len(re.findall(r"[А-Яа-яЁё]", text))
    return cyrillic_count >= 3


# ============================================================
# TRANSLATION
# ============================================================

def translate_title_to_russian(title):
    """
    Возвращает только русский заголовок.
    Если перевод не удался или остался английским,
    возвращает None: исходный английский текст не публикуется.
    """

    title = clean_text(title)

    if not title:
        return None

    # Если заголовок уже на русском, перевод не нужен.
    if is_russian(title):
        return title

    try:
        translated = GoogleTranslator(
            source="auto",
            target="ru"
        ).translate(title)

        translated = clean_text(translated)

        if not translated or not is_russian(translated):
            print(f"Заголовок не удалось перевести на русский: {title}")
            return None

        return translated

    except Exception as error:
        print(f"Ошибка перевода заголовка: {error}")
        return None


# ============================================================
# NEWS FILTERING
# ============================================================

def score_item(title, description):
    text = f"{title} {description}".lower()

    if any(term.lower() in text for term in BAD_TERMS):
        return 0

    score = 0

    for term in GOOD_TERMS:
        if term.lower() in text:
            score += 1

    return score


def fetch_candidates(state):
    candidates = []
    errors = []

    cutoff = now_utc() - timedelta(hours=NEWS_MAX_AGE_HOURS)
    seen = state.get("seen", {})

    for source_name, feed_url in FEEDS:
        try:
            response = requests.get(
                feed_url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            parsed = feedparser.parse(response.content)

            if parsed.bozo and not parsed.entries:
                raise RuntimeError("Не удалось разобрать RSS")

            for entry in parsed.entries:
                title = clean_text(entry.get("title", ""))
                url = (entry.get("link") or "").strip()

                if not title or not url:
                    continue

                # Только проверенные источники.
                if not domain_ok(url):
                    continue

                published = entry_date(entry)

                # Не публикуем старые новости и материалы
                # с неизвестной датой публикации.
                if published is None or published < cutoff:
                    continue

                description = clean_text(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                item_id = stable_id(entry, url, title)

                if item_id in seen:
                    continue

                score = score_item(title, description)

                if score <= 0:
                    continue

                candidates.append({
                    "id": item_id,
                    "source": source_name,
                    "title": title,
                    "url": url,
                    "published": published,
                    "score": score,
                })

        except Exception as error:
            message = f"{source_name}: {error}"
            print(f"Ошибка RSS: {message}")
            errors.append(message)

    # Сначала более свежие новости, затем релевантность.
    candidates.sort(
        key=lambda item: (
            item["published"],
            item["score"],
        ),
        reverse=True,
    )

    print(
        f"Подходящих новостей: {len(candidates)}; "
        f"ошибок RSS: {len(errors)}"
    )

    return candidates, errors


# ============================================================
# PUBLISHING
# ============================================================

def make_post(russian_title, source_url):
    """
    Формат публикации:
    русский заголовок
    ссылка на первоисточник

    Никаких картинок, описаний или предпросмотра ссылки.
    """

    safe_title = html.escape(russian_title)
    safe_url = html.escape(source_url, quote=True)

    return (
        f"<b>{safe_title}</b>\n\n"
        f'<a href="{safe_url}">Источник</a>'
    )


def prune_seen(state):
    seen = state.get("seen", {})

    if len(seen) <= MAX_SEEN_ITEMS:
        return

    ordered = sorted(
        seen.items(),
        key=lambda item: item[1] or "",
        reverse=True,
    )

    state["seen"] = dict(ordered[:MAX_SEEN_ITEMS])


def notify_rss_errors(state, errors):
    if len(errors) < 4:
        return

    last_notice = state.get("last_error_notice")

    if last_notice:
        try:
            last_time = datetime.fromisoformat(last_notice)

            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)

            # Не отправляем уведомления об RSS-ошибках
            # чаще одного раза в шесть часов.
            if now_utc() - last_time < timedelta(hours=6):
                return

        except (ValueError, TypeError):
            pass

    summary = "\n".join(errors[:8])

    notify_admin(
        "Проблемы с RSS-источниками:\n" + summary
    )

    state["last_error_notice"] = now_iso()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("В GitHub Secrets отсутствует BOT_TOKEN")

    if not CHANNEL_ID:
        raise RuntimeError("В GitHub Secrets отсутствует CHANNEL_ID")

    state = load_state()

    # Сначала проверяем команды администратора.
    handle_commands(state)

    if state.get("paused"):
        print("Агент приостановлен. Публикация пропущена.")
        state["last_scan"] = now_iso()
        save_state(state)
        return

    state["last_scan"] = now_iso()

    candidates, errors = fetch_candidates(state)
    notify_rss_errors(state, errors)

    # Публикуем не более одной новости за запуск.
    # Если перевод не удался, пробуем следующую новость.
    for item in candidates:
        russian_title = translate_title_to_russian(item["title"])

        if not russian_title:
            print(
                "Новость пропущена: нет надёжного русского заголовка. "
                f"Источник: {item['source']}"
            )
            continue

        post_text = make_post(
            russian_title,
            item["url"],
        )

        try:
            send_message(CHANNEL_ID, post_text)

            # Сохраняем ID только после успешной отправки.
            state.setdefault("seen", {})[item["id"]] = now_iso()
            state["last_success"] = now_iso()
            state["published_count"] = (
                int(state.get("published_count", 0)) + 1
            )

            prune_seen(state)
            save_state(state)

            print(
                f"Опубликовано: {russian_title} "
                f"(источник: {item['source']})"
            )
            return

        except Exception as error:
            print(f"Ошибка публикации в Telegram: {error}")
            raise

    print(
        "Новых новостей с корректным русским заголовком "
        "для публикации не найдено."
    )

    save_state(state)


if __name__ == "__main__":
    main()

