# txf-sim — 台指期 Shioaji 模擬連線（Zeabur）

目的：驗證 Zeabur 上能不能穩定跑 Shioaji 模擬環境、收台指期即時行情、組 1 分 K、跑策略出訊號。
**這一版不下單**，只記錄訊號。

## 部署

1. 整個資料夾推到 GitHub repo。
2. Zeabur → 新專案 → 從 GitHub 部署（會自動用 Dockerfile）。
3. Zeabur 環境變數填：

| 變數 | 說明 |
|---|---|
| `SHIOAJI_API_KEY` | 必填 |
| `SHIOAJI_SECRET_KEY` | 必填 |
| `CONTRACT_CODE` | `TXF` 大台 / `MXF` 小台，預設 TXF |
| `SIMULATION` | 先固定 `true` |
| `WARMUP_DAYS` | 啟動回補幾天 1 分 K，預設 3 |
| `FAST_MA` / `SLOW_MA` | 示範均線參數，預設 5 / 20 |
| `STALE_SECONDS` | 盤中幾秒沒 tick 就重連，預設 180 |

4. 綁網域後打開首頁就是儀表板。`/health` 給 Zeabur 健康檢查用。

## 你會看到什麼

- 登入是否成功、帳號 `signed` 狀態（期貨帳號 signed=True 才代表 API 下單資格通過）
- 合約自動抓近月連續（`TXFR1`），顯示結算日
- 暖機回補的歷史 1 分 K + 即時 tick 組出的 1 分 K
- 示範策略（均線交叉）的訊號與虛擬部位損益
- 流量用量（Shioaji 有每日流量上限）、登入次數、事件紀錄

## 驗證重點

1. 非交易時段登入成功、合約找得到、暖機有資料 → 環境 OK
2. 盤中 tick 有持續進來、1 分 K 時間對得上 → 行情 OK
3. 放一整天看事件紀錄有沒有斷線/重連、Zeabur 有沒有自己重啟容器 → 穩定性 OK
4. 對照 MultiCharts 同一分鐘的 K 線是否一致

## 版本注意

`requirements.txt` 鎖 `shioaji>=1.7,<2`。1.7 是 Rust 重寫版，介面跟舊版不同（回呼掛在 `api` 上、tick 回呼只有一個參數、登入後要另外 `fetch_contracts()`），`engine.py` 兩種都能跑，會自動偵測。若之後想沿用舊專案的 1.5.x，改 requirements 即可。

## 已知限制（下一步再處理）

- 狀態全在記憶體，容器重啟就清空（之後接 DB）
- 只有一個示範策略，MultiCharts 邏輯要搬進 `app/strategy.py` 的 `on_bar_close()`
- 重連有 5 分鐘保護間隔，避免撞每日登入次數上限
- 沒有換月邏輯（R1 由 Shioaji 自動切，但持倉跨月要自己處理）
- 沒有下單、風控、告警

## 檔案

```
Dockerfile
requirements.txt
app/main.py        入口
app/engine.py      Shioaji 登入 / 訂閱 / 組 K / 看門狗
app/strategy.py    策略（改這裡）
app/state.py       共享狀態、交易時段判斷
app/server.py      HTTP API
app/dashboard.html 儀表板
```
