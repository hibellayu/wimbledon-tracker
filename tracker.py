#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")

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


# ── 資料抓取：Sofascore（主要）───────────────────────────────────────────────

def fetch_sofascore_schedule():
    """從 Sofascore 抓取溫網賽程（今日 + 未來 5 天）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sofascore.com/",
    }
    matches = []
    taipei_now = datetime.now(TAIPEI)

    for days_ahead in range(0, 6):
        check_date = taipei_now + timedelta(days=days_ahead)
        date_str = check_date.strftime("%Y-%m-%d")
        url = f"https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/{date_str}"

        try:
            r = requests.get(url, headers=headers, timeout=15)
            print(f"  Sofascore {date_str}: HTTP {r.status_code}")
            if r.status_code != 200:
                continue

            data = r.json()
            events = data.get("events", [])

            for event in events:
                tournament     = event.get("tournament", {})
                t_name         = tournament.get("name", "").lower()
                unique_t       = tournament.get("uniqueTournament", {})
                unique_t_name  = unique_t.get("name", "").lower()
                unique_t_slug  = unique_t.get("slug", "").lower()

                if not any(kw in t_name or kw in unique_t_name or kw in unique_t_slug
                           for kw in ["wimbledon", "championship"]):
                    continue

                home = event.get("homeTeam", {}).get("name", "")
                away = event.get("awayTeam", {}).get("name", "")
                players = [home, away]
                players_lower = [p.lower() for p in players]

                if not any(
                    any(key in p for p in players_lower)
                    for key in TRACKED_PLAYERS
                ):
                    continue

                start_ts = event.get("startTimestamp")
                if not start_ts:
                    continue
                start_utc    = datetime.fromtimestamp(start_ts, tz=timezone.utc)
                start_taipei = start_utc.astimezone(TAIPEI)

                round_name = event.get("roundInfo", {}).get("name", "")
                round_zh   = translate_round(round_name)

                cat_name = tournament.get("category", {}).get("name", "")
                cat_slug = unique_t_slug
                if any(kw in (cat_name + cat_slug).lower() for kw in ["women", "wta", "ladies"]):
                    cat = "女單"
                else:
                    cat = "男單"

                match = {
                    "players":       players,
                    "start_taipei":  start_taipei,
                    "round":         round_zh,
                    "court":         "溫布頓",
                    "category":      cat,
                }
                matches.append(match)
                print(f"    ✓ {cat} {round_zh}: {home} vs {away} ({start_taipei.strftime('%m/%d %H:%M')} 台北時間)")

        except Exception as e:
            print(f"  Sofascore error ({date_str}): {e}")

    return matches


# ── 資料抓取：ESPN（備用）────────────────────────────────────────────────────

def fetch_espn_schedule():
    """從 ESPN 抓取溫網賽程（備用）"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Wimbledon-Tracker/1.0)"}
    matches = []

    urls = [
        "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
    ]

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"  ESPN HTTP {r.status_code}: {url}")
                continue
            data = r.json()
            events = data.get("events", [])
            print(f"  ESPN: {len(events)} events")

            for event in events:
                name_lower = event.get("name", "").lower()
                if not any(kw in name_lower for kw in ["wimbledon", "championship"]):
                    continue
                for comp in event.get("competitions", []):
                    match = parse_espn_match(comp, event)
                    if match:
                        matches.append(match)
        except Exception as e:
            print(f"  ESPN error: {e}")

    return matches


def parse_espn_match(comp, event):
    try:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        players = [c.get("athlete", {}).get("displayName", "") for c in competitors]
        players_lower = [p.lower() for p in players]

        if not any(
            any(key in p for p in players_lower)
            for key in TRACKED_PLAYERS
        ):
            return None

        start_str = comp.get("date", event.get("date", ""))
        if not start_str:
            return None
        start_utc    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        start_taipei = start_utc.astimezone(TAIPEI)

        round_name = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
        round_zh   = translate_round(round_name)

        series = event.get("series", {}).get("slug", "")
        cat    = "女單" if "wta" in series.lower() else "男單"

        return {
            "players":      players,
            "start_taipei": start_taipei,
            "round":        round_zh,
            "court":        "溫布頓",
            "category":     cat,
        }
    except Exception:
        return None


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


# ── 事件去重 ID ───────────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "wb" + hashlib.md5(key.encode()).hexdigest()[:12]


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

    # 主要來源
    print("\n📡 抓取 Sofascore 賽程（今日 + 未來 5 天）...")
    matches = fetch_sofascore_schedule()
    print(f"   Sofascore 找到 {len(matches)} 場追蹤選手比賽")

    # 備用
    if not matches:
        print("\n📡 Sofascore 無資料，嘗試 ESPN...")
        matches = fetch_espn_schedule()
        print(f"   ESPN 找到 {len(matches)} 場追蹤選手比賽")

    if not matches:
        print("⚠️  本次無追蹤選手賽程（可能為休賽日或 API 暫時無資料）")
        return

    print("\n📅 更新 Google Calendar...")
    try:
        service = get_calendar_service()
        added = sum(1 for m in matches if create_calendar_event(service, m))
        print(f"\n✅ 完成：新增 {added} 場，跳過已存在 {len(matches) - added} 場")
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise


if __name__ == "__main__":
    main()
