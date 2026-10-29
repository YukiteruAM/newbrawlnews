"""Бот новостей Brawl Stars: собирает новости из первоисточников,
переводит на русский и публикует в Telegram-канал. Дубли отсекаются."""
import os, sys, re, io, json, html, time, uuid, datetime, random, math, struct
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")  # @имя_канала или -100...

# ===================== ЛИМИТЫ И НАСТРОЙКИ =====================
# Публикации
MAX_PER_RUN = 3               # максимум постов за один запуск (раз в 10 минут)
DAILY_REGULAR_LIMIT = 8       # обычные посты сообщества в сутки
DAILY_IMPORTANT_LIMIT = 25    # страховка от лавины «важных» новостей
DAILY_TOTAL_LIMIT = 40        # всё вместе: новости, сообщество, гайд, мета, баланс
DAILY_GUIDE_LIMIT = 1         # гайд на бойца: один в сутки
QUIET_HOURS_UTC = (21, 4)     # обычные посты не выходят с 00:00 до 07:00 по Москве (важные выходят всегда)
PAUSE = 3                     # пауза между постами, сек
# Время и повторы
TIME_BUDGET = 300             # через сколько секунд запуск перестаёт брать новую работу
MAX_SEND_ERRORS_PER_RUN = 6   # столько ошибок отправки подряд, и запуск останавливается
MAX_FAILS = 36                # сколько раз пробовать отправить запись (36 запусков ~ 6 часов)
FILL_FAILS = 5                # сколько раз пробовать загрузить статью Supercell
# Картинки
IMG_MAX_BYTES = 10 * 1024 * 1024   # лимит Telegram на фото
IMG_MIN_SIDE_REGULAR = 480         # у постов сообщества короткая сторона не меньше (иначе картинка мыльная)
IMG_MIN_PIXELS_REGULAR = 300_000
IMG_MIN_SIDE_IMPORTANT = 300       # у официальных постов планка ниже; если не проходит, пост уходит без картинки
IMG_MAX_SUM_SIDES = 9000           # Telegram: ширина плюс высота не больше 10000
IMG_MAX_RATIO = 15                 # слишком вытянутые картинки Telegram не принимает
PAGE_MAX_BYTES = 6 * 1024 * 1024   # больше этого с сайтов не читаем
# Текст
TITLE_MAX = 220
# Состояние
STATE_FILE = "seen.json"
SEEN_KEEP = 1000
RECENT_KEEP = 200             # сколько последних новостей помнить для поиска дублей
RECENT_DAYS = 10
TITLE_DUP_DAYS = 3            # схожие заголовки считаем дублем только в пределах этих дней
GUIDED_KEEP = 40              # сколько бойцов помнить, чтобы не повторять гайд
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
    {"name": "reddit-top", "type": "feed", "important": False, "community": True,
     "url": "https://www.reddit.com/r/Brawlstars/top/.rss?t=day"},
    # Слухи и утечки на r/Brawlstars (всегда с пометкой «неподтверждённая информация»)
    {"name": "reddit-leaks", "type": "feed", "important": False, "community": True,
     "url": "https://www.reddit.com/r/Brawlstars/search.rss?q=title%3Aleak&restrict_sr=on&sort=new&t=week"},
]

# слова, которые остаются на английском (добавляй свои, например имена бойцов)
KEEP = ["Brawl Stars", "Brawl Pass", "Brawl Talk", "Starr Drop", "Supercell Make", "Supercell ID",
        "Supercell Store", "Supercell", "Clash Royale", "Clash of Clans", "Hay Day", "Boom Beach"]


def _keep_pattern(k):
    # допускаем «BrawlStars» слитно и множественное число («Starr Drops»)
    return re.escape(k).replace(r"\ ", r"\s?") + "s?"


KEEP_RE = re.compile(
    r"\b(" + "|".join(_keep_pattern(k) for k in sorted(KEEP, key=len, reverse=True)) + r")\b",
    re.I)

# Имена бойцов переводчик иногда переводит буквально (Crow -> «Ворона»), поэтому они остаются как есть.
BRAWLERS = [
    "Shelly", "Colt", "Bull", "Brock", "El Primo", "Barley", "Poco", "Rosa", "Jessie", "Dynamike", "Tick",
    "8-Bit", "Rico", "Darryl", "Penny", "Carl", "Jacky", "Gus", "Bo", "Emz", "EMZ", "Stu", "Piper", "Pam",
    "Frank", "Bibi", "BiBi", "Bea", "Nani", "Edgar", "Griff", "Grom", "Bonnie", "Gale", "Colette", "Belle",
    "Ash", "Lola", "Sam", "Mandy", "Maisie", "Hank", "Pearl", "Larry & Lawrie", "Angelo", "Berry", "Shade",
    "Spike", "Crow", "Leon", "Sandy", "Amber", "Meg", "Surge", "Chester", "Cordelius", "Kit", "Draco",
    "Kenji", "Mico", "Mortis", "Tara", "Gene", "Max", "Mr. P", "Sprout", "Byron", "Squeak", "Lou", "Ruffs",
    "Buzz", "Fang", "Eve", "Janet", "Otis", "Buster", "Gray", "R-T", "Willow", "Doug", "Chuck", "Charlie",
    "Melodie", "Lily", "Clancy", "Moe", "Juju", "Ollie", "Lumi", "Finx", "Jae-Yong", "Kaze", "Alli", "Trunk",
    "Mina", "Ziggy", "Gigi", "Glowy", "Najia", "Damian", "Starr Nova", "Bolt", "Sirius", "Pierce", "Meeple",
]


def _names_regex(names):
    alt = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(r"(?<![\w-])(" + alt + r")(?![\w-])")  # с учётом регистра


NAMES_RE = _names_regex(BRAWLERS)
QUOTED_RE = re.compile(r'"[^"\n]{2,40}"|“[^”\n]{2,40}”|«[^»\n]{2,40}»')  # названия гаджетов и сил в кавычках


def nice_name(n):
    s = (n or "").strip().title()
    return {"Bibi": "BiBi"}.get(s, s)


def register_names(names):
    """Добавляет имена бойцов, полученные из API (на случай новых бойцов)."""
    global NAMES_RE
    new = [nice_name(n) for n in names if n]
    fresh = [n for n in new if n.lower() not in {b.lower() for b in BRAWLERS}]
    if fresh:
        BRAWLERS.extend(fresh)
        NAMES_RE = _names_regex(BRAWLERS)


# Посты про «бесплатные гемы», генераторы и взломы почти всегда обман: из обычных источников их не берём.
SCAM_RE = re.compile(
    r"\b(?:free|unlimited)\s+(?:gems?|coins?|brawl pass|skins?)\b|\bgems?\s+(?:generator|hack|glitch|codes?)\b"
    r"|\b(?:hack|hacks|cheats?)\b|\bgenerator\b|\bpromo ?codes?\b|\bgift ?cards?\b|\bgiveaway", re.I)

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CYR = re.compile("[а-яА-ЯёЁ]")


class FatalError(Exception):
    """Ошибка, после которой продолжать нет смысла (неверный токен, нет прав)."""


class SkipItem(Exception):
    """Запись не нужна (например, не про Brawl Stars)."""


class LowQualityImage(Exception):
    """У обычного поста нет картинки нормального качества: лучше не публиковать."""


# ---------- сеть ----------
def get(url, headers=None, tries=2, limit=PAGE_MAX_BYTES):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=20).read(limit)
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (408, 425, 429, 500, 502, 503, 504):
                break  # 403, 404 и т.п.: повторять бессмысленно
            time.sleep(2 * (i + 1))
        except Exception as ex:
            last = ex
            time.sleep(2 * (i + 1))
    raise last


def download(url, limit=IMG_MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=30).read(limit + 1)
    if len(data) > limit:
        raise ValueError("картинка слишком большая")
    return data


def image_info(data):
    """Формат и размер картинки по заголовку файла: (формат, ширина, высота) или None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return "png", w, h
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return "gif", w, h
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return "jpeg", w, h
                i += 2 + seg
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8 " and len(data) >= 30:
                return ("webp", struct.unpack("<H", data[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", data[28:30])[0] & 0x3FFF)
            if kind == b"VP8L" and len(data) >= 25:
                bits = int.from_bytes(data[21:25], "little")
                return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X" and len(data) >= 30:
                return ("webp", int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
    except Exception:
        return None
    return None


def upgrade_image_url(url):
    """Превью Reddit (preview.redd.it?width=640) заменяем оригиналом с i.redd.it."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return url
    if u.netloc.lower() == "preview.redd.it" and u.path:
        return "https://i.redd.it" + u.path
    return url


def expand_candidates(urls):
    """Список адресов картинки от лучшего к худшему; для YouTube сначала максимальное качество."""
    out = []
    for url in urls:
        if not url:
            continue
        m = re.match(r"(https?://i\.ytimg\.com/vi/[\w-]{11}/)\w+\.jpg", url)
        variants = ([m.group(1) + "maxresdefault.jpg", m.group(1) + "sddefault.jpg", url]
                    if m else [upgrade_image_url(url), url])
        for v in variants:
            if v not in out:
                out.append(v)
    return out[:6]


try:  # Pillow нужен только для карточек и уменьшения огромных картинок; без него бот работает
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None


def shrink_image(data, max_side=2560):
    """Слишком большую картинку уменьшает (нужен Pillow), иначе возвращает None."""
    if Image is None:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.convert("RGB").save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception:
        return None


def load_image(item, important):
    """Скачивает картинку и проверяет качество. Возвращает (байты, формат) или None."""
    if item.get("img_bytes"):
        return item["img_bytes"], "jpeg"
    min_side = IMG_MIN_SIDE_IMPORTANT if important else IMG_MIN_SIDE_REGULAR
    min_px = 0 if important else IMG_MIN_PIXELS_REGULAR
    cands = item.get("imgs") or expand_candidates([item.get("img", "")])
    for url in cands[:6]:
        try:
            data = download(url, IMG_MAX_BYTES)
        except Exception:
            continue
        info = image_info(data)
        if not info:
            continue
        fmt, w, h = info
        if fmt == "gif" or min(w, h) < min_side or w * h < min_px:
            continue  # анимация или низкое качество: берём следующий вариант
        if w + h > IMG_MAX_SUM_SIDES or max(w, h) / max(1, min(w, h)) > IMG_MAX_RATIO:
            data = shrink_image(data)
            if not data or (image_info(data) or ("", 0, 0))[1] + (image_info(data) or ("", 0, 0))[2] > IMG_MAX_SUM_SIDES:
                continue
            fmt = "jpeg"
        return data, fmt
    return None


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


# ---------- исправление типичных ошибок перевода ----------
# Переводчик путает игровые слова: «brawler» превращает в «скандалиста», «trophies» в «трофеи».
# Меняем их на официальные термины игры с сохранением падежа.
_BOEC = {"": "боец", "а": "бойца", "у": "бойцу", "ом": "бойцом", "е": "бойце",
         "ы": "бойцы", "ов": "бойцов", "ам": "бойцам", "ами": "бойцами", "ах": "бойцах"}
_KUBOK = {"й": "кубок", "я": "кубка", "ю": "кубку", "ем": "кубком", "е": "кубке",
          "и": "кубки", "ев": "кубков", "ям": "кубкам", "ями": "кубками", "ях": "кубках"}
_ZABIYAKA = {"а": "боец", "и": "бойцы", "е": "бойце", "у": "бойца", "ой": "бойцом",
             "": "бойцов", "ам": "бойцам", "ами": "бойцами", "ах": "бойцах"}


def _same_case(src, word):
    return word[:1].upper() + word[1:] if src[:1].isupper() else word


def fix_ru(text):
    text = re.sub(r"\b(скандалист|драчун|бравлер)(а|у|ом|е|ы|ов|ам|ами|ах)?\b",
                  lambda m: _same_case(m.group(0), _BOEC[m.group(2) or ""]), text, flags=re.I)
    text = re.sub(r"\bзабияк(а|и|е|у|ой|ам|ами|ах)?\b",
                  lambda m: _same_case(m.group(0), _ZABIYAKA[m.group(1) or ""]), text, flags=re.I)
    text = re.sub(r"\bтрофе(й|я|ю|ем|е|и|ев|ям|ями|ях)\b",
                  lambda m: _same_case(m.group(0), _KUBOK[m.group(1)]), text, flags=re.I)
    text = re.sub(r"\b(очк\w+)\s+мощности\b", r"\1 силы", text, flags=re.I)
    return text


def translate(text, mask_quotes=False):
    if not text:
        return ""
    if mostly_russian(text):
        return text
    kept = []

    def mask(m):
        kept.append(m.group(0))
        return f"ZZ{len(kept) - 1}ZZ"

    masked = text
    if mask_quotes:
        masked = QUOTED_RE.sub(mask, masked)
    masked = KEEP_RE.sub(mask, masked)
    masked = NAMES_RE.sub(mask, masked)
    last = None
    for engine in ENGINES:
        try:
            return fix_ru(_validate(masked, kept, engine(masked)))
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
    """Все найденные адреса картинки от лучшего к худшему (оригинал, превью, миниатюра)."""
    body = html.unescape(body or "")
    found = re.findall(r'https://i\.redd\.it/[^"\s<>]+\.(?:jpe?g|png|webp)', body, re.I)
    found += re.findall(r'https://preview\.redd\.it/[^"\s<>]+', body)
    for enc in e.findall("enclosure"):
        if (enc.get("type") or "").startswith("image") and enc.get("url"):
            found.append(enc.get("url"))
    t = e.find(".//" + MEDIA + "thumbnail")
    if t is not None and t.get("url"):
        found.append(html.unescape(t.get("url")))
    found += [html.unescape(x) for x in re.findall(r'<img[^>]+src="([^"]+)"', body)]
    return expand_candidates(found)


def entry_author(e):
    n = e.find(ATOM + "author/" + ATOM + "name")
    name = (n.text or "").strip() if n is not None else ""
    return name.lstrip("/") if name else ""


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
        imgs = pick_image(e, body)
        items.append({
            "id": item_id,
            "title": title,
            "link": link,
            "text": shorten(clean(body)),
            "img": imgs[0] if imgs else "",
            "imgs": imgs,
            "urls": urls,
            "author": entry_author(e),
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
    if item.get("kind") == "blog" and re.search(r"release notes|balance|maintenance|patch", f"{title} {desc}", re.I):
        item["_page"] = page  # для разбора баланса
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
    if src.get("community"):
        for it in items:
            it["community"] = True
            it["sub"] = "r/Brawlstars"
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


_MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def tg_upload(photo, caption, fmt="jpeg"):
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", CHAT), ("caption", caption), ("parse_mode", "HTML")):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    ext = "jpg" if fmt == "jpeg" else fmt
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="p.{ext}"\r\n'
                f'Content-Type: {_MIME.get(fmt, "image/jpeg")}\r\n\r\n').encode())
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
    (r"\bleaks?\b|\bleaked\b|\brumou?rs?\b", "👀", "Утечки и слухи", "утечки"),
    (r"\bsneak peek\b|\bteaser", "👀", "Анонс", "анонс"),
    (r"\brelease notes\b|\bpatch\b|\bhotfix\b|\bchangelog\b|\bbalance changes?\b|\bversion\b|\bupdate", "🛠", "Обновление", "обновление"),
    (r"\bworld finals\b|\bchampionship|\bbsc\b|\btournament|\bqualifier|\bfinals\b|\besports\b|\bchampions\b", "🏆", "Киберспорт", "киберспорт"),
    (r"\bcrossover|\bcollaboration|\bcollab\b|\bpartnership|\btakeover\b|\bx\b", "🤝", "Коллаборация", "коллаборация"),
    (r"\bnew brawler|\bintroducing\b|\bbrawlers? (?:is |are )?(?:coming|joining|arrives?|reveal)", "🦸", "Новый боец", "новыйбоец"),
    (r"\bbrawl talk\b", "🎤", "Brawl Talk", "BrawlTalk"),
    (r"\bskins?\b|\bcosmetic|\bbling\b", "🎨", "Скины", "скины"),
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
LEAK_RE = re.compile(r"\bleaks?\b|\bleaked\b|\brumou?rs?\b|\bdata ?mine", re.I)
MAX_CAPTION = 980  # лимит Telegram 1024, берём с запасом на эмодзи


ART_RE = re.compile(
    r"\bfan ?art\b|\bart\b|\bdrawing|\bdrew\b|\bsketch|\bconcept\b|\bmake entry\b|\bsupercell make\b"
    r"|\bi made\b|\bi created\b|\bredesign|\bfan[- ]?made\b|\bcosplay\b", re.I)
MEME_RE = re.compile(r"\bmemes?\b|\bfunny\b|\bme when\b|\blol\b", re.I)
QUESTION_RE = re.compile(
    r"\?\s*$|^(?:what|which|who|whose|how|why|where|when|do you|does|did|is|are|would|should|can|could)\b",
    re.I)


def is_question(title):
    return bool(QUESTION_RE.search((title or "").strip()))


def item_topic(item, important=True):
    if item.get("topic"):
        return item["topic"]
    title = item.get("title", "")
    blob = f"{title} {item.get('text', '')}"
    for pat, emoji, label, tag in TOPICS:
        if pat.search(blob):
            return emoji, label, tag
    if important:
        return DEFAULT_TOPIC
    # обычные посты сообщества: не «новости»
    if MEME_RE.search(blob):
        return "😂", "Мемы", "мемы"
    if ART_RE.search(blob):
        return "🎨", "Фан-арт", "фанарт"
    if is_question(title):
        return "💬", "Обсуждение", "обсуждение"
    return "⭐", "Сообщество", "сообщество"


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
    ("fanart", ART_RE.pattern),
    ("meme", MEME_RE.pattern),
    ("share", r"\bi (?:got|finally|just|made|built|unlocked|pulled|bought|won|hit|reached|maxed|found)\b"
              r"|\bmy (?:first|new|favou?rite|collection|account|brawler)\b|\bfinally\b|\bhonestly\b"),
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
         "Оцените по шкале «хаха»! 😂", "Кто так же? Жмите 😂",
         "Кому бы вы отправили этот пост? 📨", "Жмите 🔥, если понравилось!",
         "Отметьте друга, который так играет! 😎", "Согласны или это перебор? 😏",
         "Каждый хоть раз попадал в такую ситуацию 😅", "Где-то это уже было с вами? 🙈", "Оценка юмора по шкале от 1 до 10? 🔟", "Смех сквозь слёзы или просто смешно? 😂", "Показать другу — святое дело 😎"]),
    "fanart": _bank(
        ["Оцените арт! 🎨", "Получилось классно, согласны? 🔥", "Автору респект! 👏",
         "Хотели бы такое в игре? 🎮", "Похоже на официальный дизайн? 🤔",
         "Что бы вы добавили в этот дизайн? 💡", "Жмите 🔥, если автор молодец!",
         "Кому бы вы показали этот арт? 📨", "Нравится стиль? 🖌", "Талант! Согласны? ⭐",
         "Что скажете про детали? 🔍",
         "Хочется такой скин в игре? 🎮", "Как вам цветовая гамма? 🎨", "Что бы вы изменили в этом рисунке? ✏️", "Автор постарался, согласны? 🙌"]),
    "question": _bank(
        ["Ваш ответ? 👇", "Что ответите? 👀", "Расскажите, что думаете! 🗣",
         "Поделитесь своим вариантом! 💬", "А как у вас? 🤔", "Интересно узнать ваше мнение! 💭",
         "Есть что добавить? ✍️",
         "Какой у вас ответ? 🎯", "Голосуйте реакцией! 👇", "А что выберете вы? ⚖️", "Делитесь опытом! 🧠", "Пишите свой вариант! ✍️"]),
    "share": _bank(
        ["Поздравим автора? 🎉", "Что скажете об этой находке? 👀", "Кто ещё так делал? 🙋",
         "Красиво получилось! Согласны? 😍", "Как вам такое достижение? 🏅", "Хотели бы так же? 🙏",
         "У вас бывало что-то подобное? 🤔", "Автору респект! 👏", "Жмите 🔥, если порадовались за автора!",
         "Расскажите о своих находках! 💬"]),
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
         "Поделитесь мнением! 🗣", "Ваше мнение? 💭", "Что скажете? 👇",
         "Жмите 🔥, если вам понравилось!", "Как вам такое? 🤔",
         "Что скажете? 💭", "Ваши мысли? 👇", "Интересно, что вы об этом думаете! 💬", "Делитесь мнением, нам важно! 🗣", "А вам как? 🤷", "Мнения разделились? Пишите! ⚖️", "Жмите 👍, если за!", "Жмите 😮, если не ожидали!", "Есть мнение? Делитесь! 🎤", "Кто что думает? 🙋", "Ждём ваших мыслей! ✍️", "Что тут самое интересное, по-вашему? 💡"]),
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
    group = best if best_score >= 2 else "generic"
    if group == "generic" and not important and is_question(title):
        return "question"  # вопрос из сообщества: просим ответить, а не оценивать
    return group


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


# ---------- «человеческий» текст через языковую модель (необязательно) ----------
# Подходит любой сервис с OpenAI-совместимым API. По умолчанию Gemini (есть бесплатный тариф).
# Без ключа бот использует обычный переводчик. Любой сбой модели тоже ведёт к переводчику.
LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE = (os.environ.get("LLM_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "gemini-2.5-flash"
LLM_DAILY_LIMIT = 60      # запросов к модели в сутки
LLM_TIMEOUT = 45

LLM_SYSTEM = """Ты редактор русскоязычного Telegram-канала о Brawl Stars. Тебе дают заголовок и текст записи на английском. \
Перепиши их по-русски живым естественным языком, как в хорошем игровом канале: без канцелярита и без дословного перевода.
Правила:
1. Не выдумывай факты и ничего не добавляй от себя. Все числа, даты и названия сохраняй как есть.
2. Имена бойцов, названия режимов, гаджетов и звёздных сил, а также Brawl Stars, Supercell, Brawl Pass, Starr Drop оставляй латиницей, как в оригинале.
3. Игровые термины по-русски: боец (не «бравлер»), кубки, гаджет, звёздная сила, гиперзаряд, снаряжение, мегаящик.
4. Ты пишешь от лица канала, а не от лица автора поста. Если исходный текст от первого лица, перескажи его в третьем лице («автор показывает…», «игрок делится…») и не используй «я», «мне», «мы».
5. Заголовок до 90 символов, без точки в конце, без эмодзи. Текст: одно-два предложения, до 250 символов; если исходного текста нет, верни пустую строку.
6. Поле phrase: одна короткая фраза-вопрос или реакция для читателей (до 60 символов) по смыслу поста, дружелюбно, без кликбейта, можно одно эмодзи в конце. К грустным и спорным новостям фраза спокойная, без шуток.
7. Ответ строго JSON без пояснений: {"title": "...", "text": "...", "phrase": "..."}"""


def llm_call(user_payload):
    body = json.dumps({
        "model": LLM_MODEL, "temperature": 0.5,
        "messages": [{"role": "system", "content": LLM_SYSTEM},
                     {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
    }).encode()
    last = None
    for i in range(2):
        try:
            req = urllib.request.Request(LLM_BASE + "/chat/completions", data=body, headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
            data = json.loads(urllib.request.urlopen(req, timeout=LLM_TIMEOUT).read())
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (408, 429, 500, 502, 503, 504):
                break
            time.sleep(3 * (i + 1))
        except Exception as ex:
            last = ex
            time.sleep(3)
    raise last


def parse_llm_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("в ответе модели нет JSON")
    return json.loads(raw[i:j + 1])


_FIRST_PERSON = re.compile(r"\b(я|мне|меня|мой|моя|моё|мои|мною|мы|нас|нам|наш\w*)\b", re.I)


def llm_valid(item, out):
    """Строгая проверка ответа модели. Любое сомнение: используем обычный перевод."""
    title, text, phrase = (str(out.get(k) or "").strip() for k in ("title", "text", "phrase"))
    if not title or len(title) > 160 or len(text) > 420:
        return None
    blob = f"{title} {text}"
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", blob)
    if not letters or len(CYR.findall(blob)) / len(letters) < 0.4:
        return None
    if re.search(r"https?://|<[^>]+>|\*\*|```", blob):
        return None
    src_title, src_text = item.get("title", ""), item.get("text", "")
    for num in set(re.findall(r"\d{2,}", f"{src_title} {src_text}")):
        if num not in blob:
            return None  # потеряли число: так нельзя
    for m in list(KEEP_RE.finditer(src_title)) + list(NAMES_RE.finditer(src_title)):
        if m.group(0).lower() not in blob.lower():
            return None  # потеряли название из заголовка
    if item.get("community") and _FIRST_PERSON.search(blob):
        return None  # получился текст от первого лица
    if not (CYR.search(phrase) and len(phrase) <= 80 and not re.search(r"https?://|<", phrase)):
        phrase = ""
    return {"title": title.rstrip(". "), "text": text, "phrase": phrase}


def llm_polish(item, important, state):
    """Возвращает {"title", "text", "phrase"} или None, если модели нет, лимит исчерпан или ответ не прошёл проверку."""
    if not LLM_KEY or item.get("title_ru"):
        return None
    daily = state["daily"]
    if daily.get("llm", 0) >= LLM_DAILY_LIMIT:
        return None
    daily["llm"] = daily.get("llm", 0) + 1
    try:
        raw = llm_call({"тип": "пост игрока с Reddit" if item.get("community") else "официальная новость",
                        "заголовок": item.get("title", "")[:300], "текст": item.get("text", "")[:700]})
        return llm_valid(item, parse_llm_json(raw))
    except Exception as ex:
        print("  языковая модель не помогла, использую переводчик:", ex)
        return None


def link_label(link):
    k = canon(link)
    if k.startswith("yt:"):
        return "▶️ Смотреть видео"
    if k.startswith("reddit:"):
        return "💬 Обсудить на Reddit"
    return "🔗 Читать подробнее"


def build_post(item, title, text, important, phrase=""):
    """Возвращает HTML для Telegram. Видимая длина не больше MAX_CAPTION символов.
    Посты игроков: помета «Из сообщества», слова автора в цитате и ссылка на автора, чтобы не выглядело,
    будто это пишет канал."""
    esc = html.escape
    emoji, label, tag = item_topic(item, important)
    community = bool(item.get("community")) and not important
    rewritten = bool(item.get("_ru"))
    if not community:
        title = decorate(title[:500], 1, skip=(emoji,))
        text = decorate(text, 2, skip=(emoji,))
    title = title[:TITLE_MAX]
    link = item.get("link", "") if (important or item.get("force_link")) else ""
    tags = f"#BrawlStars #{tag}"

    blocks = []  # (видимый текст, html)
    if important:
        head = f"🔥 ВАЖНО · {emoji} {label}"
        blocks.append((head, f"<b>🔥 ВАЖНО</b> · {esc(emoji + ' ' + label)}"))
        blocks.append((title, f"<b>{esc(title)}</b>"))
    elif community:
        head = f"{emoji} Из сообщества · {label}"
        blocks.append((head, f"<b>{esc(head)}</b>"))
        blocks.append((title, f"<b>{esc(title)}</b>"))
    else:
        t = f"{emoji} {title}"
        blocks.append((t, f"<b>{esc(t)}</b>"))
    body_at = len(blocks)
    if community:
        who = f"Автор: {'u/' + item['author'].replace('u/', '') if item.get('author') else 'игрок'} · {item.get('sub', 'r/Brawlstars')}"
        blocks.append((who, f"<i>{esc(who)}</i>"))
    if phrase:
        blocks.append((phrase, f"<i>{esc(phrase)}</i>"))
    if link:
        lab = link_label(link)
        blocks.append((lab, f'<a href="{esc(link, quote=True)}">{esc(lab)}</a>'))
    if LEAK_RE.search(f"{item.get('title', '')} {item.get('text', '')}"):
        blocks.append(("⚠️ Неподтверждённая информация", "<i>⚠️ Неподтверждённая информация</i>"))
    blocks.append((tags, esc(tags)))

    used = len("\n\n".join(b[0] for b in blocks))
    room = MAX_CAPTION - used - 2
    if text:
        if len(text) > room:
            text = text[:room - 1].rstrip() + "…" if room > 40 else ""
        if text:
            quote = community and not rewritten  # дословный текст игрока оформляем цитатой
            html_text = f"<blockquote>{esc(text)}</blockquote>" if quote else esc(text)
            blocks.insert(body_at, (text, html_text))
    return "\n\n".join(b[1] for b in blocks)


def send(item, important, phrase=""):
    """Готовит текст (языковая модель или переводчик), проверяет картинку и отправляет пост."""
    ru = item.get("_ru")
    if ru:
        title, text = ru["title"], ru["text"]
    else:
        title = item["title"] if item.get("title_ru") else translate(item["title"])
        text = translate(item.get("text", ""))
    post = build_post(item, title, text, important, phrase)
    img = load_image(item, important)
    if img:
        data, fmt = img
        try:
            tg_upload(data, post, fmt)
            return
        except FatalError:
            raise
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                raise
        except Exception:
            pass
    text_only_ok = important or item.get("force_link")
    if not text_only_ok:
        raise LowQualityImage("нет картинки нужного качества или Telegram её не принял")
    tg_json("sendMessage", {"chat_id": CHAT, "text": post, "parse_mode": "HTML"})


# ---------- разбор баланса из заметок об обновлении ----------
class _Lines(HTMLParser):
    """Превращает HTML статьи в строки; заголовки помечаются «## », выделение звёздочками."""
    BLOCKS = {"p", "div", "ul", "ol", "br", "tr", "section", "article", "blockquote", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif self.skip:
            return
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n## ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCKS:
            self.out.append("\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCKS or tag == "li":
            self.out.append("\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_lines(page):
    parser = _Lines()
    parser.feed(page)
    parser.close()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in "".join(parser.out).split("\n")]
    return [ln for ln in lines if ln]


def parse_balance(page):
    """Достаёт из заметок об обновлении список бойцов с изменениями.
    Возвращает [{"name", "kind": buff|nerf|"", "changes": [...], "why": "..."}]."""
    known = {n.lower() for n in BRAWLERS}
    entries, cur, in_bal, sect_kind = [], None, False, ""
    for ln in html_to_lines(page):
        if ln.startswith("## "):
            h = ln[3:].replace("*", "").strip().lower()
            sect_kind = "buff" if re.match(r"\W*buffs?\b", h) else "nerf" if re.match(r"\W*nerfs?\b", h) else ""
            in_bal = bool(re.search(r"balance|buff|nerf|maintenance|changes", h)) and not re.search(
                r"bug|fix|map|mode|environment|ranked|skin|shop|rework|hypercharge", h)
            cur = None
            continue
        plain = ln.replace("*", "").replace("➡️", "→").strip()
        low = plain.lower().rstrip(": ")
        if low == "balance changes":
            in_bal, cur = True, None
            continue
        if re.match(r"(bug fixes|fixes|brawl arena|nanopower|general fixes)", low) and len(low) < 40:
            in_bal, cur = False, None
            continue
        m = re.fullmatch(r"[-•]?\s*\*{1,3}\s*([^*]{2,30}?)\s*\*{1,3}", ln)
        if m and in_bal and m.group(1).strip().lower() in known:
            cur = {"name": nice_name(m.group(1)), "kind": sect_kind, "changes": [], "why": ""}
            entries.append(cur)
            continue
        if not (cur and in_bal):
            continue
        km = re.match(r"(?:[-•]\s*)?(buff|nerf)\b", low)
        if km and len(low) < 20:
            if cur["changes"] or (cur["kind"] and cur["kind"] != km.group(1)):
                cur = {"name": cur["name"], "kind": km.group(1), "changes": [], "why": ""}
                entries.append(cur)
            else:
                cur["kind"] = km.group(1)
            continue
        wm = re.fullmatch(r"\*([^*].{25,})\*", ln)
        if wm:
            cur["why"] = wm.group(1).strip()
            continue
        text = plain.lstrip("-• ").strip()
        if text and len(text) < 240 and not text.lower().startswith("note"):
            cur["changes"].append(text)
    return [e for e in entries if e["changes"]]


def build_balance_post(item, entries, limit=3900):
    """Сообщение «разбор баланса»: бафф, нерф, прочие изменения, с причинами от разработчиков."""
    def prep(e):
        try:
            ch = translate("; ".join(e["changes"][:3]), mask_quotes=True)
        except Exception:
            return None  # без полного перевода строку не показываем
        why = ""
        if e.get("why"):
            try:
                why = translate(shorten(e["why"], 230))
            except Exception:
                why = ""
        return {**e, "ru": ch.rstrip(". "), "why_ru": why}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = [r for r in pool.map(prep, entries[:20]) if r]
    if not rows:
        return ""
    try:
        title = translate(item.get("title", ""))
    except Exception:
        title = ""
    esc = html.escape
    head = "⚖️ <b>БАЛАНС · разбор изменений</b>" + (f"\n<i>{esc(title)}</i>" if title else "")
    n_buff = sum(r["kind"] == "buff" for r in rows)
    n_nerf = sum(r["kind"] == "nerf" for r in rows)
    foot = f"\n\n🔗 {item['link']}\n\n#BrawlStars #баланс" if item.get("link") else "\n\n#BrawlStars #баланс"
    summary = f"\n\nЗатронуто бойцов: {len({r['name'] for r in rows})}" + \
        (f" · баффы: {n_buff}" if n_buff else "") + (f" · нерфы: {n_nerf}" if n_nerf else "")
    parts, used = [], len(head) + len(foot) + len(summary)
    skipped = 0
    for kind, label in (("buff", "📈 <b>Бафф</b>"), ("nerf", "📉 <b>Нерф</b>"), ("", "🔧 <b>Изменения</b>")):
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        block = [label]
        for r in group:
            line = f"• <b>{esc(r['name'])}</b>: {esc(r['ru'])}."
            if r["why_ru"]:
                line += f"\n  <i>Почему: {esc(r['why_ru'])}</i>"
            if used + len(line) + 40 > limit and r["why_ru"]:  # сначала жертвуем пояснениями
                line = f"• <b>{esc(r['name'])}</b>: {esc(r['ru'])}."
            if used + len(line) + 40 > limit:
                skipped += 1
                continue
            used += len(line) + 1
            block.append(line)
        if len(block) > 1:
            parts.append("\n".join(block))
    if not parts:
        return ""
    extra = f"\n…и ещё изменений: {skipped}" if skipped else ""
    return head + summary + "\n\n" + "\n\n".join(parts) + extra + foot


def post_balance_digest(item):
    page = item.get("_page")
    if not page:
        return False
    entries = parse_balance(page)
    if not entries:
        return False
    text = build_balance_post(item, entries)
    if not text:
        return False
    tg_json("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    return True


# ---------- мета бойцов (официальный API Brawl Stars) ----------
BS_BASE = os.environ.get("BS_API_BASE") or "https://bsproxy.royaleapi.dev/v1"
BS_KEY = os.environ.get("BRAWL_API_KEY", "")
FORCE_META = os.environ.get("FORCE_META", "").strip().lower() in ("1", "true", "yes")
FORCE_GUIDE = os.environ.get("FORCE_GUIDE", "").strip().lower() in ("1", "true", "yes")
META_HOUR_UTC = 7          # публиковать мету не раньше этого часа по UTC (10:00 по Москве)
META_PLAYERS = 200         # сколько лучших игроков мира опрашиваем
META_WINDOW_H = 24         # берём бои за последние сутки
META_MIN_PICKS = 25        # минимум боёв у бойца, чтобы попасть в рейтинг
META_MODE_MIN_BATTLES = 100
META_RETRY_MIN = 60        # при неудаче повторять не чаще, чем раз в столько минут
META_TYPES = {"ranked", "soloRanked", "teamRanked"}

MODES_RU = {"gemGrab": "Захват кристаллов", "brawlBall": "Броулбол", "heist": "Ограбление",
            "hotZone": "Горячая зона", "knockout": "Нокаут", "bounty": "Награда за поимку"}
CLASSES_RU = {"Damage Dealer": "Бойцы урона", "Tank": "Танки", "Support": "Поддержка",
              "Controller": "Контролёры", "Assassin": "Убийцы", "Marksman": "Снайперы",
              "Artillery": "Артиллерия"}


def bs_api(path):
    raw = get(BS_BASE + path, {"Authorization": "Bearer " + BS_KEY, "Accept": "application/json"}, tries=3)
    return json.loads(raw)


def mode_name(m):
    return MODES_RU.get(m) or re.sub(r"(?<!^)(?=[A-Z])", " ", m).title()


def parse_bt(s):
    return datetime.datetime.strptime(s[:15], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)


def aggregate(logs, now, types=META_TYPES):
    """Статистика бойцов по боям 3 на 3. logs: {тег игрока: [бои]}."""
    seen, stats, modes, battles = set(), {}, {}, 0
    for owner, items in logs.items():
        for it in items or []:
            b = it.get("battle") or {}
            if (types is not None and b.get("type") not in types) or b.get("type") in ("friendly", "tournament"):
                continue
            teams = b.get("teams")
            if not (isinstance(teams, list) and len(teams) == 2) or b.get("result") not in ("victory", "defeat"):
                continue
            try:
                when = parse_bt(it["battleTime"])
            except Exception:
                continue
            if (now - when).total_seconds() > META_WINDOW_H * 3600:
                continue
            idx = next((i for i, tm in enumerate(teams) if any(p.get("tag") == owner for p in tm)), None)
            if idx is None:
                continue
            key = it["battleTime"] + "|" + ",".join(sorted(p.get("tag", "") for tm in teams for p in tm))
            if key in seen:
                continue
            seen.add(key)
            winner = idx if b["result"] == "victory" else 1 - idx
            mode = b.get("mode") or (it.get("event") or {}).get("mode") or "unknown"
            md = modes.setdefault(mode, {"n": 0, "b": {}})
            md["n"] += 1
            battles += 1
            for i, tm in enumerate(teams):
                for p in tm:
                    name = (p.get("brawler") or {}).get("name")
                    if not name:
                        continue
                    won = 1 if i == winner else 0
                    for d in (stats, md["b"]):
                        row = d.setdefault(name, [0, 0])
                        row[0] += 1
                        row[1] += won
    return {"battles": battles, "stats": stats, "modes": modes}


def wilson_lb(wins, n, z=1.0):
    if n <= 0:
        return 0.0
    p = wins / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)


def pct(x, digits=1):
    return f"{x * 100:.{digits}f}".replace(".", ",") + "%"


def rank_rows(stats, min_picks):
    rows = [(nice_name(n), pk, w, w / pk, wilson_lb(w, pk)) for n, (pk, w) in stats.items() if pk >= min_picks]
    return sorted(rows, key=lambda r: r[4], reverse=True)


def meta_analysis(agg, rows, prev, classes, deltas):
    """Короткий разбор по цифрам. Только то, что можно честно сказать по выборке."""
    out, total = [], agg["battles"] * 6
    if not rows:
        return out
    lead = rows[0]
    pr = lead[1] / total
    s = f"{lead[0]} лидирует: {pct(lead[3])} побед при пикрейте {pct(pr)}."
    if pr < 0.01:
        s += " Берут его редко, так что это скорее нишевая сила, чем универсальный выбор."
    elif pr >= 0.025:
        s += " Его не только часто берут, но и выигрывают: это настоящий мета-пик."
    out.append(s)
    pop = max(rows, key=lambda r: r[1])
    if pop[0] != lead[0]:
        ppr = pop[1] / total
        if pop[3] < 0.48:
            out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)} пикрейта) выигрывает только {pct(pop[3])}: "
                       f"его берут скорее по привычке, чем из-за силы.")
        elif pop[3] >= 0.53:
            out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)}) ещё и силён: {pct(pop[3])} побед.")
        else:
            out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)}) показывает средний результат: {pct(pop[3])} побед.")
    risers = [d for d in deltas if d[1] > 0][:1]
    fallers = [d for d in deltas if d[1] < 0][:1]
    if risers:
        out.append(f"За сутки заметнее всех вырос {risers[0][0]}: {risers[0][1] * 100:+.1f} п.п. побед.".replace(".", ","))
    if fallers:
        out.append(f"Сильнее всех просел {fallers[0][0]}: {fallers[0][1] * 100:+.1f} п.п. побед.".replace(".", ","))
    if classes:
        agg_cls = {}
        for name, (pk, w) in agg["stats"].items():
            c = classes.get(name.upper())
            if c in CLASSES_RU:
                a = agg_cls.setdefault(c, [0, 0])
                a[0] += pk
                a[1] += w
        good = {c: v[1] / v[0] for c, v in agg_cls.items() if v[0] >= 150}
        if len(good) >= 3:
            best, worst = max(good, key=good.get), min(good, key=good.get)
            if good[best] - good[worst] >= 0.015:
                out.append(f"Из классов впереди «{CLASSES_RU[best]}» ({pct(good[best])}), "
                           f"позади «{CLASSES_RU[worst]}» ({pct(good[worst])}).")
    return out


def build_meta_post(agg, prev=None, classes=None, min_picks=META_MIN_PICKS, limit=3900):
    esc = html.escape
    rows = rank_rows(agg["stats"], min_picks)
    if len(rows) < 5:
        return ""
    total = agg["battles"] * 6
    head = ("📊 <b>МЕТА BRAWL STARS</b> · топ-игроки мира\n"
            f"<i>{agg['battles']} боёв за последние сутки; рейтинг учитывает число боёв</i>")
    top = ["🏆 <b>Сильнее всех</b>"] + [
        f"{i}. <b>{esc(n)}</b> — {pct(wr)} побед · пикрейт {pct(pk / total)}"
        for i, (n, pk, w, wr, lb) in enumerate(rows[:8], 1)]
    pop = sorted(rows, key=lambda r: r[1], reverse=True)[:5]
    popular = ["🔥 <b>Самые популярные</b>"] + [
        f"• <b>{esc(n)}</b> — {pct(pk / total)} пикрейта, {pct(wr)} побед" for n, pk, w, wr, lb in pop]

    deltas = []
    if prev and prev.get("stats"):
        for name, (pk, w) in agg["stats"].items():
            old = prev["stats"].get(name)
            if old and pk >= 40 and old[0] >= 40:
                d = w / pk - old[1] / old[0]
                if abs(d) >= 0.03:
                    deltas.append((nice_name(name), d))
        deltas.sort(key=lambda x: x[1], reverse=True)
    movers = []
    ups = [d for d in deltas if d[1] > 0][:3]
    downs = [d for d in reversed(deltas) if d[1] < 0][:3]
    if ups:
        movers.append("📈 <b>Растут за сутки</b>: " + ", ".join(
            f"{esc(n)} ({d * 100:+.1f} п.п.)".replace(".", ",") for n, d in ups))
    if downs:
        movers.append("📉 <b>Падают за сутки</b>: " + ", ".join(
            f"{esc(n)} ({d * 100:+.1f} п.п.)".replace(".", ",") for n, d in downs))

    modes = ["🧭 <b>По режимам</b>"]
    for mode, md in sorted(agg["modes"].items(), key=lambda kv: kv[1]["n"], reverse=True):
        if md["n"] < META_MODE_MIN_BATTLES:
            continue
        mrows = rank_rows(md["b"], max(8, md["n"] // 40))
        if len(mrows) >= 3:
            modes.append(f"• <b>{esc(mode_name(mode))}</b>: " + ", ".join(esc(r[0]) for r in mrows[:3]))
    analysis = meta_analysis(agg, rows, prev, classes, deltas)
    mode_hint = ""
    if len(modes) > 1:
        mode_hint = None
    why = ["💡 <b>Разбор</b>"] + [f"• {esc(a)}" for a in analysis] if analysis else []
    foot = ("<i>Это мета лучших игроков мира; на средних кубках расклад может отличаться. "
            "Разница в 1–2 п.п. в пределах погрешности.</i>\n\n#BrawlStars #мета")
    sections = [top, popular, movers, why, modes]  # при нехватке места режимы отбрасываются первыми
    while True:
        body = "\n\n".join("\n".join(sec) for sec in sections if sec)
        text = head + "\n\n" + body + "\n\n" + foot
        if len(text) <= limit or not sections:
            return text
        sections.pop()


def meta_by_mode(agg):
    """{боец: {режим: [бои, победы]}} для гайдов (режимы с достаточной выборкой)."""
    out = {}
    for mode, md in agg["modes"].items():
        if md["n"] < META_MODE_MIN_BATTLES:
            continue
        for name, (pk, w) in md["b"].items():
            if pk >= 8:
                out.setdefault(name, {})[mode] = [pk, w]
    return out


def load_classes(state):
    """Классы бойцов (танк, убийца и т.д.) с публичного API Brawlify; обновляем раз в неделю."""
    c = state.get("classes") or {}
    if c.get("map") and time.time() - c.get("ts", 0) < 7 * 86400:
        return c["map"]
    try:
        data = json.loads(get("https://api.brawlapi.com/v1/brawlers"))
        mapping = {}
        for b in data.get("list", []):
            cls = (b.get("class") or {}).get("name")
            if b.get("name") and cls:
                mapping[b["name"].upper()] = cls
        if mapping:
            state["classes"] = {"ts": time.time(), "map": mapping}
            return mapping
    except Exception as ex:
        print("классы бойцов не загрузились:", ex)
    return c.get("map") or {}


def collect_meta(deadline, now=None):
    """Опрашивает боевые журналы лучших игроков. Возвращает (агрегат, сколько журналов загружено)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    players = bs_api(f"/rankings/global/players?limit={META_PLAYERS}").get("items", [])
    tags = [p["tag"] for p in players if p.get("tag")]
    auth_fail = []

    def one(tag):
        if time.time() > deadline:
            return None
        try:
            return bs_api("/players/" + urllib.parse.quote(tag, safe="") + "/battlelog").get("items", [])
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                auth_fail.append(ex.code)
            return None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, tags))
    if auth_fail:
        raise PermissionError("API отклонил ключ")
    logs = {t: r for t, r in zip(tags, results) if r is not None}
    agg = aggregate(logs, now)
    if agg["battles"] < 150:  # запасной вариант: считаем бои любых типов
        agg = aggregate(logs, now, types=None)
    return agg, len(logs), len(tags)


def maybe_post_meta(state, started, force=False):
    """Раз в сутки публикует мету. Возвращает True, если пост ушёл."""
    if not BS_KEY:
        print("мета отключена: нет секрета BRAWL_API_KEY")
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if not force:
        if state.get("meta_posted") == today or now.hour < META_HOUR_UTC:
            return False
        if time.time() - state.get("meta_try", 0) < META_RETRY_MIN * 60:
            return False
        if time.time() - started > 330:
            print("мета: не хватает времени в этом запуске")
            return False
    state["meta_try"] = time.time()
    try:
        agg, ok, total = collect_meta(time.time() + 150, now)
    except PermissionError:
        print("мета: API отклонил ключ. Проверь, что в ключе указан IP 45.79.218.79 и секрет BRAWL_API_KEY верный.")
        return False
    except Exception as ex:
        print("мета: не удалось получить данные:", ex)
        return False
    if total == 0 or ok < total * 0.6 or agg["battles"] < 150:
        print(f"мета: данных мало (журналов {ok} из {total}, боёв {agg['battles']}), попробую позже")
        return False
    try:
        register_names(n for n in agg["stats"])
    except Exception:
        pass
    classes = load_classes(state)
    prev = state.get("meta") or {}
    if prev.get("date"):
        try:
            age = (now.date() - datetime.date.fromisoformat(prev["date"])).days
            if age > 4:
                prev = {}
        except Exception:
            prev = {}
    text = build_meta_post(agg, prev, classes)
    if not text:
        print("мета: слишком мало бойцов в рейтинге")
        return False
    try:  # картинка-тир-лист перед текстом (если есть Pillow и портреты бойцов)
        rows15 = rank_rows(agg["stats"], META_MIN_PICKS)[:15]
        portraits = {}
        urls = {nice_name(b["name"]).upper(): portrait_url(b) for b in load_brawlify()}
        def grab(name):
            u = urls.get(name.upper())
            return (name.upper(), download(u, 3 * 1024 * 1024)) if u else (name.upper(), None)
        with ThreadPoolExecutor(max_workers=6) as pool:
            portraits = {k: v for k, v in pool.map(grab, [r[0] for r in rows15]) if v}
        card = make_meta_card(rows15, portraits, agg["battles"])
        if card:
            tg_upload(card, "📊 <b>Мета Brawl Stars</b> · топ-игроки мира\n\n#BrawlStars #мета", "jpeg")
            time.sleep(PAUSE)
    except FatalError:
        raise
    except Exception as ex:
        print("мета: картинка не получилась, пойдёт только текст:", ex)
    tg_json("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    state["meta"] = {"date": today, "stats": agg["stats"], "battles": agg["battles"],
                     "bm": {n: {m: v for m, v in per.items()} for n, per in meta_by_mode(agg).items()}}
    state["meta_posted"] = today
    count_post(state, "meta")
    print(f"мета опубликована: {agg['battles']} боёв, {ok} журналов")
    return True


# ---------- карточки (необязательно, нужен Pillow) ----------
FONT_PATHS = [
    "font.ttf",  # свой шрифт: положи файл font.ttf рядом с bot.py
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]  # все эти шрифты умеют кириллицу; шрифт без кириллицы дал бы пустые квадраты вместо букв


def card_font(size):
    if ImageFont is None:
        return None
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _gradient(w, h, top, bottom):
    return Image.composite(Image.new("RGB", (w, h), bottom), Image.new("RGB", (w, h), top),
                           Image.linear_gradient("L").resize((w, h)))


def _fit(draw, text, max_w, start, minimum=20):
    size = start
    while size > minimum:
        f = card_font(size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return card_font(minimum)


def _portrait(data, box):
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    k = min(box / im.width, box / im.height)
    return im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)


def _chip(draw, xy, text, font, fill, color=(255, 255, 255)):
    w = draw.textlength(text, font=font)
    x, y = xy
    draw.rounded_rectangle((x, y, x + w + 40, y + font.size + 24), radius=26, fill=fill)
    draw.text((x + 20, y + 10), text, font=font, fill=color)
    return x + w + 60


def _logo(img):
    if os.path.exists("logo.png"):  # свой логотип: положи logo.png рядом с bot.py
        try:
            lg = Image.open("logo.png").convert("RGBA")
            lg.thumbnail((120, 120))
            img.paste(lg, (40, 30), lg)
            return 180
        except Exception:
            pass
    return 48


def make_guide_card(name, cls_ru="", portrait=None, stat_text=""):
    """Карточка гайда 1080x1080 (JPEG). None, если Pillow или шрифт недоступны."""
    if Image is None or card_font(40) is None:
        return None
    W = H = 1080
    img = _gradient(W, H, (104, 40, 176), (20, 12, 64))
    d = ImageDraw.Draw(img)
    x0 = _logo(img)
    d.text((x0, 52), "NEW BRAWL NEWS", font=card_font(44), fill=(255, 255, 255), stroke_width=4, stroke_fill=(20, 10, 50))
    f = card_font(54)
    tw = d.textlength("ГАЙД", font=f)
    d.rounded_rectangle((W - tw - 108, 36, W - 40, 126), radius=30, fill=(255, 204, 0))
    d.text((W - tw - 74, 50), "ГАЙД", font=f, fill=(40, 20, 70))
    if portrait:
        try:
            pm = _portrait(portrait, 740)
            d.ellipse((W // 2 - 300, 820, W // 2 + 300, 900), fill=(12, 6, 40))
            img.paste(pm, (W // 2 - pm.width // 2, 140 + (740 - pm.height) // 2), pm)
        except Exception:
            pass
    title = name.upper()
    d.text((W // 2, 905), title, font=_fit(d, title, 980, 120), fill=(255, 255, 255), anchor="mm",
           stroke_width=8, stroke_fill=(20, 10, 50))
    x = 60
    cf = card_font(40)
    for text, color in ((cls_ru, (255, 140, 0)), (stat_text, (30, 160, 90))):
        if text:
            x = _chip(d, (x, 985), text, cf, color)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def make_meta_card(rows, portraits, battles):
    """Тир-лист из 15 бойцов (S, A, B) 1080x1180. rows: [(имя, бои, победы, доля побед, рейтинг)]."""
    if Image is None or card_font(40) is None or len(rows) < 10:
        return None
    W, H = 1080, 1180
    img = _gradient(W, H, (30, 20, 90), (10, 8, 40))
    d = ImageDraw.Draw(img)
    x0 = _logo(img)
    d.text((x0, 40), "МЕТА BRAWL STARS", font=card_font(58), fill=(255, 255, 255), stroke_width=5, stroke_fill=(10, 5, 40))
    d.text((x0, 112), f"топ-игроки мира · {battles} боёв за сутки", font=card_font(34), fill=(190, 190, 255))
    tiers = (("S", (230, 60, 60)), ("A", (255, 150, 30)), ("B", (60, 170, 90)))
    nf, wf = card_font(25), card_font(28)
    for t, (letter, color) in enumerate(tiers):
        y = 190 + t * 320
        d.rounded_rectangle((30, y, 150, y + 290), radius=30, fill=color)
        d.text((90, y + 145), letter, font=card_font(90), fill=(255, 255, 255), anchor="mm")
        for i, (name, pk, w, wr, lb) in enumerate(rows[t * 5:t * 5 + 5]):
            x = 175 + i * 178
            d.rounded_rectangle((x, y, x + 166, y + 290), radius=22, fill=(42, 30, 110))
            raw = portraits.get(name.upper())
            if raw:
                try:
                    pm = _portrait(raw, 146)
                    img.paste(pm, (x + 10 + (146 - pm.width) // 2, y + 12 + (146 - pm.height) // 2), pm)
                except Exception:
                    raw = None
            if not raw:
                d.rounded_rectangle((x + 10, y + 12, x + 156, y + 158), radius=18, fill=color)
                d.text((x + 83, y + 85), name[:2].upper(), font=card_font(50), fill=(255, 255, 255), anchor="mm")
            d.text((x + 83, y + 195), name, font=_fit(d, name, 150, 28, 14), fill=(255, 255, 255), anchor="mm")
            d.text((x + 83, y + 250), pct(wr), font=wf, fill=(255, 220, 90), anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


# ---------- гайды на бойцов ----------
BRAWLIFY_URL = "https://api.brawlapi.com/v1/brawlers"
GUIDE_HOUR_UTC = 15          # гайд выходит после 18:00 по Москве
GUIDE_REPEAT_DAYS = 30       # одного бойца повторяем не чаще, чем раз в столько дней
CLASS_ONE_RU = {"Damage Dealer": "боец урона", "Tank": "танк", "Support": "поддержка", "Controller": "контроль",
                "Assassin": "убийца", "Marksman": "снайпер", "Artillery": "артиллерия"}
CLASS_TIPS = {
    "Damage Dealer": "Держитесь на дистанции, добивайте раненых врагов и не лезьте в одиночку под фокус.",
    "Tank": "Идите вперёд вместе с командой, принимайте урон на себя и берегите супер для решающего момента.",
    "Support": "Держитесь за спинами союзников, помогайте им и не выходите вперёд без нужды.",
    "Controller": "Контролируйте зоны и проходы, используйте стены и замедление, чтобы навязывать бой на своих условиях.",
    "Assassin": "Заходите с фланга или из укрытия, выбирайте уязвимые цели и не оставайтесь в центре боя.",
    "Marksman": "Держитесь на дальней дистанции, бейте по укрытиям и не давайте врагам сблизиться.",
    "Artillery": "Бейте по укрытиям и зонируйте проходы, а от ближнего боя вас должна защищать команда.",
}
GUIDE_DISCLAIMER = ("⚠️ Универсальный разбор: выбор гаджета и звёздной силы зависит от карты, режима и "
                    "состава команды, ориентируйтесь по ситуации.")


def load_brawlify():
    data = json.loads(get(BRAWLIFY_URL, limit=4 * 1024 * 1024))
    lst = data.get("list") if isinstance(data, dict) else data
    return [b for b in (lst or []) if isinstance(b, dict) and b.get("name")]


def portrait_url(b):
    for k in ("imageUrl2", "imageUrl3", "imageUrl"):
        if b.get(k):
            return b[k]
    return ""


def pick_guide_brawler(state, brawlers, today=None):
    """Выбирает бойца для гайда: сначала из лучших по свежей мете, иначе того, о ком давно не писали."""
    today = today or datetime.date.today().isoformat()
    guided = state.get("guided", {})
    by_name = {b["name"].upper(): b for b in brawlers}

    def fresh(name):
        last = guided.get(name.upper())
        if not last:
            return True
        try:
            return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days >= GUIDE_REPEAT_DAYS
        except Exception:
            return True

    meta = state.get("meta") or {}
    if meta.get("stats"):
        for row in rank_rows(meta["stats"], META_MIN_PICKS)[:15]:
            if row[0].upper() in by_name and fresh(row[0]):
                return by_name[row[0].upper()]
    pool = [b for b in brawlers if fresh(b["name"])]
    return random.choice(pool) if pool else None


def _abilities(b):
    def rows(key):
        return [(nice_name(x.get("name", "")), clean(x.get("description", ""))) for x in (b.get(key) or [])
                if isinstance(x, dict) and x.get("name")]
    return rows("gadgets"), rows("starPowers"), rows("hyperCharges")


def _tr(text, n):
    try:
        return translate(shorten(text, n), mask_quotes=True).rstrip(". ") if text else ""
    except Exception:
        return ""  # без перевода описание не показываем


def guide_info(b, state):
    """Собирает данные гайда: описание и способности (переведённые), цифры из меты, совет по классу."""
    name = nice_name(b["name"])
    upper = b["name"].upper()
    cls = (b.get("class") or {}).get("name", "")
    gadgets, stars, hypers = _abilities(b)
    jobs = {"desc": (clean(b.get("description", "")), 220)}
    for kind, lst in (("g", gadgets), ("s", stars), ("h", hypers)):
        for i, (n, d) in enumerate(lst):
            jobs[f"{kind}{i}"] = (d, 130)
    with ThreadPoolExecutor(max_workers=6) as pool:
        done = dict(zip(jobs, pool.map(lambda v: _tr(*v), jobs.values())))
    meta = state.get("meta") or {}
    stat_line, short_stat = "", ""
    st = (meta.get("stats") or {}).get(upper)
    if st and st[0] >= META_MIN_PICKS and meta.get("battles"):
        wr = st[1] / st[0]
        pr = st[0] / (meta["battles"] * 6)
        short_stat = f"{pct(wr)} побед"
        modes = sorted(((m, v) for m, v in (meta.get("bm") or {}).get(upper, {}).items() if v[0] >= 8),
                       key=lambda kv: wilson_lb(kv[1][1], kv[1][0]), reverse=True)[:2]
        stat_line = f"По боям топ-игроков за сутки: {pct(wr)} побед, пикрейт {pct(pr)}."
        if modes:
            stat_line += " Лучше всего: " + ", ".join(mode_name(m) for m, v in modes) + "."
    return {
        "name": name, "cls": cls, "cls_ru": CLASS_ONE_RU.get(cls, ""), "desc": done["desc"],
        "gadgets": [(n, done[f"g{i}"]) for i, (n, d) in enumerate(gadgets)],
        "stars": [(n, done[f"s{i}"]) for i, (n, d) in enumerate(stars)],
        "hypers": [(n, done[f"h{i}"]) for i, (n, d) in enumerate(hypers)],
        "stat_line": stat_line, "short_stat": short_stat, "tip": CLASS_TIPS.get(cls, ""),
    }


def youtube_search_link(name):
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote(f"Brawl Stars {name} гайд")


def build_guide_caption(info):
    """Короткая подпись к карточке (до лимита подписи)."""
    esc = html.escape
    head = f"📘 Гайд: {info['name']}" + (f" · {info['cls_ru']}" if info["cls_ru"] else "")
    parts = [(head, f"<b>{esc(head)}</b>")]
    if info["stat_line"]:
        parts.append((info["stat_line"], esc(info["stat_line"])))
    parts.append((GUIDE_DISCLAIMER, f"<i>{esc(GUIDE_DISCLAIMER)}</i>"))
    tags = "#BrawlStars #гайд"
    parts.append((tags, esc(tags)))
    return "\n\n".join(p[1] for p in parts)


def build_guide_text(info, limit=3900):
    """Подробный текст гайда. При нехватке места сначала убираем пояснения, потом совет."""
    esc = html.escape
    link = youtube_search_link(info["name"])
    for level in (0, 1, 2):  # 0: всё, 1: пояснения короче, 2: только названия
        plain, htm = [], []

        def add(p, h):
            plain.append(p)
            htm.append(h)

        head = f"📘 Гайд: {info['name']}" + (f" · {info['cls_ru']}" if info["cls_ru"] else "")
        add(head, f"<b>{esc(head)}</b>")
        if info["desc"]:
            add(info["desc"], esc(info["desc"]))
        if info["stat_line"]:
            add("📊 " + info["stat_line"], "📊 " + esc(info["stat_line"]))
        for label, rows in (("🔧 Гаджеты", info["gadgets"]), ("⭐ Звёздные силы", info["stars"]),
                            ("⚡ Гиперзаряд", info["hypers"])):
            if not rows:
                continue
            lines_p, lines_h = [label], [f"<b>{esc(label)}</b>"]
            for n, d in rows:
                d = d if level == 0 else (shorten(d, 70) if level == 1 else "")
                lines_p.append(f"• {n}" + (f": {d}" if d else ""))
                lines_h.append(f"• <b>{esc(n)}</b>" + (f": {esc(d)}" if d else ""))
            add("\n".join(lines_p), "\n".join(lines_h))
        if info["tip"] and level < 2:
            add("🧭 Как играть: " + info["tip"], "🧭 <b>Как играть:</b> " + esc(info["tip"]))
        add(GUIDE_DISCLAIMER, f"<i>{esc(GUIDE_DISCLAIMER)}</i>")
        lab = "▶️ Видеогайды на YouTube"
        add(lab, f'<a href="{esc(link, quote=True)}">{lab}</a>')
        add("#BrawlStars #гайд", "#BrawlStars #гайд")
        if len("\n\n".join(plain)) <= limit or level == 2:
            return "\n\n".join(htm)


def maybe_post_guide(state, started, force=False):
    """Раз в сутки публикует гайд на бойца. Возвращает True, если пост ушёл."""
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    daily = state.setdefault("daily", {})
    if not force:
        if state.get("guide_posted") == today or now.hour < GUIDE_HOUR_UTC:
            return False
        if daily.get("guides", 0) >= DAILY_GUIDE_LIMIT:
            return False
        if time.time() - state.get("guide_try", 0) < 3600:
            return False
        if time.time() - started > 330:
            print("гайд: не хватает времени в этом запуске")
            return False
    state["guide_try"] = time.time()
    try:
        brawlers = load_brawlify()
    except Exception as ex:
        print("гайд: не удалось получить данные о бойцах:", ex)
        return False
    b = pick_guide_brawler(state, brawlers, today)
    if not b:
        print("гайд: все бойцы недавно были, пропускаю")
        return False
    info = guide_info(b, state)
    if not (info["gadgets"] or info["stars"] or info["desc"]):
        print("гайд: нет данных о способностях, пропускаю:", info["name"])
        return False
    text = build_guide_text(info)
    portrait = None
    try:
        portrait = download(portrait_url(b), 5 * 1024 * 1024) if portrait_url(b) else None
    except Exception:
        portrait = None
    card = make_guide_card(info["name"], info["cls_ru"], portrait, info["short_stat"]) if portrait else None
    if card:
        try:
            tg_upload(card, build_guide_caption(info), "jpeg")
            time.sleep(PAUSE)
        except FatalError:
            raise
        except Exception as ex:
            print("гайд: карточка не отправилась, пойдёт только текст:", ex)
    tg_json("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True})
    state.setdefault("guided", {})[b["name"].upper()] = today
    state["guide_posted"] = today
    count_post(state, "guide")
    print("гайд опубликован:", info["name"])
    return True


# ---------- состояние ----------
def count_post(state, kind):
    """Считает пост в суточных лимитах: kind = regular | important | guide | meta | balance."""
    d = state.setdefault("daily", {})
    d["total"] = d.get("total", 0) + 1
    if kind in ("regular", "important", "guide"):
        key = {"regular": "regular", "important": "important", "guide": "guides"}[kind]
        d[key] = d.get(key, 0) + 1


def in_quiet_hours(now):
    start, end = QUIET_HOURS_UTC
    h = now.hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def load_state():
    state = {"seen": [], "sources": [], "daily": {}, "yt": {}, "recent": [], "fails": {}, "phrases": [],
             "meta": {}, "meta_posted": "", "meta_try": 0, "classes": {}, "guided": {}, "guide_try": 0}
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
    if len(state.get("guided", {})) > GUIDED_KEEP:  # помним только последние гайды
        keep = sorted(state["guided"].items(), key=lambda kv: kv[1])[-GUIDED_KEEP:]
        state["guided"] = dict(keep)
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
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today}
    for k in ("regular", "important", "total", "guides", "llm"):
        state["daily"].setdefault(k, 0)
    daily = state["daily"]

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
    sent, errors, quiet = 0, 0, in_quiet_hours(now)
    for it, important, key in queue:
        if sent >= MAX_PER_RUN:
            break
        if time.time() - started > TIME_BUDGET:
            print("время запуска вышло, остальное в следующий раз")
            break
        if errors >= MAX_SEND_ERRORS_PER_RUN:
            print("слишком много ошибок отправки, остановка до следующего запуска")
            break
        if important and daily["important"] >= DAILY_IMPORTANT_LIMIT:
            continue  # страховка: остаётся до завтра
        if not important:
            if quiet:
                continue  # ночью обычные посты не публикуем, они дождутся утра
            if daily["regular"] >= DAILY_REGULAR_LIMIT or daily["total"] >= DAILY_TOTAL_LIMIT:
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
            if not important and SCAM_RE.search(f"{it.get('title', '')} {it.get('text', '')}"):
                print("  похоже на обман с «бесплатными гемами», пропускаю:", it.get("title", it["id"]))
                mark(it["id"])
                continue
            if not important and not it.get("img") and not it.get("force_link"):
                mark(it["id"])
                continue
            if is_dup(keys_of(it), tokens_of(it.get("title", "")), important, recent):
                print("  дубль, не публикую:", it.get("title", it["id"]))
                mark(it["id"])
                continue
            ru = llm_polish(it, important, state)
            if ru:
                it["_ru"] = ru
            phrase = (ru or {}).get("phrase") or pick_phrase(it, state["phrases"], important)
            send(it, important, phrase)
        except FatalError as ex:
            print("ОШИБКА:", ex)
            save_state(state)
            sys.exit(1)
        except LowQualityImage as ex:
            print("  пропускаю (плохая картинка):", it.get("title", it["id"]), "-", ex)
            mark(it["id"])
            continue
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                print("Telegram просит подождать, остановка до следующего запуска")
                break
            print("  не отправлено, повторю позже:", key, "-", ex)
            errors += 1
            bump(it["id"], MAX_FAILS)
            continue
        except Exception as ex:
            print("  не отправлено, повторю позже:", key, "-", ex)
            errors += 1
            bump(it["id"], MAX_FAILS)
            continue
        errors = 0  # счётчик считает ошибки подряд
        mark(it["id"])
        remember(it, important)
        state["phrases"].append(phrase)
        sent += 1
        count_post(state, "important" if important else "regular")
        save_state(state)  # сразу после каждой отправки
        time.sleep(PAUSE)
        if it.get("_page"):  # к заметкам об обновлении добавляем разбор баланса
            try:
                if post_balance_digest(it):
                    print("  разбор баланса опубликован")
                    count_post(state, "balance")
                    time.sleep(PAUSE)
            except FatalError as ex:
                print("ОШИБКА:", ex)
                save_state(state)
                sys.exit(1)
            except Exception as ex:
                print("  разбор баланса не получился:", ex)

    for job in (lambda: maybe_post_meta(state, started, force=FORCE_META),
                lambda: maybe_post_guide(state, started, force=FORCE_GUIDE)):
        try:
            job()
        except FatalError as ex:
            print("ОШИБКА:", ex)
            save_state(state)
            sys.exit(1)
        except Exception as ex:
            print("плановый пост не получился:", ex)

    save_state(state)
    print(f"готово: опубликовано {sent}; за сутки всего {daily['total']}, "
          f"сообщество {daily['regular']}, важных {daily['important']}")


if __name__ == "__main__":
    main()
