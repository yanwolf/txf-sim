"""MultiCharts 匯出的 1 分 K（ASCII/CSV）解析。

格式："Symbol","Date","Time","Open","High","Low","Close","TotalVolume"
      TXF1,1998/7/22,09:01:00,7950.000000,...,9
MultiCharts 的時間是 K 棒「結束」時間；資料庫存「開始」時間，所以預設減一分鐘。
只保留台指期交易時段內的 K（日盤 08:45–13:45、夜盤 15:00–05:00）。
"""
import io
import re
import urllib.request
import zipfile
from datetime import datetime, timedelta

from .tf import session_of

UA = {"User-Agent": "Mozilla/5.0 (compatible; txf-sim/1.0)"}


def direct_url(url):
    """把雲端硬碟的分享連結轉成直接下載網址。"""
    u = url.strip()
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", u) or re.search(r"drive\.google\.com/open\?id=([\w-]+)", u) \
        or re.search(r"drive\.google\.com/uc\?.*id=([\w-]+)", u)
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    if "dropbox.com" in u:
        base = u.split("?")[0]
        return base + "?dl=1"
    return u


def fetch(url, max_bytes=200 * 1024 * 1024):
    """下載檔案，回 (bytes, 檔名)。抓到 HTML 代表連結未公開或是下載頁面。"""
    req = urllib.request.Request(direct_url(url), headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        cd = r.headers.get("Content-Disposition") or ""
        name = (re.search(r'filename="?([^";]+)', cd) or [None, url.split("/")[-1][:60]])[1]
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"檔案超過 {max_bytes // 1024 // 1024}MB")
    if data[:2] != b"PK" and ("text/html" in ctype or data.lstrip()[:1] == b"<"):
        raise ValueError("下載到網頁而不是檔案：請把連結權限設為「知道連結的人都可以檢視」，或改用直接下載網址")
    return data, name


def iter_texts(data, name=""):
    """把下載到的資料轉成一段段文字：zip 會逐檔解開，純文字直接回傳。"""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.endswith("/") or n.startswith("__MACOSX"):
                    continue
                yield n, z.read(n).decode("utf-8", errors="replace")
    else:
        yield name or "檔案", data.decode("utf-8", errors="replace")


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


# ------------------------------------------------------------ 從永豐 Shioaji 補 1 分 K
def _norm_close_minute(dt):
    """跟 engine 一樣：13:45 / 05:00 的收盤撮合併入最後一根 1 分 K。"""
    dt = dt.replace(second=0, microsecond=0)
    if dt.hour == 13 and dt.minute == 45:
        return dt.replace(minute=44)
    if dt.hour == 5 and dt.minute == 0:
        return dt.replace(hour=4, minute=59)
    return dt


def shioaji_contract(api, code="TXF"):
    group = getattr(api.Contracts.Futures, code)
    c = getattr(group, f"{code}R1", None)
    if c is None and hasattr(group, "get"):
        c = group.get(f"{code}R1")
    if c is None:
        raise RuntimeError(f"找不到 {code}R1 連續合約（合約檔可能還沒下載完）")
    return c


def fetch_shioaji(api, start, end, code="TXF", progress=None, sleep=0.3):
    """用 api.kbars 抓 R1 連續月 1 分 K，轉成 hist_bars 的格式 [(ts,o,h,l,c,v)]，ts＝K 棒開始時間。
    轉換規則與即時暖機回補（engine._warmup）完全相同，確保跟系統其他資料對得上。"""
    import time as _t
    from datetime import date as _date
    c = shioaji_contract(api, code)
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    out, cs, n_chunk = {}, d0, 0
    while cs <= d1:
        ce = min(cs + timedelta(days=6), d1)
        k = api.kbars(c, start=cs.isoformat(), end=ce.isoformat())
        n = 0
        for ts, o, h, l, cl, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume):
            dt = _norm_close_minute(datetime.utcfromtimestamp(ts / 1e9) - timedelta(minutes=1))
            if session_of(dt) is None:
                continue
            key = dt.strftime("%Y-%m-%d %H:%M")
            b = out.get(key)
            if b:
                b[2] = max(b[2], float(h)); b[3] = min(b[3], float(l)); b[4] = float(cl); b[5] += int(v)
            else:
                out[key] = [key, float(o), float(h), float(l), float(cl), int(v)]
            n += 1
        n_chunk += 1
        if progress:
            progress(f"{cs} ~ {ce}：{n:,} 根", cs, ce)
        cs = ce + timedelta(days=1)
        _t.sleep(sleep)
    return [tuple(v) for _, v in sorted(out.items())]


def compare_bars(new_rows, old):
    """new_rows 與資料庫已有的 K（old: ts→bar）比對；同時試 −1／0／+1 分鐘位移，檢查時間標記是否一致。"""
    from datetime import datetime as _dt
    res = {}
    for shift in (-1, 0, 1):
        same = n = 0
        diffs = []
        for r in new_rows:
            t = (_dt.strptime(r[0], "%Y-%m-%d %H:%M") + timedelta(minutes=shift)).strftime("%Y-%m-%d %H:%M")
            b = old.get(t)
            if not b:
                continue
            n += 1
            d = abs(r[4] - b["close"])
            diffs.append(d)
            same += d < 1e-9
        res[shift] = {"n": n, "same_pct": round(same * 100 / n, 1) if n else None,
                      "mean_abs": round(sum(diffs) / n, 2) if n else None, "max_abs": max(diffs) if diffs else None}
    return res
