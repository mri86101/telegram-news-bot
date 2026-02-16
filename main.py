import os
import re
import json
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from dateutil import parser as dtparser

import feedparser
from langdetect import detect, LangDetectException
import urllib.request


KST = timezone(timedelta(hours=9))
SEEN_PATH = "seen.json"

QUERIES = [
    'NextBiomedical OR "Next Biomedical" OR "Nextbiomedical" OR 넥스트바이오메디컬',
    'nexpowder OR "Nex Powder" OR 넥스파우더',
    '"nexsphere f" OR "Nexsphere-f" OR "Nexsphere F" OR 넥스피어F OR 넥스피어 F',
]

KEEP_DAYS = 45
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "20"))


def google_news_rss_url(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"#.*$", "", url)
    return url


def normalize_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"\([^\)]*\)", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_keys(url: str, title: str) -> tuple[str, str]:
    url_key = sha("url::" + normalize_url(url)) if url else ""
    title_key = sha("title::" + normalize_title(title)) if title else ""
    return url_key, title_key


def load_seen() -> list[dict]:
    if not os.path.exists(SEEN_PATH):
        return []
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items", [])


def save_seen(items: list[dict]):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)


def prune_seen(items: list[dict], now: datetime) -> list[dict]:
    cutoff = now - timedelta(days=KEEP_DAYS)
    kept = []
    for it in items:
        ts = it.get("ts")
        if not ts:
            continue
        try:
            dt = dtparser.parse(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(KST)
            if dt >= cutoff:
                kept.append(it)
        except Exception:
            continue
    return kept


def is_korean(text: str) -> bool:
    text = text or ""
    if re.search(r"[가-힣]", text):
        return True
    try:
        return detect(text) == "ko"
    except LangDetectException:
        return False


def parse_published(entry) -> datetime | None:
    for key in ("published", "updated"):
        val = getattr(entry, key, None)
        if not val:
            continue
        try:
            dt = dtparser.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(KST)
        except Exception:
            pass
    return None


def telegram_send_message(token: str, chat_id: str, text: str):
    import urllib.parse
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print("[TELEGRAM ERROR]", e.code, body)
        raise


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    now = datetime.now(KST)
    cutoff_12h = now - timedelta(hours=12)

    seen_items = prune_seen(load_seen(), now)
    seen_url_keys = {it.get("url_key") for it in seen_items if it.get("url_key")}
    seen_title_keys = {it.get("title_key") for it in seen_items if it.get("title_key")}

    batch_url_keys = set()
    batch_title_keys = set()

    collected = []
    for q in QUERIES:
        feed = feedparser.parse(google_news_rss_url(q))
        for e in getattr(feed, "entries", []):
            title = getattr(e, "title", "").strip()
            url = getattr(e, "link", "").strip()
            summary = getattr(e, "summary", "")

            published_dt = parse_published(e)
            if not published_dt or published_dt < cutoff_12h:
                continue

            lang_text = f"{title}\n{re.sub('<[^<]+?>', ' ', summary)}".strip()
            if is_korean(lang_text):
                continue

            url_key, title_key = make_keys(url, title)

            if url_key and url_key in batch_url_keys:
                continue
            if title_key and title_key in batch_title_keys:
                continue

            if url_key and url_key in seen_url_keys:
                continue
            if title_key and title_key in seen_title_keys:
                continue

            batch_url_keys.add(url_key)
            batch_title_keys.add(title_key)

            collected.append({
                "title": title,
                "url": url,
                "published": published_dt,
                "url_key": url_key,
                "title_key": title_key,
            })

    if not collected:
        print("[DEBUG] No articles found in last 12 hours.")
        telegram_send_message(
            token,
            chat_id,
            "🗞️ NextBiomedical / Nexpowder / Nexsphere F\n최근 12시간 내 새 해외 뉴스가 없습니다."
        )
        return

    collected.sort(key=lambda x: x["published"], reverse=True)
    collected = collected[:MAX_ITEMS]

    lines = [f"🗞️ *NextBiomedical / Nexpowder / Nexsphere F* (last 12h, deduped)\n"]
    for i, it in enumerate(collected, 1):
        t = it["published"].strftime("%m-%d %H:%M KST")
        lines.append(f"{i}) {it['title']}\n{t}\n{it['url']}\n")

    message = "\n".join(lines).strip()
    telegram_send_message(token, chat_id, message)

    for it in collected:
        seen_items.append({
            "ts": now.isoformat(),
            "url_key": it["url_key"],
            "title_key": it["title_key"],
            "url": it["url"],
            "title": it["title"],
        })

    save_seen(seen_items)


if __name__ == "__main__":
    main()
