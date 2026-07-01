# PRD — 溫布頓 2026 重點賽程追蹤器

**版本**：v1.0  
**日期**：2026-07-01  
**作者**：Bella（黃于芹）  

---

## 1. 問題與目標

**問題**：溫布頓期間想追蹤特定選手的賽程，但每次要自己去查，容易錯過直播。

**目標**：把指定選手的賽程自動寫入 Google Calendar，讓台北時間的提醒直接出現在行事曆，不用手動查詢。

---

## 2. 功能需求

### F1 — 追蹤選手

| 組別 | 選手 |
|------|------|
| 男單 | Jannik Sinner、Carlos Alcaraz、Novak Djokovic、Alexander Zverev |
| 女單 | Aryna Sabalenka、Mirra Andreeva、Elena Rybakina、Naomi Osaka |

### F2 — 賽程範圍

- 賽事：2026 溫布頓錦標賽（Wimbledon Championships）
- 日期：6/22 – 7/12, 2026
- 包含：男單（MS）、女單（WS）所有輪次（含第一輪到決賽）

### F3 — Google Calendar 事件格式

```
標題：🎾 [輪次] MS/WS · 選手A vs 選手B
時間：台北時間（Asia/Taipei），預估時長 3 小時
地點：溫布頓 · 球場名稱
說明：賽事類別、輪次、選手名稱
```

輪次中文對照：第一輪 / 第二輪 / 第三輪 / 第四輪 / 四強賽 / 準決賽 / 決賽

### F4 — 自動更新

- 每 2 小時自動執行一次（GitHub Actions cron）
- 重複比賽不重複建立（MD5 dedup）
- 支援手動觸發（workflow_dispatch）

---

## 3. 非功能需求

- 不依賴本機電腦（全雲端）
- 費用：免費（GitHub Actions + Google Calendar API 免費額度）
- 時區：所有時間顯示為台北時間（UTC+8）

---

## 4. 範疇外

- 不追蹤男雙、女雙、混雙
- 不提供直播連結
- 不設行前提醒（靠 Google Calendar 本身的通知功能）
- 不追蹤以上 8 位以外的選手
