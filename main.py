
import os
import json
import re
import time
import hashlib
import html
import calendar
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
import feedparser


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
STATE_PATH = os.getenv("STATE_PATH", "data/state.json")

HEADERS = {
    "User-Agent": "AINewsTelegramBot/1.0 RSS reader"
}

# RSS-источники новостей об ИИ.
# Anthropic использует стороннюю RSS-ленту.
FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Anthropic", "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Google Research", "https://research.google/blog/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("NVIDIA AI", "https://blogs.nvidia.com/feed/"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/"),
    ("Microsoft AI", "https://blogs.microsoft.com/ai/feed/"),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("AWS Machine Learning", "https://aws.amazon.com/blogs/machine-learning/feed/"),
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
    "blogs.microsoft.com",
    "technologyreview.com",
    "arstechnica.com",
    "aws.amazon.com",
    "the-decoder.com",
    "arxiv.org",
    "export.arxiv.org",
}

GOOD_TERMS = [
    "launch", "launched", "released", "release", "introducing",
    "announces", "announced", "available", "update", "upgrade",
    "new model", "open source", "open-source", "free", "image",
    "video", "reasoning", "multimodal", "agent", "benchmark",
    "artificial intelligence", "machine learning", "generative ai",
    "генерац", "модель", "обновлен", "выпуст", "запуск",
    "бесплат", "открытый код", "видео", "изображен", "доступ",
    "нейросет", "искусственн", "интеллект", "обучен"
]

BAD_TERMS = [
    "sponsored", "partner content", "advertisement", "paid post",
    "promoted", "sponsored content", "промо-материал", "реклама",
    "партнёрский материал", "партнерский материал"
]

RUMOR_TERMS = [
    "rumor", "rumour", "leak", "leaked", "reportedly",
    "allegedly", "слух", "утечк", "предположительно",
    "неподтвержд"
]

PRIORITY_SOURCES = {
    "OpenAI", "Anthropic", "Google DeepMind", "Google AI",
    "Google Research", "Hugging Face", "NVIDIA AI",
    "Microsoft Research", "Microsoft AI"
}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).isoformat()


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    defaults = {
        "paused": False,
        "seen": {},
        "telegram_offset": 0,
        "last_scan": "",
        "last_success": "",
        "last_error_notice": "",
        "published_count": 0,
    }

    for key, value in defaults.items():
        state.setdefault(key, value)

    if not isinstance(state["seen"], dict):
        state["seen"] = {}

    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    tmp = STATE_PATH + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    os.replace(tmp, STATE_PATH)


def tg(method, payload=None, timeout=25):
    """Вызов Telegram API с диагностикой ошибок."""

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в GitHub Secrets")

    payload = dict(payload or {})

    if method == "sendMessage":
        message_text = payload.get("text")
        if not isinstance(message_text, str) or not message_text.strip():
            raise RuntimeError(
                "Отправка отменена: Telegram получил пустой текст. "
                f"Получатель: {payload.get('chat_id', 'не указан')}"
            )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Ошибка соединения с Telegram: {type(exc).__name__}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Telegram вернул не JSON: HTTP {response.status_code}"
        ) from exc

    if not response.ok or not data.get("ok"):
        code = data.get("error_code", response.status_code)
        description = data.get("description", "Причина не указана")
        raise RuntimeError(
            f"Telegram API, метод {method}: HTTP "
            f"{response.status_code}, код {code}: {description}"
        )

    return data.get("result")


def notify_admin(message):
    if not ADMIN_CHAT_ID:
        return

    message = str(message or "").strip()
    if not message:
        return

    try:
        tg("sendMessage", {
            "chat_id": ADMIN_CHAT_ID,
            "text": message[:3900],
            "disable_web_page_preview": True
        })
    except Exception as exc:
        print(
            "Не удалось уведомить администратора:",
            type(exc).__name__,
            str(exc)[:250]
        )


def clean_html(value):
    value = str(value or "")
    value = re.sub(
        r"<(script|style).*?</\1>",
        " ",
        value,
        flags=re.I | re.S
    )
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def entry_date(entry):
    for key in (
        "published_parsed",
        "updated_parsed",
        "created_parsed"
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(
                    calendar.timegm(parsed),
                    tz=timezone.utc
                )
            except (ValueError, OverflowError, OSError):
                pass

    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass

    return None


def domain_ok(url):
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == domain or host.endswith("." + domain)
        for domain in TRUSTED_DOMAINS
    )


def stable_id(url, title):
    key = (
        (url or "").split("#")[0].split("?")[0].rstrip("/")
        or title.lower().strip()
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def translate_ru(text):
    text = clean_html(text)
    if not text:
        return ""

    # Не вызываем переводчик, если текст уже содержит кириллицу.
    if re.search(r"[А-Яа-яЁё]", text):
        return text

    try:
        from deep_translator import GoogleTranslator

        translated = GoogleTranslator(
            source="auto",
            target="ru"
        ).translate(text[:4500])

        return clean_html(translated) or text

    except Exception as exc:
        print(
            "Перевод недоступен; оставляю оригинал:",
            type(exc).__name__,
            str(exc)[:160]
        )
        return text


def is_rumor(text):
    low = (text or "").lower()
    return any(term in low for term in RUMOR_TERMS)


def score_item(title, summary, source, published):
    blob = f"{title} {summary}".lower()

    if any(term in blob for term in BAD_TERMS):
        return -100

    score = 1
    score += sum(1 for term in GOOD_TERMS if term in blob)

    if source in PRIORITY_SOURCES:
        score += 2

    if is_rumor(blob):
        score -= 1

    if published and published > now_utc() - timedelta(hours=24):
        score += 2

    if len(title.strip()) < 12:
        score -= 2

    return score


def fetch_candidates():
    candidates = []
    errors = []
    cutoff = now_utc() - timedelta(hours=72)

    session = requests.Session()
    session.headers.update(HEADERS)

    for source, feed_url in FEEDS:
        try:
            response = session.get(feed_url, timeout=20)
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if not feed.entries:
                errors.append(f"{source}: RSS не вернул записей")
                continue

            if getattr(feed, "bozo", False):
                print(f"Предупреждение RSS {source}: некорректный формат ленты")

            for entry in feed.entries[:30]:
                title = clean_html(entry.get("title", ""))
                url = str(entry.get("link", "") or "").strip()
                summary = clean_html(
                    entry.get("summary")
                    or entry.get("description")
                    or ""
                )
                published = entry_date(entry)

                if not title or not url or not domain_ok(url):
                    continue

                # Не публикуем записи без даты или слишком старые записи.
                if published is None:
                    continue

                if (
                    published > now_utc() + timedelta(hours=12)
                    or published < cutoff
                ):
                    continue

                score = score_item(title, summary, source, published)

                if score >= 2:
                    candidates.append({
                        "id": stable_id(url, title),
                        "source": source,
                        "title": title,
                        "url": url,
                        "summary": summary[:1200],
                        "published": published,
                        "score": score,
                        "rumor": is_rumor(title + " " + summary)
                    })

        except Exception as exc:
            errors.append(
                f"{source}: {type(exc).__name__}: {str(exc)[:160]}"
            )

    session.close()

    candidates.sort(
        key=lambda item: (item["score"], item["published"]),
        reverse=True
    )

    return candidates, errors


def make_post(item):
    title = clean_html(item.get("title", ""))
    summary = clean_html(item.get("summary", ""))

    if not title:
        raise RuntimeError("У новости пустой заголовок.")

    title_ru = translate_ru(title).strip() or title

    if summary:
        summary_ru = translate_ru(summary).strip() or summary
    else:
        summary_ru = ""

    if not summary_ru:
        summary_ru = "Ключевая информация — в материале источника."

    summary_ru = summary_ru[:650].strip()

    source = clean_html(item.get("source", "Источник"))
    url = str(item.get("url", "") or "").strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("У новости отсутствует корректная ссылка.")

    rumor_label = ""
    if item.get("rumor"):
        rumor_label = "⚠️ <b>Слух / не подтверждено</b>\n\n"

    useful = (
        "Полезно тем, кто следит за новыми возможностями "
        "ИИ и нейросетей."
    )

    post = (
        f"{rumor_label}"
        f"🤖 <b>{html.escape(title_ru[:220])}</b>\n\n"
        f"{html.escape(summary_ru)}\n\n"
        f"💡 <b>Кому полезно:</b> {useful}\n\n"
        f"#нейросети #ИИ #AI\n"
        f"Источник: <a href=\"{html.escape(url, quote=True)}\">"
        f"{html.escape(source or 'Источник')}</a>"
    ).strip()

    if not post:
        raise RuntimeError("Не удалось сформировать текст новости.")

    if len(post) > 4000:
        raise RuntimeError(
            f"Сообщение слишком длинное: {len(post)} символов."
        )

    return post


def handle_commands(state):
    changed = False

    try:
        updates = tg("getUpdates", {
            "offset": int(state.get("telegram_offset", 0)),
            "timeout": 1,
            "allowed_updates": ["message"]
        }, timeout=8)

        for update in updates or []:
            state["telegram_offset"] = max(
                int(state.get("telegram_offset", 0)),
                int(update["update_id"]) + 1
            )

            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            command_text = (msg.get("text") or "").strip()
            command = command_text.split()[0].split("@")[0].lower() if command_text else ""
            chat_id = str(chat.get("id", ""))

            if not ADMIN_CHAT_ID or chat_id != ADMIN_CHAT_ID:
                continue

            if command == "/start":
                tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": (
                        "Команды агента:\n"
                        "/status — состояние\n"
                        "/pause — приостановить публикации\n"
                        "/resume — возобновить публикации"
                    )
                })

            elif command == "/status":
                tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": (
                        f"Статус: {'пауза' if state['paused'] else 'работает'}\n"
                        f"Последняя проверка: {state.get('last_scan') or 'ещё не запускался'}\n"
                        f"Последняя публикация: {state.get('last_success') or 'пока нет'}\n"
                        f"Опубликовано агентом: {state.get('published_count', 0)}"
                    )
                })

            elif command == "/pause":
                state["paused"] = True
                tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Публикации приостановлены."
                })

            elif command == "/resume":
                state["paused"] = False
                tg("sendMessage", {
                    "chat_id": chat_id,
                    "text": "Публикации возобновлены."
                })

            changed = True

    except Exception as exc:
        print(
            "Ошибка проверки команд:",
            type(exc).__name__,
            str(exc)[:250]
        )

    return changed


def prune_seen(state):
    cutoff = now_utc() - timedelta(days=7)
    cleaned = {}

    for key, value in state.get("seen", {}).items():
        try:
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                cleaned[key] = value
        except Exception:
            continue

    state["seen"] = cleaned


def main():
    if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_CHAT_ID:
        raise RuntimeError(
            "Проверь GitHub Secrets: нужны BOT_TOKEN, CHANNEL_ID и ADMIN_CHAT_ID."
        )

    state = load_state()
    handle_commands(state)
    prune_seen(state)

    last_scan = None

    if state.get("last_scan"):
        try:
            last_scan = datetime.fromisoformat(
                state["last_scan"].replace("Z", "+00:00")
            )
        except Exception:
            last_scan = None

    due = (
        last_scan is None
        or now_utc() - last_scan >= timedelta(minutes=28)
    )

    if state.get("paused"):
        save_state(state)
        print("Публикации приостановлены.")
        return

    if not due:
        save_state(state)
        print("Проверка ещё не требуется.")
        return

    state["last_scan"] = iso()

    candidates, errors = fetch_candidates()

    print(
        f"Найдено кандидатов: {len(candidates)}; "
        f"ошибок источников: {len(errors)}"
    )

    candidate = next(
        (item for item in candidates if item["id"] not in state["seen"]),
        None
    )

    if candidate:
        post = make_post(candidate)

        print(
            f"Подготовлена публикация: {candidate['source']}; "
            f"длина: {len(post)} символов."
        )

        # CHANNEL_ID для публичного канала должен быть @username.
        # Для приватного канала используй числовой ID вида -100...
        tg("sendMessage", {
            "chat_id": CHANNEL_ID,
            "text": post,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        })

        # Помечаем новость только после успешной отправки.
        state["seen"][candidate["id"]] = iso()
        state["last_success"] = iso()
        state["published_count"] = int(
            state.get("published_count", 0)
        ) + 1

        print("Опубликовано:", candidate["title"])

    else:
        print("Новых подходящих новостей нет.")

    if errors and len(errors) >= 4:
        last_notice = state.get("last_error_notice")
        allowed = True

        if last_notice:
            try:
                previous = datetime.fromisoformat(
                    last_notice.replace("Z", "+00:00")
                )
                allowed = (
                    now_utc() - previous >= timedelta(hours=6)
                )
            except Exception:
                pass

        if allowed:
            notify_admin(
                "⚠️ Ошибки при проверке RSS-источников:\n"
                + "\n".join(errors[:8])
            )
            state["last_error_notice"] = iso()

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = (
            f"Критическая ошибка: {type(exc).__name__}: "
            f"{str(exc)[:900]}"
        )
        print(message)
        notify_admin("🚨 Агент новостей столкнулся с ошибкой:\n" + message)
        raise
