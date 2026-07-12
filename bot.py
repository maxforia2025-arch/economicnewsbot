#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
economicnewrussiabot — автопостер новостей экономики РФ по индексу цитируемости.

Принцип: источником может быть кто угодно (СМИ, агрегатор, телеграм-канал).
Критерий публикации — не «откуда», а «насколько процитировано»:
широкий захват -> кластеризация по сюжетам -> подсчёт независимых источников
-> публикуются только сюжеты, перешагнувшие порог цитируемости.

Только стандартная библиотека Python. Только бесплатные источники:
  - GDELT DOC 2.0 API (сотни тысяч мировых источников, без ключа)
  - Google News RSS (готовая кластеризация + источник в заголовке)
  - Публичные Telegram-каналы (веб-витрина t.me/s/<channel>)

Запуск:
  python3 bot.py            # один проход: собрать, оценить, опубликовать
  python3 bot.py --dry-run  # то же, но без отправки в Telegram (печать в консоль)
  python3 bot.py --loop     # бесконечный цикл с паузой interval_hours
"""

import json
import os
import random
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

STOP_WORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы
по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг
ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без
будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой
совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех
никогда можно при наконец два об другой хоть после над больше тот через эти нас
про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда
лучше чуть том нельзя такой им более всегда конечно всю между это рф россии года
году новости стало стали может году дня млрд млн руб против читать подробнее
""".split())


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


SECRET_PATH = os.path.join(BASE_DIR, "secret.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Секреты вне репозитория: env (GitHub Secrets) -> локальный secret.json -> config
    token = os.environ.get("BOT_TOKEN")
    channel = os.environ.get("CHANNEL_ID")
    gemini = os.environ.get("GEMINI_KEY")
    if (not token or not channel or not gemini) and os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            sec = json.load(f)
        token = token or sec.get("bot_token")
        channel = channel or sec.get("channel_id")
        gemini = gemini or sec.get("gemini_key")
    cfg["bot_token"] = token or cfg.get("bot_token", "")
    cfg["channel_id"] = channel or cfg.get("channel_id", "")
    cfg["gemini_key"] = gemini or cfg.get("gemini_key", "")
    return cfg


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
    # определяем кодировку: заголовок -> XML-декларация -> utf-8
    enc = None
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        enc = m.group(1)
    if not enc:
        head = raw[:200].decode("ascii", "ignore").lower()
        m = re.search(r'encoding=["\']([\w-]+)', head)
        if m:
            enc = m.group(1)
    for candidate in [enc, "utf-8", "windows-1251"]:
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def normalize(text):
    """Заголовок -> множество значимых токенов (для сравнения сюжетов)."""
    text = html.unescape(text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-zа-яё0-9 ]+", " ", text)
    tokens = [t for t in text.split() if len(t) > 2 and t not in STOP_WORDS]
    # лёгкий стемминг: отбрасываем хвосты русских окончаний
    stemmed = set()
    for t in tokens:
        if len(t) > 6:
            t = re.sub(r"(ами|ями|ов|ев|ие|ый|ой|ая|ую|ет|ут|ют|ал|ил|ла|ло|ы|и|а|е|у|о)$", "", t)
        stemmed.add(t)
    return stemmed


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def domain_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def is_relevant(title, keywords):
    low = (title or "").lower()
    return any(k in low for k in keywords)


def clean_desc(text):
    """Чистим описание из RSS: теги, типовой мусор, лишние пробелы."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(?i)(FINMARKET\.RU|ИНТЕРФАКС|РИА Новости|РБК)\s*[-—:]\s*", "", text)
    text = re.sub(r"(?i)(читать далее|подробнее|читайте также|©.*)$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cap_text(text, max_chars=2000):
    """Ограничить длину, сохранив абзацы и оборвав по концу предложения."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    idx = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
    if idx > max_chars * 0.5:
        return cut[:idx + 1].strip()
    return cut.strip() + "…"


def trim_sentences(text, max_sentences=22, max_chars=1125):
    """Оставляем до N предложений и не длиннее max_chars."""
    text = text.strip()
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out, total = [], 0
    for p in parts[:max_sentences]:
        if total + len(p) > max_chars and out:
            break
        out.append(p)
        total += len(p) + 1
    result = " ".join(out).strip()
    if result and result[-1] not in ".!?…":
        result += "…"
    return result


# ---------------------------------------------------------------------------
# Источники (захват). Каждый возвращает список dict: {title, url, source}
# ---------------------------------------------------------------------------

def fetch_gdelt(keywords):
    """GDELT DOC 2.0 — широкий мировой захват, русскоязычные статьи об экономике РФ."""
    items = []
    query = '(экономика OR рубль OR инфляция OR санкции OR "ключевая ставка") sourcelang:russian'
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query="
           + urllib.parse.quote(query)
           + "&mode=ArtList&maxrecords=75&sort=DateDesc&format=json&timespan=1d")
    try:
        data = json.loads(http_get(url))
    except Exception as e:
        log(f"GDELT: ошибка ({e})")
        return items
    for art in data.get("articles", []):
        title = art.get("title", "").strip()
        u = art.get("url", "")
        if title and u and is_relevant(title, keywords):
            dom = art.get("domain") or domain_of(u)
            items.append({"title": title, "url": u, "desc": "",
                          "source": dom, "source_name": dom})
    log(f"GDELT: {len(items)} релевантных статей")
    return items


def fetch_outlet_rss(feeds, keywords):
    """Прямые RSS-ленты изданий — дают заголовок И лид-абзац (пояснение)."""
    items = []
    for source, url in feeds.items():
        try:
            root = ET.fromstring(http_get(url))
        except Exception as e:
            log(f"RSS {source}: ошибка ({e})")
            continue
        cnt = 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = clean_desc(item.findtext("description") or "")
            if title and is_relevant(title + " " + desc, keywords):
                items.append({"title": title, "url": link or url,
                              "desc": desc, "source": source.lower(),
                              "source_name": source})
                cnt += 1
        log(f"RSS {source}: {cnt} релевантных")
    return items


def fetch_google_news(queries, keywords):
    """Google News RSS — уже сгруппированы по сюжетам, источник зашит в заголовок."""
    items = []
    for q in queries:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q)
               + "&hl=ru&gl=RU&ceid=RU:ru")
        try:
            xml = http_get(url)
            root = ET.fromstring(xml)
        except Exception as e:
            log(f"Google News '{q}': ошибка ({e})")
            continue
        for item in root.iter("item"):
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            # формат заголовка: "Текст новости - Источник"
            source = ""
            title = raw_title
            if " - " in raw_title:
                title, source = raw_title.rsplit(" - ", 1)
            src_tag = item.find("source")
            if src_tag is not None and src_tag.text:
                source = src_tag.text.strip()
            if title and link and is_relevant(title, keywords):
                items.append({"title": title.strip(), "url": link, "desc": "",
                              "source": (source or "googlenews").strip().lower(),
                              "source_name": (source or "").strip()})
    log(f"Google News: {len(items)} релевантных заголовков")
    return items


def fetch_telegram(channels, keywords):
    """Публичные Telegram-каналы через веб-витрину t.me/s/<channel>."""
    items = []
    msg_re = re.compile(
        r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
    tag_re = re.compile(r"<[^>]+>")
    for ch in channels:
        url = f"https://t.me/s/{ch}"
        try:
            page = http_get(url)
        except Exception as e:
            log(f"Telegram @{ch}: ошибка ({e})")
            continue
        for block in msg_re.findall(page):
            text = html.unescape(tag_re.sub(" ", block)).strip()
            text = re.sub(r"\s+", " ", text)
            if len(text) < 25:
                continue
            title = text[:200]
            if is_relevant(title, keywords):
                items.append({"title": title, "url": url, "desc": text,
                              "source": f"tg:{ch}", "source_name": "Telegram"})
    log(f"Telegram: {len(items)} релевантных сообщений из {len(channels)} каналов")
    return items


# ---------------------------------------------------------------------------
# Кластеризация и индекс цитируемости
# ---------------------------------------------------------------------------

def fallback_body(cluster):
    """Если лид-абзаца нет — собрать пояснение из разных заголовков сюжета."""
    best_title = max(cluster["items"],
                     key=lambda i: (not i["source"].startswith("tg:"), len(i["title"])))
    seen = [normalize(best_title["title"])]
    picks = []
    for it in sorted(cluster["items"], key=lambda i: len(i["title"]), reverse=True):
        nt = normalize(it["title"])
        if not nt or any(jaccard(nt, s) > 0.55 for s in seen):
            continue
        seen.append(nt)
        picks.append(it["title"].strip().rstrip(". "))
        if len(picks) >= 5:
            break
    return ". ".join(picks)


def cluster_items(items, sim_threshold=0.32):
    """Жадная кластеризация по сходству заголовков. Возвращает список кластеров.

    Сравниваем с «семенем» кластера (токены первого сообщения), а не с
    разрастающимся объединением — иначе крупные сюжеты перестают притягивать
    свои же переформулировки и плодятся дубли.
    """
    clusters = []
    for it in items:
        it["_tokens"] = normalize(it["title"])
        if not it["_tokens"]:
            continue
        best, best_sim = None, 0.0
        for cl in clusters:
            sim = jaccard(it["_tokens"], cl["seed"])
            if sim > best_sim:
                best, best_sim = cl, sim
        if best and best_sim >= sim_threshold:
            best["items"].append(it)
        else:
            clusters.append({"items": [it], "seed": set(it["_tokens"])})
    # индекс цитируемости = число независимых источников в кластере
    for cl in clusters:
        cl["tokens"] = set().union(*[i["_tokens"] for i in cl["items"]])
        sources = {i["source"] for i in cl["items"] if i["source"]}
        cl["citation"] = len(sources)
        cl["sources"] = sorted(sources)
        # лучший заголовок = самый длинный среди СМИ (не телеграм-обрывок)
        ranked = sorted(cl["items"],
                        key=lambda i: (not i["source"].startswith("tg:"), len(i["title"])),
                        reverse=True)
        cl["best"] = ranked[0]
        # URL для мини-превью: реальная статья издания (не редирект Google, не telegram)
        cl["preview_url"] = ""
        for it in cl["items"]:
            u = it.get("url", "")
            if u.startswith("http") and "news.google." not in u and "//t.me/" not in u:
                cl["preview_url"] = u
                break
        # пояснение = самый длинный лид-абзац среди источников кластера
        with_desc = [i for i in cl["items"] if i.get("desc")]
        cl["body"] = max(with_desc, key=lambda i: len(i["desc"]))["desc"] if with_desc else ""
        if not cl["body"]:
            cl["body"] = fallback_body(cl)
    clusters.sort(key=lambda c: c["citation"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# История (антидубль) — JSON-файл (коммитится обратно в репозиторий в облаке)
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(history):
    # храним последние 400 записей, чтобы файл не разрастался
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history[-400:], f, ensure_ascii=False, indent=0)


def already_posted(history, tokens):
    """Совпадает ли сюжет с чем-то опубликованным ранее."""
    for rec in history[-300:]:
        prev = set(rec.get("sig", "").split(","))
        if jaccard(tokens, prev) >= 0.45:
            return True
    return False


def mark_posted(history, tokens, title):
    history.append({
        "sig": ",".join(sorted(tokens)),
        "title": title,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Формирование и отправка поста
# ---------------------------------------------------------------------------

WHY_RULES = [
    (("ключев", "ключевая ставк", "ставку до", "ставки до", "дкп",
      "денежно-кредитн", "смягчени", "ужесточени"),
     "Ключевая ставка задаёт проценты по кредитам, ипотеке и вкладам — её изменение напрямую отражается на вашем кошельке."),
    (("инфляц", "подорожа", "цены выросл", "рост цен"),
     "Инфляция — это скорость роста цен в магазинах: чем она выше, тем быстрее дорожает повседневная жизнь."),
    (("рубль", "доллар", "евро", "юань", "курс валют", "девальвац"),
     "Курс рубля определяет цены на импорт, технику, зарубежный отдых и покупки в валюте."),
    (("нефт", "газ", "нефтегаз", "opec", "опек", "баррел"),
     "Нефтегазовые доходы — основа бюджета России, от них зависят курс рубля и государственные расходы."),
    (("санкц", "эмбарго", "ограничени"),
     "Санкции ограничивают внешнюю торговлю и доступ к технологиям — это давит на рубль и внутренние цены."),
    (("ипотек", "кредит", "займ"),
     "Условия по кредитам и ипотеке определяют доступность жилья и долговую нагрузку семей."),
    (("налог", "ндс", "ндфл", "акциз"),
     "Изменение налогов напрямую затрагивает доходы граждан и цены на товары."),
    (("бюджет", "минфин", "дефицит", "профицит", "фнб"),
     "Состояние бюджета влияет на налоги, социальные выплаты и устойчивость рубля."),
    (("акци", "биржа", "индекс мосбирж", "дивиденд", "инвестор", "фондов"),
     "Динамика рынка отражает настроения инвесторов и влияет на сбережения и пенсионные накопления."),
    (("зарплат", "пенси", "пособи", "мрот", "прожиточн"),
     "Это напрямую касается доходов граждан и уровня жизни."),
    (("вклад", "депозит", "сбереж"),
     "От этого зависит доходность ваших вкладов и выгода хранения денег в рублях."),
]


def why_it_matters(text):
    """Определяет тему новости и возвращает объяснение её важности (без ИИ)."""
    low = (text or "").lower()
    for keys, explanation in WHY_RULES:
        if any(k in low for k in keys):
            return explanation
    return "Событие влияет на экономику России — курс рубля, цены и деловой климат."


def confidence_label(citation):
    if citation >= 10:
        return "🔴 Горячее"
    if citation >= 6:
        return "🟠 Важное"
    return "🟡 Новое"


GEMINI_MODEL = "gemini-2.0-flash"


def gemini_rewrite(cfg, cluster):
    """Оригинальный текст новости через Gemini на основе реальных фактов кластера.

    Строго по фактам источников; при любой ошибке — пустая строка (откат).
    """
    key = cfg.get("gemini_key")
    if not key:
        return ""
    title = html.unescape(cluster["best"]["title"]).strip()
    # факты: разные заголовки + лид-абзац, если есть
    seen, facts = [normalize(title)], []
    for it in cluster["items"]:
        nt = normalize(it["title"])
        if nt and not any(jaccard(nt, s) > 0.6 for s in seen):
            seen.append(nt)
            facts.append("- " + it["title"].strip())
        if len(facts) >= 6:
            break
    lead = clean_desc(cluster.get("body", ""))[:500]
    prompt = (
        "Ты — экономический обозреватель Telegram-канала «Эвномия». На основе фактов "
        "ниже напиши небольшую аналитическую заметку на русском языке "
        "(2 коротких абзаца, 5–8 предложений), своими словами, ёмко и без воды.\n\n"
        "Структура:\n"
        "1) Что произошло — по фактам источников.\n"
        "2) Контекст и причины — почему это происходит.\n"
        "3) Что это значит — последствия для рубля, цен, бизнеса или граждан.\n\n"
        "Требования: нейтральный деловой тон, простой понятный язык. Конкретные "
        "факты (цифры, даты, имена, цитаты) бери ТОЛЬКО из исходных данных — не "
        "выдумывай их. Контекст, причины и выводы можешь строить на общей "
        "экономической логике, но без ложных конкретных фактов. Раздели текст на "
        "абзацы пустой строкой. Без заголовка, без ссылок, без хэштегов, без "
        "markdown — только текст заметки.\n\n"
        f"Тема: {title}\n"
        "Сообщения источников:\n" + "\n".join(facts)
    )
    if lead:
        prompt += f"\n\nПодробности: {lead}"

    model = cfg.get("gemini_model", GEMINI_MODEL)
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 950,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        return re.sub(r"\s+\n", "\n", text)
    except Exception as e:
        log(f"Gemini: откат на пересказ ({e})")
        return ""


def build_post(cluster, compact=False):
    best = cluster["best"]
    title = html.unescape(best["title"]).strip().rstrip(".")
    link = best["url"]

    # хэштеги по ключевым темам
    tags = []
    low = title.lower()
    tagmap = {"рубль": "#рубль", "ставк": "#ставка_ЦБ", "инфляц": "#инфляция",
              "нефть": "#нефть", "санкц": "#санкции", "бюджет": "#бюджет",
              "доллар": "#доллар", "газ": "#газ", "акци": "#акции",
              "налог": "#налоги", "ипотек": "#ипотека", "фрс": "#ФРС",
              "сша": "#США", "китай": "#Китай", "ецб": "#ЕЦБ",
              "еврозон": "#еврозона", "биткоин": "#крипта"}
    for k, t in tagmap.items():
        if k in low and t not in tags:
            tags.append(t)
    tags.append("#экономика")

    # тело: приоритет — оригинальная ИИ-заметка (с абзацами), иначе лид-абзац источника
    ai = cluster.get("ai_body")
    if ai:
        body = cap_text(ai, max_chars=560 if compact else 1600)
    elif compact:
        body = trim_sentences(clean_desc(cluster.get("body", "")), max_sentences=7, max_chars=480)
    else:
        body = trim_sentences(clean_desc(cluster.get("body", "")))
    # не дублировать заголовок в теле (только для пересказа из источника)
    if not ai and body and normalize(body[:120]) and jaccard(normalize(body), normalize(title)) > 0.7:
        body = ""

    src_name = best.get("source_name") or "Источник"

    lines = [f"📊 <b>{html.escape(title)}</b>"]
    if body:
        lines += ["", html.escape(body)]
    lines += [
        "",
        f'🔗 <a href="{html.escape(link)}">{html.escape(src_name)}</a>',
        "",
        " ".join(tags[:5]),
    ]
    return "\n".join(lines)


def cbr_currency_series(val_code, days=30):
    """Динамика курса валюты из официального API ЦБ РФ. [(dd.mm, value), ...]."""
    end = datetime.now().date()
    start = end - timedelta(days=days)
    url = ("https://www.cbr.ru/scripts/XML_dynamic.asp"
           f"?date_req1={start:%d/%m/%Y}&date_req2={end:%d/%m/%Y}&VAL_NM_RQ={val_code}")
    raw = http_get(url)
    raw = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw)  # ET не любит decl в str
    root = ET.fromstring(raw)
    out = []
    for rec in root.iter("Record"):
        d = rec.get("Date", "")
        v = (rec.findtext("Value") or "").replace(",", ".")
        nom = (rec.findtext("Nominal") or "1").replace(",", ".")
        try:
            val = round(float(v) / float(nom), 2)
        except ValueError:
            continue
        out.append((d[:5], val))  # dd.mm
    return out


def quickchart_url(labels, values, title, color="#22C55E"):
    """URL картинки-графика через бесплатный QuickChart (Telegram сам её скачает)."""
    cfg = {
        "type": "line",
        "data": {"labels": labels, "datasets": [{
            "label": title, "data": values, "borderColor": color,
            "backgroundColor": color, "fill": False, "borderWidth": 2,
            "pointRadius": 0, "tension": 0.3}]},
        "options": {
            "title": {"display": True, "text": title, "fontSize": 15},
            "legend": {"display": False},
            "scales": {"xAxes": [{"ticks": {"maxTicksLimit": 6, "fontSize": 10}}],
                       "yAxes": [{"ticks": {"fontSize": 10}}]}},
    }
    return ("https://quickchart.io/chart?w=520&h=300&bkg=white&c="
            + urllib.parse.quote(json.dumps(cfg, ensure_ascii=False)))


def chart_for(cluster):
    """Если пост про курс валюты — вернуть URL мини-графика, иначе None."""
    low = (cluster["best"]["title"] + " " + cluster.get("body", "")).lower()
    is_rate = "курс" in low or "цб установил" in low or "официальный курс" in low
    if not is_rate:
        return None
    if "евро" in low and "доллар" not in low:
        code, title = "R01239", "Курс евро ЦБ РФ, 30 дней (₽)"
    else:
        code, title = "R01235", "Курс доллара ЦБ РФ, 30 дней (₽)"
    try:
        series = cbr_currency_series(code)
        if len(series) < 5:
            return None
        labels = [d for d, _ in series]
        values = [v for _, v in series]
        return quickchart_url(labels, values, title)
    except Exception as e:
        log(f"chart_for: не удалось построить график ({e})")
        return None


def send_telegram_photo(token, channel_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = urllib.parse.urlencode({
        "chat_id": channel_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if not resp.get("ok"):
        raise RuntimeError(resp)
    return resp


def seed_reaction(token, channel_id, message_id, emoji="👍"):
    """Ставит стартовую реакцию на пост, чтобы полоска голосования была видна."""
    url = f"https://api.telegram.org/bot{token}/setMessageReaction"
    payload = urllib.parse.urlencode({
        "chat_id": channel_id, "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"Реакция не поставлена: {e}")


COMMONS_TOPICS = [
    (("фрс",), "federal reserve building washington"),
    (("ецб", "еврозон"), "european central bank frankfurt"),
    (("рубль", "рубл"), "russian ruble banknotes"),
    (("доллар",), "one dollar bill washington"),
    (("евро",), "euro banknotes money"),
    (("нефть", "brent", "баррел", "нефтегаз"), "oil pump jack"),
    (("газ", "газпром"), "natural gas pipeline"),
    (("ставк", "цб", "центробанк", "дкп", "набиуллин"), "central bank of russia building"),
    (("инфляц", "цены", "подорожа"), "supermarket shelves groceries"),
    (("санкц",), "european union flags brussels"),
    (("китай", "юань"), "shanghai skyline finance"),
    (("биржа", "акци", "фондов", "индекс", "рынок", "уолл"), "stock exchange trading floor"),
    (("бюджет", "минфин", "налог"), "russian ruble coins"),
    (("биткоин", "крипт"), "bitcoin cryptocurrency"),
    (("ипотек", "квартир", "жиль"), "apartment buildings city"),
]
DEFAULT_IMG_QUERY = "stock market chart finance"
USED_IMAGES_PATH = os.path.join(BASE_DIR, "used_images.json")
_candidates_cache = {}


def commons_images(query, limit=25):
    """Список прямых URL тематических картинок из Wikimedia Commons."""
    url = ("https://commons.wikimedia.org/w/api.php?action=query&generator=search"
           f"&gsrnamespace=6&gsrlimit={limit}&prop=imageinfo&iiprop=url&iiurlwidth=800"
           "&format=json&gsrsearch=" + urllib.parse.quote(query))
    out = []
    try:
        d = json.loads(http_get(url))
        pages = d.get("query", {}).get("pages", {})
        # сортируем по индексу поиска (релевантность)
        for p in sorted(pages.values(), key=lambda x: x.get("index", 999)):
            ii = (p.get("imageinfo") or [{}])[0]
            u = ii.get("thumburl") or ii.get("url") or ""
            low = u.lower()
            bad = ("icon", "logo", "thumbnail", "map_marker", "favicon",
                   "emblem", "diagram", "chart", "graph")
            if re.search(r"\.(jpg|jpeg|png)", low) and not any(b in low for b in bad):
                out.append(u)
    except Exception as e:
        log(f"Commons: {e}")
    return out


def topic_image(text, used):
    """Тематическая картинка, всегда новая: ротация кандидатов, без недавних повторов."""
    low = (text or "").lower()
    q = DEFAULT_IMG_QUERY
    for keys, query in COMMONS_TOPICS:
        if any(k in low for k in keys):
            q = query
            break
    if q not in _candidates_cache:
        _candidates_cache[q] = commons_images(q)
    cands = _candidates_cache[q]
    if not cands:
        return ""
    fresh = [u for u in cands if u not in used]
    pick = fresh[0] if fresh else random.choice(cands)
    used.append(pick)
    return pick


def send_telegram(token, channel_id, text, preview_url=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if preview_url:
        lpo = {"url": preview_url, "prefer_large_media": True,
               "show_above_text": False, "is_disabled": False}
    else:
        lpo = {"is_disabled": True}
    payload = urllib.parse.urlencode({
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps(lpo),
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if not resp.get("ok"):
        raise RuntimeError(resp)
    return resp


# ---------------------------------------------------------------------------
# Главный проход
# ---------------------------------------------------------------------------

def gather_clusters(cfg):
    """Собрать все источники и сгруппировать в сюжеты."""
    kw = cfg["keywords"]
    items = []
    items += fetch_outlet_rss(cfg.get("outlet_rss_feeds", {}), kw)
    items += fetch_google_news(cfg["google_news_queries"], kw)
    items += fetch_gdelt(kw)
    items += fetch_telegram(cfg["telegram_channels"], kw)
    log(f"Всего собрано сообщений: {len(items)}")
    if not items:
        return []
    clusters = cluster_items(items)
    log(f"Сюжетов после кластеризации: {len(clusters)}")
    return clusters


def run_once(cfg, dry_run=False):
    clusters = gather_clusters(cfg)
    if not clusters:
        log("Нет данных — пропуск.")
        return

    history = load_history()
    used_images = []
    if os.path.exists(USED_IMAGES_PATH):
        try:
            with open(USED_IMAGES_PATH, "r", encoding="utf-8") as f:
                used_images = json.load(f)
        except Exception:
            used_images = []
    threshold = cfg["citation_threshold"]
    posted = 0
    for cl in clusters:
        if posted >= cfg["max_posts_per_run"]:
            break
        if cl["citation"] < threshold:
            continue
        if already_posted(history, cl["tokens"]):
            continue
        ai_body = gemini_rewrite(cfg, cl)
        if ai_body:
            cl["ai_body"] = ai_body
        chart = chart_for(cl)
        text = build_post(cl, compact=bool(chart))
        if dry_run:
            log(f"[DRY-RUN] цит={cl['citation']} график={'да' if chart else 'нет'} "
                f"источники={cl['sources'][:5]}")
            print("-" * 60 + "\n" + text + "\n" + "-" * 60)
        else:
            try:
                if chart:
                    resp = send_telegram_photo(cfg["bot_token"], cfg["channel_id"], chart, text)
                else:
                    # прямая картинка Wikimedia показывается надёжно (превью статьи — нет)
                    preview = topic_image(
                        cl["best"]["title"] + " " + cl.get("ai_body", ""), used_images)
                    resp = send_telegram(cfg["bot_token"], cfg["channel_id"], text,
                                         preview_url=preview)
                mid = resp.get("result", {}).get("message_id")
                if mid:
                    seed_reaction(cfg["bot_token"], cfg["channel_id"], mid)
                log(f"Опубликовано (цит.={cl['citation']}"
                    f"{', +график' if chart else ''}): {cl['best']['title'][:60]}")
            except Exception as e:
                log(f"Ошибка отправки: {e}")
                continue
        if not dry_run:
            mark_posted(history, cl["tokens"], cl["best"]["title"])
        posted += 1
        time.sleep(3)  # не упираться в лимиты Telegram

    if not dry_run and posted:
        save_history(history)
        with open(USED_IMAGES_PATH, "w", encoding="utf-8") as f:
            json.dump(used_images[-120:], f, ensure_ascii=False)
    log(f"Готово. Опубликовано в этот проход: {posted}")


# ---------------------------------------------------------------------------
# Дайджесты (утро / вечер)
# ---------------------------------------------------------------------------

CURRENCIES = [("💵 Доллар", "R01235"), ("💶 Евро", "R01239"), ("🇨🇳 Юань", "R01375")]


def rate_line(name, code):
    """Строка курса с изменением за день: '💵 Доллар: 78.27 ₽ 🔺0.52'."""
    try:
        s = cbr_currency_series(code, days=10)
    except Exception:
        return None
    if not s:
        return None
    val = s[-1][1]
    arrow = ""
    if len(s) >= 2:
        d = round(val - s[-2][1], 2)
        mark = "🔺" if d > 0 else "🔻" if d < 0 else "▪️"
        arrow = f" {mark}{abs(d):.2f}"
    return f"{name}: <b>{val:.2f} ₽</b>{arrow}"


def key_rate():
    """Текущая ключевая ставка ЦБ (best-effort парсинг), либо None."""
    try:
        page = http_get("https://www.cbr.ru/hd_base/KeyRate/")
    except Exception:
        return None
    m = re.search(r"\d{2}\.\d{2}\.\d{4}\s*</td>\s*<td[^>]*>\s*(\d+,\d+)", page)
    return m.group(1) if m else None


def top_stories(cfg, n=3, min_cit=2):
    clusters = gather_clusters(cfg)
    return [c for c in clusters if c["citation"] >= min_cit][:n]


def _story_lines(top):
    out = []
    for i, c in enumerate(top, 1):
        t = html.unescape(c["best"]["title"]).strip().rstrip(".")
        out.append(f"{i}. {html.escape(t)}")
    return out


def send_digest_morning(cfg, dry_run=False):
    top = top_stories(cfg, 3)
    if not top:
        log("Утренний дайджест: нет сюжетов — пропуск.")
        return
    lines = ["☀️ <b>Главное за ночь</b>", ""] + _story_lines(top)
    lines += ["", "#дайджест #экономика"]
    text = "\n".join(lines)
    if dry_run:
        print(text)
    else:
        send_telegram(cfg["bot_token"], cfg["channel_id"], text)
        log("Утренний дайджест отправлен.")


def send_digest_evening(cfg, dry_run=False):
    lines = ["🌙 <b>Итоги дня</b>", "", "<b>Курсы ЦБ РФ:</b>"]
    rates = [rate_line(n, c) for n, c in CURRENCIES]
    lines += [r for r in rates if r]
    kr = key_rate()
    if kr:
        lines += ["", f"🏦 <b>Ключевая ставка ЦБ:</b> {kr}%"]
    top = top_stories(cfg, 3)
    if top:
        lines += ["", "<b>Главное за день:</b>"] + _story_lines(top)
    lines += ["", "#итоги_дня #экономика"]
    text = "\n".join(lines)
    if dry_run:
        print(text)
    else:
        send_telegram(cfg["bot_token"], cfg["channel_id"], text)
        log("Вечерний дайджест отправлен.")


def main():
    cfg = load_config()
    dry = "--dry-run" in sys.argv

    # режим: env MODE (облако) или аргумент (локально): morning | evening | news
    mode = os.environ.get("MODE", "news").lower()
    argv = " ".join(sys.argv[1:])
    if "morning" in argv:
        mode = "morning"
    elif "evening" in argv:
        mode = "evening"

    if mode == "morning":
        send_digest_morning(cfg, dry_run=dry)
        return
    if mode == "evening":
        send_digest_evening(cfg, dry_run=dry)
        return

    if "--loop" in sys.argv:
        while True:
            try:
                run_once(cfg, dry_run=dry)
            except Exception as e:
                log(f"Сбой прохода: {e}")
            sleep_s = int(cfg.get("interval_hours", 2) * 3600)
            log(f"Пауза {sleep_s // 60} мин...")
            time.sleep(sleep_s)
    else:
        run_once(cfg, dry_run=dry)


if __name__ == "__main__":
    main()
