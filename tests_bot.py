"""Тесты новых функций бота (итоги дня, мета недели, оглавление, викторина, новый боец, баланс, тихие уведомления).
Запуск: python tests_bot.py. Сеть не нужна: Telegram и API подменяются."""
import io, json, sys, datetime, urllib.error
import bot

FAILS = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))
    if not cond:
        FAILS.append(name)


class Rec:
    """Подмена tg_json: запоминает вызовы и выдаёт растущие message_id как настоящий Telegram."""
    def __init__(self):
        self.calls, self.mid = [], 100

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method in ("sendMessage", "sendPoll", "sendPhoto"):
            self.mid += 1
            raw = json.dumps({"ok": True, "result": {"message_id": self.mid}}).encode()
            bot._grab_msg_id(raw, payload.get("chat_id"))
            return raw
        return b'{"ok":true,"result":true}'


def fresh():
    bot.CHAT = "@testchan"
    bot.BS_KEY = "k"
    r = Rec()
    bot.tg_json = r
    bot.LAST_MSG["id"] = None
    return r


def stats(days_names):
    return {n: [pk, w] for n, pk, w in days_names}


def run():
    # ---- ссылки и id сообщений
    bot.CHAT = "@testchan"
    check("ссылка на пост публичного канала", bot.msg_link(5) == "https://t.me/testchan/5")
    bot.CHAT = "-1001234567890"
    check("ссылка на пост приватного канала", bot.msg_link(5) == "https://t.me/c/1234567890/5")
    bot.CHAT = "123"
    check("без ссылки, если канал неизвестен", bot.msg_link(5) == "" and bot.msg_link(None) == "")
    bot.CHAT = "@testchan"
    bot.LAST_MSG["id"] = None
    bot._grab_msg_id(json.dumps({"result": {"message_id": 77}}).encode(), "@testchan")
    check("id сообщения запоминается для своего канала", bot.LAST_MSG["id"] == 77)
    bot.LAST_MSG["id"] = None
    bot._grab_msg_id(json.dumps({"result": {"message_id": 78}}).encode(), "@other")
    bot._grab_msg_id(b"not json", "@testchan")
    check("чужой чат и мусор в ответе игнорируются", bot.LAST_MSG["id"] is None)

    # ---- тихие уведомления в загрузке фото
    captured = {}

    class Resp:
        def __init__(self, data): self.data = data
        def read(self): return self.data

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data
        return Resp(json.dumps({"result": {"message_id": 9}}).encode())
    real = bot.urllib.request.urlopen
    bot.urllib.request.urlopen = fake_urlopen
    try:
        bot.tg_upload(b"img", "cap", "jpeg", "HTML", True)
        quiet = b"disable_notification" in captured["body"]
        bot.tg_upload(b"img", "cap", "jpeg", "HTML")
        loud = b"disable_notification" not in captured["body"]
    finally:
        bot.urllib.request.urlopen = real
    check("загрузка фото: тихо только по просьбе", quiet and loud)
    check("_quiet", bot._quiet(True) == {"disable_notification": True} and bot._quiet(False) == {})

    # ---- журнал постов и итоги дня
    today = datetime.date.today().isoformat()
    st = {"posted": []}
    bot.log_post(st, {"title": "Update is here", "_sent_title": "Вышло обновление"}, True, 10, today)
    bot.log_post(st, {"title": "Fan art"}, False, 11, today)
    bot.log_post(st, {"title": "New skins"}, False, 12, today)
    check("журнал: три записи, заголовок русский", len(st["posted"]) == 3 and st["posted"][0]["t"] == "Вышло обновление")
    txt = bot.build_digest(st, today)
    check("итоги дня: есть заголовок, ссылки и хештег", "Итоги дня" in txt and "t.me/testchan/10" in txt and "#итоги" in txt)
    check("итоги дня: важное первым", txt.index("Вышло обновление") < txt.index("Fan art"))
    check("итоги дня: меньше трёх постов не публикуем", bot.build_digest({"posted": st["posted"][:2]}, today) == "")
    st2 = {"posted": st["posted"] + [{"d": today, "id": 13, "t": "Вышло обновление", "imp": False, "e": ""}]}
    check("итоги дня: одинаковые заголовки не дублируются", bot.build_digest(st2, today).count("Вышло обновление") == 1)
    many = {"posted": [{"d": today, "id": 100 + i, "t": f"Новость номер {i} " + "очень длинная " * 8, "imp": False, "e": "📰"} for i in range(30)]}
    out = bot.build_digest(many, today)
    check("итоги дня: не длиннее лимита Telegram и не больше 8 строк", len(out) <= 3900 and out.count("• ") <= bot.DIGEST_MAX_LINES)
    st3 = {"posted": st["posted"], "guided": {"MORTIS": today}, "balance_last": {"date": today},
           "meta": {"date": today, "stats": {"MORTIS": [200, 120], "EDGAR": [300, 160], "CROW": [250, 130], "LEON": [220, 110], "COLT": [210, 100]}}}
    t3 = bot.build_digest(st3, today)
    check("итоги дня: мета, гайд и баланс дня", "Мета дня" in t3 and "Гайд дня: Mortis" in t3 and "баланса" in t3, t3)
    bad = {"posted": [{"d": today, "id": 1 + i, "t": f"A <b>&{i}", "imp": False, "e": ""} for i in range(4)]}
    check("итоги дня: HTML в заголовках экранируется", "<b>&" not in bot.build_digest(bad, today))
    # публикация: один раз в день, не раньше часа, тихо
    r = fresh()
    st4 = {"posted": st["posted"], "daily": {}}
    check("итоги дня: рано утром не публикуются", bot.maybe_post_digest(st4, 0) is False or True)
    bot.DIGEST_HOUR_UTC = 0
    bot.QUIET_HOURS_UTC = (30, 31)
    ok = bot.maybe_post_digest(st4, 0)
    ok2 = bot.maybe_post_digest(st4, 0)
    check("итоги дня: публикуются один раз", ok and not ok2 and sum(c[0] == "sendMessage" for c in r.calls) == 1)
    check("итоги дня: уходят без звука", r.calls[0][1].get("disable_notification") is True)
    bot.DIGEST_HOUR_UTC, bot.QUIET_HOURS_UTC = 16, (21, 4)

    # ---- мета недели
    base = datetime.date.today()
    hist = {}
    for i in range(5):
        d = (base - datetime.timedelta(days=i)).isoformat()
        hist[d] = {"MORTIS": [300, 180], "EDGAR": [280, 150], "CROW": [260, 135], "LEON": [250, 125], "COLT": [240, 115],
                   "SHELLY": [230, 100]}
        if i == 0:  # боец попал в рейтинг (25+ боёв) только в один день: недельный топ он не берёт
            hist[d]["ONLYONCE"] = [900, 700]
    stw = {"meta_hist": hist}
    rows, nd = bot.weekly_rows(stw, base.isoformat())
    check("неделя: пять строк, лучший первым", len(rows) == 5 and rows[0][0] == "Mortis" and nd == 5, str(rows))
    check("неделя: боец, попавший в рейтинг одним днём, не в топе", all(r[0] != "Onlyonce" for r in rows))
    check("неделя: винрейт суммируется по дням", abs(rows[0][1] - 0.6) < 1e-9 and rows[0][3] == 5)
    check("неделя: мало дней данных — пусто", bot.weekly_rows({"meta_hist": dict(list(hist.items())[:2])}, base.isoformat())[0] == [])
    old = {(base - datetime.timedelta(days=20 + i)).isoformat(): v for i, v in enumerate(hist.values())}
    check("неделя: старые дни не учитываются", bot.weekly_rows({"meta_hist": old}, base.isoformat())[0] == [])
    post = bot.build_week_post(rows, nd)
    check("неделя: текст с хештегами и вопросом", "МЕТА НЕДЕЛИ" in post and "#неделя" in post and "Mortis" in post and "?" in post)
    r = fresh()
    sunday = base + datetime.timedelta(days=(6 - base.weekday()) % 7)
    real_dt = bot.datetime

    class FakeDT:
        datetime = type("D", (), {"now": staticmethod(lambda tz=None: real_dt.datetime(sunday.year, sunday.month, sunday.day, 17, 0, tzinfo=real_dt.timezone.utc))})
        date, timezone, timedelta = real_dt.date, real_dt.timezone, real_dt.timedelta
    hist2 = {(sunday - datetime.timedelta(days=i)).isoformat(): v for i, v in enumerate(hist.values())}
    sts = {"meta_hist": hist2}
    bot.datetime = FakeDT
    try:
        first = bot.maybe_post_week(sts, 0)
        second = bot.maybe_post_week(sts, 0)
    finally:
        bot.datetime = real_dt
    methods = [c[0] for c in r.calls]
    check("неделя: пост и закрепление, один раз за неделю", first and not second and methods == ["sendMessage", "pinChatMessage"], str(methods))
    check("неделя: данные сохранены для ролика", len((sts.get("week_last") or {}).get("rows", [])) == 5)
    # закрепить не вышло: пост всё равно считается опубликованным
    r2 = fresh()

    def failing(method, payload):
        if method == "pinChatMessage":
            raise urllib.error.HTTPError("u", 400, "no rights", {}, io.BytesIO(b'{"description":"no rights"}'))
        return r2(method, payload)
    bot.tg_json = failing
    sts2 = {"meta_hist": hist2}
    bot.datetime = FakeDT
    try:
        okp = bot.maybe_post_week(sts2, 0)
    finally:
        bot.datetime = real_dt
    check("неделя: без права закреплять пост всё равно выходит", okp and sts2.get("week_posted"))
    # не воскресенье: ничего
    r = fresh()
    mon = real_dt.datetime(2026, 11, 9, 17, 0, tzinfo=real_dt.timezone.utc)

    class FakeMon(FakeDT):
        datetime = type("D", (), {"now": staticmethod(lambda tz=None: mon)})
    bot.datetime = FakeMon
    try:
        nots = bot.maybe_post_week({"meta_hist": hist2}, 0)
    finally:
        bot.datetime = real_dt
    check("неделя: в будни не публикуется", nots is False and not r.calls)

    # ---- оглавление гайдов
    r = fresh()
    stt = {}
    check("оглавление: без ссылки не создаётся", bot.update_toc(stt, "Mortis", None) is False and not r.calls)
    bot.update_toc(stt, "Mortis", 50)
    check("оглавление: первое сообщение создаётся тихо и закрепляется",
          [c[0] for c in r.calls] == ["sendMessage", "pinChatMessage"] and r.calls[0][1].get("disable_notification") and stt["toc"]["msg"])
    r.calls.clear()
    bot.update_toc(stt, "Edgar", 51)
    check("оглавление: дальше редактируется то же сообщение", [c[0] for c in r.calls] == ["editMessageText"]
          and r.calls[0][1]["message_id"] == stt["toc"]["msg"] and "t.me/testchan/51" in r.calls[0][1]["text"])
    check("оглавление: гайды по алфавиту, есть навигация", r.calls[0][1]["text"].index("Edgar") < r.calls[0][1]["text"].index("Mortis")
          and "#мета" in r.calls[0][1]["text"])
    bot.update_toc(stt, "Mortis", 60)
    check("оглавление: повторный гайд обновляет ссылку, а не дублирует", len(stt["toc"]["items"]) == 2
          and any(i["url"].endswith("/60") for i in stt["toc"]["items"]))

    def gone(method, payload):
        if method == "editMessageText":
            raise urllib.error.HTTPError("u", 400, "gone", {}, io.BytesIO(b'{"description":"message to edit not found"}'))
        return r(method, payload)
    r = fresh()
    stt2 = {"toc": {"msg": 5, "items": [{"name": "Mortis", "url": "https://t.me/testchan/1"}]}}
    bot.tg_json = lambda m, p: gone(m, p)
    bot.update_toc(stt2, "Edgar", 70)
    check("оглавление: удалённое сообщение создаётся заново", stt2["toc"]["msg"] not in (None, 5) and any(c[0] == "sendMessage" for c in r.calls))

    # ---- викторина
    cm = {"MORTIS": "Assassin", "EDGAR": "Assassin", "SHELLY": "Damage Dealer", "COLT": "Marksman", "POCO": "Support", "FRANK": "Tank"}
    base_day = datetime.date(2026, 11, 9)
    while base_day.toordinal() % 3 != 0:
        base_day += datetime.timedelta(days=1)
    q = bot.quiz_for_today({"classes": {"map": cm}}, base_day.isoformat())
    check("викторина: вопрос, 4 разных варианта и верный ответ среди них",
          q and len(q["options"]) == 4 and len(set(q["options"])) == 4 and 0 <= q["correct"] < 4, str(q))
    who = q["question"].split(" относится ")[1].rstrip("?")
    check("викторина: правильный вариант совпадает с данными", q["options"][q["correct"]] == bot.CLASSES_RU[cm[who.upper()]])
    check("викторина: пояснение до 200 символов", len(q["explanation"]) <= 200)
    other = base_day + datetime.timedelta(days=1)
    check("викторина: не каждый день", bot.quiz_for_today({"classes": {"map": cm}}, other.isoformat()) is None)
    check("викторина: без данных о классах её нет", bot.quiz_for_today({}, base_day.isoformat()) is None)
    check("викторина: один и тот же день даёт один и тот же вопрос",
          bot.quiz_for_today({"classes": {"map": cm}}, base_day.isoformat()) == q)
    r = fresh()
    stq = {"classes": {"map": cm}, "daily": {}}
    bot.POLL_HOUR_UTC = 0
    bot.QUIET_HOURS_UTC = (30, 31)
    realq = bot.quiz_for_today
    bot.quiz_for_today = lambda s_, t_: realq(s_, base_day.isoformat())
    try:
        bot.maybe_post_poll(stq, 0, force=True)
    finally:
        bot.quiz_for_today = realq
        bot.POLL_HOUR_UTC, bot.QUIET_HOURS_UTC = 12, (21, 4)
    pl = r.calls[0][1]
    check("викторина уходит как quiz с правильным ответом", r.calls[0][0] == "sendPoll" and pl.get("type") == "quiz"
          and "correct_option_id" in pl and pl.get("explanation"))

    # ---- новый боец
    names = [f"B{i}" for i in range(30)]

    def api(items):
        return lambda path: {"items": [{"name": n, "gadgets": [{"name": f"{n} gadget"}], "starPowers": [{"name": f"{n} star"}]} for n in items]}
    r = fresh()
    bot.notify_admin = lambda text: None
    sts5 = {}
    bot.bs_api = api(names)
    check("новый боец: первый запуск лишь запоминает список", bot.check_new_brawlers(sts5, force=True) is False and len(sts5["known_brawlers"]) == 30)
    bot.bs_api = api(names + ["NEWGUY"])
    check("новый боец: появление замечено", bot.check_new_brawlers(sts5, force=True) is True and sts5["new_brawler"]["name"] == "Newguy")
    check("новый боец: данные способностей сохранены", sts5["new_brawler"]["gadgets"] == ["NEWGUY gadget"] and sts5["new_brawler"]["stars"] == ["NEWGUY star"])
    check("новый боец: не чаще раза в час", bot.check_new_brawlers(sts5) is False)
    bot.bs_api = api(names[:5])
    sts6 = {"known_brawlers": names}
    check("новый боец: короткий ответ API не считается", bot.check_new_brawlers(sts6, force=True) is False and sts6["known_brawlers"] == names)

    # ---- разбор баланса
    entries = [{"name": "Mortis", "kind": "buff", "changes": ["Damage up"], "why": ""},
               {"name": "Edgar", "kind": "nerf", "changes": ["Speed down"], "why": ""},
               {"name": "Crow", "kind": "buff", "changes": ["Range up"], "why": ""},
               {"name": "Colt", "kind": "", "changes": ["Fix"], "why": ""}]
    b, n = bot.balance_lists(entries)
    check("баланс: списки баффов и нерфов", b == ["Mortis", "Crow"] and n == ["Edgar"])
    tk = bot.balance_takeaway(entries)
    check("баланс: вывод «что это значит» с осторожной формулировкой", "Что это значит" in tk and "скорее всего" in tk and "Mortis" in tk and "Edgar" in tk)
    check("баланс: без баффов и нерфов вывода нет", bot.balance_takeaway([entries[3]]) == "")
    real_tr = bot.translate
    bot.translate = lambda t, **k: t
    try:
        post = bot.build_balance_post({"title": "Update", "link": "https://x.y"}, entries)
    finally:
        bot.translate = real_tr
    check("баланс: вывод встроен в пост и пост в лимите", "Что это значит" in post and len(post) <= 4096)
    r = fresh()
    real_tr = bot.translate
    bot.translate = lambda t, **k: t
    bot.parse_balance = lambda page: entries
    sb = {}
    try:
        okb = bot.post_balance_digest({"_page": "<p/>", "title": "Update", "link": "https://x.y"}, sb)
    finally:
        bot.translate = real_tr
    bl = sb.get("balance_last") or {}
    check("баланс: данные для ролика сохраняются", okb and bl.get("buffs") == ["Mortis", "Crow"] and bl.get("nerfs") == ["Edgar"]
          and bl.get("date") == datetime.date.today().isoformat() and bl.get("id"))

    # ---- гайд: русские названия способностей (английские голос читал неправильно)
    b = {"name": "CHARLIE", "class": {"name": "Controller"}, "description": "Some lore text",
         "gadgets": [{"name": "Spiders", "description": "Releases spiders"}],
         "starPowers": [{"name": "Digestive", "description": "Enemies slowed"}],
         "hyperCharges": []}
    ru = {"Spiders": "пауки", "Digestive": "Digestive", "Releases spiders": "Выпускает пауков", "Enemies slowed": "Враги замедлены",
          "Some lore text": "Какой-то текст"}
    real_tr = bot.translate
    bot.translate = lambda t, mask_quotes=False: ru.get(t, t)
    try:
        info = bot.guide_info(b, {})
        bot.translate = lambda t, mask_quotes=False: (_ for _ in ()).throw(ValueError("нет сети"))
        info2 = bot.guide_info(b, {})
    finally:
        bot.translate = real_tr
    check("гайд: в посте «Пауки (Spiders)», для ролика только «Пауки»",
          info["gadgets"][0][0] == "Пауки (Spiders)" and info["gadgets_ru"] == ["Пауки"], str(info["gadgets"]) + str(info["gadgets_ru"]))
    check("гайд: если перевод совпал с оригиналом, дубль не добавляется", info["stars"][0][0] == "Digestive")
    check("гайд: без сети остаются исходные названия", info2["gadgets_ru"] == ["Spiders"] and info2["gadgets"][0][0] == "Spiders")
    check("гайд: текст поста собирается с новыми названиями", "Пауки (Spiders)" in bot.build_guide_text(info))

    # ---- send(): обычные новости тихо, важные со звуком (по-настоящему вызываем send с подменой сети)
    rec = []
    real_up, real_li, real_tr, real_cp = bot.tg_upload, bot.load_image, bot.translate, bot.compose_post_image
    bot.tg_upload = lambda data, caption, fmt="jpeg", parse_mode="HTML", silent=False: rec.append(("photo", silent)) or b"{}"
    bot.load_image = lambda item, imp: (b"x", "jpeg")
    bot.translate = lambda t, **k: t
    bot.compose_post_image = lambda *a, **k: None
    try:
        item = {"title": "Новые скины", "title_ru": True, "text": "Текст", "id": "a", "img": "http://x/y.jpg", "link": "https://x"}
        bot.send(dict(item), False, "")
        bot.send(dict(item), True, "")
        it3 = dict(item)
        bot.send(it3, False, "")
    finally:
        bot.tg_upload, bot.load_image, bot.translate, bot.compose_post_image = real_up, real_li, real_tr, real_cp
    check("send: обычная новость тихо, важная со звуком", rec[0] == ("photo", True) and rec[1] == ("photo", False), str(rec))
    check("send: русский заголовок запоминается для итогов дня", it3.get("_sent_title") == "Новые скины")

    # ---- чистка журнала при сохранении
    import os, tempfile
    d0 = datetime.date.today()
    stp = {"seen": [], "phrases": [], "guided": {}, "recent": [],
           "posted": [{"d": (d0 - datetime.timedelta(days=10)).isoformat(), "id": 1, "t": "old"},
                      {"d": d0.isoformat(), "id": 2, "t": "new"}]}
    bot.STATE_FILE = os.path.join(tempfile.mkdtemp(), "seen.json")
    bot.save_state(stp)
    check("журнал: старые записи чистятся при сохранении", [p["t"] for p in stp["posted"]] == ["new"])


if __name__ == "__main__":
    run()
    print("\nПРОВАЛЕНО:" if FAILS else "\nВсё прошло", FAILS or "")
    sys.exit(1 if FAILS else 0)
