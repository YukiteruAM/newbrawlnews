"""Фабрика коротких вертикальных видео для рекламы канала (TikTok, YouTube Shorts, Reels, VK Клипы).

Что делает: берёт данные самого канала (мета бойцов, гайд дня), рисует слайды, озвучивает их русским голосом,
накладывает субтитры и тихую музыку, собирает ролик 1080x1920 и присылает его в личный чат с ботом.
Дальше остаётся нажать «Поделиться» в TikTok. Если у тебя есть доступ к официальному API TikTok, ролик
можно также отправлять в черновики автоматически (см. tiktok_upload).

Запуск:  python make_video.py auto     собрать ролики, которые готовы на сегодня (мета, гайд)
         python make_video.py meta     мета, даже если сегодня уже была
         python make_video.py guide    гайд на бойца, даже если сегодня уже был
         python make_video.py demo     тестовый ролик (проверка озвучки и сборки)
Нужны: ffmpeg, Pillow, edge-tts (pip install edge-tts, запасной gTTS), файл bot.py рядом.
"""
import os, sys, io, re, json, time, wave, struct, shutil, subprocess, tempfile, datetime, asyncio, uuid, random
import urllib.request, urllib.parse, urllib.error

import bot

# ===================== НАСТРОЙКИ И ЛИМИТЫ =====================
W, H, FPS = 1080, 1920, 24      # 24 кадра в секунду: плавно и быстро собирается
VIDEO_STATE = "videos.json"
VIDEO_DAILY_LIMIT = 2            # не больше двух роликов в сутки: так канал не выглядит спамом
MAX_VIDEO_SEC = 55               # короткие ролики досматривают чаще
MIN_SLIDE_SEC = 2.4
PAD_SEC = 0.35                   # пауза после фразы
FADE_FRAMES = 6                  # плавный переход между слайдами
OUT_DIR = "out"
MUSIC_DIR = "music"              # сюда клади СВОИ треки или музыку без авторских прав
MUSIC_VOLUME = 0.12
TTS_VOICE = os.environ.get("TTS_VOICE") or "ru-RU-DmitryNeural"
TTS_RATE = os.environ.get("TTS_RATE") or "+8%"
VIDEO_CHAT_ID = os.environ.get("VIDEO_CHAT_ID", "")      # личный чат, куда бот пришлёт готовые ролики
CTA_HANDLE = os.environ.get("VIDEO_CTA_HANDLE") or (bot.CHAT if bot.CHAT.startswith("@") else "")
HASHTAGS = "#brawlstars #бравлстарс #бс #мета #гайд"
TT_BASE = "https://open.tiktokapis.com"


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
                     ("TikTok", "Тик Ток"), ("Telegram", "Телеграм"), ("YouTube", "Ютуб")):
        t = re.sub(re.escape(lat), cyr, t, flags=re.I)

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
def tts(text, out_path):
    """Озвучка: сначала бесплатный голос Microsoft Edge (edge-tts), затем запасной Google (gTTS).
    Возвращает путь к файлу или None (тогда ролик собирается без голоса, только с субтитрами)."""
    last = None
    try:
        import edge_tts

        async def run_edge():
            await edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE).save(out_path)

        for attempt in range(3):
            try:
                asyncio.run(run_edge())
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
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


# ===================== СЛАЙДЫ =====================
def have_graphics():
    return bot.Image is not None and bot.card_font(40) is not None


def make_bg(seed=0):
    """Общий фон ролика: градиент и размытые цветные пятна (одинаковый на всех слайдах, чтобы ролик был цельным)."""
    rnd = random.Random(seed)
    img = bot._gradient(W, H, (40, 16, 100), (10, 6, 36)).convert("RGBA")
    for color in ((130, 40, 255), (0, 110, 255), (255, 40, 150)):
        layer = bot.Image.new("RGBA", (W, H), (0, 0, 0, 0))
        x, y, r = rnd.randint(-200, W - 300), rnd.randint(-100, H - 500), rnd.randint(500, 800)
        bot.ImageDraw.Draw(layer).ellipse((x, y, x + r, y + r), fill=color + (110,))
        img = bot.Image.alpha_composite(img, layer.filter(bot.ImageFilter.GaussianBlur(120)))
    return img.convert("RGB")


def _new_slide(bg):
    img = bg.copy()
    d = bot.ImageDraw.Draw(img)
    d.text((54, 82), "NEW BRAWL NEWS", font=bot.card_font(44), fill=(255, 255, 255), anchor="lm",
           stroke_width=4, stroke_fill=(18, 8, 50))
    return img, d


def _glow(img, cx, cy, r, color):
    layer = bot.Image.new("RGBA", img.size, (0, 0, 0, 0))
    bot.ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (120,))
    img.paste(layer.filter(bot.ImageFilter.GaussianBlur(70)), (0, 0), layer.filter(bot.ImageFilter.GaussianBlur(70)))


def _portrait_or_initials(img, d, portrait, name, cx, cy, box):
    if portrait:
        try:
            pm = bot._portrait(portrait, box)
            img.paste(pm, (cx - pm.width // 2, cy - pm.height // 2), pm)
            return
        except Exception:
            pass
    d.rounded_rectangle((cx - box // 2, cy - box // 2, cx + box // 2, cy + box // 2), radius=70, fill=(120, 70, 230))
    d.text((cx, cy), name[:2].upper(), font=bot.card_font(240), fill=(255, 255, 255), anchor="mm")


def _chip(d, cx, y, text, fill, color=(255, 255, 255), size=52):
    f = bot._fit(d, text, 900, size, 28)
    w = d.textlength(text, font=f)
    d.rounded_rectangle((cx - w / 2 - 34, y, cx + w / 2 + 34, y + f.size + 36), radius=34, fill=fill)
    d.text((cx, y + (f.size + 36) / 2), text, font=f, fill=color, anchor="mm")


def slide_hero(bg, name, chip="", chip2="", portrait=None, rank=None):
    """Крупный боец: портрет, имя, до двух плашек. rank: номер места в рейтинге."""
    img, d = _new_slide(bg)
    _glow(img, W // 2, 560, 360, (255, 190, 40))
    _portrait_or_initials(img, d, portrait, name, W // 2, 560, 700)
    if rank:
        d.text((90, 270), f"#{rank}", font=bot.card_font(190), fill=(255, 204, 0), anchor="lm",
               stroke_width=10, stroke_fill=(30, 14, 70))
    title = name.upper()
    d.text((W // 2, 1000), title, font=bot._fit(d, title, 960, 130, 56), fill=(255, 255, 255), anchor="mm",
           stroke_width=8, stroke_fill=(30, 14, 70))
    if chip:
        _chip(d, W // 2 if not chip2 else W // 2 - 260, 1060, chip, (255, 140, 0))
    if chip2:
        _chip(d, W // 2 + 260, 1060, chip2, (30, 160, 90))
    return img


def slide_title(bg, title, subtitle=""):
    img, d = _new_slide(bg)
    f, lines = bot._fit_lines(d, title.upper(), 940, 700, start=130, minimum=60, max_lines=5)
    lh = int(f.size * 1.2)
    y0 = 560 - lh * len(lines) // 2
    for i, line in enumerate(lines):
        d.text((W // 2, y0 + i * lh + lh // 2), line, font=f, fill=(255, 204, 0), anchor="mm",
               stroke_width=8, stroke_fill=(30, 14, 70))
    if subtitle:
        _chip(d, W // 2, 900, subtitle, (130, 60, 230), size=50)
    return img


def slide_text(bg, heading, body, accent=(255, 140, 0)):
    img, d = _new_slide(bg)
    _chip(d, W // 2, 250, heading, accent, size=56)
    over = bot.Image.new("RGBA", img.size, (0, 0, 0, 0))
    bot.ImageDraw.Draw(over).rounded_rectangle((60, 400, W - 60, 1130), radius=48, fill=(12, 6, 44, 170))
    img = bot.Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    d = bot.ImageDraw.Draw(img)
    f, lines = bot._fit_lines(d, body, 860, 620, start=68, minimum=38, max_lines=9)
    lh = int(f.size * 1.25)
    y0 = 765 - lh * len(lines) // 2
    for i, line in enumerate(lines):
        d.text((W // 2, y0 + i * lh + lh // 2), line, font=f, fill=(255, 255, 255), anchor="mm")
    return img


def slide_cta(bg, handle):
    img, d = _new_slide(bg)
    d.text((W // 2, 470), "БОЛЬШЕ РАЗБОРОВ", font=bot._fit(d, "БОЛЬШЕ РАЗБОРОВ", 960, 120, 60), fill=(255, 255, 255),
           anchor="mm", stroke_width=8, stroke_fill=(30, 14, 70))
    d.text((W // 2, 610), "И НОВОСТЕЙ", font=bot._fit(d, "И НОВОСТЕЙ", 960, 120, 60), fill=(255, 255, 255),
           anchor="mm", stroke_width=8, stroke_fill=(30, 14, 70))
    _chip(d, W // 2, 760, "в нашем Telegram", (40, 150, 235), size=64)
    if handle:
        d.text((W // 2, 990), handle, font=bot._fit(d, handle, 960, 110, 50), fill=(255, 204, 0), anchor="mm",
               stroke_width=8, stroke_fill=(30, 14, 70))
    _chip(d, W // 2, 1070, "ссылка в профиле", (255, 255, 255), color=(30, 14, 70), size=44)
    return img


def caption_layer(text):
    """Субтитры: тёмная плашка с текстом в безопасной зоне (выше интерфейса TikTok)."""
    layer = bot.Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = bot.ImageDraw.Draw(layer)
    f, lines = bot._fit_lines(d, text, 880, 300, start=58, minimum=34, max_lines=4)
    lh = int(f.size * 1.3)
    top = 1215
    d.rounded_rectangle((50, top, W - 50, top + lh * len(lines) + 56), radius=40, fill=(0, 0, 0, 165))
    for i, line in enumerate(lines):
        d.text((W // 2, top + 28 + i * lh + lh // 2), line, font=f, fill=(255, 255, 255), anchor="mm",
               stroke_width=2, stroke_fill=(0, 0, 0))
    return layer


# ===================== СБОРКА РОЛИКА =====================
def _zoom(base, z, cx=0.5, cy=0.5):
    w, h = W / z, H / z
    x0, y0 = (W - w) * cx, (H - h) * cy
    return base.resize((W, H), bot.Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h))


def render_frames(slides, total):
    """Генератор кадров: медленный наезд, плавный переход, субтитры и полоска прогресса."""
    Image, ImageDraw = bot.Image, bot.ImageDraw
    prev_last, done = None, 0
    for si, sl in enumerate(slides):
        n = max(1, round(sl["dur"] * FPS))
        cap, box = None, None
        if sl.get("caption"):
            full = caption_layer(sl["caption"])
            box = full.getbbox()
            cap = full.crop(box)  # субтитры накладываем только на свою область: так в разы быстрее
        for k in range(n):
            t = k / max(1, n - 1)
            ease = t * t * (3 - 2 * t)
            z = 1.0 + 0.07 * (ease if si % 2 == 0 else 1 - ease)
            frame = _zoom(sl["base"], z)
            if cap is not None:
                a = min(1.0, k / (FPS * 0.25))
                layer = cap
                if a < 1.0:
                    layer = cap.copy()
                    layer.putalpha(cap.split()[3].point(lambda v, a=a: int(v * a)))
                frame.paste(layer, (box[0], box[1]), layer)
            if prev_last is not None and k < FADE_FRAMES:
                frame = Image.blend(prev_last, frame, (k + 1) / (FADE_FRAMES + 1))
            ImageDraw.Draw(frame).rectangle((0, 0, int(W * (done + k + 1) / max(1, total)), 12), fill=(255, 204, 0))
            if k == n - 1:
                prev_last = frame.copy()
            yield frame.tobytes()
        done += n


def encode_video(frames, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
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


def build_voice_track(slides, workdir):
    """Склеивает озвучку слайдов в один звук; каждая фраза дополняется паузой до длины своего слайда."""
    parts = []
    for i, sl in enumerate(slides):
        wav = os.path.join(workdir, f"seg{i}.wav")
        if sl.get("voice"):
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", sl["voice"], "-af", "apad", "-t", f"{sl['dur']:.3f}",
                 "-ar", "44100", "-ac", "2", wav])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", f"{sl['dur']:.3f}", wav])
        parts.append(wav)
    lst = os.path.join(workdir, "voice.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{p}'\n" for p in parts))
    out = os.path.join(workdir, "voice.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return out


def mux(video, voice, music, total, out_path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", voice]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music, "-filter_complex",
                f"[2:a]volume={MUSIC_VOLUME},afade=t=in:d=1,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
                "[1:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]", "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]
    run(cmd)


def render_video(specs, out_path, vstate=None, seed=0):
    """specs: [{'image': PIL, 'speech': текст для голоса, 'caption': субтитры, 'priority': 0..9}].
    Озвучивает, подгоняет длительности, при необходимости выкидывает второстепенные слайды, собирает ролик."""
    if not have_graphics():
        raise RuntimeError("нет Pillow или шрифта с кириллицей: " + bot.cards_status())
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("не найден ffmpeg")
    vstate = vstate if vstate is not None else {}
    workdir = tempfile.mkdtemp(prefix="vid_")
    try:
        slides, voiced = [], 0
        for i, sp in enumerate(specs):
            voice = tts(spoken(sp["speech"]), os.path.join(workdir, f"v{i}.mp3")) if sp.get("speech") else None
            if voice:
                voiced += 1
                dur = max(MIN_SLIDE_SEC, audio_duration(voice) + PAD_SEC)
            else:
                dur = max(MIN_SLIDE_SEC, 1.0 + len(sp.get("caption", "")) / 14)  # без голоса читаем глазами
            slides.append({"base": sp["image"], "caption": sp.get("caption", ""), "voice": voice, "dur": dur,
                           "priority": sp.get("priority", 5)})
        while sum(s["dur"] for s in slides) > MAX_VIDEO_SEC and len(slides) > 2:
            drop = min((s for s in slides[1:-1]), key=lambda s: (s["priority"], -s["dur"]), default=None)
            if drop is None:
                break
            slides.remove(drop)  # выкидываем самый неважный слайд
        if sum(s["dur"] for s in slides) > MAX_VIDEO_SEC:
            print(f"ролик длиннее {MAX_VIDEO_SEC} с даже после сокращения, оставляю как есть")
        total_sec = sum(s["dur"] for s in slides)
        total_frames = sum(max(1, round(s["dur"] * FPS)) for s in slides)
        silent = os.path.join(workdir, "silent.mp4")
        encode_video(render_frames(slides, total_frames), silent)
        voice_track = build_voice_track(slides, workdir)
        music = pick_music(vstate)
        mux(silent, voice_track, music, total_sec, out_path)
        return {"path": out_path, "duration": total_sec, "slides": len(slides), "voiced": voiced > 0,
                "music": os.path.basename(music) if music else ""}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ===================== СЦЕНАРИИ РОЛИКОВ =====================
CTA_SPEECH = "Больше разборов, гайдов и новостей по Бравл Старс в нашем телеграм-канале. Ссылка в описании профиля."


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


ORD = {1: "Первое", 2: "Второе", 3: "Третье"}


def script_meta(meta):
    """Ролик «Мета сегодня» по данным, которые бот сохранил после публикации меты."""
    rows = bot.rank_rows(meta["stats"], bot.META_MIN_PICKS)
    if len(rows) < 3:
        return None
    top3 = rows[:3]
    total = meta["battles"] * 6
    pop = max(rows, key=lambda r: r[1])
    por = _portraits([r[0] for r in top3] + [pop[0]])
    bg = make_bg(seed=int(meta["date"].replace("-", "") or 0))
    specs = [{"image": slide_title(bg, "Мета Brawl Stars", "сегодня"),
              "speech": "Мета Бравл Старс на сегодня. Кто сильнее всех по боям лучших игроков мира?",
              "caption": "Мета Brawl Stars на сегодня", "priority": 9}]
    for rank in (3, 2, 1):
        n, pk, w, wr, lb = top3[rank - 1]
        specs.append({"image": slide_hero(bg, n, chip=f"{bot.pct(wr)} побед", chip2=f"пикрейт {bot.pct(pk / total)}",
                                          portrait=por.get(n.upper()), rank=rank),
                      "speech": f"{ORD[rank]} место: {n}. {bot.pct(wr)} побед.",
                      "caption": f"{rank} место: {n} · {bot.pct(wr)} побед", "priority": 8 if rank == 1 else 6})
    if pop[0] not in [r[0] for r in top3]:
        specs.append({"image": slide_hero(bg, pop[0], chip="самый популярный", chip2=f"{bot.pct(pop[3])} побед",
                                          portrait=por.get(pop[0].upper())),
                      "speech": f"Самый популярный боец: {pop[0]}. Его берут в {bot.pct(pop[1] / total)} боёв.",
                      "caption": f"Самый популярный: {pop[0]}", "priority": 3})
    specs.append({"image": slide_cta(bg, CTA_HANDLE), "speech": CTA_SPEECH, "caption": "Подробный разбор в Telegram",
                  "priority": 10})
    names = ", ".join(r[0] for r in top3)
    return {"kind": "meta", "specs": specs,
            "caption": f"🔥 Мета Brawl Stars сегодня: {names}. Кого берёте вы?"}


def script_guide(b, state):
    """Ролик «Гайд на бойца» по тем же данным, что и гайд в канале."""
    info = bot.guide_info(b, state)
    por = _portraits([info["name"]]).get(info["name"].upper())
    bg = make_bg(seed=sum(map(ord, info["name"])))
    specs = [{"image": slide_hero(bg, info["name"], chip=info["cls_ru"] or "гайд", chip2=info["short_stat"], portrait=por),
              "speech": f"Гайд на бойца {info['name']}." + (f" Это {info['cls_ru']}." if info["cls_ru"] else ""),
              "caption": f"Гайд: {info['name']}", "priority": 10}]
    if info["desc"]:
        specs.append({"image": slide_text(bg, "Кто это", info["desc"]), "speech": info["desc"],
                      "caption": "", "priority": 4})
    for label, rows, accent, prio in (("Гаджет", info["gadgets"], (255, 140, 0), 7),
                                      ("Звёздная сила", info["stars"], (245, 95, 165), 7)):
        for n, d in [r for r in rows if r[1]][:2 if label == "Гаджет" else 1]:
            specs.append({"image": slide_text(bg, label, f"{n}: {d}", accent), "speech": f"{label}. {d}",
                          "caption": "", "priority": prio})
    if info["stat_line"]:
        specs.append({"image": slide_text(bg, "Данные топ-игроков", info["stat_line"], (30, 160, 90)),
                      "speech": info["stat_line"], "caption": "", "priority": 6})
    if info["tip"]:
        specs.append({"image": slide_text(bg, "Как играть", info["tip"], (80, 150, 255)), "speech": info["tip"],
                      "caption": "", "priority": 8})
    specs.append({"image": slide_text(bg, "Важно", "Это универсальный разбор. Гаджет и звёздную силу выбирай "
                                      "под карту, режим и команду.", (200, 60, 60)),
                  "speech": "Это универсальный разбор. Гаджет и звёздную силу выбирай под карту, режим и команду.",
                  "caption": "", "priority": 9})
    specs.append({"image": slide_cta(bg, CTA_HANDLE), "speech": CTA_SPEECH, "caption": "Больше гайдов в Telegram",
                  "priority": 10})
    if len(specs) < 4:
        return None
    return {"kind": "guide", "specs": specs,
            "caption": f"📘 Гайд на {info['name']} в Brawl Stars: что умеет и как играть. Играете за него?"}


def script_demo():
    bg = make_bg(seed=1)
    return {"kind": "demo", "caption": "Тестовый ролик", "specs": [
        {"image": slide_title(bg, "Тест ролика", "проверка"), "speech": "Привет! Это тест автоматической сборки роликов.",
         "caption": "Тест автоматической сборки", "priority": 9},
        {"image": slide_text(bg, "Всё работает", "Голос, субтитры, музыка и переходы собраны автоматически."),
         "speech": "Голос, субтитры, музыка и переходы собраны автоматически.", "caption": "", "priority": 5},
        {"image": slide_cta(bg, CTA_HANDLE or "@your_channel"), "speech": CTA_SPEECH, "caption": "Подписывайся в Telegram",
         "priority": 10}]}


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
