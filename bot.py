"""Бот новостей Brawl Stars: собирает новости из первоисточников,
переводит на русский и публикует в Telegram-канал. Дубли отсекаются."""
import os, sys, re, io, json, html, time, uuid, datetime, random, math
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT = os.environ.get("CHAT_ID", "")

MAX_PER_RUN = 3
DAILY_REGULAR_LIMIT = 8
PAUSE = 3
TIME_BUDGET = 300
MAX_FAILS = 36
FILL_FAILS = 5
STATE_FILE = "seen.json"
SEEN_KEEP = 1000
RECENT_KEEP = 200
RECENT_DAYS = 10
TITLE_DUP_DAYS = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SOURCES = [
    {"name": "supercell-blog", "type": "supercell", "kind": "blog",
     "url": "https://supercell.com/en/games/brawlstars/blog/", "important": True},
    {"name": "supercell-announcements", "type": "supercell", "kind": "announcement",
     "url": "https://supercell.com/en/news/announcement/brawlstars/", "important": True},
    {"name": "appstore-update", "type": "appstore", "app_id": "1229016807", "important": True},
    {"name": "yt-brawlstars", "type": "youtube", "handle": "BrawlStars", "important": True,
     "expect": "brawl stars", "reject": "esports"},
    {"name": "yt-esports", "type": "youtube", "handle": "brawlstarsesports", "important": False,
     "link": True, "expect": "esports"},
    {"name": "reddit-official", "type": "feed", "important": True,
     "url": "https://www.reddit.com/r/Brawlstars/search.rss?q=flair%3A%22Supercell+Official%22&restrict_sr=on&sort=new"},
    {"name": "reddit-top", "type": "feed", "important": False,
     "url": "https://www.reddit.com/r/BrawlStars/top/.rss?t=day"},
    {"name": "reddit-leaks", "type": "feed", "important": False,
     "url": "https://www.reddit.com/r/BrawlStars/search.rss?q=title%3Aleak&restrict_sr=on&sort=new&t=week"},
    {"name": "brawlify-guides", "type": "guide", "important": False},
]

KEEP = ["Brawl Stars", "Brawl Pass", "Brawl Talk", "Starr Drop", "Supercell Make", "Supercell ID",
        "Supercell Store", "Supercell", "Clash Royale", "Clash of Clans", "Hay Day", "Boom Beach"]

def _keep_pattern(k): return re.escape(k).replace(r"\ ", r"\s?") + "s?"
KEEP_RE = re.compile(r"\b(" + "|".join(_keep_pattern(k) for k in sorted(KEEP, key=len, reverse=True)) + r")\b", re.I)

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
    return re.compile(r"(?<![\w-])(" + alt + r")(?![\w-])", re.I)

NAMES_RE = _names_regex(BRAWLERS)
QUOTED_RE = re.compile(r'"[^"\n]{2,40}"|“[^”\n]{2,40}”|«[^»\n]{2,40}»')

def nice_name(n):
    s = (n or "").strip().title()
    return {"Bibi": "BiBi"}.get(s, s)

def register_names(names):
    global NAMES_RE
    new = [nice_name(n) for n in names if n]
    fresh = [n for n in new if n.lower() not in {b.lower() for b in BRAWLERS}]
    if fresh:
        BRAWLERS.extend(fresh)
        NAMES_RE = _names_regex(BRAWLERS)

SCAM_RE = re.compile(
    r"\b(?:free|unlimited)\s+(?:gems?|coins?|brawl pass|skins?)\b|\bgems?\s+(?:generator|hack|glitch|codes?)\b"
    r"|\b(?:hack|hacks|cheats?)\b|\bgenerator\b|\bpromo ?codes?\b|\bgift ?cards?\b|\bgiveaway", re.I)

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CYR = re.compile("[а-яА-ЯёЁ]")

class FatalError(Exception): pass
class SkipItem(Exception): pass

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
            if ex.code not in (408, 425, 429, 500, 502, 503, 504): break
            time.sleep(2 * (i + 1))
        except Exception as ex:
            last = ex
            time.sleep(2 * (i + 1))
    raise last

def download(url, limit=10 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=30).read(limit + 1)
    if len(data) > limit: raise ValueError("картинка слишком большая")
    return data

def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\[link\]|\[comments\]|submitted by\s+/u/\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def shorten(s, n=300):
    if len(s) <= n: return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",.;: ") + "…"

def mostly_russian(s):
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", s)
    return bool(letters) and len(CYR.findall(s)) / len(letters) > 0.6

def _gtx(masked):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q=" + urllib.parse.quote(masked))
    data = json.loads(get(url))
    return "".join(p[0] for p in data[0]).strip()

def _mymemory(masked):
    if len(masked.encode()) > 450: raise ValueError("слишком длинно")
    url = ("https://api.mymemory.translated.net/get?langpair=en|ru&q=" + urllib.parse.quote(masked))
    data = json.loads(get(url))
    return html.unescape(data["responseData"]["translatedText"]).strip()

ENGINES = [_gtx, _mymemory]

def _validate(masked, kept, result):
    rest = re.sub(r"ZZ\d+ZZ", "", masked)
    words = re.findall(r"[A-Za-z]{2,}", rest)
    cyr = len(CYR.findall(result))
    lat = len(re.findall(r"[A-Za-z]", re.sub(r"ZZ\s*\d+\s*ZZ", "", result)))
    if len(words) >= 4 and cyr < 0.5 * (cyr + lat): raise ValueError("перевод не удался")
    pattern = re.compile(r"ZZ\s*(\d+)\s*ZZ", re.I)
    found = pattern.findall(result)
    if len(found) != len(kept) or any(int(i) >= len(kept) for i in found):
        raise ValueError("ключевые слова потерялись")
    return pattern.sub(lambda m: kept[int(m.group(1))], result)

_BOEC = {"": "боец", "а": "бойца", "у": "бойцу", "ом": "бойцом", "е": "бойце", "ы": "бойцы", "ов": "бойцов", "ам": "бойцам", "ами": "бойцами", "ах": "бойцах"}
_KUBOK = {"й": "кубок", "я": "кубка", "ю": "кубку", "ем": "кубком", "е": "кубке", "и": "кубки", "ев": "кубков", "ям": "кубкам", "ями": "кубками", "ях": "кубках"}
_ZABIYAKA = {"а": "боец", "и": "бойцы", "е": "бойце", "у": "бойца", "ой": "бойцом", "": "бойцов", "ам": "бойцам", "ами": "бойцами", "ах": "бойцах"}

def _same_case(src, word): return word[:1].upper() + word[1:] if src[:1].isupper() else word

def fix_ru(text):
    text = re.sub(r"\b(скандалист|драчун|бравлер)(а|у|ом|е|ы|ов|ам|ами|ах)?\b", lambda m: _same_case(m.group(0), _BOEC[m.group(2) or ""]), text, flags=re.I)
    text = re.sub(r"\bзабияк(а|и|е|у|ой|ам|ами|ах)?\b", lambda m: _same_case(m.group(0), _ZABIYAKA[m.group(1) or ""]), text, flags=re.I)
    text = re.sub(r"\bтрофе(й|я|ю|ем|е|и|ев|ям|ями|ях)\b", lambda m: _same_case(m.group(0), _KUBOK[m.group(1)]), text, flags=re.I)
    text = re.sub(r"\b(очк\w+)\s+мощности\b", r"\1 силы", text, flags=re.I)
    return text

def translate(text, mask_quotes=False):
    if not text: return ""
    if mostly_russian(text): return text
    kept = []
    def mask(m):
        kept.append(m.group(0))
        return f"ZZ{len(kept) - 1}ZZ"
    masked = text
    if mask_quotes: masked = QUOTED_RE.sub(mask, masked)
    masked = KEEP_RE.sub(mask, masked)
    masked = NAMES_RE.sub(mask, masked)
    last = None
    for engine in ENGINES:
        try: return fix_ru(_validate(masked, kept, engine(masked)))
        except Exception as ex: last = ex
    raise ValueError(f"перевод не получился: {last}")

GENERIC = set("""the and for with from this that your you are has have now out get into will its all more here how what when why about was were can our their just new update updates video videos official brawler brawlers skin skins game games""".split())
MONTHS = set("""january february march april may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec""".split())

def canon(url):
    try: u = urllib.parse.urlparse(url.strip())
    except Exception: return ""
    host = u.netloc.lower()
    if host.startswith("www."): host = host[4:]
    if host == "youtu.be":
        vid = u.path.strip("/")
        return "yt:" + vid if re.fullmatch(r"[\w-]{11}", vid) else ""
    if host.endswith("youtube.com"):
        q = urllib.parse.parse_qs(u.query)
        if "v" in q: return "yt:" + q["v"][0]
        m = re.match(r"/(?:shorts|embed|live)/([\w-]{11})", u.path)
        return "yt:" + m.group(1) if m else ""
    if host.endswith("reddit.com"):
        m = re.search(r"/comments/(\w+)", u.path)
        return "reddit:" + m.group(1) if m else ""
    path = u.path.rstrip("/")
    segs = [x for x in path.split("/") if x]
    if not segs: return ""
    last = segs[-1]
    if (len(segs) >= 2 and "-" in last and len(last) >= 8) or re.fullmatch(r"\d{6,}", last): return host + path
    return ""

def keys_of(item):
    ks = set()
    for u in [item.get("link", "")] + list(item.get("urls", [])):
        k = canon(u) if u else ""
        if k: ks.add(k)
    iid = str(item.get("id", ""))
    if iid.startswith("yt:video:"): ks.add("yt:" + iid[len("yt:video:"):])
    return ks

SHORT_STOP = set("an of in on to is it as at by or vs x".split())

def tokens_of(title):
    t = KEEP_RE.sub(" ", (title or "").lower())
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if w in GENERIC or w in SHORT_STOP: continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out

def _marks(tokens): return {w for w in tokens if w.isdigit() or w in MONTHS or len(w) <= 2}

def is_dup(keys, toks, important, recent, now=None):
    now = now or time.time()
    for r in recent:
        if important and not r.get("imp"): continue
        if keys & set(r["k"]): return True
        rt = set(r["t"])
        if not (toks and rt) or now - r.get("ts", 0) > TITLE_DUP_DAYS * 86400: continue
        ma, mb = _marks(toks), _marks(rt)
        if ma and mb and ma != mb: continue
        common = len(toks & rt)
        if (common >= 3 and common / min(len(toks), len(rt)) >= 0.6) or (len(toks) >= 2 and toks == rt): return True
    return False

def ctext(e, *tags):
    for tag in tags:
        n = e.find(tag)
        if n is not None and n.text and n.text.strip(): return n.text.strip()
    return ""

def pick_image(e, body):
    body = html.unescape(body or "")
    m = re.search(r'https://i\.redd\.it/[^"\s<>]+\.(?:jpe?g|png|webp)', body, re.I)
    if m: return m.group(0)
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', body)
    for img in imgs:
        if any(ext in img.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            if not any(bad in img.lower() for bad in ['icon', 'avatar', 'thumb', 'emoji']):
                return html.unescape(img)
    for enc in e.findall("enclosure"):
        if (enc.get("type") or "").startswith("image") and enc.get("url"): return enc.get("url")
    t = e.find(".//" + MEDIA + "thumbnail")
    if t is not None and t.get("url"): return html.unescape(t.get("url"))
    return ""

def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = root.findall(ATOM + "entry") or list(root.iter("item"))
    items = []
    for e in entries:
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href"); break
        if not link: link = ctext(e, "link")
        body = ctext(e, ATOM + "content", ATOM + "summary", "description")
        if not body:
            d = e.find(".//" + MEDIA + "description")
            body = d.text if d is not None and d.text else ""
        item_id = ctext(e, ATOM + "id", "guid", "id") or link
        title = clean(ctext(e, ATOM + "title", "title"))
        if not title or not item_id: continue
        urls = re.findall(r'https?://[^\s"<>)\]]+', html.unescape(body))
        items.append({"id": item_id, "title": title, "link": link, "text": shorten(clean(body)), "img": pick_image(e, body), "urls": urls})
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
    "blog": (r"((?:https://supercell\.com)?/en/games/brawlstars/blog/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+))" + _END, {"page", "tag", "category"}),
    "announcement": (r"((?:https://supercell\.com)?/en/news/([A-Za-z0-9][A-Za-z0-9_-]*))" + _END, {"announcement", "blog", "brawlstars", "page"}),
}

def fetch_supercell(src, state):
    kind = src.get("kind", "blog")
    page = get(src["url"]).decode("utf-8", "replace").replace("\\/", "/").replace("\\u002F", "/")
    pat, skip = SUPERCELL_LINKS[kind]
    links = []
    for m in re.finditer(pat, page):
        if any(seg in skip for seg in m.groups()[1:]): continue
        path = m.group(1)
        url = path if path.startswith("http") else "https://supercell.com" + path
        url = url.rstrip("/") + "/"
        if url not in links: links.append(url)
    return [{"id": u, "link": u, "lazy": True, "kind": kind} for u in links[:12]]

def fill_supercell(item):
    page = get(item["link"]).decode("utf-8", "replace")
    m = metas(page)
    title = re.sub(r"\s*[×|\-–—]\s*Supercell\s*$", "", m.get("og:title", ""))
    if not title: raise ValueError("нет заголовка")
    desc = m.get("og:description") or m.get("description", "")
    if item.get("kind") == "announcement" and not re.search(r"brawl", f"{title} {desc} {item['link']}", re.I):
        raise SkipItem("не про Brawl Stars")
    img = m.get("og:image", "")
    item["title"] = title
    item["text"] = shorten(clean(desc))
    item["img"] = "" if "og_default" in img else img
    item["urls"] = re.findall(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}", page)
    if item.get("kind") == "blog" and re.search(r"release notes|balance|maintenance|patch", f"{title} {desc}", re.I):
        item["_page"] = page
    item["lazy"] = False

def fetch_appstore(src, state):
    data = json.loads(get(f"https://itunes.apple.com/lookup?id={src['app_id']}&country=us"))
    r = data["results"][0]
    ver = r["version"]
    return [{
        "id": f"appstore:{ver}", "title": f"Вышло обновление Brawl Stars: версия {ver}",
        "title_ru": True, "topic": ("🛠", "Вышло обновление", "обновление"),
        "link": r.get("trackViewUrl", ""), "text": shorten(clean(r.get("releaseNotes", ""))),
        "img": r.get("artworkUrl512", ""),
    }]

def yt_channel_id(handle, state):
    cache = state.setdefault("yt", {})
    if handle in cache: return cache[handle]
    page = get(f"https://www.youtube.com/@{handle}", {"Cookie": "CONSENT=YES+1; SOCS=CAI"}).decode("utf-8", "replace")
    pats = (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', r'"externalId":"(UC[\w-]{22})"', r'"channelId":"(UC[\w-]{22})"')
    for pat in pats:
        m = re.search(pat, page)
        if m: cache[handle] = m.group(1); return m.group(1)
    raise ValueError(f"не нашёл channel_id для @{handle}")

def fetch_youtube(src, state):
    cid = yt_channel_id(src["handle"], state)
    xml = get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid)
    name = (ET.fromstring(xml).findtext(ATOM + "title") or "").strip()
    low = name.lower()
    if (src.get("expect") and src["expect"] not in low) or (src.get("reject") and src["reject"] in low):
        state.get("yt", {}).pop(src["handle"], None)
        raise ValueError(f"канал @{src['handle']} называется «{name}», это не тот канал")
    return parse_feed(xml)

# --- БАЗОВАЯ ОТРИСОВКА (ГРАДИЕНТ, ШРИФТ, ЛОГО) ---
def get_base_image():
    W, H = 1080, 1080
    base = Image.new('RGB', (W, H), "#2d0059")
    top = Image.new('RGB', (W, H), "#004d99")
    mask = Image.new('L', (W, H))
    mask_data = []
    for y in range(H):
        for x in range(W):
            mask_data.append(int(255 * (x / W + y / H) / 2))
    mask.putdata(mask_data)
    bg = Image.composite(top, base, mask).convert("RGBA")
    
    # Логотип и название
    if os.path.exists("logo.png"):
        try:
            logo = Image.open("logo.png").convert("RGBA")
            min_dim = min(logo.size)
            logo = logo.crop(((logo.width - min_dim)/2, (logo.height - min_dim)/2, (logo.width + min_dim)/2, (logo.height + min_dim)/2))
            logo = logo.resize((140, 140), Image.Resampling.LANCZOS)
            mask = Image.new("L", (140, 140), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 140, 140), fill=255)
            circle_logo = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
            circle_logo.paste(logo, (0, 0), mask)
            draw = ImageDraw.Draw(bg)
            draw.ellipse([(58, 38), (202, 182)], outline="#ffcc00", width=5)
            bg.paste(circle_logo, (60, 40), circle_logo)
        except Exception as e: print("Ошибка лого:", e)
            
    font_path = "LilitaOne-Regular.ttf"
    if not os.path.exists(font_path):
        try:
            font_url = "https://github.com/google/fonts/raw/main/ofl/lilitaone/LilitaOne-Regular.ttf"
            with open(font_path, "wb") as f: f.write(download(font_url))
        except Exception as e: print("Не удалось скачать шрифт:", e)
        
    try: font_channel = ImageFont.truetype(font_path, 55)
    except: font_channel = ImageFont.load_default()
    
    draw = ImageDraw.Draw(bg)
    draw.text((230, 70), "NEW BRAWL NEWS", font=font_channel, fill="white", stroke_width=3, stroke_fill="black")
    return bg

# --- ГЕНЕРАЦИЯ КАРТИНКИ ДЛЯ ГАЙДА ---
def generate_guide_image(b, gadget_name, sp_name):
    try:
        bg = get_base_image()
        draw = ImageDraw.Draw(bg)
        
        font_path = "LilitaOne-Regular.ttf"
        try:
            font_name = ImageFont.truetype(font_path, 120)
            font_class = ImageFont.truetype(font_path, 50)
            font_label = ImageFont.truetype(font_path, 35)
            font_desc = ImageFont.truetype(font_path, 40)
        except:
            font_name = font_class = font_label = font_desc = ImageFont.load_default()

        draw.text((60, 230), "ГАЙД НА БОЙЦА", font=font_label, fill="#ffcc00", stroke_width=2, stroke_fill="black")
        draw.text((60, 270), b['name'].upper(), font=font_name, fill="white", stroke_width=5, stroke_fill="black")
        
        role = (b.get("class") or {}).get("name", "Боец")
        draw.text((65, 410), f"Класс: {role}", font=font_class, fill="#ffcc00", stroke_width=2, stroke_fill="black
