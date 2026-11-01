"""Фабрика коротких вертикальных рекламных видео для канала (TikTok, YouTube Shorts, Reels, VK Клипы).

Что делает: берёт данные самого канала (мета бойцов, гайд дня), собирает динамичный ролик 1080x1920:
анимированный фон, говорящий ведущий-робот с микрофоном (рот двигается в такт голосу), реплики в пузыре,
крупный цветной текст, быстрые смены кадров, звуки переходов, тихая музыка и QR-код канала в конце.
Готовый ролик присылается в личный чат с ботом, остаётся нажать «Поделиться → TikTok».

Запуск:  python make_video.py auto     ролики, которые готовы на сегодня (мета, гайд)
         python make_video.py meta     мета, даже если сегодня уже была
         python make_video.py guide    гайд на бойца, даже если сегодня уже был
         python make_video.py demo     тестовый ролик (проверка озвучки, сборки и доставки)
Нужны: ffmpeg, Pillow, edge-tts (запасной gTTS), файл bot.py рядом.
"""
import os, sys, io, re, json, time, math, wave, array, shutil, subprocess, tempfile, datetime, asyncio, uuid, random
import urllib.request, urllib.parse, urllib.error

import bot

# ===================== НАСТРОЙКИ И ЛИМИТЫ =====================
W, H, FPS = 1080, 1920, 24       # 24 кадра в секунду: плавно и быстро собирается
VIDEO_STATE = "videos.json"
VIDEO_DAILY_LIMIT = 2            # не больше двух роликов в сутки: так канал не выглядит спамом
MAX_VIDEO_SEC = 50               # короткие ролики досматривают чаще
MIN_SLIDE_SEC = 1.6              # быстрый монтаж: кадры сменяются каждые 2-3 секунды
CTA_MIN_SEC = 5.0                # финальный кадр с QR держим дольше, чтобы успели навести камеру
PAD_SEC = 0.12                   # пауза после фразы
POP_FRAMES = 9                   # длина анимации появления кадра
OUT_DIR = "out"
MUSIC_DIR = "music"              # сюда клади СВОИ треки или музыку без авторских прав
MUSIC_VOLUME, SFX_VOLUME = 0.12, 0.30
TTS_VOICE = os.environ.get("TTS_VOICE") or "ru-RU-DmitryNeural"
TTS_RATE = os.environ.get("TTS_RATE") or "+22%"      # быстрее обычного: ролик должен звучать бодро
GTTS_TEMPO = 1.25                # запасной голос Google ускоряем сами
VIDEO_CHAT_ID = os.environ.get("VIDEO_CHAT_ID", "")  # личный чат, куда бот пришлёт готовые ролики
CTA_HANDLE = os.environ.get("VIDEO_CTA_HANDLE") or (bot.CHAT if bot.CHAT.startswith("@") else "")
QR_URL = os.environ.get("VIDEO_QR_URL") or (f"https://t.me/{CTA_HANDLE.lstrip('@')}" if CTA_HANDLE else "")
HASHTAGS = "#brawlstars #бравлстарс #бс #мета #гайд"
TT_BASE = "https://open.tiktokapis.com"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/russoone/RussoOne-Regular.ttf"  # жирный шрифт с кириллицей

# палитра ролика
YELLOW, ORANGE, DARK = (255, 214, 64), (255, 140, 0), (38, 14, 86)
PURPLE, PINK, BLUE, GREEN, RED = (140, 80, 255), (245, 95, 165), (60, 150, 255), (40, 190, 110), (235, 70, 70)


# ===================== QR-КОД (свой кодировщик, без библиотек) =====================
# версия: (всего кодовых слов, EC-слов на блок, блоков, байт данных в блоке)
_VER = {1: (26, 10, 1, 16), 2: (44, 16, 1, 28), 3: (70, 26, 1, 44), 4: (100, 18, 2, 32),
        5: (134, 24, 2, 43), 6: (172, 16, 4, 27)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_remainder(data, n):
    gen = [1]
    for i in range(n):  # порождающий многочлен степени n
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        gen = nxt
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(n):
            rem[i] ^= _mul(gen[i + 1], f)
    return rem


def _bch(value, poly, bits_total, bits_data):
    v = value << (bits_total - bits_data)
    for i in range(bits_data - 1, -1, -1):
        if v >> (i + bits_total - bits_data) & 1:
            v ^= poly << i
    return (value << (bits_total - bits_data)) | v


def _mask_fn(m):
    return [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
            lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
            lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
            lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0][m]


def _penalty(m):
    n, p = len(m), 0
    for rows in (m, [list(col) for col in zip(*m)]):
        for line in rows:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                    if run == 5:
                        p += 3
                    elif run > 5:
                        p += 1
                else:
                    run = 1
            s = "".join("1" if v else "0" for v in line)
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    dark = sum(sum(row) for row in m) * 100 // (n * n)
    return p + 10 * (abs(dark - 50) // 5)


def make_qr(text):
    """Возвращает матрицу модулей (список строк из True/False) без белой рамки."""
    data = text.encode("utf-8")
    ver = next((v for v, (tot, ec, blocks, per) in _VER.items() if len(data) <= blocks * per - 2), None)
    if ver is None:
        raise ValueError("текст слишком длинный для мини-QR")
    tot, ec, blocks, per = _VER[ver]
    cap = blocks * per
    bits = "0100" + format(len(data), "08b") + "".join(format(b, "08b") for b in data)
    bits += "0" * min(4, cap * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(words) < cap:
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    blks = [words[i * per:(i + 1) * per] for i in range(blocks)]
    ecs = [_rs_remainder(b, ec) for b in blks]
    final = [b[i] for i in range(per) for b in blks] + [e[i] for i in range(ec) for e in ecs]
    stream = "".join(format(w, "08b") for w in final)

    n = 17 + 4 * ver
    mat = [[False] * n for _ in range(n)]
    fn = [[False] * n for _ in range(n)]  # функциональные модули

    def put(r, c, v):
        if 0 <= r < n and 0 <= c < n:
            mat[r][c], fn[r][c] = v, True
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):  # поисковые узоры с разделителями
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                inside = 0 <= dr < 7 and 0 <= dc < 7
                ring = inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                put(r0 + dr, c0 + dc, ring)
    for i in range(8, n - 8):  # линии синхронизации
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    pos = _ALIGN[ver]
    for r in pos:
        for c in pos:
            if (r, c) in ((6, 6), (6, pos[-1]), (pos[-1], 6)) and len(pos) == 2 and (r, c) != (pos[-1], pos[-1]):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    put(n - 8, 8, True)  # тёмный модуль
    for i in range(9):  # резерв под информацию о формате
        for (r, c) in ((8, i), (i, 8)):
            if not fn[r][c]:
                put(r, c, False)
    for i in range(8):
        for (r, c) in ((8, n - 1 - i), (n - 1 - i, 8)):
            if not fn[r][c]:
                put(r, c, False)

    # данные зигзагом справа налево
    bit_i, up, c = 0, True, n - 1
    cells = []
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                if not fn[r][cc]:
                    cells.append((r, cc))
        up, c = not up, c - 2
    best = None
    for mask in range(8):
        m = [row[:] for row in mat]
        f = _mask_fn(mask)
        for i, (r, cc) in enumerate(cells):
            v = stream[i] == "1" if i < len(stream) else False
            m[r][cc] = v != f(r, cc)
        fmt = _bch((0b00 << 3) | mask, 0b10100110111, 15, 5) ^ 0b101010000010010  # уровень M
        for i in range(15):
            v = bool(fmt >> i & 1)
            # первая копия: вокруг левого верхнего угла
            if i < 6:
                m[i][8] = v
            elif i == 6:
                m[7][8] = v
            elif i == 7:
                m[8][8] = v
            elif i == 8:
                m[8][7] = v
            else:
                m[8][14 - i] = v
            # вторая копия: у правого верхнего и левого нижнего углов
            if i < 8:
                m[8][n - 1 - i] = v
            else:
                m[n - 15 + i][8] = v
        sc = _penalty(m)
        if best is None or sc < best[0]:
            best = (sc, m)
    return best[1]



def qr_tile(url, size=440):
    """Плитка с QR-кодом: белая подложка, тёмные модули, скруглённые углы."""
    m = make_qr(url)
    n, quiet = len(m), 3
    cell = max(2, size // (n + 2 * quiet))
    side = cell * (n + 2 * quiet)
    img = bot.Image.new("RGBA", (side, side), (255, 255, 255, 255))
    d = bot.ImageDraw.Draw(img)
    for r in range(n):
        for c in range(n):
            if m[r][c]:
                x, y = (c + quiet) * cell, (r + quiet) * cell
                d.rectangle((x, y, x + cell - 1, y + cell - 1), fill=DARK + (255,))
    mask = bot.Image.new("L", img.size, 0)
    bot.ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 12, fill=255)
    img.putalpha(mask)
    return img


# ===================== ТЕКСТ ДЛЯ ОЗВУЧКИ =====================
NAME_SAY = {
    "Shelly": "Шелли", "Colt": "Кольт", "Bull": "Булл", "Brock": "Брок", "El Primo": "Эль Примо", "Poco": "Поко",
    "Rosa": "Роза", "Mortis": "Мортис", "Crow": "Кроу", "Leon": "Леон", "Spike": "Спайк", "Colette": "Колетт",
    "Max": "Макс", "Lola": "Лола", "Piper": "Пайпер", "Edgar": "Эдгар", "Bibi": "Биби", "BiBi": "Биби",
    "Gene": "Джин", "Surge": "Сёрдж", "Amber": "Эмбер", "Chuck": "Чак", "Charlie": "Чарли", "Gale": "Гейл",
    "Jessie": "Джесси", "Dynamike": "Динамайк", "Nita": "Нита", "Barley": "Барли", "Tick": "Тик", "Rico": "Рико",
    "Darryl": "Дэррил", "Penny": "Пенни", "Carl": "Карл", "Jacky": "Джеки", "Gus": "Гас", "Emz": "Эмз",
    "Stu": "Сту", "Pam": "Пэм", "Frank": "Фрэнк", "Bea": "Бия", "Nani": "Нани", "Griff": "Грифф", "Grom": "Гром",
    "Bonnie": "Бонни", "Belle": "Белль", "Ash": "Эш", "Sam": "Сэм", "Mandy": "Мэнди", "Maisie": "Мэйзи",
    "Hank": "Хэнк", "Pearl": "Перл", "Angelo": "Анджело", "Berry": "Берри", "Shade": "Шейд", "Sandy": "Сэнди",
    "Meg": "Мэг", "Chester": "Честер", "Kit": "Кит", "Draco": "Драко", "Kenji": "Кэндзи", "Tara": "Тара",
    "Sprout": "Спраут", "Byron": "Байрон", "Squeak": "Сквик", "Lou": "Лу", "Buzz": "Базз", "Fang": "Фэнг",
    "Eve": "Ива", "Janet": "Джанет", "Otis": "Отис", "Buster": "Бастер", "Gray": "Грей", "Willow": "Уиллоу",
    "Doug": "Даг", "Lumi": "Луми", "Najia": "Наджия", "Damian": "Дамиан", "Sirius": "Сириус", "Pierce": "Пирс",
    "Larry & Lawrie": "Ларри и Лори", "Starr Nova": "Старр Нова", "Mr. P": "Мистер Пи", "8-Bit": "Восемь бит",
}
_LAT = [("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ck", "к"), ("kh", "х"), ("zh", "ж"),
        ("ee", "и"), ("oo", "у"), ("ou", "ау"), ("ea", "и"), ("ie", "и"), ("qu", "кв"), ("a", "а"), ("b", "б"),
        ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"),
        ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"),
        ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "и"), ("z", "з")]


def translit(word):
    """Грубая транслитерация, чтобы русский голос не читал латиницу по буквам."""
    w, out, i = word.lower(), "", 0
    while i < len(w):
        for lat, cyr in _LAT:
            if w.startswith(lat, i):
                out += cyr
                i += len(lat)
                break
        else:
            out += w[i]
            i += 1
    return out.capitalize()


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def spoken(text):
    """Текст для голоса: имена и числа в удобном для озвучки виде (на экране остаётся оригинал)."""
    t = text
    for name in sorted(set(list(NAME_SAY) + list(bot.BRAWLERS)), key=len, reverse=True):
        say = NAME_SAY.get(name) or translit(name)
        t = re.sub(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", say, t)
    for lat, cyr in (("Brawl Stars", "Бравл Старс"), ("Brawl Pass", "Бравл Пасс"), ("Supercell", "Суперселл"),
                     ("TikTok", "Тик Ток"), ("Telegram", "Телеграм"), ("YouTube", "Ютуб"), ("QR", "кью ар")):
        t = re.sub(re.escape(lat), cyr, t, flags=re.I if lat != "QR" else 0)

    def pct(m):
        num = m.group(1)
        if re.fullmatch(r"\d+", num):
            return f"{num} " + plural(int(num), "процент", "процента", "процентов")
        return f"{num} процента"
    t = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", pct, t)
    t = re.sub(r"п\.\s?п\.", "процентных пунктов", t)
    t = re.sub(r"[#@][\w_]+", "", t)
    t = re.sub(r"[^\w\s.,!?:;()\-—«»\"'…]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ===================== ОЗВУЧКА И ЗВУК =====================
_ENGINE = {}


def tts(text, out_path):
    """Озвучка: сначала бесплатный голос Microsoft Edge (edge-tts), затем запасной Google (gTTS).
    Возвращает путь к файлу или None (тогда ролик собирается без голоса, только с текстом)."""
    last = None
    try:
        import edge_tts

        async def run_edge():
            await edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE).save(out_path)

        for attempt in range(3):
            try:
                asyncio.run(run_edge())
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                    _ENGINE[out_path] = "edge"
                    return out_path
            except Exception as ex:
                last = ex
                time.sleep(2 * (attempt + 1))
        print("озвучка Microsoft не получилась:", last)
    except Exception as ex:
        print("озвучка Microsoft недоступна:", ex)
    try:  # запасной вариант: голос Google (звучит проще, но работает почти всегда)
        from gtts import gTTS
        gTTS(text, lang="ru").save(out_path)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            _ENGINE[out_path] = "gtts"
            print("использована запасная озвучка gTTS")
            return out_path
    except Exception as ex:
        print("запасная озвучка тоже не получилась:", ex)
    return None


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, check=True, capture_output=True, **kw)
    except subprocess.CalledProcessError as ex:
        raise RuntimeError(f"{cmd[0]} завершился с ошибкой: " + (ex.stderr or b"").decode(errors="replace")[-300:]) from ex


def audio_duration(path):
    try:
        out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).stdout
        return float(out.decode().strip())
    except Exception:
        if path.endswith(".wav"):
            with wave.open(path) as w:
                return w.getnframes() / w.getframerate()
        raise


_TRIM = ("silenceremove=start_periods=1:start_threshold=-48dB:start_silence=0.02,areverse,"
         "silenceremove=start_periods=1:start_threshold=-48dB:start_silence=0.02,areverse")


def clean_voice(src, dst):
    """Обрезает тишину в начале и конце фразы (ролик не тянется) и при необходимости ускоряет голос."""
    tempo = GTTS_TEMPO if _ENGINE.get(src) == "gtts" else 1.0
    af = _TRIM + (f",atempo={tempo}" if tempo != 1.0 else "")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", af, "-ar", "44100", "-ac", "2", dst])
    return dst


def envelope(wav_path, fps, n_frames):
    """Громкость голоса по кадрам (0..1): по ней двигается рот ведущего."""
    try:
        with wave.open(wav_path) as w:
            ch, sr, raw = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
        samples = array.array("h")
        samples.frombytes(raw[:len(raw) // 2 * 2])
        mono = samples[::ch] if ch > 1 else samples
        win = max(1, int(sr / fps))
        vals = []
        for k in range(n_frames):
            seg = mono[k * win:(k + 1) * win]
            vals.append(math.sqrt(sum(x * x for x in seg) / len(seg)) if len(seg) else 0.0)
        top = sorted(vals)[int(len(vals) * 0.9)] if vals else 0
        if top <= 0:
            return [0.0] * n_frames
        out, prev = [], 0.0
        for v in vals:
            cur = max(v / top, prev * 0.55)  # небольшое сглаживание: рот не дёргается
            out.append(min(1.0, cur))
            prev = cur
        return out
    except Exception:
        return [0.0] * n_frames


def mouth_state(v):
    return 0 if v < 0.10 else 1 if v < 0.32 else 2 if v < 0.62 else 3


def pick_music(vstate):
    """Следующий трек из папки music по кругу (только свои или свободные треки!)."""
    if not os.path.isdir(MUSIC_DIR):
        return None
    tracks = sorted(f for f in os.listdir(MUSIC_DIR) if f.lower().endswith((".mp3", ".wav", ".m4a", ".ogg")))
    if not tracks:
        return None
    idx = int(vstate.get("music_idx", 0)) % len(tracks)
    vstate["music_idx"] = (idx + 1) % len(tracks)
    return os.path.join(MUSIC_DIR, tracks[idx])


# ===================== ШРИФТ, ТЕКСТ, ФОН =====================
def setup_fonts():
    """Скачивает жирный шрифт Russo One (кириллица) и подключает его; без сети остаются обычные шрифты."""
    path = os.path.join("fonts", "RussoOne-Regular.ttf")
    try:
        if not os.path.exists(path):
            os.makedirs("fonts", exist_ok=True)
            data = bot.get(FONT_URL, limit=2_000_000)
            if len(data) < 30_000:
                return False
            with open(path, "wb") as f:
                f.write(data)
        f = bot.ImageFont.truetype(path, 60)
        shots = []
        for sample in ("ГАЙД", "ЖЩЮЯ"):
            im = bot.Image.new("L", (400, 120), 0)
            bot.ImageDraw.Draw(im).text((10, 10), sample, font=f, fill=255)
            shots.append(im.tobytes())
        if shots[0] == shots[1]:
            return False  # в шрифте нет кириллицы: вместо букв были бы одинаковые квадраты
        if path not in bot.FONT_PATHS:
            bot.FONT_PATHS.insert(1, path)  # после пользовательского font.ttf
        return True
    except Exception as ex:
        print("красивый шрифт не подключился, беру обычный:", ex)
        return False


def have_graphics():
    return bot.Image is not None and bot.card_font(40) is not None


def _vgrad(w, h, top, bottom):
    Image = bot.Image
    return Image.composite(Image.new("RGB", (w, h), bottom), Image.new("RGB", (w, h), top),
                           Image.linear_gradient("L").resize((w, h)))


def grad_text(layer, xy, text, font, top=(255, 244, 150), bottom=ORANGE, stroke=9, outline=DARK, anchor="mm"):
    """Крупный текст: градиентная заливка, толстая обводка и тень (рисуется на прозрачном слое)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    d = ImageDraw.Draw(layer)
    l, t, r, b = d.textbbox(xy, text, font=font, anchor=anchor, stroke_width=stroke)
    pad = 24
    box = (l - pad, t - pad, r + pad + 10, b + pad + 14)
    w, h = box[2] - box[0], box[3] - box[1]
    ox, oy = xy[0] - box[0], xy[1] - box[1]
    tmp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    td.text((ox, oy + 10), text, font=font, fill=(8, 3, 28, 190), anchor=anchor, stroke_width=stroke, stroke_fill=(8, 3, 28, 190))
    td.text((ox, oy), text, font=font, fill=outline + (255,), anchor=anchor, stroke_width=stroke, stroke_fill=outline + (255,))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((ox, oy), text, font=font, fill=255, anchor=anchor)
    tmp.paste(_vgrad(w, h, top, bottom), (0, 0), mask)
    region = Image.alpha_composite(layer.crop(box), tmp)
    layer.paste(region, (box[0], box[1]))


def make_bg(seed=0):
    """Большой фон ролика (двигается за кадром): градиент, цветные пятна и световые полосы."""
    Image, ImageDraw, ImageFilter = bot.Image, bot.ImageDraw, bot.ImageFilter
    rnd = random.Random(seed)
    bw, bh = W + 220, H + 320
    img = _vgrad(bw, bh, (48, 18, 120), (8, 5, 36)).convert("RGBA")
    small = Image.new("RGBA", (bw // 4, bh // 4), (0, 0, 0, 0))  # пятна рисуем мелко и растягиваем: так быстро и мягко
    sd = ImageDraw.Draw(small)
    for color in (PURPLE, BLUE, PINK, (0, 200, 170), ORANGE, PURPLE):
        r = rnd.randint(110, 200)
        x, y = rnd.randint(-40, bw // 4 - r), rnd.randint(-40, bh // 4 - r)
        sd.ellipse((x, y, x + r, y + r), fill=color + (rnd.randint(110, 150),))
    blobs = small.filter(ImageFilter.GaussianBlur(34)).resize((bw, bh), Image.BICUBIC)
    img = Image.alpha_composite(img, blobs)
    stripes = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    st = ImageDraw.Draw(stripes)
    for i in range(-4, 9):
        x = i * 260
        st.polygon([(x, 0), (x + 90, 0), (x - 500 + 90, bh), (x - 500, bh)], fill=(255, 255, 255, 14))
    return Image.alpha_composite(img, stripes).convert("RGB")


def make_particles(seed=0, count=16):
    """Мелкие светящиеся шарики и звёздочки, медленно плывущие вверх."""
    Image, ImageDraw, ImageFilter = bot.Image, bot.ImageDraw, bot.ImageFilter
    rnd = random.Random(seed + 99)
    parts = []
    for i in range(count):
        size = rnd.choice((28, 38, 54, 72))
        sp = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sp)
        if i % 3 == 0:  # четырёхлучевая звёздочка
            c = size
            sd.polygon([(c, 4), (c + size * .18, c - size * .18), (size * 2 - 4, c), (c + size * .18, c + size * .18),
                        (c, size * 2 - 4), (c - size * .18, c + size * .18), (4, c), (c - size * .18, c - size * .18)],
                       fill=(255, 255, 255, 190))
        else:
            color = rnd.choice(((255, 255, 255), YELLOW, (150, 220, 255), PINK))
            sd.ellipse((size * .4, size * .4, size * 1.6, size * 1.6), fill=color + (120,))
            sp = sp.filter(ImageFilter.GaussianBlur(size * .12))
        parts.append({"sp": sp, "x": rnd.randint(0, W), "y": rnd.randint(0, H + 200), "v": rnd.randint(40, 120),
                      "sway": rnd.randint(14, 50), "ph": rnd.random() * 6.28})
    return parts


# ===================== ВЕДУЩИЙ: РОБОТ С МИКРОФОНОМ =====================
MASCOT_SIZE = (340, 510)


def make_mascot():
    """Спрайты ведущего: 4 положения рта x открытые/закрытые глаза. Рисуем крупно и уменьшаем: края гладкие."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    S = 3
    dark = DARK + (255,)

    def draw(mouth, blink):
        im = Image.new("RGBA", (400 * S, 600 * S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        sc = lambda *v: [x * S for x in v]

        def rr(x0, y0, x1, y1, r, fill, w=7):
            d.rounded_rectangle(sc(x0, y0, x1, y1), radius=r * S, fill=fill, outline=dark, width=w * S)

        def circ(cx, cy, r, fill, w=6):
            d.ellipse(sc(cx - r, cy - r, cx + r, cy + r), fill=fill, outline=dark if w else None, width=w * S)

        def limb(x0, y0, x1, y1, width, fill):
            d.line(sc(x0, y0, x1, y1), fill=dark, width=(width + 14) * S)
            for x, y in ((x0, y0), (x1, y1)):
                circ(x, y, (width + 14) / 2, dark, 0)
            d.line(sc(x0, y0, x1, y1), fill=fill, width=width * S)
            for x, y in ((x0, y0), (x1, y1)):
                circ(x, y, width / 2, fill, 0)
        body, white = PURPLE + (255,), (238, 244, 255, 255)
        d.line(sc(200, 66, 200, 24), fill=dark, width=14 * S)  # антенна
        circ(200, 18, 24, YELLOW + (255,))
        for cx in (58, 342):  # уши
            circ(cx, 195, 30, body)
            circ(cx, 195, 13, (200, 165, 255, 255), 0)
        rr(92, 335, 308, 592, 72, body, 8)  # корпус
        rr(135, 398, 265, 510, 30, YELLOW + (255,), 6)
        for cx, col in ((166, RED), (200, GREEN), (234, BLUE)):
            circ(cx, 428, 12, col + (255,), 4)
        rr(152, 468, 248, 486, 9, (255, 255, 255, 255), 4)
        limb(112, 392, 62, 512, 46, body)  # левая рука
        circ(58, 526, 27, white)
        rr(336, 196, 378, 284, 18, (205, 205, 225, 255), 6)  # микрофон
        circ(357, 176, 36, (62, 52, 96, 255))
        circ(346, 165, 10, (255, 255, 255, 120), 0)
        rr(332, 262, 382, 278, 8, YELLOW + (255,), 5)
        limb(286, 388, 346, 306, 46, body)  # правая рука с микрофоном
        circ(358, 294, 28, white)
        rr(68, 60, 332, 332, 104, white, 8)  # голова
        rr(96, 104, 304, 218, 56, (28, 14, 66, 255), 5)  # визор
        eye = (90, 235, 255, 255)
        for cx in (150, 250):
            if blink:
                d.rounded_rectangle(sc(cx - 24, 158, cx + 24, 168), radius=5 * S, fill=eye)
            else:
                d.ellipse(sc(cx - 24, 128, cx + 24, 190), fill=eye)
                d.ellipse(sc(cx - 14, 138, cx - 2, 152), fill=(255, 255, 255, 255))
        for cx in (112, 288):  # щёки
            d.ellipse(sc(cx - 17, 246, cx + 17, 278), fill=(255, 160, 190, 255))
        mc = (50, 18, 82, 255)
        if mouth == 0:
            d.rounded_rectangle(sc(168, 272, 232, 283), radius=5 * S, fill=mc)
        else:
            rx, ry, cy = ((28, 12, 277), (34, 22, 281), (38, 34, 285))[mouth - 1]
            d.ellipse(sc(200 - rx, cy - ry, 200 + rx, cy + ry), fill=mc)
            if mouth == 3:
                d.ellipse(sc(178, 292, 222, 316), fill=(255, 120, 150, 255))
        return im.resize(MASCOT_SIZE, Image.LANCZOS)
    return {(m, b): draw(m, b) for m in range(4) for b in (0, 1)}


def make_bubble(text, max_w=640):
    """Облачко с репликой справа от ведущего (хвостик слева)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    f, lines = bot._fit_lines(probe, text, max_w - 70, 330, start=50, minimum=30, max_lines=6)
    lh = int(f.size * 1.22)
    w, h = max_w, lh * len(lines) + 70
    img = Image.new("RGBA", (w + 36, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(0, h * .55), (40, h * .38), (40, h * .72)], fill=(255, 255, 255, 250), outline=DARK + (255,))
    d.rounded_rectangle((30, 0, w + 35, h - 1), radius=44, fill=(255, 255, 255, 250), outline=DARK + (255,), width=6)
    d.polygon([(8, h * .55), (44, h * .41), (44, h * .69)], fill=(255, 255, 255, 250))
    for i, line in enumerate(lines):
        d.text((30 + w / 2, 35 + i * lh + lh // 2), line, font=f, fill=DARK + (255,), anchor="mm")
    return img


# ===================== КАДРЫ-СЛАЙДЫ (прозрачные слои) =====================
def new_layer():
    return bot.Image.new("RGBA", (W, H), (0, 0, 0, 0))


def sunburst(layer, cx, cy, radius=760, rays=14, alpha=34):
    """Лучи за героем: добавляют глубины и энергии."""
    d = bot.ImageDraw.Draw(layer)
    for i in range(rays):
        a0, a1 = 2 * math.pi * i / rays, 2 * math.pi * (i + .5) / rays
        d.polygon([(cx, cy), (cx + radius * math.cos(a0), cy + radius * math.sin(a0)),
                   (cx + radius * math.cos(a1), cy + radius * math.sin(a1))], fill=(255, 255, 255, alpha))


def chip(layer, cx, y, text, fill, color=(255, 255, 255), size=54):
    d = bot.ImageDraw.Draw(layer)
    f = bot._fit(d, text, 900, size, 28)
    w = d.textlength(text, font=f)
    d.rounded_rectangle((cx - w / 2 - 36, y + 8, cx + w / 2 + 36, y + f.size + 52), radius=36, fill=(10, 4, 30, 150))
    d.rounded_rectangle((cx - w / 2 - 36, y, cx + w / 2 + 36, y + f.size + 44), radius=36, fill=fill + (255,),
                        outline=(255, 255, 255, 230), width=4)
    d.text((cx, y + (f.size + 44) / 2), text, font=f, fill=color, anchor="mm")


def L_title(title, subtitle="", accent=PURPLE):
    layer = new_layer()
    sunburst(layer, W // 2, 500, 570)
    d = bot.ImageDraw.Draw(layer)
    f, lines = bot._fit_lines(d, title.upper(), 960, 620, start=150, minimum=70, max_lines=4)
    lh = int(f.size * 1.16)
    y0 = 500 - lh * len(lines) // 2
    for i, line in enumerate(lines):
        grad_text(layer, (W // 2, y0 + i * lh + lh // 2), line, f)
    if subtitle:
        chip(layer, W // 2, 830, subtitle, accent, size=58)
    return layer


def L_hero(name, chip1="", chip2="", portrait=None, rank=None):
    layer = new_layer()
    sunburst(layer, W // 2, 470, 600)
    d = bot.ImageDraw.Draw(layer)
    if portrait:
        try:
            pm = bot._portrait(portrait, 600)
            glow = bot.Image.new("RGBA", (W, H), (0, 0, 0, 0))
            bot.ImageDraw.Draw(glow).ellipse((W // 2 - 330, 140, W // 2 + 330, 800), fill=YELLOW + (110,))
            layer.alpha_composite(glow.filter(bot.ImageFilter.GaussianBlur(60)))
            layer.alpha_composite(pm, (W // 2 - pm.width // 2, 470 - pm.height // 2))
        except Exception:
            portrait = None
    if not portrait:
        d.rounded_rectangle((W // 2 - 280, 190, W // 2 + 280, 750), radius=90, fill=PURPLE + (255,), outline=DARK + (255,), width=8)
        d.text((W // 2, 470), name[:2].upper(), font=bot.card_font(260), fill=(255, 255, 255, 255), anchor="mm")
    if rank:
        grad_text(layer, (150, 235), f"#{rank}", bot.card_font(200), stroke=11)
    title = name.upper()
    grad_text(layer, (W // 2, 850), title, bot._fit(d, title, 940, 124, 56), top=(255, 255, 255), bottom=(205, 190, 255), stroke=9)
    if chip1:
        chip(layer, W // 2 if not chip2 else W // 2 - 255, 930, chip1, ORANGE, size=48)
    if chip2:
        chip(layer, W // 2 + 255, 930, chip2, GREEN, size=48)
    return layer


def L_text(heading, body, accent=ORANGE):
    layer = new_layer()
    chip(layer, W // 2, 210, heading, accent, size=60)
    d = bot.ImageDraw.Draw(layer)
    d.rounded_rectangle((50, 350, W - 50, 1060), radius=56, fill=(14, 6, 50, 205), outline=accent + (255,), width=6)
    f, lines = bot._fit_lines(d, body, 880, 600, start=70, minimum=38, max_lines=9)
    lh = int(f.size * 1.26)
    y0 = 705 - lh * len(lines) // 2
    for i, line in enumerate(lines):
        d.text((W // 2, y0 + i * lh + lh // 2), line, font=f, fill=(255, 255, 255, 255), anchor="mm")
    return layer


def L_cta(handle, qr_url):
    layer = new_layer()
    sunburst(layer, W // 2, 560, 510, alpha=28)
    d = bot.ImageDraw.Draw(layer)
    f = bot._fit(d, "СКАНИРУЙ И ПОДПИСЫВАЙСЯ", 980, 84, 44)
    if qr_url:
        grad_text(layer, (W // 2, 215), "СКАНИРУЙ", bot._fit(d, "СКАНИРУЙ", 900, 120, 50))
        tile = qr_tile(qr_url, 450)
        d.rounded_rectangle((W // 2 - 255, 320, W // 2 + 255, 830), radius=60, fill=(255, 214, 64, 255), outline=DARK + (255,), width=8)
        layer.alpha_composite(tile, (W // 2 - tile.width // 2, 575 - tile.height // 2))
    else:
        grad_text(layer, (W // 2, 330), "ПОДПИСЫВАЙСЯ", bot._fit(d, "ПОДПИСЫВАЙСЯ", 940, 120, 50))
    if handle:
        grad_text(layer, (W // 2, 900), handle, bot._fit(d, handle, 940, 96, 44), top=(255, 255, 255), bottom=(160, 215, 255), stroke=8)
    chip(layer, W // 2, 975, "ссылка в профиле", (255, 255, 255), color=DARK, size=44)
    return layer


# ===================== СБОРКА РОЛИКА =====================
def _ease_out_back(p):
    c1 = 1.70158
    p = min(1.0, max(0.0, p)) - 1
    return 1 + (c1 + 1) * p ** 3 + c1 * p ** 2


def _crop_layer(layer):
    box = layer.getbbox() or (0, 0, 1, 1)
    return layer.crop(box), (box[0], box[1])


def render_frames(slides, bg, particles, sprites, total):
    """Кадры ролика: плывущий фон и частицы, появление слайда «с отскоком», дышащий ведущий, пузырь с репликой."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    top = Image.new("RGBA", (W, 150), (0, 0, 0, 0))
    ImageDraw.Draw(top).text((54, 78), "NEW BRAWL NEWS", font=bot.card_font(44), fill=(255, 255, 255, 255), anchor="lm",
                             stroke_width=4, stroke_fill=DARK + (255,))
    done = 0
    for si, sl in enumerate(slides):
        n = max(1, round(sl["dur"] * FPS))
        content, (cx, cy) = sl["content"]
        bubble = sl.get("bubble")
        for k in range(n):
            gt = (done + k) / FPS
            ox = int(110 + 100 * math.sin(gt * 0.55))
            oy = int(160 + 130 * math.cos(gt * 0.40))
            frame = bg.crop((ox, oy, ox + W, oy + H))
            for p in particles:
                py = (p["y"] - p["v"] * gt) % (H + 260) - 130
                px = p["x"] + p["sway"] * math.sin(gt * 0.9 + p["ph"])
                frame.paste(p["sp"], (int(px), int(py)), p["sp"])
            # контент: появляется с отскоком, потом чуть «парит»
            if k < POP_FRAMES:
                pr = k / POP_FRAMES
                e = _ease_out_back(pr)
                s = 0.80 + 0.20 * e
                lay = content.resize((max(1, int(content.width * s)), max(1, int(content.height * s))), Image.BILINEAR)
                a = 0.35 + 0.65 * min(1.0, pr * 1.6)
                if a < 1.0:
                    lay.putalpha(lay.split()[3].point(lambda v, a=a: int(v * a)))
                x = cx + (content.width - lay.width) // 2
                y = cy + (content.height - lay.height) // 2 + int((1 - e) * 70)
                frame.paste(lay, (x, y), lay)
            else:
                frame.paste(content, (cx, cy + int(8 * math.sin(gt * 2.4))), content)
            # ведущий
            blink = 1 if (gt % 3.3) < 0.15 else 0
            mouth = sl["mouth"][k] if k < len(sl["mouth"]) else 0
            spr = sprites[(mouth, blink)]
            bob = int(9 * math.sin(gt * 5.2)) if mouth else int(6 * math.sin(gt * 2.4))
            if k < POP_FRAMES:
                s2 = 0.85 + 0.15 * _ease_out_back(k / POP_FRAMES)
                sp2 = spr.resize((int(spr.width * s2), int(spr.height * s2)), Image.BILINEAR)
                frame.paste(sp2, (30 + (spr.width - sp2.width) // 2, 1085 + (spr.height - sp2.height) + bob), sp2)
            else:
                frame.paste(spr, (30, 1085 + bob), spr)
            if bubble is not None:
                if k < POP_FRAMES:
                    s3 = 0.7 + 0.3 * _ease_out_back(k / POP_FRAMES)
                    bb = bubble.resize((max(1, int(bubble.width * s3)), max(1, int(bubble.height * s3))), Image.BILINEAR)
                else:
                    bb = bubble
                frame.paste(bb, (360, 1100 + (bubble.height - bb.height) // 2), bb)
            frame.paste(top, (0, 0), top)
            ImageDraw.Draw(frame).rectangle((0, 0, int(W * (done + k + 1) / max(1, total)), 12), fill=YELLOW)
            yield frame.tobytes()
        done += n


def encode_video(frames, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", out_path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for fr in frames:
            p.stdin.write(fr)
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        err = p.stderr.read().decode(errors="replace")
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg не смог собрать видео: " + err[-300:])


def _concat_wavs(parts, workdir, name):
    lst = os.path.join(workdir, name + ".txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{p}'\n" for p in parts))
    out = os.path.join(workdir, name + ".wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return out


def build_audio_tracks(slides, workdir):
    """Две дорожки по слайдам: голос (подогнан по длине слайда) и звук перехода «вжух» в начале каждого."""
    whoosh = os.path.join(workdir, "whoosh.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:r=44100",
         "-af", "highpass=f=500,lowpass=f=7000,afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.25,volume=0.8",
         "-ac", "2", whoosh])
    voice_parts, sfx_parts = [], []
    for i, sl in enumerate(slides):
        v = os.path.join(workdir, f"vseg{i}.wav")
        if sl.get("voice_wav"):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", sl["voice_wav"], "-af", "apad", "-t", f"{sl['dur']:.3f}",
                 "-ar", "44100", "-ac", "2", v])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{sl['dur']:.3f}", v])
        s = os.path.join(workdir, f"sseg{i}.wav")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", whoosh, "-af", "apad", "-t", f"{sl['dur']:.3f}",
             "-ar", "44100", "-ac", "2", s])
        voice_parts.append(v)
        sfx_parts.append(s)
    return _concat_wavs(voice_parts, workdir, "voice"), _concat_wavs(sfx_parts, workdir, "sfx")


def mux(video, voice, sfx, music, total, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", voice, "-i", sfx]
    flt = f"[2:a]volume={SFX_VOLUME}[s];[1:a][s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[v]"
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        flt += (f";[3:a]volume={MUSIC_VOLUME},afade=t=in:d=1,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
                "[v][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        out_label = "[a]"
    else:
        out_label = "[v]"
    cmd += ["-filter_complex", flt, "-map", "0:v", "-map", out_label, "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]
    run(cmd)


def render_video(specs, out_path, vstate=None, seed=0):
    """specs: [{'layer': RGBA-слой 1080x1920, 'speech': текст для голоса, 'caption': реплика в пузыре,
    'priority': 0..10, 'min': минимальная длительность}]. Озвучивает, подгоняет длительности, при необходимости
    выкидывает второстепенные слайды, собирает ролик."""
    if not have_graphics():
        raise RuntimeError("нет Pillow или шрифта с кириллицей: " + bot.cards_status())
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("не найден ffmpeg")
    vstate = vstate if vstate is not None else {}
    workdir = tempfile.mkdtemp(prefix="vid_")
    try:
        slides, voiced = [], 0
        for i, sp in enumerate(specs):
            wav = None
            if sp.get("speech"):
                raw = tts(spoken(sp["speech"]), os.path.join(workdir, f"v{i}.mp3"))
                if raw:
                    wav = clean_voice(raw, os.path.join(workdir, f"c{i}.wav"))
            if wav:
                voiced += 1
                dur = max(MIN_SLIDE_SEC, audio_duration(wav) + PAD_SEC, sp.get("min", 0))
            else:
                dur = max(MIN_SLIDE_SEC, 1.0 + len(sp.get("caption", "")) / 14, sp.get("min", 0))
            slides.append({"layer": sp["layer"], "caption": sp.get("caption", ""), "voice_wav": wav, "dur": dur,
                           "priority": sp.get("priority", 5)})
        while sum(s["dur"] for s in slides) > MAX_VIDEO_SEC and len(slides) > 2:
            drop = min((s for s in slides[1:-1]), key=lambda s: (s["priority"], -s["dur"]), default=None)
            if drop is None:
                break
            slides.remove(drop)  # выкидываем самый неважный слайд
        if sum(s["dur"] for s in slides) > MAX_VIDEO_SEC:
            print(f"ролик длиннее {MAX_VIDEO_SEC} с даже после сокращения, оставляю как есть")
        for sl in slides:
            n = max(1, round(sl["dur"] * FPS))
            sl["content"] = _crop_layer(sl["layer"])
            sl["bubble"] = make_bubble(sl["caption"]) if sl["caption"] else None
            sl["mouth"] = [mouth_state(v) for v in (envelope(sl["voice_wav"], FPS, n) if sl["voice_wav"] else [0.0] * n)]
        total_sec = sum(s["dur"] for s in slides)
        total_frames = sum(max(1, round(s["dur"] * FPS)) for s in slides)
        silent = os.path.join(workdir, "silent.mp4")
        encode_video(render_frames(slides, make_bg(seed), make_particles(seed), make_mascot(), total_frames), silent)
        voice_track, sfx_track = build_audio_tracks(slides, workdir)
        music = pick_music(vstate)
        mux(silent, voice_track, sfx_track, music, total_sec, out_path)
        return {"path": out_path, "duration": total_sec, "slides": len(slides), "voiced": voiced > 0,
                "music": os.path.basename(music) if music else ""}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ===================== СЦЕНАРИИ РОЛИКОВ =====================
CTA_SPEECH = "Больше разборов и гайдов в нашем телеграм-канале. Сканируй код и подписывайся!"
ORD = {1: "Первое", 2: "Второе", 3: "Третье"}


def _portraits(names):
    """Портреты бойцов по именам (словарь ИМЯ_ВЕРХНИМ: байты). Без сети или данных вернёт пустой словарь."""
    try:
        urls = {bot.nice_name(b["name"]).upper(): bot.portrait_url(b) for b in bot.load_brawlify()}
    except Exception as ex:
        print("портреты бойцов недоступны:", ex)
        return {}
    out = {}
    for n in names:
        u = urls.get(n.upper())
        if u:
            try:
                out[n.upper()] = bot.download(u, 5 * 1024 * 1024)
            except Exception:
                pass
    return out


def _short(text, n=130):
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",.;: ")
    return cut + "."


def cta_spec():
    return {"layer": L_cta(CTA_HANDLE, QR_URL), "speech": CTA_SPEECH, "caption": "Сканируй QR и подписывайся!",
            "priority": 10, "min": CTA_MIN_SEC}


def script_meta(meta):
    """Ролик «Мета сегодня» по данным, которые бот сохранил после публикации меты."""
    rows = bot.rank_rows(meta["stats"], bot.META_MIN_PICKS)
    if len(rows) < 3:
        return None
    top3 = rows[:3]
    total = meta["battles"] * 6
    pop = max(rows, key=lambda r: r[1])
    por = _portraits([r[0] for r in top3] + [pop[0]])
    specs = [{"layer": L_title("Кто сильнее всех?", "мета Brawl Stars сегодня"),
              "speech": "Мета Бравл Старс сегодня! Кто сильнее всех?", "caption": "Мета Brawl Stars сегодня", "priority": 9}]
    for rank in (3, 2, 1):
        n, pk, w, wr, lb = top3[rank - 1]
        specs.append({"layer": L_hero(n, f"{bot.pct(wr)} побед", f"пикрейт {bot.pct(pk / total)}", por.get(n.upper()), rank),
                      "speech": f"{ORD[rank]} место: {n}. {bot.pct(wr)} побед.",
                      "caption": f"{rank} место: {n}, {bot.pct(wr)} побед", "priority": 8 if rank == 1 else 6})
    if pop[0] not in [r[0] for r in top3]:
        specs.append({"layer": L_hero(pop[0], "самый популярный", f"{bot.pct(pop[3])} побед", por.get(pop[0].upper())),
                      "speech": f"Самый популярный: {pop[0]}. Его берут в {bot.pct(pop[1] / total)} боёв.",
                      "caption": f"Самый популярный: {pop[0]}", "priority": 3})
    specs.append(cta_spec())
    names = ", ".join(r[0] for r in top3)
    return {"kind": "meta", "specs": specs, "caption": f"🔥 Мета Brawl Stars сегодня: {names}. Кого берёте вы?"}


def script_guide(b, state):
    """Ролик «Гайд на бойца» по тем же данным, что и гайд в канале."""
    info = bot.guide_info(b, state)
    por = _portraits([info["name"]]).get(info["name"].upper())
    specs = [{"layer": L_hero(info["name"], info["cls_ru"] or "гайд", info["short_stat"], por),
              "speech": f"Гайд на бойца {info['name']}!" + (f" Это {info['cls_ru']}." if info["cls_ru"] else ""),
              "caption": f"Гайд: {info['name']}", "priority": 10}]
    if info["desc"]:
        d = _short(info["desc"], 120)
        specs.append({"layer": L_text("Кто это", d, BLUE), "speech": d, "caption": "", "priority": 4})
    for label, rows, accent, take in (("Гаджет", info["gadgets"], ORANGE, 1), ("Звёздная сила", info["stars"], PINK, 1)):
        for n, d in [r for r in rows if r[1]][:take]:
            d = _short(d, 120)
            specs.append({"layer": L_text(label, f"{n}: {d}", accent), "speech": f"{label}. {d}", "caption": "", "priority": 7})
    if info["short_stat"]:
        specs.append({"layer": L_text("Данные топ-игроков", f"Процент побед: {info['short_stat'].replace(' побед', '')}", GREEN),
                      "speech": f"У лучших игроков мира {info['short_stat']}.", "caption": "", "priority": 6})
    if info["tip"]:
        t = _short(info["tip"], 130)
        specs.append({"layer": L_text("Как играть", t, BLUE), "speech": t, "caption": "", "priority": 8})
    disc = "Это универсальный разбор. Выбирай под карту и режим."
    specs.append({"layer": L_text("Важно", disc, RED), "speech": disc, "caption": "", "priority": 9})
    specs.append(cta_spec())
    if len(specs) < 4:
        return None
    return {"kind": "guide", "specs": specs,
            "caption": f"📘 Гайд на {info['name']} в Brawl Stars: что умеет и как играть. Играете за него?"}


def script_demo():
    return {"kind": "demo", "caption": "Тестовый ролик", "specs": [
        {"layer": L_title("Тест ролика", "проверка сборки"), "speech": "Привет! Это тест автоматической сборки роликов.",
         "caption": "Тест автоматической сборки", "priority": 9},
        {"layer": L_text("Всё работает", "Голос, ведущий, QR-код и переходы собраны автоматически."),
         "speech": "Голос, ведущий, кью ар код и переходы собраны автоматически.", "caption": "", "priority": 5},
        cta_spec()]}


# ===================== СОСТОЯНИЕ, ДОСТАВКА, ПУБЛИКАЦИЯ =====================

def load_vstate():
    try:
        with open(VIDEO_STATE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("made", {})
            return data
    except Exception:
        pass
    return {"made": {}, "music_idx": 0}


def save_vstate(v):
    keep = sorted(v["made"])[-60:]
    v["made"] = {k: v["made"][k] for k in keep}
    tmp = VIDEO_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, ensure_ascii=False)
    os.replace(tmp, VIDEO_STATE)


def full_caption(text):
    handle = f"\n\nТелеграм-канал: {CTA_HANDLE} (ссылка в профиле)" if CTA_HANDLE else "\n\nТелеграм-канал в профиле"
    return f"{text}{handle}\n\n{HASHTAGS}"


def tg_send_video(path, caption, duration=0, chat_id=None):
    """Отправляет готовый ролик в личный чат с ботом (до 50 МБ)."""
    chat_id = chat_id or VIDEO_CHAT_ID
    if not chat_id or not bot.TOKEN:
        return False
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", chat_id), ("caption", caption[:1000]), ("supports_streaming", "true"),
                      ("width", W), ("height", H), ("duration", int(duration))):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="video"; filename="video.mp4"\r\n'
                'Content-Type: video/mp4\r\n\r\n').encode())
    with open(path, "rb") as f:
        body.write(f.read())
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot.TOKEN}/sendVideo", data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=180).read()
    return True


def tiktok_upload(path):
    """Необязательно. Загружает ролик в ЧЕРНОВИКИ TikTok через официальный API (Content Posting API, режим upload).
    Нужны: приложение на developers.tiktok.com со scope video.upload и токены в секретах
    (TIKTOK_ACCESS_TOKEN или TIKTOK_CLIENT_KEY + TIKTOK_CLIENT_SECRET + TIKTOK_REFRESH_TOKEN).
    Публикация из черновиков делается в приложении TikTok одним нажатием. Эта часть не проверялась на живом API."""
    token = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
    if not token and os.environ.get("TIKTOK_REFRESH_TOKEN"):
        form = urllib.parse.urlencode({
            "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""), "client_secret": os.environ.get("TIKTOK_CLIENT_SECRET", ""),
            "grant_type": "refresh_token", "refresh_token": os.environ["TIKTOK_REFRESH_TOKEN"]}).encode()
        req = urllib.request.Request(TT_BASE + "/v2/oauth/token/", data=form,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        token = data.get("access_token", "")
        if data.get("refresh_token") and data["refresh_token"] != os.environ["TIKTOK_REFRESH_TOKEN"]:
            print("TikTok выдал новый refresh-токен: обнови секрет TIKTOK_REFRESH_TOKEN, иначе старый скоро перестанет работать")
    if not token:
        return None
    size = os.path.getsize(path)
    init = urllib.request.Request(
        TT_BASE + "/v2/post/publish/inbox/video/init/",
        data=json.dumps({"source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                                         "total_chunk_count": 1}}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8"})
    resp = json.loads(urllib.request.urlopen(init, timeout=30).read())
    if (resp.get("error") or {}).get("code", "ok") != "ok":
        raise RuntimeError("TikTok init: " + json.dumps(resp.get("error"), ensure_ascii=False))
    upload_url = resp["data"]["upload_url"]
    with open(path, "rb") as f:
        payload = f.read()
    put = urllib.request.Request(upload_url, data=payload, method="PUT", headers={
        "Content-Type": "video/mp4", "Content-Length": str(size), "Content-Range": f"bytes 0-{size - 1}/{size}"})
    urllib.request.urlopen(put, timeout=300).read()
    return resp["data"].get("publish_id", "")


def deliver(result, caption):
    """Отправляет ролик туда, куда настроено. Любая ошибка доставки не ломает запуск."""
    text = full_caption(caption)
    with open(os.path.splitext(result["path"])[0] + ".txt", "w") as f:
        f.write(text)  # подпись и хештеги лежат рядом с роликом (и в артефакте запуска)
    sent = False
    try:
        sent = tg_send_video(result["path"], text, result["duration"])
        if sent:
            print("ролик отправлен в Telegram")
    except Exception as ex:
        print("ролик не отправился в Telegram (написал ли ты боту /start?):", ex)
    try:
        pid = tiktok_upload(result["path"])
        if pid is not None:
            print("ролик загружен в черновики TikTok, publish_id:", pid)
            sent = True
    except Exception as ex:
        print("загрузка в TikTok не удалась:", ex)
    return sent


# ===================== ЗАПУСК =====================
def today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def pending_jobs(state, vstate, force=()):
    """Какие ролики пора делать: мета и гайд, уже опубликованные сегодня в канале и ещё не превращённые в видео."""
    day, made, jobs = today(), vstate["made"], []
    if "meta" in force or ((state.get("meta") or {}).get("date") == day and f"meta:{day}" not in made):
        jobs.append("meta")
    if "guide" in force or (state.get("guide_posted") == day and f"guide:{day}" not in made):
        jobs.append("guide")
    return jobs


def make_one(kind, state):
    if kind == "meta":
        meta = state.get("meta") or {}
        if not meta.get("stats"):
            print("мета: данных нет, ролик пропускаю")
            return None
        return script_meta(meta)
    if kind == "guide":
        guided = state.get("guided") or {}
        name = max(guided, key=guided.get) if guided else ""
        if not name:
            print("гайд: бот ещё не публиковал гайд, ролик пропускаю")
            return None
        found = [b for b in bot.load_brawlify() if b["name"].upper() == name.upper()]
        return script_guide(found[0], state) if found else None
    if kind == "demo":
        return script_demo()
    return None


def main(argv=None):
    argv = argv or sys.argv[1:]
    mode = argv[0] if argv else "auto"
    bot.ensure_pillow()
    if setup_fonts():
        print("шрифт Russo One подключён")
    print(bot.cards_status())
    state = bot.load_state()
    vstate = load_vstate()
    jobs = pending_jobs(state, vstate) if mode == "auto" else [mode]
    day = today()
    made_today = sum(1 for k in vstate["made"] if k.endswith(day))
    os.makedirs(OUT_DIR, exist_ok=True)
    errors = 0
    for kind in jobs:
        if mode == "auto" and made_today >= VIDEO_DAILY_LIMIT:
            print("дневной лимит роликов исчерпан")
            break
        try:
            script = make_one(kind, state)
            if not script:
                continue
            out = os.path.join(OUT_DIR, f"{kind}_{day}.mp4")
            res = render_video(script["specs"], out, vstate, seed=len(kind))
            print(f"ролик собран: {out}, {res['duration']:.1f} с, слайдов {res['slides']}, "
                  f"голос: {'да' if res['voiced'] else 'нет'}, музыка: {res['music'] or 'нет'}")
            delivered = deliver(res, script["caption"])
            if kind != "demo":
                vstate["made"][f"{kind}:{day}"] = {"delivered": bool(delivered), "sec": round(res["duration"], 1)}
                made_today += 1
        except Exception as ex:
            errors += 1
            print(f"ролик «{kind}» не получился:", ex)
    save_vstate(vstate)
    if errors and errors == len(jobs):
        sys.exit(1)


if __name__ == "__main__":
    main()
