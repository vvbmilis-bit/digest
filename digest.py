import html
import json
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)

NOW = datetime.now(timezone(timedelta(hours=5)))  # время Казахстана
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
DATE_STR = f"{NOW.day} {MONTHS[NOW.month - 1]} {NOW.year}"

# ---------- Курсы ----------
def get_rates():
    url = f"https://nationalbank.kz/rss/get_rates.cfm?fdate={NOW:%d.%m.%Y}"
    with urllib.request.urlopen(url, timeout=15) as r:
        root = ET.fromstring(r.read())
    result = {}
    for item in root.iter("item"):
        code = item.findtext("title")
        if code in ("USD", "RUB", "CNY"):
            rate = float(item.findtext("description"))
            quant = float(item.findtext("quant") or 1)
            result[code] = rate / quant  # тенге за 1 единицу
    return result

# ---------- Новости ----------
def read_feed(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        items.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "text": (title + " " + (item.findtext("description") or "")).lower(),
        })
        if len(items) >= limit:
            break
    return items

def read_many(urls, limit):
    items = []
    for url in urls:
        try:
            items += read_feed(url, limit)
        except Exception as e:
            print(f"[!] Не удалось прочитать {url}: {e}")
    return items

def matches(text, words):
    return any(re.search(r"\b" + w, text) for w in words)

def kazakhstan(cfg):
    items = read_many(cfg["kz_feeds"], 60)
    n = cfg["news_per_block"]
    used = set()

    def pick(words):
        out = []
        for it in items:
            if it["link"] in used:
                continue
            if matches(it["text"], words):
                out.append(it)
                used.add(it["link"])
            if len(out) >= n:
                break
        return out

    finance = pick(cfg["finance_words"])
    border = pick(cfg["border_words"])
    general = [it for it in items if it["link"] not in used][:n]
    return finance, border, general

# ---------- Пересказ через ИИ (Google Gemini, бесплатный уровень) ----------
def summarize(blocks):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not CFG.get("ai_summary", True):
        return {}
    lines = []
    for name, items in blocks.items():
        if items:
            lines.append(f"[{name}]")
            lines += [f"- {it['title']}" for it in items]
    if not lines:
        return {}
    prompt = (
        "Ты помощник, который пишет утренний дайджест на русском языке. "
        "Ниже заголовки новостей по блокам: finance (законы и финансы), "
        "border (границы и въезд), kz (Казахстан), ai (искусственный интеллект). "
        "Для каждого блока напиши 1-2 коротких предложения: о чём главное. "
        "Опирайся ТОЛЬКО на заголовки, ничего не выдумывай и не добавляй цифр "
        "или деталей, которых в них нет. Блоки без новостей пропусти. "
        "Поле overview: 2-3 предложения о главном за утро. "
        'Ответь только JSON вида {"overview": "...", "finance": "...", '
        '"border": "...", "kz": "...", "ai": "..."}.'
        "\n\n" + "\n".join(lines)
    )
    model = CFG.get("model", "gemini-3.8-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"x-goog-api-key": key,
                 "Content-Type": "application/json",
                 "User-Agent": "digest-bot"})

    data = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 3:
                wait = 20 * (attempt + 1)
                print(f"[!] ИИ занят (код {e.code}), повтор через {wait} с")
                time.sleep(wait)
                continue
            print("[!] Ошибка ИИ:", e.read().decode()[:300])
            return {}
        except Exception as e:
            print("[!] Ошибка ИИ:", e)
            return {}

    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        result = json.loads(text)
        print("Пересказ от ИИ получен")
        return result
    except Exception as e:
        print("[!] Ошибка ИИ (разбор ответа):", e)
        return {}

# ---------- Страница сайта ----------
CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d1220;color:#e8ecf5;font-family:'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:28px 16px 40px;display:flex;flex-direction:column;gap:16px}
.kicker{font-size:13px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;color:#4fd1c5}
h1{margin:6px 0 0;font-size:32px;line-height:1.15}
.sub{font-size:15px;color:#8d97ad;margin-top:6px}
.card{background:#151c2c;border-radius:16px;padding:6px 18px 8px}
.h{margin:0;padding:14px 0 6px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#8d97ad;font-weight:600}
.sum{padding:2px 0 12px;font-size:15px;line-height:1.5;color:#c5cce0}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;min-height:44px;padding:8px 0;border-top:1px solid #232c42;font-size:16px;line-height:1.35}
.muted{color:#8d97ad}
a{color:#e8ecf5;text-decoration:none}a:hover{color:#4fd1c5}
.foot{text-align:center;font-size:13px;color:#8d97ad;padding-top:6px}
"""

def card(title, rows):
    return f'<div class="card"><h2 class="h">{title}</h2>{rows}</div>'

def overview_card(text):
    if not text:
        return ""
    return card("✨ Главное за утро", f'<div class="sum">{html.escape(text)}</div>')

def rates_card(r):
    title = "💱 Курсы к тенге"
    if not r:
        return card(title, '<div class="row muted">Недоступны</div>')
    usd = r["USD"]
    rows = f'<div class="row"><span>🇺🇸 Доллар</span><b>{usd:.2f} ₸</b></div>'
    for code, flag, name in (("RUB", "🇷🇺", "Рубль"), ("CNY", "🇨🇳", "Юань")):
        kzt = r[code]
        rows += (f'<div class="row"><span>{flag} {name}</span><span><b>{kzt:.2f} ₸</b> '
                 f'<span class="muted">· {kzt / usd:.4f} $</span></span></div>')
    rub_per_usd = usd / r["RUB"]
    cny_per_usd = usd / r["CNY"]
    rows += (f'<div class="row muted"><span>💵 За 1 $</span>'
             f'<span>{rub_per_usd:.2f} ₽ · {cny_per_usd:.2f} ¥</span></div>')
    return card(title, rows)

def news_card(title, icon, items, summary=""):
    rows = ""
    if summary:
        rows += f'<div class="sum">{html.escape(summary)}</div>'
    if not items:
        rows += '<div class="row muted">Свежих новостей по теме пока нет</div>'
    else:
        for it in items:
            link = html.escape(it["link"], quote=True)
            text = html.escape(it["title"])
            rows += f'<div class="row"><a href="{link}">{icon} {text}</a></div>'
    return card(title, rows)

def build_page(rates, finance, border, general, ai, sums):
    body = "".join([
        overview_card(sums.get("overview", "")),
        rates_card(rates),
        news_card("⚖️ Законы и финансы", "💰", finance, sums.get("finance", "")),
        news_card("🛂 Границы и въезд", "🛃", border, sums.get("border", "")),
        news_card("🇰🇿 Казахстан", "📰", general, sums.get("kz", "")),
        news_card("🤖 Искусственный интеллект", "✨", ai, sums.get("ai", "")),
    ])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Дайджест · {DATE_STR}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<div><div class="kicker">📅 Ежедневный дайджест</div><h1>{DATE_STR}</h1>
<div class="sub">Курсы, Казахстан и ИИ. Обновляется каждое утро.</div></div>
{body}
<div class="foot">☀️ Собрано автоматически</div>
</div></body></html>"""

# ---------- Telegram: короткое сообщение со ссылкой ----------
def telegram_text(rates, sums):
    lines = [f"📅 <b>Дайджест · {DATE_STR}</b>", ""]
    if sums.get("overview"):
        lines.append("✨ " + html.escape(sums["overview"]))
        lines.append("")
    if rates:
        usd, rub, cny = rates["USD"], rates["RUB"], rates["CNY"]
        lines.append("💱 <b>Курсы к тенге</b>")
        lines.append(f"🇺🇸 {usd:.2f} ₸ · 🇷🇺 {rub:.2f} ₸ · 🇨🇳 {cny:.2f} ₸")
        lines.append("")
    url = html.escape(CFG["site_url"], quote=True)
    lines.append(f'🌐 <a href="{url}">Открыть дайджест на сайте</a>')
    return "\n".join(lines)

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[!] Нет BOT_TOKEN или CHAT_ID, Telegram пропущен")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=15)
        print("Отправлено в Telegram")
    except urllib.error.HTTPError as e:
        print("Ошибка Telegram:", e.read().decode())

if __name__ == "__main__":
    rates = None
    try:
        rates = get_rates()
        if not all(c in rates for c in ("USD", "RUB", "CNY")):
            rates = None
    except Exception as e:
        print(f"[!] Курсы недоступны: {e}")

    finance, border, general = kazakhstan(CFG)
    ai = read_many(CFG["ai_feeds"], CFG["news_per_block"])
    sums = summarize({"finance": finance, "border": border, "kz": general, "ai": ai})

    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(build_page(rates, finance, border, general, ai, sums))
    print("Страница сохранена: site/index.html")

    if CFG.get("send_to_telegram", True):
        send_telegram(telegram_text(rates, sums))
