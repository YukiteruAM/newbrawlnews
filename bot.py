"""Бот новостей Brawl Stars: собирает новости из первоисточников,
переводит на русский и публикует в Telegram-канал. Дубли отсекаются."""
import os, re, io, json, html, time, uuid, datetime
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")  # @имя_канала или -100...

MAX_PER_RUN = 3            # максимум постов за один запуск
DAILY_REGULAR_LIMIT = 8    # максимум обычных (не важных) постов в сутки
PAUSE = 3                  # пауза между постами, сек
STATE_FILE = "seen.json"
SEEN_KEEP = 1000
RECENT_KEEP = 200          # сколько последних новостей помнить для поиска дублей
RECENT_DAYS = 10
UA = "Mozilla/5.0 (compatible; BrawlNewsBot/3.0)"

# important=True  -> картинка + текст + ссылка «Подробнее»
# important=False -> только картинка + текст (посты без картинки пропускаются)
SOURCES = [
    # Блог Supercell по Brawl Stars: патчноуты, ивенты, киберспорт, сообщество
    {"name": "supercell-blog", "type": "supercell", "kind": "blog",
     "url": "https://supercell.com/en/games/brawlstars/blog/", "important": True},
    # Анонсы Supercell по Brawl Stars: коллаборации, чемпионы, большие новости
    {"name": "supercell-announcements", "type": "supercell", "kind": "announcement",
     "url": "https://supercell.com/en/news/announcement/brawlstars/", "important": True},
    # Выход новой версии игры в App Store (версия + заметки об обновлении)
    {"name": "appstore-update", "type": "appstore", "app_id": "1229016807", "important": True},
    # Официальный YouTube Brawl Stars (Brawl Talk, трейлеры, анонсы)
    {"name": "yt-brawlstars", "type": "youtube", "handle": "BrawlStars", "important": True},
    # Официальный YouTube киберспорта Brawl Stars
    {"name": "yt-esports", "type": "youtube", "handle": "brawlstarsesports", "important": False},
    # Официальные посты Supercell на r/Brawlstars (проверь название метки, если пусто)
    {"name": "reddit-official", "type": "feed", "important": True,
     "url": "https://www.reddit.com/r/Brawlstars/search.rss?q=flair%3A%22Supercell+Official%22&restrict_sr=on&sort=new"},
    # Самое популярное за день на r/Brawlstars
    {"name": "reddit-top", "type": "feed", "important": False,
     "url": "https://www.reddit.com/r/Brawlstars/top/.rss?t=day"},
]

# слова, которые остаются на английском (добавляй свои, например имена бойцов)
KEEP = ["Brawl Stars", "Brawl Pass", "Brawl Talk", "Starr Drop", "Supercell",
        "Clash Royale", "Clash of Clans", "Hay Day", "Boom Beach"]
KEEP_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(KEEP, key=len, reverse=True)) + r")\b",
    re.I)

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CYR = re.compile("[а-яА-ЯёЁ]")


# ---------- сеть ----------
def get(url, headers=None, tries=3):
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=30).read()
        except Exception as ex:
            last = ex
            time.sleep(2 * (i + 1))
    raise last


def download(url, limit=10 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=30).read(limit + 1)
    if len(data) > limit:
        raise ValueError("картинка слишком большая")
    return data


# ---------- текст ----------
def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\[link\]|\[comments\]|submitted by\s+/u/\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(s, n=300):
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",.;: ") + "…"


def mostly_russian(s):
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", s)
    return bool(letters) and len(CYR.findall(s)) / len(letters) > 0.6


# ---------- перевод ----------
def _gtx(masked):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q="
           + urllib.parse.quote(masked))
    data = json.loads(get(url, tries=2))
    return "".join(p[0] for p in data[0])


def _mymemory(masked):
    if len(masked.encode()) > 450:
        raise ValueError("слишком длинно для запасного переводчика")
    url = ("https://api.mymemory.translated.net/get?langpair=en|ru&q="
           + urllib.parse.quote(masked))
    data = json.loads(get(url, tries=2))
    return data["responseData"]["translatedText"]


ENGINES = [_gtx, _mymemory]


def _validate(masked, kept, result):
    rest = re.sub(r"ZZ\d+ZZ", "", masked)
    words = re.findall(r"[A-Za-z]{2,}", rest)
    cyr = len(CYR.findall(result))
    lat = len(re.findall(r"[A-Za-z]", re.sub(r"ZZ\s*\d+\s*ZZ", "", result)))
    if len(words) >= 4 and cyr < 0.5 * (cyr + lat):
        raise ValueError("перевод не удался")
    pattern = re.compile(r"ZZ\s*(\d+)\s*ZZ", re.I)
    found = pattern.findall(result)
    if len(found) != len(kept) or any(int(i) >= len(kept) for i in found):
        raise ValueError("ключевые слова потерялись при переводе")
    return pattern.sub(lambda m: kept[int(m.group(1))], result)


def translate(text):
    if not text:
        return ""
    if mostly_russian(text):
        return text
    kept = []

    def mask(m):
        kept.append(m.group(0))
        return f"ZZ{len(kept) - 1}ZZ"

    masked = KEEP_RE.sub(mask, text)
    last = None
    for engine in ENGINES:
        try:
            return _validate(masked, kept, engine(masked))
        except Exception as ex:
            last = ex
    raise ValueError(f"перевод не получился: {last}")


# ---------- поиск дублей ----------
GENERIC = set("""the and for with from this that your you are has have now out get into will its all
more here how what when why about was were can our their just new update updates video videos official
brawler brawlers skin skins game games""".split())


def canon(url):
    """Приводит ссылку к виду, по которому одинаковые ссылки совпадают.
    Главные страницы, каналы, профили и разделы пустой ключ не дают,
    иначе разные новости считались бы дублями из-за общей ссылки."""
    try:
        u = urllib.parse.urlparse(url.strip())
    except Exception:
        return ""
    host = u.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        vid = u.path.strip("/")
        return "yt:" + vid if re.fullmatch(r"[\w-]{11}", vid) else ""
    if host.endswith("youtube.com"):
        q = urllib.parse.parse_qs(u.query)
        if "v" in q:
            return "yt:" + q["v"][0]
        m = re.match(r"/(?:shorts|embed|live)/([\w-]{11})", u.path)
        return "yt:" + m.group(1) if m else ""  # каналы и плейлисты не в счёт
    if host.endswith("reddit.com"):
        m = re.search(r"/comments/(\w+)", u.path)
        return "reddit:" + m.group(1) if m else ""  # профили и сабреддиты не в счёт
    path = u.path.rstrip("/")
    segs = [x for x in path.split("/") if x]
    if not segs:
        return ""
    last = segs[-1]
    # нужна ссылка именно на статью: слаг с дефисом или длинный числовой id
    if (len(segs) >= 2 and "-" in last and len(last) >= 8) or re.fullmatch(r"\d{6,}", last):
        return host + path
    return ""


def keys_of(item):
    ks = set()
    for u in [item.get("link", "")] + list(item.get("urls", [])):
        k = canon(u) if u else ""
        if k:
            ks.add(k)
    iid = str(item.get("id", ""))
    if iid.startswith("yt:video:"):
        ks.add("yt:" + iid[len("yt:video:"):])
    return ks


def tokens_of(title):
    t = KEEP_RE.sub(" ", (title or "").lower())
    out = set()
    for w in re.findall(r"[a-z0-9]{3,}", t):
        if w in GENERIC:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def is_dup(keys, toks, important, recent):
    for r in recent:
        if important and not r.get("imp"):
            continue  # важная новость не прячется за обычным постом
        if keys & set(r["k"]):
            return True
        rt = set(r["t"])
        if toks and rt:
            common = len(toks & rt)
            if (common >= 3 and common / min(len(toks), len(rt)) >= 0.6) or \
                    (len(toks) >= 2 and toks == rt):
                return True
    return False


# ---------- разбор источников ----------
def ctext(e, *tags):
    for tag in tags:
        n = e.find(tag)
        if n is not None and n.text and n.text.strip():
            return n.text.strip()
    return ""


def pick_image(e, body):
    body = html.unescape(body or "")
    m = re.search(r'https://i\.redd\.it/[^"\s<>]+\.(?:jpe?g|png|webp)', body, re.I)
    if m:
        return m.group(0)
    for enc in e.findall("enclosure"):
        if (enc.get("type") or "").startswith("image") and enc.get("url"):
            return enc.get("url")
    t = e.find(".//" + MEDIA + "thumbnail")
    if t is not None and t.get("url"):
        return html.unescape(t.get("url"))
    m = re.search(r'<img[^>]+src="([^"]+)"', body)
    return html.unescape(m.group(1)) if m else ""


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = root.findall(ATOM + "entry") or list(root.iter("item"))
    items = []
    for e in entries:
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href")
                break
        if not link:
            link = ctext(e, "link")
        body = ctext(e, ATOM + "content", ATOM + "summary", "description")
        if not body:
            d = e.find(".//" + MEDIA + "description")
            body = d.text if d is not None and d.text else ""
        item_id = ctext(e, ATOM + "id", "guid", "id") or link
        title = clean(ctext(e, ATOM + "title", "title"))
        if not title or not item_id:
            continue
        urls = re.findall(r'https?://[^\s"<>)\]]+', html.unescape(body))
        items.append({
            "id": item_id,
            "title": title,
            "link": link,
            "text": shorten(clean(body)),
            "img": pick_image(e, body),
            "urls": urls,
        })
    return items


def metas(page):
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        attrs = {}
        for k, a, b in re.findall(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag):
            attrs[k.lower()] = a or b
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs and key.lower() not in out:
            out[key.lower()] = html.unescape(attrs["content"]).strip()
    return out


SUPERCELL_LINKS = {
    # /en/games/brawlstars/blog/<раздел>/<статья>/
    "blog": (r'href="((?:https://supercell\.com)?/en/games/brawlstars/blog/([a-z0-9-]+)/([a-z0-9-]+)/?)"', {"page"}),
    # /en/news/<статья>/
    "announcement": (r'href="((?:https://supercell\.com)?/en/news/([a-z0-9][a-z0-9-]*)/?)"', {"announcement", "blog"}),
}


def fetch_supercell(src, state):
    page = get(src["url"]).decode("utf-8", "replace")
    pat, skip = SUPERCELL_LINKS[src.get("kind", "blog")]
    links = []
    for m in re.finditer(pat, page):
        if m.group(2) in skip:
            continue
        path = m.group(1)
        url = path if path.startswith("http") else "https://supercell.com" + path
        if url not in links:
            links.append(url)
    # подробности (заголовок, описание, картинка) грузим позже, только для новых
    return [{"id": u, "link": u, "lazy": True} for u in links[:10]]


def fill_supercell(item):
    page = get(item["link"]).decode("utf-8", "replace")
    m = metas(page)
    title = re.sub(r"\s*[×|\-–—]\s*Supercell\s*$", "", m.get("og:title", ""))
    if not title:
        raise ValueError("нет заголовка у статьи")
    img = m.get("og:image", "")
    item["title"] = title
    item["text"] = shorten(clean(m.get("og:description") or m.get("description", "")))
    item["img"] = "" if "og_default" in img else img
    item["urls"] = re.findall(
        r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}", page)
    item["lazy"] = False


def fetch_appstore(src, state):
    data = json.loads(get(f"https://itunes.apple.com/lookup?id={src['app_id']}&country=us"))
    r = data["results"][0]
    ver = r["version"]
    return [{
        "id": f"appstore:{ver}",
        "title": f"Вышло обновление Brawl Stars: версия {ver}",
        "title_ru": True,  # заголовок уже по-русски, переводить не нужно
        "topic": ("🛠", "Вышло обновление", "обновление"),
        "link": r.get("trackViewUrl", ""),
        "text": shorten(clean(r.get("releaseNotes", ""))),
        "img": r.get("artworkUrl512", ""),
    }]


def yt_channel_id(handle, state):
    cache = state.setdefault("yt", {})
    if handle in cache:
        return cache[handle]
    page = get(f"https://www.youtube.com/@{handle}",
               {"Cookie": "CONSENT=YES+1; SOCS=CAI", "Accept-Language": "en-US,en;q=0.9"}
               ).decode("utf-8", "replace")
    pats = (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
            r'"externalId":"(UC[\w-]{22})"',
            r'"channelId":"(UC[\w-]{22})"')
    for pat in pats:
        m = re.search(pat, page)
        if m:
            cache[handle] = m.group(1)
            return m.group(1)
    raise ValueError(f"не нашёл channel_id для @{handle}")


def fetch(src, state):
    t = src["type"]
    if t == "supercell":
        return fetch_supercell(src, state)
    if t == "appstore":
        return fetch_appstore(src, state)
    if t == "youtube":
        cid = yt_channel_id(src["handle"], state)
        return parse_feed(get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
    return parse_feed(get(src["url"]))


# ---------- Telegram ----------
def tg_json(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=30).read()


def tg_upload(photo, caption):
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", CHAT), ("caption", caption), ("parse_mode", "HTML")):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="p.jpg"\r\n'
                'Content-Type: image/jpeg\r\n\r\n').encode())
    body.write(photo)
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    return urllib.request.urlopen(req, timeout=60).read()


# ---------- оформление ----------
# (регулярка по английскому тексту, эмодзи, подпись, хештег); срабатывает первая подходящая
TOPICS = [
    (r"maintenance|downtime", "🔧", "Технические работы", "техработы"),
    (r"release notes|patch notes|balance|patch|hotfix|changelog|version|update", "🛠", "Обновление", "обновление"),
    (r"world finals|championship|\bbsc\b|tournament|qualifier|finals|esports|champions", "🏆", "Киберспорт", "киберспорт"),
    (r"crossover|collaboration|\bcollab\b|partnership|takeover|\bx\b", "🤝", "Коллаборация", "коллаборация"),
    (r"new brawler|introducing|brawler.{0,20}(?:coming|joining|arrives|reveal)", "🦸", "Новый боец", "новыйбоец"),
    (r"brawl talk", "🎤", "Brawl Talk", "BrawlTalk"),
    (r"skin|cosmetic|bling", "🎨", "Скины", "скины"),
    (r"leak|rumou?r|sneak peek|teaser", "👀", "Утечки и слухи", "утечки"),
    (r"giveaway|free|gift|reward|voucher|\bcode", "🎁", "Подарки", "подарки"),
    (r"event|season|starr drop|challenge|\bmode\b|\bmap\b", "🎉", "События", "события"),
    (r"trailer|video|animation|livestream|stream", "🎬", "Видео", "видео"),
]
TOPICS = [(re.compile(p, re.I), e, l, t) for p, e, l, t in TOPICS]
DEFAULT_TOPIC = ("📰", "Новости", "новости")

# эмодзи после ключевых слов уже готового русского текста (по одному на слово, не больше лимита)
DECOR = [(re.compile(p, re.I), e) for p, e in [
    (r"\bтехническ\w+ (?:работ\w*|перерыв\w*)|\bтехобслуживан\w*", "🔧"),
    (r"\bобновлени\w*|\bпатч\w*", "🛠"),
    (r"\bчемпион\w*|\bтурнир\w*|\bфинал\w*", "🏆"),
    (r"\bколлаборац\w*|\bкроссовер\w*", "🤝"),
    (r"\bскин\w*", "🎨"),
    (r"\bбесплатн\w*|\bподар\w+|\bнаград\w*", "🎁"),
    (r"\bгиперзаряд\w*|\bhypercharge\b", "⚡"),
    (r"\bBrawl Pass\b", "🎟"),
    (r"\bсобыти\w*|\bивент\w*|\bсезон\w*", "🎉"),
    (r"\bтрейлер\w*|\bролик\w*|\bвидео\b", "🎬"),
    (r"\bутечк\w*|\bслух\w*", "👀"),
    (r"\bбо(?:ец|йц\w*)\b", "🥊"),
]]
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
MAX_CAPTION = 1024


def item_topic(item):
    if item.get("topic"):
        return item["topic"]
    blob = f"{item.get('title', '')} {item.get('text', '')}"
    for pat, emoji, label, tag in TOPICS:
        if pat.search(blob):
            return emoji, label, tag
    return DEFAULT_TOPIC


def decorate(text, limit, skip=()):
    """Добавляет эмодзи после ключевых слов (не больше limit штук).
    Эмодзи из skip не повторяются (например, тот, что уже стоит в заголовке)."""
    if not text or limit <= 0 or EMOJI_RE.search(text):
        return text
    for pat, emo in DECOR:
        if limit <= 0:
            break
        if emo in skip:
            continue
        m = pat.search(text)
        if m:
            text = text[:m.end()] + " " + emo + text[m.end():]
            limit -= 1
    return text


def link_label(link):
    k = canon(link)
    if k.startswith("yt:"):
        return "▶️ Смотреть видео"
    if k.startswith("reddit:"):
        return "💬 Обсудить на Reddit"
    return "🔗 Читать подробнее"


def build_post(item, title, text, important):
    """Возвращает HTML для Telegram. Видимая длина не больше 1024 символов."""
    esc = html.escape
    emoji, label, tag = item_topic(item)
    title = decorate(title[:500], 1, skip=(emoji,))
    text = decorate(text, 2, skip=(emoji,))
    link = item.get("link", "") if important else ""
    tags = f"#BrawlStars #{tag}"

    blocks = []  # (видимый текст, html)
    if important:
        head = f"🔥 ВАЖНО · {emoji} {label}"
        blocks.append((head, f"<b>🔥 ВАЖНО</b> · {esc(emoji + ' ' + label)}"))
        blocks.append((title, f"<b>{esc(title)}</b>"))
    else:
        t = f"{emoji} {title}"
        blocks.append((t, f"<b>{esc(t)}</b>"))
    body_at = len(blocks)
    if link:
        lab = link_label(link)
        blocks.append((lab, f'<a href="{esc(link, quote=True)}">{esc(lab)}</a>'))
    blocks.append((tags, esc(tags)))

    used = len("\n\n".join(b[0] for b in blocks))
    room = MAX_CAPTION - used - 2
    if text:
        if len(text) > room:
            text = text[:room - 1].rstrip() + "…" if room > 40 else ""
        if text:
            blocks.insert(body_at, (text, esc(text)))
    return "\n\n".join(b[1] for b in blocks)


def send(item, important):
    title = item["title"] if item.get("title_ru") else translate(item["title"])
    text = translate(item.get("text", ""))
    post = build_post(item, title, text, important)
    img = item.get("img", "")
    if img:
        try:
            tg_json("sendPhoto", {"chat_id": CHAT, "photo": img, "caption": post,
                                  "parse_mode": "HTML"})
            return
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                raise
        except Exception:
            pass
        try:  # Telegram не смог сам скачать картинку: качаем и загружаем сами
            tg_upload(download(img), post)
            return
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                raise
        except Except
