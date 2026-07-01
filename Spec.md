# Spec — 溫布頓 2026 重點賽程追蹤器執行規格

**版本**：v1.0  
**日期**：2026-07-01  

---

## 執行步驟

### S1 — 建立 Google Cloud 專案 ✅
- 執行者：貝拉
- 在 Google Cloud Console 建立專案 `wimbledon-tracker`

### S2 — 啟用 Google Calendar API ✅
- 執行者：貝拉
- 在專案中啟用 Google Calendar API

### S3 — 建立 Service Account ✅
- 執行者：貝拉
- 名稱：`wimbledon-tracker`
- Email：`wimbledon-tracker@wimbledon-tracker.iam.gserviceaccount.com`
- 下載 JSON 金鑰（`wimbledon-tracker-976dfce2ccfd.json`）

### S4 — 分享 Google Calendar ☐
- 執行者：貝拉
- 開啟 Google Calendar 設定
- 找到要使用的日曆 → 「與特定人員共用」
- 新增：`wimbledon-tracker@wimbledon-tracker.iam.gserviceaccount.com`
- 權限：**「進行變更」（Editor）**
- 取得 Calendar ID（設定頁底部，格式如 `xxxx@group.calendar.google.com` 或 `primary`）

### S5 — 建立 GitHub Repo 並推送程式碼 ✅（由 Luca 執行）
- Repo：`hibellayu/wimbledon-tracker`（公開）
- 包含：`tracker.py`、`.github/workflows/wimbledon-tracker.yml`、`PRD.md`、`SDD.md`、`Spec.md`

### S6 — 設定 GitHub Secrets ✅（由 Luca 執行）
- `GOOGLE_SERVICE_ACCOUNT_JSON`：Service Account JSON 金鑰內容
- `GOOGLE_CALENDAR_ID`：使用者的 Calendar ID（S4 取得後設定）

### S7 — 驗證首次執行
- 執行者：Luca
- GitHub Actions → 手動觸發 `wimbledon-tracker` workflow
- 確認 Actions log 顯示找到比賽並新增到 Calendar
- 確認 Google Calendar 中出現 🎾 開頭的事件

---

## 驗證指令

```bash
# 本機測試（需設定環境變數）
export GOOGLE_SERVICE_ACCOUNT_JSON='...'
export GOOGLE_CALENDAR_ID='primary'
cd wimbledon-tracker && python tracker.py
```
