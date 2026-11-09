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
import os, sys, io, re, json, time, math, wave, array, shutil, subprocess, tempfile, datetime, asyncio, uuid, random, itertools
import urllib.request, urllib.parse, urllib.error

import bot

# ===================== НАСТРОЙКИ И ЛИМИТЫ =====================
W, H, FPS = 1080, 1920, int(os.environ.get("VIDEO_FPS") or 30)   # 30 к/с: родная частота TikTok, движение заметно плавнее
VIDEO_STATE = "videos.json"
VIDEO_DAILY_LIMIT = 4            # не больше четырёх роликов в сутки: так канал не выглядит спамом
MAX_VIDEO_SEC = 58               # до минуты: длиннее ролики досматривают реже
MIN_SLIDE_SEC = 1.6              # быстрый монтаж: кадры сменяются каждые 2-3 секунды
CTA_MIN_SEC = 5.0                # финальный кадр с QR держим дольше, чтобы успели навести камеру
PAD_SEC = 0.12                   # пауза после фразы
POP_FRAMES = max(5, round(0.375 * FPS))   # длина анимации появления кадра (~0,4 с)
OUT_DIR = "out"
MUSIC_DIR = "music"              # сюда клади СВОИ треки или музыку без авторских прав
MUSIC_VOLUME, SFX_VOLUME = 0.12, 0.30
MUSIC_LUFS = float(os.environ.get("MUSIC_LUFS") or -26)   # громкость фоновой музыки до приглушения голосом
TTS_VOICE = os.environ.get("TTS_VOICE") or "ru-RU-DmitryNeural"
TTS_RATE = os.environ.get("TTS_RATE") or "+12%"      # чуть бодрее обычного, но имена и цифры ещё разборчивы
TTS_PITCH = os.environ.get("TTS_PITCH") or "+0Hz"
TTS_STRESS = os.environ.get("TTS_STRESS", "")        # "acute": ставить знак ударения (проверяется командой voicetest)
TTS_STYLE = os.environ.get("TTS_STYLE") or "lively"   # "lively": интонация по смыслу фраз; "plain": как раньше
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


_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
         "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def num_words(n, fem=False):
    """Число прописью (до миллиона): голос не путает падежи и не читает цифры по-английски."""
    n = int(n)
    if n < 0:
        return "минус " + num_words(-n, fem)
    if n < 20:
        w = _ONES[n]
        return {"один": "одна", "два": "две"}.get(w, w) if fem else w
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (" " + num_words(o, fem) if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return _HUND[h] + (" " + num_words(r, fem) if r else "")
    if n < 1_000_000:
        th, r = divmod(n, 1000)
        return num_words(th, True) + " " + plural(th, "тысяча", "тысячи", "тысяч") + (" " + num_words(r, fem) if r else "")
    return str(n)


def decimal_words(ip, fp):
    """53,4 -> «пятьдесят три целых четыре десятых»."""
    fp = fp[:2]
    i, f = int(ip), int(fp)
    unit = ("десятая", "десятых") if len(fp) == 1 else ("сотая", "сотых")
    whole = "целая" if i % 10 == 1 and i % 100 != 11 else "целых"
    part = unit[0] if f % 10 == 1 and f % 100 != 11 else unit[1]
    return f"{num_words(i, True)} {whole} {num_words(f, True)} {part}"


# термины игры: голос читает их по-русски (регистр важен только у сокращений)
TERMS = (("Brawl Stars", "Бравл Старс"), ("Brawl Pass", "Бравл Пасс"), ("Supercell", "Суперселл"), ("TikTok", "Тик Ток"),
         ("Telegram", "Телеграм"), ("YouTube", "Ютуб"), ("Hypercharge", "Гиперзаряд"), ("Star Power", "Звёздная сила"),
         ("Gem Grab", "Гем Граб"), ("Brawl Ball", "Бравл Бол"), ("Showdown", "Шоудаун"), ("Heist", "Хейст"),
         ("Bounty", "Баунти"), ("Hot Zone", "Хот Зон"), ("Knockout", "Нокаут"), ("Trophy Road", "Трофи Роуд"),
         ("Power League", "Пауэр Лига"), ("Ranked", "Ранкед"), ("QR", "кью ар"), ("HP", "хэ пэ"), ("FPS", "эф пэ эс"),
         ("DPS", "дэ пэ эс"), ("UI", "ю ай"), ("OK", "окей"), ("PvP", "пи ви пи"))
_CASE_SENS = {"QR", "HP", "FPS", "DPS", "UI", "OK", "PvP"}
_LETTER = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", ["а", "бэ", "цэ", "дэ", "е", "эф", "гэ", "аш", "и", "джей", "ка", "эль", "эм",
                                                  "эн", "о", "пэ", "кью", "эр", "эс", "тэ", "у", "вэ", "дабл ю", "икс", "игрек", "зэт"]))
# ё всегда ударная: переводчик её теряет, а голос без неё иногда ставит ударение не туда
_YO = {"еще": "ещё", "звездная": "звёздная", "звездной": "звёздной", "звездную": "звёздную", "звезды": "звёзды",
       "звезд": "звёзд", "идет": "идёт", "берет": "берёт", "несет": "несёт", "ведет": "ведёт",
       "зеленый": "зелёный", "зеленая": "зелёная", "тяжелый": "тяжёлый", "тяжелая": "тяжёлая", "огнем": "огнём",
       "своем": "своём", "ее": "её", "поведет": "поведёт", "придет": "придёт", "уйдет": "уйдёт", "лед": "лёд"}
# слова, где голос чаще всего ошибается в ударении (знак ударения ставится только при TTS_STRESS=acute)
STRESS = {"мета": "ме́та", "гайд": "га́йд", "гаджет": "га́джет", "пикрейт": "пикре́йт", "винрейт": "винре́йт",
          "бафф": "ба́фф", "нерф": "не́рф", "донат": "дона́т", "процента": "проце́нта", "процентов": "проце́нтов",
          "мортис": "мо́ртис", "эдгар": "э́дгар", "кроу": "кро́у", "шелли": "ше́лли", "спайк": "спа́йк",
          "пайпер": "па́йпер", "эмбер": "э́мбер", "леон": "лео́н", "колетт": "коле́тт", "поко": "по́ко",
          "нита": "ни́та", "рико": "ри́ко", "тара": "та́ра", "берри": "бе́рри"}


def _map_words(t, table):
    def one(m):
        w = m.group(0)
        r = table.get(w.lower())
        if not r:
            return w
        if len(w) > 1 and w.isupper():
            return r.upper()
        return r[:1].upper() + r[1:] if w[:1].isupper() else r
    return re.sub(r"[А-Яа-яЁё]+", one, t)


def spoken(text):
    """Текст для голоса: имена, термины и числа в удобном для озвучки виде (на экране остаётся оригинал)."""
    t = text
    for name in sorted(set(list(NAME_SAY) + list(bot.BRAWLERS)), key=len, reverse=True):
        say = NAME_SAY.get(name) or translit(name)
        t = re.sub(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", say, t)
    for lat, cyr in TERMS:
        t = re.sub(r"(?<!\w)" + re.escape(lat) + r"(?!\w)", cyr, t, flags=0 if lat in _CASE_SENS else re.I)

    t = re.sub(r"(?<=[A-Za-zА-Яа-я])['’]s\b", "", t)  # притяжательное 's: «Supercell's» -> «Суперселл»
    # одиночная латинская буква (x, X, a...) называется буквой: «икс», а не «кс»; рядом с цифрой ставим пробелы (2x, x2)
    t = re.sub(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", lambda m: " " + _LETTER[m.group(0).upper()] + " ", t)

    def latin(m):
        w = m.group(0)
        if w.isupper() and 2 <= len(w) <= 4:
            return " ".join(_LETTER.get(c, c) for c in w)  # сокращение читаем по буквам
        return translit(w.replace("'", "").replace("-", ""))
    t = re.sub(r"[A-Za-z][A-Za-z'\-]*", latin, t)  # всё, что осталось латиницей, голос не должен читать по-английски

    def pct(m):
        num = m.group(1).replace(",", ".")
        if re.fullmatch(r"\d+", num):
            n = int(num)
            return num_words(n) + " " + plural(n, "процент", "процента", "процентов")
        ip, fp = num.split(".")
        return decimal_words(ip, fp) + " процента"
    t = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", pct, t)
    t = re.sub(r"п\.\s?п\.", "процентных пунктов", t)
    t = re.sub(r"[#@][\w_]+", "", t)
    t = re.sub(r"[^\w\s.,!?:;()\-—«»\"'…]", " ", t)
    t = _map_words(re.sub(r"\s+", " ", t).strip(), _YO)
    if TTS_STRESS == "acute":
        t = _map_words(t, STRESS)
    return t


# ===================== ОЗВУЧКА И ЗВУК =====================
_ENGINE = {}


def tts(text, out_path, rate=None, pitch=None, volume=None):
    """Озвучка: сначала бесплатный голос Microsoft Edge (edge-tts), затем запасной Google (gTTS).
    Возвращает путь к файлу или None (тогда ролик собирается без голоса, только с текстом)."""
    last = None
    try:
        import edge_tts

        async def run_edge():
            kw = {"rate": rate or TTS_RATE, "pitch": pitch or TTS_PITCH}
            if volume:
                kw["volume"] = volume
            try:
                comm = edge_tts.Communicate(text, TTS_VOICE, **kw)
            except TypeError:  # старая версия edge-tts без pitch или volume
                comm = edge_tts.Communicate(text, TTS_VOICE, rate=kw["rate"])
            await comm.save(out_path)

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


# ---- «живой» голос: каждая фраза озвучивается со своей интонацией (радость, вопрос, спад к концу),
# между фразами ставятся паузы, а точные границы фраз дают точную подсветку слов
def _num(text, default=0):
    m = re.search(r"[+-]?\d+", text or "")
    return int(m.group(0)) if m else default


def split_segments(tokens, lead_n=0):
    """Делит слова озвучки на сегменты (предложения, а длинные ещё по запятым). Заголовок-«лид» всегда отдельно.
    Возвращает списки индексов слов; ничего не теряется."""
    segs, cur = [], []
    for i, tk in enumerate(tokens):
        cur.append(i)
        end = tk.rstrip("»\"')").endswith((".", "!", "?", "…"))
        soft = tk.endswith((",", ";", ":", "—")) and len(cur) >= 5
        if i + 1 == lead_n or end or soft or len(cur) >= 14:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    if len(segs) > 1 and len(segs[-1]) < 2 and segs[-2][-1] >= lead_n:
        segs[-2:] = [segs[-2] + segs[-1]]
    return segs


def prosody(text, first=False, kind=""):
    """Сдвиги скорости (%), высоты (Гц) и громкости (%) относительно базовых: по знаку в конце фразы и роли слайда."""
    t = text.strip()
    last = t.rstrip("»\"')")[-1:]
    rate = pitch = vol = 0
    if last == "!":
        rate, pitch, vol = 6, 5, 8
    elif last == "?":
        rate, pitch = 2, 6
    elif last in ",;:—":
        pitch = 2
    else:
        rate, pitch = -2, -2  # спад к концу мысли: звучит уверенно и закончено
    if first:  # первая фраза ролика, «крючок»: бодрее и громче
        rate, pitch, vol = rate + 8, pitch + 3, vol + 10
    if kind == "cta":
        rate, pitch, vol = rate + 4, pitch + 3, vol + 4
    if re.search(r"\d|%", t):  # цифры чуть медленнее и громче, чтобы их было слышно
        rate, vol = rate - 4, vol + 5
    return rate, pitch, vol


def _prosody_args(rate, pitch, vol):
    r = max(-15, min(30, _num(TTS_RATE, 0) + rate))
    p = max(-8, min(10, _num(TTS_PITCH, 0) + pitch))
    v = max(-10, min(20, vol))
    return f"{r:+d}%", f"{p:+d}Hz", f"{v:+d}%"


def _pause_after(text):
    last = text.strip().rstrip("»\"')")[-1:]
    return {"!": 0.22, "?": 0.22, ".": 0.20, "…": 0.25, ",": 0.07, ":": 0.12, ";": 0.10, "—": 0.10}.get(last, 0.08)


def speak(sp, i, workdir, first=False):
    """Озвучка слайда. Возвращает (wav, сегменты) где сегменты: [{'start','dur','a','b'}] (b не включительно, индексы слов),
    либо (wav, None) в простом режиме, либо (None, None) без голоса."""
    speech = (sp.get("speech") or "").strip()
    if not speech:
        return None, None

    def plain():
        raw = tts(spoken(speech), os.path.join(workdir, f"v{i}.mp3"))
        return (clean_voice(raw, os.path.join(workdir, f"c{i}.wav")), None) if raw else (None, None)
    tokens = speech.split(" ")
    if TTS_STYLE != "lively" or len(tokens) < 2:
        return plain()
    lead_n = len((sp.get("lead") or "").split())
    groups = split_segments(tokens, lead_n)
    parts, info, t = [], [], 0.0
    for gi, g in enumerate(groups):
        text = " ".join(tokens[j] for j in g)
        r, pt, v = _prosody_args(*prosody(text, first and gi == 0, sp.get("kind", "")))
        raw = tts(spoken(text), os.path.join(workdir, f"v{i}_{gi}.mp3"), r, pt, v)
        if not raw:
            return plain()
        wav = clean_voice(raw, os.path.join(workdir, f"c{i}_{gi}.wav"))
        d = audio_duration(wav)
        info.append({"start": t, "dur": d, "a": g[0], "b": g[-1] + 1})
        parts.append(wav)
        t += d
        if gi + 1 < len(groups):
            gap = 0.16 if gi == 0 and lead_n and g[-1] + 1 == lead_n else _pause_after(text)
            gp = os.path.join(workdir, f"g{i}_{gi}.wav")
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{gap:.3f}",
                 "-c:a", "pcm_s16le", gp])
            parts.append(gp)
            t += gap
    return _concat_wavs(parts, workdir, f"vo{i}"), info


def _word_weight(w):
    extra = 3.0 if w.endswith((",", ";", ":")) else 6.0 if w.endswith((".", "!", "?", "…")) else 0.0
    return max(1.0, len(spoken(w))) + extra


def word_times_seg(words, info, lead_n):
    """Время звучания каждого слова: внутри каждого сегмента пропорционально длине слова, границы сегментов точные."""
    out = [None] * len(words)
    for seg in info:
        idxs = [tok - lead_n for tok in range(seg["a"], seg["b"]) if lead_n <= tok < lead_n + len(words)]
        if not idxs:
            continue
        ws = [_word_weight(words[j]["word"]) for j in idxs]
        total, t = sum(ws), seg["start"]
        for j, wt in zip(idxs, ws):
            out[j] = (t, t + seg["dur"] * wt / total)
            t += seg["dur"] * wt / total
    last = 0.0
    for j, v in enumerate(out):  # слова вне сегментов (не должно быть) получают пустое окно рядом с соседями
        if v is None:
            out[j] = (last, last + 0.05)
        last = out[j][1]
    return out


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
    af = _TRIM + ",silenceremove=stop_periods=-1:stop_duration=0.5:stop_threshold=-45dB:stop_silence=0.22"
    af += f",atempo={tempo}" if tempo != 1.0 else ""
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


def grad_text(layer, xy, text, font, top=(255, 244, 150), bottom=ORANGE, stroke=9, outline=DARK, anchor="mm", depth=14):
    """Крупный текст: градиентная заливка, толстая обводка и тень (рисуется на прозрачном слое)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    d = ImageDraw.Draw(layer)
    l, t, r, b = d.textbbox(xy, text, font=font, anchor=anchor, stroke_width=stroke)
    pad = 24
    box = (l - pad, t - pad, r + pad + 10 + depth, b + pad + 14 + depth)
    w, h = box[2] - box[0], box[3] - box[1]
    ox, oy = xy[0] - box[0], xy[1] - box[1]
    tmp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    td.text((ox + depth * 0.4, oy + depth + 10), text, font=font, fill=(8, 3, 28, 190), anchor=anchor, stroke_width=stroke,
            stroke_fill=(8, 3, 28, 190))
    ext = tuple(int(c * 0.42) for c in bottom) + (255,)  # «толщина» букв: слои со сдвигом вниз, как у объёмного текста
    for i in range(depth, 0, -2):
        td.text((ox + i * 0.4, oy + i), text, font=font, fill=ext, anchor=anchor, stroke_width=stroke, stroke_fill=ext)
    td.text((ox, oy), text, font=font, fill=outline + (255,), anchor=anchor, stroke_width=stroke, stroke_fill=outline + (255,))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((ox, oy), text, font=font, fill=255, anchor=anchor)
    tmp.paste(_vgrad(w, h, top, bottom), (0, 0), mask)
    region = Image.alpha_composite(layer.crop(box), tmp)
    layer.paste(region, (box[0], box[1]))


_BG_PAL = [((18, 62, 255), (112, 22, 214), [BLUE, PURPLE, (0, 205, 235), PINK]),
           ((255, 48, 136), (92, 16, 192), [PINK, ORANGE, PURPLE, YELLOW]),
           ((0, 178, 196), (30, 52, 205), [GREEN, BLUE, (0, 225, 205), PURPLE])]
BURST_COLORS = [YELLOW, (0, 225, 235), (255, 120, 200)]   # по кругу: цвет вспышки контрастирует с палитрой фона слайда


def make_bg(seed=0, pal=0):
    """Яркий фон (три палитры, слайды чередуют их): градиент, цветные пятна, растр из точек и диагональные полосы."""
    Image, ImageDraw, ImageFilter = bot.Image, bot.ImageDraw, bot.ImageFilter
    top, low, colors = _BG_PAL[pal % len(_BG_PAL)]
    rnd = random.Random(seed * 7 + pal)
    bw, bh = W + 220, H + 320
    img = _vgrad(bw, bh, top, low).convert("RGBA")
    small = Image.new("RGBA", (bw // 4, bh // 4), (0, 0, 0, 0))  # пятна рисуем мелко и растягиваем: так быстро и мягко
    sd = ImageDraw.Draw(small)
    for color in colors + colors[:2]:
        r = rnd.randint(110, 200)
        x, y = rnd.randint(-40, bw // 4 - r), rnd.randint(-40, bh // 4 - r)
        sd.ellipse((x, y, x + r, y + r), fill=color + (rnd.randint(120, 170),))
    img = Image.alpha_composite(img, small.filter(ImageFilter.GaussianBlur(30)).resize((bw, bh), Image.BICUBIC))
    dots = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))  # растр как в комиксах: точки крупнее к низу
    dd = ImageDraw.Draw(dots)
    step = 44
    for row, gy in enumerate(range(0, bh, step)):
        r = 1.5 + 11 * (gy / bh) ** 1.6
        for gx in range(-(step // 2) if row % 2 else 0, bw, step):
            dd.ellipse((gx - r, gy - r, gx + r, gy + r), fill=(255, 255, 255, 36))
    img = Image.alpha_composite(img, dots)
    stripes = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    st = ImageDraw.Draw(stripes)
    for i in range(-4, 9):
        x = i * 300
        st.polygon([(x, 0), (x + 120, 0), (x - 500 + 120, bh), (x - 500, bh)], fill=(255, 255, 255, 26))
    from PIL import ImageChops
    out = Image.alpha_composite(img, stripes).convert("RGB")
    noise = Image.effect_noise((bw, bh), 2.5).convert("RGB")  # шум в ±2 уровня: убирает ступеньки (banding)
    return ImageChops.add(out, noise, 1.0, -128)


def _shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


# ---- мини-3D: многогранники рисуются многоугольниками с освещением (без библиотек)
def _mesh_cube():
    faces = []
    for axis in range(3):
        for sgn in (-1, 1):
            pts = []
            for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                c = [0, 0, 0]
                c[axis], c[(axis + 1) % 3], c[(axis + 2) % 3] = sgn, a, b
                pts.append(tuple(c))
            faces.append(pts)
    return {"faces": faces, "inset": 0.62}  # «рамка» на гранях: как у силового куба


def _mesh_gem():
    n = 8
    ring = lambda r, y: [(r * math.cos(2 * math.pi * i / n), y, r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    table, girdle, culet = ring(0.55, 0.95), ring(1.0, 0.3), (0, -1.25, 0)
    faces = [table]
    for i in range(n):
        j = (i + 1) % n
        faces.append([table[i], table[j], girdle[j], girdle[i]])
        faces.append([girdle[i], girdle[j], culet])
    return {"faces": faces, "inset": 0}


def _mesh_star():
    pts = [((1.25 if i % 2 == 0 else 0.55) * math.cos(math.pi / 2 + i * math.pi / 5),
            (1.25 if i % 2 == 0 else 0.55) * math.sin(math.pi / 2 + i * math.pi / 5)) for i in range(10)]
    zf, zb = 0.38, -0.38
    faces = [[(x, y, zf) for x, y in pts], [(x, y, zb) for x, y in pts][::-1]]
    for i in range(10):
        j = (i + 1) % 10
        faces.append([(pts[i][0], pts[i][1], zf), (pts[j][0], pts[j][1], zf), (pts[j][0], pts[j][1], zb), (pts[i][0], pts[i][1], zb)])
    return {"faces": faces, "inset": 0}


def _v_sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _v_dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _v_cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def _v_norm(a):
    l = math.sqrt(_v_dot(a, a)) or 1.0
    return (a[0] / l, a[1] / l, a[2] / l)


def _rotate3(v, ax, ay):
    x, y, z = v
    c, sn = math.cos(ay), math.sin(ay)
    x, z = x * c + z * sn, -x * sn + z * c
    c, sn = math.cos(ax), math.sin(ax)
    y, z = y * c - z * sn, y * sn + z * c
    return (x, y, z)


_LIGHT = _v_norm((-0.45, 0.6, 0.75))
_HALF = _v_norm((_LIGHT[0], _LIGHT[1], _LIGHT[2] + 1))


def render_mesh(mesh, color, size, ax, ay, radius=0.30, line=0.018):
    """Один кадр вращающегося предмета: грани с освещением и бликом, чёрные контуры как в игре, мягкое свечение."""
    Image, ImageDraw, ImageFilter = bot.Image, bot.ImageDraw, bot.ImageFilter
    ss = 2
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((S * .12, S * .12, S * .88, S * .88), fill=color + (120,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.07)))
    d = ImageDraw.Draw(img)
    sc, c = S * radius, S / 2
    faces = []
    for face in mesh["faces"]:
        pts = [_rotate3(v, ax, ay) for v in face]
        cen = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
        n = _v_norm(_v_cross(_v_sub(pts[1], pts[0]), _v_sub(pts[2], pts[0])))
        if _v_dot(n, cen) < 0:
            n = (-n[0], -n[1], -n[2])  # нормаль всегда наружу
        if n[2] <= 0.03:
            continue  # грань смотрит от зрителя
        faces.append((cen[2], pts, cen, n))
    faces.sort(key=lambda f: f[0])
    lw = max(2, int(S * line))
    for _, pts, cen, n in faces:
        k = 0.42 + 0.75 * max(0.0, _v_dot(n, _LIGHT))
        spec = max(0.0, _v_dot(n, _HALF)) ** 22
        col = tuple(min(255, int(ch * k + 255 * spec * 0.7)) for ch in color)
        poly = [(c + p[0] * sc, c - p[1] * sc) for p in pts]
        d.polygon(poly, fill=col + (255,))
        if mesh.get("inset"):
            ic = (c + cen[0] * sc, c - cen[1] * sc)
            inner = [(ic[0] + (x - ic[0]) * mesh["inset"], ic[1] + (y - ic[1]) * mesh["inset"]) for x, y in poly]
            col2 = tuple(min(255, int(ch * (k * 1.15 + 0.12) + 60 * spec)) for ch in color)
            d.polygon(inner, fill=col2 + (255,))
            d.line(inner + [inner[0]], fill=_shade(color, 0.5) + (255,), width=max(1, lw // 2), joint="curve")
        d.line(poly + [poly[0]], fill=DARK + (255,), width=lw, joint="curve")
    return img.resize((size, size), Image.LANCZOS)


_MESHES = {"cube": (_mesh_cube, 0.27, 0.022, 0.55), "gem": (_mesh_gem, 0.31, 0.012, 0.55), "star": (_mesh_star, 0.32, 0.02, 0.28)}
OBJ_COLORS = {"cube": [(70, 225, 95), (255, 175, 35)], "gem": [PINK, (80, 175, 255), (175, 105, 255)],
              "star": [YELLOW, (255, 150, 40)]}
_OBJ_CACHE = {}


def obj_frames(kind, color, size, n=30, alpha=1.0):
    """Цикл вращения предмета (n кадров) для силового куба, кристалла и звезды. Считается один раз и кэшируется."""
    key = (kind, color, size, n, alpha)
    if key not in _OBJ_CACHE:
        mk, radius, line, tiltx = _MESHES[kind]
        mesh = mk()
        frames = []
        for i in range(n):
            a = 2 * math.pi * i / n
            turn = 0.95 * math.sin(a) if kind == "star" else a  # звезда качается, а не крутится: с ребра она выглядит как полоски
            fr = render_mesh(mesh, color, size, tiltx + 0.15 * math.sin(a), turn, radius, line)
            if alpha < 1.0:
                fr.putalpha(fr.getchannel("A").point(lambda v, al=alpha: int(v * al)))
            frames.append(fr)
        _OBJ_CACHE[key] = frames
    return _OBJ_CACHE[key]


def make_burst(color, size=1100):
    """Комиксный «взрыв» за текстом (вращается в ролике)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, spikes = size / 2, 16

    def star(rk):
        return [(c + c * (rk if i % 2 == 0 else rk * 0.80) * math.cos(math.pi * i / spikes),
                 c + c * (rk if i % 2 == 0 else rk * 0.80) * math.sin(math.pi * i / spikes)) for i in range(spikes * 2)]
    outer = star(0.98)
    d.polygon(outer, fill=color + (255,))
    d.line(outer + [outer[0]], fill=DARK + (255,), width=14, joint="curve")
    core = tuple(int(v * 0.22 + dk * 0.78) for v, dk in zip(color, DARK))  # тёмное ядро: белый и жёлтый текст на нём читается
    d.polygon(star(0.80), fill=core + (255,))
    d.line(star(0.80) + [star(0.80)[0]], fill=(255, 255, 255, 210), width=6, joint="curve")
    im.putalpha(im.getchannel("A").point(lambda v: int(v * 0.94)))
    return im


def make_particles(seed=0, count=10):
    """Искры и вращающиеся 3D-предметы (силовой куб, кристалл, звезда): ближние крупные у краёв, дальние мельче и бледнее."""
    Image, ImageDraw, ImageFilter = bot.Image, bot.ImageDraw, bot.ImageFilter
    rnd = random.Random(seed + 99)
    parts = []
    for i in range(count):
        size = rnd.choice((28, 38, 54))
        sp = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sp)
        c = size
        sd.polygon([(c, 4), (c + size * .18, c - size * .18), (size * 2 - 4, c), (c + size * .18, c + size * .18),
                    (c, size * 2 - 4), (c - size * .18, c + size * .18), (4, c), (c - size * .18, c - size * .18)],
                   fill=(255, 255, 255, 200))
        parts.append({"sp": sp, "x": rnd.randint(0, W), "y": rnd.randint(0, H + 200), "v": rnd.randint(60, 150),
                      "sway": rnd.randint(14, 50), "ph": rnd.random() * 6.28})
    rnd2 = random.Random(seed + 7)
    sizes = (250, 200, 160, 130, 110, 220, 140)
    for j, size in enumerate(sizes):
        kind = ("cube", "gem", "star")[j % 3]
        color = rnd2.choice(OBJ_COLORS[kind])
        near = size >= 200
        frames = obj_frames(kind, color, size, 30, 1.0 if near else 0.9)
        x = rnd2.choice((rnd2.randint(-60, 40), rnd2.randint(W - size - 40, W - size + 60))) if near else rnd2.randint(0, W - size)
        parts.append({"frames": frames, "x": x, "y": rnd2.randint(0, H + 200), "v": int(16 + size * 0.2),
                      "sway": rnd2.randint(10, 34), "ph": rnd2.random() * 6.28, "rate": rnd2.randint(14, 22),
                      "off": rnd2.randint(0, 29)})
    return parts


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


def _prep_portrait(im):
    """Если у портрета непрозрачный фон, вырезаем круг: иначе обводка-стикер пошла бы по прямоугольнику."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    im = im.convert("RGBA")
    a = im.getchannel("A")
    if min(a.getpixel(c) for c in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1))) > 200:
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, im.width - 1, im.height - 1), fill=255)
        im.putalpha(mask)
    return im


def _sticker(img, white=9, dark=6):
    """Обводка как у наклейки: белая кайма и тёмный контур вокруг силуэта."""
    Image, ImageFilter = bot.Image, bot.ImageFilter
    pad = white + dark + 2
    base = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    base.alpha_composite(img, (pad, pad))

    def grow(alpha, r):
        return alpha.filter(ImageFilter.GaussianBlur(r * 0.6)).point(lambda v: 255 if v > 12 else 0)
    m1 = grow(base.getchannel("A"), white)
    m2 = grow(m1, dark)
    out = Image.new("RGBA", base.size, DARK + (255,))
    out.putalpha(m2)
    wl = Image.new("RGBA", base.size, (255, 255, 255, 255))
    wl.putalpha(m1)
    out.alpha_composite(wl)
    out.alpha_composite(base)
    return out


HOST_SIZE, HOST_X = (380, 510), 10


def _over(canvas, img, x, y):
    """alpha_composite, который не падает, если картинка выходит за края."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(canvas.width, x + img.width), min(canvas.height, y + img.height)
    if x1 > x0 and y1 > y0:
        canvas.alpha_composite(img.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


class Host3D:
    """Диктор как объёмная коллекционная карточка: вращается в 3D с перспективой, переворачивается при смене слайда,
    «кивает» и подпрыгивает, когда говорит, по ней пробегает блик, вокруг по орбите летают 3D-предметы,
    внизу индикатор голоса. faces: {(рот 0..3, моргание 0..1): RGBA-картинка бойца или робота}."""
    FW, FH, TH, D = 252, 328, 30, 900   # лицевая грань, толщина, расстояние до камеры
    CX, CY, SS = 190, 240, 2            # центр карточки на холсте и суперсэмплинг (сглаживание краёв)

    def __init__(self, faces, label="", accent=PURPLE):
        Image, ImageDraw = bot.Image, bot.ImageDraw
        S = self.SS
        fw, fh = self.FW * S, self.FH * S
        r = 30 * S
        base = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        d = ImageDraw.Draw(base)
        d.rounded_rectangle((0, 0, fw - 1, fh - 1), radius=r, fill=DARK + (255,))
        d.rounded_rectangle((5 * S, 5 * S, fw - 1 - 5 * S, fh - 1 - 5 * S), radius=r - 5 * S, fill=(255, 214, 64, 255))  # золотая рамка
        inner = _vgrad(fw, fh, _shade(accent, 1.3), _shade(accent, 0.5)).convert("RGBA")
        dots = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dots)
        step = 20 * S
        for row, gy in enumerate(range(0, fh, step)):
            rad = (1 + 3.5 * gy / fh) * S
            for gx in range(-(step // 2) if row % 2 else 0, fw, step):
                dd.ellipse((gx - rad, gy - rad, gx + rad, gy + rad), fill=(255, 255, 255, 38))
        inner = Image.alpha_composite(inner, dots)
        mask = Image.new("L", (fw, fh), 0)
        ImageDraw.Draw(mask).rounded_rectangle((13 * S, 13 * S, fw - 1 - 13 * S, fh - 1 - 13 * S), radius=r - 13 * S, fill=255)
        base.paste(inner, (0, 0), mask)
        d = ImageDraw.Draw(base)
        if label:
            d.rounded_rectangle((22 * S, 278 * S, fw - 22 * S, 314 * S), radius=15 * S, fill=DARK + (255,), outline=YELLOW + (255,), width=3 * S)
            f = bot._fit(d, label, fw - 70 * S, 26 * S, 12 * S)
            d.text((fw // 2, 296 * S), label, font=f, fill=(255, 255, 255, 255), anchor="mm")
        self.mask = base.getchannel("A")
        win = (16 * S, 16 * S, fw - 16 * S, 272 * S)
        ww, wh = win[2] - win[0], win[3] - win[1]
        ref = faces[(0, 0)]
        k0 = min(ww / ref.width, wh / ref.height)
        self.tex = {}
        for key, face in faces.items():
            f2 = face.resize((max(1, int(face.width * k0)), max(1, int(face.height * k0))), Image.LANCZOS)
            lay = Image.new("RGBA", (ww, wh), (0, 0, 0, 0))
            _over(lay, f2, (ww - f2.width) // 2, wh - f2.height)
            t = base.copy()
            _over(t, lay, win[0], win[1])
            self.tex[key] = t
        band = Image.new("RGBA", (fw * 3, fh), (0, 0, 0, 0))  # блик: наклонная полоса
        bd = ImageDraw.Draw(band)
        for x0, wdt, al in ((fw, 46 * S, 70), (fw + 70 * S, 16 * S, 90)):
            bd.polygon([(x0, 0), (x0 + wdt, 0), (x0 + wdt - 120 * S, fh), (x0 - 120 * S, fh)], fill=(255, 255, 255, al))
        self.shine = band
        self.orbit = [(obj_frames("cube", (70, 225, 95), 64, 30), 0.0), (obj_frames("star", YELLOW, 72, 30), 2.09),
                      (obj_frames("gem", PINK, 66, 30), 4.19)]
        self.accent = accent

    def _proj(self, v, th, ph, cy):
        x, y, z = v
        c, sn = math.cos(th), math.sin(th)
        x, z = x * c + z * sn, -x * sn + z * c
        c, sn = math.cos(ph), math.sin(ph)
        y, z = y * c - z * sn, y * sn + z * c
        sc = self.D / (self.D - z)
        return (self.CX + x * sc, cy - y * sc), z

    def render(self, gt, mouth, k, si, scale=1.0):
        from PIL import ImageChops
        Image, ImageDraw = bot.Image, bot.ImageDraw
        S = self.SS
        cw, ch = HOST_SIZE[0] * S, HOST_SIZE[1] * S
        blink = 1 if (gt % 3.3) < 0.15 else 0
        tex = self.tex.get((mouth, blink)) or self.tex[(mouth, 0)]
        spin = 0.0
        if k < 9:  # на каждой смене слайда карточка эффектно переворачивается
            spin = (1 - _ease_out_back(k / 9)) * 1.25 * (1 if si % 2 == 0 else -1)
        talk = mouth / 3.0
        th = 0.34 * math.sin(gt * 1.25) + 0.10 * talk * math.sin(gt * 8.0) + spin
        ph = 0.07 * math.sin(gt * 0.9) + (0.09 * math.sin(gt * 7.0) if mouth else 0.0)
        cy = self.CY - 8 * talk * abs(math.sin(gt * 9)) + 6 * math.sin(gt * 2.2)
        off = int(((gt * 0.45) % 1.0) * self.FW * S * 2)
        sh = self.shine.crop((off, 0, off + self.FW * S, self.FH * S))
        sh.putalpha(ImageChops.multiply(sh.getchannel("A"), self.mask))
        tx = tex.copy()
        tx.alpha_composite(sh)
        hw, hh, tz = self.FW / 2, self.FH / 2, self.TH / 2
        corners = lambda z: [(-hw, hh, z), (hw, hh, z), (hw, -hh, z), (-hw, -hh, z)]
        front = [self._proj(v, th, ph, cy)[0] for v in corners(tz)]
        back = [self._proj(v, th, ph, cy)[0] for v in corners(-tz)]
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        sw = 1.0 - 0.12 * talk * abs(math.sin(gt * 9))  # тень сжимается, когда карточка подпрыгивает
        d.ellipse(((self.CX - 105 * sw) * S, 470 * S, (self.CX + 105 * sw) * S, 498 * S), fill=(0, 0, 0, 95))

        def orbiters(front_side):
            for frs, ph0 in self.orbit:
                a = gt * 1.15 + ph0
                z = math.sin(a)
                if (z >= 0) != front_side:
                    continue
                spr = frs[int(gt * 20 + ph0 * 5) % len(frs)]
                f = S * (0.8 + 0.3 * z)
                spr = spr.resize((max(1, int(spr.width * f)), max(1, int(spr.height * f))), Image.BILINEAR)
                ox, oy = self.CX + 150 * math.cos(a), cy - 30 + 40 * math.sin(a)
                _over(canvas, spr, int(ox * S - spr.width / 2), int(oy * S - spr.height / 2))
        orbiters(False)
        for (a, b), nrm in (((1, 2), (1, 0, 0)), ((3, 0), (-1, 0, 0)), ((0, 1), (0, 1, 0)), ((2, 3), (0, -1, 0))):
            nx, ny, nz = _rotate3(nrm, ph, th)  # те же углы и порядок поворотов, что у _proj
            if nz <= 0.02:
                continue
            kk = 0.55 + 0.6 * max(0.0, _v_dot((nx, ny, nz), _LIGHT))
            poly = [(x * S, y * S) for x, y in (front[a], front[b], back[b], back[a])]
            d.polygon(poly, fill=_shade((255, 196, 40), kk) + (255,))
            d.line(poly + [poly[0]], fill=DARK + (255,), width=4 * S, joint="curve")
        dst = [(x * S, y * S) for x, y in front]
        src = [(0, 0), (tx.width, 0), (tx.width, tx.height), (0, tx.height)]
        mode = getattr(Image, "Transform", Image).PERSPECTIVE
        canvas.alpha_composite(tx.transform((cw, ch), mode, _coeffs(dst, src), Image.BILINEAR))
        orbiters(True)
        d = ImageDraw.Draw(canvas)
        amp = 0.2 if mouth == 0 else 0.55 + 0.15 * mouth  # индикатор голоса: столбики прыгают в такт речи
        for i in range(5):
            hgt = (8 + amp * 50 * (0.5 + 0.5 * math.sin(gt * 13 + i * 1.7))) * S
            bx = (self.CX + (i - 2) * 34) * S
            d.rounded_rectangle((bx - 12 * S, 468 * S - hgt, bx + 12 * S, 468 * S), radius=8 * S, fill=YELLOW + (255,),
                                outline=DARK + (255,), width=3 * S)
        return canvas.resize((int(HOST_SIZE[0] * scale), int(HOST_SIZE[1] * scale)), Image.LANCZOS)


def make_host(png, name=""):
    """Диктор-боец: портрет на объёмной карточке. «Говорит» масштабом портрета, кивками и индикатором голоса."""
    Image = bot.Image
    im = _prep_portrait(Image.open(io.BytesIO(png)))
    faces = {}
    for m in range(4):
        sc = 1 + 0.045 * m
        f = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))), Image.LANCZOS)
        faces[(m, 0)] = faces[(m, 1)] = f
    return Host3D(faces, (name or "").upper()[:14])


def make_robot_host():
    """Запасной диктор: робот на такой же объёмной карточке."""
    return Host3D(make_mascot(), "NEW BRAWL")


def make_bubble(text, max_w=500):
    """Облачко с репликой справа от ведущего (хвостик слева)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    fit = fit_exact(probe, text, max_w - 70, 330, 50, 28, 7)
    if fit is None:
        print("реплика не помещается в пузырь, пузырь пропущен:", text[:50])
        return None
    f, lines = fit
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


def _chip_draw(layer, cx, y, text, fill, color, size, max_w):
    d = bot.ImageDraw.Draw(layer)
    f = bot._fit(d, text, max_w, size, 24)
    w = d.textlength(text, font=f)
    box = (cx - w / 2 - 38, y, cx + w / 2 + 38, y + f.size + 44)
    d.rounded_rectangle((box[0], box[1] + 12, box[2], box[3] + 12), radius=38, fill=DARK + (255,))      # «толщина» плашки
    d.rounded_rectangle(box, radius=38, fill=fill + (255,), outline=(255, 255, 255, 255), width=5)
    d.rounded_rectangle((box[0] + 14, box[1] + 8, box[2] - 14, box[1] + 20), radius=6, fill=(255, 255, 255, 70))  # блик
    d.text((cx, y + (f.size + 44) / 2), text, font=f, fill=color, anchor="mm", stroke_width=2, stroke_fill=DARK + (255,))


def chip(layer, cx, y, text, fill, color=(255, 255, 255), size=54, max_w=900, angle=0):
    """Плашка-стикер с толщиной и бликом; angle наклоняет её (живее, чем ровная подпись)."""
    if not angle:
        return _chip_draw(layer, cx, y, text, fill, color, size, max_w)
    tmp = bot.Image.new("RGBA", (W, 300), (0, 0, 0, 0))
    _chip_draw(tmp, cx, 80, text, fill, color, size, max_w)
    tmp = tmp.rotate(angle, resample=bot.Image.BICUBIC, center=(cx, 80 + (size + 44) / 2))
    layer.alpha_composite(tmp, (0, y - 80))


def L_title(title, subtitle="", accent=PURPLE):
    layer = new_layer()
    d = bot.ImageDraw.Draw(layer)
    f, lines = bot._fit_lines(d, title.upper(), 960, 620, start=150, minimum=70, max_lines=4)
    lh = int(f.size * 1.16)
    y0 = 500 - lh * len(lines) // 2
    looks = [((255, 250, 170), (255, 170, 0)), ((255, 255, 255), (120, 215, 255))]  # строки разных цветов
    for i, line in enumerate(lines):
        top, bottom = looks[i % 2]
        grad_text(layer, (W // 2, y0 + i * lh + lh // 2), line, f, top=top, bottom=bottom, stroke=12, depth=24)
    if subtitle:
        chip(layer, W // 2, 790, subtitle, accent, size=58, angle=3)
    layer.rot = -4  # весь кадр чуть наклонён: энергичнее ровной «презентации»
    return layer


def _mosaic(im, n):
    """Мозаика n блоков по ширине: цвета и форма остаются, детали пропадают (викторина «Угадай бойца»)."""
    small = im.resize((n, max(1, round(n * im.height / im.width))), bot.Image.BOX)
    return small.resize(im.size, bot.Image.NEAREST)


def L_hero(name, chip1="", chip2="", portrait=None, rank=None, pixelate=0):
    layer = new_layer()
    d = bot.ImageDraw.Draw(layer)
    if portrait:
        try:
            raw = bot._portrait(portrait, 560)
            if pixelate:  # мозаика вместо силуэта: у портретов фон непрозрачный, и силуэт превращался в чёрный круг
                raw = _mosaic(raw.convert("RGBA"), pixelate)
            pm = _sticker(_prep_portrait(raw), 16, 8)
            sh = bot.Image.new("RGBA", (W, H), (0, 0, 0, 0))
            bot.ImageDraw.Draw(sh).ellipse((W // 2 - 260, 640, W // 2 + 260, 720), fill=(0, 0, 0, 120))
            layer.alpha_composite(sh.filter(bot.ImageFilter.GaussianBlur(14)))
            layer.alpha_composite(pm, (W // 2 - pm.width // 2, 420 - pm.height // 2))
        except Exception:
            portrait = None
    if not portrait:
        d.rounded_rectangle((W // 2 - 270, 180, W // 2 + 270, 700), radius=90, fill=PURPLE + (255,), outline=(255, 255, 255, 255), width=10)
        d.text((W // 2, 440), name[:2].upper(), font=bot.card_font(260), fill=(255, 255, 255, 255), anchor="mm",
               stroke_width=8, stroke_fill=DARK + (255,))
    if rank:
        grad_text(layer, (170, 250), f"#{rank}", bot.card_font(230), stroke=13, depth=26)
    title = name.upper()
    grad_text(layer, (W // 2, 770), title, bot._fit(d, title, 960, 132, 56), top=(255, 255, 255), bottom=(190, 170, 255),
              stroke=11, depth=22)
    if chip1:
        chip(layer, W // 2 if not chip2 else W // 2 - 255, 880, chip1, ORANGE, size=48, max_w=900 if not chip2 else 400, angle=-4)
    if chip2:
        chip(layer, W // 2 + 255, 880, chip2, GREEN, size=48, max_w=400, angle=4)
    layer.rot = 3 if len(name) % 2 == 0 else -3
    return layer


TEXT_W, TEXT_H, TEXT_START, TEXT_MIN, TEXT_LINES = 880, 600, 70, 46, 10


def fit_exact(draw, text, max_w=TEXT_W, max_h=TEXT_H, start=TEXT_START, minimum=TEXT_MIN, max_lines=TEXT_LINES):
    """Подбирает шрифт так, чтобы ВЕСЬ текст поместился. Если не помещается даже мелким, возвращает None (текст не режем)."""
    size = start
    while size >= minimum:
        f = bot.card_font(size)
        lines = bot._wrap(draw, text, f, max_w)
        if (len(lines) <= max_lines and len(lines) * int(size * 1.26) <= max_h
                and all(draw.textlength(l, font=f) <= max_w for l in lines)):
            return f, lines
        size -= 4
    return None


def _probe():
    return bot.Image and bot.ImageDraw.Draw(bot.Image.new("RGBA", (10, 10)))


_GID = itertools.count(1)


def L_kinetic(heading, accent):
    """Слайд с «бегущими» словами: в слое только плашка-заголовок, сами слова рисуются по кадрам (см. paste_kinetic)."""
    layer = new_layer()
    chip(layer, W // 2, 250, heading, accent, size=62, max_w=860, angle=-4)
    return layer


def text_specs(heading, body, accent=ORANGE, priority=5, say_first=""):
    """Текстовый слайд: слова появляются по одному в такт голосу (по 2-3 слова на экране), поэтому текст никогда
    не обрезается и не теряет смысл. Озвучивается ровно то, что написано (say_first проговаривается перед текстом)."""
    body = re.sub(r"\s+", " ", (body or "").strip())
    if not body:
        return []
    return [{"layer": L_kinetic(heading, accent), "speech": (say_first + " " + body).strip(), "lead": say_first,
             "caption": "", "priority": priority, "group": f"t{next(_GID)}", "words": [{"word": w} for w in body.split(" ")],
             "accent": accent, "sfx": "pop", "kind": "kinetic"}]


def L_cta(handle, qr_url):
    layer = new_layer()
    d = bot.ImageDraw.Draw(layer)
    if qr_url:
        grad_text(layer, (W // 2, 200), "СКАНИРУЙ", bot._fit(d, "СКАНИРУЙ", 900, 120, 50), stroke=11, depth=22)
        tile = qr_tile(qr_url, 440)
        d.rounded_rectangle((W // 2 - 255, 314, W // 2 + 255, 824), radius=64, fill=(170, 110, 0, 255))
        d.rounded_rectangle((W // 2 - 255, 300, W // 2 + 255, 810), radius=64, fill=(255, 214, 64, 255), outline=DARK + (255,), width=9)
        layer.alpha_composite(tile, (W // 2 - tile.width // 2, 555 - tile.height // 2))
    else:
        grad_text(layer, (W // 2, 330), "ПОДПИСЫВАЙСЯ", bot._fit(d, "ПОДПИСЫВАЙСЯ", 940, 120, 50), stroke=11, depth=22)
    if handle:
        grad_text(layer, (W // 2, 862), handle, bot._fit(d, handle, 900, 76, 44), top=(255, 255, 255), bottom=(160, 215, 255),
                  stroke=9, depth=16)
    chip(layer, W // 2, 925, "ссылка в профиле", (255, 255, 255), color=DARK, size=44, angle=-2)
    return layer


# ===================== СБОРКА РОЛИКА =====================
def _ease_out_back(p):
    c1 = 1.70158
    p = min(1.0, max(0.0, p)) - 1
    return 1 + (c1 + 1) * p ** 3 + c1 * p ** 2


def _crop_layer(layer):
    box = layer.getbbox() or (0, 0, 1, 1)
    return layer.crop(box), (box[0], box[1])


CONTENT_DY, MASCOT_Y, BUBBLE_Y = -70, 985, 1000   # всё выше: низ экрана (~400 px) закрывает интерфейс TikTok
KIN_CY, KIN_W = 610, 960                           # центр и ширина блока «бегущих» слов
BURST_Y = {"kinetic": KIN_CY, "title": 440, "hero": 390, "cta": 480}
PROPS = {"title": [("star", YELLOW, 250, 150, 340), ("cube", (70, 225, 95), 220, W - 150, 330)],
         "hero": [("star", YELLOW, 190, 120, 330), ("gem", (80, 175, 255), 190, W - 120, 330)],
         "cta": [("star", YELLOW, 200, 120, 290), ("gem", PINK, 200, W - 120, 290)]}
ENTRY = [(0, 70), (-520, 0), (520, 0), (0, -130)]   # откуда «влетает» слайд: снизу, слева, справа, сверху


def _coeffs(dst, src):
    """Коэффициенты перспективного преобразования PIL (решаем 8 уравнений)."""
    rows = []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    n = 8
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(rows[r][i]))
        rows[i], rows[piv] = rows[piv], rows[i]
        for r in range(n):
            if r != i:
                f = rows[r][i] / rows[i][i]
                rows[r] = [a - f * b for a, b in zip(rows[r], rows[i])]
    return [rows[i][8] / rows[i][i] for i in range(n)]


def tilt(img, s):
    """Объёмный поворот вокруг вертикальной оси (s от -1 до 1, 0 = без наклона): дальняя сторона становится ниже."""
    if abs(s) < 0.02:
        return img
    w, h = img.size
    k = min(1.0, abs(s)) * 0.20 * h / 2
    dst = [(0, 0), (w, k), (w, h - k), (0, h)] if s > 0 else [(0, k), (w, 0), (w, h), (0, h - k)]
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    mode = getattr(bot.Image, "Transform", bot.Image).PERSPECTIVE
    return img.transform((w, h), mode, _coeffs(dst, src), bot.Image.BILINEAR)


def _orient(layer):
    """Наклон всего кадра (rot задаёт L_title / L_hero)."""
    rot = getattr(layer, "rot", 0)
    return layer.rotate(rot, resample=bot.Image.BICUBIC, center=(W // 2, 520)) if rot else layer


_BURSTS = {}


def get_burst(color):
    if color not in _BURSTS:
        _BURSTS[color] = make_burst(color)
    return _BURSTS[color]


def _scaled(img, sc):
    if abs(sc - 1) < 0.01:
        return img
    return img.resize((max(1, int(img.width * sc)), max(1, int(img.height * sc))), bot.Image.BICUBIC)


def render_frames(slides, bgs, particles, sprites, total):
    """Кадры ролика: яркий фон с параллаксом, вращающиеся 3D-предметы, комиксный взрыв за героем, слова «в такт» голосу,
    диктор-стикер, быстрые склейки (полоса-шторка, RGB-сдвиг, вспышка, наезд камеры)."""
    from PIL import ImageChops
    Image, ImageDraw = bot.Image, bot.ImageDraw
    logo = Image.new("RGBA", (W, 150), (0, 0, 0, 0))
    ld = ImageDraw.Draw(logo)
    ld.rounded_rectangle((36, 34, 546, 112), radius=39, fill=DARK + (235,), outline=(255, 255, 255, 240), width=4)
    ld.text((112, 73), "NEW BRAWL NEWS", font=bot.card_font(38), fill=(255, 255, 255, 255), anchor="lm")
    white = Image.new("RGB", (W, H), (255, 255, 255))
    starts, acc = [], 0
    for sl in slides:
        starts.append(acc)
        acc += max(1, round(sl["dur"] * FPS))
    done = 0
    for si, sl in enumerate(slides):
        n = max(1, round(sl["dur"] * FPS))
        content, (cx, cy) = sl["content"]
        cy += CONTENT_DY
        bubble = sl.get("bubble")
        kin = sl.get("kin")
        bars = sl.get("bars")
        footer = sl.get("footer")
        bg = bgs[si % len(bgs)]
        bcolor = BURST_COLORS[si % len(BURST_COLORS)]
        burst = get_burst(bcolor) if sl.get("burst", True) else None
        by = sl.get("burst_y", 560)
        props = [(obj_frames(k_, c_, sz, 30), x_, y_) for k_, c_, sz, x_, y_ in sl.get("props", [])]
        ent = ENTRY[sl.get("anim", 0) % len(ENTRY)]
        for k in range(n):
            gt = (done + k) / FPS
            kk = k * 24.0 / FPS  # длительность склеек задана в кадрах при 24 к/с
            ox = int(110 + 100 * math.sin(gt * 0.9))
            oy = int(160 + 130 * math.cos(gt * 0.7))
            if kk < 6 and si > 0:  # короткая «тряска камеры» при смене слайда
                ox += int(18 * math.sin(kk * 3.3) * (1 - kk / 6))
                oy += int(14 * math.cos(kk * 2.7) * (1 - kk / 6))
            frame = bg.crop((ox, oy, ox + W, oy + H))
            pe = _ease_out_back(min(1.0, k / POP_FRAMES))
            for p in particles:
                py = (p["y"] - p["v"] * gt) % (H + 300) - 150
                px = p["x"] + p["sway"] * math.sin(gt * 0.9 + p["ph"])
                spr = p["frames"][int(gt * p["rate"] + p["off"]) % len(p["frames"])] if "frames" in p else p["sp"]
                frame.paste(spr, (int(px), int(py)), spr)
            if burst is not None:
                b = _scaled(burst.rotate(gt * 16 + si * 30, resample=Image.BILINEAR), 0.55 + 0.45 * pe)
                frame.paste(b, (W // 2 - b.width // 2, by - b.height // 2), b)
            for i, (frs, px_, py_) in enumerate(props):
                spr = _scaled(frs[int(gt * 20 + i * 11) % len(frs)], 0.4 + 0.6 * pe)
                frame.paste(spr, (px_ - spr.width // 2, int(py_ - spr.height // 2 + 12 * math.sin(gt * 3 + i * 2))), spr)
            # контент: появляется с отскоком и наклоном, потом чуть «парит» и медленно наезжает
            if k < POP_FRAMES:
                pr = k / POP_FRAMES
                lay = _scaled(content, 0.80 + 0.20 * pe)
                a = 0.35 + 0.65 * min(1.0, pr * 1.6)
                if a < 1.0:
                    lay = lay.copy()  # _scaled может вернуть сам content: менять его нельзя
                    lay.putalpha(lay.split()[3].point(lambda v, a=a: int(v * a)))
                lay = tilt(lay, (1 - pe) * 0.95 * (1 if sl.get("anim", 0) % 2 == 0 else -1))
                x = cx + (content.width - lay.width) // 2 + int((1 - pe) * ent[0])
                y = cy + (content.height - lay.height) // 2 + int((1 - pe) * ent[1])
            else:
                lay = _scaled(content, 1 + 0.05 * min(1.0, (k - POP_FRAMES) / max(1, n - POP_FRAMES)))
                x = cx + (content.width - lay.width) // 2
                y = cy + (content.height - lay.height) // 2 + int(8 * math.sin(gt * 2.4))
            frame.paste(lay, (x, y), lay)
            tt = (k + 0.5) / FPS
            for te, ex, ey in sl.get("emph", []):  # ударная волна вокруг важного слова
                pr_ = (tt - te) / 0.5
                if 0 <= pr_ < 1:
                    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    rad = 70 + 430 * pr_
                    ImageDraw.Draw(ov).ellipse((ex - rad, ey - rad, ex + rad, ey + rad), outline=YELLOW + (int(235 * (1 - pr_)),),
                                               width=max(3, int(16 * (1 - pr_))))
                    frame.paste(ov, (0, 0), ov)
            if kin:
                paste_kinetic(frame, kin, tt)
            if bars:
                paste_bars(frame, bars, tt)
            # ведущий
            mouth = sl["mouth"][k] if k < len(sl["mouth"]) else 0
            if bubble is None and sl.get("kind") != "bars":  # без пузыря диктор крупнее: на текстовых слайдах он главный собеседник
                hs = sprites.render(gt, mouth, kk, si, 1.3)
                frame.paste(hs, (0, 1495 - hs.height), hs)
            else:
                hs = sprites.render(gt, mouth, kk, si)
                frame.paste(hs, (HOST_X, MASCOT_Y), hs)
            if bubble is not None:
                bb = _scaled(bubble, 0.7 + 0.3 * pe) if k < POP_FRAMES else bubble
                frame.paste(bb, (400, BUBBLE_Y + (bubble.height - bb.height) // 2), bb)
            if footer is not None:
                frame.paste(footer, (FOOTER_X, FOOTER_Y), footer)
            for te, _ex, _ey in sl.get("emph", []):  # короткий наезд камеры на важное слово
                q = tt - te
                if 0 <= q < 0.18:
                    z = 1 + 0.045 * (1 - q / 0.18)
                    w2, h2 = int(W / z), int(H / z)
                    frame = frame.crop(((W - w2) // 2, (H - h2) // 2, (W - w2) // 2 + w2, (H - h2) // 2 + h2)).resize((W, H), Image.BILINEAR)
            if si > 0 and kk < 6:  # наезд камеры при смене слайда
                z = 1 + 0.07 * (1 - kk / 6)
                w2, h2 = int(W / z), int(H / z)
                frame = frame.crop(((W - w2) // 2, (H - h2) // 2, (W - w2) // 2 + w2, (H - h2) // 2 + h2)).resize((W, H), Image.BILINEAR)
            if si > 0 and kk < 4:  # RGB-сдвиг, как глитч
                amt = int(20 - 5 * kk)
                r, g, b_ = frame.split()
                frame = Image.merge("RGB", (ImageChops.offset(r, amt, 0), g, ImageChops.offset(b_, -amt, 0)))
            if si > 0 and kk < 8:  # диагональная полоса-шторка пролетает через кадр
                x0 = int(-520 + (kk / 7) * (W + 1100))
                dr = ImageDraw.Draw(frame)
                dr.polygon([(x0, 0), (x0 + 420, 0), (x0 + 160, H), (x0 - 260, H)], fill=bcolor)
                dr.polygon([(x0 + 420, 0), (x0 + 470, 0), (x0 + 210, H), (x0 + 160, H)], fill=DARK)
            if si > 0 and kk < 2:  # вспышка на склейке
                frame = Image.blend(frame, white, 0.35 - 0.15 * kk)
            frame.paste(logo, (0, 0), logo)
            dr = ImageDraw.Draw(frame)
            dr.ellipse((58, 62, 80, 84), fill=(255, 60, 70) if int(gt * 2) % 2 == 0 else (120, 30, 50))  # «эфир»
            for j, st0 in enumerate(starts):  # прогресс по слайдам: у каждого свой сегмент
                en = starts[j + 1] if j + 1 < len(starts) else total
                x0, x1 = int(W * st0 / total) + 3, int(W * en / total) - 3
                x1 = max(x1, x0)
                dr.rectangle((x0, 0, x1, 11), fill=(70, 50, 130))
                cur = done + k + 1
                if cur > st0:
                    dr.rectangle((x0, 0, max(x0, min(x1, int(W * cur / total))), 11), fill=YELLOW)
            yield frame.tobytes()
        done += n


VIDEO_CRF = os.environ.get("VIDEO_CRF") or "16"          # меньше = чище (14-18); 16 заметно лучше прежних 20
VIDEO_PRESET = os.environ.get("VIDEO_PRESET") or "fast"
TG_LIMIT = 49_000_000                                     # лимит Bot API 50 МБ с запасом


def video_maxrate(duration):
    """Потолок битрейта: чем короче ролик, тем выше качество, но файл всегда влезает в лимит Telegram."""
    if not duration:
        return 12_000_000
    return int(max(2_500_000, min(16_000_000, 46e6 * 8 / duration - 250_000)))


def encode_video(frames, out_path, duration=None):
    mr = video_maxrate(duration)
    vf = "scale=out_color_matrix=bt709:out_range=tv:flags=lanczos+accurate_rnd+full_chroma_int,format=yuv420p"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-vf", vf, "-c:v", "libx264", "-preset", VIDEO_PRESET, "-tune", "animation", "-crf", VIDEO_CRF,
           "-maxrate", str(mr), "-bufsize", str(2 * mr), "-profile:v", "high", "-level", "4.2", "-x264-params", "aq-mode=3",
           "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv", "-movflags", "+faststart", out_path]
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


def ensure_size(path, duration, limit=TG_LIMIT):
    """Запасной вариант: если готовый файл всё же больше лимита Telegram, пережимаем его под нужный битрейт."""
    if os.path.getsize(path) <= limit:
        return False
    target = int(limit * 8 / max(1.0, duration) - 200_000)
    tmp = path + ".small.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-c:v", "libx264", "-preset", VIDEO_PRESET, "-b:v", str(target),
         "-maxrate", str(int(target * 1.1)), "-bufsize", str(2 * target), "-pix_fmt", "yuv420p", "-colorspace", "bt709",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-c:a", "copy", "-movflags", "+faststart", tmp])
    os.replace(tmp, path)
    print("файл был больше лимита Telegram, пережат до", os.path.getsize(path) // 1000000, "МБ")
    return True


def _concat_wavs(parts, workdir, name):
    lst = os.path.join(workdir, name + ".txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{p}'\n" for p in parts))
    out = os.path.join(workdir, name + ".wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return out


SFX_MAKERS = {
    "whoosh": (["-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:r=44100"],
               "highpass=f=500,lowpass=f=7000,afade=t=in:d=0.18,afade=t=out:st=0.2:d=0.25,volume=0.8"),
    "pop": (["-f", "lavfi", "-i", "sine=frequency=760:duration=0.16:sample_rate=44100"],
            "afade=t=in:d=0.005,afade=t=out:st=0.03:d=0.13,volume=0.9"),
    "ding": (["-f", "lavfi", "-i", "sine=frequency=1175:duration=0.55:sample_rate=44100"],
             "afade=t=in:d=0.005,afade=t=out:st=0.05:d=0.5,volume=0.7"),
    "hit": (["-f", "lavfi", "-i", "sine=frequency=95:duration=0.32:sample_rate=44100"],
            "afade=t=in:d=0.003,afade=t=out:st=0.03:d=0.29,volume=1.6"),
}


def build_audio_tracks(slides, workdir):
    """Две дорожки по слайдам: голос (подогнан по длине слайда) и звук перехода (разный по типу слайда) в начале каждого."""
    kinds = {}
    for name, (src, af) in SFX_MAKERS.items():
        path = os.path.join(workdir, f"sfx_{name}.wav")
        run(["ffmpeg", "-y", "-loglevel", "error"] + src + ["-af", af, "-ar", "44100", "-ac", "2", path])
        kinds[name] = path
    voice_parts, sfx_parts = [], []
    for i, sl in enumerate(slides):
        v = os.path.join(workdir, f"vseg{i}.wav")
        if sl.get("voice_wav"):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", sl["voice_wav"], "-af", "apad", "-t", f"{sl['dur']:.3f}",
                 "-ar", "44100", "-ac", "2", v])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{sl['dur']:.3f}", v])
        s_ = os.path.join(workdir, f"sseg{i}.wav")
        hits = [te for te, _x, _y in sl.get("emph", []) if te < sl["dur"] - 0.1]
        if hits:  # звук перехода и «удары» на важных словах
            ins = ["-i", kinds.get(sl.get("sfx"), kinds["whoosh"])] + sum([["-i", kinds["hit"]] for _ in hits], [])
            flt = "[0:a]apad[a0];" + "".join(f"[{j + 1}:a]adelay={int(te * 1000)}|{int(te * 1000)}[h{j}];" for j, te in enumerate(hits))
            flt += "[a0]" + "".join(f"[h{j}]" for j in range(len(hits))) + f"amix=inputs={len(hits) + 1}:duration=first:normalize=0[o]"
            run(["ffmpeg", "-y", "-loglevel", "error"] + ins + ["-filter_complex", flt, "-map", "[o]", "-t", f"{sl['dur']:.3f}",
                                                                  "-ar", "44100", "-ac", "2", s_])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", kinds.get(sl.get("sfx"), kinds["whoosh"]), "-af", "apad",
                 "-t", f"{sl['dur']:.3f}", "-ar", "44100", "-ac", "2", s_])
        voice_parts.append(v)
        sfx_parts.append(s_)
    return _concat_wavs(voice_parts, workdir, "voice"), _concat_wavs(sfx_parts, workdir, "sfx")


LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"   # стандартная громкость для соцсетей: не тише и не громче чужих роликов


def mux(video, voice, sfx, music, total, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", voice, "-i", sfx]
    flt = (f"[1:a]acompressor=threshold=-20dB:ratio=3:attack=5:release=80[vc];[2:a]volume={SFX_VOLUME}[s];"
           "[vc][s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[v]")
    if music:
        off = 0.0
        try:  # у длинного трека каждый ролик стартует с другого места, чтобы ролики не звучали одинаково
            mdur = audio_duration(music)
            if mdur > total + 6:
                off = round(random.Random(today() + os.path.basename(music)).uniform(0, mdur - total - 3), 2)
        except Exception:
            off = 0.0
        cmd += (["-ss", f"{off}"] if off else []) + ["-stream_loop", "-1", "-i", music]
        # трек выравнивается по громкости (любой файл звучит одинаково), из него вырезается «карман» под голос
        # (убираем низкий гул и середину, где живёт речь), а при речи он приглушается (sidechain)
        flt += (f";[v]asplit=2[v1][v2];[3:a]loudnorm=I={MUSIC_LUFS}:TP=-2:LRA=7,highpass=f=70,"
                "equalizer=f=2400:t=q:w=1:g=-4,equalizer=f=500:t=q:w=1:g=-2,"
                f"afade=t=in:d=1,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
                "[m][v2]sidechaincompress=threshold=0.03:ratio=9:attack=15:release=400[md];"
                f"[v1][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{LOUDNORM}[a]")
    else:
        flt += f";[v]{LOUDNORM}[a]"
    cmd += ["-filter_complex", flt, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]
    run(cmd)


# ---- анимированный топ: столбики растут, проценты «набегают», портреты в кружках
BAR_COLORS = [(255, 205, 40), (200, 212, 230), (255, 150, 70), (80, 170, 255), (175, 105, 255)]
BARS_Y0, BARS_ROW_H, BARS_X0, BARS_X1 = 310, 128, 215, 1020


def _bar_badge(png, name, rank):
    """Портрет бойца в кружке с номером места (без портрета: инициалы)."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    S = 118
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, S - 1, S - 1), fill=(255, 255, 255, 255))
    face = Image.new("RGBA", (S - 14, S - 14), PURPLE + (255,))
    drawn = False
    if png:
        try:
            pm = _prep_portrait(Image.open(io.BytesIO(png)))
            k = min((S - 14) / pm.width, (S - 14) / pm.height)
            pm = pm.resize((max(1, int(pm.width * k)), max(1, int(pm.height * k))), Image.LANCZOS)
            _over(face, pm, (face.width - pm.width) // 2, face.height - pm.height)
            drawn = True
        except Exception:
            drawn = False
    if not drawn:
        ImageDraw.Draw(face).text((face.width // 2, face.height // 2), name[:2].upper(), font=bot.card_font(44),
                                  fill=(255, 255, 255, 255), anchor="mm")
    mask = Image.new("L", face.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, face.width - 1, face.height - 1), fill=255)
    im.paste(face, (7, 7), mask)
    d.ellipse((S - 46, S - 46, S - 2, S - 2), fill=DARK + (255,), outline=(255, 255, 255, 255), width=3)
    d.text((S - 24, S - 25), str(rank), font=bot.card_font(32), fill=(255, 255, 255, 255), anchor="mm")
    return im


def build_bars(src):
    """Готовит анимированный топ. src: {'rows': [{'name','value','portrait'}], 'lo': нижняя граница шкалы, 'note': подпись}."""
    rows = src["rows"][:5]
    lo = src.get("lo", 0.40)
    hi = max(r["value"] for r in rows) + 0.02
    items = []
    for i, r in enumerate(rows):
        frac = max(0.14, min(1.0, (r["value"] - lo) / max(0.01, hi - lo)))
        items.append({"name": r["name"].upper(), "value": r["value"], "frac": frac, "y": BARS_Y0 + i * BARS_ROW_H,
                      "spr": _bar_badge(r.get("portrait"), r["name"], i + 1), "color": BAR_COLORS[i], "t0": 0.25 + i * 0.18})
    return {"items": items, "note": src.get("note", "")}


def bars_end_x(it):
    return int(BARS_X0 + 6 + (BARS_X1 - BARS_X0 - 12) * it["frac"])


def paste_bars(frame, bars, tt):
    """Столбики растут по очереди; значение набегает вместе со столбиком."""
    d = bot.ImageDraw.Draw(frame)
    fn, fv = bot.card_font(44), bot.card_font(48)
    for i, it in enumerate(bars["items"]):
        if tt < it["t0"] - 0.1:
            continue
        p = max(0.0, min(1.0, (tt - it["t0"]) / 0.9))
        e = 1 - (1 - p) ** 3
        y = it["y"]
        frame.paste(it["spr"], (62, y + 2), it["spr"])
        d.rounded_rectangle((BARS_X0, y + 16, BARS_X1, y + 102), radius=43, fill=(24, 12, 60), outline=(255, 255, 255), width=4)
        fw = int((BARS_X1 - BARS_X0 - 12) * it["frac"] * e)
        if fw > 36:
            d.rounded_rectangle((BARS_X0 + 6, y + 22, BARS_X0 + 6 + fw, y + 96), radius=37, fill=it["color"], outline=DARK, width=4)
            light = tuple(min(255, int(c + (255 - c) * 0.45)) for c in it["color"])
            d.rounded_rectangle((BARS_X0 + 24, y + 28, BARS_X0 + 6 + fw - 18, y + 40), radius=6, fill=light)
        d.text((BARS_X0 + 30, y + 60), it["name"], font=fn, fill=(255, 255, 255), anchor="lm", stroke_width=4, stroke_fill=DARK)
        d.text((BARS_X1 - 22, y + 60), bot.pct(it["value"] * e), font=fv, fill=(255, 255, 255), anchor="rm",
               stroke_width=5, stroke_fill=DARK)
    if bars.get("note"):
        d.text((W // 2, BARS_Y0 + len(bars["items"]) * BARS_ROW_H + 14), bars["note"], font=bot.card_font(30),
               fill=(235, 230, 255), anchor="mm", stroke_width=3, stroke_fill=DARK)


# ---- обязательное уведомление Supercell для фан-контента (формулировка из Fan Content Policy)
FAN_NOTICE = ("This content is not affiliated with, endorsed, sponsored, or specifically approved by Supercell and "
              "Supercell is not responsible for it. For more information see Supercell's Fan Content Policy: "
              "www.supercell.com/fan-content-policy.")
FOOTER_X, FOOTER_Y, FOOTER_W = 400, 1215, 555


def make_disclaimer():
    """Плашка с уведомлением на финальном слайде (читаемым шрифтом, не перекрывает диктора и пузырь).
    Ссылка выводится отдельной строкой с подбором размера, чтобы не обрезаться."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    f = bot.card_font(26)
    main, _, url = FAN_NOTICE.rpartition(" ")
    lines = bot._wrap(_probe(), main, f, FOOTER_W - 36)
    lh = 32
    im = Image.new("RGBA", (FOOTER_W, (len(lines) + 1) * lh + 28), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=22, fill=(10, 5, 30, 228), outline=(255, 255, 255, 210), width=3)
    for i, line in enumerate(lines):
        d.text((18, 14 + i * lh + lh // 2), line, font=f, fill=(255, 255, 255, 255), anchor="lm")
    d.text((18, 14 + len(lines) * lh + lh // 2), url, font=bot._fit(d, url, FOOTER_W - 36, 26, 14), fill=(255, 255, 255, 255), anchor="lm")
    return im


def word_times(words, lead, span):
    """Когда звучит каждое слово: время делится пропорционально длине произносимого слова и паузам после знаков."""
    def weight(w):
        extra = 3.0 if w.endswith((",", ";", ":")) else 6.0 if w.endswith((".", "!", "?", "…")) else 0.0
        return max(1.0, len(spoken(w))) + extra
    ws = [weight(w["word"]) for w in words]
    lw = (len(spoken(lead)) + 4.0) if lead else 0.0
    total = (lw + sum(ws)) or 1.0
    t, out = span * lw / total, []
    for w in ws:
        out.append((t, t + span * w / total))
        t += span * w / total
    return out


KAR_LEAD, KAR_FADE = 0.07, 0.18


def karaoke_alpha(wd, tt):
    """Яркость подсветки слова в момент tt (0..1) и признак «слово уже прозвучало»: плавно разгорается и гаснет."""
    if tt < wd["t0"] - KAR_LEAD:
        return 0.0, False
    if tt < wd["t0"]:
        return (tt - (wd["t0"] - KAR_LEAD)) / KAR_LEAD, False
    if tt < wd["t1"]:
        return 1.0, False
    return max(0.0, 1.0 - (tt - wd["t1"]) / KAR_FADE), True


_HOT_NAMES = None


def _is_hot(word):
    """Цифры, проценты, сокращения и имена бойцов выделяются цветом."""
    global _HOT_NAMES
    if _HOT_NAMES is None:
        _HOT_NAMES = {x.lower() for x in list(NAME_SAY.values()) + list(bot.BRAWLERS)}
    w = re.sub(r"[^\w%]", "", word)
    return bool(re.search(r"\d", w)) or (len(w) > 1 and w.isupper()) or w.lower() in _HOT_NAMES


def _kin_fit(words, minimum=58):
    """Шрифт и разбивка на строки (до двух), при которых фраза целиком влезает по ширине."""
    probe = _probe()
    for size in range(150, minimum - 1, -6):
        f = bot.card_font(size)
        lines, cur, ok = [], [], True
        for w in words:
            if probe.textlength(w, font=f) > KIN_W:
                ok = False
                break
            if cur and probe.textlength(" ".join(cur + [w]), font=f) > KIN_W:
                lines.append(cur)
                cur = []
            cur.append(w)
        if ok:
            lines.append(cur)
            if len(lines) <= 2:
                return f, lines
    return None


def _fit_group(words, g):
    if len(g) == 1 or _kin_fit([words[i] for i in g], 96):  # из нескольких слов экран только с крупным шрифтом
        return [g]
    half = len(g) // 2
    return _fit_group(words, g[:half]) + _fit_group(words, g[half:])


def split_phrases(words, maxw=3):
    """Делит слова на короткие экраны по 1-3 слова (по знакам препинания), ничего не теряя. Возвращает списки индексов."""
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(i)
        end = w.rstrip("»\"')").endswith((".", "!", "?", "…"))
        if len(cur) >= maxw or end or (w.endswith((",", ";", ":", "—")) and len(cur) >= 2):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    if len(groups) > 1 and len(groups[-1]) == 1 and len(groups[-2]) < maxw and not words[groups[-2][-1]].endswith((".", "!", "?", "…")):
        groups[-2:] = [groups[-2] + groups[-1]]  # одинокое слово в хвосте приклеиваем к предыдущему экрану
    out = []
    for g in groups:
        out.extend(_fit_group(words, g))
    return out


def _kin_sprite(word, f, hot, cur, angle):
    """Слово крупными объёмными буквами: обычное (белое), «звучащее» (жёлтое) или выделенное (бирюзовое)."""
    probe = _probe()
    pad = int(f.size * 0.55)
    w, h = int(probe.textlength(word, font=f)) + 2 * pad, int(f.size * 1.4) + 2 * pad
    lay = bot.Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if hot:
        top, bottom = ((255, 255, 255), (110, 235, 255)) if cur else ((190, 255, 235), (60, 225, 150))
    else:
        top, bottom = ((255, 252, 200), (255, 196, 0)) if cur else ((255, 255, 255), (212, 205, 255))
    if hot:  # «маркер» под важным словом (цифра, имя, сокращение)
        bh, by = max(12, int(f.size * 0.17)), h // 2 + int(f.size * 0.43)
        bot.ImageDraw.Draw(lay).rounded_rectangle((pad - 8, by, w - pad + 8, by + bh), radius=bh // 2, fill=(255, 70, 150, 240),
                                                  outline=DARK + (255,), width=3)
    grad_text(lay, (w // 2, h // 2), word, f, top=top, bottom=bottom, stroke=max(6, int(f.size * 0.085)), outline=DARK,
              depth=max(8, int(f.size * 0.12)))
    return lay.rotate(angle, resample=bot.Image.BICUBIC) if angle else lay


def emphasis_events(kin, maxn=4, gap=0.9):
    """Моменты, когда звучит важное слово: [(время, x, y)]. Не чаще раза в gap секунд и не больше maxn на слайд."""
    out = []
    for ph in kin:
        for w in ph["words"]:
            if w.get("hot") and (not out or w["t0"] - out[-1][0] >= gap) and len(out) < maxn:
                out.append((w["t0"], w["cx"], w["cy"]))
    return out


def build_kinetic(words, times):
    """Готовит экраны со словами: для каждого слова два спрайта (обычный и «звучащий»), позиция и время звучания."""
    probe = _probe()
    texts = [w["word"] for w in words]
    phrases = []
    for g in split_phrases(texts):
        ws = [texts[i] for i in g]
        fit = _kin_fit(ws) or _kin_fit(ws, minimum=30)
        f, lines = fit if fit else (bot.card_font(30), [ws])
        lh = int(f.size * 1.12)
        y0 = KIN_CY - lh * (len(lines) - 1) / 2
        items, idx = [], 0
        for li, line in enumerate(lines):
            yc = y0 + li * lh
            x0 = W / 2 - probe.textlength(" ".join(line), font=f) / 2
            for j, w in enumerate(line):
                wx = x0 + probe.textlength(" ".join(line[:j]) + (" " if j else ""), font=f) + probe.textlength(w, font=f) / 2
                gi = g[idx]
                idx += 1
                hot, ang = _is_hot(w), (-3, 2, -2, 3, 0)[gi % 5]
                items.append({"n": _kin_sprite(w, f, hot, False, ang), "c": _kin_sprite(w, f, hot, True, ang),
                              "cx": wx, "cy": yc, "t0": times[gi][0], "t1": times[gi][1], "hot": hot})
        phrases.append({"start": times[g[0]][0], "words": items})
    return phrases


def paste_kinetic(frame, kin, tt):
    """Рисует текущий экран: слова «выскакивают» по мере озвучки, звучащее слово плавно желтеет и чуть крупнее."""
    ph = None
    for p in kin:
        if tt + 0.03 >= p["start"]:
            ph = p
        else:
            break
    if ph is None:
        ph = kin[0]  # до начала речи показываем бледный предпросмотр первого экрана
    for w in ph["words"]:
        age = tt - w["t0"] + 0.03
        if age < 0:  # слово ещё не прозвучало: бледная подсказка на своём месте
            ghost = w["n"].copy()
            ghost.putalpha(ghost.getchannel("A").point(lambda v: int(v * 0.30)))
            frame.paste(ghost, (int(w["cx"] - ghost.width / 2), int(w["cy"] - ghost.height / 2)), ghost)
            continue
        al, _ = karaoke_alpha(w, tt)
        spr = w["n"] if al <= 0.01 else (w["c"] if al >= 0.99 else bot.Image.blend(w["n"], w["c"], al))
        sc = (0.45 + 0.55 * _ease_out_back(min(1.0, age / 0.18))) * (1 + 0.05 * al)
        if w.get("hot"):  # важное слово «бьёт» сильнее: крупный отскок
            sc *= 1 + 0.28 * max(0.0, 1 - age / 0.32)
        spr = _scaled(spr, sc)
        frame.paste(spr, (int(w["cx"] - spr.width / 2), int(w["cy"] - spr.height / 2)), spr)


def render_video(specs, out_path, vstate=None, seed=0, host=None):
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
            wav, segs = speak(sp, i, workdir, first=(i == 0))
            if wav:
                voiced += 1
                dur = max(MIN_SLIDE_SEC, audio_duration(wav) + PAD_SEC, sp.get("min", 0))
            else:
                dur = max(MIN_SLIDE_SEC, 1.0 + len(sp.get("caption", "")) / 14, sp.get("min", 0))
            slides.append({"layer": sp["layer"], "caption": sp.get("caption", ""), "voice_wav": wav, "dur": dur,
                           "voice_dur": audio_duration(wav) if wav else 0.0, "priority": sp.get("priority", 5),
                           "group": sp.get("group") or f"s{i}", "words": sp.get("words"), "lead": sp.get("lead", ""),
                           "accent": sp.get("accent"), "sfx": sp.get("sfx", "whoosh"), "kind": sp.get("kind", ""),
                           "segs": segs, "bars_src": sp.get("bars")})
        while sum(s["dur"] for s in slides) > MAX_VIDEO_SEC and len(slides) > 2:
            groups = {}
            for sl in slides[1:-1]:
                groups.setdefault(sl["group"], []).append(sl)
            if not groups:
                break
            gid = min(groups, key=lambda g: (max(x["priority"] for x in groups[g]), -sum(x["dur"] for x in groups[g])))
            for sl in groups[gid]:  # выкидываем самый неважный блок целиком: половина мысли хуже, чем её отсутствие
                slides.remove(sl)
            print("не влезаем в лимит длины, убран блок:", gid)
        if sum(s["dur"] for s in slides) > MAX_VIDEO_SEC:
            print(f"ролик длиннее {MAX_VIDEO_SEC} с даже после сокращения, оставляю как есть")
        for idx, sl in enumerate(slides):
            n = max(1, round(sl["dur"] * FPS))
            sl["anim"] = idx
            if sl.get("words"):
                span = sl["voice_dur"] if sl.get("voice_wav") else max(0.5, sl["dur"] - 0.3)
                lead_n = len(sl.get("lead", "").split())
                times = (word_times_seg(sl["words"], sl["segs"], lead_n) if sl.get("segs")
                         else word_times(sl["words"], sl.get("lead", ""), span))
                sl["kin"] = build_kinetic(sl["words"], times)
                sl["emph"] = emphasis_events(sl["kin"])
            if sl.get("bars_src"):  # анимированный топ: без взрыва за столбиками, чтобы цифры читались
                sl["bars"] = build_bars(sl["bars_src"])
                sl["burst"] = False
                first = sl["bars"]["items"][0]
                sl["emph"] = [(first["t0"] + 0.9, bars_end_x(first), first["y"] + 60)]  # «удар» на столбике лидера
            if sl["kind"] == "cta":
                sl["footer"] = make_disclaimer()
            sl["burst_y"] = BURST_Y.get(sl["kind"], 560)
            sl["props"] = PROPS.get(sl["kind"], [])
            sl["content"] = _crop_layer(_orient(sl["layer"]))
            sl["bubble"] = make_bubble(sl["caption"]) if sl["caption"] else None
            sl["mouth"] = [mouth_state(v) for v in (envelope(sl["voice_wav"], FPS, n) if sl["voice_wav"] else [0.0] * n)]
        total_sec = sum(s["dur"] for s in slides)
        total_frames = sum(max(1, round(s["dur"] * FPS)) for s in slides)
        silent = os.path.join(workdir, "silent.mp4")
        sprites = None
        if host:
            try:
                sprites = make_host(*host)
            except Exception as ex:
                print("диктор-персонаж не получился, беру робота:", ex)
        encode_video(render_frames(slides, [make_bg(seed, i) for i in range(3)], make_particles(seed), sprites or make_robot_host(), total_frames), silent, total_frames / FPS)
        voice_track, sfx_track = build_audio_tracks(slides, workdir)
        music = pick_music(vstate)
        mux(silent, voice_track, sfx_track, music, total_sec, out_path)
        ensure_size(out_path, total_sec)
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


CTA_SPEECHES = [CTA_SPEECH,
                "Свежая мета и гайды каждый день в телеграм-канале. Сканируй код и подписывайся!",
                "Не пропускай новости Бравл Старс! Сканируй код и жми подписаться!"]
HOOKS = [("Кто сильнее всех?", "мета Brawl Stars сегодня", "Мета Бравл Старс сегодня! Кто сильнее всех?"),
         ("Кто ломает мету?", "топ бойцов за сутки", "Вот кто сейчас ломает мету в Бравл Старс!"),
         ("Топ бойцов дня", "что берут лучшие игроки", "Топ бойцов за сутки! Угадаешь, кто первый?")]


def cta_spec(question=""):
    """Финальный кадр: вопрос зрителю (комментарии усиливают охват) и призыв подписаться."""
    speech = random.Random(today() + "cta").choice(CTA_SPEECHES)  # фразы чередуются по дням: ролики не слипаются в один
    if question:
        speech = f"{question} Пиши в комментариях! {speech}"
    return {"layer": L_cta(CTA_HANDLE, QR_URL), "speech": speech, "caption": "Сканируй QR и подписывайся!",
            "priority": 10, "min": CTA_MIN_SEC, "sfx": "ding", "accent": YELLOW, "kind": "cta"}


def script_meta(meta):
    """Ролик «Мета сегодня» по данным, которые бот сохранил после публикации меты."""
    rows = bot.rank_rows(meta["stats"], bot.META_MIN_PICKS)
    if len(rows) < 3:
        return None
    top3 = rows[:3]
    total = meta["battles"] * 6
    pop = max(rows, key=lambda r: r[1])
    por = _portraits([r[0] for r in top3] + [pop[0]])
    title, sub, say = random.Random(today() + "hook").choice(HOOKS)
    specs = [{"layer": L_title(title, sub), "speech": say, "caption": "Мета Brawl Stars сегодня", "priority": 9,
              "accent": PURPLE, "kind": "title"}]
    for rank, accent in ((3, BLUE), (2, PINK), (1, ORANGE)):
        n, pk, w, wr, lb = top3[rank - 1]
        say = (f"И первое место: {n}!" if rank == 1 else f"{ORD[rank]} место: {n}.") + f" {bot.pct(wr)} побед."
        specs.append({"layer": L_hero(n, f"{bot.pct(wr)} побед", f"пикрейт {bot.pct(pk / total)}", por.get(n.upper()), rank),
                      "speech": say, "caption": f"{rank} место: {n}, {bot.pct(wr)} побед",
                      "priority": 8 if rank == 1 else 6, "accent": accent, "sfx": "ding" if rank == 1 else "whoosh", "kind": "hero"})
    if pop[0] not in [r[0] for r in top3]:
        specs.append({"layer": L_hero(pop[0], "самый популярный", f"{bot.pct(pop[3])} побед", por.get(pop[0].upper())),
                      "speech": f"Самый популярный: {pop[0]}. Его берут в {bot.pct(pop[1] / total)} боёв.",
                      "caption": f"Самый популярный: {pop[0]}", "priority": 3, "accent": GREEN, "kind": "hero"})
    if len(rows) >= 5:  # анимированный топ-5: цифры вместо слов
        top5 = rows[:5]
        por.update(_portraits([r[0] for r in top5 if r[0].upper() not in por]))
        specs.append({"layer": L_kinetic("Топ-5 по рейтингу", BLUE), "kind": "bars", "priority": 4, "accent": BLUE, "caption": "",
                      "speech": "А вот весь топ пять по рейтингу: " + ", ".join(r[0] for r in top5) + ".",
                      "bars": {"rows": [{"name": r[0], "value": r[3], "portrait": por.get(r[0].upper())} for r in top5],
                               "lo": min(0.40, min(r[3] for r in top5) - 0.05), "note": "шкала начинается не с нуля"}})
    specs.append(cta_spec("Кто для тебя номер один?"))
    names = ", ".join(r[0] for r in top3)
    top1 = top3[0][0]
    return {"kind": "meta", "specs": specs, "host": (por[top1.upper()], top1) if por.get(top1.upper()) else None,
            "caption": f"🔥 Мета Brawl Stars сегодня: {names}. Кого берёте вы?"}


def _join_names(names, limit=5):
    names = list(names)
    if len(names) > limit:
        return ", ".join(names[:limit]) + " и другие"
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " и " + names[-1]


def script_balance(bl):
    """Ролик «Баланс»: кого усилили и ослабили. Данные бот сохраняет сразу после публикации разбора баланса в канале."""
    buffs, nerfs = list(bl.get("buffs") or []), list(bl.get("nerfs") or [])
    if not (buffs or nerfs):
        return None
    first = (buffs or nerfs)[0]
    por = _portraits([first]).get(first.upper())
    specs = [{"layer": L_title("Баланс изменился!", "разбор за минуту"), "kind": "title", "priority": 10, "accent": ORANGE,
              "speech": "Баланс в Бравл Старс изменился! Разбираем, кого усилили и кого ослабили.", "caption": "Обновление баланса"}]
    if buffs:
        specs += text_specs("Усилили", _join_names(buffs) + ".", GREEN, 8, say_first="Усилили.")
    if nerfs:
        specs += text_specs("Ослабили", _join_names(nerfs) + ".", RED, 8, say_first="Ослабили.")
    meaning = ("Усиленных бойцов, скорее всего, станут брать чаще, а ослабленных реже." if buffs and nerfs else
               "Усиленных бойцов, скорее всего, станут брать чаще." if buffs else
               "Ослабленных бойцов, скорее всего, будут брать реже.")
    specs += text_specs("Что это значит", meaning, BLUE, 6)
    specs.append(cta_spec("Согласен с изменениями?"))
    tail = ("усилили " + ", ".join(buffs[:4]) if buffs else "") + ("; " if buffs and nerfs else "") + \
           ("ослабили " + ", ".join(nerfs[:4]) if nerfs else "")
    return {"kind": "balance", "specs": specs, "host": (por, first) if por else None,
            "caption": f"⚖️ Баланс Brawl Stars: {tail}. Согласны с изменениями?"}


def script_newbrawler(nb):
    """Ролик «Новый боец» по данным официального API: имя, гаджеты, звёздные силы."""
    name = nb.get("name")
    if not name:
        return None
    gad, stars, hyp = nb.get("gadgets") or [], nb.get("stars") or [], nb.get("hypers") or []
    por = _portraits([name]).get(name.upper())
    parts = []
    if gad:
        parts.append(f"{num_words(len(gad))} {plural(len(gad), 'гаджет', 'гаджета', 'гаджетов')}")
    if stars:
        parts.append(f"{num_words(len(stars), True)} {plural(len(stars), 'звёздная сила', 'звёздные силы', 'звёздных сил')}")
    hero_say = f"Это {name}!" + (f" У него {' и '.join(parts)}." if parts else "")
    specs = [{"layer": L_title("Новый боец!", "знакомьтесь"), "kind": "title", "priority": 10, "accent": PINK,
              "speech": f"Новый боец в Бравл Старс! Знакомьтесь: {name}!", "caption": "Новый боец"},
             {"layer": L_hero(name, "новый боец", f"{len(gad)} гаджета" if gad else "", por), "kind": "hero", "priority": 9,
              "accent": ORANGE, "sfx": "ding", "speech": hero_say, "caption": f"Новый боец: {name}"}]
    if gad:
        specs += text_specs("Гаджеты", _join_names(gad, 4) + ".", ORANGE, 7, say_first="Гаджеты.")
    if stars:
        specs += text_specs("Звёздные силы", _join_names(stars, 4) + ".", PINK, 7, say_first="Звёздные силы.")
    if hyp:
        specs += text_specs("Гиперзаряд", _join_names(hyp, 3) + ".", PURPLE, 5, say_first="Гиперзаряд.")
    specs.append(cta_spec("Берёшь нового бойца?"))
    return {"kind": "newbrawler", "specs": specs, "host": (por, name) if por else None,
            "caption": f"🆕 Новый боец в Brawl Stars: {name}. Берёте?"}


def script_week(wl):
    """Ролик «Мета недели» из данных, которые бот сохранил при публикации недельного поста."""
    rows = wl.get("rows") or []
    if len(rows) < 3:
        return None
    top5 = rows[:5]
    por = _portraits([r[0] for r in top5])
    vals = [r[1] for r in top5]
    first = top5[0]
    specs = [{"layer": L_title("Мета недели", f"топ бойцов за {wl.get('ndays', 7)} дн."), "kind": "title", "priority": 10,
              "accent": PURPLE, "speech": "Мета Бравл Старс за неделю! Кто был сильнее всех?", "caption": "Мета недели"},
             {"layer": L_kinetic("Топ-5 недели", BLUE), "kind": "bars", "priority": 8, "accent": BLUE, "caption": "",
              "speech": "Вот топ пять недели по рейтингу: " + ", ".join(r[0] for r in top5) + ".",
              "bars": {"rows": [{"name": r[0], "value": r[1], "portrait": por.get(r[0].upper())} for r in top5],
                       "lo": min(0.40, min(vals) - 0.05), "note": "шкала начинается не с нуля"}},
             {"layer": L_hero(first[0], f"{bot.pct(first[1])} побед", f"в рейтинге дней: {first[3]}", por.get(first[0].upper()), 1),
              "kind": "hero", "priority": 7, "accent": ORANGE, "sfx": "ding",
              "speech": f"Боец недели: {first[0]}! {bot.pct(first[1])} побед.", "caption": f"Боец недели: {first[0]}"},
             cta_spec("Согласен с таким топом?")]
    names = ", ".join(r[0] for r in top5[:3])
    return {"kind": "week", "specs": specs, "host": (por[first[0].upper()], first[0]) if por.get(first[0].upper()) else None,
            "caption": f"🏆 Мета недели в Brawl Stars: {names}. Согласны с таким топом?"}


def script_quiz(state):
    """Рубрика «Угадай бойца по пикселям»: портрет проясняется по этапам (крупная мозаика, мельче, затем ответ).
    Подсказка: класс из данных Brawlify. Диктор в этом ролике без портрета, чтобы не выдать ответ."""
    cmap = (state.get("classes") or {}).get("map") or {}
    pool = sorted(n for n, c in cmap.items() if c in bot.CLASSES_RU)
    if not pool:
        return None
    who = random.Random(today() + "quiz").choice(pool)
    name, cls = bot.nice_name(who), bot.CLASSES_RU[cmap[who]]
    por = _portraits([name]).get(name.upper())
    if not por:
        print("викторина: нет портрета бойца, ролик пропускаю")
        return None
    specs = [{"layer": L_title("Угадай бойца!", "по пикселям"), "kind": "title", "priority": 10, "accent": PINK,
              "speech": "Угадай бойца по пикселям! Время пошло.", "caption": "Угадай бойца"},
             {"layer": L_hero("?????", f"класс: {cls}", "", por, None, pixelate=9), "kind": "hero", "priority": 10,
              "accent": PURPLE, "speech": f"Подсказка: он из класса {cls}. Пиши ответ в комментариях!", "caption": "Кто это?"},
             {"layer": L_hero("?????", "становится яснее", "", por, None, pixelate=20), "kind": "hero", "priority": 10,
              "accent": BLUE, "speech": "Три... Два... Один...", "caption": "Ну что, узнал?"},
             {"layer": L_hero(name, cls, "", por), "kind": "hero", "priority": 10, "accent": ORANGE, "sfx": "ding",
              "speech": f"Это {name}! Угадал?", "caption": f"Это {name}!"},
             cta_spec("Угадал? Пиши ответ!")]
    return {"kind": "quiz", "specs": specs, "host": None,
            "caption": "🎯 Угадай бойца Brawl Stars по пикселям! Ответ в конце ролика. Угадали?"}


def script_guide(b, state):
    """Ролик «Гайд на бойца» по тем же данным, что и гайд в канале. Тексты не режутся: длинное делится на экраны."""
    info = bot.guide_info(b, state)
    por = _portraits([info["name"]]).get(info["name"].upper())
    specs = [{"layer": L_hero(info["name"], info["cls_ru"] or "гайд", info["short_stat"], por),
              "speech": f"Гайд на бойца {info['name']}!" + (f" Это {info['cls_ru']}." if info["cls_ru"] else ""),
              "caption": f"Гайд: {info['name']}", "priority": 10, "accent": PURPLE, "kind": "hero"}]
    if info["desc"]:
        specs += text_specs("Кто это", info["desc"], BLUE, 4)
    for label, rows, accent, prio in (("Гаджет", info["gadgets"], ORANGE, 7), ("Звёздная сила", info["stars"], PINK, 7),
                                      ("Гиперзаряд", info.get("hypers") or [], PURPLE, 5)):
        for n, d in [r for r in rows if r[1]][:1]:
            specs += text_specs(label, f"{n}: {d}", accent, prio, say_first=label + ".")
    if info["short_stat"]:
        specs += text_specs("Данные топ-игроков", f"У лучших игроков мира {info['short_stat']}.", GREEN, 6)
    if info["tip"]:
        specs += text_specs("Как играть", info["tip"], BLUE, 8)
    specs += text_specs("Важно", "Это универсальный разбор. Выбирай под карту и режим.", RED, 9)
    specs.append(cta_spec("Играешь за этого бойца?"))
    if len(specs) < 4:
        return None
    return {"kind": "guide", "specs": specs, "host": (por, info["name"]) if por else None,
            "caption": f"📘 Гайд на {info['name']} в Brawl Stars: что умеет и как играть. Играете за него?"}


DEMO_LONG = ("Длинный текст теперь не обрезается и не теряет смысл. Если он не помещается на один экран, "
             "ролик сам делит его на несколько частей, а голос читает ровно то, что написано. "
             "Слова подсвечиваются в такт озвучке, чтобы глаз не отставал от звука.")


def script_demo():
    specs = [{"layer": L_title("Тест ролика", "проверка сборки"), "speech": "Привет! Это тест автоматической сборки роликов.",
              "caption": "Тест автоматической сборки", "priority": 9, "accent": PURPLE, "kind": "title"}]
    specs += text_specs("Всё работает", "Голос, ведущий, QR-код и переходы собраны автоматически.", ORANGE, 5)
    specs += text_specs("Длинный текст", DEMO_LONG, BLUE, 5)
    specs.append(cta_spec())
    return {"kind": "demo", "caption": "Тестовый ролик", "specs": specs}


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
    return f"{text}{handle}\n\n{HASHTAGS}\n\n{FAN_NOTICE}"


def channel_caption(text):
    """Подпись для ролика в самом канале (лимит Telegram 1024 знака): текст, обязательное уведомление и хештеги."""
    tags = "#BrawlStars #бравлстарс #видео"
    room = 1000 - len(FAN_NOTICE) - len(tags) - 8
    return f"{text[:max(80, room)]}\n\n{FAN_NOTICE}\n\n{tags}"


def tg_send_video(path, caption, duration=0, chat_id=None, silent=False):
    """Отправляет готовый ролик в личный чат с ботом (до 50 МБ)."""
    chat_id = chat_id or VIDEO_CHAT_ID
    if not chat_id or not bot.TOKEN:
        return False
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", chat_id), ("caption", caption[:1000]), ("supports_streaming", "true"),
                      ("width", W), ("height", H), ("duration", int(duration))) + ((("disable_notification", "true"),) if silent else ()):
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


def tg_send_document(path, caption, chat_id=None):
    """Отправляет тот же файл как документ: Telegram не пережимает документы, это оригинал без потери качества."""
    chat_id = chat_id or VIDEO_CHAT_ID
    if not chat_id or not bot.TOKEN:
        return False
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", chat_id), ("caption", caption[:1000]), ("disable_content_type_detection", "true")):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"\r\n'
                'Content-Type: video/mp4\r\n\r\n').encode())
    with open(path, "rb") as f:
        body.write(f.read())
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot.TOKEN}/sendDocument", data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=300).read()
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


def tg_send_audio(path, caption, chat_id=None):
    chat_id = chat_id or VIDEO_CHAT_ID
    if not chat_id or not bot.TOKEN:
        return False
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for name, val in (("chat_id", chat_id), ("caption", caption[:900])):
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    body.write((f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="voice.mp3"\r\n'
                'Content-Type: audio/mpeg\r\n\r\n').encode())
    with open(path, "rb") as f:
        body.write(f.read())
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot.TOKEN}/sendAudio", data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    urllib.request.urlopen(req, timeout=120).read()
    return True


def voice_test():
    """python make_video.py voicetest: шлёт в личку одну фразу тремя вариантами, чтобы на слух выбрать лучший.
    Как озвучка читает ударения, проверить без прослушивания нельзя, поэтому выбор за тобой."""
    sample = ("Мета Бравл Старс сегодня! Мортис, Эдгар и Кроу. Гаджет и звёздная сила дают 53,4% побед. "
              "Пикрейт Леона: 12%. Угадаешь, кто первый? Сканируй код и подписывайся!")
    os.makedirs(OUT_DIR, exist_ok=True)
    for tag, stress, rate in (("A: обычная", "", TTS_RATE), ("B: со знаками ударения", "acute", TTS_RATE),
                              ("C: помедленнее", "", "+0%")):
        globals()["TTS_STRESS"], old_rate = stress, globals()["TTS_RATE"]
        globals()["TTS_RATE"] = rate
        try:
            text = spoken(sample)
            out = tts(text, os.path.join(OUT_DIR, f"voice_{tag[0]}.mp3"))
        finally:
            globals()["TTS_RATE"] = old_rate
        print(tag, "->", text)
        if out:
            try:
                tg_send_audio(out, f"Вариант {tag}")
            except Exception as ex:
                print("не отправилось:", ex)
    globals()["TTS_STRESS"] = ""
    try:  # D: живая интонация, как в роликах (по умолчанию включена, TTS_STYLE=plain выключает)
        wav, _ = speak({"speech": sample, "kind": ""}, 0, OUT_DIR, first=True)
        if wav:
            mp3 = os.path.join(OUT_DIR, "voice_D.mp3")
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", mp3])
            tg_send_audio(mp3, "Вариант D: живая интонация (как в роликах)")
    except Exception as ex:
        print("вариант D не получился:", ex)


def deliver(result, caption, kind=""):
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
    if sent and os.environ.get("SEND_ORIGINAL", "1") != "0":
        try:  # оригинал файлом: в приложении Telegram видео из sendVideo может показываться пережатым
            tg_send_document(result["path"], "Оригинал без сжатия: сохраняй этот файл и загружай в TikTok")
            print("оригинал отправлен документом")
        except Exception as ex:
            print("оригинал документом не отправился:", ex)
    if kind != "demo" and os.environ.get("POST_VIDEO_TO_CHANNEL", "1") != "0" and bot.CHAT:
        try:  # тот же ролик в канал: подписчики смотрят его там же, ночью без звука уведомления
            quiet = bot.in_quiet_hours(datetime.datetime.now(datetime.timezone.utc))
            if tg_send_video(result["path"], channel_caption(caption), result["duration"], chat_id=bot.CHAT, silent=quiet):
                print("ролик опубликован в канале")
                sent = True
        except Exception as ex:
            print("ролик не опубликовался в канале (бот должен быть админом с правом публикации):", ex)
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


VIDEO_KINDS = ("balance", "newbrawler", "meta", "guide", "week", "quiz")  # порядок важности: сначала срочное


def pending_jobs(state, vstate, force=()):
    """Какие ролики пора делать. Срочное (баланс, новый боец) собирается в течение часа после события,
    остальное после публикации в канале: мета, гайд, мета недели, по средам викторина."""
    day, made, jobs = today(), vstate["made"], []
    now = datetime.datetime.now(datetime.timezone.utc)
    bl = state.get("balance_last") or {}
    if "balance" in force or (bl.get("date") == day and (bl.get("buffs") or bl.get("nerfs")) and f"balance:{day}" not in made):
        jobs.append("balance")
    nb = state.get("new_brawler") or {}
    try:
        fresh = bool(nb.get("name")) and (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(nb.get("date", "2000-01-01"))).days <= 2
    except Exception:
        fresh = False
    if "newbrawler" in force or (fresh and f"newbrawler:{nb['name']}" not in made):
        jobs.append("newbrawler")
    if "meta" in force or ((state.get("meta") or {}).get("date") == day and f"meta:{day}" not in made):
        jobs.append("meta")
    if "guide" in force or (state.get("guide_posted") == day and f"guide:{day}" not in made):
        jobs.append("guide")
    wk = state.get("week_last") or {}
    if "week" in force or (wk.get("key") and wk.get("key") == bot.week_key(day) and f"week:{wk['key']}" not in made):
        jobs.append("week")
    if "quiz" in force or (datetime.date.fromisoformat(day).weekday() == 2 and now.hour >= 13 and f"quiz:{day}" not in made
                           and (state.get("classes") or {}).get("map")):
        jobs.append("quiz")
    return jobs


def job_key(kind, state):
    """Ключ в videos.json: по нему ролик не собирается дважды."""
    day = today()
    if kind == "newbrawler":
        return f"newbrawler:{(state.get('new_brawler') or {}).get('name', '')}"
    if kind == "week":
        return f"week:{(state.get('week_last') or {}).get('key', day)}"
    return f"{kind}:{day}"


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
    if kind == "balance":
        return script_balance(state.get("balance_last") or {})
    if kind == "newbrawler":
        return script_newbrawler(state.get("new_brawler") or {})
    if kind == "week":
        return script_week(state.get("week_last") or {})
    if kind == "quiz":
        return script_quiz(state)
    if kind == "demo":
        return script_demo()
    return None


def count_made_today(vstate, day):
    return sum(1 for k, v in vstate["made"].items() if k.endswith(day) or (isinstance(v, dict) and v.get("day") == day))


def check_pending():
    """python make_video.py check: есть ли что собирать. Результат в лог и в GITHUB_OUTPUT (pending=true/false).
    Без этого каждый часовой запуск зря ставил бы ffmpeg и шрифты."""
    state, vstate = bot.load_state(), load_vstate()
    jobs = pending_jobs(state, vstate)
    room = VIDEO_DAILY_LIMIT - count_made_today(vstate, today())
    jobs = jobs[:max(0, room)]
    print("к сборке:", ", ".join(jobs) if jobs else "ничего")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"pending={'true' if jobs else 'false'}\n")
    return jobs


def main(argv=None):
    argv = argv or sys.argv[1:]
    mode = argv[0] if argv else "auto"
    if mode == "voicetest":
        return voice_test()
    if mode == "check":
        return check_pending()
    bot.ensure_pillow()
    if setup_fonts():
        print("шрифт Russo One подключён")
    print(bot.cards_status())
    state = bot.load_state()
    vstate = load_vstate()
    jobs = pending_jobs(state, vstate) if mode == "auto" else [mode]
    day = today()
    made_today = count_made_today(vstate, day)
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
            res = render_video(script["specs"], out, vstate, seed=len(kind), host=script.get("host"))
            print(f"ролик собран: {out}, {res['duration']:.1f} с, слайдов {res['slides']}, "
                  f"голос: {'да' if res['voiced'] else 'нет'}, музыка: {res['music'] or 'нет'}")
            delivered = deliver(res, script["caption"], kind)
            if kind != "demo":
                vstate["made"][job_key(kind, state)] = {"delivered": bool(delivered), "sec": round(res["duration"], 1), "day": day}
                made_today += 1
        except Exception as ex:
            errors += 1
            print(f"ролик «{kind}» не получился:", ex)
    save_vstate(vstate)
    if errors and errors == len(jobs):
        sys.exit(1)


if __name__ == "__main__":
    main()
