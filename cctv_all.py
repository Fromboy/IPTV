#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iptv-ysp-combo v1: IPTV静态源 + 央视频双源直播代理
双模式:
- IPTV: 静态m3u8链接，优先使用
- YSP: bkliveinfo + JCE时移协议，IPTV失效自动切换备用
播放器直连CDN拉分片，本机只下发清单，不跑视频流量。
仅标准库, 无第三方依赖。
"""
import argparse
import base64
import gzip
import json
import os
import random
import re
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ================================================================ JCE 协议 (原样复用 ysp-live)
class W:
    def __init__(self): self.b = bytearray()
    def head(self, typ, tag):
        if tag < 15: self.b.append(((tag & 0xf) << 4) | (typ & 0xf))
        else: self.b.append(0xf0 | (typ & 0xf)); self.b.append(tag)
    def byte(self, v, tag):
        v = int(v)
        if v == 0: self.head(12, tag)
        else: self.head(0, tag); self.b += struct.pack('>b', v)
    def short(self, v, tag):
        v = int(v)
        if -128 <= v <= 127: self.byte(v, tag)
        else: self.head(1, tag); self.b += struct.pack('>h', v)
    def int(self, v, tag):
        v = int(v)
        if -32768 <= v <= 32767: self.short(v, tag)
        else: self.head(2, tag); self.b += struct.pack('>i', v)
    def long(self, v, tag):
        v = int(v)
        if -2147483648 <= v <= 2147483647: self.int(v, tag)
        else: self.head(3, tag); self.b += struct.pack('>q', v)
    def float(self, v, tag): self.head(4, tag); self.b += struct.pack('>f', float(v))
    def double(self, v, tag): self.head(5, tag); self.b += struct.pack('>d', float(v))
    def string(self, s, tag):
        if s is None: return
        data = str(s).encode('utf-8')
        if len(data) > 255: self.head(7, tag); self.b += struct.pack('>i', len(data)); self.b += data
        else: self.head(6, tag); self.b.append(len(data)); self.b += data
    def bytes(self, data, tag):
        data = bytes(data); self.head(13, tag); self.head(0, 0); self.int(len(data), 0); self.b += data
    def struct(self, fn, tag): self.head(10, tag); fn(self); self.head(11, 0)
    def list(self, items, tag, wf): self.head(9, tag); self.int(len(items), 0)
    def out(self): return bytes(self.b)

class R:
    def __init__(self, data): self.d = memoryview(data); self.p = 0
    def rem(self): return len(self.d) - self.p
    def get(self, n):
        if self.p + n > len(self.d): raise EOFError
        b = self.d[self.p:self.p + n].tobytes(); self.p += n; return b
    def u8(self): return self.get(1)[0]
    def head(self):
        b = self.u8(); typ = b & 0xf; tag = (b & 0xf0) >> 4
        if tag == 15: tag = self.u8()
        return typ, tag
    def value(self, typ):
        if typ == 0: return struct.unpack('>b', self.get(1))[0]
        if typ == 1: return struct.unpack('>h', self.get(2))[0]
        if typ == 2: return struct.unpack('>i', self.get(4))[0]
        if typ == 3: return struct.unpack('>q', self.get(8))[0]
        if typ == 4: return struct.unpack('>f', self.get(4))[0]
        if typ == 5: return struct.unpack('>d', self.get(8))[0]
        if typ == 6: n = self.u8(); return self.get(n).decode('utf-8', 'replace')
        if typ == 7: n = struct.unpack('>i', self.get(4))[0]; return self.get(n).decode('utf-8', 'replace')
        if typ == 8: n = self._int(); return {self._fv(): self._fv() for _ in range(n)}
        if typ == 9: n = self._int(); return [self._fv() for _ in range(n)]
        if typ == 10: return self.struct()
        if typ == 11: return None
        if typ == 12: return 0
        if typ == 13: t, _ = self.head(); n = self._int(); return self.get(n)
        raise ValueError('type %d' % typ)
    def _fv(self): t, _ = self.head(); return self.value(t)
    def _int(self): t, _ = self.head(); return int(self.value(t))
    def struct(self):
        m = {}
        while self.rem() > 0:
            t, tag = self.head()
            if t == 11: break
            m[tag] = self.value(t)
        return m

VER_NAME, VER_CODE = '3.2.7.26212', '302070'
APP_ID, QMF_APP_ID, QMF_PLATFORM, BIZ_ID = '1200013', 10012, 1, 0
CHAN_ID = '10070'
GUID = ''.join(random.choice('0123456789abcdef') for _ in range(32))

def _qua(w):
    w.string(VER_NAME, 0); w.string(VER_CODE, 1)
    w.int(1080, 2); w.int(2400, 3); w.int(3, 4); w.string('12', 5)
    w.int(1, 6); w.int(1, 7); w.int(420, 8); w.string(CHAN_ID, 9)
    for i in range(10, 15): w.string('', i)
    w.struct(lambda ww: (ww.int(0, 0), ww.byte(0, 1), ww.string('', 2)), 15)
    w.string('', 16); w.string('', 17); w.string('', 18)
    w.struct(lambda ww: (ww.int(0, 0), ww.float(0, 1), ww.float(0, 2), ww.double(0, 3)), 19)
    w.string(GUID[:16], 20); w.string('Pixel 6', 21)
    w.int(1, 22)
    for i in range(23, 27): w.int(0, i)
    w.string('', 27); w.string('', 28); w.string(GUID, 29)

def _head(w, cmd, reqid):
    w.int(reqid, 0); w.int(cmd, 1)
    w.struct(lambda ww: _qua(ww), 2)
    w.string(APP_ID, 3); w.string(GUID, 4)
    w.list([], 5, None); w.struct(lambda ww: None, 6)
    w.list([], 7, None)
    w.int(0, 8); w.int(0, 9); w.int(0, 10)

def _wrap(cmd, body, reqid):
    w = W()
    w.struct(lambda ww: _head(ww, cmd, reqid), 0)
    w.bytes(body, 1)
    reqcmd = w.out()
    inner = bytearray([38]) + struct.pack('>i', len(reqcmd) + 17) + bytes([1]) + b'\x00' * 10 + reqcmd + bytes([40])
    comp = gzip.compress(bytes(inner))
    out = bytearray([19]) + struct.pack('>i', 0) + struct.pack('>H', 2) + struct.pack('>H', 65281)
    out += struct.pack('>H', cmd) + struct.pack('>H', 0) + struct.pack('>q', reqid)
    out += struct.pack('>i', 531) + struct.pack('>i', QMF_APP_ID) + struct.pack('>q', BIZ_ID)
    g = GUID.encode()[:32]; out += g + b'\x00' * (32 - len(g))
    out += struct.pack('>b', QMF_PLATFORM) + struct.pack('>i', int(VER_CODE)) + b'\x00' * 6
    out += bytes([0]) + struct.pack('>H', 0) + struct.pack('>H', 0)
    out += struct.pack('>i', len(inner)) + comp + bytes([3])
    struct.pack_into('>i', out, 1, len(out))
    return bytes(out)

def _unwrap(data):
    if data[:1] != b'\x13' or len(data) < 90: return None
    flags = struct.unpack('>i', data[21:25])[0]
    payload = data[89:-1]
    if flags & 2: payload = gzip.decompress(payload)
    if payload[:1] != b'&' or payload[-1:] != b'(': return None
    rc = R(payload[16:-1]).struct()
    return rc.get(1) or b''

class DeadHostError(RuntimeError):
    pass

def jce_timeshift_url(pid, sid, start, end, stream='fhd'):
    w = W()
    w.string(pid, 0); w.string(sid, 1); w.long(start, 2); w.long(end, 3); w.string(stream, 4)
    body = w.out()
    CMD = 25312
    reqid = int(time.time() * 1000) & 0x7fffffff
    packet = _wrap(CMD, body, reqid)
    req = urllib.request.Request('https://jacc.ysp.cctv.cn', data=packet, method='POST')
    req.add_header('Content-Type', 'application/octet-stream')
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    resp_body = _unwrap(raw)
    if not resp_body: raise RuntimeError('bad response')
    m = R(resp_body).struct()
    err = m.get(0, 0)
    if err != 0: raise RuntimeError(m.get(1, 'errCode=%s' % err))
    url = m.get(2, '')
    if not url: raise RuntimeError('empty m3u8')
    if 'liverecord.video.cloud.cctv.com' in url:
        raise DeadHostError('dead cdn host')
    return url

# ================================================================ cKey + bkliveinfo
_CK_PLATFORM = 4330403
_CK_APPVER = 'V8.22.1035.3031'
_CK_TEA = bytes.fromhex('59b2f7cf725ef43c34fdd7c123411ed3')
_CK_GTEA = bytes.fromhex('110DBEC10C23E7D2E56A1CAD6914EF1B')
_CK_XOR = bytes([0x84, 0x2e, 0xed, 0x08, 0xf0, 0x66, 0xe6, 0xea, 0x48, 0xb4, 0xca, 0xa9, 0x91, 0xed, 0x6f, 0xf3])
_CK_GXOR = bytes([0xb3, 0xc9, 0x53, 0xa0, 0x69, 0x13, 0xad, 0x4d])

def _u32(v): return v & 0xFFFFFFFF
def _tea_blk(blk, key):
    y, z = struct.unpack('>2I', blk)
    k = struct.unpack('>4I', key)
    s = 0
    for _ in range(16):
        s = _u32(s + 0x9e3779b9)
        y = _u32(y + _u32(_u32(_u32(z << 4) + k[0]) ^ _u32(z + s) ^ _u32((z >> 5) + k[1])))
        z = _u32(z + _u32(_u32(_u32(y << 4) + k[2]) ^ _u32(y + s) ^ _u32((y >> 5) + k[3])))
    return struct.pack('>2I', y, z)

def _cksum(buf):
    v = 0
    for b in buf: v = (0x83 * v + b) & 0x7fffffff
    return v

def _tea_pkt(data, key):
    pad = (8 - ((len(data) + 10) % 8)) % 8
    plain = bytes([(os.urandom(1)[0] & 0xf8) | pad]) + os.urandom(pad) + os.urandom(2) + data + bytes(7)
    out, pp, pc = b'', bytes(8), bytes(8)
    for off in range(0, len(plain), 8):
        mixed = bytes(a ^ b for a, b in zip(plain[off:off + 8], pc))
        enc = _tea_blk(mixed, key)
        cipher = bytes(a ^ b for a, b in zip(enc, pp))
        out += cipher
        pp, pc = mixed, cipher
    return out

def _lp(s):
    d = s.encode() if isinstance(s, str) else s
    return struct.pack('>H', len(d)) + d

def _ck_guard(ts, guid):
    def tail(v):
        t = str(v); return t[-5:] if len(t) >= 5 else ''
    body = struct.pack('>I', ts) + _lp(tail(guid)) + _lp(tail('null')) + _lp(tail('null')) + _lp('-1')
    plain = _lp(body)
    enc = _tea_pkt(plain, _CK_GTEA) + struct.pack('>I', _cksum(plain))
    enc = bytes(a ^ _CK_GXOR[i & 7] for i, a in enumerate(enc))
    return enc.hex().upper()

def _ckey(channel_id):
    ts = int(time.time())
    guid = os.urandom(16).hex()
    guard = _ck_guard(ts, guid)
    uid = os.urandom(4).hex().upper()
    body = (bytes.fromhex('0000004200000004000004d2') + struct.pack('>I', _CK_PLATFORM)
            + struct.pack('>I', 0) + struct.pack('>I', ts) + _lp('dcgh')
            + _lp('_zj1A5Gh6QYcxWjIUGos2w==') + _lp(_CK_APPVER) + _lp(str(channel_id))
            + _lp(guid) + struct.pack('>I', 1) + struct.pack('>I', 1) + _lp(uid) + _lp('nil')
            + _lp('57eab0c4-2c58-44c6-8ae9-dd2757525dc5') + _lp('nil') + _lp('v0.1.000')
            + _lp('com.cctv.yangshipin.app.iphone') + _lp(str(_CK_PLATFORM))
            + _lp('ex_json_bus') + _lp('ex_json_vs') + _lp(guard))
    pkt = bytearray(struct.pack('>H', len(body)) + body)
    pkt[18:22] = struct.pack('>I', _cksum(bytes(pkt)))
    pkt = bytes(pkt)
    enc = _tea_pkt(pkt, _CK_TEA) + struct.pack('>I', _cksum(pkt))
    enc = bytes(a ^ _CK_XOR[i & 15] for i, a in enumerate(enc))
    b64 = base64.b64encode(enc).decode().replace('+', '_').replace('/', '-').rstrip('=')
    return {'cKey': '--01' + b64, 'guid': guid, 'ts': ts,
            'flowId': '%s_%d' % (uuid.uuid4().hex.upper(), _CK_PLATFORM)}

_BK_H264 = base64.b64encode(b'H(30:1080,60:1080|30:1080,60:1080)').decode()

def bk_playurls(channel_id, live_pid, defn='fhd'):
    t = _ckey(channel_id)
    q = urllib.parse.urlencode({
        'atime': '120', 'livepid': live_pid, 'cnlid': channel_id,
        'appVer': _CK_APPVER, 'app_version': '300090', 'caplv': '1', 'cmd': '2',
        'defn': defn, 'device': 'iPhone', 'encryptVer': '4.2', 'getpreviewinfo': '0',
        'hevclv': '0', 'lang': 'zh-Hans_CN', 'livequeue': '0', 'logintype': '1',
        'nettype': '1', 'newnettype': '1', 'newplatform': str(_CK_PLATFORM),
        'platform': str(_CK_PLATFORM), 'sdtfrom': 'v3021', 'spacode': '23',
        'spaudio': '1', 'spdemuxer': '6', 'spdrm': '2', 'spdynamicrange': '1',
        'spflv': '1', 'spflvaudio': '1', 'sphdrfps': '60', 'sphttps': '1',
        'spvcode': _BK_H264, 'spvideo': '4', 'stream': '1', 'system': '1',
        'sysver': 'ios18.2.1', 'uhd_flag': '0', 'cKey': t['cKey'], 'guid': t['guid'],
        'fntick': str(t['ts']), 'flowid': t['flowId'], 'playbacktime': '0',
    })
    req = urllib.request.Request('https://bkliveinfo.ysp.cctv.cn/?' + q,
                                 headers={'User-Agent': 'qqlive', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        p = json.loads(r.read().decode())
    if int(p.get('iretcode', -1)) != 0:
        raise RuntimeError('iretcode=%s %s' % (p.get('iretcode'), p.get('errinfo', '')))
    urls = []
    if p.get('playurl'): urls.append(p['playurl'])
    bu = p.get('backurl_list') or p.get('backurlList') or p.get('backurl')
    if isinstance(bu, list):
        for it in bu: urls.append(it if isinstance(it, str) else (it.get('url') or it.get('playurl') or ''))
    elif isinstance(bu, str):
        urls += [x for x in re.split(r'[;,]', bu) if x.strip()]
    urls = [u for u in dict.fromkeys(urls) if u and '.cctv.' in u]
    if not urls: raise RuntimeError('no playurl')
    urls.sort(key=lambda u: (0 if 'bklive-' in u else 1, u))
    return urls

def fetch_abs_playlist(url, depth=0):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'qqlive', 'Referer': 'https://live.cctv.cn/',
        'Accept': 'application/vnd.apple.mpegurl,application/json,*/*'})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode('utf-8', 'replace')
        final = r.geturl()
    if depth < 2:
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith('#EXT-X-STREAM-INF'):
                for j in range(i + 1, len(lines)):
                    s = lines[j].strip()
                    if s and not s.startswith('#'):
                        return fetch_abs_playlist(urllib.parse.urljoin(final, s), depth + 1)
                break
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and not s.startswith('#'):
            out.append(urllib.parse.urljoin(final, s))
        else:
            out.append(ln)
    return '\n'.join(out)

# ================================================================ 频道表 (新增 IPTV静态源字段)
# 元组格式：(slug, name, sid, pid, defn, iptv_m3u8_url)
# iptv_m3u8_url=None 代表没有IPTV源，直接使用ysp
CHANNELS = [
    # CCTV 央视全套
    ('cctv1', 'CCTV-1 综合', '2024078201', '600001859', 'fhd', None),
    ('cctv2', 'CCTV-2 财经', '2024075401', '600001800', 'fhd', None),
    ('cctv3', 'CCTV-3 综艺', '2024068501', '600001801', 'fhd', None),
    ('cctv4', 'CCTV-4 中文国际', '2029797101', '600001814', 'fhd', None),
    ('cctv5', 'CCTV-5 体育', '2024078401', '600001818', 'fhd', None),
    ('cctv5plus', 'CCTV-5+ 体育赛事', '2024078402', '600001819', 'fhd', None),
    ('cctv6', 'CCTV-6 电影', '2024078501', '600001820', 'fhd', None),
    ('cctv7', 'CCTV-7 国防军事', '2024078601', '600001822', 'fhd', None),
    ('cctv8', 'CCTV-8 电视剧', '2024078701', '600001824', 'fhd', None),
    ('cctv9', 'CCTV-9 纪录', '2024078801', '600001826', 'fhd', None),
    ('cctv10', 'CCTV-10 科教', '2024078901', '600001828', 'fhd', None),
    ('cctv11', 'CCTV-11 戏曲', '2024079001', '600001830', 'fhd', None),
    ('cctv12', 'CCTV-12 社会与法', '2024079101', '600001832', 'fhd', None),
    ('cctv13', 'CCTV-13 新闻', '2024079201', '600001834', 'fhd', None),
    ('cctv14', 'CCTV-14 少儿', '2024079301', '600001836', 'fhd', None),
    ('cctv15', 'CCTV-15 音乐', '2024079401', '600001838', 'fhd', None),
    ('cctv17', 'CCTV-17 农业农村', '2029797501', '600001840', 'fhd', None),

    # 省级卫视
    ('hnws', '湖南卫视', '2024054803', '600002475', 'fhd', None),
    ('zjws', '浙江卫视', '2024054703', '600002520', 'fhd', None),
    ('henanws', '河南卫视', '2029797303', '600002525', 'fhd', None),
    ('jsws', '江苏卫视', '2024054903', '600002476', 'fhd', None),
    ('shws', '东方卫视', '2024055003', '600002477', 'fhd', None),
    ('bjws', '北京卫视', '2024055103', '600002478', 'fhd', None),
    ('gdws', '广东卫视', '2024055203', '600002479', 'fhd', None),
    ('sdws', '山东卫视', '2024055303', '600002480', 'fhd', None),
    ('hbsws', '湖北卫视', '2024055403', '600002481', 'fhd', None),
    ('hunws', '安徽卫视', '2024055503', '600002482', 'fhd', None),
    ('cqws', '重庆卫视', '2024055603', '600002483', 'fhd', None),
    ('scws', '四川卫视', '2024055703', '600002484', 'fhd', None),
    ('sxxws', '陕西卫视', '2024055803', '600002485', 'fhd', None),
    ('fjws', '福建东南卫视', '2024055903', '600002486', 'fhd', None),
    ('gxws', '广西卫视', '2024056003', '600002487', 'fhd', None),
    ('ynws', '云南卫视', '2024056103', '600002488', 'fhd', None),
    ('gzws', '贵州卫视', '2024056203', '600002489', 'fhd', None),
    ('lnws', '辽宁卫视', '2024056303', '600002490', 'fhd', None),
    ('jlws', '吉林卫视', '2024056403', '600002491', 'fhd', None),
    ('hljws', '黑龙江卫视', '2024056503', '600002492', 'fhd', None),
    ('nmws', '内蒙古卫视', '2024056603', '600002493', 'fhd', None),
    ('nxws', '宁夏卫视', '2024056703', '600002494', 'fhd', None),
    ('xjws', '新疆卫视', '2024056803', '600002495', 'fhd', None),
    ('xzws', '西藏卫视', '2024056903', '600002496', 'fhd', None),
    ('qhws', '青海卫视', '2024057003', '600002497', 'fhd', None),
    ('tjws', '天津卫视', '2024057103', '600002498', 'fhd', None),
    ('hebeiws', '河北卫视', '2024057203', '600002499', 'fhd', None),
    ('shanxiws', '山西卫视', '2024057303', '600002500', 'fhd', None),
    ('hainanws', '海南卫视', '2024057403', '600002501', 'fhd', None),
]

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
REFRESH_INTERVAL = 15
IDLE_TIMEOUT = 120
WINDOW = 300
MAX_SEGS = 60
BK_URL_TTL = 600

class Channel:
    def __init__(self, slug, name, sid, pid, defn, iptv_url):
        self.slug, self.name, self.sid, self.pid, self.defn = slug, name, sid, pid, defn
        self.iptv_url = iptv_url
        self.lock = threading.Lock()
        self.segments = {}
        self.order = deque()
        self.seq = 0
        self.last_access = 0.0
        self.thread = None
        self.last_error = ''
        self.mode = 'iptv' if iptv_url else 'jce'
        self.bk_urls = []
        self.bk_urls_time = 0.0
        self.bk_playlist = ''
        self.iptv_playlist = ''
        self._starting = False

def seg_key(url, pdt):
    if pdt: return 'pdt:' + pdt
    p = urllib.parse.urlsplit(url)
    return p.scheme + '://' + p.netloc + p.path

def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)

def iptv_refresh(ch):
    if not ch.iptv_url: raise RuntimeError('无IPTV源')
    pl = fetch_abs_playlist(ch.iptv_url)
    if '#EXTM3U' not in pl: raise RuntimeError('iptv返回非m3u8')
    with ch.lock:
        ch.iptv_playlist = pl
        ch.last_error = ''
    log(f'{ch.slug} IPTV源刷新成功')
    return True

def jce_fetch(ch):
    now = int(time.time())
    m3u8_url = jce_timeshift_url(ch.pid, ch.sid, now - WINDOW, now, ch.defn)
    req = urllib.request.Request(m3u8_url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode('utf-8', 'replace')
    segs, dur, pdt = [], 6.0, ''
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            try: dur = float(line[len('#EXTINF:'):].split(',')[0])
            except ValueError: dur = 6.0
        elif line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            pdt = line[len('#EXT-X-PROGRAM-DATE-TIME:'):]
        elif line and not line.startswith('#'):
            segs.append((dur, pdt, urllib.parse.urljoin(m3u8_url, line)))
            pdt = ''
    if not segs: raise RuntimeError('empty playlist')
    return segs

def jce_refresh(ch):
    segs = jce_fetch(ch)
    with ch.lock:
        added = 0
        for dur, pdt, url in segs:
            key = seg_key(url, pdt)
            if key in ch.segments:
                ch.segments[key][3] = url
                continue
            ch.seq += 1
            ch.segments[key] = [ch.seq, dur, pdt, url]
            ch.order.append(key)
            added += 1
        while len(ch.order) > MAX_SEGS:
            ch.segments.pop(ch.order.popleft(), None)
        ch.last_error = ''
    if added: log('%s +%d 片 (共%d)' % (ch.slug, added, len(ch.segments)))
    return True

def bk_refresh(ch):
    now = time.time()
    if now - ch.bk_urls_time > BK_URL_TTL or not ch.bk_urls:
        ch.bk_urls = bk_playurls(ch.sid, ch.pid, ch.defn)
        ch.bk_urls_time = now
        log('%s bkliveinfo 拿到 %d 个地址' % (ch.slug, len(ch.bk_urls)))
    last_err = ''
    for attempt in range(2):
        for u in ch.bk_urls:
            try:
                pl = fetch_abs_playlist(u)
                if '#EXTM3U' not in pl: continue
                with ch.lock:
                    ch.bk_playlist = pl
                    ch.last_error = ''
                return True
            except urllib.error.HTTPError as e:
                last_err = 'HTTPError: HTTP %s' % e.code
                if e.code == 403:
                    time.sleep(2)
                continue
            except Exception as e:
                last_err = '%s: %s' % (type(e).__name__, e)
        if attempt == 0:
            try:
                ch.bk_urls = bk_playurls(ch.sid, ch.pid, ch.defn)
                ch.bk_urls_time = time.time()
                log('%s 地址疑似过期, 已重取' % ch.slug)
            except Exception:
                pass
    ch.bk_urls_time = 0
    raise RuntimeError(last_err[:120] or 'bk playlist failed')

def refresh_once(ch):
    try:
        if ch.mode == 'iptv':
            try:
                return iptv_refresh(ch)
            except Exception as e:
                log(f'{ch.slug} IPTV源失效，切换备用ysp: {e}')
                ch.mode = 'jce'
        if ch.mode == 'bk':
            return bk_refresh(ch)
        try:
            return jce_refresh(ch)
        except DeadHostError:
            ch.mode = 'bk'
            log('%s JCE 返回坏域名, 切换 bkliveinfo' % ch.slug)
            return bk_refresh(ch)
    except Exception as e:
        ch.last_error = ('%s: %s' % (type(e).__name__, e))[:120]
        log('%s 刷新失败: %s' % (ch.slug, ch.last_error))
        return False

def refresh_loop(ch):
    log('%s 后台刷新启动 [%s]' % (ch.slug, ch.mode))
    fails = 0
    while time.time() - ch.last_access < IDLE_TIMEOUT:
        ok = refresh_once(ch)
        fails = 0 if ok else fails + 1
        time.sleep(REFRESH_INTERVAL if fails < 3 else 60)
    log('%s 无人观看, 停止刷新' % ch.slug)

def ensure_channel(ch):
    ch.last_access = time.time()
    with ch.lock:
        if ch._starting:
            return
        need_fetch = (not ch.iptv_playlist and not ch.segments and not ch.bk_playlist)
        need_thread = ch.thread is None or not ch.thread.is_alive()
        if need_fetch or need_thread:
            ch._starting = True
        else:
            return
    try:
        if need_fetch:
            refresh_once(ch)
        if need_thread:
            ch.thread = threading.Thread(target=refresh_loop, args=(ch,), daemon=True)
            ch.thread.start()
    finally:
        with ch.lock:
            ch._starting = False

def build_playlist(ch):
    with ch.lock:
        if ch.mode == 'iptv':
            return ch.iptv_playlist or None
        if ch.mode == 'bk':
            return ch.bk_playlist or None
        keys = list(ch.order)[-30:]
        segs = [ch.segments[k] for k in keys if k in ch.segments]
    if not segs: return None
    target = max(6, max(int(s[1] + 0.5) for s in segs))
    out = ['#EXTM3U', '#EXT-X-VERSION:3',
           '#EXT-X-TARGETDURATION:%d' % target,
           '#EXT-X-MEDIA-SEQUENCE:%d' % segs[0][0]]
    for _, dur, pdt, url in segs:
        if pdt: out.append('#EXT-X-PROGRAM-DATE-TIME:' + pdt)
        out.append('#EXTINF:%.3f,' % dur)
        out.append(url)
    return '\n'.join(out) + '\n'

CHANNEL_MAP = {c[0]: Channel(*c) for c in CHANNELS}
FORCE_BK = {'cctv11', 'cctv12', 'cctv14', 'cctv15', 'cctv16', 'cctv164k',
            'cctv17', 'cctv4k', 'cctvfyjc', 'cctvdyjc', 'cctvhjjc'}
for _s in FORCE_BK:
    if _s in CHANNEL_MAP:
        CHANNEL_MAP[_s].mode = 'bk'

# ================================================================ HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = 'iptv-ysp-combo/1.0'
    def log_message(self, fmt, *args):
        pass
    def _send(self, code, body, ctype='text/plain; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._send(200, index_page(), 'text/html; charset=utf-8')
            return
        if path == '/health':
            self._send(200, 'ok')
            return
        if path == '/all.m3u':
            host = self.headers.get('Host', 'localhost:18767')
            lines = ['#EXTM3U']
            for slug, name, _s, _p, _d, _url in CHANNELS:
                lines.append('#EXTINF:-1,%s' % name)
                lines.append('http://%s/%s.m3u8' % (host, slug))
            self._send(200, '\n'.join(lines) + '\n', 'application/vnd.apple.mpegurl')
            return
        if path == '/diag':
            info = []
            for slug, ch in CHANNEL_MAP.items():
                info.append('%s mode=%s err=%s' % (slug, ch.mode, ch.last_error))
            self._send(200, '\n'.join(info) + '\n')
            return
        m = re.match(r'^/([\w]+)\.m3u8$', path)
        if m:
            ch = CHANNEL_MAP.get(m.group(1))
            if not ch:
                self._send(404, '未知频道\n')
                return
            ensure_channel(ch)
            pl = build_playlist(ch)
            if not pl:
                self._send(503, '频道 %s 暂无数据 (%s), 请稍后重试\n' % (ch.name, ch.last_error or '拉取中'))
                return
            self._send(200, pl, 'application/vnd.apple.mpegurl')
            return
        self._send(404, 'not found\n')

def index_page():
    items = []
    for slug, name, _s, _p, _d, _url in CHANNELS:
        items.append('<li><a href="/%s.m3u8">%s</a> <span>/%s.m3u8</span></li>' % (slug, name, slug))
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>IPTV+央视频双源直播</title><style>'
            'body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:720px;margin:0 auto;padding:20px;}'
            'li{margin:6px 0;}span{color:#888;font-size:12px;margin-left:8px;}</style></head>'
            '<body><h2>IPTV静态源 + 央视频备用 (%d 路)</h2>'
            '<p>优先IPTV源，失效自动切央视频。播放器直连CDN，本机不转发视频流量。</p>'
            '<p>聚合订阅: <a href="/all.m3u">/all.m3u</a></p>'
            '<ul>%s</ul></body></html>' % (len(items), ''.join(items)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('port', nargs='?', type=int, default=18767)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    log('iptv-ysp-combo v1 启动: %d 个频道, 监听端口 %d' % (len(CHANNEL_MAP), args.port))
    log('首页: http://localhost:%d/' % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
