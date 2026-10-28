"""Бот новостей Brawl Stars"""
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
        except Exception as e: pass
            
    font_path = "LilitaOne-Regular.ttf"
    if not os.path.exists(font_path):
        try:
            font_url = "https://github.com/google/fonts/raw/main/ofl/lilitaone/LilitaOne-Regular.ttf"
            with open(font_path, "wb") as f: f.write(download(font_url))
        except Exception as e: pass
        
    try: font_channel = ImageFont.truetype(font_path, 55)
    except: font_channel = ImageFont.load_default()
    
    draw = ImageDraw.Draw(bg)
    draw.text((230, 70), "NEW BRAWL NEWS", font=font_channel, fill="white", stroke_width=3, stroke_fill="black")
    return bg

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
        draw.text((65, 410), f"Класс: {role}", font=font_class, fill="#ffcc00", stroke_width=2, stroke_fill="black")

        img_url = b.get("imageUrl")
        if img_url:
            try:
                b_img = Image.open(io.BytesIO(download(img_url))).convert("RGBA")
                b_img.thumbnail((700, 700))
                bg.paste(b_img, (400, 320), b_img)
            except Exception: pass

        draw.text((60, 750), "РЕКОМЕНДУЕМЫЙ БИЛД", font=font_label, fill="#ffcc00", stroke_width=2, stroke_fill="black")
        draw.rounded_rectangle([(60, 800), (170, 910)], radius=15, outline="#ffcc00", width=5)
        draw.rounded_rectangle([(60, 930), (170, 1040)], radius=15, outline="#ffcc00", width=5)
        
        if b.get("gadgets"):
            try:
                g_url = b['gadgets'][0].get('imageUrl')
                if g_url:
                    g_img = Image.open(io.BytesIO(download(g_url))).convert("RGBA")
                    g_img.thumbnail((90, 90))
                    bg.paste(g_img, (70, 810), g_img)
            except Exception: pass
        if b.get("starPowers"):
            try:
                sp_url = b['starPowers'][0].get('imageUrl')
                if sp_url:
                    s_img = Image.open(io.BytesIO(download(sp_url))).convert("RGBA")
                    s_img.thumbnail((90, 90))
                    bg.paste(s_img, (70, 940), s_img)
            except Exception: pass

        draw.text((200, 830), gadget_name, font=font_desc, fill="white", stroke_width=2, stroke_fill="black")
        draw.text((200, 960), sp_name, font=font_desc, fill="white", stroke_width=2, stroke_fill="black")

        out = io.BytesIO()
        bg.convert("RGB").save(out, format="JPEG", quality=95)
        return out.getvalue()
    except Exception as e:
        return b""

def generate_meta_image(rows, images):
    try:
        bg = get_base_image()
        draw = ImageDraw.Draw(bg)
        
        font_path = "LilitaOne-Regular.ttf"
        try:
            font_title = ImageFont.truetype(font_path, 70)
            font_tier = ImageFont.truetype(font_path, 80)
            font_pct = ImageFont.truetype(font_path, 35)
        except:
            font_title = font_tier = font_pct = ImageFont.load_default()

        draw.text((250, 160), "МЕТА BRAWL STARS", font=font_title, fill="#ffcc00", stroke_width=3, stroke_fill="black")

        tiers = [(280, "S", "#cc0044", rows[:4]), (480, "A", "#cc6600", rows[4:8]), (680, "B", "#ccaa00", rows[8:12])]
        
        for y, letter, color, brawlers in tiers:
            draw.rounded_rectangle([(60, y), (1020, y+180)], radius=20, fill=color)
            draw.text((90, y+30), letter, font=font_tier, fill="white", stroke_width=4, stroke_fill="black")
            
            x_offset = 260
            for n, pk, w, wr, lb in brawlers:
                img_url = images.get(n.upper(), "")
                if not img_url: continue
                try:
                    b_img = Image.open(io.BytesIO(download(img_url))).convert("RGBA")
                    b_img.thumbnail((140, 140))
                    bg.paste(b_img, (x_offset, y+20), b_img)
                    draw.text((x_offset+20, y+140), f"{pct(wr)}", font=font_pct, fill="white", stroke_width=2, stroke_fill="black")
                    x_offset += 170  # ИСПРАВЛЕНО: сдвиг только внутри try
                except: pass

        out = io.BytesIO()
        bg.convert("RGB").save(out, format="JPEG", quality=95)
        return out.getvalue()
    except Exception as e:
        return b""
def fetch_guide(src, state):
    try:
        if state.get("daily", {}).get("guides", 0) >= 1:
            return [] 
            
        data = json.loads(get("https://api.brawlapi.com/v1/brawlers"))
        brawlers = data.get("list", [])
        if not brawlers: return []
        
        posted = state.get("posted_guides", [])
        available = [b for b in brawlers if b["name"] not in posted]
        if not available:
            state["posted_guides"] = []
            available = brawlers
            
        b = random.choice(available)
        state.setdefault("posted_guides", []).append(b["name"])
        
        g_name = b["gadgets"][0]["name"] if b.get("gadgets") else "Нет данных"
        s_name = b["starPowers"][0]["name"] if b.get("starPowers") else "Нет данных"
        
        img_bytes = generate_guide_image(b, g_name, s_name)
        
        text = (
            f"🛠 Гаджет: {g_name}\n"
            f"⭐ Звездная сила: {s_name}\n\n"
            f"💡 Совет: Играйте на своей дистанции, следите за картой и не стесняйтесь отступать для восстановления ХП."
        )
        
        return [{
            "id": f"guide:{b['name']}:{int(time.time()/86400)}",
            "title": f"Гайд на бойца: {b['name']}",
            "text": text,
            "img_bytes": img_bytes,
            "link": f"https://brawlify.com/stats/brawlers/{b['id']}",
            "force_link": True,
            "topic": ("📚", "Гайд", "гайд"),
            "custom_disclaimer": "\n\n⚠️ Это универсальная база. В зависимости от карты и врагов, билд стоит менять."
        }]
    except Exception as ex:
        return []

def fetch(src, state):
    t = src["type"]
    if t == "supercell": items = fetch_supercell(src, state)
    elif t == "appstore": items = fetch_appstore(src, state)
    elif t == "youtube": items = fetch_youtube(src, state)
    elif t == "guide": items = fetch_guide(src, state)
    else: items = parse_feed(get(src["url"]))
    if src.get("link"):
        for it in items: it["force_link"] = True
    return items

def fetch_all(sources, state):
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(sources)))) as pool:
        futs = {s["name"]: pool.submit(fetch, s, state) for s in sources}
        for s in sources:
            try: out[s["name"]] = futs[s["name"]].result(timeout=120)
            except Exception as ex: out[s["name"]] = None
    return out

def _fatal_check(ex):
    if ex.code in (401, 403): raise FatalError("Telegram отказал.")

def tg_json(method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try: return urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as ex: _fatal_check(ex); raise

def tg_upload(photo, caption):
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", CHAT), ("caption", caption), ("parse_mode", "HTML")):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="p.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode())
    body.write(photo)
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data=body.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try: return urllib.request.urlopen(req, timeout=60).read()
    except urllib.error.HTTPError as ex: _fatal_check(ex); raise

def check_telegram():
    try:
        me = json.loads(tg_json("getMe", {}))["result"]
        info = json.loads(tg_json("getChatMember", {"chat_id": CHAT, "user_id": me["id"]}))["result"]
    except Exception: return
    status = info.get("status")
    if status not in ("administrator", "creator"): raise FatalError("Бот не админ.")

TOPICS = [
    (r"\bmaintenance\b|\bdowntime\b", "🔧", "Технические работы", "техработы"),
    (r"\bleaks?\b|\bleaked\b|\brumou?rs?\b", "👀", "Утечки и слухи", "утечки"),
    (r"\brelease notes\b|\bpatch\b|\bupdate", "🛠", "Обновление", "обновление"),
    (r"\bworld finals\b|\besports\b", "🏆", "Киберспорт", "киберспорт"),
    (r"\bcrossover|\bcollab\b", "🤝", "Коллаборация", "коллаборация"),
    (r"\bnew brawler|\bintroducing\b", "🦸", "Новый боец", "новыйбоец"),
    (r"\bbrawl talk\b", "🎤", "Brawl Talk", "BrawlTalk"),
    (r"\bskins?\b|\bcosmetic", "🎨", "Скины", "скины"),
]
TOPICS = [(re.compile(p, re.I), e, l, t) for p, e, l, t in TOPICS]
DEFAULT_TOPIC = ("📰", "Новости", "новости")

def item_topic(item, important=True):
    if item.get("topic"): return item["topic"]
    blob = f"{item.get('title', '')} {item.get('text', '')}"
    for pat, emoji, label, tag in TOPICS:
        if pat.search(blob): return emoji, label, tag
    return DEFAULT_TOPIC

PHRASES = {
    "generic": ["Что думаете? 🤔", "А вы что скажете? 💬", "Жмите 🔥, если понравилось!"],
    "guide": ["Пользуетесь таким билдом? 🧐", "Заходит сборка? 🔥", "Пробовали так играть? 🎮"],
}

def pick_phrase(item):
    if item.get("topic") and item["topic"][2] == "гайд":
        return random.choice(PHRASES["guide"])
    return random.choice(PHRASES["generic"])

def link_label(link):
    k = canon(link)
    if k.startswith("yt:"): return "▶️ Смотреть видео"
    return "🔗 Читать подробнее"

def build_post(item, title, text, important, phrase=""):
    esc = html.escape
    emoji, label, tag = item_topic(item, important)
    blocks = []
    if important:
        blocks.append((f"ВАЖНО · {label}", f"<b>🔥 ВАЖНО</b> · {esc(emoji + ' ' + label)}"))
        blocks.append((title, f"<b>{esc(title[:300])}</b>"))
    else:
        blocks.append((f"{emoji} {title}", f"<b>{esc(emoji + ' ' + title[:300])}</b>"))
    if text: blocks.append((text[:600], esc(text[:600])))
    if item.get("custom_disclaimer"):
        plain_disc = re.sub(r"<[^>]+>", "", item["custom_disclaimer"])
        blocks.append((plain_disc, item["custom_disclaimer"]))
    if phrase: blocks.append((phrase, f"<i>{esc(phrase)}</i>"))
    link = item.get("link", "") if (important or item.get("force_link")) else ""
    if link:
        lab = link_label(link)
        blocks.append((lab, f'<a href="{esc(link, quote=True)}">{esc(lab)}</a>'))
    
    blocks.append((f"#BrawlStars #{tag}", esc(f"#BrawlStars #{tag}")))
    return "\n\n".join(b[1] for b in blocks)

def send(item, important, phrase=""):
    title = item["title"] if item.get("title_ru") else translate(item["title"])
    text = translate(item.get("text", ""))
    post = build_post(item, title, text, important, phrase)
    
    if item.get("img_bytes"):
        try:
            tg_upload(item["img_bytes"], post)
            return
        except Exception as ex: print(f"  [ОШИБКА TG] Фото с текстом не ушло: {ex}")
            
    img = item.get("img", "")
    if img:
        try:
            img_data = download(img)
            if len(img_data) > 15000:
                tg_upload(img_data, post)
                return
        except Exception as ex: print(f"  [ОШИБКА TG] Картинка не ушла: {ex}")

    try:
        tg_json("sendMessage", {"chat_id": CHAT, "text": post, "parse_mode": "HTML"})
    except Exception as ex:
        print(f"  [ОШИБКА TG] Текст не ушел: {ex}")

BS_BASE = os.environ.get("BS_API_BASE", "https://bsproxy.royaleapi.dev/v1")
BS_KEY = os.environ.get("BRAWL_API_KEY", "")
FORCE_META = os.environ.get("FORCE_META", "").strip().lower() in ("1", "true", "yes")
META_HOUR_UTC = 7
META_PLAYERS = 200
META_WINDOW_H = 24
META_MIN_PICKS = 25
META_MODE_MIN_BATTLES = 100
META_RETRY_MIN = 60
META_TYPES = {"ranked", "soloRanked", "teamRanked"}

MODES_RU = {"gemGrab": "Захват кристаллов", "brawlBall": "Броулбол", "heist": "Ограбление", "hotZone": "Горячая зона", "knockout": "Нокаут", "bounty": "Награда за поимку"}
CLASSES_RU = {"Damage Dealer": "Бойцы урона", "Tank": "Танки", "Support": "Поддержка", "Controller": "Контролёры", "Assassin": "Убийцы", "Marksman": "Снайперы", "Artillery": "Артиллерия"}

def bs_api(path):
    raw = get(BS_BASE + path, {"Authorization": "Bearer " + BS_KEY, "Accept": "application/json"}, tries=3)
    return json.loads(raw)

def mode_name(m): return MODES_RU.get(m) or re.sub(r"(?<!^)(?=[A-Z])", " ", m).title()
def parse_bt(s): return datetime.datetime.strptime(s[:15], "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)

def aggregate(logs, now, types=META_TYPES):
    seen, stats, modes, battles = set(), {}, {}, 0
    for owner, items in logs.items():
        for it in items or []:
            b = it.get("battle") or {}
            if (types is not None and b.get("type") not in types) or b.get("type") in ("friendly", "tournament"): continue
            teams = b.get("teams")
            if not (isinstance(teams, list) and len(teams) == 2) or b.get("result") not in ("victory", "defeat"): continue
            try: when = parse_bt(it["battleTime"])
            except Exception: continue
            if (now - when).total_seconds() > META_WINDOW_H * 3600: continue
            idx = next((i for i, tm in enumerate(teams) if any(p.get("tag") == owner for p in tm)), None)
            if idx is None: continue
            key = it["battleTime"] + "|" + ",".join(sorted(p.get("tag", "") for tm in teams for p in tm))
            if key in seen: continue
            seen.add(key)
            winner = idx if b["result"] == "victory" else 1 - idx
            mode = b.get("mode") or (it.get("event") or {}).get("mode") or "unknown"
            md = modes.setdefault(mode, {"n": 0, "b": {}})
            md["n"] += 1
            battles += 1
            for i, tm in enumerate(teams):
                for p in tm:
                    name = (p.get("brawler") or {}).get("name")
                    if not name: continue
                    won = 1 if i == winner else 0
                    for d in (stats, md["b"]):
                        row = d.setdefault(name, [0, 0])
                        row[0] += 1
                        row[1] += won
    return {"battles": battles, "stats": stats, "modes": modes}

def wilson_lb(wins, n, z=1.0):
    if n <= 0: return 0.0
    p = wins / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n)

def pct(x, digits=1): return f"{x * 100:.{digits}f}".replace(".", ",") + "%"

def rank_rows(stats, min_picks):
    rows = [(nice_name(n), pk, w, w / pk, wilson_lb(w, pk)) for n, (pk, w) in stats.items() if pk >= min_picks]
    return sorted(rows, key=lambda r: r[4], reverse=True)

def meta_analysis(agg, rows, prev, classes, deltas):
    out, total = [], agg["battles"] * 6
    if not rows: return out
    lead = rows[0]
    pr = lead[1] / total
    s = f"{lead[0]} лидирует: {pct(lead[3])} побед при пикрейте {pct(pr)}."
    if pr < 0.01: s += " Берут его редко, так что это скорее нишевая сила, чем универсальный выбор."
    elif pr >= 0.025: s += " Его не только часто берут, но и выигрывают: это настоящий мета-пик."
    out.append(s)
    pop = max(rows, key=lambda r: r[1])
    if pop[0] != lead[0]:
        ppr = pop[1] / total
        if pop[3] < 0.48: out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)} пикрейта) выигрывает только {pct(pop[3])}: его берут скорее по привычке, чем из-за силы.")
        elif pop[3] >= 0.53: out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)}) ещё и силён: {pct(pop[3])} побед.")
        else: out.append(f"Самый популярный боец {pop[0]} ({pct(ppr)}) показывает средний результат: {pct(pop[3])} побед.")
    risers = [d for d in deltas if d[1] > 0][:1]
    fallers = [d for d in deltas if d[1] < 0][:1]
    if risers: out.append(f"За сутки заметнее всех вырос {risers[0][0]}: {risers[0][1] * 100:+.1f} п.п. побед.".replace(".", ","))
    if fallers: out.append(f"Сильнее всех просел {fallers[0][0]}: {fallers[0][1] * 100:+.1f} п.п. побед.".replace(".", ","))
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
                out.append(f"Из классов впереди «{CLASSES_RU[best]}» ({pct(good[best])}), позади «{CLASSES_RU[worst]}» ({pct(good[worst])}).")
    return out

def build_meta_post(agg, prev=None, classes=None, min_picks=META_MIN_PICKS, limit=1020):
    esc = html.escape
    rows = rank_rows(agg["stats"], min_picks)
    if len(rows) < 5: return ""
    total = agg["battles"] * 6
    head = ("📊 <b>МЕТА BRAWL STARS</b> · топ-игроки мира\n"
            f"<i>{agg['battles']} боёв за последние сутки</i>")
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
                if abs(d) >= 0.03: deltas.append((nice_name(name), d))
        deltas.sort(key=lambda x: x[1], reverse=True)
    movers = []
    ups = [d for d in deltas if d[1] > 0][:3]
    downs = [d for d in reversed(deltas) if d[1] < 0][:3]
    if ups: movers.append("📈 <b>Растут за сутки</b>: " + ", ".join(f"{esc(n)} ({d * 100:+.1f} п.п.)".replace(".", ",") for n, d in ups))
    if downs: movers.append("📉 <b>Падают за сутки</b>: " + ", ".join(f"{esc(n)} ({d * 100:+.1f} п.п.)".replace(".", ",") for n, d in downs))
    modes = ["🧭 <b>По режимам</b>"]
    for mode, md in sorted(agg["modes"].items(), key=lambda kv: kv[1]["n"], reverse=True):
        if md["n"] < META_MODE_MIN_BATTLES: continue
        mrows = rank_rows(md["b"], max(8, md["n"] // 40))
        if len(mrows) >= 3: modes.append(f"• <b>{esc(mode_name(mode))}</b>: " + ", ".join(esc(r[0]) for r in mrows[:3]))
    analysis = meta_analysis(agg, rows, prev, classes, deltas)
    why = ["💡 <b>Разбор</b>"] + [f"• {esc(a)}" for a in analysis] if analysis else []
    foot = ("<i>Это мета лучших игроков мира.</i>\n\n#BrawlStars #мета")
    
    # ИСПРАВЛЕНО: Выкидываем блоки, пока не влезет в лимит
    sections = [top, popular, movers, why, modes]
    while True:
        body = "\n\n".join("\n".join(sec) for sec in sections if sec)
        text = head + "\n\n" + body + "\n\n" + foot
        if len(text) <= limit or not sections: return text
        sections.pop() # Выкидываем "По режимам", потом "Разбор", потом "Падают" и т.д.

def load_classes(state):
    c = state.get("classes") or {}
    if c.get("map") and time.time() - c.get("ts", 0) < 7 * 86400:
        return c["map"], c.get("images", {})
    try:
        data = json.loads(get("https://api.brawlapi.com/v1/brawlers"))
        mapping = {}
        images = {}
        for b in data.get("list", []):
            cls = (b.get("class") or {}).get("name")
            img = b.get("imageUrl")
            if b.get("name"):
                if cls: mapping[b["name"].upper()] = cls
                if img: images[b["name"].upper()] = img
        if mapping:
            state["classes"] = {"ts": time.time(), "map": mapping, "images": images}
            return mapping, images
    except Exception as ex: pass
    return c.get("map") or {}, c.get("images", {})

def collect_meta(deadline, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    players = bs_api(f"/rankings/global/players?limit={META_PLAYERS}").get("items", [])
    tags = [p["tag"] for p in players if p.get("tag")]
    auth_fail = []
    def one(tag):
        if time.time() > deadline: return None
        try: return bs_api("/players/" + urllib.parse.quote(tag, safe="") + "/battlelog").get("items", [])
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403): auth_fail.append(ex.code)
            return None
        except Exception: return None
    with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(one, tags))
    if auth_fail: raise PermissionError("API отклонил ключ")
    logs = {t: r for t, r in zip(tags, results) if r is not None}
    agg = aggregate(logs, now)
    if agg["battles"] < 150: agg = aggregate(logs, now, types=None)
    return agg, len(logs), len(tags)

def maybe_post_meta(state, started, force=False):
    if not BS_KEY: 
        print("  [МЕТА] Отключена: нет BRAWL_API_KEY")
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    
    if not force:
        if state.get("meta_posted") == today: 
            print("  [МЕТА] Уже постили сегодня (пропуск)")
            return False
        if now.hour < META_HOUR_UTC: 
            print("  [МЕТА] Еще рано (ждем 10:00 МСК)")
            return False
            
    state["meta_try"] = time.time()
    try: 
        agg, ok, total = collect_meta(time.time() + 150, now)
    except PermissionError: 
        print("  [МЕТА] API отклонил ключ (проверь IP 45.79.218.79)")
        return False
    except Exception as ex: 
        print(f"  [МЕТА] Ошибка сбора данных: {ex}")
        return False
        
    if total == 0 or ok < total * 0.6 or agg["battles"] < 150: 
        print(f"  [МЕТА] Мало данных (журналов {ok}/{total}, боёв {agg['battles']})")
        return False
        
    try: register_names(n for n in agg["stats"])
    except Exception: pass
    
    classes, images = load_classes(state)
    prev = state.get("meta") or {}
    
    rows = rank_rows(agg["stats"], META_MIN_PICKS)
    # ИСПРАВЛЕНО: limit=1020 гарантирует, что текст влезет в подпись Телеграма
    text = build_meta_post(agg, prev, classes, limit=1020)
    
    if not text: 
        print("  [МЕТА] Слишком мало бойцов в рейтинге для поста")
        return False
        
    img_bytes = generate_meta_image(rows, images)
    
    if img_bytes:
        try:
            tg_upload(img_bytes, text)
            print("  [МЕТА] Успешно опубликована с картинкой!")
        except Exception as ex:
            print(f"  [МЕТА] Ошибка отправки картинки: {ex}. Шлю текстом...")
            try: tg_json("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML"})
            except Exception as e: print(f"  [МЕТА] Текст тоже не ушел: {e}")
    else:
        print("  [МЕТА] Картинка не сгенерировалась, шлю текст.")
        try: tg_json("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML"})
        except Exception as e: print(f"  [МЕТА] Текст не ушел: {e}")
        
    state["meta"] = {"date": today, "stats": agg["stats"], "battles": agg["battles"]}
    state["meta_posted"] = today
    return True

def load_state():
    state = {"seen": [], "sources": [], "daily": {}, "yt": {}, "recent": [], "fails": {}, "phrases": [],
             "meta": {}, "meta_posted": "", "meta_try": 0, "classes": {}, "posted_guides": []}
    if os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE))
            if isinstance(data, dict): state.update(data)
        except Exception: pass
    return state

def save_state(state):
    state["seen"] = state["seen"][-SEEN_KEEP:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

def main():
    started = time.time()
    if not TOKEN or not CHAT: sys.exit(1)
    try: check_telegram()
    except FatalError: sys.exit(1)
    
    state = load_state()
    seen = set(state["seen"])
    fails = state["fails"]
