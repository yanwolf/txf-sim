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
