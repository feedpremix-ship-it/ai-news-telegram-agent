import os, json, re, time, hashlib, html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import requests
import feedparser

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
CHANNEL_ID = os.getenv('CHANNEL_ID', '').strip()
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '').strip()
STATE_PATH = os.getenv('STATE_PATH', 'data/state.json')

FEEDS = [
    ('OpenAI', 'https://openai.com/news/rss.xml'),
    ('Anthropic', 'https://www.anthropic.com/news/rss.xml'),
    ('Google DeepMind', 'https://deepmind.google/blog/rss.xml'),
    ('Hugging Face', 'https://huggingface.co/blog/feed.xml'),
    ('TechCrunch AI', 'https://techcrunch.com/category/artificial-intelligence/feed/'),
    ('VentureBeat AI', 'https://venturebeat.com/category/ai/feed/'),
    ('The Verge AI', 'https://www.theverge.com/ai-artificial-intelligence/rss/index.xml'),
    ('NVIDIA AI', 'https://blogs.nvidia.com/blog/category/ai/feed/'),
    ('Google Research', 'https://blog.research.google/feeds/posts/default?alt=rss'),
    ('Microsoft Research', 'https://www.microsoft.com/en-us/research/feed/'),
    ('MIT Technology Review', 'https://www.technologyreview.com/topic/artificial-intelligence/feed/'),
    ('Ars Technica', 'https://feeds.arstechnica.com/arstechnica/technology-lab'),
    ('Stability AI', 'https://stability.ai/news?format=rss'),
    ('Meta AI', 'https://ai.meta.com/blog/rss/'),
    ('Runway', 'https://runwayml.com/blog/rss.xml'),
]

TRUSTED_DOMAINS = {
    'openai.com','anthropic.com','deepmind.google','google.com','research.google','huggingface.co',
    'techcrunch.com','venturebeat.com','theverge.com','nvidia.com','blogs.nvidia.com',
    'microsoft.com','technologyreview.com','arstechnica.com','stability.ai','ai.meta.com','meta.com','runwayml.com'
}
GOOD_TERMS = [
    'launch','released','release','introducing','announces','announced','available','update','upgrade',
    'new model','open source','free','image','video','reasoning','multimodal','agent','benchmark',
    'генерац','модель','обновлен','выпуст','запуск','бесплат','открытый код','видео','изображен','доступ'
]
BAD_TERMS = [
    'sponsored','partner content','advertisement','paid post','promoted','sponsored content',
    'промо-материал','реклама','партнёрский материал','партнерский материал'
]
RUMOR_TERMS = ['rumor','rumour','leak','leaked','reportedly','allegedly','слух','утечк','предположительно','неподтвержд']

HEADERS = {'User-Agent': 'AINewsTelegramBot/1.0 (+RSS reader)'}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).isoformat()


def load_state():
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault('paused', False)
    state.setdefault('seen', {})
    state.setdefault('telegram_offset', 0)
    state.setdefault('last_scan', '')
    state.setdefault('last_success', '')
    state.setdefault('last_error_notice', '')
    state.setdefault('published_count', 0)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or '.', exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def tg(method, payload=None, timeout=25):
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN не задан в Secrets')
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    response = requests.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get('ok'):
        raise RuntimeError(f'Telegram API: {data}')
    return data.get('result')


def notify_admin(text):
    if ADMIN_CHAT_ID:
        try:
            tg('sendMessage', {'chat_id': ADMIN_CHAT_ID, 'text': text[:3900], 'disable_web_page_preview': True})
        except Exception as exc:
            print('Не удалось отправить уведомление администратору:', exc)


def clean_html(value):
    value = re.sub(r'<(script|style).*?</\1>', ' ', value or '', flags=re.I | re.S)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = html.unescape(value)
    return re.sub(r'\s+', ' ', value).strip()


def entry_date(entry):
    for key in ('published_parsed', 'updated_parsed', 'created_parsed'):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    for key in ('published', 'updated', 'created'):
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
    host = (urlparse(url).hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in TRUSTED_DOMAINS)


def stable_id(url, title):
    key = (url or '').split('#')[0].split('?')[0].rstrip('/') or title.lower().strip()
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def translate_ru(text):
    text = clean_html(text)
    if not text:
        return ''
    # Translation is free but uses an unofficial Google Translate interface; failures fall back to source text.
    try:
        from deep_translator import GoogleTranslator
        if re.search(r'[А-Яа-яЁё]', text):
            return text
        out = GoogleTranslator(source='auto', target='ru').translate(text[:4500])
        return out or text
    except Exception as exc:
        print('Перевод недоступен, оставляю оригинал:', exc)
        return text


def is_rumor(text):
    low = text.lower()
    return any(term in low for term in RUMOR_TERMS)


def score_item(title, summary, source, published):
    blob = f'{title} {summary}'.lower()
    if any(term in blob for term in BAD_TERMS):
        return -100
    score = 1
    score += sum(1 for term in GOOD_TERMS if term in blob)
    if source in ('OpenAI', 'Anthropic', 'Google DeepMind', 'Hugging Face', 'NVIDIA AI', 'Google Research', 'Microsoft Research', 'Meta AI', 'Stability AI', 'Runway'):
        score += 2
    if is_rumor(blob):
        score -= 1
    if published and published > now_utc() - timedelta(hours=24):
        score += 2
    if len(title.strip()) < 12:
        score -= 2
    return score


def fetch_candidates():
    candidates, errors = [], []
    cutoff = now_utc() - timedelta(hours=72)
    for source, feed_url in FEEDS:
        try:
            response = requests.get(feed_url, headers=HEADERS, timeout=18)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.entries:
                errors.append(f'{source}: RSS не вернул записей')
                continue
            for entry in feed.entries[:20]:
                title = clean_html(entry.get('title', ''))
                url = entry.get('link', '').strip()
                summary = clean_html(entry.get('summary', entry.get('description', '')))
                published = entry_date(entry)
                if not title or not url or not domain_ok(url):
                    continue
                if published is None or published > now_utc() + timedelta(hours=12) or published < cutoff:
                    continue
                score = score_item(title, summary, source, published)
                if score >= 2:
                    candidates.append({
                        'id': stable_id(url, title), 'source': source, 'title': title,
                        'url': url, 'summary': summary[:1200], 'published': published,
                        'score': score, 'rumor': is_rumor(title + ' ' + summary)
                    })
        except Exception as exc:
            errors.append(f'{source}: {type(exc).__name__}: {str(exc)[:180]}')
    candidates.sort(key=lambda x: (x['score'], x['published']), reverse=True)
    return candidates, errors


def make_post(item):
    title_ru = translate_ru(item['title'])
    summary_ru = translate_ru(item['summary']) if item['summary'] else ''
    if item['rumor']:
        label = '⚠️ <b>Слух / не подтверждено</b>\n\n'
    else:
        label = ''
    if not summary_ru:
        summary_ru = 'Ключевая информация — в материале источника.'
    summary_ru = summary_ru[:650].strip()
    source = item['source']
    useful = 'Полезно тем, кто следит за новыми возможностями ИИ и нейросетей.'
    text = (
        f'{label}🤖 <b>{html.escape(title_ru[:220])}</b>\n\n'
        f'{html.escape(summary_ru)}\n\n'
        f'💡 <b>Кому полезно:</b> {useful}\n\n'
        f'#нейросети #ИИ #AI\n'
        f'Источник: <a href="{html.escape(item["url"], quote=True)}">{html.escape(source)}</a>'
    )
    return text[:4000]


def handle_commands(state):
    changed = False
    try:
        updates = tg('getUpdates', {
            'offset': int(state.get('telegram_offset', 0)),
            'timeout': 1,
            'allowed_updates': ['message', 'channel_post']
        }, timeout=8)
        for update in updates or []:
            state['telegram_offset'] = max(int(state.get('telegram_offset', 0)), int(update['update_id']) + 1)
            channel_post = update.get('channel_post')
            if channel_post:
                channel_chat = channel_post.get('chat') or {}
                if channel_chat.get('type') == 'channel' and not state.get('private_channel_id_reported'):
                    notify_admin('ℹ️ Обнаружен канал Telegram. Его ID: ' + str(channel_chat.get('id')) + '\nДля приватного канала укажите этот ID в секрете CHANNEL_ID. Для публичного можно использовать @username.')
                    state['private_channel_id_reported'] = True
                changed = True
                continue
            msg = update.get('message') or {}
            chat = msg.get('chat') or {}
            text = (msg.get('text') or '').strip().split('@')[0].lower()
            chat_id = str(chat.get('id', ''))
            if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
                continue
            if text == '/start':
                tg('sendMessage', {'chat_id': chat_id, 'text': 'Команды агента:\n/status — состояние\n/pause — приостановить публикации\n/resume — возобновить публикации'})
            elif text == '/status':
                last = state.get('last_scan') or 'ещё не запускался'
                tg('sendMessage', {'chat_id': chat_id, 'text': f'Статус: {"пауза" if state["paused"] else "работает"}\nПоследняя проверка: {last}\nОпубликовано агентом: {state.get("published_count", 0)}'})
            elif text == '/pause':
                state['paused'] = True
                tg('sendMessage', {'chat_id': chat_id, 'text': 'Публикации приостановлены. Команды управления продолжают работать.'})
            elif text == '/resume':
                state['paused'] = False
                tg('sendMessage', {'chat_id': chat_id, 'text': 'Публикации возобновлены.'})
            changed = True
    except Exception as exc:
        print('Ошибка проверки команд:', exc)
    return changed


def prune_seen(state):
    cutoff = now_utc() - timedelta(days=7)
    cleaned = {}
    for key, val in state.get('seen', {}).items():
        try:
            dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                cleaned[key] = val
        except Exception:
            continue
    state['seen'] = cleaned


def main():
    if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_CHAT_ID:
        raise RuntimeError('Нужно задать BOT_TOKEN, CHANNEL_ID и ADMIN_CHAT_ID в GitHub Secrets')
    state = load_state()
    handle_commands(state)
    prune_seen(state)

    last_scan = None
    if state.get('last_scan'):
        try:
            last_scan = datetime.fromisoformat(state['last_scan'].replace('Z', '+00:00'))
        except Exception:
            last_scan = None
    due = last_scan is None or now_utc() - last_scan >= timedelta(minutes=28)
    if state.get('paused') or not due:
        save_state(state)
        print('Пауза или проверка ещё не требуется.')
        return

    state['last_scan'] = iso()
    candidates, errors = fetch_candidates()
    print(f'Найдено кандидатов: {len(candidates)}; ошибок источников: {len(errors)}')
    candidate = next((x for x in candidates if x['id'] not in state['seen']), None)
    if candidate:
        post = make_post(candidate)
        tg('sendMessage', {
            'chat_id': CHANNEL_ID,
            'text': post,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False
        })
        state['seen'][candidate['id']] = iso()
        state['last_success'] = iso()
        state['published_count'] = int(state.get('published_count', 0)) + 1
        print('Опубликовано:', candidate['title'])
    else:
        print('Достойных новых новостей нет — публикация пропущена.')

    if errors:
        # Notify only if errors occur in multiple sources; rate limit notices to once per 6 hours.
        if len(errors) >= 4:
            last_notice = state.get('last_error_notice')
            allowed = True
            if last_notice:
                try:
                    allowed = now_utc() - datetime.fromisoformat(last_notice.replace('Z', '+00:00')) >= timedelta(hours=6)
                except Exception:
                    pass
            if allowed:
                notify_admin('⚠️ Ошибки при проверке RSS-источников:\n' + '\n'.join(errors[:8]))
                state['last_error_notice'] = iso()
    save_state(state)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Критическая ошибка:', repr(exc))
        try:
            notify_admin('🚨 Агент новостей столкнулся с ошибкой:\n' + str(exc)[:1000])
        except Exception:
            pass
        raise
