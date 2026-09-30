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

## 策略（多策略框架）

`app/strategy_config.json`：每支策略的 enabled / mode / minutes / lots / inputs，改完整包推上去。
`mode: "paper"` = 實驗策略：照跑帳本、訊號、Telegram，但不計入送券商的淨部位；要上線改 `"live"`。
新策略：在 `app/strategies.py` 加一個 `Strategy` 子類別（實作 `on_bar`，並加進 `REGISTRY`），config 加一段即可。
`app/strategies.py`：四支從 MultiCharts 移植的策略，逐行對照原 PowerLanguage。
`app/el.py`：PowerLanguage 語意層（next bar stop/limit 掛單、逐 tick 觸價、maxpositionprofit、EntriesToday、setexitonclose、CheckDay）。
`app/tf.py`：1 分 K → N 分 K / 時段 / 交易日 / 週，台指時段對齊。
`app/portfolio.py`：各策略獨立帳本，淨部位才送下單層；啟動時用歷史 1 分 K 重播（同 MC 重算圖表）。

啟動需要足夠歷史：`WARMUP_DAYS=45`（AvgRange(200) 在 60 分 K 約需 11 個交易日，週高低需要跨週）。

## 儀表板設定頁

「設定」分頁可改每支策略的啟用 / live-paper / K 線週期 / 口數 / 全部參數（含中文說明），儲存後寫進 DB、立刻重載並重播，不用推程式。
DB 裡的設定優先於 `strategy_config.json`。設 `DASHBOARD_PASSWORD` 後，儲存與操作按鈕都要先在設定頁登入。

## 持久化 / 下單 / 告警

**DB**：Zeabur 專案加一個 PostgreSQL 服務，把它的連線字串填到 `DATABASE_URL`。
存 K 線、訊號、事件、委託、成交、部位與風控狀態；容器重啟後自動還原。
不填則用 SQLite（`DATA_DIR`，沒掛 volume 會落在 /tmp，重啟就沒了）。

**下單** `MODE`：
- `signal`：只記錄訊號、算虛擬部位（預設）
- `sim`：真的送單到 Shioaji 模擬環境，市價 IOC、自動新平倉。需 `SIMULATION=true`
- `live`：正式下單。需 `SIMULATION=false`、`LIVE_CONFIRM=YES`、`CA_PFX_BASE64`/`CA_PASSWORD`/`PERSON_ID`

策略只呼叫 `BROKER.set_target(+1/-1/0, price, reason)`，下單層負責：
- 算差額口數、送單、追蹤回報（送出 → 成交 / 取消 / 失敗）
- IOC 沒成交重送一次；連續 `MAX_ORDER_FAILURES` 次失敗就停策略（儀表板按「恢復」）
- 每分鐘跟券商對帳，不一致告警並以券商為準（`RECONCILE_ADOPT`）
- 風控：kill switch、`DAILY_LOSS_LIMIT_PTS` 單日虧損上限（達到就平倉並鎖到隔天）、`MAX_POSITION`、非交易時段不送單、`FLAT_AT_DAY_CLOSE` 日盤收盤前平倉
- 儀表板按鈕：Kill switch / 全部平倉 / 恢復 / 立即對帳
- 保證金：盤中每 5 分鐘查帳戶保證金（`api.margin`）；另外每 `MARGIN_TABLE_REFRESH_MIN` 分鐘抓期交所 OpenAPI 的每口原始／維持保證金，存進 DB，數字變動就推播並記在儀表板「最近調整」。期交所 API 只給目前生效的數字，尚未生效的公告看不到
- 下單前保證金檢查（`MARGIN_CHECK`）：開倉口數 × 每口原始保證金 ×（1 + `MARGIN_BUFFER_PCT`%）大於可用保證金（同張單先平掉的口數會算回來）就不送單並通知，不計入委託失敗；策略帳上仍視為進場，出場訊號時帳戶本來就沒部位，不會多送單。券商收得比期交所高時用 `MARGIN_PER_LOT` 指定

**Telegram**：填 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，推播訊號、成交、委託失敗、斷線重連、對帳不一致、kill。

## 建議切換順序

1. `MODE=signal` + DB：確認重啟後部位/K 線有還原
2. `MODE=sim`：模擬環境真的送單，看委託表的狀態流轉、對帳是否一致、Telegram 有沒有收到
3. 策略搬完並跟 MultiCharts 比對一致後，才考慮 `live`

## 已知限制

- 換月：R1 會自動切到次月，但既有持倉在舊月合約，結算日前要手動處理（下一步做自動換月）
- 對帳只看單一合約代碼，若手上有其他月份或手動單會顯示不一致
- `live` 路徑沒有實測過，第一次上正式務必 `LOTS=1` 盯著看

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
app/broker.py      下單層：狀態機 / 風控 / 對帳 / kill switch
app/db.py          SQLite 或 PostgreSQL 持久化
app/notify.py      Telegram 推播
```

## 軌道鞅 v1（Orbit_cost30，paper 實驗）
- `app/orbit.py`：多空成本（LADDER=0 開盤法：時段首根 K 對前一時段收盤，階梯 = 前一時段收盤；LADDER=1 量倍數法）、大小流氓（20MA/40MA）、公道伯（VWAP，AVL_RESET 0=結算日、1=每月）計算，策略與圖表共用
- `app/strategies.py` 的 `OrbitCost30`：回測有守 / 頂底被破進場，階梯停損並隨新階梯移動，滿足點 TP_PCT% 停利
- `strategy_config.json` 預設 `mode: paper`，不計入淨部位、不下單
- 圖表頁 `/chart`（儀表板「圖表」分頁）：K 線 + 階梯 + 均線 + 公道伯 + 大量 K 標色 + 最近一次回測的進出場點；可切一般盤/合併盤、臨時調 VOLX/VOLN 校準
- 圖表頁下方「階梯變動」清單：每次階梯更新的時間、方向、價位，用來和 App 的紅/綠量柱逐根對照
