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
    samples = [
        "Это быстрый боец дальнего боя, который любит держать дистанцию и забрасывать врагов гранатами из-за укрытий.",
        "Мортис: наносит урон по области, затем отталкивает ближайших врагов и на короткое время оглушает всех, кого зацепил рывок. " * 3,
        "Это универсальный разбор. Выбирай под карту и режим.",
        "Слово " * 70,
    ]
    for i, text in enumerate(samples):
        parts = mv.chunk_text(text)
        joined = " ".join(parts).split()
        check(f"чанки #{i}: слова не потеряны", joined == text.split(), f"{len(joined)} vs {len(text.split())}")
        check(f"чанки #{i}: каждый помещается", all(mv.text_fits(p) for p in parts), str(len(parts)))
        check(f"чанки #{i}: нет '…'", all("…" not in p for p in parts))
        for p in parts[:1]:
            layer = mv.L_text("Тест", p)
            check(f"слои #{i}: слова размечены", len(layer.words) == len(p.split()))
    sp_ = mv.text_specs("Гаджет", "Быстрая стрелка: " + samples[0], mv.ORANGE, 7, say_first="Гаджет.")
    check("text_specs: озвучка = экран", all(re.sub(r"^Гаджет\. ", "", s["speech"]) == " ".join(w["word"] for w in s["words"]) for s in sp_))
    check("text_specs: общий group", len({s["group"] for s in sp_}) == 1)
    words = [{"word": w} for w in "Привет, это тест. Как дела?".split()]
    tm = mv.word_times(words, "Гаджет.", 4.0)
    check("тайминги: растут и укладываются", all(tm[i][1] <= tm[i + 1][0] + 1e-9 for i in range(len(tm) - 1)) and abs(tm[-1][1] - 4.0) < 1e-6, str(tm))
    check("тайминги: лид занимает начало", tm[0][0] > 0.2)
    # плавность подсветки: яркость плашки растёт, держится и гаснет без скачков
    wd = {"t0": 1.0, "t1": 1.4}
    seq = [mv.karaoke_alpha(wd, 0.9 + i * 0.02)[0] for i in range(60)]
    steps = [abs(seq[i + 1] - seq[i]) for i in range(len(seq) - 1)]
    check("подсветка: нет скачков яркости", max(steps) <= 0.3 and seq[0] == 0 and max(seq) == 1 and seq[-1] == 0, str(max(steps)))
    # наложения: низ любого слайда не заходит на зону ведущего и пузыря
    real_sun = mv.sunburst
    mv.sunburst = lambda *a, **k: None  # лучи на фоне могут заходить куда угодно, проверяем только текст и плашки
    layers = [mv.script_demo()["specs"]]
    for sl in layers[0]:
        bottom = sl["layer"].getbbox()[3] + mv.CONTENT_DY
        check(f"нет наложения на ведущего ({sl['speech'][:18]})", bottom <= mv.BUBBLE_Y - 4, str(bottom))
    hero = mv.L_hero("Mortis", "самый популярный", "53,4% побед", None, None)
    check("L_hero: низ выше зоны ведущего", hero.getbbox()[3] + mv.CONTENT_DY <= mv.BUBBLE_Y - 4, str(hero.getbbox()))
    mv.sunburst = real_sun
    from PIL import Image as _I
    t = mv.tilt(_I.new("RGBA", (400, 300), (255, 0, 0, 255)), 0.8)
    check("наклон: размер сохранён, края не пустые", t.size == (400, 300) and t.getbbox() is not None)
    import io as _io
    buf = _io.BytesIO(); _I.new("RGB", (200, 240), (60, 120, 200)).save(buf, "PNG")
    hs = mv.make_host(buf.getvalue(), "Mortis")
    check("диктор: 8 спрайтов нужного размера", len(hs) == 8 and hs[(0, 0)].size == mv.MASCOT_SIZE)
    check("диктор: открытый рот отличается от закрытого", hs[(0, 0)].tobytes() != hs[(3, 0)].tobytes())
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
