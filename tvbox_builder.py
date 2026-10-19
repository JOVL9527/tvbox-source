#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 直播源聚合工具  tvbox_builder.py
================================================================================
流程：读配置 -> 抓订阅源 -> 解析(去重/黑名单/协议过滤) -> 并发测速校验
      -> 按分辨率+响应时间排序取前 N -> 输出 result.txt / result.m3u / source.json

依赖：Python 3.9+，全部使用标准库，无需 pip install 任何东西。
用法：
    python tvbox_builder.py                    # 正常执行
    python tvbox_builder.py --skip-probe       # 不测速，只做抓取+去重（调试用）
    python tvbox_builder.py -c my.ini          # 指定其他配置文件
    python tvbox_builder.py --max-candidates 200 --workers 16   # 小机器上快速跑一遍
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urljoin
from urllib.request import Request, build_opener, ProxyHandler

VERSION = "1.0.0"
UA_DEFAULT = "okhttp/Mursor-1.4.0.0"
TXT_EXTINF = re.compile(r'^#EXTINF:\s*(-?[\d.]+)\s*(.*?),(.*)$', re.IGNORECASE)
ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
RESOLUTION_RE = re.compile(r'RESOLUTION=(\d{3,4})[xX](\d{3,4})')

_lock = threading.Lock()
_log_lines: list[str] = []


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    with _lock:
        _log_lines.append(line)
    print(line, flush=True)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class Config:
    """把 config.ini 读成好用的 Python 对象，缺项一律给安全默认值。"""

    def __init__(self, path: str) -> None:
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # 保留 key 原始大小写
        if not os.path.isfile(path):
            raise SystemExit(f"找不到配置文件：{path}")
        cp.read(path, encoding="utf-8")
        self._cp = cp
        self.root = os.path.dirname(os.path.abspath(path))

    # --- 基础取值 ---
    def get(self, section: str, key: str, default: str = "") -> str:
        if self._cp.has_option(section, key):
            return self._cp.get(section, key).strip()
        return default

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        raw = self.get(section, key, "").strip().strip('"').strip("'")
        if raw == "":
            return default
        return raw.lower() in ("1", "true", "yes", "on", "y")

    def get_int(self, section: str, key: str, default: int = 0) -> int:
        raw = self.get(section, key, "")
        try:
            return int(float(raw))
        except Exception:
            return default

    def get_list(self, section: str, key: str) -> list[str]:
        raw = self.get(section, key, "")
        if not raw:
            return []
        parts = re.split(r"[,\n]", raw)
        return [p.strip() for p in parts if p.strip() and not p.strip().startswith("#")]

    def abs_path(self, rel: str) -> str:
        if not rel:
            return ""
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    # --- 结构化导出 ---
    def load(self) -> "Config":
        g, s, o = "global", "source", "output"
        self.open_update = self.get_bool(g, "open_update", True)
        self.open_url_info = self.get_bool(g, "open_url_info", False)
        self.open_update_time = self.get_bool(g, "open_update_time", True)
        self.open_m3u_result = self.get_bool(g, "open_m3u_result", True)

        self.subscribe_urls = self.get_list(s, "subscribe_urls")
        self.subscribe_file = self.abs_path(self.get(s, "subscribe_file", "config/subscribe.txt"))
        self.source_file = self.abs_path(self.get(s, "source_file", "config/demo.txt"))
        self.local_file = self.abs_path(self.get(s, "local_file", "config/local.txt"))

        self.domain_blacklist = self.get_list(s, "domain_blacklist")
        self.url_keywords_blacklist = self.get_list(s, "url_keywords_blacklist")
        self.channel_blacklist = self.get_list(s, "channel_blacklist")
        self.blacklist_file = self.abs_path(self.get(s, "blacklist_file", "config/blacklist.txt"))

        self.urls_limit = self.get_int(s, "urls_limit", 5)
        self.ipv_type = self.get(s, "ipv_type", "ipv4").lower()
        self.ipv_type_prefer = self.get(s, "ipv_type_prefer", "auto").lower()
        self.min_resolution = self.get_int(s, "min_resolution", 0)
        self.max_response_time = self.get_int(s, "max_response_time", 3000)
        self.probe_timeout = self.get_int(s, "probe_timeout", 4)
        self.probe_workers = self.get_int(s, "probe_workers", 48)
        self.max_candidates = self.get_int(s, "max_candidates", 3000)
        self.request_timeout = self.get_int(s, "request_timeout", 15)
        self.user_agent = self.get(s, "user_agent", UA_DEFAULT) or UA_DEFAULT

        self.open_auto_disable_source = self.get_bool("filter", "open_auto_disable_source", False)
        self.cdn_url = self.get_list("filter", "cdn_url")
        self.http_proxy = self.get("filter", "http_proxy", "")

        self.final_file = self.abs_path(self.get(o, "final_file", "output/result.txt"))
        self.final_m3u_file = self.abs_path(self.get(o, "final_m3u_file", "output/result.m3u"))
        self.source_json = self.abs_path(self.get(o, "source_json", "output/source.json"))
        self.sites_json = self.abs_path(self.get(o, "sites_json", "output/sites.json"))
        self.log_file = self.abs_path(self.get(o, "log_file", "output/update.log"))
        self.report_file = self.abs_path(self.get(o, "report_file", "output/report.json"))
        self.category_file = self.abs_path(self.get(o, "category_file", "config/category.txt"))
        self.publish_base_url = self.get(o, "publish_base_url", "").rstrip("/")

        # ---- 点播（非直播）源 ----
        st = "sites"
        self.open_sites = self.get_bool(st, "open_sites", True)
        self.sites_source_urls = self.get_list(st, "sites_source_urls")
        self.sites_source_file = self.abs_path(self.get(st, "sites_source_file", "config/sites_source.txt"))
        self.sites_file = self.abs_path(self.get(st, "sites_file", "config/sites.txt"))
        self.sites_limit = self.get_int(st, "sites_limit", 150)
        self.probe_sites = self.get_bool(st, "probe_sites", True)
        self.sites_probe_timeout = self.get_int(st, "sites_probe_timeout", 6)
        self.sites_keywords_blacklist = self.get_list(st, "sites_keywords_blacklist")
        self.sites_api_blacklist = self.get_list(st, "sites_api_blacklist")
        self.spider = self.get(st, "spider", "")
        self.open_auto_spider = self.get_bool(st, "open_auto_spider", True)
        self.parses_limit = self.get_int(st, "parses_limit", 40)

        if self.ipv_type not in ("ipv4", "ipv6", "all"):
            raise SystemExit(f"ipv_type 只能是 ipv4 / ipv6 / all，当前为：{self.ipv_type}")
        return self


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
_opener = build_opener()


def build_opener_for(proxy: str):
    global _opener
    handlers = [ProxyHandler({"http": proxy, "https": proxy})] if proxy else [ProxyHandler({})]
    _opener = build_opener(*handlers)


def http_get(url: str, timeout: int, ua: str, referer: str = "",
             read_limit: int = 0, extra_headers: Optional[dict] = None) -> tuple[bool, bytes, int]:
    """返回 (是否成功, 内容字节, 耗时毫秒)。read_limit=0 表示读完。"""
    headers = {"User-Agent": ua, "Accept": "*/*", "Connection": "close"}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    start = time.perf_counter()
    try:
        req = Request(url, headers=headers)
        with _opener.open(req, timeout=timeout) as resp:
            data = resp.read(read_limit) if read_limit else resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" and data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
        cost = int((time.perf_counter() - start) * 1000)
        return True, data, cost
    except (HTTPError, URLError, socket.timeout, OSError, ValueError):
        return False, b"", int((time.perf_counter() - start) * 1000)


def fetch_text(url: str, timeout: int, ua: str, cdn_list: Optional[list[str]] = None) -> str:
    """抓取订阅源文本，失败时依次用 CDN 代理重试。"""
    ok, data, _ = http_get(url, timeout, ua)
    if ok and data:
        return decode_bytes(data)
    for cdn in (cdn_list or []):
        alt = url if url.startswith(cdn) else f"{cdn.rstrip('/')}/{url}"
        ok, data, _ = http_get(alt, timeout, ua)
        if ok and data:
            log(f"  ↳ 使用 CDN 回退成功：{cdn}", "WARN")
            return decode_bytes(data)
    return ""


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Stream:
    name: str
    url: str
    source: str = ""          # 来源（订阅源地址或 local/template）
    group: str = ""           # 订阅源自带的分类
    logo: str = ""            # tvg-logo
    resolution: int = 0       # 纵向像素数，如 1080
    ip_type: str = ""         # ipv4 / ipv6 / unknown
    rtt: int = 99999          # 响应耗时(ms)，越小越好
    verified: bool = False

    @property
    def sort_key(self):
        # 分辨率高的优先；其次响应快的优先；同分时 IPv4 优先（兼容性更好）
        ip_rank = 0 if self.ip_type == "ipv4" else 1
        return (-self.resolution, self.rtt, ip_rank)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_m3u(text: str, source: str) -> list[Stream]:
    out: list[Stream] = []
    pending: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            m = TXT_EXTINF.match(line)
            if m:
                attrs = dict(ATTR_RE.findall(m.group(2) or ""))
                name = (attrs.get("tvg-name") or m.group(3) or "").strip()
                if not name and m.group(3):
                    name = m.group(3).strip()
                pending = {
                    "name": name,
                    "group": attrs.get("group-title", "").strip(),
                    "logo": attrs.get("tvg-logo", "").strip(),
                }
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        out.append(Stream(name=pending["name"], url=line, source=source,
                          group=pending["group"], logo=pending["logo"]))
        pending = None
    return out


def parse_txt(text: str, source: str) -> list[Stream]:
    """TVBox txt 格式：`分类,#genre#` 分组 + `频道名,url`，一个频道可用 # 挂多个 url。

    额外兼容「只有频道名、没有地址」的模板写法：这类行会生成 url 为空的 Stream，
    仅用于确定频道优先级顺序，不会被送去测速。
    """
    out: list[Stream] = []
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            group = line.split(",")[0].strip()
            continue
        if "," not in line:
            # 模板写法：单独一行频道名
            out.append(Stream(name=line, url="", source=source, group=group))
            continue
        name, _, rest = line.partition(",")
        name, rest = name.strip(), rest.strip()
        if not name:
            continue
        if not rest:
            out.append(Stream(name=name, url="", source=source, group=group))
            continue
        for one in rest.split("#"):
            one = one.strip()
            if not one:
                continue
            out.append(Stream(name=name, url=one, source=source, group=group))
    return out


def parse_any(text: str, source: str) -> list[Stream]:
    if "#EXTM3U" in text.upper() or "#EXTINF" in text.upper():
        return parse_m3u(text, source)
    return parse_txt(text, source)


CCTV_MAP = {
    "1": "CCTV-1 综合", "2": "CCTV-2 财经", "3": "CCTV-3 综艺",
    "4": "CCTV-4 中文国际", "5": "CCTV-5 体育", "5+": "CCTV-5+ 体育赛事",
    "6": "CCTV-6 电影", "7": "CCTV-7 国防军事", "8": "CCTV-8 电视剧",
    "9": "CCTV-9 纪录", "10": "CCTV-10 科教", "11": "CCTV-11 戏曲",
    "12": "CCTV-12 社会与法", "13": "CCTV-13 新闻", "14": "CCTV-14 少儿",
    "15": "CCTV-15 音乐", "16": "CCTV-16 奥林匹克", "17": "CCTV-17 农业农村",
}
_CCTV_RE = re.compile(r"^CCTV[-\s_]?(\d{1,2})\s*(\+)?", re.IGNORECASE)


def normalize_name(name: str) -> str:
    """频道名归一化：统一 CCTV 写法、去掉高/标清后缀与空格，便于跨源合并同一频道。"""
    n = re.sub(r"\s+", "", name.strip())
    n = re.sub(r"[（(](?:高清|标清|超清|HD|4K|1080P|720P)[)）]", "", n, flags=re.IGNORECASE)
    n = re.sub(r"(高清|标清|超清|蓝光|4K)$", "", n, flags=re.IGNORECASE)
    if not n:
        return name.strip()
    m = _CCTV_RE.match(n)
    if m:
        key = f"{m.group(1)}{'+' if m.group(2) else ''}"
        if key in CCTV_MAP:
            return CCTV_MAP[key]
    return n


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #
def load_blacklist(cfg: Config) -> tuple[list[str], list[str]]:
    """返回 (域名黑名单, 关键字黑名单)"""
    domains = list(cfg.domain_blacklist)
    keywords = list(cfg.url_keywords_blacklist)
    if cfg.blacklist_file and os.path.isfile(cfg.blacklist_file):
        for raw in open(cfg.blacklist_file, encoding="utf-8", errors="ignore"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            (domains if ("." in line or ":" in line) else keywords).append(line)
    return [d.lower() for d in domains], [k.lower() for k in keywords]


_ip_cache: dict[str, str] = {}


def detect_ip_type(url: str) -> str:
    host = urlparse(url).hostname or ""
    if not host:
        return "unknown"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return "ipv4" if ip.version == 4 else "ipv6"
    except ValueError:
        pass
    if host in _ip_cache:
        return _ip_cache[host]
    result = "unknown"
    try:
        infos = socket.getaddrinfo(host, None)
        families = {i[0] for i in infos}
        has4 = socket.AF_INET in families
        has6 = socket.AF_INET6 in families
        result = "ipv4" if has4 else ("ipv6" if has6 else "unknown")
    except Exception:
        result = "unknown"
    _ip_cache[host] = result
    return result


def passes_filters(st: Stream, cfg: Config, domains: list[str], keywords: list[str]) -> tuple[bool, str]:
    url_l, name_l = st.url.lower(), st.name.lower()
    for d in domains:
        if d and d in url_l:
            return False, f"域名黑名单:{d}"
    for k in keywords:
        if k and (k in url_l or k in name_l):
            return False, f"关键字黑名单:{k}"
    for c in cfg.channel_blacklist:
        if c and c.lower() in name_l:
            return False, f"频道黑名单:{c}"
    if not st.url.startswith(("http://", "https://")):
        return False, "非HTTP协议"
    if not st.name:
        return False, "无频道名"
    return True, ""


# --------------------------------------------------------------------------- #
# 测速校验
# --------------------------------------------------------------------------- #
def probe(st: Stream, cfg: Config) -> Optional[Stream]:
    """访问直播地址：能拿到内容即视为可用，同时尝试解析分辨率。"""
    headers = {}
    if st.logo and st.logo.startswith("http"):
        headers["Referer"] = "/".join(st.logo.split("/")[:3])

    ip_type = detect_ip_type(st.url)
    if cfg.ipv_type != "all" and ip_type != cfg.ipv_type:
        return None
    st.ip_type = ip_type

    ok, data, cost = http_get(st.url, cfg.probe_timeout, cfg.user_agent, read_limit=4096,
                              extra_headers=headers)
    if not ok or not data:
        return None
    if cfg.max_response_time and cost > cfg.max_response_time:
        return None

    body = decode_bytes(data)
    # 广告/无信号占位源识别
    if "#EXT-X-ENDLIST" in body and len(body) < 2048:
        return None
    if any(w in body for w in ("广告", "加群", "no signal", "无信号")) and len(body) < 4096:
        return None

    m = RESOLUTION_RE.search(body)
    if m:
        st.resolution = int(m.group(2))
        if cfg.open_m3u_result and "RESOLUTION" in body and "#EXT-X-STREAM-INF" in body:
            # 主播放列表：取最高码率变体的一路再探一次，拿更准的分辨率
            best = 0
            for res_match in RESOLUTION_RE.finditer(body):
                best = max(best, int(res_match.group(2)))
            st.resolution = best or st.resolution

    if cfg.min_resolution and st.resolution and st.resolution < cfg.min_resolution:
        return None

    st.rtt = cost
    st.verified = True
    return st


def interleave_by_source(items: list[Stream]) -> list[Stream]:
    """按来源轮流取一条，避免截断时全部集中在某一个订阅源上。"""
    buckets: dict[str, list[Stream]] = {}
    order: list[str] = []
    for st in items:
        if st.source not in buckets:
            buckets[st.source] = []
            order.append(st.source)
        buckets[st.source].append(st)
    out: list[Stream] = []
    idx = 0
    while True:
        added = False
        for src in order:
            if idx < len(buckets[src]):
                out.append(buckets[src][idx])
                added = True
        if not added:
            break
        idx += 1
    return out


# --------------------------------------------------------------------------- #
# 点播源（sites）—— 非直播部分
# --------------------------------------------------------------------------- #
SITE_KEEP_KEYS = ("key", "name", "type", "api", "searchable", "quickSearch",
                  "filterable", "ext", "jar", "playerType", "categories", "time",
                  "style", "changeable", "poster", "logo", "version")


@dataclass
class Site:
    key: str = ""
    name: str = ""
    type: int = 3
    api: str = ""
    searchable: int = 1
    quickSearch: int = 1
    filterable: int = 1
    ext: str = ""
    jar: str = ""
    playerType: int = 0
    source: str = ""          # 来源配置地址 / manual
    rtt: int = 99999
    verified: bool = False    # 仅 type=1 的 CMS 接口能真正校验
    classes: int = 0          # CMS 接口返回的分类数

    def to_json(self) -> dict:
        d: dict = {"key": self.key, "name": self.name, "type": self.type, "api": self.api}
        if self.searchable is not None:
            d["searchable"] = self.searchable
        if self.quickSearch is not None:
            d["quickSearch"] = self.quickSearch
        if self.filterable is not None:
            d["filterable"] = self.filterable
        if self.ext:
            d["ext"] = self.ext
        if self.jar:
            d["jar"] = self.jar
        if self.playerType:
            d["playerType"] = self.playerType
        return {k: v for k, v in d.items() if v not in ("", None)}


def _to_int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def resolve_ref(val: str, base_url: str) -> str:
    """把配置里的相对引用（./lib/xx.js、./jar/spider.jar;md5;xxx）解析成绝对地址。

    注意必须保留 `;md5;hash` 这类后缀，TVBox 靠它校验 spider 包。
    """
    v = str(val or "").strip()
    if not v:
        return ""
    low = v.lower()
    if low.startswith(("http://", "https://", "csp_", "assets:", "file://", "clan://", "proxy://")):
        return v
    if v.startswith("./") or v.startswith("../"):
        return urljoin(base_url, v)
    if "/" in v and "." in v.split("/")[-1]:
        return urljoin(base_url, v)
    return v


def normalize_site(raw: dict, base_url: str, source: str) -> Optional[Site]:
    api = str(raw.get("api", "") or "").strip()
    if not api:
        return None
    key = str(raw.get("key", "") or "").strip()
    name = str(raw.get("name", "") or "").strip()
    if not name:
        return None
    return Site(
        key=key or name,
        name=name,
        type=_to_int(raw.get("type"), 3),
        api=resolve_ref(api, base_url),
        searchable=_to_int(raw.get("searchable"), 1),
        quickSearch=_to_int(raw.get("quickSearch"), 1),
        filterable=_to_int(raw.get("filterable"), 1),
        ext=resolve_ref(raw.get("ext", ""), base_url),
        jar=resolve_ref(raw.get("jar", ""), base_url),
        playerType=_to_int(raw.get("playerType"), 0),
        source=source,
    )


def strip_json_comments(text: str) -> str:
    """去掉 JSONC 里的 // 与 /* */ 注释、以及对象/数组尾部的多余逗号。

    公开 TVBox 配置大量使用 JSONC 写法，Python 的 json 模块不接受，
    所以需要先用状态机清一遍（必须区分字符串内外，否则会误伤 URL 里的 // ）。
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if ch in "}]":
            # 回退去掉紧邻的尾逗号
            k = len(out) - 1
            while k >= 0 and out[k] in " \t\r\n":
                k -= 1
            if k >= 0 and out[k] == ",":
                del out[k:]
        out.append(ch)
        i += 1
    return "".join(out)


def loads_tolerant(text: str) -> Optional[dict]:
    """宽容 JSON 解析：支持 BOM、JSONC 注释、尾逗号；判定加密配置。"""
    t = text.lstrip("\ufeff").strip()
    if not t:
        return None
    if t[0] not in "{[":
        return None                      # 加密/混淆配置（十六进制串），无法聚合
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return json.loads(strip_json_comments(t))
    except (json.JSONDecodeError, ValueError) as e:
        log(f"  JSON 解析失败：{e}", "WARN")
        return None


def parse_sites_config(text: str, url: str) -> dict:
    """解析一个公开 TVBox 配置，抽出点播相关部分。"""
    data = loads_tolerant(text)
    if data is None:
        head = text.lstrip("\ufeff").strip()[:1]
        if head and head not in "{[":
            log(f"  疑似加密/混淆配置，无法聚合，跳过：{url.split('/')[-1]}", "WARN")
        else:
            log(f"  配置内容为空或非法，跳过：{url}", "WARN")
        return {"sites": [], "spider": "", "parses": [], "flags": []}
    if not isinstance(data, dict):
        return {"sites": [], "spider": "", "parses": [], "flags": []}

    sites: list[Site] = []
    for raw in data.get("sites", []) or []:
        if isinstance(raw, dict):
            st = normalize_site(raw, url, url)
            if st:
                sites.append(st)

    spider = resolve_ref(data.get("spider", ""), url)

    parses: list[dict] = []
    for p in data.get("parses", []) or []:
        if not isinstance(p, dict):
            continue
        u = resolve_ref(p.get("url", ""), url)
        if not u.lower().startswith(("http://", "https://")):
            continue                      # 过滤 "Demo" / "./xxx" 之类的无效解析项
        item = {"name": str(p.get("name", "")).strip() or u.split("/")[2],
                "type": _to_int(p.get("type"), 1), "url": u}
        if p.get("ext"):
            item["ext"] = resolve_ref(p.get("ext"), url)
        parses.append(item)

    flags = [f for f in (data.get("flags", []) or []) if isinstance(f, dict) and f.get("key")]

    return {"sites": sites, "spider": spider, "parses": parses, "flags": flags}


def site_dedup_key(st: Site) -> str:
    """点播源去重键。

    - type=1 的 CMS 接口：按「主机+路径」去重（忽略 http/https 与结尾斜杠差异）
    - type=3/0 的 js 源：按 ext 的文件名去重，这样同一个 js 源出现在不同镜像仓库时
      只会保留一份（例如 qist/js/播客.js 与 gao/js/播客.js 视为同一个）
    - 内置 csp_XXX：api 本身就是唯一标识
    """
    if st.type == 1:
        p = urlparse(st.api)
        return f"1|{(p.netloc + p.path).rstrip('/').lower()}"
    if st.ext:
        base = os.path.basename(urlparse(st.ext).path)
        if base:
            return f"{st.type}|{base}"
    if st.api.startswith(("csp_", "assets:", "file://")):
        return f"{st.type}|{st.api}"
    return f"{st.type}|{st.key}|{st.api}"


def probe_cms_site(st: Site, cfg: Config) -> Optional[Site]:
    """type=1 的 CMS 接口用 ?ac=list 校验：能返回带 class/list 的 JSON 才算活。"""
    sep = "&" if "?" in st.api else "?"
    url = f"{st.api}{sep}ac=list"
    ok, data, cost = http_get(url, cfg.sites_probe_timeout, cfg.user_agent, read_limit=131072)
    if not ok or not data:
        return None
    try:
        j = json.loads(decode_bytes(data))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(j, dict):
        return None
    classes = j.get("class") or []
    if isinstance(classes, list) and classes:
        st.classes = len(classes)
    elif j.get("list"):
        st.classes = len(j["list"])
    else:
        return None
    st.rtt = cost
    st.verified = True
    return st


def load_manual_sites(cfg: Config, base_url: str = "") -> list[Site]:
    """读取 config/sites.txt：支持 `名称,type,api[,ext]` 与整行 JSON 两种写法。"""
    out: list[Site] = []
    if not (cfg.sites_file and os.path.isfile(cfg.sites_file)):
        return out
    for raw in open(cfg.sites_file, encoding="utf-8", errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            try:
                st = normalize_site(json.loads(line), base_url, "manual")
                if st:
                    st.verified = True
                    out.append(st)
            except (json.JSONDecodeError, ValueError):
                log(f"  manual sites 行解析失败：{line[:60]}", "WARN")
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        name, type_s, api = parts[0], parts[1], parts[2]
        ext = parts[3] if len(parts) > 3 else ""
        out.append(Site(key=name, name=name, type=_to_int(type_s, 1), api=api,
                        ext=ext, source="manual", verified=True))
    return out


def build_sites(cfg: Config) -> tuple[list[Site], str, list[dict], list[dict], dict]:
    """聚合点播源，返回 (sites, spider, parses, flags, stats)"""
    stats = {"sources_ok": 0, "sources_failed": 0, "raw": 0,
             "dup": 0, "blacklist": 0, "cms_dead": 0, "cms_alive": 0}

    src_list: list[str] = list(cfg.sites_source_urls)
    if cfg.sites_source_file and os.path.isfile(cfg.sites_source_file):
        for raw in open(cfg.sites_source_file, encoding="utf-8", errors="ignore"):
            line = raw.strip()
            if line and not line.startswith("#"):
                src_list.append(line.split()[0])
    # 去重保序
    seen_src: set[str] = set()
    src_list = [u for u in src_list if not (u in seen_src or seen_src.add(u))]

    all_sites: list[Site] = []
    parses_all: list[dict] = []
    flags_all: list[dict] = []
    spider_votes: dict[str, int] = {}

    for idx, url in enumerate(src_list, 1):
        text = fetch_text(url, cfg.request_timeout, cfg.user_agent, cfg.cdn_url)
        if not text.strip():
            stats["sources_failed"] += 1
            log(f"  [{idx}/{len(src_list)}] 点播来源抓取失败：{url}", "WARN")
            continue
        parsed = parse_sites_config(text, url)
        stats["sources_ok"] += 1
        stats["raw"] += len(parsed["sites"])
        all_sites.extend(parsed["sites"])
        parses_all.extend(parsed["parses"])
        flags_all.extend(parsed["flags"])
        if parsed["spider"]:
            spider_votes[parsed["spider"]] = spider_votes.get(parsed["spider"], 0) + 1
        log(f"  [{idx}/{len(src_list)}] sites {len(parsed['sites']):>4} / "
            f"parses {len(parsed['parses']):>3} <- {url.split('/')[-1]}")

    # 手工源优先
    manual = load_manual_sites(cfg)
    if manual:
        log(f"  手工点播源 {len(manual)} 条（优先保留）")

    # 过滤 + 去重
    def bad(st: Site) -> bool:
        low_name, low_api = st.name.lower(), st.api.lower()
        for k in cfg.sites_keywords_blacklist:
            if k and k.lower() in low_name:
                return True
        for k in cfg.sites_api_blacklist:
            if k and k.lower() in low_api:
                return True
        return False

    merged: list[Site] = []
    seen: set[str] = set()
    for st in manual + all_sites:
        k = site_dedup_key(st)
        if k in seen:
            stats["dup"] += 1
            continue
        if bad(st):
            stats["blacklist"] += 1
            continue
        seen.add(k)
        merged.append(st)

    # type=1 的 CMS 接口做真校验，其余类型无法 HTTP 校验，原样保留
    if cfg.probe_sites:
        cms = [s for s in merged if s.type == 1 and s.source != "manual"]
        if cms:
            log(f"  校验 {len(cms)} 个 CMS 点播接口（ac=list）...")
            alive: list[Site] = []
            with ThreadPoolExecutor(max_workers=min(16, max(1, len(cms)))) as pool:
                for res in pool.map(lambda s: probe_cms_site(s, cfg), cms):
                    if res:
                        alive.append(res)
                    else:
                        stats["cms_dead"] += 1
            stats["cms_alive"] = len(alive)
            alive_map = {site_dedup_key(s): s for s in alive}
            kept: list[Site] = []
            for s in merged:
                if s.type == 1 and s.source != "manual":
                    if site_dedup_key(s) in alive_map:      # 校验失败的 CMS 接口直接丢弃
                        kept.append(alive_map[site_dedup_key(s)])
                else:
                    kept.append(s)
            merged = kept
            log(f"  CMS 接口有效 {len(alive)} / {len(cms)}，"
                f"丢弃 {stats['cms_dead']} 个失效接口")

    # 排序：手工源 > 校验过的 CMS（按响应快慢）> 其余（保持来源顺序）
    def rank(i_st: tuple[int, Site]) -> tuple:
        i, st = i_st
        if st.source == "manual":
            return (0, 0, i)
        if st.verified:
            return (1, st.rtt, i)
        return (2, 0, i)

    merged = [st for _, st in sorted(enumerate(merged), key=rank)]

    if cfg.sites_limit and len(merged) > cfg.sites_limit:
        log(f"  点播源超过上限，截断至 {cfg.sites_limit} 条", "WARN")
        merged = merged[: cfg.sites_limit]

    # spider 选择
    spider = cfg.spider
    if not spider and cfg.open_auto_spider and spider_votes:
        spider = max(spider_votes.items(), key=lambda kv: kv[1])[0]
        log(f"  自动选定 spider（{spider_votes[spider]} 个来源一致）：{spider}")

    # parses 去重
    parses: list[dict] = []
    seen_p: set[str] = set()
    for p in parses_all:
        u = p["url"]
        if u in seen_p:
            continue
        seen_p.add(u)
        parses.append(p)
        if cfg.parses_limit and len(parses) >= cfg.parses_limit:
            break

    # flags 去重
    flags: list[dict] = []
    seen_f: set[str] = set()
    for f in flags_all:
        k = f"{f.get('key')}|{f.get('name')}"
        if k in seen_f:
            continue
        seen_f.add(k)
        flags.append(f)

    return merged, spider, parses, flags, stats


# --------------------------------------------------------------------------- #
# 分类菜单
# --------------------------------------------------------------------------- #
def load_categories(path: str) -> list[tuple[str, list[str]]]:
    cats: list[tuple[str, list[str]]] = []
    if not os.path.isfile(path):
        return cats
    for raw in open(path, encoding="utf-8", errors="ignore"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, rhs = line.partition("=")
        keys = [k.strip() for k in rhs.split(",") if k.strip()]
        cats.append((name.strip(), keys))
    return cats


def classify(name: str, cats: list[tuple[str, list[str]]]) -> str:
    for cat_name, keys in cats:
        if keys == ["*"]:
            continue
        for k in keys:
            if k.lower() in name.lower():
                return cat_name
    for cat_name, keys in cats:
        if keys == ["*"]:
            return cat_name
    return "其他"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="TVBox 直播源聚合工具")
    ap.add_argument("-c", "--config", default="config.ini", help="配置文件路径")
    ap.add_argument("--skip-probe", action="store_true", help="跳过测速校验（调试用）")
    ap.add_argument("--max-candidates", type=int, default=0, help="覆盖每轮最大候选数")
    ap.add_argument("--workers", type=int, default=0, help="覆盖并发线程数")
    args = ap.parse_args()

    t0 = time.time()
    cfg = Config(args.config).load()
    if args.max_candidates:
        cfg.max_candidates = args.max_candidates
    if args.workers:
        cfg.probe_workers = args.workers
    build_opener_for(cfg.http_proxy)

    log(f"===== TVBox 聚合工具 v{VERSION} 启动 =====")
    log(f"ipv_type={cfg.ipv_type}  urls_limit={cfg.urls_limit}  "
        f"workers={cfg.probe_workers}  min_resolution={cfg.min_resolution}")

    domains, keywords = load_blacklist(cfg)
    log(f"黑名单加载完成：域名 {len(domains)} 条 / 关键字 {len(keywords)} 条")

    cats = load_categories(cfg.category_file)
    log(f"频道菜单加载完成：{len(cats)} 个分类 -> {[c[0] for c in cats]}")

    # ---------- 1. 收集订阅源地址 ----------
    sources: list[tuple[str, str]] = []   # (url, ua)
    seen_src: set[str] = set()

    def add_source(u: str, ua: str) -> None:
        if not u or u in seen_src:
            return
        seen_src.add(u)
        sources.append((u, ua))

    for u in cfg.subscribe_urls:
        add_source(u, cfg.user_agent)
    if cfg.subscribe_file and os.path.isfile(cfg.subscribe_file):
        in_whitelist = False
        for raw in open(cfg.subscribe_file, encoding="utf-8", errors="ignore"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("["):
                in_whitelist = "WHITELIST" in line.upper()
                continue
            if line.startswith("#") or in_whitelist:
                continue
            m = re.match(r'^(\S+)(?:\s+UA="([^"]*)")?', line)
            if m:
                add_source(m.group(1), m.group(2) or cfg.user_agent)
    log(f"订阅源合计 {len(sources)} 个（已按地址去重）")

    # ---------- 2. 抓取 + 解析 ----------
    raw_streams: list[Stream] = []
    failed_sources: list[str] = []
    ok_count = 0
    if cfg.open_update:
        for idx, (url, ua) in enumerate(sources, 1):
            text = fetch_text(url, cfg.request_timeout, ua, cfg.cdn_url)
            if not text.strip():
                failed_sources.append(url)
                log(f"[{idx}/{len(sources)}] 抓取失败：{url}", "WARN")
                continue
            items = parse_any(text, source=url)
            ok_count += 1
            raw_streams.extend(items)
            log(f"[{idx}/{len(sources)}] {len(items):>5} 条 <- {url}")

    # ---------- 3. 合并本地源（模板 + local.txt） ----------
    template_names: list[str] = []   # 模板里声明的频道顺序，决定结果排列优先级
    template_streams = 0
    if cfg.source_file and os.path.isfile(cfg.source_file):
        items = parse_any(open(cfg.source_file, encoding="utf-8", errors="ignore").read(), "template")
        for st in items:
            n = normalize_name(st.name)
            if n and n not in template_names:
                template_names.append(n)
        real = [s for s in items if s.url]
        template_streams = len(real)
        raw_streams.extend(real)
        log(f"模板 {os.path.basename(cfg.source_file)}：声明 {len(template_names)} 个频道，"
            f"其中 {template_streams} 条自带地址")
    if cfg.local_file and os.path.isfile(cfg.local_file):
        items = parse_any(open(cfg.local_file, encoding="utf-8", errors="ignore").read(), "local")
        real = [s for s in items if s.url]
        raw_streams.extend(real)
        for st in real:
            n = normalize_name(st.name)
            if n and n not in template_names:
                template_names.append(n)
        log(f"本地源 local.txt 载入 {len(real)} 条")

    log(f"原始条目合计 {len(raw_streams)} 条")

    # ---------- 4. 过滤 + 归一化 + 去重 ----------
    kept: list[Stream] = []
    seen_urls: set[str] = set()
    stats = {"黑名单": 0, "重复URL": 0, "协议不符": 0}
    for st in raw_streams:
        ok, reason = passes_filters(st, cfg, domains, keywords)
        if not ok:
            stats["黑名单" if "黑名单" in reason else "协议不符"] += 1
            continue
        st.name = normalize_name(st.name)
        if st.url in seen_urls:
            stats["重复URL"] += 1
            continue
        seen_urls.add(st.url)
        kept.append(st)
    log(f"过滤后 {len(kept)} 条（丢弃：{stats}）")

    if cfg.max_candidates and len(kept) > cfg.max_candidates:
        log(f"候选数超上限，按来源轮流取样后截断至 {cfg.max_candidates} 条", "WARN")
        kept = interleave_by_source(kept)[: cfg.max_candidates]

    # ---------- 5. 并发测速 ----------
    if args.skip_probe or not cfg.open_update:
        log("已跳过测速，所有条目视为未校验", "WARN")
        valid = kept
    else:
        log(f"开始测速校验，{cfg.probe_workers} 线程，单源超时 {cfg.probe_timeout}s ...")
        done, valid = 0, []
        with ThreadPoolExecutor(max_workers=max(1, cfg.probe_workers)) as pool:
            futures = {pool.submit(probe, st, cfg): st for st in kept}
            for fut in as_completed(futures):
                done += 1
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res:
                    valid.append(res)
                if done % 200 == 0:
                    log(f"  进度 {done}/{len(kept)}，有效 {len(valid)}")
        log(f"测速完成：有效 {len(valid)} / {len(kept)} 条")

    # ---------- 6. 按频道聚合 + 排序 + 截断 ----------
    grouped: dict[str, list[Stream]] = {}
    order: list[str] = []
    for st in valid:
        if st.name not in grouped:
            grouped[st.name] = []
            order.append(st.name)
        grouped[st.name].append(st)

    final: dict[str, list[Stream]] = {}
    for name in order:
        items = sorted(grouped[name], key=lambda s: s.sort_key)
        # 同频道内部按 URL 再去一次重（不同订阅源可能重复）
        uniq, seen = [], set()
        for it in items:
            if it.url in seen:
                continue
            seen.add(it.url)
            uniq.append(it)
            if len(uniq) >= cfg.urls_limit:
                break
        if uniq:
            final[name] = uniq

    # 模板频道优先排序：demo.txt 里声明的频道排最前，且保持声明顺序
    ordered_names = [n for n in template_names if n in final]
    missing = [n for n in template_names if n not in final]
    ordered_names += [n for n in final if n not in ordered_names]
    if template_names:
        log(f"模板频道命中 {len(template_names) - len(missing)}/{len(template_names)}，"
            f"未命中 {len(missing)} 个（多为本轮未抓到可用地址）")
        if missing:
            log(f"  未命中示例：{'、'.join(missing[:12])}", "WARN")
    log(f"最终产出 {len(ordered_names)} 个频道，共 "
        f"{sum(len(v) for v in final.values())} 条有效地址")

    # ---------- 6.5 点播（非直播）源聚合 ----------
    sites: list[Site] = []
    spider, parses, flags = "", [], []
    sites_stats: dict = {}
    if cfg.open_sites:
        log("开始聚合点播源（sites / spider / parses / flags）...")
        sites, spider, parses, flags, sites_stats = build_sites(cfg)
        by_type: dict[int, int] = {}
        for st in sites:
            by_type[st.type] = by_type.get(st.type, 0) + 1
        log(f"点播源合计 {len(sites)} 条（type 分布 {by_type}），"
            f"parses {len(parses)} 条，flags {len(flags)} 条")
        if spider:
            log(f"spider = {spider}")

    # ---------- 7. 写文件 ----------
    os.makedirs(os.path.dirname(cfg.final_file), exist_ok=True)
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    # 7.1 result.txt（按分类分组）
    lines: list[str] = []
    if cfg.open_update_time:
        lines.append(f"更新时间,{now_str}")
        lines.append("")
    by_cat: dict[str, list[str]] = {}
    for name in ordered_names:
        cat = classify(name, cats)
        by_cat.setdefault(cat, []).append(name)
    cat_order = [c[0] for c in cats if c[0] in by_cat]
    cat_order += [c for c in by_cat if c not in cat_order]
    for cat in cat_order:
        lines.append(f"{cat},#genre#")
        for name in by_cat[cat]:
            urls = []
            for st in final[name]:
                u = st.url
                if cfg.open_url_info:
                    res = f"{st.resolution}p" if st.resolution else "未知"
                    u = f"{u}${st.source.split('/')[2] if '//' in st.source else st.source}|{res}|{st.ip_type}"
                urls.append(u)
            lines.append(f"{name},{'#'.join(urls)}")
        lines.append("")
    write_text(cfg.final_file, "\n".join(lines).rstrip() + "\n")
    log(f"已生成 {os.path.relpath(cfg.final_file, cfg.root)}")

    # 7.2 result.m3u
    if cfg.open_m3u_result:
        m3u = ["#EXTM3U"]
        if cfg.open_update_time:
            m3u.append(f"# 更新时间：{now_str}")
        for cat in cat_order:
            for name in by_cat[cat]:
                for st in final[name]:
                    logo = st.logo or f"https://www.xn--rgv465a.top/tvlogo/png/{name}.png"
                    m3u.append(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}" '
                               f'group-title="{cat}",{name}')
                    m3u.append(st.url)
        write_text(cfg.final_m3u_file, "\n".join(m3u) + "\n")
        log(f"已生成 {os.path.relpath(cfg.final_m3u_file, cfg.root)}")

    # 7.3 source.json（TVBox 配置：lives 直播 + sites 点播，直接作为「配置地址」用）
    base = cfg.publish_base_url
    rel_txt = os.path.relpath(cfg.final_file, cfg.root).replace(os.sep, "/")
    rel_m3u = os.path.relpath(cfg.final_m3u_file, cfg.root).replace(os.sep, "/")
    txt_url = f"{base}/{rel_txt}" if base else rel_txt
    m3u_url = f"{base}/{rel_m3u}" if base else rel_m3u
    sites_payload = [s.to_json() for s in sites]
    source_json = {
        "name": "我的 TVBox 配置源",
        "version": now_str,
        "update_time": now_str,
        "spider": spider,
        "sites": sites_payload,
        "lives": [
            {"name": "电视直播(TXT)", "type": 0, "url": txt_url, "playerType": 1},
            {"name": "电视直播(M3U)", "type": 0, "url": m3u_url, "playerType": 1},
        ],
        "parses": parses,
        "flags": flags,
        "wallpaper": "",
        "categories": cat_order,
        "stats": {
            "channels": len(ordered_names),
            "streams": sum(len(v) for v in final.values()),
            "sites": len(sites),
            "sources_ok": ok_count,
            "sources_failed": len(failed_sources),
        },
    }
    write_text(cfg.source_json, json.dumps(source_json, ensure_ascii=False, indent=2) + "\n")
    log(f"已生成 {os.path.relpath(cfg.source_json, cfg.root)}"
        f"（直播 {len(ordered_names)} 频道 / 点播 {len(sites)} 站）")

    # 7.3b 纯点播配置，方便只想用点播的人
    if cfg.open_sites:
        vod = {
            "name": "我的 TVBox 点播配置",
            "version": now_str,
            "update_time": now_str,
            "spider": spider,
            "sites": sites_payload,
            "parses": parses,
            "flags": flags,
            "wallpaper": "",
            "lives": [],
        }
        write_text(cfg.sites_json, json.dumps(vod, ensure_ascii=False, indent=2) + "\n")
        log(f"已生成 {os.path.relpath(cfg.sites_json, cfg.root)}")

    # 7.4 报告
    report = {
        "run_at": now_str,
        "elapsed_sec": round(time.time() - t0, 1),
        "live": {
            "raw": len(raw_streams),
            "after_filter": len(kept),
            "verified": len(valid),
            "channels": len(ordered_names),
            "streams_out": sum(len(v) for v in final.values()),
            "sources_ok": ok_count,
            "sources_failed": failed_sources,
            "category_counts": {c: len(by_cat[c]) for c in cat_order},
            "hit_rate": f"{len(valid)}/{len(kept)}",
        },
        "vod": {
            "sites_out": len(sites),
            "type_counts": {str(t): sum(1 for s in sites if s.type == t)
                            for t in sorted({s.type for s in sites})},
            "spider": spider,
            "parses": len(parses),
            "flags": len(flags),
            **sites_stats,
        },
    }
    write_text(cfg.report_file, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    # 7.5 自动停用失效订阅源
    if cfg.open_auto_disable_source and failed_sources and os.path.isfile(cfg.subscribe_file):
        lines_in = open(cfg.subscribe_file, encoding="utf-8", errors="ignore").read().splitlines()
        out, changed = [], 0
        for ln in lines_in:
            hit = next((f for f in failed_sources if f in ln), None)
            if hit and not ln.lstrip().startswith("#"):
                out.append(f"# [自动停用 {now_str}] {ln}")
                changed += 1
            else:
                out.append(ln)
        if changed:
            write_text(cfg.subscribe_file, "\n".join(out) + "\n")
            log(f"已自动停用 {changed} 个失效订阅源，请重新拉取仓库", "WARN")

    write_text(cfg.log_file, "\n".join(_log_lines) + "\n")
    log(f"===== 全部完成，耗时 {time.time() - t0:.1f}s =====")
    return 0


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


if __name__ == "__main__":
    sys.exit(main())
