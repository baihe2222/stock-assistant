#!/usr/bin/env python3
"""
A股/港股股票实时行情查询（基于新浪行情接口）

功能：
- 输入一个或多个A股代码（如 600519、000001），实时查询：当前价、涨跌幅、涨跌额、成交量/额、委比、买卖比等
- 自动推断交易所前缀（sh/sz/bj）
- 可循环刷新显示
- 可选打印五档盘口

使用示例：
  1) 查询单个：
     python a_stock_quote.py 600519

  2) 查询多个：
     python a_stock_quote.py 600519 000001 300750

  3) 循环刷新（每2秒）：
     python a_stock_quote.py 600519 --loop -i 2

  4) 展示五档盘口：
     python a_stock_quote.py 600519 --detail

说明：
- 数据来源于新浪行情接口，仅用于学习交流。
"""

from __future__ import annotations

import argparse
import sys
import time
import os
import atexit
from typing import Dict, List, Optional, Tuple
import json
import math
import random
import logging

try:
    import requests  # type: ignore
    HAS_REQUESTS = True
except Exception:  # requests may be unavailable
    requests = None  # type: ignore
    HAS_REQUESTS = False

import urllib.request
import urllib.error
import urllib.parse

# 配置结构化日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SINA_QUOTE_ENDPOINT = "https://hq.sinajs.cn/list="
SINA_QUOTE_FALLBACK_ENDPOINT = "https://hq.sina.com.cn/list="  # 备用新浪行情接口
SINA_HEADERS = {
    # Referer 头有助于避免部分防盗链策略
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Charset": "GBK,utf-8;q=0.7,*;q=0.3",
}

SINA_SUGGEST_ENDPOINT = "https://suggest3.sinajs.cn/suggest"


try:
    import readline  # type: ignore
    HAS_READLINE = True
except Exception:
    readline = None  # type: ignore
    HAS_READLINE = False


def setup_readline_history(history_path: Optional[str] = None) -> None:
    """Enable arrow-key history for input() and persist it across sessions."""
    if not HAS_READLINE:
        return
    path = history_path or os.path.expanduser("~/.a_stock_quote_history")
    try:
        if os.path.exists(path):
            readline.read_history_file(path)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        readline.set_history_length(1000)  # type: ignore[attr-defined]
    except Exception:
        pass

    # Improve interactivity: enable prefix history search on Up/Down and
    # reduce ESC sequence waiting time so arrow keys feel more responsive.
    try:
        doc = str(getattr(readline, "__doc__", "")).lower()
        is_libedit = ("libedit" in doc) or ("editline" in doc)

        if is_libedit:
            # libedit (common on macOS). Use its 'bind' syntax and ed-search-*
            try:
                readline.parse_and_bind("bind -e")  # emacs mode
            except Exception:
                pass
            for cmd in (
                "bind '^[[A' ed-search-prev-history",
                "bind '^[[B' ed-search-next-history",
                # Cover some terminals' modified sequences
                "bind '^[[1;5A' ed-search-prev-history",
                "bind '^[[1;5B' ed-search-next-history",
            ):
                try:
                    readline.parse_and_bind(cmd)
                except Exception:
                    pass
        else:
            # GNU Readline. Use native settings and keymaps.
            for cmd in (
                "set editing-mode emacs",
                "set bell-style none",
                # Reduce delay waiting for ambiguous ESC sequences (ms)
                "set keyseq-timeout 20",
                # Prefix history search with Up/Down (both normal and application mode)
                r"\"\e[A\": history-search-backward",
                r"\"\e[B\": history-search-forward",
                r"\"\eOA\": history-search-backward",
                r"\"\eOB\": history-search-forward",
            ):
                try:
                    readline.parse_and_bind(cmd)
                except Exception:
                    pass
    except Exception:
        # Best-effort improvements; ignore if unsupported
        pass

    def _save_history() -> None:
        try:
            readline.write_history_file(path)  # type: ignore[attr-defined]
        except Exception:
            pass

    atexit.register(_save_history)


def http_get_text(url: str, headers: Dict[str, str], timeout: float, max_retries: int = 3) -> str:
    """HTTP GET that returns decoded text (GBK preferred), using requests if available, otherwise urllib.
    
    Args:
        url: 请求URL
        headers: HTTP请求头
        timeout: 超时时间（秒）
        max_retries: 最大重试次数（默认3次）
    
    Returns:
        解码后的文本内容
    
    Raises:
        最后一次请求的异常
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            if HAS_REQUESTS:
                resp = requests.get(url, headers=headers, timeout=timeout)  # type: ignore[attr-defined]
                resp.encoding = "gbk"
                if attempt > 0:
                    logger.info(f"请求成功 url={url} attempt={attempt + 1}")
                return resp.text
            else:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp_obj:
                    data = resp_obj.read()
                    if attempt > 0:
                        logger.info(f"请求成功 url={url} attempt={attempt + 1}")
                    try:
                        return data.decode("gbk", errors="ignore")
                    except Exception:
                        return data.decode("utf-8", errors="ignore")
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                # 指数退避 + 抖动（避免惊群效应）
                base_delay = min(2 ** attempt, 8)  # 最大8秒
                jitter = random.uniform(0, 0.3 * base_delay)  # 30%抖动
                delay = base_delay + jitter
                logger.warning(
                    f"请求失败，{delay:.2f}秒后重试 url={url} attempt={attempt + 1}/{max_retries + 1} error={str(e)}"
                )
                time.sleep(delay)
            else:
                logger.error(f"请求最终失败 url={url} attempts={max_retries + 1} error={str(e)}")
    
    raise last_exception  # type: ignore


def parse_suggest_value(text: str) -> List[Dict[str, str]]:
    """Parse Sina suggest API raw text to a list of entries.

    The response format is like:
        var suggestvalue="name,type,code,full,display,...;name2,type2,code2,full2,display2,...";

    Returns a list of dicts with minimal fields: name, type, code, full.
    """
    if not text:
        return []

    # Extract the content between the first and the last double quotes
    start = text.find('"')
    end = text.rfind('"')
    if start == -1 or end == -1 or end <= start:
        return []

    payload = text[start + 1:end]
    if not payload:
        return []

    entries: List[Dict[str, str]] = []
    for rec in payload.split(";"):
        if not rec:
            continue
        fields = rec.split(",")
        if len(fields) < 4:
            continue
        name = fields[0].strip()
        typ = fields[1].strip()
        code = fields[2].strip()
        full = fields[3].strip()
        entries.append({
            "name": name,
            "type": typ,
            "code": code,
            "full": full,
        })
    return entries


def suggest_full_codes_for_key(keyword: str, timeout: float = 5.0) -> List[str]:
    """Query Sina suggest API and return candidate full codes like sh600000 / hk00700.

    Filters to exchange-prefixed codes that our quote endpoint can handle: sh/sz/bj/hk.
    """
    key = keyword.strip()
    if not key:
        return []
    urls = [
        # A股优先
        f"{SINA_SUGGEST_ENDPOINT}/type=11&key={urllib.parse.quote(key)}",
        # 港股候选
        f"{SINA_SUGGEST_ENDPOINT}/type=31&key={urllib.parse.quote(key)}",
        # 兜底（不指定 type）
        f"{SINA_SUGGEST_ENDPOINT}/key={urllib.parse.quote(key)}",
    ]

    result: List[str] = []
    seen = set()

    for url in urls:
        try:
            text = http_get_text(url, headers=SINA_HEADERS, timeout=timeout)
        except Exception:
            continue
        entries = parse_suggest_value(text)
        for e in entries:
            typ = (e.get("type") or e.get("typ") or "").strip()
            full_raw = (e.get("full") or "").strip()
            code_raw = (e.get("code") or "").strip()
            # A股：full 已带前缀（sh/sz/bj）
            full_lower = full_raw.lower()
            if full_lower.startswith(("sh", "sz", "bj")):
                full = full_lower
            else:
                # 港股 suggest 返回 5 位数字代码或场内品种代码，转换为 hk 前缀
                # 优先使用 full 字段，否则回落到 code 字段
                val = full_raw or code_raw
                if not val:
                    continue
                # 纯数字 => 左侧补零到 5 位
                if val.isdigit():
                    val = val.zfill(5)
                else:
                    # 指数/品种代码 => 大写
                    val = val.upper()
                full = f"hk{val}"

            if full not in seen and full.startswith(("sh", "sz", "bj", "hk")):
                seen.add(full)
                result.append(full)

        if result:
            break

    return result


def build_index_alias_map() -> Dict[str, str]:
    """Builtin index name aliases to full codes.

    Covers common indices for convenience.
    """
    aliases: Dict[str, str] = {
        # Shanghai Composite
        "上证": "sh000001",
        "上证指数": "sh000001",
        "上证综指": "sh000001",

        # SZ Component Index
        "深证成指": "sz399001",
        "深成指": "sz399001",

        # ChiNext
        "创业板": "sz399006",
        "创业板指": "sz399006",
        "创业板指数": "sz399006",

        # STAR 50
        "科创50": "sh000688",
        "科创板50": "sh000688",
        "科创50指数": "sh000688",

        # HS300
        "沪深300": "sh000300",
        "沪深三百": "sh000300",

        # SSE 50
        "上证50": "sh000016",
        "上证五十": "sh000016",

        # CSI 500
        "中证500": "sh000905",
        # CSI 1000
        "中证1000": "sh000852",

        # Hong Kong indices (Hang Seng family)
        "恒生指数": "hkHSI",
        "恒指": "hkHSI",
        "HSI": "hkHSI",
        "恒生科技指数": "hkHSTECH",
        "恒生科技": "hkHSTECH",
        "HSTECH": "hkHSTECH",
        "恒生中国企业指数": "hkHSCEI",
        "国企指数": "hkHSCEI",
        "HSCEI": "hkHSCEI",
        "恒生香港中资企业指数": "hkHSCCI",
        "HSCCI": "hkHSCCI",
    }
    # Also allow ascii fallbacks (no change needed for Chinese case)
    return aliases


def resolve_inputs_to_prefixed_codes(inputs: List[str]) -> List[str]:
    """Resolve a list of user inputs (codes or names) to prefixed codes.

    Resolution order per token:
      1) If it looks like a code (with/without prefix), normalize directly
      2) Builtin index aliases
      3) Sina suggest API (first sh/sz/bj result)
    """
    alias_map = build_index_alias_map()

    resolved: List[str] = []
    seen = set()
    for raw in inputs:
        token = (raw or "").strip()
        if not token:
            continue

        # 1) Direct code normalization
        direct = normalize_codes([token])
        if direct:
            for full in direct:
                if full not in seen:
                    seen.add(full)
                    resolved.append(full)
            continue

        # 2) Builtin alias
        alias_code = alias_map.get(token)
        if alias_code and alias_code not in seen:
            seen.add(alias_code)
            resolved.append(alias_code)
            continue

        # 3) Suggest API
        candidates = suggest_full_codes_for_key(token)
        if candidates:
            first = candidates[0]
            if first not in seen:
                seen.add(first)
                resolved.append(first)
            continue

        # Not resolved; skip silently (query_and_display will handle empty)

    return resolved


def infer_exchange_prefix(stock_code: str) -> Optional[str]:
    """Infer A-share exchange prefix for a given 6-digit code.

    Returns one of {"sh", "sz", "bj"} or None if cannot infer.
    """
    code = stock_code.strip().lower()
    if not code:
        return None

    # Already prefixed (including hk)
    if code.startswith("sh") or code.startswith("sz") or code.startswith("bj") or code.startswith("hk"):
        return code[:2]

    # Normalize digits only
    if not code.isdigit() or len(code) != 6:
        return None

    first_digit = code[0]

    # Shanghai: 6/9/5 typically (A股主板、B股、基金/债等)
    if first_digit in {"6", "9", "5"}:
        return "sh"

    # Shenzhen: 0/2/3 (主板/中小板/创业板)
    if first_digit in {"0", "2", "3"}:
        return "sz"

    # Beijing: 4/8 often used for 北交所
    if first_digit in {"4", "8"}:
        return "bj"

    return None


def normalize_codes(codes: List[str]) -> List[str]:
    """Normalize input codes to Sina format with exchange prefix.

    - Accepts inputs like "600519", "sh600519", case-insensitive.
    - Returns only valid, de-duplicated prefixed codes.
    """
    normalized: List[str] = []
    seen = set()
    for raw in codes:
        code = raw.strip().lower()
        if not code:
            continue

        full: Optional[str] = None

        # Explicit prefixes
        if code.startswith(("sh", "sz", "bj")):
            full = code
        elif code.startswith("hk"):
            tail = code[2:]
            if tail.isdigit():
                tail = tail.zfill(5)
            else:
                tail = tail.upper()
            full = "hk" + tail
        else:
            # Digits only
            if code.isdigit():
                if len(code) == 6:
                    prefix = infer_exchange_prefix(code)
                    if not prefix:
                        continue
                    full = prefix + code
                elif 1 <= len(code) <= 5:
                    # Treat as HK numeric code
                    full = "hk" + code.zfill(5)
                else:
                    continue
            else:
                # Non-digit and no explicit prefix => not a direct code
                # Leave for alias/suggest resolution
                continue

        if full not in seen:
            seen.add(full)
            normalized.append(full)
    return normalized


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value: str) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def parse_sina_line(line: str) -> Optional[Dict[str, object]]:
    """Parse one line of Sina quote response.

    Example line:
    var hq_str_sh600000="浦发银行,10.85,10.85,10.93,10.94,10.80,10.92,10.93,104808884,1140674352,85500,10.92,...,2024-09-13,15:00:03,00";
    """
    if not line or "hq_str_" not in line:
        return None

    try:
        head, payload = line.split("=", 1)
    except ValueError:
        return None

    # Extract code like sh600000
    code_pos = head.rfind("hq_str_")
    if code_pos == -1:
        return None
    var_name = head[code_pos + len("hq_str_"):].strip()
    var_name = var_name.replace(" ", "")

    # payload is like "...";
    if not payload.strip().startswith("\""):
        return None
    content = payload.strip().strip("\r\n").strip(";")
    if content.startswith("\"") and content.endswith("\""):
        content = content[1:-1]

    fields = content.split(",") if content else []
    exchange = var_name[:2]
    code_digits = var_name[2:]

    # Hong Kong format (19 fields)
    if exchange == "hk":
        if len(fields) < 19 or (len(fields) == 1 and not fields[0]):
            return None
        name_cn = fields[1]
        current_price = _safe_float(fields[2])
        prev_close = _safe_float(fields[3])
        high_price = _safe_float(fields[4])
        low_price = _safe_float(fields[5])
        today_open = _safe_float(fields[6])
        bid_price = _safe_float(fields[9])
        ask_price = _safe_float(fields[10])
        amount_hkd = _safe_float(fields[11])
        volume_shares = _safe_int(fields[12])
        trade_date = fields[17] if len(fields) > 17 else ""
        trade_time = fields[18] if len(fields) > 18 else ""

        return {
            "exchange": exchange,
            "code": code_digits,
            "name": name_cn,
            "current": current_price,
            "prev_close": prev_close,
            "open": today_open,
            "high": high_price,
            "low": low_price,
            "bid": bid_price,
            "ask": ask_price,
            "volume_shares": volume_shares,
            "amount_yuan": amount_hkd,  # amount in HKD for HK market
            "buys": [],  # hk endpoint does not provide 5-level book here
            "sells": [],
            "date": trade_date,
            "time": trade_time,
        }

    # A-share format (>=32 fields)
    if len(fields) < 32 or (len(fields) == 1 and not fields[0]):
        # invalid or empty
        return None

    name = fields[0]
    today_open = _safe_float(fields[1])
    prev_close = _safe_float(fields[2])
    current_price = _safe_float(fields[3])
    high_price = _safe_float(fields[4])
    low_price = _safe_float(fields[5])
    bid_price = _safe_float(fields[6])
    ask_price = _safe_float(fields[7])
    volume_shares = _safe_int(fields[8])  # 累计成交量（股）
    amount_yuan = _safe_float(fields[9])  # 累计成交额（元）

    # Five-level order book
    # Buy: (vol, price) at indices (10,11), (12,13), (14,15), (16,17), (18,19)
    # Sell: (vol, price) at indices (20,21), (22,23), (24,25), (26,27), (28,29)
    buy_levels: List[Tuple[float, float]] = []
    sell_levels: List[Tuple[float, float]] = []

    try:
        for i in range(5):
            vol_idx = 10 + i * 2
            price_idx = 11 + i * 2
            buy_levels.append((_safe_int(fields[vol_idx]), _safe_float(fields[price_idx])))

        for i in range(5):
            vol_idx = 20 + i * 2
            price_idx = 21 + i * 2
            sell_levels.append((_safe_int(fields[vol_idx]), _safe_float(fields[price_idx])))
    except Exception:
        # be tolerant of incomplete data
        pass

    trade_date = fields[30] if len(fields) > 30 else ""
    trade_time = fields[31] if len(fields) > 31 else ""

    return {
        "exchange": exchange,
        "code": code_digits,
        "name": name,
        "current": current_price,
        "prev_close": prev_close,
        "open": today_open,
        "high": high_price,
        "low": low_price,
        "bid": bid_price,
        "ask": ask_price,
        "volume_shares": volume_shares,
        "amount_yuan": amount_yuan,
        "buys": buy_levels,   # list[(vol, price)] level1..5
        "sells": sell_levels, # list[(vol, price)] level1..5
        "date": trade_date,
        "time": trade_time,
    }


def fetch_sina_quotes(prefixed_codes: List[str], timeout: float = 5.0) -> Dict[str, Dict[str, object]]:
    """Fetch quotes for a list of prefixed codes from Sina.

    Returns a dict keyed by full code like "sh600519".
    \u5c1d\u8bd5\u4e3b\u63a5\u53e3\uff0c\u5931\u8d25\u540e\u5c1d\u8bd5\u5907\u7528\u63a5\u53e3\u3002
    """
    if not prefixed_codes:
        return {}

    codes_str = ",".join(prefixed_codes)
    endpoints = [
        SINA_QUOTE_ENDPOINT + codes_str,
        SINA_QUOTE_FALLBACK_ENDPOINT + codes_str,
    ]
    
    last_exception = None
    for idx, url in enumerate(endpoints):
        try:
            text = http_get_text(url, headers=SINA_HEADERS, timeout=timeout, max_retries=3)
            result: Dict[str, Dict[str, object]] = {}
            for line in text.splitlines():
                parsed = parse_sina_line(line)
                if not parsed:
                    continue
                full_code = f"{parsed['exchange']}{parsed['code']}"
                result[full_code] = parsed
            
            if idx > 0:
                logger.info(f"使用备用接口成功 endpoint={url}")
            return result
        except Exception as e:
            last_exception = e
            logger.warning(f"接口请求失败 endpoint={url} error={str(e)}")
            if idx < len(endpoints) - 1:
                logger.info(f"尝试备用接口 fallback_endpoint={endpoints[idx + 1]}")
    
    # 所有接口都失败，抛出异常
    if last_exception:
        raise last_exception
    return {}


def compute_order_metrics(quote: Dict[str, object]) -> Tuple[float, float]:
    """Compute 委比(%) and 买卖比 from order book.

    委比 = (委买手数合计 - 委卖手数合计) / (委买手数合计 + 委卖手数合计) * 100%
    买卖比 = 委买手数合计 / 委卖手数合计
    注：这里用五档委托量近似代表。
    """
    buys: List[Tuple[int, float]] = quote.get("buys") or []
    sells: List[Tuple[int, float]] = quote.get("sells") or []

    buy_shares = sum(v for v, _ in buys)
    sell_shares = sum(v for v, _ in sells)

    total = buy_shares + sell_shares
    order_ratio = 0.0
    if total > 0:
        order_ratio = (buy_shares - sell_shares) / total * 100.0

    if sell_shares == 0:
        buy_sell_ratio = float("inf") if buy_shares > 0 else 1.0
    else:
        buy_sell_ratio = buy_shares / sell_shares

    return order_ratio, buy_sell_ratio


def format_number(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


# ------------------------------
# EastMoney K-line helpers
# ------------------------------

def _get_eastmoney_secid(full_code: str) -> Optional[str]:
    """Map a Sina-style full code to EastMoney secid.

    EastMoney secid format: "<market>.<code>", where market is:
      - 1 for SH
      - 0 for SZ (and BJ commonly also uses 0 here)
      - 116 for HK (not used for indicators below)
    """
    code = (full_code or "").strip().lower()
    if not code or len(code) < 3:
        return None
    prefix = code[:2]
    digits = code[2:]
    if not digits:
        return None
    if prefix == "sh":
        return f"1.{digits}"
    if prefix == "sz" or prefix == "bj":
        return f"0.{digits}"
    if prefix == "hk":
        # HK market id commonly 116
        return f"116.{digits.zfill(5)}"
    return None


def _http_get_json(url: str, headers: Dict[str, str], timeout: float) -> Dict[str, object]:
    text = http_get_text(url, headers=headers, timeout=timeout, max_retries=3)
    try:
        return json.loads(text)
    except Exception:
        return {}


def fetch_daily_klines_from_eastmoney(full_code: str, limit: int = 130, timeout: float = 6.0) -> List[Dict[str, object]]:
    """Fetch daily K-line data for a stock from EastMoney.

    Returns a list of dicts with keys:
      date, open, close, high, low, volume, amount, amplitude_pct, change_pct, change_amt, turnover_pct
    支持主接口失败后使用备用接口。
    """
    secid = _get_eastmoney_secid(full_code)
    if not secid:
        return []
    
    # klt=101 => 日K; fqt=1 前复权; lmt=limit 条
    query_params = f"?secid={urllib.parse.quote(secid)}&klt=101&fqt=1&lmt={int(limit)}&end=20500101&iscca=1&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    
    # 主接口和备用接口
    endpoints = [
        "https://push2his.eastmoney.com/api/qt/stock/kline/get" + query_params,
        "https://push2.eastmoney.com/api/qt/stock/kline/get" + query_params,
    ]
    
    headers = {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": SINA_HEADERS.get("User-Agent", "Mozilla/5.0"),
        "Accept": "application/json, text/plain, */*",
    }
    
    last_exception = None
    for idx, url in enumerate(endpoints):
        try:
            data = _http_get_json(url, headers=headers, timeout=timeout)
            result: List[Dict[str, object]] = []
            try:
                klines = ((data or {}).get("data") or {}).get("klines") or []
            except Exception:
                klines = []
            
            for rec in klines:
                # rec like: "YYYY-MM-DD,open,close,high,low,volume,amount,amplitude,chg_pct,chg_amt,turnover"
                try:
                    parts = str(rec).split(",")
                    if len(parts) < 11:
                        continue
                    result.append({
                        "date": parts[0],
                        "open": _safe_float(parts[1]),
                        "close": _safe_float(parts[2]),
                        "high": _safe_float(parts[3]),
                        "low": _safe_float(parts[4]),
                        "volume": _safe_float(parts[5]),
                        "amount": _safe_float(parts[6]),
                        "amplitude_pct": _safe_float(parts[7]),
                        "change_pct": _safe_float(parts[8]),
                        "change_amt": _safe_float(parts[9]),
                        "turnover_pct": _safe_float(parts[10]),
                    })
                except Exception:
                    continue
            
            if idx > 0:
                logger.info(f"东方财富日线数据使用备用接口成功 endpoint={url}")
            return result
        except Exception as e:
            last_exception = e
            logger.warning(f"东方财富日线数据接口失败 endpoint={url} error={str(e)}")
            if idx < len(endpoints) - 1:
                logger.info(f"尝试备用接口 fallback_endpoint={endpoints[idx + 1]}")
    
    # 所有接口都失败，返回空列表（避免影响其他功能）
    logger.error(f"东方财富日线数据所有接口都失败 code={full_code} error={str(last_exception)}")
    return []


def fetch_minute_klines_from_eastmoney(full_code: str, klt: int = 60, limit: int = 200, timeout: float = 6.0) -> List[Dict[str, object]]:
    """Fetch intraday minute-level K-line data (e.g., 60-min) for a stock from EastMoney.
    
    klt examples:
      1: 1-minute, 5: 5-minute, 15: 15-minute, 30: 30-minute, 60: 60-minute
    Returns a list of dicts with keys:
      datetime, open, close, high, low, volume, amount, amplitude_pct, change_pct, change_amt, turnover_pct
    支持主接口失败后使用备用接口。
    """
    secid = _get_eastmoney_secid(full_code)
    if not secid:
        return []
    
    klt_val = int(klt)
    query_params = f"?secid={urllib.parse.quote(secid)}&klt={klt_val}&fqt=1&lmt={int(limit)}&end=20500101&iscca=1&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    
    # 主接口和备用接口
    endpoints = [
        "https://push2his.eastmoney.com/api/qt/stock/kline/get" + query_params,
        "https://push2.eastmoney.com/api/qt/stock/kline/get" + query_params,
    ]
    
    headers = {
        "Referer": "https://quote.eastmoney.com/",
        "User-Agent": SINA_HEADERS.get("User-Agent", "Mozilla/5.0"),
        "Accept": "application/json, text/plain, */*",
    }
    
    last_exception = None
    for idx, url in enumerate(endpoints):
        try:
            data = _http_get_json(url, headers=headers, timeout=timeout)
            result: List[Dict[str, object]] = []
            try:
                klines = ((data or {}).get("data") or {}).get("klines") or []
            except Exception:
                klines = []
            
            for rec in klines:
                # rec like: "YYYY-MM-DD HH:MM,open,close,high,low,volume,amount,amplitude,chg_pct,chg_amt,turnover"
                try:
                    parts = str(rec).split(",")
                    if len(parts) < 11:
                        continue
                    result.append({
                        "datetime": parts[0],
                        "open": _safe_float(parts[1]),
                        "close": _safe_float(parts[2]),
                        "high": _safe_float(parts[3]),
                        "low": _safe_float(parts[4]),
                        "volume": _safe_float(parts[5]),
                        "amount": _safe_float(parts[6]),
                        "amplitude_pct": _safe_float(parts[7]),
                        "change_pct": _safe_float(parts[8]),
                        "change_amt": _safe_float(parts[9]),
                        "turnover_pct": _safe_float(parts[10]),
                    })
                except Exception:
                    continue
            
            if idx > 0:
                logger.info(f"东方财富分钟线数据使用备用接口成功 endpoint={url}")
            return result
        except Exception as e:
            last_exception = e
            logger.warning(f"东方财富分钟线数据接口失败 endpoint={url} error={str(e)}")
            if idx < len(endpoints) - 1:
                logger.info(f"尝试备用接口 fallback_endpoint={endpoints[idx + 1]}")
    
    # 所有接口都失败，返回空列表（避免影响其他功能）
    logger.error(f"东方财富分钟线数据所有接口都失败 code={full_code} klt={klt_val} error={str(last_exception)}")
    return []


def _simple_moving_average(values: List[float], window: int) -> List[float]:
    if window <= 0:
        return []
    out: List[float] = []
    cumsum = 0.0
    for i, v in enumerate(values):
        cumsum += v
        if i >= window:
            cumsum -= values[i - window]
        if i + 1 >= window:
            out.append(cumsum / window)
    return out


def _ema(values: List[float], period: int) -> List[float]:
    if period <= 0 or not values:
        return []
    k = 2.0 / (period + 1.0)
    ema_vals: List[float] = []
    ema_prev = values[0]
    ema_vals.append(ema_prev)
    for i in range(1, len(values)):
        ema_prev = values[i] * k + ema_prev * (1.0 - k)
        ema_vals.append(ema_prev)
    return ema_vals


def compute_kdj_j(bars: List[Dict[str, object]], period: int = 9) -> Optional[float]:
    if not bars:
        return None
    highs = [float(b.get("high") or 0.0) for b in bars]
    lows = [float(b.get("low") or 0.0) for b in bars]
    closes = [float(b.get("close") or 0.0) for b in bars]
    if len(closes) == 0:
        return None
    k_prev = 50.0
    d_prev = 50.0
    for i in range(len(closes)):
        start = max(0, i - period + 1)
        window_high = max(highs[start:i + 1])
        window_low = min(lows[start:i + 1])
        denom = (window_high - window_low)
        rsv = 0.0 if denom <= 0 else (closes[i] - window_low) / denom * 100.0
        k_curr = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
        d_curr = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_curr
        k_prev, d_prev = k_curr, d_curr
    j = 3.0 * k_prev - 2.0 * d_prev
    return j


def compute_macd_status(bars: List[Dict[str, object]]) -> str:
    """Return '金叉' / '死叉' / '—' based on last two DIF-DEA relationships."""
    closes = [float(b.get("close") or 0.0) for b in bars]
    if len(closes) < 2:
        return "—"
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    n = min(len(ema12), len(ema26))
    if n < 2:
        return "—"
    dif = [ema12[i] - ema26[i] for i in range(n)]
    dea = _ema(dif, 9)
    if len(dea) < 2:
        return "—"
    last = dif[-1] - dea[-1]
    prev = dif[-2] - dea[-2]
    if last >= 0 and prev < 0:
        return "金叉"
    if last <= 0 and prev > 0:
        return "死叉"
    return "—"


def _format_percent(value: Optional[float], digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value:.{digits}f}%"


def _print_table(headers: List[str], rows: List[List[str]]) -> None:
    # compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    # header
    header_line = " ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = " ".join("-" * widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)
    for row in rows:
        print(" ".join((row[i]).ljust(widths[i]) for i in range(len(headers))))


def print_quote_line(quote: Dict[str, object]) -> None:
    current_price = float(quote.get("current") or 0.0)
    prev_close = float(quote.get("prev_close") or 0.0)
    change = current_price - prev_close if prev_close > 0 else 0.0
    change_pct = (change / prev_close * 100.0) if prev_close > 0 else 0.0
    order_ratio, buy_sell_ratio = compute_order_metrics(quote)

    volume_shares = int(quote.get("volume_shares") or 0)
    amount_yuan = float(quote.get("amount_yuan") or 0.0)

    code_display = f"{quote['exchange']}{quote['code']}"
    name_display = str(quote.get("name") or "-")
    tdate = str(quote.get("date") or "")
    ttime = str(quote.get("time") or "")

    # Simple aligned one-line output
    if code_display.startswith("hk"):
        volume_part = f"成交量 {volume_shares}股 成交额 {format_number(amount_yuan / 1e8, 2)}亿"
        ratio_part = ""  # HK: skip 委比/买卖比
    else:
        volume_part = f"成交量 {volume_shares // 100}手 成交额 {format_number(amount_yuan / 1e8, 2)}亿"
        ratio_part = f"| 委比 {format_number(order_ratio, 2)}% 买卖比 {('∞' if buy_sell_ratio == float('inf') else format_number(buy_sell_ratio, 2))} "

    line = (
        f"{code_display} {name_display} | 现价 {format_number(current_price, 2)} "
        f"| 涨跌 {format_number(change, 2)} {format_number(change_pct, 2)}% "
        f"| {volume_part} "
        f"{ratio_part}| {tdate} {ttime}"
    )
    print(line)


def print_quote_kv(
    quote: Dict[str, object],
    change_amt_str: Optional[str] = None,
    ma12_pos_str: Optional[str] = None,
    kdj_j_str: Optional[str] = None,
    macd_status_str: Optional[str] = None,
    turnover_pct_str: Optional[str] = None,
) -> None:
    """Print quote in key-value Chinese label style.

    Example:
      名称: 浦发银行
      代码: 600000
      现价: 10.92
      涨跌额: 0.07
      涨跌幅: 0.64%
      换手率: 1.23%
      距MA12: 1.23%
      KDJ_J: 85.60
      MACD: 金叉
      成交量: 1234手
      成交额: 12.34亿
      委比: 10.11%
      买卖比: 1.23
      时间: 2024-09-13 15:00:03
    """
    current_price = float(quote.get("current") or 0.0)
    prev_close = float(quote.get("prev_close") or 0.0)
    change_pct = (current_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
    order_ratio, buy_sell_ratio = compute_order_metrics(quote)

    exchange = str(quote.get("exchange") or "")
    code_digits = str(quote.get("code") or "")
    name_display = str(quote.get("name") or "-")
    volume_shares = int(quote.get("volume_shares") or 0)
    amount_yuan = float(quote.get("amount_yuan") or 0.0)
    tdate = str(quote.get("date") or "").strip()
    ttime = str(quote.get("time") or "").strip()

    print(f"名称: {name_display}")
    print(f"代码: {code_digits}")
    print(f"现价: {format_number(current_price, 2)}")
    # 涨跌额（现价-昨收）
    if change_amt_str is None:
        change_amt_str = format_number(current_price - prev_close, 2)
    print(f"涨跌额: {change_amt_str}")
    print(f"涨跌幅: {format_number(change_pct, 2)}%")
    # 技术指标：距MA12/KDJ_J/MACD（若无则用 - 占位）
    print(f"距MA12: {str(ma12_pos_str) if ma12_pos_str else '-'}")
    print(f"KDJ_J: {str(kdj_j_str) if kdj_j_str else '-'}")
    print(f"MACD: {str(macd_status_str) if macd_status_str else '-'}")

    if exchange == "hk":
        print(f"成交量: {volume_shares}股")
        print(f"成交额: {format_number(amount_yuan / 1e8, 2)}亿")
    else:
        print(f"成交量: {volume_shares // 100}手")
        print(f"成交额: {format_number(amount_yuan / 1e8, 2)}亿")
        print(f"委比: {format_number(order_ratio, 2)}%")
        print(f"买卖比: {('∞' if buy_sell_ratio == float('inf') else format_number(buy_sell_ratio, 2))}")

    # Turnover rate (换手率)，若无则显示 "-"
    print(f"换手率: {str(turnover_pct_str) if turnover_pct_str else '-'}")

    ts = f"{tdate} {ttime}".strip()
    if ts:
        print(f"时间: {ts}")
    print("")


def print_order_book(quote: Dict[str, object]) -> None:
    # HK sources do not provide five-level order book in this endpoint
    if str(quote.get("exchange")) == "hk":
        print("  当前接口不提供港股五档盘口。")
        return
    buys: List[Tuple[int, float]] = quote.get("buys") or []
    sells: List[Tuple[int, float]] = quote.get("sells") or []

    # Display from 5 to 1 for sells (ask high to low), and 1 to 5 for buys
    print("  卖盘(五档):")
    for level in range(5, 0, -1):
        idx = level - 1
        if idx < len(sells):
            vol, price = sells[idx]
            print(f"    卖{level}: 价 {format_number(price, 2)} 量 {vol}股")

    print("  买盘(五档):")
    for level in range(1, 6):
        idx = level - 1
        if idx < len(buys):
            vol, price = buys[idx]
            print(f"    买{level}: 价 {format_number(price, 2)} 量 {vol}股")


def query_and_display(codes_or_names: List[str], show_detail: bool = False) -> None:
    normalized = resolve_inputs_to_prefixed_codes(codes_or_names)
    if not normalized:
        print("未识别到有效标的。可输入代码或名称，如 600519、浦发银行、上证指数、科创50")
        return

    try:
        quotes = fetch_sina_quotes(normalized, timeout=5.0)
    except Exception as exc:
        print(f"请求行情失败：{exc}")
        return

    details: List[Tuple[str, Dict[str, object]]] = []

    for full_code in normalized:
        quote = quotes.get(full_code)
        if not quote:
            # Minimal placeholder output when no quote is returned
            print(f"名称: -")
            print(f"代码: {full_code}")
            print("")
            continue

        exchange = str(quote.get("exchange") or "")
        code_digits = str(quote.get("code") or "")
        name_display = str(quote.get("name") or "-")
        current_price = float(quote.get("current") or 0.0)
        prev_close = float(quote.get("prev_close") or 0.0)
        change_pct = (current_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
        tdate = str(quote.get("date") or "")
        ttime = str(quote.get("time") or "")

        turnover_pct_str = "-"
        ma12_pos_str = "-"
        kdj_j_str = "-"
        macd_status_str = "-"

        full = f"{exchange}{code_digits}"
        if exchange in {"sh", "sz", "bj"}:
            # Daily bars for turnover/KDJ/MACD
            bars_daily = fetch_daily_klines_from_eastmoney(full, limit=130)
            if bars_daily:
                # Turnover rate: use last daily record
                last_turnover = float(bars_daily[-1].get("turnover_pct") or 0.0)
                turnover_pct_str = _format_percent(last_turnover)

                # KDJ_J and MACD based on daily bars
                j_val = compute_kdj_j(bars_daily)
                if j_val is not None:
                    kdj_j_str = format_number(float(j_val), 2)
                macd_status_str = compute_macd_status(bars_daily)

            # MA12 position based on 60-minute bars; fall back to daily if unavailable
            bars_60m = fetch_minute_klines_from_eastmoney(full, klt=60, limit=200)
            closes_60m = [float(b.get("close") or 0.0) for b in bars_60m]
            ma12_value: Optional[float] = None
            if len(closes_60m) >= 12:
                ma12_list_60m = _simple_moving_average(closes_60m, 12)
                if ma12_list_60m:
                    ma12_value = float(ma12_list_60m[-1])
            if ma12_value is None and bars_daily:
                closes_daily = [float(b.get("close") or 0.0) for b in bars_daily]
                if len(closes_daily) >= 12:
                    ma12_list_daily = _simple_moving_average(closes_daily, 12)
                    if ma12_list_daily:
                        ma12_value = float(ma12_list_daily[-1])
            if ma12_value is not None and ma12_value != 0:
                diff_pct = (current_price - ma12_value) / ma12_value * 100.0
                ma12_pos_str = _format_percent(diff_pct)

        elif exchange == "hk":
            # HK not computed for now
            pass

        # Print in key-value style，补充：涨跌额、距MA12、KDJ_J、MACD
        change_amt_str = format_number(current_price - prev_close, 2)
        print_quote_kv(
            quote,
            change_amt_str=change_amt_str,
            ma12_pos_str=ma12_pos_str,
            kdj_j_str=kdj_j_str,
            macd_status_str=macd_status_str,
            turnover_pct_str=turnover_pct_str,
        )

        details.append((full, quote))

    if show_detail:
        for _, quote in details:
            print_order_book(quote)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A股/港股实时行情查询（Sina）")
    parser.add_argument(
        "codes",
        nargs="*",
        help=(
            "标的代码或名称，支持：600519、sh600519、浦发银行、上证指数、科创50、00700、hk00700、恒生指数 等，可多只"
        ),
    )
    parser.add_argument("-l", "--loop", action="store_true", help="循环刷新显示")
    parser.add_argument("-i", "--interval", type=float, default=2.0, help="刷新间隔秒(配合 --loop)")
    parser.add_argument("-d", "--detail", action="store_true", help="显示五档盘口")

    args = parser.parse_args(argv)

    codes: List[str] = args.codes
    if not codes:
        # Interactive prompt loop: only 'q' exits
        setup_readline_history()
        while True:
            try:
                code_input = input("请输入代码/名称(如 600519、浦发银行、00700、恒生指数；多只用空格分隔，输入 q 退出): ").strip()
            except EOFError:
                # EOF or non-interactive stdin; exit gracefully
                return 0
            if code_input.lower() == "q":
                return 0
            if not code_input:
                continue
            if HAS_READLINE:
                try:
                    readline.add_history(code_input)  # type: ignore[attr-defined]
                except Exception:
                    pass
            codes = code_input.split()
            query_and_display(codes, show_detail=args.detail)

    if args.loop:
        try:
            while True:
                query_and_display(codes, show_detail=args.detail)
                time.sleep(max(0.5, float(args.interval)))
        except KeyboardInterrupt:
            return 0
    else:
        query_and_display(codes, show_detail=args.detail)
        return 0


if __name__ == "__main__":
    sys.exit(main())

