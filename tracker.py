#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, hashlib, re
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")
BST    = ZoneInfo("Europe/London")

TRACKED_PLAYERS = [
    "sinner", "alcaraz", "djokovic", "zverev",
    "sabalenka", "andreeva", "rybakina", "osaka",
]

PLAYER_DISPLAY = {
    "sinner":    "Jannik Sinner",
    "alcaraz":   "Carlos Alcaraz",
    "djokovic":  "Novak Djokovic",
    "zverev":    "Alexander Zverev",
    "sabalenka": "Aryna Sabalenka",
    "andreeva":  "Mirra Andreeva",
    "rybakina":  "Elena Rybakina",
    "osaka":     "Naomi Osaka",
}

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

ROUND_MAP = {
    "round of 128":  "第一輪", "first round":  "第一輪", "1st round": "第一輪", "round 1": "第一輪",
    "round of 64":   "第二輪", "second round": "第二輪", "2nd round": "第二輪", "round 2": "第二輪",
    "round of 32":   "第三輪", "third round":  "第三輪", "3rd round": "第三輪", "round 3": "第三輪",
    "round of 16":   "第四輪", "fourth round": "第四輪", "4th round": "第四輪", "round 4": "第四輪",
    "quarterfinal":  "四強賽", "quarter-final": "四強賽", "quarter finals": "四強賽", "qf": "四強賽",
    "semifinal":     "準決賽", "semi-final":   "準決賽", "semi finals":    "準決賽", "sf": "準決賽",
    "final":         "決賽",
}

# 溫網 2026 各輪預計日期（假設首日 6/29）
ROUND_DATES = {
    "round of 128": [date(2026, 6, 29), date(2026, 6, 30)],
    "first round":  [date(2026, 6, 29), date(2026, 6, 30)],
    "round of 64":  [date(2026, 7, 1),  date(2026, 7, 2)],
    "second round": [date(2026, 7, 1),  date(2026, 7, 2)],
    "round of 32":  [date(2026, 7, 3),  date(2026, 7, 4)],
    "third round":  [date(2026, 7, 3),  date(2026, 7, 4)],
    "round of 16":  [date(2026, 7, 6),  date(2026, 7, 7)],
    "fourth round": [date(2026, 7, 6),  date(2026, 7, 7)],
    "quarterfinal": [date(2026, 7, 8),  date(2026, 7, 9)],
    "quarter-final":[date(2026, 7, 8),  date(2026, 7, 9)],
    "semifinal":    [date(2026, 7, 10), date(2026, 7, 11)],
    "semi-final":   [date(2026, 7, 10), date(2026, 7, 11)],
    "final":        [date(2026, 7, 12), date(2026, 7, 13)],
}

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ── 資料抓取：ATP Tour draw（主要來源）──────────────────────────────────────

def fetch_atp_draws():
    """ATP Tour Wimbledon draw — 從 bracket 抓取未完成比賽"""
    matches = []

    sources = [
        ("男單", "ATP", "https://www.atptour.com/en/scores/current/wimbledon/540/draws"),
        # WTA 官網是 JS 渲染，暫時停用（改由 ESPN live 補充女單資料）
        # ("女單", "WTA", "https://www.wtatennis.com/tournaments/1114/wimbledon/2026/draws"),
    ]

    for cat, label, url in sources:
        try:
            r = requests.get(url, headers=HEADERS_BROWSER, timeout=15)
            print(f"  {label} draws: HTTP {r.status_code}")
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator=" ")
            found = [k for k in TRACKED_PLAYERS if k in text.lower()]
            print(f"  {label}: tracked players in page = {found}")

            if not found:
                print(f"  {label}: no tracked players, skipping")
                continue

            found_matches = parse_draw_bracket(soup, cat, label)
            print(f"  {label}: {len(found_matches)} upcoming matches found")
            matches.extend(found_matches)

        except Exception as e:
            print(f"  {label} error: {e}")

    return matches


def expand_name(abbr):
    """縮寫選手名 → 全名（追蹤選手才展開）"""
    abbr_lower = abbr.lower()
    for key, full in PLAYER_DISPLAY.items():
        if key in abbr_lower:
            return full
    # 去掉括號內的種子號碼，如 J. Sinner(1) → J. Sinner
    return re.sub(r'\(\d+\)', '', abbr).strip()


def parse_draw_bracket(soup, cat, label):
    """解析 draw bracket：每個 .draw-item 是單一選手格，需配對找對手"""
    matches = []
    today = datetime.now(TAIPEI).date()

    # 收集所有 draw-item 及其選手名稱
    all_items = soup.select(".draw-item")
    print(f"  {label}: total draw-items = {len(all_items)}")

    # 建立 index → (item, name) 對映
    item_players = []
    for item in all_items:
        name_el = (
            item.select_one("div.name a") or
            item.select_one(".name a") or
            item.select_one("a[href*='/en/players/']") or
            item.select_one("a[href*='/players/']")
        )
        raw_name = name_el.get_text(strip=True) if name_el else ""
        # 找種子（draw-item 內獨立的 seed span）
        seed_el = item.select_one(".seed, [class*='seed']")
        if seed_el:
            seed = seed_el.get_text(strip=True)
            raw_name = raw_name.replace(seed, "").strip()
        item_players.append((item, raw_name))

    # 找出含有追蹤選手的 draw-item，配對相鄰選手
    for i, (item, player_raw) in enumerate(item_players):
        if not any(k in player_raw.lower() for k in TRACKED_PLAYERS):
            continue

        # 找對手：相鄰索引配對（奇偶）
        partner_idx = i - 1 if i % 2 == 1 else i + 1
        if 0 <= partner_idx < len(item_players):
            _, opponent_raw = item_players[partner_idx]
        else:
            opponent_raw = "TBD"

        player   = expand_name(player_raw)
        opponent = expand_name(opponent_raw) if opponent_raw else "TBD"

        # 確認是否有比分（比賽已結束）—— 往前找含分數的父元素
        parent = item.parent
        context_text = parent.get_text(" ", strip=True) if parent else ""
        stripped = re.sub(r'[A-Z]\.\s*\w+', "", context_text)  # 去選手名
        has_score = bool(re.search(r'\b[0-7]\s+[0-7]\b', stripped))

        # 找輪次 —— 在同一輪的 draw-round 容器裡找標題
        round_text = ""
        el = item
        for _ in range(6):
            el = el.parent
            if el is None:
                break
            # 檢查 class 裡有沒有 "round" 字樣
            classes = " ".join(el.get("class", []))
            if "round" in classes.lower():
                # 找裡面的標題文字
                title_el = el.select_one("h2, h3, h4, .round-title, [class*='round-header'], [class*='round-name']")
                if title_el:
                    round_text = title_el.get_text(strip=True)
                    break
                # 或直接用 class 名稱推算
                for cls in el.get("class", []):
                    if "round" in cls.lower():
                        round_text = cls.replace("-", " ").replace("_", " ")
                        break
                if round_text:
                    break

        round_zh = translate_round(round_text)

        print(f"  {label}: {player} vs {opponent} | round={round_text!r} | scored={has_score}")

        if not has_score and opponent and opponent != player:
            match_date   = estimate_match_date(round_text, today)
            start_taipei = datetime.combine(match_date, datetime.min.time()).replace(
                hour=20, minute=0, tzinfo=TAIPEI
            )
            matches.append({
                "players":      [player, opponent],
                "start_taipei": start_taipei,
                "round":        round_zh,
                "court":        "溫布頓",
                "category":     cat,
            })

    return matches


def _get_player_from_item(item):
    name_el = (
        item.select_one("div.name a") or
        item.select_one(".name a") or
        item.select_one("a[href*='/players/']")
    )
    name = name_el.get_text(strip=True) if name_el else ""
    return item, name


def find_round_for_item(item, soup):
    """往前找最近的輪次標題"""
    for el in item.find_all_previous():
        if el.name in ["h2", "h3", "h4"]:
            t = el.get_text(strip=True).lower()
            if any(kw in t for kw in ["round", "final", "quarter", "semi"]):
                return el.get_text(strip=True)
        if el.get("class"):
            classes = " ".join(el.get("class", []))
            if "round-title" in classes or "round-header" in classes or "round-name" in classes:
                return el.get_text(strip=True)
    return ""


def estimate_match_date(round_text, today):
    """根據輪次估算比賽日期"""
    rt = round_text.lower().strip()
    for key, dates in ROUND_DATES.items():
        if key in rt:
            for d in dates:
                if d >= today:
                    return d
            return dates[-1]
    return today


# ── 資料抓取：ESPN Live（備用）──────────────────────────────────────────────

def fetch_espn_live():
    """ESPN scoreboard — 僅有比賽正在進行時才有資料"""
    matches = []
    taipei_now = datetime.now(TAIPEI)

    for days in range(0, 2):
        date_str = (taipei_now + timedelta(days=days)).strftime("%Y%m%d")
        for tour, cat in [("atp", "男單"), ("wta", "女單")]:
            url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard?dates={date_str}"
            try:
                r = requests.get(url, headers=HEADERS_JSON, timeout=10)
                if r.status_code != 200:
                    continue
                data = r.json()
                for event in data.get("events", []):
                    if not any(kw in event.get("name", "").lower() for kw in ["wimbledon", "championship"]):
                        continue
                    for comp in event.get("competitions", []):
                        players = [c.get("athlete", {}).get("displayName", "") for c in comp.get("competitors", [])]
                        if len(players) < 2:
                            continue
                        if not any(any(k in p.lower() for k in TRACKED_PLAYERS) for p in players):
                            continue
                        start_str = comp.get("date", event.get("date", ""))
                        if not start_str:
                            continue
                        start_taipei = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(TAIPEI)
                        round_name   = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
                        matches.append({
                            "players":      players,
                            "start_taipei": start_taipei,
                            "round":        translate_round(round_name),
                            "court":        comp.get("venue", {}).get("fullName", "") or "溫布頓",
                            "category":     cat,
                        })
                        print(f"  ESPN live: {players[0]} vs {players[1]}")
            except Exception as e:
                print(f"  ESPN error: {e}")
    return matches


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


# ── 事件去重 ID ───────────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    """
    Google Calendar event ID 規則：
    - 只能用 a-v 和 0-9（base32hex）
    - 5 ~ 1024 字元
    MD5 hexdigest 只含 0-9, a-f，全部都在合法範圍內
    前綴用 "tm"（t 和 m 都在 a-v 內）
    """
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "tm" + hashlib.md5(key.encode()).hexdigest()  # 34 chars total, all valid


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_calendar_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 環境變數未設定")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def event_exists(service, event_id):
    try:
        service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception:
        return False


def create_calendar_event(service, match):
    players = match["players"]
    start   = match["start_taipei"]
    end     = start + timedelta(hours=3)
    round_  = match["round"]
    court   = match["court"]
    cat     = match["category"]

    date_str = start.strftime("%Y%m%d")
    event_id = make_event_id(players, date_str, cat)

    if event_exists(service, event_id):
        print(f"  ⏭️  已存在：{players[0]} vs {players[1]}")
        return False

    title = f"🎾 [{round_}] {cat} · {players[0]} vs {players[1]}"
    body = {
        "id":       event_id,
        "summary":  title,
        "location": f"溫布頓 · {court}",
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Taipei"},
        "end":   {"dateTime": end.isoformat(),   "timeZone": "Asia/Taipei"},
        "description": f"溫布頓 2026 · {cat}\n{round_} · {court}\n{players[0]} vs {players[1]}",
        "colorId": "11",
    }

    service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    print(f"  ✅ 新增：{title} ({start.strftime('%m/%d %H:%M')})")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🎾 溫布頓賽程追蹤器啟動")
    print(f"⏰ 執行時間：{datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M')} 台北時間")

    # [1] ATP draw 簽表（男單）
    print("\n📡 [1] ATP draw 簽表（男單）...")
    matches = fetch_atp_draws()
    print(f"   → {len(matches)} 場（男單）")

    # [2] ESPN 即時比分（女單 + 補充男單 live 資料）
    print("\n📡 [2] ESPN live scoreboard（女單 + live 補充）...")
    live_matches = fetch_espn_live()
    print(f"   → {len(live_matches)} 場（live）")
    matches.extend(live_matches)

    if not matches:
        print("\n⚠️  本次無可用賽程（休賽日或比賽時段外）")
        return

    print(f"\n📅 更新 Google Calendar（{len(matches)} 場）...")
    try:
        service = get_calendar_service()
        added   = sum(1 for m in matches if create_calendar_event(service, m))
        print(f"\n✅ 完成：新增 {added} 場，跳過 {len(matches) - added} 場（已存在）")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
