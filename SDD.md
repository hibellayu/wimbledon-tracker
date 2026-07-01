# SDD — 溫布頓 2026 重點賽程追蹤器系統設計

**版本**：v1.0  
**日期**：2026-07-01  

---

## 1. 架構總覽

```
GitHub Actions (每 2 小時)
    │
    ▼
tracker.py
    │
    ├─ 主要來源：ESPN JSON API
    │      site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard
    │      site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard
    │
    ├─ 備用來源：BBC Sport HTML（BeautifulSoup 解析）
    │
    └─ Google Calendar API（Service Account 認證）
           └─ 寫入指定 Calendar
```

---

## 2. 資料流

1. ESPN API → JSON → 篩選 Wimbledon 事件 → 篩選追蹤選手
2. 比賽時間 UTC → 台北時間（ZoneInfo Asia/Taipei）
3. 產生 MD5 event_id（players + date + category）
4. 查詢 Calendar 是否已存在該 event_id
5. 不存在 → 建立事件；已存在 → 跳過

---

## 3. 元件設計

### tracker.py

| 函數 | 說明 |
|------|------|
| `fetch_espn_schedule()` | 從 ESPN JSON API 抓取賽程 |
| `parse_espn_match()` | 解析單場比賽資料，過濾追蹤選手 |
| `fetch_bbc_schedule()` | BBC Sport HTML 備援抓取 |
| `translate_round()` | 英文輪次 → 中文 |
| `make_event_id()` | MD5 去重 ID |
| `get_calendar_service()` | Service Account 認證 Google Calendar API |
| `event_exists()` | 查詢事件是否已存在 |
| `create_calendar_event()` | 建立 Calendar 事件 |

### GitHub Actions Workflow

- Trigger：`cron: '0 */2 * * *'`（每 2 小時）+ `workflow_dispatch`
- Python 3.12
- 套件：`requests beautifulsoup4 google-auth google-api-python-client`
- Secrets：`GOOGLE_SERVICE_ACCOUNT_JSON`、`GOOGLE_CALENDAR_ID`

---

## 4. 認證設計

- Google Calendar API 使用 **Service Account**（非 OAuth，適合無人值守執行）
- JSON 金鑰存於 GitHub Secrets（`GOOGLE_SERVICE_ACCOUNT_JSON`）
- 用戶需將 Google Calendar 分享給 Service Account email（Editor 權限）

---

## 5. 時區處理

- ESPN API 時間格式：UTC（Z 結尾）
- 轉換：`datetime.fromisoformat(...).astimezone(ZoneInfo("Asia/Taipei"))`
- Calendar 事件 timeZone 欄位：`"Asia/Taipei"`

---

## 6. 去重機制

```python
key = f"wimbledon2026-{sorted_players}-{YYYYMMDD}-{category}"
event_id = "wb" + md5(key)[:12]
```

每次執行前先查詢 `events().get()`，已存在則跳過，避免重複事件。
