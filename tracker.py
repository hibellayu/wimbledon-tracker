#!/usr/bin/env python3
"""
Wimbledon 2026 重點選手賽程追蹤器
主要來源：tennis-db.com（靜態 HTML，ATP + WTA）
補充來源：ESPN live（即時比賽時間）
每 2 小時自動執行，更新 Google Calendar
"""
import os, json, hashlib, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 設定 ─────────────────────────────────────────────────────────────────────

TAIPEI = ZoneInfo("Asia/Taipei")

TRACKED_PLAYERS = [
    "sinner", "djokovic", "zverev",         # 男單（Alcaraz 腕傷退賽）
    "sabalenka", "andreeva", "rybakina", "osaka",  # 女單
]

PLAYER_DISPLAY = {
    "sinner":    "Jannik Sinner",
    "djokovic":  "Novak Djokovic",
    "zverev":    "Alexander Zverev",
    "sabalenka": "Aryna Sabalenka",
    "andreeva":  "Mirra Andreeva",
    "rybakina":  "Elena Rybakina",
    "osaka":     "Naomi Osaka",
}

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

ROUND_MAP = {
    "round of 128": "第一輪", "first round":  "第一輪", "1st round": "第一輪",
    "round of 64":  "第二輪", "second round": "第二輪", "2nd round": "第二輪",
    "round of 32":  "第三輪", "third round":  "第三輪", "3rd round": "第三輪",
    "round of 16":  "第四輪", "fourth round": "第四輪", "4th round": "第四輪",
    "quarterfinal": "四強賽", "quarter-final": "四強賽", "qf": "四強賽",
    "semifinal":    "準決賽", "semi-final":   "準決賽", "sf": "準決賽",
    "final":        "決賽",
}

# tennis-db.com 的輪次代碼
ESPN_ROUND_MAP = {
    "1ST":  "第一輪", "FIRST":  "第一輪",
    "2ND":  "第二輪", "SECOND": "第二輪",
    "3RD":  "第三輪", "THIRD":  "第三輪",
    "4TH":  "第四輪", "FOURTH": "第四輪",
    "QF":   "四強賽",
    "SF":   "準決賽",
    "FINAL":"決賽",
}

# 溫布頓 2026 ESPN Sports Core API 事件 ID（ATP 與 WTA 共用）
ESPN_EVENT_ID = "188-2026"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}
HEADERS_JSON = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


# ── 輔助函數 ──────────────────────────────────────────────────────────────────

def expand_name(raw):
    """縮寫/含排名號碼的選手名 → 清理後名稱（追蹤選手展開全名）"""
    clean = re.sub(r'#\d+', '', raw).strip()
    clean_lower = clean.lower()
    for key, full in PLAYER_DISPLAY.items():
        if key in clean_lower:
            return full
    return clean


def translate_round(raw):
    raw_lower = raw.lower().strip()
    for key, zh in ROUND_MAP.items():
        if key in raw_lower:
            return zh
    return raw or "待定"


def _dedup_matches(matches):
    """同一對選手只保留一筆"""
    seen = set()
    out  = []
    for m in matches:
        key = frozenset(p.lower() for p in m["players"])
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# ── 資料抓取：ESPN Sports Core API（主要來源，ATP + WTA）───────────────────

def fetch_espn_sports_core():
    """
    ESPN Sports Core API — 完整賽事資料，不被 GitHub Actions 封鎖
    - ATP 男單 + WTA 女單 各 333 場，全部內嵌在同一個 JSON 回應
    - 有精確 UTC 時間（`timeValid=True`）或未定時間（`timeValid=False`）
    - 不含 Qualifying 輪
    """
    matches = []
    today_utc = datetime.now(timezone.utc).date()
    sources = [
        (f"http://sports.core.api.espn.com/v2/sports/tennis/leagues/atp/events/{ESPN_EVENT_ID}?lang=en&region=us", "男單"),
        (f"http://sports.core.api.espn.com/v2/sports/tennis/leagues/wta/events/{ESPN_EVENT_ID}?lang=en&region=us", "女單"),
    ]

    for url, category in sources:
        try:
            r = requests.get(url, headers=HEADERS_JSON, timeout=20)
            print(f"  ESPN sports core {category}: HTTP {r.status_code}")
            if r.status_code != 200:
                continue

            comps = r.json().get("competitions", [])
            print(f"  {category} 總場次: {len(comps)}")

            for comp in comps:
                # 略過 Qualifying
                round_info = comp.get("round", {})
                round_desc = round_info.get("description", "")
                if "Qualifying" in round_desc:
                    continue

                # 篩選未完賽（雙方 winner 皆為 False）
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                if any(cp.get("winner", False) for cp in competitors):
                    continue

                p1_raw = competitors[0].get("name", "")
                p2_raw = competitors[1].get("name", "")

                # 確認是追蹤選手之一
                if not (any(k in p1_raw.lower() for k in TRACKED_PLAYERS) or
                        any(k in p2_raw.lower() for k in TRACKED_PLAYERS)):
                    continue

                # 日期／時間（UTC → 台北）
                date_str = comp.get("date", "")
                if not date_str:
                    continue
                comp_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if comp_utc.date() < today_utc:
                    continue

                time_valid = comp.get("timeValid", False)
                if time_valid:
                    start_taipei = comp_utc.astimezone(TAIPEI)
                else:
                    # 時間未定 → 預設台北 20:00 那天
                    match_date = comp_utc.astimezone(TAIPEI).date()
                    start_taipei = datetime(
                        match_date.year, match_date.month, match_date.day,
                        20, 0, tzinfo=TAIPEI
                    )

                # 輪次
                abbrev = round_info.get("abbreviation", "").upper()
                round_zh = ESPN_ROUND_MAP.get(abbrev, round_desc or "待定")

                # 球場
                court = comp.get("court", {}).get("description", "") or "溫布頓"

                p1 = expand_name(p1_raw)
                p2 = expand_name(p2_raw)

                matches.append({
                    "players":      [p1, p2],
                    "start_taipei": start_taipei,
                    "round":        round_zh,
                    "court":        court,
                    "category":     category,
                })
                time_tag = start_taipei.strftime('%m/%d %H:%M') + (" ✓時間" if time_valid else " ⚠️待定")
                print(f"  ESPN core ✓ {category}: {p1} vs {p2} | {round_zh} | {time_tag} 台北")

        except Exception as e:
            print(f"  ESPN sports core error ({category}): {e}")
            import traceback; traceback.print_exc()

    return _dedup_matches(matches)




# ── Google Calendar ───────────────────────────────────────────────────────────

def make_event_id(players, date_str, category):
    """Google Calendar ID：只能用 a-v 和 0-9（base32hex），前綴 tm"""
    key = f"wimbledon2026-{'-'.join(sorted(p.lower().replace(' ','') for p in players))}-{date_str}-{category}"
    return "tm" + hashlib.md5(key.encode()).hexdigest()  # 34 chars，全部合法


def get_calendar_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON 環境變數未設定")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def event_exists(service, event_id, players=None, start=None):
    """先用 ID 查，若找不到再用選手名稱在前後 24h 內搜尋（防止手動建立的事件被重複）"""
    try:
        service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception:
        pass
    if players and start:
        fragment = f"{players[0]} vs {players[1]}"
        try:
            result = service.events().list(
                calendarId=CALENDAR_ID,
                q=fragment,
                timeMin=(start - timedelta(hours=24)).isoformat(),
                timeMax=(start + timedelta(hours=24)).isoformat(),
                singleEvents=True,
            ).execute()
            for item in result.get("items", []):
                if fragment in item.get("summary", "") and item.get("status") != "cancelled":
                    print(f"  ⏭️  依名稱查到已存在：{fragment}")
                    return True
        except Exception:
            pass
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

    if event_exists(service, event_id, players, start):
        print(f"  ⏭️  已存在：{players[0]} vs {players[1]}")
        return False

    cat_code = "MS" if cat == "男單" else "WS"
    title    = f"🎾 [{round_}] {cat_code} · {players[0]} vs {players[1]}"
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

    # ESPN Sports Core API — ATP 男單 + WTA 女單，不被 GitHub Actions 封鎖
    print("\n📡 ESPN Sports Core API（ATP 男單 + WTA 女單）...")
    matches = fetch_espn_sports_core()
    print(f"   → 找到 {len(matches)} 場")

    if not matches:
        print("\n⚠️  本次無可用賽程資料")
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
