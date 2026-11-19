#!/usr/bin/env python3
"""Радар медиа: забирает свежее из sources.txt, модель оценивает по anketa.md,
одинаковые поводы склеиваются, результат – data/items.json для дашборда.

Запуск один раз:  python3 radar.py
Ключ: OPENROUTER_API_KEY или ANTHROPIC_API_KEY в .env рядом.
"""
from __future__ import annotations

import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
STORE = DATA / "items.json"
STATUS = DATA / "status.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) media-radar/1.0"
PER_SOURCE = 15          # сколько самых свежих материалов брать из источника
KEEP_DAYS = 7            # сколько дней держать материалы в базе
TOPICS = ["персоны и скандалы", "медиа и культура", "бизнес и тренды", "другое"]
TIERS = ["редакция", "таблоид", "платформа", "поиск"]
QUALITY_DEFAULT = HERE / "QUALITY.md"  # свой путь – RADAR_QUALITY в .env (например, заметка в Obsidian)


def quality_rules() -> str:
    """Раздел «Инструкция для модели» из заметки в Obsidian; без заметки – короткая запасная версия."""
    path = Path(os.environ.get("RADAR_QUALITY") or QUALITY_DEFAULT)
    try:
        t = path.read_text(encoding="utf-8")
        m = re.search(r"^## Инструкция для модели\s*\n(.*?)(?=^## |\Z)", t, re.S | re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except OSError:
        pass
    return ("Достоверность (cred) 0–3: редакция – 2, таблоид и платформа – 1; +1 за названных людей, документы, "
            "заявление героя; −1 за анонимные источники, слух, сенсационный заголовок. cred_why – до 10 слов.")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------- источники ----------------------------------------------------------------

def gnews(query: str, lang: str) -> str:
    hl = "hl=ru&gl=RU&ceid=RU:ru" if lang == "ru" else "hl=en-US&gl=US&ceid=US:en"
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&" + hl


def read_sources(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 3:
            continue
        lang, name, addr = parts[0].lower(), parts[1], parts[2]
        hours = next((int(x) for x in parts[3:] if x.isdigit()), 48)
        tier = next((x for x in parts[3:] if x in TIERS), "поиск" if addr.startswith("news:") else "редакция")
        if addr.startswith("news:"):
            out.append({"kind": "rss", "gn": True, "lang": lang, "name": name, "hours": hours, "tier": tier,
                        "url": gnews(addr[5:].strip(), lang)})
        elif "t.me/" in addr or addr.startswith("@"):
            nick = re.sub(r"^.*t\.me/(s/)?|^@", "", addr).strip("/")
            out.append({"kind": "tg", "lang": lang, "name": name, "hours": hours, "tier": tier, "url": f"https://t.me/s/{nick}"})
        else:
            out.append({"kind": "rss", "lang": lang, "name": name, "hours": hours, "tier": tier, "url": addr})
    return out


def get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


class TgParser(HTMLParser):
    """Открытая страница t.me/s/канал: посты, текст, дата, первая картинка."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self.depth = 0
        self.post: dict | None = None
        self.post_depth = 0
        self.text_depth = 0

    def handle_starttag(self, tag, attrs_list):
        a = dict(attrs_list)
        cls = set((a.get("class") or "").split())
        if tag == "div":
            self.depth += 1
            if "tgme_widget_message" in cls and a.get("data-post"):
                self.post = {"id": a["data-post"], "text": [], "date": None, "img": None}
                self.post_depth = self.depth
            elif self.post is not None and "js-message_text" in cls and not self.text_depth:
                self.text_depth = self.depth
        if self.post is not None:
            if tag == "time" and a.get("datetime") and not self.post["date"]:
                self.post["date"] = a["datetime"]
            if tag == "br" and self.text_depth:
                self.post["text"].append("\n")
            st = a.get("style") or ""
            if tag == "a" and "tgme_widget_message_photo_wrap" in cls and not self.post["img"]:
                m = re.search(r"url\('([^']+)'\)", st)
                if m:
                    self.post["img"] = m.group(1)

    def handle_startendtag(self, tag, attrs_list):
        if tag != "div":
            self.handle_starttag(tag, attrs_list)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.text_depth and self.depth == self.text_depth:
            self.text_depth = 0
        if self.post is not None and self.depth == self.post_depth:
            self.posts.append(self.post)
            self.post = None
            self.text_depth = 0
        self.depth -= 1

    def handle_data(self, data):
        if self.post is not None and self.text_depth:
            self.post["text"].append(data)


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def fetch(src: dict) -> list[dict]:
    items = []
    if src["kind"] == "tg":
        p = TgParser()
        p.feed(get(src["url"]))
        for post in p.posts:
            text = "".join(post["text"]).strip()
            if text:
                items.append({"link": f"https://t.me/{post['id']}", "date": post["date"], "text": text[:900],
                              "outlet": src["name"], "img": post["img"]})
        return items
    root = ET.fromstring(get(src["url"]).encode("utf-8"))
    ns = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
    for it in root.findall("channel/item"):
        title = strip_tags(it.findtext("title") or "")
        outlet = (it.findtext("source") or "").strip() if src.get("gn") else src["name"]
        if src.get("gn") and outlet and title.endswith(" - " + outlet):
            title = title[: -len(outlet) - 3]
        desc = "" if src.get("gn") else strip_tags(it.findtext("description") or "")[:600]
        img = None
        m = it.find("media:content", ns)
        if m is None:
            m = it.find("media:thumbnail", ns)
        if m is not None and m.get("url"):
            img = m.get("url")
        enc = it.find("enclosure")
        if not img and enc is not None and (enc.get("type") or "").startswith("image"):
            img = enc.get("url")
        items.append({"link": (it.findtext("link") or "").strip(), "date": it.findtext("pubDate"),
                      "text": title + ("\n" + desc if desc else ""), "outlet": outlet or src["name"], "img": img})
    for it in root.findall("a:entry", ns):
        link = it.find("a:link", ns)
        items.append({"link": link.get("href") if link is not None else "",
                      "date": it.findtext("a:updated", namespaces=ns) or it.findtext("a:published", namespaces=ns),
                      "text": strip_tags(it.findtext("a:title", namespaces=ns) or "") + "\n"
                      + strip_tags(it.findtext("a:summary", namespaces=ns) or "")[:600],
                      "outlet": src["name"], "img": None})
    return items


def to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------- модель -------------------------------------------------------------------

PROMPT = """Ты фильтр моего радара инфоповодов. Анкета:

{anketa}

Ниже свежие материалы на русском и английском, у каждого номер.
Для каждого верни:
- score 0–3: насколько это яркий инфоповод по анкете (3 – громкая история, которую обсуждают сегодня; 2 – хороший повод; 1 – фон; 0 – не моё или реклама)
- topic: ровно одно из «персоны и скандалы», «медиа и культура», «бизнес и тренды», «другое»
- title: суть повода по-русски, до 12 слов, с именами людей и компаний; английское переведи
- why: почему это повод для меня, до 14 слов по-русски
- cred 0–3 и cred_why: достоверность по правилам ниже

Правила оценки качества:
{quality}

Ответ – только JSON-массив без пояснений: [{{"i": 1, "score": 2, "topic": "…", "title": "…", "why": "…", "cred": 2, "cred_why": "…"}}]

Материалы:
{posts}"""

GLUE = """Ниже заголовки инфоповодов, у каждого номер. Найди группы, где два и больше заголовка про одно и то же событие
(одни и те же люди и один и тот же случай, язык может отличаться). Похожая тема без общего события – не группа.
Ответ – только JSON-массив групп номеров, например [[3, 17], [5, 8, 21]]. Нет групп – [].

{lines}"""


def call_model(prompt: str, max_tokens: int = 6000) -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        body = {"model": os.environ.get("RADAR_MODEL", "anthropic/claude-haiku-5.5"), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                                              "content-type": "application/json", "X-Title": "media-radar"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)["choices"][0]["message"]["content"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        body = {"model": os.environ.get("RADAR_MODEL", "claude-haiku-5-5"), "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                     headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                              "anthropic-version": "2023-06-01", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)["content"][0]["text"]
    raise SystemExit("нет ключа: OPENROUTER_API_KEY или ANTHROPIC_API_KEY в .env")


def parse_json(out: str, open_ch: str = "[", close_ch: str = "]"):
    a, b = out.find(open_ch), out.rfind(close_ch)
    try:
        return json.loads(out[a:b + 1]) if a >= 0 and b > a else None
    except json.JSONDecodeError:
        return None


def score_batch(part: list[dict], anketa: str, retry: bool = True) -> None:
    quality = quality_rules()
    """Пачка без ответа делится пополам и уходит ещё раз; неоценённое не сохраняется и придёт в следующий сбор."""
    lines = [f"{i}. [{it['lang']} · {it['outlet']} · {it.get('tier', 'редакция')}] {' '.join(it['text'].split())[:420]}"
             for i, it in enumerate(part, 1)]
    try:
        data = parse_json(call_model(PROMPT.format(anketa=anketa, quality=quality, posts="\n".join(lines)))) or []
    except Exception as e:
        print(f"! модель: {type(e).__name__}: {e}", file=sys.stderr)
        data = []
    if not data and retry and len(part) > 1:
        h = len(part) // 2
        score_batch(part[:h], anketa, False)
        score_batch(part[h:], anketa, False)
        return
    for d in data:
        try:
            k = int(d.get("i", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= k < len(part):
            topic = d.get("topic", "другое")
            part[k].update(score=max(0, min(3, int(d.get("score", 0) or 0))),
                           topic=topic if topic in TOPICS else "другое",
                           title=(d.get("title") or "").replace("\u2014", "\u2013"),
                           why=(d.get("why") or "").replace("\u2014", "\u2013"),
                           cred_base=max(0, min(3, int(d.get("cred", 1) or 0))),
                           cred_why=(d.get("cred_why") or "").replace("\u2014", "\u2013"))


def glue(items: list[dict]) -> int:
    """Склейка: одинаковые события из разных источников – одна карточка, остальные в «ещё»."""
    for it in items:
        it.pop("dup_of", None)
        it["corr"] = 1
        it["cred"] = it.get("cred_base", 1)
    cand = sorted([it for it in items if it.get("score", 0) >= 2], key=lambda x: x["date"], reverse=True)[:120]
    if len(cand) < 2:
        return 0
    lines = "\n".join(f"{i}. {it.get('title') or it['text'][:120]}" for i, it in enumerate(cand, 1))
    groups = parse_json(call_model(GLUE.format(lines=lines), max_tokens=1500)) or []
    n = 0
    for g in groups:
        try:
            mem = [cand[int(x) - 1] for x in g if 0 < int(x) <= len(cand)]
        except (TypeError, ValueError):
            continue
        if len(mem) < 2:
            continue
        head = max(mem, key=lambda x: (x.get("score", 0), x["date"]))
        outlets = len({m["outlet"].lower() for m in mem})
        if outlets >= 2:  # SIFT: другое освещение того же события – +1 к достоверности
            for m in mem:
                m["corr"] = outlets
                m["cred"] = min(3, m.get("cred_base", 1) + 1)
        for m in mem:
            if m is not head:
                m["dup_of"] = head["link"]
                n += 1
    return n


# ---------- сбор ---------------------------------------------------------------------

def write_status(**kw) -> None:
    cur = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    cur.update(kw)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATUS)


def run() -> dict:
    load_env(HERE / ".env")
    DATA.mkdir(exist_ok=True)
    t0 = time.time()
    now = datetime.now(timezone.utc)
    write_status(running=True, started=now.isoformat(), error=None)
    store: dict = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {}
    sources = read_sources(HERE / "sources.txt")
    anketa = (HERE / "anketa.md").read_text(encoding="utf-8")

    def one(src):
        try:
            got = fetch(src)
        except Exception as e:  # источник упал – сбор идёт дальше
            return src, [], f"{type(e).__name__}: {str(e)[:80]}"
        fresh = []
        for it in got:
            d = to_dt(it["date"])
            if d and now - d <= timedelta(hours=src["hours"]) and it["link"]:
                it.update(date=d.isoformat(), lang=src["lang"], source=src["name"], tier=src["tier"])
                fresh.append(it)
        fresh.sort(key=lambda x: x["date"], reverse=True)
        return src, fresh[:PER_SOURCE], None

    stats, new = [], []
    with cf.ThreadPoolExecutor(10) as ex:
        for src, fresh, err in ex.map(one, sources):
            add = [it for it in fresh if it["link"] not in store]
            stats.append({"name": src["name"], "lang": src["lang"], "fresh": len(fresh), "new": len(add), "error": err})
            new += add
    seen_links: set = set()
    new = [it for it in new if not (it["link"] in seen_links or seen_links.add(it["link"]))]
    # материалы без оценки достоверности (собраны до её появления) уходят в модель ещё раз
    tier_of = {s["name"]: s["tier"] for s in sources}
    old = [v for v in store.values() if "cred_base" not in v and v.get("text") and v["link"] not in seen_links]
    for v in old:
        v.setdefault("tier", tier_of.get(v.get("source"), "поиск"))
        for k in ("score", "topic", "title", "why"):
            v.pop(k, None)
    new += old

    batches = [new[i:i + 20] for i in range(0, len(new), 20)]
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda b: score_batch(b, anketa), batches))
    unscored = [it for it in new if "score" not in it]
    new = [it for it in new if "score" in it]
    for it in new:
        it["seen"] = now.isoformat()
        store[it["link"]] = it

    cutoff = now - timedelta(days=KEEP_DAYS)
    store = {k: v for k, v in store.items() if (to_dt(v["date"]) or now) >= cutoff}
    recent = [v for v in store.values() if now - (to_dt(v["date"]) or now) <= timedelta(hours=72)]
    for v in store.values():
        v.setdefault("cred", v.get("cred_base", 1))
    glued = glue(recent) if new else sum(1 for v in recent if v.get("dup_of"))

    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE)
    res = {"running": False, "finished": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0),
           "new": len(new), "unscored": len(unscored), "total": len(store), "glued": glued, "sources": stats, "error": None}
    write_status(**res)
    return res


if __name__ == "__main__":
    try:
        r = run()
    except BaseException as e:
        write_status(running=False, error=f"{type(e).__name__}: {e}")
        raise
    print(f"новых {r['new']}, всего {r['total']}, склеено {r['glued']}, {r['seconds']} с")
    for s in r["sources"]:
        print(f"  {s['lang']} {s['name'][:30]:30s} свежих {s['fresh']:3d} новых {s['new']:3d} {s['error'] or ''}")
