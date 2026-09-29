"""MultiCharts 匯出的 1 分 K（ASCII/CSV）解析。

格式："Symbol","Date","Time","Open","High","Low","Close","TotalVolume"
      TXF1,1998/7/22,09:01:00,7950.000000,...,9
MultiCharts 的時間是 K 棒「結束」時間；資料庫存「開始」時間，所以預設減一分鐘。
只保留台指期交易時段內的 K（日盤 08:45–13:45、夜盤 15:00–05:00）。
"""
from datetime import datetime, timedelta

from .tf import session_of


def parse_chunk(text, time_label="end"):
    """回 (rows, stats)。rows = [(ts, o, h, l, c, v)]；stats 用來回報與偵測時間標記是否選錯。"""
    rows, bad, off, first, last = [], 0, 0, None, None
    suspicious = 0          # 轉換後落在 08:44 / 14:59 這種時段邊界外一分鐘：通常代表時間標記選錯
    shift = timedelta(minutes=1) if time_label == "end" else timedelta(0)
    for line in text.splitlines():
        line = line.strip()
        if not line or "Date" in line[:40]:          # 空行或標題列
            continue
        parts = line.split(",")
        if len(parts) < 8:
            bad += 1
            continue
        try:
            d = datetime.strptime(parts[1].strip() + " " + parts[2].strip(), "%Y/%m/%d %H:%M:%S") - shift
            o, h, l, c = (float(x) for x in parts[3:7])
            v = int(float(parts[7]))
        except Exception:
            bad += 1
            continue
        hm = d.hour * 100 + d.minute
        if hm in (844, 1459):
            suspicious += 1
        if session_of(d) is None:
            off += 1
            continue
        ts = d.strftime("%Y-%m-%d %H:%M")
        rows.append((ts, o, h, l, c, v))
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
    return rows, {"rows": len(rows), "bad": bad, "off_session": off, "suspicious": suspicious,
                  "first": first, "last": last}
