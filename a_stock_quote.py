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

try:
    import requests  # type: ignore
    HAS_REQUESTS = True
except Exception:  # requests may be unavailable
    requests = None  # type: ignore
    HAS_REQUESTS = False

import urllib.request
import urllib.error
import urllib.parse


SINA_QUOTE_ENDPOINT = "https://hq.sinajs.cn/list="
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

    def _save_history() -> None:
        try:
            readline.write_history_file(path)  # type: ignore[attr-defined]
        except Exception:
            pass

    atexit.register(_save_history)


def http_get_text(url: str, headers: Dict[str, str], timeout: float) -> str:
    """HTTP GET that returns decoded text (GBK preferred), using requests if available, otherwise urllib.
    """
    if HAS_REQUESTS:
        resp = requests.get(url, headers=headers, timeout=timeout)  # type: ignore[attr-defined]
        resp.encoding = "gbk"
        return resp.text

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp_obj:
        data = resp_obj.read()
        try:
            return data.decode("gbk", errors="ignore")
        except Exception:
            return data.decode("utf-8", errors="ignore")


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
    """
    if not prefixed_codes:
        return {}

    url = SINA_QUOTE_ENDPOINT + ",".join(prefixed_codes)
    text = http_get_text(url, headers=SINA_HEADERS, timeout=timeout)

    result: Dict[str, Dict[str, object]] = {}
    for line in text.splitlines():
        parsed = parse_sina_line(line)
        if not parsed:
            continue
        full_code = f"{parsed['exchange']}{parsed['code']}"
        result[full_code] = parsed
    return result


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

    for full_code in normalized:
        quote = quotes.get(full_code)
        if not quote:
            print(f"{full_code}: 无数据")
            continue
        print_quote_line(quote)
        if show_detail:
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

