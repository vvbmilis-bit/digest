import html
import re
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# ---------- Курсы ----------
FLAGS = {"USD": "🇺🇸", "RUB": "🇷🇺", "CNY": "🇨🇳"}
NAMES = {"USD": "Доллар", "RUB": "Рубль", "CNY": "Юань"}

def get_rates():
    today = date.today().strftime("%d.%m.%Y")
    url = f"https://nationalbank.kz/rss/get_rates.cfm?fdate={today}"
    with urllib.request.urlopen(url, timeout=15) as r:
        root = ET.fromstring(r.read())
    result = {}
    for item in root.iter("item"):
        code = item.findtext("title")
        if code in NAMES:
            rate = float(item.findtext("description"))
            quant = float(item.findtext("quant") or 1)
            result[code] = rate / quant  # тенге за 1 единицу
    return result

def rates_block():
    title = "💱 <b>Курсы валют</b>"
    try:
        r = get_rates()
        usd = r["USD"]
        lines = [f"{FLAGS['USD']} {NAMES['USD']}: {usd:.2f} ₸"]
        for code in ("RUB", "CNY"):
            lines.append(
                f"{FLAGS[code]} {NAMES[code]}: {r[code]:.2f} ₸ · {r[code] / usd:.4f} $"
            )
        lines.append(f"💵 За 1 $: {usd / r['RUB']:.2f} ₽ · {usd / r['CNY']:.2f} ¥")
        return title + "\n" + "\n".join(lines)
    except Exception as e:
        return title + "\nНедоступны: " + html.escape(str(e))

# ---------- Новости ----------
KZ_FEEDS = [
    "https://tengrinews.kz/news.rss",
]
AI_FEEDS = [
    "https://habr.com/ru/rss/hub/artificial_intelligence/all/",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
]
KZ_READ = 60     # сколько новостей читать из каждой казахстанской ленты
SHOW = 5         # сколько показывать в каждом блоке

# Слова для отбора (ищем по началу слова). Список можно менять.
FINANCE_WORDS = [
    "закон", "кодекс", "налог", "ндс", "финанс", "тенге", "нацбанк",
    "национальный банк", "банк", "кредит", "ипотек", "пенси", "пособи",
    "выплат", "штраф", "бюджет", "депозит", "инфляц", "тариф",
]
BORDER_WORDS = [
    "границ", "пограничн", "въезд", "выезд", "безвиз", "визов", "визы",
    "визу", "кпп", "таможн", "миграци", "пересечен",
]

def read_feed(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    items = []
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "text": ((item.findtext("title") or "") + " " +
                     (item.findtext("description") or "")).lower(),
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

def news_block(header, icon, items, empty_note=None):
    if not items:
        return f"{header}\n🤷 {empty_note}" if empty_note else None
    lines = [header]
    for it in items:
        link = html.escape(it["link"], quote=True)
        lines.append(f'{icon} <a href="{link}">{html.escape(it["title"])}</a>')
    return "\n".join(lines)

def kazakhstan_blocks():
    items = read_many(KZ_FEEDS, KZ_READ)
    used = set()

    finance = []
    for it in items:
        if matches(it["text"], FINANCE_WORDS) and it["link"] not in used:
            finance.append(it)
            used.add(it["link"])
        if len(finance) >= SHOW:
            break

    border = []
    for it in items:
        if matches(it["text"], BORDER_WORDS) and it["link"] not in used:
            border.append(it)
            used.add(it["link"])
        if len(border) >= SHOW:
            break

    general = [it for it in items if it["link"] not in used][:SHOW]

    return [
        news_block("⚖️ <b>Законы и финансы</b>", "💰", finance,
                   "Свежих новостей по теме пока нет"),
        news_block("🛂 <b>Границы и въезд</b>", "🛃", border,
                   "Свежих новостей по теме пока нет"),
        news_block("🇰🇿 <b>Казахстан</b>", "📰", general),
    ]

def ai_block():
    items = read_many(AI_FEEDS, SHOW)
    return news_block("🤖 <b>ИИ</b>", "✨", items)

# ---------- Отправка ----------
def send_telegram(blocks):
    messages, current = [], ""
    for b in blocks:
        if current and len(current) + len(b) + 2 > 3800:
            messages.append(current)
            current = b
        else:
            current = current + "\n\n" + b if current else b
    if current:
        messages.append(current)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for text in messages:
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            urllib.request.urlopen(url, data=data, timeout=15)
        except urllib.error.HTTPError as e:
            print("Ошибка Telegram:", e.read().decode())
            return
    print("Отправлено в Telegram")

if __name__ == "__main__":
    blocks = [f"📅 <b>Дайджест · {date.today():%d.%m.%Y}</b>", rates_block()]
    blocks += kazakhstan_blocks()
    blocks.append(ai_block())
    blocks.append("☀️ Хорошего дня!")
    send_telegram([b for b in blocks if b])
