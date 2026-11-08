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


def fake_tts(text, out_path):
    """Подмена озвучки: тон с «слогами», длина пропорциональна тексту (как у настоящего голоса, ~15 символов в секунду)."""
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
    check("диктор: 8 спрайтов нужного размера", len(hs) == 8 and hs[(0, 0)].size == mv.MASCOT_SIZE)
    check("диктор: открытый рот отличается от закрытого", hs[(0, 0)].tobytes() != hs[(3, 0)].tobytes())
    # регрессия: кадры не должны портить исходный слой (раньше он становился полупрозрачным)
    cont = _I.new("RGBA", (300, 200), (255, 0, 0, 255))
    sl = {"dur": 0.6, "content": (cont, (100, 400)), "mouth": [0] * 15, "anim": 0, "kind": "title", "burst_y": 500, "props": []}
    list(mv.render_frames([sl], [mv.make_bg(0, 0)], [], mv.make_mascot(), 15))
    check("кадры не меняют исходный слой", cont.getpixel((150, 100)) == (255, 0, 0, 255), str(cont.getpixel((150, 100))))
    bub = mv.make_bubble("Слово " * 80)
    check("пузырь: не режет, а пропускается", bub is None)
    check("пузырь: короткая реплика есть", mv.make_bubble("Мета Brawl Stars сегодня") is not None)


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
    else:
        script = mv.script_demo()
    texts = [s["speech"] for s in script["specs"]]
    out = os.path.join(work, "t.mp4")
    t0 = time.time()
    host = None
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
    if mode == "demo":
        t_render("demo")
    if mode == "demo_music":
        t_render("demo", True)
    if mode == "guide":
        t_render("guide")
    print("\nПРОВАЛЕНО:" if FAILS else "\nВсё прошло", FAILS or "")
    sys.exit(1 if FAILS else 0)
