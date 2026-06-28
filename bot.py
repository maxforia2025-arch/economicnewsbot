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
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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
    if (not token or not channel) and os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            sec = json.load(f)
        token = token or sec.get("bot_token")
        channel = channel or sec.get("channel_id")
    cfg["bot_token"] = token or cfg.get("bot_token", "")
    cfg["channel_id"] = channel or cfg.get("channel_id", "")
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


def trim_sentences(text, max_sentences=15, max_chars=750):
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
            items.append({"title": title, "url": u, "desc": "",
                          "source": art.get("domain") or domain_of(u)})
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
                              "desc": desc, "source": source.lower()})
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
                              "source": (source or "googlenews").strip().lower()})
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
                              "source": f"tg:{ch}"})
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
        if len(picks) >= 3:
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

def confidence_label(citation):
    if citation >= 10:
        return "🔴 Горячее · подтверждено многими источниками"
    if citation >= 6:
        return "🟠 Важное · хорошо подтверждено"
    return "🟡 Новое · подтверждается"


def build_post(cluster):
    best = cluster["best"]
    title = html.unescape(best["title"]).strip().rstrip(".")
    link = best["url"]
    label = confidence_label(cluster["citation"])

    # хэштеги по ключевым темам
    tags = []
    low = title.lower()
    tagmap = {"рубль": "#рубль", "ставк": "#ставка_ЦБ", "инфляц": "#инфляция",
              "нефть": "#нефть", "санкц": "#санкции", "бюджет": "#бюджет",
              "доллар": "#доллар", "газ": "#газ", "акци": "#акции",
              "налог": "#налоги", "ипотек": "#ипотека"}
    for k, t in tagmap.items():
        if k in low and t not in tags:
            tags.append(t)
    tags.append("#экономика")

    # пояснение: лид-абзац статьи, до 15 предложений
    body = trim_sentences(clean_desc(cluster.get("body", "")))
    # не дублировать заголовок в теле
    if body and normalize(body[:120]) and jaccard(normalize(body), normalize(title)) > 0.7:
        body = ""

    lines = [f"📊 <b>{html.escape(title)}</b>"]
    if body:
        lines += ["", html.escape(body)]
    lines += [
        "",
        f"{label} ({cluster['citation']} ист.)",
        f'🔗 <a href="{html.escape(link)}">Источник</a>',
        "",
        " ".join(tags[:5]),
    ]
    return "\n".join(lines)


def send_telegram(token, channel_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
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

def run_once(cfg, dry_run=False):
    kw = cfg["keywords"]
    items = []
    items += fetch_outlet_rss(cfg.get("outlet_rss_feeds", {}), kw)
    items += fetch_google_news(cfg["google_news_queries"], kw)
    items += fetch_gdelt(kw)
    items += fetch_telegram(cfg["telegram_channels"], kw)
    log(f"Всего собрано сообщений: {len(items)}")

    if not items:
        log("Нет данных — пропуск.")
        return

    clusters = cluster_items(items)
    log(f"Сюжетов после кластеризации: {len(clusters)}")

    history = load_history()
    threshold = cfg["citation_threshold"]
    posted = 0
    for cl in clusters:
        if posted >= cfg["max_posts_per_run"]:
            break
        if cl["citation"] < threshold:
            continue
        if already_posted(history, cl["tokens"]):
            continue
        text = build_post(cl)
        if dry_run:
            log(f"[DRY-RUN] цитируемость={cl['citation']} "
                f"источники={cl['sources'][:6]}")
            print("-" * 60 + "\n" + text + "\n" + "-" * 60)
        else:
            try:
                send_telegram(cfg["bot_token"], cfg["channel_id"], text)
                log(f"Опубликовано (цит.={cl['citation']}): {cl['best']['title'][:70]}")
            except Exception as e:
                log(f"Ошибка отправки: {e}")
                continue
        if not dry_run:
            mark_posted(history, cl["tokens"], cl["best"]["title"])
        posted += 1
        time.sleep(3)  # не упираться в лимиты Telegram

    if not dry_run and posted:
        save_history(history)
    log(f"Готово. Опубликовано в этот проход: {posted}")


def main():
    cfg = load_config()
    dry = "--dry-run" in sys.argv
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
