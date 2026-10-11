import html
import json
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)

NOW = datetime.now(timezone(timedelta(hours=5)))  # время Казахстана
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
DATE_STR = f"{NOW.day} {MONTHS[NOW.month - 1]} {NOW.year}"
CODES = ("USD", "RUB", "CNY")

# ---------- Курсы и история ----------
def get_rates_for(day):
    url = f"https://nationalbank.kz/rss/get_rates.cfm?fdate={day:%d.%m.%Y}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None
    result = {}
    for item in root.iter("item"):
        code = item.findtext("title")
        if code in CODES:
            try:
                rate = float(item.findtext("description"))
                quant = float(item.findtext("quant") or 1)
            except (TypeError, ValueError):
                continue
            result[code] = rate / quant  # тенге за 1 единицу
    return result if len(result) == len(CODES) else None

def get_history(days):
    """Список (дата, курсы) от старых дней к новым. Дни без данных пропускаются."""
    dates = [NOW.date() - timedelta(days=i) for i in range(days - 1, -1, -1)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(get_rates_for, dates))
    return [(d, r) for d, r in zip(dates, results) if r]

def change_pct(values):
    if len(values) < 2 or not values[0]:
        return None
    return (values[-1] - values[0]) / values[0] * 100

def arrow(ch):
    if ch is None:
        return ""
    if abs(ch) < 0.05:
        return f"▬ {abs(ch):.1f}%"
    return ("▲" if ch > 0 else "▼") + f" {abs(ch):.1f}%"

def arrow_class(ch):
    if ch is None or abs(ch) < 0.05:
        return "flat"
    return "up" if ch > 0 else "down"

def sparkline(values):
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    n = len(values) - 1

    def y(v):
        if hi == lo:
            return 14
        return 26 - (v - lo) / (hi - lo) * 24

    pts = " ".join(f"{i / n * 100:.1f},{y(v):.1f}" for i, v in enumerate(values))
    return ('<svg viewBox="0 0 100 28" preserveAspectRatio="none" width="100%" '
            'height="28" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="var(--accent)" '
            'stroke-width="2" vector-effect="non-scaling-stroke" '
            'stroke-linejoin="round" stroke-linecap="round"/></svg>')

# ---------- Telegram-каналы (публичные, через t.me/s/) ----------
def read_tg_channel(name, limit, max_age_days):
    req = urllib.request.Request(
        f"https://t.me/s/{name}", headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", errors="replace")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    text_limit = CFG.get("tg_text_limit", 4000)
    posts = []
    for chunk in page.split("tgme_widget_message_wrap")[1:]:
        m_text = re.search(r"tgme_widget_message_text[^>]*>(.*?)</div>", chunk, re.S)
        m_link = re.search(
            r'href="(https://t\.me/' + re.escape(name) + r'/\d+)"', chunk, re.I
        )
        if not m_text or not m_link:
            continue
        m_time = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        if m_time:
            try:
                if datetime.fromisoformat(m_time.group(1)) < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        text = re.sub(r"<br\s*/?>", " ", m_text.group(1))
        text = re.sub(r"<[^>]+>", "", text)
        text = " ".join(html.unescape(text).split())
        if not text:
            continue
        short = text if len(text) <= 160 else text[:157].rstrip() + "…"
        posts.append({"channel": name, "title": short,
                      "full": text[:text_limit], "link": m_link.group(1)})
    return posts[-limit:][::-1]  # сначала самые новые

def read_tg_channels(cfg):
    items = []
    for name in cfg.get("tg_channels", []):
        try:
            posts = read_tg_channel(name, cfg.get("tg_posts", 3),
                                    cfg.get("tg_max_age_days", 3))
            if not posts:
                print(f"[!] Канал {name}: свежих постов не найдено "
                      f"(возможно, веб-просмотр отключён)")
            items += posts
        except Exception as e:
            print(f"[!] Канал {name}: {e}")
    return items

# ---------- Пересказ через ИИ (Google Gemini, бесплатный уровень) ----------
def summarize(tg, hist):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not CFG.get("ai_summary", True):
        return {}
    parts = []
    if tg:
        parts.append("Посты из Telegram-каналов (с номерами):")
        for i, p in enumerate(tg, 1):
            parts.append(f"[{i}] @{p['channel']}: {p['full']}")
    if len(hist) >= 2:
        parts.append("Курсы к тенге (первый день периода -> последний):")
        for c in CODES:
            parts.append(f"{c}: {hist[0][1][c]:.2f} -> {hist[-1][1][c]:.2f}")
    if not parts:
        return {}
    prompt = (
        "Ты помощник, который пишет утренний дайджест на русском языке. "
        "Ниже посты из Telegram-каналов (каждый с номером) и динамика курсов к тенге. "
        "Для каждого поста напиши одно короткое предложение по сути (поле items, "
        "ключ это номер поста). Поле tg: 1-2 предложения о главном по постам. "
        "Поле overview: 2-3 предложения о главном за утро, можно упомянуть "
        "изменение курсов. Опирайся ТОЛЬКО на присланный текст, ничего не "
        "выдумывай и не добавляй цифр или деталей, которых там нет. "
        'Ответь только JSON вида {"overview": "...", "tg": "...", '
        '"items": {"1": "...", "2": "..."}}.'
        "\n\n" + "\n".join(parts)
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
            with urllib.request.urlopen(req, timeout=90) as r:
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
            if attempt < 3:
                wait = 20 * (attempt + 1)
                print(f"[!] ИИ не ответил ({e}), повтор через {wait} с")
                time.sleep(wait)
                continue
            print("[!] Ошибка ИИ:", e)
            return {}

    try:
        resp_parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in resp_parts).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        result = json.loads(text)
        print("Пересказ от ИИ получен")
        return result
    except Exception as e:
        print("[!] Ошибка ИИ (разбор ответа):", e)
        return {}

def item_summaries(sums):
    items = sums.get("items")
    if isinstance(items, list):
        items = {str(i + 1): v for i, v in enumerate(items)}
    return items if isinstance(items, dict) else {}

# ---------- Страница сайта ----------
CSS = """
:root{--bg:#0d1220;--card:#151c2c;--text:#e8ecf5;--muted:#8d97ad;--line:#232c42;--accent:#4fd1c5;--sum:#c5cce0;--up:#5ad19a;--down:#ff7a7a}
@media (prefers-color-scheme: light){:root{--bg:#f4f6fb;--card:#ffffff;--text:#16203a;--muted:#5d6785;--line:#e3e8f2;--accent:#0b7a72;--sum:#34405e;--up:#17803d;--down:#c0392b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:28px 16px 40px;display:flex;flex-direction:column;gap:16px}
.kicker{font-size:13px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;color:var(--accent)}
h1{margin:6px 0 0;font-size:32px;line-height:1.15}
.sub{font-size:15px;color:var(--muted);margin-top:6px}
.card{background:var(--card);border-radius:16px;padding:6px 18px 8px}
.h{margin:0;padding:14px 0 6px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
.sum{padding:2px 0 12px;font-size:15px;line-height:1.5;color:var(--sum)}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;min-height:44px;padding:8px 0;border-top:1px solid var(--line);font-size:16px;line-height:1.35}
.nm{flex:0 0 auto}
.spark{flex:1;min-width:56px;max-width:140px}
.val{display:flex;flex-direction:column;align-items:flex-end;text-align:right}
.val small{font-size:13px;color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
.muted{color:var(--muted)}
.note{font-size:12px;color:var(--muted);padding:8px 0 4px;border-top:1px solid var(--line)}
a{color:var(--text);text-decoration:none}a:hover{color:var(--accent)}
.foot{text-align:center;font-size:13px;color:var(--muted);padding-top:6px}
"""

def card(title, rows):
    return f'<div class="card"><h2 class="h">{title}</h2>{rows}</div>'

def overview_card(text):
    if not text:
        return ""
    return card("✨ Главное за утро", f'<div class="sum">{html.escape(text)}</div>')

def rates_card(hist):
    title = "💱 Курсы к тенге"
    if not hist:
        return card(title, '<div class="row muted">Недоступны</div>')
    rate_date, last = hist[-1]
    series = {c: [r[c] for _, r in hist] for c in CODES}
    usd = last["USD"]
    rows = ""
    if rate_date != NOW.date():
        rows += (f'<div class="sum muted">Последние данные Нацбанка: '
                 f'{rate_date:%d.%m.%Y}</div>')
    for code, flag, name in (("USD", "🇺🇸", "Доллар"), ("RUB", "🇷🇺", "Рубль"),
                             ("CNY", "🇨🇳", "Юань")):
        kzt = last[code]
        ch = change_pct(series[code])
        sub = []
        if code != "USD":
            sub.append(f"{kzt / usd:.4f} $")
        if ch is not None:
            sub.append(f'<span class="{arrow_class(ch)}">{arrow(ch)}</span>')
        rows += (f'<div class="row"><span class="nm">{flag} {name}</span>'
                 f'<span class="spark">{sparkline(series[code])}</span>'
                 f'<span class="val"><b>{kzt:.2f} ₸</b>'
                 f'<small>{" · ".join(sub)}</small></span></div>')
    rows += (f'<div class="row muted"><span>💵 За 1 $</span>'
             f'<span>{usd / last["RUB"]:.2f} ₽ · {usd / last["CNY"]:.2f} ¥</span></div>')
    if len(hist) >= 2:
        days = max((hist[-1][0] - hist[0][0]).days, 1)
        rows += f'<div class="note">График и изменение за {days} дн.</div>'
    return card(title, rows)

def tg_card(tg, sums):
    rows = ""
    if sums.get("tg"):
        rows += f'<div class="sum">{html.escape(sums["tg"])}</div>'
    if not tg:
        rows += ('<div class="row muted">Свежих постов нет '
                 '(проверь tg_channels в config.json)</div>')
    else:
        items = item_summaries(sums)
        for i, p in enumerate(tg, 1):
            link = html.escape(p["link"], quote=True)
            text = html.escape(str(items.get(str(i)) or p["title"]))
            rows += (f'<div class="row"><a href="{link}">'
                     f'🎁 @{html.escape(p["channel"])}: {text}</a></div>')
    return card("📣 Telegram и подарки", rows)

def build_page(hist, tg, sums):
    body = "".join([
        overview_card(sums.get("overview", "")),
        rates_card(hist),
        tg_card(tg, sums),
    ])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Дайджест · {DATE_STR}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<div><div class="kicker">📅 Ежедневный дайджест</div><h1>{DATE_STR}</h1>
<div class="sub">Курсы валют и посты из Telegram. Обновляется каждое утро.</div></div>
{body}
<div class="foot">☀️ Собрано автоматически</div>
</div></body></html>"""

# ---------- Telegram: короткое сообщение со ссылкой ----------
def telegram_text(hist, sums):
    lines = [f"📅 <b>Дайджест · {DATE_STR}</b>", ""]
    if sums.get("overview"):
        lines.append("✨ " + html.escape(sums["overview"]))
        lines.append("")
    if hist:
        last = hist[-1][1]
        parts = []
        for code, flag in (("USD", "🇺🇸"), ("RUB", "🇷🇺"), ("CNY", "🇨🇳")):
            ch = change_pct([r[code] for _, r in hist])
            tail = f" {arrow(ch)}" if ch is not None else ""
            parts.append(f"{flag} {last[code]:.2f} ₸{tail}")
        lines.append("💱 <b>Курсы к тенге</b>")
        lines.append(" · ".join(parts))
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
    hist = get_history(int(CFG.get("history_days", 14)))
    if not hist:
        print("[!] Курсы Нацбанка недоступны")
    tg = read_tg_channels(CFG)
    sums = summarize(tg, hist)

    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(build_page(hist, tg, sums))
    print("Страница сохранена: site/index.html")

    if CFG.get("send_to_telegram", True):
        send_telegram(telegram_text(hist, sums))
