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

def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

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

# ---------- Погода (Open-Meteo, без ключа) ----------
def weather_desc(code):
    if code == 0:
        return "☀️", "Ясно"
    if code == 1:
        return "🌤️", "Преимущественно ясно"
    if code == 2:
        return "⛅", "Переменная облачность"
    if code == 3:
        return "☁️", "Пасмурно"
    if code in (45, 48):
        return "🌫️", "Туман"
    if code in (51, 53, 55, 56, 57):
        return "🌦️", "Морось"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "🌧️", "Дождь"
    if code in (71, 73, 75, 77, 85, 86):
        return "🌨️", "Снег"
    if code in (95, 96, 99):
        return "⛈️", "Гроза"
    return "🌡️", "Погода"

def get_weather():
    w = CFG.get("weather")
    if not w:
        return None
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={w['lat']}&longitude={w['lon']}"
           "&current=temperature_2m,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,wind_speed_10m_max,weather_code"
           "&timezone=auto&forecast_days=1")
    try:
        d = get_json(url)
        daily = d["daily"]
        return {
            "name": w.get("name", ""),
            "now": float(d["current"]["temperature_2m"]),
            "code": int(daily["weather_code"][0]),
            "tmax": float(daily["temperature_2m_max"][0]),
            "tmin": float(daily["temperature_2m_min"][0]),
            "rain": float(daily["precipitation_probability_max"][0] or 0),
            "wind": float(daily["wind_speed_10m_max"][0] or 0),
        }
    except Exception as e:
        print(f"[!] Погода недоступна: {e}")
        return None

# ---------- Криптовалюты (CoinGecko, без ключа) ----------
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.0f}".replace(",", " ")
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.4f}"

def get_crypto():
    ids = CFG.get("crypto_ids", [])
    if not ids:
        return []
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
           f"&ids={','.join(ids)}&sparkline=true&price_change_percentage=24h,7d")
    try:
        data = get_json(url)
    except Exception as e:
        print(f"[!] Криптовалюты недоступны: {e}")
        return []
    order = {c: i for i, c in enumerate(ids)}
    data.sort(key=lambda x: order.get(x.get("id"), 99))
    out = []
    for c in data:
        if c.get("current_price") is None:
            continue
        spark = ((c.get("sparkline_in_7d") or {}).get("price") or [])[::4]
        out.append({
            "symbol": (c.get("symbol") or "").upper(),
            "price": float(c["current_price"]),
            "ch24": c.get("price_change_percentage_24h_in_currency"),
            "ch7": c.get("price_change_percentage_7d_in_currency"),
            "spark": spark,
        })
    return out

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

# ---------- Пересказ и урок через ИИ (Google Gemini, бесплатный уровень) ----------
def lesson_topic():
    topics = CFG.get("lesson_topics") or []
    if not topics or not CFG.get("show_lesson", True):
        return None
    return topics[NOW.timetuple().tm_yday % len(topics)]

def summarize(tg, hist, weather, crypto, topic):
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
    if weather:
        _, desc = weather_desc(weather["code"])
        parts.append(
            f"Погода сегодня ({weather['name']}): сейчас {weather['now']:+.0f}°, "
            f"днём от {weather['tmin']:+.0f}° до {weather['tmax']:+.0f}°, {desc}, "
            f"вероятность осадков до {weather['rain']:.0f}%")
    if crypto:
        parts.append("Криптовалюты, изменение за 24 часа: " + ", ".join(
            f"{c['symbol']} {c['ch24']:+.1f}%" for c in crypto
            if c.get("ch24") is not None))
    if topic:
        parts.append(f"Тема ИИ-урока дня: {topic}")
    if not parts:
        return {}
    prompt = (
        "Ты помощник, который пишет утренний дайджест на русском языке. "
        "Ниже данные: посты из Telegram-каналов (с номерами), курсы, погода, "
        "криптовалюты и тема урока. "
        "Поле items: для каждого поста одно короткое предложение по сути, ключ это "
        "номер поста. Поле tg: 1-2 предложения о главном по постам. "
        "Поле overview: 2-3 предложения о главном за утро, можно упомянуть курсы, "
        "погоду и криптовалюты. По данным выше опирайся ТОЛЬКО на присланный текст "
        "и цифры, ничего не выдумывай. "
        "Поле lesson: объект с полями title (заголовок) и text (4-6 предложений) "
        "про указанную тему ИИ-урока: объясни простыми словами для новичка. "
        "Без заданий, без упражнений, без советов что попрактиковать и без "
        "упоминания ИИ-агентов. Если не уверен в факте, не пиши его, цифры и "
        "названия не выдумывай. Если темы урока нет, пропусти поле lesson. "
        'Ответь только JSON вида {"overview": "...", "tg": "...", '
        '"items": {"1": "..."}, "lesson": {"title": "...", "text": "..."}}.'
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

def weather_card(w):
    if not w:
        return ""
    icon, desc = weather_desc(w["code"])
    rows = (f'<div class="row"><span>{icon} {desc}</span><b>{w["now"]:+.0f}°</b></div>'
            f'<div class="row"><span>Сегодня</span>'
            f'<span>{w["tmin"]:+.0f}° … {w["tmax"]:+.0f}°</span></div>'
            f'<div class="row muted"><span>Осадки до {w["rain"]:.0f}%</span>'
            f'<span>Ветер до {w["wind"]:.0f} км/ч</span></div>')
    return card(f'🌤️ Погода · {html.escape(w["name"])}', rows)

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

def crypto_card(crypto):
    if not crypto:
        return ""
    rows = ""
    for c in crypto:
        sub = []
        for label, key in (("24ч", "ch24"), ("7д", "ch7")):
            ch = c.get(key)
            if ch is not None:
                sub.append(f'<span class="{arrow_class(ch)}">{label} {arrow(ch)}</span>')
        rows += (f'<div class="row"><span class="nm">🪙 {html.escape(c["symbol"])}</span>'
                 f'<span class="spark">{sparkline(c["spark"])}</span>'
                 f'<span class="val"><b>${fmt_price(c["price"])}</b>'
                 f'<small>{" · ".join(sub)}</small></span></div>')
    rows += '<div class="note">Цены в долларах США, график за 7 дней</div>'
    return card("🪙 Криптовалюты", rows)

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

def lesson_card(sums):
    lesson = sums.get("lesson")
    if not lesson:
        return ""
    if isinstance(lesson, dict):
        title = str(lesson.get("title", "")).strip()
        text = str(lesson.get("text", "")).strip()
    else:
        title, text = "", str(lesson).strip()
    if not text:
        return ""
    head = f"<b>{html.escape(title)}</b><br>" if title else ""
    return card("🧠 ИИ-урок дня", f'<div class="sum">{head}{html.escape(text)}</div>')

def reminders_card():
    items = [str(x) for x in CFG.get("reminders", []) if str(x).strip()]
    if not items:
        return ""
    rows = "".join(f'<div class="row">📌 {html.escape(x)}</div>' for x in items)
    return card("📌 Мои напоминания", rows)

def build_page(hist, weather, crypto, tg, sums):
    body = "".join([
        overview_card(sums.get("overview", "")),
        weather_card(weather),
        rates_card(hist),
        crypto_card(crypto),
        tg_card(tg, sums),
        lesson_card(sums),
        reminders_card(),
    ])
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Дайджест · {DATE_STR}</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<div><div class="kicker">📅 Ежедневный дайджест</div><h1>{DATE_STR}</h1>
<div class="sub">Погода, курсы, крипта и посты из Telegram. Обновляется каждое утро.</div></div>
{body}
<div class="foot">☀️ Собрано автоматически</div>
</div></body></html>"""

# ---------- Telegram: короткое сообщение со ссылкой ----------
def telegram_text(hist, weather, crypto, sums):
    lines = [f"📅 <b>Дайджест · {DATE_STR}</b>", ""]
    if sums.get("overview"):
        lines.append("✨ " + html.escape(sums["overview"]))
        lines.append("")
    if weather:
        icon, desc = weather_desc(weather["code"])
        lines.append(f"{icon} <b>{html.escape(weather['name'])}</b>: "
                     f"{weather['tmin']:+.0f}…{weather['tmax']:+.0f}°, {desc.lower()}")
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
    if crypto:
        lines.append("🪙 <b>Крипта</b>")
        lines.append(" · ".join(
            f"{c['symbol']} ${fmt_price(c['price'])}"
            + (f" {arrow(c['ch24'])}" if c.get("ch24") is not None else "")
            for c in crypto))
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
    weather = get_weather()
    crypto = get_crypto()
    tg = read_tg_channels(CFG)
    topic = lesson_topic()
    sums = summarize(tg, hist, weather, crypto, topic)

    os.makedirs("site", exist_ok=True)
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(build_page(hist, weather, crypto, tg, sums))
    print("Страница сохранена: site/index.html")

    if CFG.get("send_to_telegram", True):
        send_telegram(telegram_text(hist, weather, crypto, sums))
