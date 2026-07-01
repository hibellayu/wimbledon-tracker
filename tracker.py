#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
每 2 小時自動抓取賽程，更新 Google Calendar
"""
import os, json, re, time, hashlib
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")
BST    = ZoneInfo("Europe/London")

TRACKED_PLAYERS = [
    # 男單
    "sinner", "alcaraz", "djokovic", "zverev",
    # 女單
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
    "first round":   "第一輪", "1st round": "第一輪", "round 1": "第一輪",
    "second round":  "第二輪", "2nd round": "第二輪", "round 2": "第二輪",
    "third round":   "第三輪", "3rd round": "第三輪", "round 3": "第三輪",
    "fourth round":  "第四輪", "4th round": "第四輪", "round 4": "第四輪",
    "round of 16":   "第四輪",
    "quarterfinal":  "四強賽", "quarter-final": "四強賽", "qf": "四強賽",
    "semifinal":     "準決賽", "semi-final": "準決賽", "sf": "準決賽",
    "final":         "決賽",
}


# ── 資料抓取：ESPN ─────────────────────────────────────────────────────────────

def fetch_espn_schedule():
    """從 ESPN 抓取溫網賽程"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Wimbledon-Tracker/1.0)"}
    matches = []

    # ESPN Wimbledon schedule endpoint
    urls = [
        "https://www.espn.com/tennis/schedule/_/tour/grand-slam",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
    ]

    for url in urls[1:]:  # 使用 JSON API
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            events = data.get("events", [])
            for event in events:
                name = event.get("name", "").lower()
                if "wimbledon" not in name and "championship" not in name:
                    continue
                for comp in event.get("competitions", []):
                    match = parse_espn_match(comp, event)
                    if match:
                        matches.append(match)
        except Exception as e:
            print(f"  ESPN API error ({url}): {e}")

    return matches


def parse_espn_match(comp, event):
    """解析 ESPN 比賽資料"""
    try:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        players = [c.get("athlete", {}).get("displayName", "") for c in competitors]
        players_lower = [p.lower() for p in players]

        # 篩選追蹤選手
        if not any(
            any(key in p for p in players_lower)
            for key in TRACKED_PLAYERS
        ):
            return None

        # 比賽時間（BST）
        start_str = comp.get("date", event.get("date", ""))
        if not start_str:
            return None
        start_bst = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(BST)
        start_taipei = start_bst.astimezone(TAIPEI)

        # 輪次與球場
        round_name = comp.get("notes", [{}])[0].get("headline", "") if comp.get("notes") else ""
        round_zh = translate_round(round_name)
        venue = comp.get("venue", {}).get("fullName", "溫布頓")

        # 比賽類別
        series = event.get("series", {}).get("slug", "")
        category = "女單" if "wta" in series.lower() else "男單"

        return {
            "players": players,
            "start_taipei": start_taipei,
            "round": round_zh,
            "court": venue,
            "category": category,
        }
    except Exception:
        return None


# ── 備用抓取：BBC Sport ───────────────────────────────────────────────────────

def fetch_bbc_schedule():
    """從 BBC Sport 抓取溫網賽程（ESPN 失敗時的備用）"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Wimbledon-Tracker/1.0)"}
    matches = []
    try:
        r = requests.get(
            "https://www.bbc.com/sport/tennis/wimbledon/scores-fixtures",
            headers=headers, timeout=15
        )
        soup = BeautifulSoup(r.text, "html.parser")
        # 解析 BBC 賽程（依實際 HTML 結構調整）
        for block in soup.select("[data-testid='fixture-block']"):
            text = block.get_text(" ", strip=True).lower()
            if any(key in text for key in TRACKED_PLAYERS):
                matches.append({"raw": text, "source": "bbc"})
    except Exception as e:
        print(f"  BBC scrape error: {e}")
    return matches


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


# ── 事件去重 ID ───────────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    """產生穩定的去重 ID（避免重複建立事件）"""
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ', '') for p in players))}-{date_str}-{category}"
    return "wb" + hashlib.md5(key.encode()).hexdigest()[:12]


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_calendar_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 環境變數未設定")
    creds_data = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_data,
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
    start  = match["start_taipei"]
    end    = start + timedelta(hours=3)  # 預估 3 小時
    round_ = match["round"]
    court  = match["court"]
    cat    = match["category"]

    date_str  = start.strftime("%Y%m%d")
    event_id  = make_event_id(players, date_str, cat)

    if event_exists(service, event_id):
        print(f"  ⏭️  已存在：{players[0]} vs {players[1]}")
        return

    title = f"🎾 [{round_}] {cat} · {players[0]} vs {players[1]}"
    body = {
        "id": event_id,
        "summary": title,
        "location": f"溫布頓 · {court}",
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Asia/Taipei",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Asia/Taipei",
        },
        "description": f"溫布頓 2026 · {cat}\n{round_} · {court}\n{players[0]} vs {players[1]}",
        "colorId": "11",  # 番茄紅
    }

    service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    print(f"  ✅ 新增：{title}")
    print(f"     台北時間：{start.strftime('%m/%d %H:%M')}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🎾 溫布頓賽程追蹤器啟動")
    print(f"⏰ 執行時間：{datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M')} 台北時間")

    # 抓取賽程
    print("\n📡 抓取 ESPN 賽程...")
    matches = fetch_espn_schedule()
    print(f"   找到 {len(matches)} 場追蹤選手比賽")

    if not matches:
        print("   ESPN 無資料，嘗試 BBC Sport...")
        raw = fetch_bbc_schedule()
        print(f"   BBC 原始結果：{len(raw)} 筆（需人工確認）")
        return

    # 建立 Google Calendar 事件
    print("\n📅 更新 Google Calendar...")
    try:
        service = get_calendar_service()
        for match in matches:
            create_calendar_event(service, match)
    except Exception as e:
        print(f"❌ Google Calendar 錯誤：{e}")
        raise

    print(f"\n✅ 完成，共處理 {len(matches)} 場比賽")


if __name__ == "__main__":
    main()
