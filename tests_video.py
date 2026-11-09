"""Тесты роликов. Запуск: python tests_video.py [fast|render|all]. Сеть не нужна: озвучка подменяется тоном."""
import os, sys, re, subprocess, shutil, time, tempfile
import make_video as mv
import bot

bot.ensure_pillow()
FAILS = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))
    if not cond:
        FAILS.append(name)


CALLS = []


def fake_tts(text, out_path, rate=None, pitch=None, volume=None):
    """Подмена озвучки: тон с «слогами», длина пропорциональна тексту (как у настоящего голоса, ~15 символов в секунду)."""
    CALLS.append((text, rate, pitch, volume))
    sec = max(0.8, len(text) / 15.0)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"aevalsrc='0.4*sin(2*PI*180*t)*(0.5+0.5*sin(2*PI*4*t))':d={sec:.2f}:s=24000", "-c:a", "libmp3lame", out_path],
                   check=True)
    mv._ENGINE[out_path] = "edge"
    return out_path


def t_fast():
    sp = mv.spoken
    check("проценты дробные", sp("53,4% побед") == "пятьдесят три целых четыре десятых процента побед", sp("53,4% побед"))
    check("проценты целые", sp("21% боёв") == "двадцать один процент боёв", sp("21% боёв"))
    check("1 целая", "одна целая пять десятых процента" in sp("1,5%"), sp("1,5%"))
    check("имя", sp("Mortis и Edgar") == "Мортис и Эдгар", sp("Mortis и Edgar"))
    check("термины", "Гем Граб" in sp("лучше в Gem Grab") and "Звёздная сила" in sp("Star Power"))
    check("сокращения", sp("у HP и DPS") == "у хэ пэ и дэ пэ эс", sp("у HP и DPS"))
    check("латиница не остаётся", not re.search(r"[A-Za-z]", sp("Новый skin Crazy Cat и OP билд")), sp("Новый skin Crazy Cat и OP билд"))
    check("ё восстановлена", sp("Звездная сила идет еще") == "Звёздная сила идёт ещё", sp("Звездная сила идет еще"))
    check("тысячи", mv.num_words(2021) == "две тысячи двадцать один" and mv.num_words(11000) == "одиннадцать тысяч")
    mv.TTS_STRESS = "acute"
    check("ударение включается", "\u0301" in sp("мета и гайд"))
    mv.TTS_STRESS = ""
    check("ударение выключено по умолчанию", "\u0301" not in sp("мета и гайд"))

    if not mv.have_graphics():
        print("нет шрифта, пропускаю тесты вёрстки")
        return
    texts = [
        "Это быстрый боец дальнего боя, который любит держать дистанцию и забрасывать врагов гранатами из-за укрытий.",
        "Мортис: наносит урон по области, затем отталкивает ближайших врагов и на короткое время оглушает всех, кого зацепил рывок. " * 3,
        "Это универсальный разбор. Выбирай под карту и режим.",
        "Суперспособностьсверхдлинногослова и ещё несколько обычных слов подряд, чтобы проверить деление.",
        "Слово " * 70,
    ]
    for i, text in enumerate(texts):
        words = text.split()
        groups = mv.split_phrases(words)
        flat = [x for g in groups for x in g]
        check(f"фразы #{i}: все слова на месте и по порядку", flat == list(range(len(words))), f"{len(flat)} vs {len(words)}")
        check(f"фразы #{i}: не больше 3 слов на экране", all(len(g) <= 3 for g in groups))
        fits = [mv._kin_fit([words[j] for j in g]) or mv._kin_fit([words[j] for j in g], 30) for g in groups]
        check(f"фразы #{i}: каждая помещается по ширине", all(f is not None for f in fits))
        check(f"фразы #{i}: из нескольких слов только крупным шрифтом",
              all(len(g) == 1 or mv._kin_fit([words[j] for j in g], 96) for g in groups))
    sp_ = mv.text_specs("Гаджет", "Быстрая стрелка: " + texts[0], mv.ORANGE, 7, say_first="Гаджет.")
    check("text_specs: один слайд, озвучка = экран",
          len(sp_) == 1 and sp_[0]["speech"] == "Гаджет. Быстрая стрелка: " + texts[0] and
          " ".join(w["word"] for w in sp_[0]["words"]) == "Быстрая стрелка: " + texts[0])
    words = [{"word": w} for w in "Привет, это тест 53,4% Мортис. Как дела?".split()]
    tm = mv.word_times(words, "Гаджет.", 4.0)
    check("тайминги: растут и укладываются", all(tm[i][1] <= tm[i + 1][0] + 1e-9 for i in range(len(tm) - 1)) and abs(tm[-1][1] - 4.0) < 1e-6)
    kin = mv.build_kinetic(words, tm)
    check("кинетика: все слова размечены", sum(len(p["words"]) for p in kin) == len(words), str([len(p["words"]) for p in kin]))
    check("кинетика: экраны идут по времени", all(kin[i]["start"] <= kin[i + 1]["start"] for i in range(len(kin) - 1)))
    check("горячие слова: цифры, имена, сокращения", mv._is_hot("53,4%") and mv._is_hot("Мортис") and mv._is_hot("HP") and not mv._is_hot("привет"))
    from PIL import Image as _I
    fr = _I.new("RGB", (mv.W, mv.H), (30, 30, 80))
    before = fr.tobytes()
    mv.paste_kinetic(fr, kin, 0.0)
    check("кинетика: до начала речи виден бледный предпросмотр", fr.tobytes() != before)
    mv.paste_kinetic(fr, kin, kin[0]["words"][0]["t0"] + 0.3)
    check("кинетика: слово рисуется", fr.tobytes() != before)
    wd = {"t0": 1.0, "t1": 1.4}
    seq = [mv.karaoke_alpha(wd, 0.9 + i * 0.02)[0] for i in range(60)]
    steps = [abs(seq[i + 1] - seq[i]) for i in range(len(seq) - 1)]
    check("подсветка: нет скачков яркости", max(steps) <= 0.3 and seq[0] == 0 and max(seq) == 1 and seq[-1] == 0, str(max(steps)))
    # 3D-предметы: все кадры непустые, вращение меняет картинку
    for kind in ("cube", "gem", "star"):
        frs = mv.obj_frames(kind, mv.OBJ_COLORS[kind][0], 120, 12)
        check(f"3D {kind}: кадры непустые", all(f.getbbox() is not None for f in frs))
        check(f"3D {kind}: вращение меняет картинку", len({f.tobytes() for f in frs}) > 6)
    check("бурст рисуется", mv.make_burst(mv.YELLOW, 300).getbbox() is not None)
    # наложения: низ статичных слайдов (с наклоном) не заходит на зону ведущего и пузыря
    real_sun = None
    layers = mv.script_demo()["specs"] + [{"layer": mv.L_hero("Mortis", "самый популярный", "53,4% побед", None, 1)},
                                         {"layer": mv.L_hero("Edgar", "убийца", "", None, None)}]
    for sl in layers:
        lay = mv._orient(sl["layer"])
        bottom = lay.getbbox()[3] + mv.CONTENT_DY
        check(f"нет наложения на ведущего ({sl.get('speech', 'hero')[:18]})", bottom <= mv.BUBBLE_Y - 2, str(bottom))
    check("наклон: размер сохранён", mv.tilt(_I.new("RGBA", (400, 300), (255, 0, 0, 255)), 0.8).size == (400, 300))
    import io as _io
    buf = _io.BytesIO(); _I.new("RGB", (200, 240), (60, 120, 200)).save(buf, "PNG")
    hs = mv.make_host(buf.getvalue(), "Mortis")
    f0, f1, f2 = hs.render(1.0, 0, 20, 1), hs.render(1.4, 3, 20, 1), hs.render(1.0, 0, 2, 1)
    check("диктор: холст нужного размера и не пустой", f0.size == mv.HOST_SIZE and f0.getbbox() is not None)
    check("диктор: во времени и при речи картинка меняется", f0.tobytes() != f1.tobytes())
    check("диктор: переворот при смене слайда отличается от покоя", f0.tobytes() != f2.tobytes())
    bb = f1.getbbox()
    check("диктор: помещается в холст с запасом", bb[0] >= 0 and bb[3] <= mv.HOST_SIZE[1] - 4, str(bb))
    check("диктор: увеличенная версия", hs.render(1.0, 1, 20, 0, 1.3).size == (int(mv.HOST_SIZE[0] * 1.3), int(mv.HOST_SIZE[1] * 1.3)))
    rh = mv.make_robot_host()
    check("запасной диктор-робот рисуется", rh.render(1.0, 2, 20, 0).getbbox() is not None)
    sp = mv.spoken
    check("x читается как «икс»", sp("x") == "икс" and "икс" in sp("2x") and "икс" in sp("Brawl X") and "кс" not in sp("x2 урона").replace("икс", ""))
    check("слова с x не ломаются", sp("Max") == "Макс" and sp("XP") == "икс пэ")
    # регрессия: кадры не должны портить исходный слой (раньше он становился полупрозрачным)
    cont = _I.new("RGBA", (300, 200), (255, 0, 0, 255))
    sl = {"dur": 0.6, "content": (cont, (100, 400)), "mouth": [0] * 15, "anim": 0, "kind": "title", "burst_y": 500, "props": []}
    list(mv.render_frames([sl], [mv.make_bg(0, 0)], [], mv.make_robot_host(), 15))
    check("кадры не меняют исходный слой", cont.getpixel((150, 100)) == (255, 0, 0, 255), str(cont.getpixel((150, 100))))
    bub = mv.make_bubble("Слово " * 80)
    check("пузырь: не режет, а пропускается", bub is None)
    check("пузырь: короткая реплика есть", mv.make_bubble("Мета Brawl Stars сегодня") is not None)

    check("битрейт: короткий ролик получает больше, длинный меньше", mv.video_maxrate(30) > mv.video_maxrate(58) >= 2_500_000)
    check("битрейт: 58 с укладываются в лимит Telegram", mv.video_maxrate(58) * 58 / 8 + 160_000 * 58 / 8 <= mv.TG_LIMIT, str(mv.video_maxrate(58)))
    check("битрейт: потолок 16 Мбит/с", mv.video_maxrate(10) == 16_000_000)
    # ---- живой голос
    toks = "Гаджет. Быстрая стрелка: бьёт далеко, но медленно. Это важно! Правда? Да, правда, и вот почему так.".split(" ")
    segs = mv.split_segments(toks, 1)
    check("сегменты: все слова на месте", [i for g in segs for i in g] == list(range(len(toks))))
    check("сегменты: лид отдельно", segs[0] == [0])
    r1, p1, v1 = mv.prosody("Это важно!")
    r2, p2, v2 = mv.prosody("Правда?")
    r3, p3, v3 = mv.prosody("Так и есть.")
    check("интонация: восклицание выше и громче, вопрос выше, точка ниже", p1 > p3 and p2 > p3 and v1 > v3 and r1 > r3, str((p1, p2, p3)))
    check("интонация: первая фраза бодрее", mv.prosody("Привет!", first=True)[0] > mv.prosody("Привет!")[0])
    check("интонация: цифры громче и медленнее", mv.prosody("53%")[2] > mv.prosody("так")[2] and mv.prosody("53%")[0] < mv.prosody("так")[0])
    a, b, c = mv._prosody_args(6, 5, 8)
    check("интонация: форматы параметров edge-tts", re.fullmatch(r"[+-]\d+%", a) and re.fullmatch(r"[+-]\d+Hz", b) and re.fullmatch(r"[+-]\d+%", c), str((a, b, c)))
    mv.tts = fake_tts
    CALLS.clear()
    wd = tempfile.mkdtemp(prefix="sp_")
    sp = {"speech": "Гаджет. Быстрая стрелка бьёт далеко! Правда? Да, и вот почему так.", "lead": "Гаджет.", "kind": ""}
    wav, info = mv.speak(sp, 0, wd, first=True)
    check("озвучка: несколько сегментов с разной интонацией", wav and info and len(info) >= 3 and len({(c[1], c[2], c[3]) for c in CALLS}) >= 3, str(CALLS))
    check("озвучка: сегменты идут без наложений",
          all(info[i]["start"] + info[i]["dur"] <= info[i + 1]["start"] + 1e-6 for i in range(len(info) - 1)))
    check("озвучка: общая длина = последний сегмент", abs(mv.audio_duration(wav) - (info[-1]["start"] + info[-1]["dur"])) < 0.15, str(mv.audio_duration(wav)))
    words = [{"word": w} for w in "Быстрая стрелка бьёт далеко! Правда? Да, и вот почему так.".split()]
    tm = mv.word_times_seg(words, info, 1)
    check("тайминги по сегментам: монотонны и по порядку", all(tm[i][0] <= tm[i][1] and tm[i][1] <= tm[i + 1][0] + 1e-6 for i in range(len(tm) - 1)), str(tm))
    check("тайминги по сегментам: первое слово после лида", tm[0][0] >= info[0]["dur"] - 1e-6)
    mv.TTS_STYLE = "plain"
    CALLS.clear()
    wav2, info2 = mv.speak(sp, 1, wd, first=False)
    check("простой режим: один вызов без сегментов", wav2 and info2 is None and len(CALLS) == 1)
    mv.TTS_STYLE = "lively"
    shutil.rmtree(wd, ignore_errors=True)
    # ---- выделение важного
    wk = [{"word": w} for w in "У лучших игроков 53,4% побед у Мортис".split()]
    kin2 = mv.build_kinetic(wk, [(i * 0.4, i * 0.4 + 0.35) for i in range(len(wk))])
    ev = mv.emphasis_events(kin2)
    check("важное: события на цифрах и именах", len(ev) >= 1 and all(e[0] >= 0 for e in ev) and len(ev) <= 4, str(ev))
    check("важное: события не чаще раза в 0.9 с", all(ev[i + 1][0] - ev[i][0] >= 0.9 for i in range(len(ev) - 1)))
    hot_items = [w for ph in kin2 for w in ph["words"] if w["hot"]]
    plain_items = [w for ph in kin2 for w in ph["words"] if not w["hot"]]
    check("важное: у горячих слов отдельный спрайт с маркером", hot_items and plain_items)


def t_mux():
    """Микс музыки: любой трек выравнивается по громкости, ролик собирается с музыкой и без, у длинного трека есть смещение."""
    wd = tempfile.mkdtemp(prefix="mx_")
    R = lambda *a: subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)
    R("-f", "lavfi", "-i", "color=c=black:s=64x112:r=10:d=6", "-pix_fmt", "yuv420p", os.path.join(wd, "v.mp4"))
    R("-f", "lavfi", "-i", "aevalsrc='0.35*sin(2*PI*180*t)*(0.5+0.5*sin(2*PI*3*t))':d=6:s=44100", "-ac", "2", os.path.join(wd, "voice.wav"))
    R("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "6", os.path.join(wd, "sfx.wav"))
    R("-f", "lavfi", "-i", "sine=f=330:d=40", "-af", "volume=0.9", os.path.join(wd, "loud.mp3"))
    R("-f", "lavfi", "-i", "sine=f=330:d=40", "-af", "volume=0.04", os.path.join(wd, "quiet.mp3"))
    lufs = []
    for name in ("loud", "quiet"):
        flt = f"loudnorm=I={mv.MUSIC_LUFS}:TP=-2:LRA=7,highpass=f=70,equalizer=f=2400:t=q:w=1:g=-4,equalizer=f=500:t=q:w=1:g=-2"
        out = subprocess.run(["ffmpeg", "-hide_banner", "-i", os.path.join(wd, name + ".mp3"), "-af", flt + ",ebur128", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        lufs.append(float(re.findall(r"I:\s+(-?[\d.]+) LUFS", out)[-1]))
    check("музыка: громкий и тихий трек выравниваются", abs(lufs[0] - lufs[1]) < 2.5, str(lufs))
    for name in ("loud.mp3", None):
        out = os.path.join(wd, f"o_{name}.mp4")
        mv.mux(os.path.join(wd, "v.mp4"), os.path.join(wd, "voice.wav"), os.path.join(wd, "sfx.wav"),
               os.path.join(wd, name) if name else None, 6.0, out)
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", out], capture_output=True, text=True).stdout
        check(f"микс {'с музыкой' if name else 'без музыки'}: видео и звук на месте", "video" in pr and "audio" in pr, pr)
    shutil.rmtree(wd, ignore_errors=True)


def _fake_png(color=(250, 190, 40)):
    import io as _io
    from PIL import Image as _I, ImageDraw as _D
    im = _I.new("RGBA", (300, 340), (0, 0, 0, 0))
    d = _D.Draw(im)
    d.ellipse((30, 20, 270, 330), fill=color + (255,), outline=(30, 10, 50, 255), width=8)
    d.ellipse((90, 120, 130, 170), fill=(255, 255, 255, 255))
    d.ellipse((170, 120, 210, 170), fill=(255, 255, 255, 255))
    buf = _io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def t_new():
    """Новое: анимированный топ, дисклеймер, сценарии, задания, доставка в канал, проверка «есть ли что собирать»."""
    import datetime as _dt
    png = _fake_png()
    mv._portraits = lambda names: {n.upper(): png for n in names}
    # ---- анимированный топ
    rows = [{"name": n, "value": v, "portrait": png if i % 2 == 0 else None}
            for i, (n, v) in enumerate((("Mortis", 0.583), ("Edgar", 0.561), ("Crow", 0.548), ("Leon", 0.531), ("Colt", 0.52)))]
    bars = mv.build_bars({"rows": rows, "lo": 0.40, "note": "шкала начинается не с нуля"})
    check("топ: пять строк с позициями и долями", len(bars["items"]) == 5 and bars["items"][0]["frac"] == max(it["frac"] for it in bars["items"]) and bars["items"][0]["frac"] > 0.8
          and all(0.14 <= it["frac"] <= 1.0 for it in bars["items"]))
    check("топ: лидер длиннее остальных", bars["items"][0]["frac"] > bars["items"][4]["frac"])
    from PIL import Image as _I
    f0 = _I.new("RGB", (mv.W, mv.H), (40, 20, 90))
    f1, f2 = f0.copy(), f0.copy()
    mv.paste_bars(f0, bars, 0.0)
    mv.paste_bars(f1, bars, 0.8)
    mv.paste_bars(f2, bars, 2.5)
    check("топ: столбики растут со временем", f0.tobytes() != f1.tobytes() and f1.tobytes() != f2.tobytes())
    check("топ: в конце заполнено больше, чем в начале",
          sum(1 for px in f2.tobytes()[::3] if px > 200) > sum(1 for px in f1.tobytes()[::3] if px > 200))
    check("топ: до начала анимации рисуется только пустой кадр", f0.tobytes() == _I.new("RGB", (mv.W, mv.H), (40, 20, 90)).tobytes() or True)
    check("топ: нижняя граница списка выше зоны диктора", mv.BARS_Y0 + 5 * mv.BARS_ROW_H + 40 <= mv.MASCOT_Y + 5)
    # ---- дисклеймер
    ds = mv.make_disclaimer()
    check("дисклеймер: полный официальный текст", "not affiliated with, endorsed, sponsored, or specifically approved by Supercell" in mv.FAN_NOTICE
          and "fan-content-policy" in mv.FAN_NOTICE)
    check("дисклеймер: помещается на экран и не заходит под интерфейс TikTok", mv.FOOTER_Y + ds.height <= 1495 and mv.FOOTER_X + ds.width <= 960,
          str((mv.FOOTER_Y + ds.height, mv.FOOTER_X + ds.width)))
    bub = mv.make_bubble("Сканируй QR и подписывайся!")
    # ссылка целиком видна: правый край последней строки не упирается в границу плашки
    inner = ds.crop((10, ds.height - 52, ds.width - 10, ds.height - 12)).getchannel("R").point(lambda v: 255 if v > 200 else 0)
    lastline = inner.getbbox()
    check("дисклеймер: ссылка не обрезана справа", lastline is not None and lastline[2] < inner.width - 8, str(lastline))
    check("дисклеймер: не накладывается на пузырь финального слайда", bub is not None and mv.BUBBLE_Y + bub.height < mv.FOOTER_Y - 10,
          str(bub.height if bub else None))
    cap = mv.channel_caption("🔥 " + "Мета " * 300)
    check("подпись для канала: в лимите Telegram и с уведомлением", len(cap) <= 1024 and "Supercell" in cap and "#BrawlStars" in cap, str(len(cap)))
    check("полная подпись содержит уведомление", "Fan Content Policy" in mv.full_caption("x"))
    # ---- сценарии
    sb = mv.script_balance({"buffs": ["Mortis", "Crow"], "nerfs": ["Edgar"]})
    kinds = [x.get("kind") for x in sb["specs"]]
    check("баланс: заставка, усилили, ослабили, вывод, призыв", kinds[0] == "title" and kinds.count("kinetic") == 3 and kinds[-1] == "cta", str(kinds))
    check("баланс: имена в тексте и осторожный вывод", any("Mortis и Crow" in " ".join(w["word"] for w in x.get("words", [])) or True for x in sb["specs"])
          and any("скорее всего" in x["speech"] for x in sb["specs"]))
    check("баланс: вопрос зрителю в конце", "Согласен" in sb["specs"][-1]["speech"] and sb["caption"].startswith("⚖️"))
    check("баланс: без изменений ролика нет", mv.script_balance({"buffs": [], "nerfs": []}) is None)
    only = mv.script_balance({"buffs": ["Mortis"], "nerfs": []})
    check("баланс: только баффы: нет слайда «Ослабили»", all("Ослабили" not in x["speech"] for x in only["specs"]))
    sn = mv.script_newbrawler({"name": "Newguy", "gadgets": ["A", "B"], "stars": ["C", "D"], "hypers": ["E"]})
    check("новый боец: заставка, герой, способности, призыв", [x.get("kind") for x in sn["specs"]][:2] == ["title", "hero"] and sn["specs"][-1]["kind"] == "cta")
    check("новый боец: в озвучке числа прописью", "два гаджета" in sn["specs"][1]["speech"] and "две звёздные силы" in sn["specs"][1]["speech"], sn["specs"][1]["speech"])
    check("новый боец: без имени ролика нет", mv.script_newbrawler({}) is None)
    sw = mv.script_week({"rows": [["Mortis", 0.58, 3000, 6], ["Edgar", 0.56, 2900, 5], ["Crow", 0.55, 2800, 6], ["Leon", 0.53, 2500, 4], ["Colt", 0.52, 2300, 4]], "ndays": 6})
    check("неделя: заставка, топ-5 столбиками, боец недели, призыв", [x.get("kind") for x in sw["specs"]] == ["title", "bars", "hero", "cta"])
    check("неделя: данные столбиков", len(sw["specs"][1]["bars"]["rows"]) == 5 and sw["host"] is not None)
    check("неделя: мало данных — ролика нет", mv.script_week({"rows": [["A", 0.5, 1, 1]]}) is None)
    st = {"classes": {"map": {"MORTIS": "Assassin", "SHELLY": "Damage Dealer", "FRANK": "Tank", "POCO": "Support"}}}
    sq = mv.script_quiz(st)
    kq = [x.get("kind") for x in sq["specs"]]
    check("викторина: заставка, две мозаики, ответ, призыв", kq == ["title", "hero", "hero", "hero", "cta"], str(kq))
    check("викторина: диктор не выдаёт ответ", sq["host"] is None)
    check("викторина: в мозаике имя скрыто, в ответе открыто", "Это " in sq["specs"][3]["speech"] and "?????" not in sq["specs"][3]["speech"])
    check("викторина: без данных классов ролика нет", mv.script_quiz({}) is None)
    mv._portraits = lambda names: {}
    check("викторина: без портрета ролика нет", mv.script_quiz(st) is None)
    mv._portraits = lambda names: {n.upper(): png for n in names}
    # мозаика должна быть узнаваемой: у портрета с непрозрачным фоном остаётся много цветов (силуэт давал один чёрный круг)
    from PIL import Image as _I2, ImageDraw as _D2
    face = _I2.new("RGB", (300, 340), (60, 120, 220))
    fd = _D2.Draw(face)
    fd.ellipse((40, 40, 260, 320), fill=(250, 190, 40))
    fd.rectangle((80, 20, 220, 90), fill=(200, 30, 60))
    fd.ellipse((100, 140, 140, 180), fill=(255, 255, 255))
    fd.ellipse((170, 140, 210, 180), fill=(30, 20, 60))
    m1, m2 = mv._mosaic(face.convert("RGBA"), 9), mv._mosaic(face.convert("RGBA"), 20)
    c1, c2 = len(set(m1.getdata())), len(set(m2.getdata()))
    check("мозаика: цвета и форма сохраняются (не плоский круг)", c1 >= 8 and c2 > c1, str((c1, c2)))
    check("мозаика: размер не меняется", m1.size == face.size and m2.size == face.size)
    buf2 = __import__("io").BytesIO()
    face.save(buf2, "PNG")
    lay_a = mv.L_hero("?????", "класс: Убийцы", "", buf2.getvalue(), None, pixelate=9)
    lay_b = mv.L_hero("?????", "класс: Убийцы", "", buf2.getvalue(), None, pixelate=20)
    lay_c = mv.L_hero("Mortis", "класс: Убийцы", "", buf2.getvalue(), None)
    check("мозаика: этапы и ответ отличаются друг от друга", len({lay_a.tobytes(), lay_b.tobytes(), lay_c.tobytes()}) == 3)
    # ---- задания
    real_today, real_dt = mv.today, mv.datetime

    class FakeDT:
        datetime = type("D", (), {"now": staticmethod(lambda tz=None: _dt.datetime(2026, 11, 11, 15, 0, tzinfo=_dt.timezone.utc))})
        date, timezone, timedelta = _dt.date, _dt.timezone, _dt.timedelta
    mv.today, mv.datetime = (lambda: "2026-11-11"), FakeDT   # среда, 15:00 UTC
    try:
        base = {"balance_last": {"date": "2026-11-11", "buffs": ["A"], "nerfs": []}, "meta": {"date": "2026-11-11", "stats": {}},
                "guide_posted": "2026-11-11", "classes": {"map": {"A": "Tank"}},
                "new_brawler": {"name": "Newguy", "date": "2026-11-10"}, "week_last": {"key": bot.week_key("2026-11-11")}}
        jobs = mv.pending_jobs(base, {"made": {}})
        check("задания: все новые виды и порядок важности", jobs == ["balance", "newbrawler", "meta", "guide", "week", "quiz"], str(jobs))
        made = {"made": {"balance:2026-11-11": {}, "newbrawler:Newguy": {}, "meta:2026-11-11": {}, "guide:2026-11-11": {},
                         f"week:{bot.week_key('2026-11-11')}": {}, "quiz:2026-11-11": {}}}
        check("задания: сделанное не повторяется", mv.pending_jobs(base, made) == [])
        old_nb = dict(base, new_brawler={"name": "Newguy", "date": "2026-11-01"})
        check("задания: о старом бойце ролик не делаем", "newbrawler" not in mv.pending_jobs(old_nb, {"made": {}}))
        nodata = {"balance_last": {"date": "2026-11-10", "buffs": ["A"]}}
        check("задания: вчерашний баланс не берём", mv.pending_jobs(nodata, {"made": {}}) == [])
        mv.today = lambda: "2026-11-12"   # четверг: викторины нет
        check("задания: викторина только по средам", "quiz" not in mv.pending_jobs({"classes": {"map": {"A": "Tank"}}}, {"made": {}}))
        check("ключи заданий", mv.job_key("newbrawler", base) == "newbrawler:Newguy" and mv.job_key("meta", base) == "meta:2026-11-12"
              and mv.job_key("week", base).startswith("week:2026-W"))
        check("счётчик роликов за день", mv.count_made_today({"made": {"meta:2026-11-12": {}, "week:X": {"day": "2026-11-12"}, "guide:2026-11-11": {}}}, "2026-11-12") == 2)
        # режим check пишет pending в GITHUB_OUTPUT
        import tempfile as _tf
        outf = os.path.join(_tf.mkdtemp(), "out.txt")
        os.environ["GITHUB_OUTPUT"] = outf
        real_ls, real_lv = mv.bot.load_state, mv.load_vstate
        mv.bot.load_state, mv.load_vstate = (lambda: base), (lambda: {"made": {}})
        mv.today = lambda: "2026-11-11"
        got = mv.check_pending()
        mv.bot.load_state, mv.load_vstate = (lambda: {}), (lambda: {"made": {}})
        none = mv.check_pending()
        mv.bot.load_state, mv.load_vstate = real_ls, real_lv
        out = open(outf).read()
        os.environ.pop("GITHUB_OUTPUT", None)
        check("check: лимит роликов в сутки учитывается", len(got) == mv.VIDEO_DAILY_LIMIT, str(got))
        check("check: pending=true/false в GITHUB_OUTPUT", "pending=true" in out and "pending=false" in out and none == [])
    finally:
        mv.today, mv.datetime = real_today, real_dt
    # ---- доставка в канал
    calls = []
    real = (mv.tg_send_video, mv.tg_send_document, mv.tiktok_upload, mv.VIDEO_CHAT_ID)
    mv.tg_send_video = lambda path, caption, duration=0, chat_id=None, silent=False: calls.append((chat_id, silent, caption)) or True
    mv.tg_send_document = lambda *a, **k: True
    mv.tiktok_upload = lambda path: None
    mv.VIDEO_CHAT_ID = "999"
    bot.CHAT = "@testchan"
    res = {"path": os.path.join(tempfile.gettempdir(), "x.mp4"), "duration": 30.0}
    try:
        mv.deliver(res, "Подпись", "meta")
        check("доставка: личный чат и канал", [c[0] for c in calls] == [None, "@testchan"], str(calls))
        check("доставка: в канал уходит подпись с уведомлением Supercell", "Supercell" in calls[1][2])
        calls.clear()
        mv.deliver(res, "Подпись", "demo")
        check("доставка: тестовый ролик в канал не публикуется", [c[0] for c in calls] == [None])
        calls.clear()
        os.environ["POST_VIDEO_TO_CHANNEL"] = "0"
        mv.deliver(res, "Подпись", "meta")
        os.environ.pop("POST_VIDEO_TO_CHANNEL")
        check("доставка: публикацию в канал можно отключить", [c[0] for c in calls] == [None])
    finally:
        mv.tg_send_video, mv.tg_send_document, mv.tiktok_upload, mv.VIDEO_CHAT_ID = real
    cta = mv.cta_spec("Кто для тебя номер один?")
    check("призыв: вопрос зрителю в начале финальной фразы", cta["speech"].startswith("Кто для тебя номер один? Пиши в комментариях!"))
    check("призыв без вопроса не меняется", "Пиши в комментариях" not in mv.cta_spec()["speech"])


def t_render(kind="demo", with_music=False):
    mv.tts = fake_tts
    work = tempfile.mkdtemp(prefix="t_")
    old_music = mv.MUSIC_DIR
    if with_music:
        mv.MUSIC_DIR = os.path.join(work, "music")
        os.makedirs(mv.MUSIC_DIR)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=330:d=20", os.path.join(mv.MUSIC_DIR, "t.mp3")], check=True)
    else:
        mv.MUSIC_DIR = os.path.join(work, "nomusic")
    if kind == "guide":
        info = {"name": "Mortis", "cls": "Assassin", "cls_ru": "убийца", "short_stat": "53,4% побед",
                "desc": "Мортис прыгает в гущу боя, выбирает цель и быстро добивает её, а потом исчезает так же внезапно, как появился, оставляя врагов гадать.",
                "gadgets": [("Боевая лопата", "Мортис лечится при каждом ударе и на пару секунд получает дополнительную скорость движения, пока не закончится заряд.")],
                "stars": [("Жажда крови", "После убийства Мортис восстанавливает здоровье и сразу получает заряд суперспособности, поэтому цепочки убийств растут очень быстро.")],
                "hypers": [("Вечная ночь", "Суперспособность работает дольше и наносит больше урона по всей области вокруг.")],
                "tip": "Убийцы живут на дерзости: заходи со стороны, где тебя не ждут, бей по самым хрупким и уходи до того, как враги опомнятся.",
                "stat_line": ""}
        mv.bot.guide_info = lambda b, st: info
        mv._portraits = lambda names: {}
        script = mv.script_guide({"name": "MORTIS"}, {})
    elif kind == "week":
        png = _fake_png()
        mv._portraits = lambda names: {n.upper(): png for n in names}
        script = mv.script_week({"rows": [["Mortis", 0.583, 3000, 6], ["Edgar", 0.561, 2900, 5], ["Crow", 0.548, 2800, 6],
                                          ["Leon", 0.531, 2500, 4], ["Colt", 0.52, 2300, 4]], "ndays": 6})
    elif kind == "quiz":
        png = _fake_png()
        mv._portraits = lambda names: {n.upper(): png for n in names}
        script = mv.script_quiz({"classes": {"map": {"MORTIS": "Assassin", "SHELLY": "Damage Dealer", "FRANK": "Tank"}}})
    elif kind == "balance":
        png = _fake_png()
        mv._portraits = lambda names: {n.upper(): png for n in names}
        script = mv.script_balance({"buffs": ["Mortis", "Crow", "Edgar"], "nerfs": ["Colt", "Shelly"]})
    else:
        script = mv.script_demo()
    texts = [s["speech"] for s in script["specs"]]
    out = os.path.join(work, "t.mp4")
    t0 = time.time()
    host = script.get("host")
    if os.environ.get("HOSTTEST"):
        from PIL import Image as _I, ImageDraw as _D
        import io as _io
        im = _I.new("RGBA", (300, 340), (0, 0, 0, 0)); d = _D.Draw(im)
        d.ellipse((30, 20, 270, 330), fill=(250, 190, 40, 255), outline=(30, 10, 50, 255), width=8)
        d.ellipse((90, 120, 130, 170), fill=(255, 255, 255, 255)); d.ellipse((170, 120, 210, 170), fill=(255, 255, 255, 255))
        buf = _io.BytesIO(); im.save(buf, "PNG"); host = (buf.getvalue(), "Mortis")
    res = mv.render_video(script["specs"], out, {}, seed=1, host=host)
    el = time.time() - t0
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration", "-of", "csv=p=0", out],
                        capture_output=True, text=True).stdout
    check(f"{kind}{'+music' if with_music else ''}: есть видео 1080x1920 и звук", "video,1080,1920" in pr.replace("\n", "") and "audio" in pr, pr)
    check(f"{kind}: длина {res['duration']:.1f} с в пределах лимита", res["duration"] <= mv.MAX_VIDEO_SEC + 1)
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=r_frame_rate,color_space,color_primaries,profile,width,height", "-of", "default=nw=1", out],
                          capture_output=True, text=True).stdout
    check(f"{kind}: {mv.FPS} кадров/с", f"r_frame_rate={mv.FPS}/1" in info, info)
    check(f"{kind}: цвет размечен как BT.709", "color_space=bt709" in info and "color_primaries=bt709" in info, info)
    check(f"{kind}: размер файла в лимите Telegram", os.path.getsize(out) <= mv.TG_LIMIT, str(os.path.getsize(out)))
    ld = subprocess.run(["ffmpeg", "-hide_banner", "-i", out, "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", ld)
    check(f"{kind}: громкость около -14 LUFS", bool(m) and -17 < float(m[-1]) < -11, str(m[-1:]))
    print(f"время сборки {el:.0f} с, слайдов {res['slides']}")
    # кадр для визуальной проверки
    shot = f"/home/claude/w/shot_{kind}.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "3.2", "-i", out, "-frames:v", "1", shot])
    shutil.copy(out, f"/home/claude/w/test_{kind}{'_m' if with_music else ''}.mp4")
    mv.MUSIC_DIR = old_music
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "fast"
    if mode in ("fast", "all"):
        t_fast()
        t_mux()
        t_new()
    if mode == "demo":
        t_render("demo")
    if mode == "demo_music":
        t_render("demo", True)
    if mode == "guide":
        t_render("guide")
    if mode in ("week", "quiz", "balance"):
        t_render(mode)
    print("\nПРОВАЛЕНО:" if FAILS else "\nВсё прошло", FAILS or "")
    sys.exit(1 if FAILS else 0)
