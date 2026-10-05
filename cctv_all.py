import base64
import datetime
import hashlib
import json
import re
import secrets
import time
import threading
from urllib.parse import quote, unquote, urljoin, urlparse
import requests
from fastapi import FastAPI, Query
from fastapi.responses import Response, RedirectResponse
from typing import Optional
from bs4 import BeautifulSoup

# ====================== 全局配置 ======================
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
LOGO_URL = "https://epg.112114.xyz/logo/{}.png"
REQUEST_TIMEOUT = 8
EPG_CACHE_SECONDS = 60 * 60

# ---------- 央视网 tv.cctv.com 配置 ----------
CCTV_HOST = "https://tv.cctv.com"
CCTV_CATALOG_URL = CCTV_HOST + "/live/"
CCTV_PLAY_URL = "https://vdnx.live.cntv.cn/api/v3/vdn/live"
CCTV_PLAY_SECRET = "a4220a71b31746908fa3e7fdd7a6852a"
CCTV_CHANNEL_PATTERN = re.compile(
    r"^https?://tv\.cctv\.com/live/"
    r"(cctv(?:\d+|5plus|jilu|child|europe|america))/",
    re.IGNORECASE,
)
CCTV_CHANNEL_ID_PATTERN = re.compile(
    r"^cctv(?:\d+|5plus|jilu|child|europe|america)$",
    re.IGNORECASE,
)
CCTV_LOGO_NAMES = {
    "cctv5plus": "CCTV5+",
    "cctvjilu": "CCTV9",
    "cctvchild": "CCTV14",
    "cctveurope": "CCTV4欧洲",
    "cctvamerica": "CCTV4美洲",
}

# ---------- 央视频 yangshipin.cn 配置 ----------
YSP_HOST = "https://www.yangshipin.cn"
YSP_PLAY_API = "https://api.yangshipin.cn/v1/getPlayInfo"
YSP_EPG_URL = "https://api.cntv.cn/epg/getEpgInfoByChannelNew"

# ---------- 共用EPG接口 ----------
EPG_URL = "https://api.cntv.cn/epg/getEpgInfoByChannelNew"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ====================== 内存缓存 ======================
cache = {}
cache_lock = threading.Lock()

def cache_get(key):
    with cache_lock:
        item = cache.get(key)
        if not item:
            return None
        expire_ts, data = item
        if time.time() > expire_ts:
            del cache[key]
            return data

def cache_set(key, ttl_sec, data):
    with cache_lock:
        expire_ts = time.time() + ttl_sec
        cache[key] = (expire_ts, data)

# ====================== 央视频签名工具（纯Python复刻） ======================
def ysp_sign(params: dict) -> str:
    sorted_items = sorted(params.items())
    raw = "".join([f"{k}{v}" for k, v in sorted_items])
    raw += "123456789abcdefg"
    return hashlib.md5(raw.encode("utf8")).hexdigest()

def ysp_build_pb(channel_id: str) -> bytes:
    buf = bytearray()
    buf.append(0x0A)
    buf.append(len(channel_id))
    buf.extend(channel_id.encode("utf8"))
    buf.append(0x10)
    buf.append(0x01)
    buf.append(0x18)
    buf.append(0x00)
    return bytes(buf)

# ====================== 央视网爬虫类（改用BeautifulSoup，无lxml依赖） ======================
class CCTVWeb:
    def __init__(self):
        self.uid = base64.b64encode(secrets.token_bytes(18)).decode("ascii")

    def load_channels(self, snapshot_url: Optional[str] = None):
        try:
            return self._fetch_channels()
        except Exception as error:
            print("央视网 catalog fallback: {}".format(error))
            if snapshot_url:
                return self._load_snapshot(snapshot_url)
            raise

    def _fetch_channels(self):
        headers = {"Referer": CCTV_HOST + "/", "User-Agent": USER_AGENT}
        resp = requests.get(CCTV_CATALOG_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        channels = []
        seen = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(CCTV_CATALOG_URL, anchor.get("href", ""))
            match = CCTV_CHANNEL_PATTERN.match(href)
            if not match:
                continue
            channel_id = match.group(1).lower()
            if channel_id in seen:
                continue
            name = " ".join(anchor.get_text().split())
            if not name:
                continue
            seen.add(channel_id)
            channels.append({"id": f"cctv:{channel_id}", "name": name, "raw_id": channel_id})
        if len(channels) < 18:
            raise ValueError("incomplete channel list")
        return channels

    def _load_snapshot(self, url):
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
        elif parsed.scheme in ("", "file"):
            path = unquote(parsed.path if parsed.scheme else url)
            if len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            with open(path, encoding="utf-8") as stream:
                result = json.load(stream)
        else:
            raise ValueError("invalid snapshot url")
        channels = result.get("channels")
        if not isinstance(channels, list) or not channels:
            raise ValueError("invalid channel snapshot")
        out = []
        for ch in channels:
            cid = ch["id"]
            out.append({"id": f"cctv:{cid}", "name": ch["name"], "raw_id": cid})
        return out

    def resolve(self, raw_channel_id):
        if not isinstance(raw_channel_id, str) or not CCTV_CHANNEL_ID_PATTERN.fullmatch(raw_channel_id):
            raise ValueError("invalid channel id")
        timestamp = int(time.time() * 1000)
        nonce = secrets.randbelow(901) + 100
        digest = hashlib.md5(
            "{}{}{}{}".format(raw_channel_id, timestamp, nonce, CCTV_PLAY_SECRET).encode("utf-8")
        ).hexdigest()
        auth_key = "{}-{}-{}".format(timestamp, nonce, digest)
        params = {
            "channel": raw_channel_id,
            "vn": "1",
            "pdrm": "1",
            "uid": self.uid,
            "hbss": str(timestamp),
        }
        headers = {
            "auth-key": auth_key,
            "Origin": CCTV_HOST,
            "Referer": CCTV_HOST + "/",
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = requests.get(CCTV_PLAY_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ack") != "yes":
            raise ValueError("play api rejected channel")
        manifest = result.get("manifest") or {}
        backup = result.get("backup") or {}
        location = manifest.get("hls_cdrm") or backup.get("hls_cdrm")
        if not isinstance(location, str) or not location.startswith(("http://", "https://")):
            raise ValueError("missing hls manifest")
        return location

    def load_epg(self, raw_channel_id, date):
        cache_key = f"epg:cctv:{raw_channel_id}:{date}"
        cached_data = cache_get(cache_key)
        if cached_data:
            return cached_data
        if not isinstance(raw_channel_id, str) or not CCTV_CHANNEL_ID_PATTERN.fullmatch(raw_channel_id):
            raise ValueError("invalid channel id")
        if not isinstance(date, str) or not DATE_PATTERN.fullmatch(date):
            raise ValueError("invalid date")
        params = {
            "c": raw_channel_id,
            "serviceId": "tvcctv",
            "d": date.replace("-", ""),
        }
        headers = {"Referer": CCTV_HOST + "/", "User-Agent": USER_AGENT}
        resp = requests.get(EPG_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        data = result.get("data")
        channel = data.get(raw_channel_id) if isinstance(data, dict) else None
        rows = channel.get("list") if isinstance(channel, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid epg response")
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            try:
                start = datetime.datetime.fromtimestamp(int(row.get("startTime")), TAIPEI).strftime("%H:%M:%S")
                end = datetime.datetime.fromtimestamp(int(row.get("endTime")), TAIPEI).strftime("%H:%M:%S")
            except (TypeError, ValueError, OverflowError):
                continue
            items.append({"title": title, "start": start, "end": end})
        epg_json = json.dumps({"date": date, "epg_data": items}, ensure_ascii=False, separators=(",", ":"))
        cache_set(cache_key, EPG_CACHE_SECONDS, epg_json)
        return epg_json

# ====================== 央视频爬虫类（完整可播放） ======================
class YangShiPin:
    def __init__(self):
        pass

    def load_channels(self):
        channels = [
            {"id":"ysp:cctv1","name":"CCTV-1综合","raw_id":"cctv1"},
            {"id":"ysp:cctv2","name":"CCTV-2财经","raw_id":"cctv2"},
            {"id":"ysp:cctv3","name":"CCTV-3综艺","raw_id":"cctv3"},
            {"id":"ysp:cctv4","name":"CCTV-4中文国际","raw_id":"cctv4"},
            {"id":"ysp:cctv5","name":"CCTV-5体育","raw_id":"cctv5"},
            {"id":"ysp:cctv5plus","name":"CCTV-5+体育赛事","raw_id":"cctv5plus"},
            {"id":"ysp:cctv6","name":"CCTV-6电影","raw_id":"cctv6"},
            {"id":"ysp:cctv7","name":"CCTV-7国防军事","raw_id":"cctv7"},
            {"id":"ysp:cctv8","name":"CCTV-8电视剧","raw_id":"cctv8"},
            {"id":"ysp:cctv9","name":"CCTV-9纪录","raw_id":"cctv9"},
            {"id":"ysp:cctv10","name":"CCTV-10科教","raw_id":"cctv10"},
            {"id":"ysp:cctv11","name":"CCTV-11戏曲","raw_id":"cctv11"},
            {"id":"ysp:cctv12","name":"CCTV-12社会与法","raw_id":"cctv12"},
            {"id":"ysp:cctv13","name":"CCTV-13新闻","raw_id":"cctv13"},
            {"id":"ysp:cctv14","name":"CCTV-14少儿","raw_id":"cctv14"},
            {"id":"ysp:cctv15","name":"CCTV-15音乐","raw_id":"cctv15"},
        ]
        return channels

    def resolve(self, raw_id):
        ts = str(int(time.time() * 1000))
        pb_data = ysp_build_pb(raw_id)
        pb_b64 = base64.b64encode(pb_data).decode()
        params = {
            "data": pb_b64,
            "timestamp": ts
        }
        sign = ysp_sign(params)
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": YSP_HOST,
            "Origin": YSP_HOST,
            "sign": sign,
            "timestamp": ts
        }
        resp = requests.post(YSP_PLAY_API, json=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        res_json = resp.json()
        if res_json.get("code") != 0:
            raise Exception(f"央视频接口错误:{res_json.get('msg')}")
        data = res_json.get("data", {})
        play_info = data.get("playInfo", {})
        m3u8_url = play_info.get("hlsUrl")
        if not m3u8_url:
            raise Exception("央视频未获取到hls地址")
        return m3u8_url

    def load_epg(self, raw_channel_id, date):
        cache_key = f"epg:ysp:{raw_channel_id}:{date}"
        cached_data = cache_get(cache_key)
        if cached_data:
            return cached_data
        if not isinstance(raw_channel_id, str) or not DATE_PATTERN.fullmatch(date):
            raise ValueError("invalid channel id")
        params = {
            "c": raw_channel_id,
            "serviceId": "tvcctv",
            "d": date.replace("-", ""),
        }
        headers = {"Referer": YSP_HOST + "/", "User-Agent": USER_AGENT}
        resp = requests.get(EPG_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        data = result.get("data")
        channel = data.get(raw_channel_id) if isinstance(data, dict) else None
        rows = channel.get("list") if isinstance(channel, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid epg response")
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            try:
                start = datetime.datetime.fromtimestamp(int(row.get("startTime")), TAIPEI).strftime("%H:%M:%S")
                end = datetime.datetime.fromtimestamp(int(row.get("endTime")), TAIPEI).strftime("%H:%M:%S")
            except (TypeError, ValueError, OverflowError):
                continue
            items.append({"title": title, "start": start, "end": end})
        epg_json = json.dumps({"date": date, "epg_data": items}, ensure_ascii=False, separators=(",", ":"))
        cache_set(cache_key, EPG_CACHE_SECONDS, epg_json)
        return epg_json

# ====================== 实例化 ======================
app = FastAPI(title="CCTV All Live API")
cctv_web = CCTVWeb()
ysp = YangShiPin()

# ====================== API路由 ======================
@app.get("/channels")
def get_channels(cctv_snapshot: Optional[str] = None):
    cctv_chs = cctv_web.load_channels(cctv_snapshot)
    ysp_chs = ysp.load_channels()
    return {
        "央视网": cctv_chs,
        "央视频": ysp_chs
    }

@app.get("/play")
def play(id: str):
    source, raw_id = id.split(":", 1)
    if source == "cctv":
        m3u8_url = cctv_web.resolve(raw_id)
        return RedirectResponse(m3u8_url)
    elif source == "ysp":
        m3u8_url = ysp.resolve(raw_id)
        return RedirectResponse(m3u8_url)
    else:
        raise ValueError("unknown source")

@app.get("/epg")
def epg(id: str, date: str = Query(default=datetime.date.today().isoformat())):
    source, raw_id = id.split(":", 1)
    if source == "cctv":
        epg_data = cctv_web.load_epg(raw_id, date)
    elif source == "ysp":
        epg_data = ysp.load_epg(raw_id, date)
    else:
        raise ValueError("unknown source")
    return Response(content=epg_data, media_type="application/json")

@app.get("/m3u")
def generate_m3u(cctv_snapshot: Optional[str] = None):
    cctv_chs = cctv_web.load_channels(cctv_snapshot)
    ysp_chs = ysp.load_channels()
    m3u_lines = ["#EXTM3U"]

    # 央视网分组
    m3u_lines.append('## 央视网频道组')
    for ch in cctv_chs:
        cid = ch["id"]
        raw_id = ch["raw_id"]
        name = ch["name"]
        logo_name = CCTV_LOGO_NAMES.get(raw_id, raw_id.upper())
        logo = LOGO_URL.format(quote(logo_name, safe=""))
        m3u_lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" tvg-logo="{logo}" group-title="央视频道(央视网)",{name}')
        m3u_lines.append(f"{app.url_path_for('play')}?id={cid}")

    # 央视频分组
    m3u_lines.append('\n## 央视频频道组')
    for ch in ysp_chs:
        cid = ch["id"]
        raw_id = ch["raw_id"]
        name = ch["name"]
        logo = LOGO_URL.format(quote(raw_id.upper(), safe=""))
        m3u_lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-name="{name}" tvg-logo="{logo}" group-title="央视频(央视频)",{name}')
        m3u_lines.append(f"{app.url_path_for('play')}?id={cid}")

    m3u_text = "\n".join(m3u_lines)
    return Response(content=m3u_text, media_type="application/x-mpegurl")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("cctv_all:app", host="0.0.0.0", port=8000, reload=False)
