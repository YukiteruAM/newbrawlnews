"""Бот новостей Brawl Stars: собирает новости из первоисточников,
переводит на русский и публикует в Telegram-канал. Дубли отсекаются."""
import os, sys, re, io, json, html, time, uuid, datetime, random
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")  # @имя_канала или -100...

MAX_PER_RUN = 3            # максимум постов за один запуск
DAILY_REGULAR_LIMIT = 8    # максимум обычных (не важных) постов в сутки
PAUSE = 3                  # пауза между постами, сек
TIME_BUDGET = 300          # через сколько секунд бот перестаёт брать новую работу
MAX_FAILS = 36             # сколько раз пробовать отправить запись (36 запусков ~ 6 часов)
FILL_FAILS = 5             # сколько раз пробовать загрузить статью Supercell
STATE_FILE = "seen.json"
SEEN_KEEP = 1000
RECENT_KEEP = 200          # сколько последних новостей помнить для поиска дублей
RECENT_DAYS = 10
TITLE_DUP_DAYS = 3         # схожие заголовки считаем дублем только в пределах этих дней
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# important=True  -> картинка + текст + ссылка, пометка «ВАЖНО», без дневного лимита
# important=False -> обычный пост: картинка + текст (посты без картинки пропускаются)
# link=True       -> у обычного поста всё равно показывать ссылку (для видео)
# expect/reject   -> проверка названия YouTube-канала, чтобы не публиковать чужое
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
    {"name": "yt-brawlstars", "type": "youtube", "handle": "BrawlStars", "important": True,
     "expect": "brawl stars", "reject": "esports"},
    # Официальный YouTube киберспорта Brawl Stars
    {"name": "yt-esports", "type": "youtube", "handle": "brawlstarsesports", "important": False,
     "link": True, "expect": "esports"},
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


def _keep_pattern(k):
    # допускаем «BrawlStars» слитно и множественное число («Starr Drops»)
    return re.escape(k).replace(r"\ ", r"\s?") + "s?"


KEEP_RE = re.compile(
    r"\b(" + "|".join(_keep_pattern(k) for k in sorted(KEEP, key=len, reverse=True)) + r")\b",
    re.I)

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CYR = re.compile("[а-яА-ЯёЁ]")


class FatalError(Exception):
    """Ошибка, после которой продолжать нет смысла (неверный токен, нет прав)."""


class SkipItem(Exception):
    """Запись не нужна (например, не про Brawl Stars)."""


# ---------- сеть ----------
def get(url, headers=None, tries=2):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=20).read()
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (408, 425, 429, 500, 502, 503, 504):
                break  # 403, 404 и т.п.: повторять бессмысленно
            time.sleep(2 * (i + 1))
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
    data = json.loads(get(url))
    return "".join(p[0] for p in data[0]).strip()


def _mymemory(masked):
    if len(masked.encode()) > 450:
        raise ValueError("слишком длинно для запасного переводчика")
    url = ("https://api.mymemory.translated.net/get?langpair=en|ru&q="
           + urllib.parse.quote(masked))
    data = json.loads(get(url))
    return html.unescape(data["responseData"]["translatedText"]).strip()


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
MONTHS = set("""january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec""".split())


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


SHORT_STOP = set("an of in on to is it as at by or vs x".split())


def tokens_of(title):
    t = KEEP_RE.sub(" ", (title or "").lower())
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if w in GENERIC or w in SHORT_STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _marks(tokens):
    """Признаки, отличающие выпуски одной серии: числа, месяцы, короткие метки (A, B, 2)."""
    return {w for w in tokens if w.isdigit() or w in MONTHS or len(w) <= 2}


def is_dup(keys, toks, important, recent, now=None):
    now = now or time.time()
    for r in recent:
        if important and not r.get("imp"):
            continue  # важная новость не прячется за обычным постом
        if keys & set(r["k"]):
            return True
        rt = set(r["t"])
        if not (toks and rt) or now - r.get("ts", 0) > TITLE_DUP_DAYS * 86400:
            continue
        ma, mb = _marks(toks), _marks(rt)
        if ma and mb and ma != mb:
            continue  # «Release Notes August» и «Release Notes September» не одно и то же
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


_END = r"""(?=[/"'\\?#<\s,)])"""
SUPERCELL_LINKS = {
    # /en/games/brawlstars/blog/<раздел>/<статья>/
    "blog": (r"((?:https://supercell\.com)?/en/games/brawlstars/blog/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+))" + _END,
             {"page", "tag", "category"}),
    # /en/news/<статья>/
    "announcement": (r"((?:https://supercell\.com)?/en/news/([A-Za-z0-9][A-Za-z0-9_-]*))" + _END,
                     {"announcement", "blog", "brawlstars", "page"}),
}


def fetch_supercell(src, state):
    kind = src.get("kind", "blog")
    page = get(src["url"]).decode("utf-8", "replace")
    page = page.replace("\\/", "/").replace("\\u002F", "/")  # ссылки внутри JSON
    pat, skip = SUPERCELL_LINKS[kind]
    links = []
    for m in re.finditer(pat, page):
        if any(seg in skip for seg in m.groups()[1:]):
            continue
        path = m.group(1)
        url = path if path.startswith("http") else "https://supercell.com" + path
        url = url.rstrip("/") + "/"
        if url not in links:
            links.append(url)
    if not links:
        print(f"  на странице {src['url']} не нашёл ссылок на статьи (длина страницы {len(page)})")
    # подробности (заголовок, описание, картинка) грузим позже, только для новых
    return [{"id": u, "link": u, "lazy": True, "kind": kind} for u in links[:12]]


def fill_supercell(item):
    page = get(item["link"]).decode("utf-8", "replace")
    m = metas(page)
    title = re.sub(r"\s*[×|\-–—]\s*Supercell\s*$", "", m.get("og:title", ""))
    if not title:
        raise ValueError("нет заголовка у статьи")
    desc = m.get("og:description") or m.get("description", "")
    if item.get("kind") == "announcement" and not re.search(
            r"brawl", f"{title} {desc} {item['link']}", re.I):
        raise SkipItem("не про Brawl Stars")
    img = m.get("og:image", "")
    item["title"] = title
    item["text"] = shorten(clean(desc))
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
               {"Cookie": "CONSENT=YES+1; SOCS=CAI"}).decode("utf-8", "replace")
    pats = (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
            r'"externalId":"(UC[\w-]{22})"',
            r'"channelId":"(UC[\w-]{22})"')
    for pat in pats:
        m = re.search(pat, page)
        if m:
            cache[handle] = m.group(1)
            return m.group(1)
    raise ValueError(f"не нашёл channel_id для @{handle}")


def fetch_youtube(src, state):
    cid = yt_channel_id(src["handle"], state)
    xml = get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
    name = (ET.fromstring(xml).findtext(ATOM + "title") or "").strip()
    low = name.lower()
    if (src.get("expect") and src["expect"] not in low) or \
            (src.get("reject") and src["reject"] in low):
        state.get("yt", {}).pop(src["handle"], None)
        raise ValueError(f"канал @{src['handle']} называется «{name}», это не тот канал")
    return parse_feed(xml)


def fetch(src, state):
    t = src["type"]
    if t == "supercell":
        items = fetch_supercell(src, state)
    elif t == "appstore":
        items = fetch_appstore(src, state)
    elif t == "youtube":
        items = fetch_youtube(src, state)
    else:
        items = parse_feed(get(src["url"]))
    if src.get("link"):
        for it in items:
            it["force_link"] = True
    return items


def fetch_all(sources, state):
    """Загружает все источники одновременно. Возвращает {имя: записи или None}."""
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(sources)))) as pool:
        futs = {s["name"]: pool.submit(fetch, s, state) for s in sources}
        for s in sources:
            try:
                out[s["name"]] = futs[s["name"]].result(timeout=120)
            except Exception as ex:
                print("источник недоступен:", s["name"], "-", ex)
                out[s["name"]] = None
    return out


# ---------- Telegram ----------
def _fatal_check(ex):
    if ex.code in (401, 403):
        raise FatalError(
            "Telegram отказал в доступе: проверь токен бота (BOT_TOKEN) и что бот "
            "добавлен администратором канала с правом публикации.")


def tg_json(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as ex:
        _fatal_check(ex)
        raise


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
    try:
        return urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as ex:
        _fatal_check(ex)
        raise


def check_telegram():
    """Проверка в начале запуска: токен верный, бот админ канала с правом публикации."""
    try:
        me = json.loads(tg_json("getMe", {}))["result"]
        info = json.loads(tg_json("getChatMember", {"chat_id": CHAT, "user_id": me["id"]}))["result"]
    except FatalError:
        raise
    except urllib.error.HTTPError as ex:
        if ex.code in (400, 404):
            raise FatalError("Telegram не нашёл канал или бота: проверь секреты CHAT_ID "
                             "(адрес канала с @) и BOT_TOKEN.")
        print("проверка Telegram пропущена:", ex)
        return
    except Exception as ex:
        print("проверка Telegram пропущена:", ex)
        return
    status = info.get("status")
    if status not in ("administrator", "creator"):
        raise FatalError("Бот не администратор канала. Добавь его администратором "
                         "с правом «Публикация сообщений».")
    if status == "administrator" and info.get("can_post_messages") is False:
        raise FatalError("У бота нет права «Публикация сообщений» в канале.")


# ---------- оформление ----------
# (регулярка по английскому тексту, эмодзи, подпись, хештег); срабатывает первая подходящая
TOPICS = [
    (r"\bmaintenance\b|\bdowntime\b", "🔧", "Технические работы", "техработы"),
    (r"\brelease notes\b|\bpatch\b|\bhotfix\b|\bchangelog\b|\bbalance changes?\b|\bversion\b|\bupdate", "🛠", "Обновление", "обновление"),
    (r"\bworld finals\b|\bchampionship|\bbsc\b|\btournament|\bqualifier|\bfinals\b|\besports\b|\bchampions\b", "🏆", "Киберспорт", "киберспорт"),
    (r"\bcrossover|\bcollaboration|\bcollab\b|\bpartnership|\btakeover\b|\bx\b", "🤝", "Коллаборация", "коллаборация"),
    (r"\bnew brawler|\bintroducing\b|\bbrawlers? (?:is |are )?(?:coming|joining|arrives?|reveal)", "🦸", "Новый боец", "новыйбоец"),
    (r"\bbrawl talk\b", "🎤", "Brawl Talk", "BrawlTalk"),
    (r"\bskins?\b|\bcosmetic|\bbling\b", "🎨", "Скины", "скины"),
    (r"\bleaks?\b|\bleaked\b|\brumou?rs?\b|\bsneak peek\b|\bteaser", "👀", "Утечки и слухи", "утечки"),
    (r"\bgiveaway|\bfree\b|\bgifts?\b|\brewards?\b|\bvoucher|\bcodes?\b", "🎁", "Подарки", "подарки"),
    (r"\bevents?\b|\bseasons?\b|\bstarr drop|\bchallenge|\bmodes?\b|\bmaps?\b", "🎉", "События", "события"),
    (r"\btrailer|\bvideos?\b|\banimation|\blivestream|\bstream\b", "🎬", "Видео", "видео"),
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
MAX_CAPTION = 980  # лимит Telegram 1024, берём с запасом на эмодзи


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


# ---------- фразы от бота ----------
# Как выбирается фраза:
# 1) тема: у каждой группы считаются совпадения (в заголовке вес 3, в описании 1), побеждает набравшая больше;
#    одного слабого упоминания в описании мало, тогда берётся нейтральная фраза;
# 2) тактичность: при грустных или спорных новостях шуточные фразы не используются;
# 3) время: «уже забрали?» подходит к вышедшему, «будете смотреть?» к анонсу, поэтому у многих групп
#    есть три набора: any (всегда), now (уже вышло или произошло), soon (скоро). Не уверены во времени: только any.
# Порядок групп важен: при равном счёте побеждает та, что выше.
PHRASE_RULES = [(g, re.compile(p, re.I)) for g, p in [
    ("maintenance", r"\bmaintenance\b|\bdowntime\b"),
    ("leak", r"\bleaks?\b|\bleaked\b|\brumou?rs?\b|\bdata ?mine|\binsider\b"),
    ("balance", r"\bbuffs?\b|\bbuffed\b|\bnerfs?\b|\bnerfed\b|\bbalance changes?\b|\bbalance update\b"),
    ("update", r"\brelease notes\b|\bpatch notes\b|\bpatch\b|\bhotfix\b|\bchangelog\b|\bupdates?\b|\bversion\b"),
    ("drops", r"\bstarr drops?\b|\bmega box|\bboxes\b|\bchests?\b|\bloot\b"),
    ("rewards", r"\bgiveaway|\bfree\b|\bgifts?\b|\brewards?\b|\bvoucher|\bcodes?\b|\bclaim\b"),
    ("esports", r"\bworld finals\b|\bchampionship|\bbsc\b|\btournament|\bqualifier|\bfinals\b|\besports\b|\bchampions\b"),
    ("collab", r"\bcrossover|\bcollaboration|\bcollab\b|\bpartnership|\btakeover\b|\b[A-Za-z]+ x [A-Za-z]+\b"),
    ("brawler", r"\bnew brawler|\bintroducing\b|\bbrawlers? (?:is |are )?(?:coming|joining|arrives?|reveal)"),
    ("talk", r"\bbrawl talk\b"),
    ("skin", r"\bskins?\b|\bcosmetic|\bbling\b"),
    ("event", r"\bevents?\b|\bseasons?\b|\bchallenge|\bmodes?\b|\bmaps?\b"),
    ("video", r"\btrailer|\bvideos?\b|\banimation|\blivestream|\bstream\b"),
    ("meme", r"\bmemes?\b|\bfan ?art\b|\bart\b|\bconcept\b|\bfunny\b|\bdrawing|\bme when\b|\bcosplay"),
]]

# грустные и спорные новости: только спокойные фразы
SERIOUS_RE = re.compile(
    r"\bapolog|\bcompensat|\boutage\b|\bbanned?\b|\bban wave\b|\bhack(?:ed|ers?|ing)?\b|\bexploit"
    r"|\bscam|\blawsuit|\bpassed away\b|\bmemorial\b|\btragic|\bin memory\b|\bdeath\b|\bdied\b"
    r"|\bharass|\bcheaters?\b|\bcheating\b|\bdata breach\b|\bemergency\b|\bunexpected(?:ly)?\b"
    r"|\bdelayed\b|\bpostponed\b|\bcancel+ed\b|\bshutting down\b|\bsunset", re.I)
# жалобы (только для обычных постов с Reddit)
COMPLAINT_RE = re.compile(
    r"\bwhy (?:is|does|do|are|did|can't|cant)\b|\bworst\b|\bhate\b|\bbroken\b|\bunfair\b|\brigged\b"
    r"|\bplease fix\b|\bhelp\b|\bquit(?:ting)?\b|\btoxic\b|\bfrustrat|\bannoying\b|\bpay to win\b|\bp2w\b"
    r"|\bsucks?\b|\bterrible\b|\bawful\b|\bdisappoint", re.I)
# время события
SOON_RE = re.compile(
    r"\bcoming\b|\bsoon\b|\bnext (?:week|month|season|update|brawl talk)\b|\bupcoming\b|\bpreview\b"
    r"|\bsneak peek\b|\bteaser\b|\btomorrow\b|\bpre-?register|\bget ready\b|\bcountdown\b"
    r"|\bwill (?:be|get|arrive|come|start|begin|launch|return)\b|\b(?:starts?|begins?) (?:on|at|in)\b", re.I)
NOW_RE = re.compile(
    r"\bnow\b|\btoday\b|\blive\b|\bout now\b|\bavailable\b|\bis here\b|\bare here\b|\bhas arrived\b"
    r"|\bjust (?:dropped|released|launched)\b|\breleased\b|\blaunched\b|\bis out\b|\bare out\b"
    r"|\bcrowned\b|\bwon\b|\bwins?\b|\bvictory\b|\bdefeat|\bresults?\b|\brecap\b|\brelease notes\b"
    r"|\bpatch notes\b|\bstarted\b|\bkicks? off\b", re.I)


def _bank(any_, now=(), soon=()):
    return {"any": list(any_), "now": list(now), "soon": list(soon)}


PHRASES = {
    "drops": _bank(
        ["Любите дропы? 🎁", "Вам обычно везёт с дропами? 🍀", "Какой самый редкий дроп у вас выпадал? 🌟",
         "Дропы щедрые или нет? 🧐", "Что хотите получить из дропов больше всего? 🙏",
         "Какой дроп для вас мечта? ✨", "Жмите 🔥, если любите открывать дропы!"],
        ["Что вам выпало? 👀", "Повезло с дропом? 🍀", "Кто уже открыл? Делитесь, что выпало!",
         "Выпало что-то редкое? 😮", "Что выпало в первую очередь? 🎰", "Открывали уже? Как улов? 🎁",
         "А у вас какой улов? 🎣", "Жмите 🔥, если выпало что-то стоящее!",
         "Жмите 😢, если опять выпал мусор.", "Сколько дропов открыли? Хвастайтесь! 💪"],
        ["На что рассчитываете в дропах? 🤞", "Какой дроп хотите получить в первую очередь? 🎁",
         "Готовы открывать? 🍀", "Кому-то повезёт больше остальных. Вам? 😏"]),
    "rewards": _bank(
        ["Собираетесь забирать награды? 🎁", "Что из наград самое ценное для вас? 💎",
         "Как вам щедрость разработчиков? 💰", "Награда по вкусу или могло быть щедрее? 😏",
         "Какая награда нужна вам больше всего? 🎯", "Жмите 🎁, если планируете забрать!",
         "Любите бесплатные награды? 😄"],
        ["Уже забрали свои награды? ✅", "Кто уже забрал? Поднимите руку! ✋",
         "Не забудьте забрать, пока не пропало! ⏰", "Награды ждут. Успеете забрать? ⏳",
         "Что заберёте в первую очередь? 🤔", "Забираете всё или только самое нужное? 🧐",
         "Напомните себе зайти за наградой! 📲", "Отметьте друга, который точно забудет забрать! 😄",
         "Уже потратили награды или копите? 🪙"],
        ["Будете забирать награды, когда они появятся? 🎁", "Что хотите получить в первую очередь? 🤔",
         "Какую награду ждёте больше всего? 💎", "Не забудьте зайти за наградами, когда начнётся! ⏰"]),
    "balance": _bank(
        ["Согласны с такими изменениями? 🧐", "Что думаете о балансе? ⚖️",
         "Жмите 🔥, если вас всё устраивает!", "Какой боец, по-вашему, сейчас сильнее всех? 💪",
         "Кого бы вы ещё усилили? 🙏", "Кого бы вы ещё ослабили? 😏"],
        ["Кого усилили, рады? 💪", "Кого ослабили, жалко? 😢", "Ваш любимчик получил бафф? 🙌",
         "Ваш боец попал под нерф? 😬", "Баланс стал лучше или хуже? ⚖️",
         "Жмите 🔥, если ваш боец стал сильнее!", "Жмите 💔, если ваш боец стал слабее.",
         "Будете менять основного бойца? 🔄", "Кого пересядете качать после патча? 📊",
         "Кто теперь в топе меты, как думаете? 📈", "Какое изменение баланса удивило больше всего? 😮",
         "Кого ждали в списке баффов, но не дождались? 🙁"],
        ["Что изменится в мете после этого? 🤔", "Кого ждёте в списке баффов? 🙏",
         "Кого боитесь увидеть в списке нерфов? 😬", "Как думаете, кого пересмотрят? 🧐"]),
    "update": _bank(
        ["Что думаете об обновлении? 🤔", "Как вам такие перемены? 🔄",
         "Что бы вы добавили в следующем обновлении? 💡", "Есть что-то, что вы бы изменили? ✍️",
         "Чего вам не хватает в игре сейчас? 🧐", "Жмите 🔥, если любите обновления!",
         "Какое обновление запомнилось вам больше всего? 🏆"],
        ["Уже заходили в обновлённую игру? 🎮", "Что в обновлении понравилось больше всего? 👍",
         "Чего не хватило в этом обновлении? 🧐", "Обновление зашло или ждали большего? 😏",
         "Что из нового попробуете первым? 🚀", "Оценка обновлению по десятибалльной шкале? 🔟",
         "Жмите 🔥, если довольны обновлением!", "Жмите 👎, если ждали другого.",
         "Заметили баги? Расскажите! 🐛", "Какая новинка зацепила сильнее всего? 🎯"],
        ["Что ждёте от обновления больше всего? 🔥", "Какую новинку хотите попробовать первой? 🚀",
         "Готовы к обновлению? 🙌", "Жмите 🔥, если ждёте обновление!",
         "Что надеетесь увидеть в обновлении? 💡"]),
    "appupdate": _bank(
        ["Уже обновились? 📲", "Обновились, что нового заметили? 👀", "Кто ещё ждёт загрузку? ⏳",
         "Как вам свежая версия? 🤔", "Всё работает без глюков? 🐛",
         "Что проверите в первую очередь после обновления? 🎮", "Жмите 🔥, если уже в игре!",
         "Обновление скачано? Заходим! 🚀", "Что хотите увидеть в следующей версии? 💡",
         "Нашли что-то новое в игре? Рассказывайте! 🔍"]),
    "esports": _bank(
        ["Хотели бы сыграть в таком турнире? 🎮", "Болеем вместе! Пишите, за кого! 📣",
         "Что думаете о турнире? 🏆", "Кто ваша любимая команда? 💪",
         "Смотрите киберспорт по Brawl Stars? 📺", "Кто для вас лучший игрок? ⭐",
         "Жмите 🔥, если любите киберспорт!"],
        ["Смотрели матчи? Как впечатления? 🍿", "Какой матч запомнился больше всего? 🔥",
         "Согласны с результатом? 🧐", "Был момент, от которого мурашки по коже? 😮",
         "Кого считаете лучшим игроком турнира? ⭐", "Ожидали такого исхода? 😲",
         "Кто сегодня удивил? 👀", "Какая команда вас разочаровала? 😕",
         "Жмите 🔥, если смотрели трансляцию!"],
        ["За кого будете болеть? 🏆", "Кто, по-вашему, лучшая команда? 💪",
         "Кто станет чемпионом, как думаете? 🥇", "Будете смотреть трансляцию? 📺",
         "Кто ваш фаворит? 🔥"]),
    "collab": _bank(
        ["Как вам коллаборация? 🤝", "Нравится такой кроссовер? 🔥", "Ждали такого сотрудничества? 😮",
         "Что хотели бы увидеть в следующей коллаборации? 💡",
         "С кем бы ещё хотели увидеть коллаборацию? 🤔", "Это круто или перебор? 😏",
         "Идея топ или не очень? ⚡", "Кто бы мог подумать, что такое случится? 😲",
         "Жмите 🤷, если это не ваше."],
        ["Заберёте что-то из коллаборации? 🛒", "Жмите 🔥, если коллаборация зашла!",
         "Какой предмет из коллаборации хочется больше всего? 🎁"],
        ["Что ждёте от коллаборации? 👀", "Как думаете, что нас ждёт? 🤔",
         "Какой предмет коллаборации хотите больше всего? 🎁"]),
    "brawler": _bank(
        ["Берёте нового бойца или пропустите? 🤔", "Каким будет новый боец в мете? 📈",
         "Как вам дизайн нового бойца? 🎨", "Что скажете про способности новичка? ⚡",
         "Станет ли он любимчиком сообщества? ❤️", "Новый боец, хайп или пустышка? 🧐",
         "Какого бойца ещё хотели бы увидеть в игре? 💡", "Кто хочет такого в свою команду? 🙋"],
        ["Уже попробовали нового бойца? 🎮", "Жмите 🔥, если уже качаете нового бойца!",
         "Готовы качать его до максимума? 🚀", "Как он показал себя в бою? ⚔️"],
        ["Скорее бы попробовать! Кто ждёт? ⏳", "Жмите 🔥, если ждали нового бойца!",
         "Сильным получится новичок или слабым? 💪", "Какую роль пророчите новичку? 🎯"]),
    "talk": _bank(
        ["Что думаете о Brawl Talk? 🤔", "Что бы вы хотели увидеть в Brawl Talk? 💡",
         "Жмите 🔥, если любите Brawl Talk!", "Какой Brawl Talk запомнился вам больше всего? 🏆",
         "Смотрите Brawl Talk каждый раз? 📺", "Какая новинка из таких трансляций была вашей любимой? 🎯"],
        ["Смотрели в прямом эфире? 📺", "Что из показанного ждёте больше всего? 🔥",
         "Какая новость в трансляции удивила? 😮", "Всё ли понравилось в трансляции? 🧐",
         "Оценка трансляции от 1 до 10? 🔟", "Что разочаровало в показанном? 😕",
         "Жмите 🔥, если всё понравилось!", "Чего не хватило в этой трансляции? 🤔",
         "Угадали какие-нибудь новинки заранее? 🕵️", "Что попробуете первым делом? 🎮",
         "Какая новинка кажется самой сильной? 💪"],
        ["Будете смотреть? 📺", "Что надеетесь увидеть в трансляции? 🔥",
         "Какую новинку ждёте больше всего? 🎯", "Жмите 🔥, если будете смотреть!",
         "Что хотите увидеть в Brawl Talk? 💡"]),
    "skin": _bank(
        ["Какой скин вам понравился больше всех? 🎨", "Купили бы такой скин? 🛒",
         "Скин огонь или так себе? 🔥", "На каком бойце он смотрелся бы лучше всего? 🤔",
         "Какой скин хочется увидеть следующим? 💡", "Что скажете про дизайн? 👀",
         "Жмите 😍, если хотите этот скин!", "Сколько скинов в вашей коллекции? Хвастайтесь! 🎽",
         "Кто собирает скины? Покажите коллекцию! 🖼", "Ваш любимый скин на сегодня? ❤️",
         "Этот скин лучше предыдущих или хуже? ⚖️", "Хотите такой на своего основного бойца? 🙏"]),
    "leak": _bank(
        ["Верите в слив? 🕵️", "Это правда или фейк? 🤔", "Хотели бы, чтобы это оказалось правдой? 🙏",
         "Как вам утечка? 👀", "Насколько вы доверяете сливам? 🧐",
         "Что в утечке цепляет больше всего? 🔍", "Жмите 🔥, если верите, что так и будет!",
         "Жмите 🤨, если сомневаетесь.", "Если это правда, вы довольны? 😮",
         "Что ещё хотели бы увидеть в утечках? 💡", "Ждём подтверждения. Кто со мной? ⏳",
         "Выглядит правдоподобно? 🧩"]),
    "maintenance": _bank(
        ["Как думаете, что будет после работ? 🤔", "Надеемся, после работ будет что-то интересное! 🤞",
         "Жмите ⏳, если ждёте вместе с нами!", "Чем занимаетесь, пока ждём? ☕",
         "Кто ждёт возвращения игры? 🙋", "Что сделаете первым делом, когда игра вернётся? 🚀"],
        ["Чем займётесь, пока идут работы? ⏳", "Кто уже обновляет страницу каждые пять минут? 😅"],
        ["Успеете поиграть до перерыва? 🎮", "Что успеете сделать до работ? ⏰",
         "Чем займётесь во время работ? ☕"]),
    "event": _bank(
        ["Как вам идея нового события? 🤔", "Что планируете забрать? 🎁",
         "Обещает быть жарко. Согласны? 🔥", "Жмите 🔥, если любите ивенты!",
         "Какое событие в игре было вашим любимым? 🏅", "Какая награда самая желанная? 🏅",
         "Хватит ли вам времени на всё? ⏰"],
        ["Успели поучаствовать? 🎉", "Кто уже в деле? 🙋", "Какое задание самое сложное? 😅",
         "Кто пройдёт всё до конца? 💪", "Жмите 🔥, если уже играете!",
         "Что из этого зацепило больше всего? 🎯"],
        ["Что планируете делать в первую очередь? 🚀", "Что ждёте больше всего? 🔥",
         "Готовы к новому событию? 🙌", "Кто будет участвовать? 🙋"]),
    "video": _bank(
        ["Жмите 🔥, если любите такие видео!", "Что бы вы хотели видеть в таких видео? 💡",
         "Любите такие ролики? 🎬", "Что вам больше нравится смотреть у Brawl Stars? 🍿"],
        ["Как вам ролик? 🎬", "Посмотрели? Какие впечатления? 🍿", "Что запомнилось больше всего? 👀",
         "Жмите 🔥, если понравилось!", "Оценка ролику от 1 до 10? 🔟",
         "Какой момент зацепил сильнее всего? 🎯", "Пересмотрели бы ещё раз? 🔁",
         "Это круто или могло быть лучше? 🤔", "Кто досмотрел до конца? 🙋",
         "Какая деталь вас удивила? 😮"],
        ["Будете смотреть? 📺", "Что надеетесь увидеть? 🍿", "Жмите 🔥, если ждёте!",
         "Готовы к премьере? 🎬"]),
    "meme": _bank(
        ["Смешно или не очень? 😄", "Кому это знакомо? 🙋", "Это про вас? 😅",
         "Оцените по шкале «хаха»! 😂", "Кто так же? Жмите 😂", "Талант или случайность? 🎨",
         "Кому бы вы отправили этот пост? 📨", "Автору респект! 👏", "Жмите 🔥, если понравилось!",
         "Получилось или мимо? 🤔", "Отметьте друга, который так играет! 😎",
         "Это шедевр? 🖼", "Согласны или это перебор? 😏"]),
    "opinion": _bank(
        ["Согласны? 🤔", "У вас так же? 🙋", "Что бы вы предложили? 💡",
         "Как вы к этому относитесь? 🧐", "Ваше мнение? 🗣", "Сталкивались с таким? 👀",
         "Что, по-вашему, нужно изменить? ✍️", "Кто ещё так считает? ✋"]),
    "serious": _bank(
        ["Как вы к этому относитесь?", "Что вы об этом думаете?", "Каково ваше мнение?",
         "Что скажете об этой ситуации?", "Как считаете, как это повлияет на игру?",
         "Поделитесь мнением.", "Как бы вы поступили на месте разработчиков?"]),
    "generic": _bank(
        ["Что думаете? 🤔", "А вы что скажете? 💬", "Что вы об этом думаете? 👀",
         "Поделитесь мнением! 🗣", "Что скажете? 👇", "Ваше мнение? 💭",
         "Жмите 🔥, если вам понравилось!", "Жмите 🤔, если пока не определились.",
         "Оцените от 1 до 10! 🔟", "Что в этом самое интересное, по-вашему? 💡"]),
}
PHRASE_MEMORY = 40  # столько последних фраз не повторяется


def phrase_group(item, important=True):
    topic = item.get("topic")
    if topic and "обновление" in topic[2]:
        return "appupdate"
    title, text = item.get("title", ""), item.get("text", "")
    blob = f"{title} {text}"
    if SERIOUS_RE.search(blob):
        return "serious"
    if not important and COMPLAINT_RE.search(blob):
        return "opinion"
    best, best_score = "generic", 0
    for group, pat in PHRASE_RULES:
        score = 3 * len(pat.findall(title)) + len(pat.findall(text))
        if score > best_score:  # при равенстве остаётся группа выше по списку
            best, best_score = group, score
    return best if best_score >= 2 else "generic"


def tense_of(item):
    """'now' (уже вышло или произошло), 'soon' (скоро) или '' (не уверены)."""
    for blob in (item.get("title", ""), item.get("text", "")):
        soon, now = bool(SOON_RE.search(blob)), bool(NOW_RE.search(blob))
        if soon != now:
            return "soon" if soon else "now"
        if soon and now:
            return ""  # противоречие: лучше не гадать
    if canon(item.get("link", "")).startswith("yt:"):
        return "now"  # ролик на YouTube уже опубликован
    return ""


def phrase_pool(item, important=True):
    bank = PHRASES[phrase_group(item, important)]
    pool = list(bank["any"])
    t = tense_of(item)
    if t:
        pool += bank[t]
    return pool


def pick_phrase(item, used, important=True, rng=random):
    """Фраза по смыслу и времени поста; недавно использованные не повторяются."""
    pool = phrase_pool(item, important)
    fresh = [p for p in pool if p not in used] or pool
    return rng.choice(fresh)


def link_label(link):
    k = canon(link)
    if k.startswith("yt:"):
        return "▶️ Смотреть видео"
    if k.startswith("reddit:"):
        return "💬 Обсудить на Reddit"
    return "🔗 Читать подробнее"


def build_post(item, title, text, important, phrase=""):
    """Возвращает HTML для Telegram. Видимая длина не больше MAX_CAPTION символов."""
    esc = html.escape
    emoji, label, tag = item_topic(item)
    title = decorate(title[:500], 1, skip=(emoji,))
    text = decorate(text, 2, skip=(emoji,))
    link = item.get("link", "") if (important or item.get("force_link")) else ""
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
    if phrase:
        blocks.append((phrase, f"<i>{esc(phrase)}</i>"))
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


def send(item, important, phrase=""):
    title = item["title"] if item.get("title_ru") else translate(item["title"])
    text = translate(item.get("text", ""))
    post = build_post(item, title, text, important, phrase)
    img = item.get("img", "")
    if img:
        try:
            tg_json("sendPhoto", {"chat_id": CHAT, "photo": img, "caption": post,
                                  "parse_mode": "HTML"})
            return
        except FatalError:
            raise
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                raise
        except Exception:
            pass
        try:  # Telegram не смог сам скачать картинку: качаем и загружаем сами
            tg_upload(download(img), post)
            return
        except FatalError:
            raise
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                raise
        except Exception:
            pass
    if not important and not item.get("force_link"):
        raise ValueError("не получилось отправить картинку")
    tg_json("sendMessage", {"chat_id": CHAT, "text": post, "parse_mode": "HTML"})


# ---------- состояние ----------
def load_state():
    state = {"seen": [], "sources": [], "daily": {}, "yt": {}, "recent": [], "fails": {}, "phrases": []}
    if os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE))
            if isinstance(data, list):  # старый формат
                state["seen"] = data
            elif isinstance(data, dict):
                state.update(data)
        except Exception as ex:
            print("seen.json повреждён, начинаю с чистого листа:", ex)
    return state


def save_state(state):
    state["seen"] = state["seen"][-SEEN_KEEP:]
    state["phrases"] = state["phrases"][-PHRASE_MEMORY:]
    cutoff = time.time() - RECENT_DAYS * 86400
    state["recent"] = [r for r in state["recent"] if r.get("ts", 0) > cutoff][-RECENT_KEEP:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)  # файл не повредится, даже если запуск оборвётся


def main():
    started = time.time()
    if not TOKEN or not CHAT:
        print("ОШИБКА: не заданы секреты BOT_TOKEN и/или CHAT_ID "
              "(Settings → Secrets and variables → Actions).")
        sys.exit(1)
    try:
        check_telegram()
    except FatalError as ex:
        print("ОШИБКА:", ex)
        sys.exit(1)

    state = load_state()
    seen = set(state["seen"])
    recent = state["recent"]
    fails = state["fails"]
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "regular": 0}

    def mark(item_id):
        fails.pop(item_id, None)
        if item_id not in seen:
            seen.add(item_id)
            state["seen"].append(item_id)

    def bump(item_id, limit):
        fails[item_id] = fails.get(item_id, 0) + 1
        if fails[item_id] >= limit:
            print(f"  отказываюсь от записи после {limit} попыток:", item_id)
            mark(item_id)

    def remember(item, important):
        recent.append({"k": sorted(keys_of(item)), "t": sorted(tokens_of(item.get("title", ""))),
                       "ts": time.time(), "imp": bool(important)})

    results = fetch_all(SOURCES, state)
    queue = []
    for src in SOURCES:
        key = src["name"]
        items = results.get(key)
        if items is None:
            continue
        print(f"источник {key}: {len(items)} записей")
        if not items:
            continue
        if key not in state["sources"]:
            # новый источник: запоминаем всё, что уже есть, чтобы не завалить канал
            for it in items:
                mark(it["id"])
                remember(it, src["important"])
            state["sources"].append(key)
            print("  источник добавлен, старые записи пропущены")
            continue
        for it in items:
            if it["id"] not in seen:
                queue.append((it, src["important"], key))

    queue.sort(key=lambda x: not x[1])  # важное первым
    sent = 0
    for it, important, key in queue:
        if sent >= MAX_PER_RUN:
            break
        if time.time() - started > TIME_BUDGET:
            print("время запуска вышло, остальное в следующий раз")
            break
        if not important and state["daily"]["regular"] >= DAILY_REGULAR_LIMIT:
            mark(it["id"])  # лимит на сегодня, старое не копим
            continue
        try:
            if it.get("lazy"):
                try:
                    fill_supercell(it)
                except SkipItem as ex:
                    print("  пропускаю статью:", it["link"], "-", ex)
                    mark(it["id"])
                    continue
                except Exception as ex:
                    print("  не удалось загрузить статью:", it["link"], "-", ex)
                    bump(it["id"], FILL_FAILS)
                    continue
            if not important and not it.get("img") and not it.get("force_link"):
                mark(it["id"])
                continue
            if is_dup(keys_of(it), tokens_of(it.get("title", "")), important, recent):
                print("  дубль, не публикую:", it.get("title", it["id"]))
                mark(it["id"])
                continue
            phrase = pick_phrase(it, state["phrases"], important)
            send(it, important, phrase)
        except FatalError as ex:
            print("ОШИБКА:", ex)
            save_state(state)
            sys.exit(1)
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                print("Telegram просит подождать, остановка до следующего запуска")
                break
            print("  не отправлено, повторю позже:", key, "-", ex)
            bump(it["id"], MAX_FAILS)
            continue
        except Exception as ex:
            print("  не отправлено, повторю позже:", key, "-", ex)
            bump(it["id"], MAX_FAILS)
            continue
        mark(it["id"])
        remember(it, important)
        state["phrases"].append(phrase)
        sent += 1
        if not important:
            state["daily"]["regular"] += 1
        save_state(state)  # сразу после каждой отправки
        time.sleep(PAUSE)

    save_state(state)
    print(f"готово: опубликовано {sent}")


if __name__ == "__main__":
    main()
