# -*- coding: utf-8 -*-
"""
三市场「市场宽度」免费数据源 —— 无需任何 API Key / 无需注册 / 无需付费。

在真实 GitHub Actions（美国华盛顿 Azure）上实测可达：
  - 东方财富 push2.eastmoney.com 会 302 -> //push2delay.eastmoney.com，
    跟随重定向（或直接用 push2delay）即返回完整 JSON；
  - 新浪 hq.sinajs.cn 需带 Referer: https://finance.sina.com.cn/；
  - 腾讯 qt.gtimg.cn、Yahoo、TickFlow 免费服务器均 200。
项目自带 AkShare / efinance 拿不到数据，不是「海外连不上」，而是没跟随 302 /
Referer 不对 / 一次性拉全市场被限流。

数据源与用途（全部免费）：
  1. 东财 /api/qt/ulist.np/get
     A 股指数（1.000001, 0.399001）直接带 f104/f105/f106(涨/跌/平家数) 与 f6(成交额)；
     港股指数（100.HSI, 100.HSCEI）带 f6(成交额)。
  2. 东财 /api/qt/clist/get
     行业板块榜（A 股 m:90+t:2、港股 m:124+t:1..4）、涨停/跌停池。
  3. TickFlow 免费服务器 https://free-api.tickflow.org（无需注册）
     /v1/universes/{HK_Equity|US_Equity} 标的池 + /v1/klines/batch 批量日 K，
     用于算港股/美股的涨跌家数与成交额（收盘后日 K，正好匹配盘后日报）。
  4. 腾讯 https://qt.gtimg.cn
     美股 11 只 SPDR 行业 ETF 涨跌幅 -> 美股板块榜。

返回结构对齐项目 MarketOverview 需要的字段：
    up_count / down_count / flat_count / limit_up_count / limit_down_count /
    total_amount（单位：亿本币；A 股=亿元，港股=亿港元，美股=亿美元）

独立运行：python free_market_breadth.py  -> 打印三市场实测宽度。
"""
import datetime
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EM_HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}
EM_HOSTS = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com"]
TF_FREE = "https://free-api.tickflow.org"
TX_QUOTE = "https://qt.gtimg.cn/q="

CN_INDEX_SECIDS = "1.000001,0.399001"
HK_INDEX_SECIDS = "100.HSI,100.HSCEI"
CN_UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
HK_UNIVERSE_FS = "m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"
CN_SECTOR_FS = "m:90+t:2+f:!50"
HK_SECTOR_FS = "m:124+t:1,m:124+t:2,m:124+t:3,m:124+t:4"
US_SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP",
                  "XLY", "XLU", "XLB", "XLRE", "XLC"]

EMPTY = {
    "up_count": 0, "down_count": 0, "flat_count": 0,
    "limit_up_count": 0, "limit_down_count": 0, "total_amount": 0.0,
}


def _get(url: str, params: Optional[Dict] = None, *, retries: int = 4,
         timeout: int = 12, gbk: bool = False) -> Any:
    """带重试的 GET。失败返回 None，绝不抛异常阻断主流程（fail-open）。"""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=EM_HEADERS, timeout=timeout)
            r.raise_for_status()
            if gbk:
                return r.content.decode("gbk", "replace")
            return r.json()
        except Exception as exc:
            logger.debug("free_breadth GET failed (%s/%s): %s", i + 1, retries, exc)
            time.sleep(0.4 * (i + 1))
    return None


def _em(path: str, params: Dict) -> Optional[Dict]:
    """东财多域名回退（push2 会 302 到 push2delay）。"""
    for host in EM_HOSTS:
        j = _get(host + path, params)
        if j:
            return j
    return None


def _diff(j: Optional[Dict]) -> List[Dict]:
    if not j:
        return []
    d = j.get("data") or {}
    x = d.get("diff") or []
    if isinstance(x, dict):
        x = list(x.values())
    return x


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- A 股
def _cn_limit_counts(date_str: str) -> Tuple[int, int]:
    """东财涨停/跌停池，一次请求各拿总数；失败返回 (0, 0)。"""
    base = "https://push2ex.eastmoney.com/getTopic"
    out = []
    for pool, key in (("ZTPool", "zt"), ("DTPool", "dt")):
        j = _get(base + pool, {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt" if key == "zt" else "wz.dtzt",
            "Pageindex": 0, "pagesize": 1, "sort": "fbt:asc", "date": date_str,
        }, retries=2)
        data = (j or {}).get("data") or {}
        total = data.get("tc") or data.get("te") or data.get("total")
        if total is None and isinstance(data.get("pool"), list):
            total = len(data["pool"])
        out.append(int(_num(total)))
    return out[0], out[1]


def _cn_breadth() -> Optional[Dict]:
    j = _em("/api/qt/ulist.np/get", {
        "fltt": 2, "invt": 2, "secids": CN_INDEX_SECIDS,
        "fields": "f2,f3,f6,f12,f14,f104,f105,f106",
    })
    rows = _diff(j)
    rows = [x for x in rows if isinstance(x, dict)][:2]
    if not rows:
        return None
    amount = sum(_num(x.get("f6")) for x in rows) / 1e8  # 元 -> 亿元
    up = sum(int(_num(x.get("f104"))) for x in rows)
    down = sum(int(_num(x.get("f105"))) for x in rows)
    flat = sum(int(_num(x.get("f106"))) for x in rows)
    if up + down + flat == 0:
        return None
    today = datetime.datetime.now().strftime("%Y%m%d")
    lu, ld = _cn_limit_counts(today)
    return {
        "up_count": up, "down_count": down, "flat_count": flat,
        "limit_up_count": lu, "limit_down_count": ld,
        "total_amount": round(amount, 2),
    }


# ------------------------------------------------- 港股 / 美股（TickFlow 免费）
def _tf_breadth(region: str) -> Optional[Dict]:
    uid = {"hk": "HK_Equity", "us": "US_Equity"}.get(region)
    if not uid:
        return None
    u = _get(TF_FREE + "/v1/universes/" + uid, retries=3, timeout=20)
    syms = ((u or {}).get("data") or {}).get("symbols") or []
    if not syms:
        return None
    up = down = flat = 0
    amount = 0.0
    got = 0
    # 免费服务器 /v1/klines/batch 的 GET 查询串超过约 1000 字符会失败（实测 100 只
    # OK / 200 只 FAIL），因此固定 100 只一批；美股 12407 只约 124 批、约 90 秒。
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        j = _get(TF_FREE + "/v1/klines/batch", {
            "symbols": ",".join(chunk), "period": "1d", "count": 2,
        }, retries=3, timeout=25)
        data = (j or {}).get("data") or {}
        if not isinstance(data, dict):
            continue
        for v in data.values():
            if not isinstance(v, dict):
                continue
            close = v.get("close") or []
            amt = v.get("amount") or []
            # 停牌 / 当日无成交的标的不计入涨跌家数，否则「平盘」会被大量虚增
            if len(close) < 2 or not amt or _num(amt[-1]) <= 0:
                continue
            if close[-1] > close[-2]:
                up += 1
            elif close[-1] < close[-2]:
                down += 1
            else:
                flat += 1
            amount += _num(amt[-1])
            got += 1
        time.sleep(0.12)
    if got == 0:
        return None
    return {
        "up_count": up, "down_count": down, "flat_count": flat,
        "limit_up_count": 0, "limit_down_count": 0,   # 港美股无涨跌停制度
        "total_amount": round(amount / 1e8, 2),       # 亿本币
    }


def _em_paged_breadth(region: str, max_pages: int = 160) -> Optional[Dict]:
    """东财全市场分页兜底（当 TickFlow 免费服务器不可用时）。

    注意：必须按代码排序（fid=f12）遍历「全部」页。早期版本按涨跌幅降序
    只取前 40 页，会把上涨股全部排到前面，得出「涨 4000 / 跌 0」的错误结论。
    """
    fs = {"hk": HK_UNIVERSE_FS, "us": "m:105,m:106,m:107"}.get(region)
    if not fs:
        return None
    base = {
        "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f12", "fs": fs, "fields": "f3,f6,f12",
    }
    first = _em("/api/qt/clist/get", {**base, "pn": 1})
    total = int(_num((first or {}).get("data", {}).get("total")))
    if not total:
        return None
    pages = min((total + 99) // 100, max_pages)
    up = down = flat = 0
    amount = 0.0
    ok_pages = 0
    for pn in range(1, pages + 1):
        j = first if pn == 1 else _em("/api/qt/clist/get", {**base, "pn": pn})
        rows = _diff(j)
        if not rows:
            continue
        ok_pages += 1
        for x in rows:
            if not isinstance(x, dict):
                continue
            c = x.get("f3")
            if isinstance(c, (int, float)):
                if c > 0:
                    up += 1
                elif c < 0:
                    down += 1
                else:
                    flat += 1
            amount += _num(x.get("f6"))
        time.sleep(0.2)
    if ok_pages == 0:
        return None
    return {
        "up_count": up, "down_count": down, "flat_count": flat,
        "limit_up_count": 0, "limit_down_count": 0,
        "total_amount": round(amount / 1e8, 2),
    }


def get_breadth(region: str = "cn") -> Dict[str, Any]:
    """返回对齐项目 MarketOverview 的市场宽度；任何失败都返回全 0，不阻断主流程。"""
    try:
        if region == "cn":
            data = _cn_breadth()
        else:
            data = _tf_breadth(region) or _em_paged_breadth(region)
        return data or dict(EMPTY)
    except Exception as exc:
        logger.warning("[free_breadth] region=%s failed: %s", region, exc)
        return dict(EMPTY)


# ------------------------------------------------------------ 板块涨跌榜
def _em_sectors(fs: str, n: int, po: int) -> List[Dict]:
    j = _em("/api/qt/clist/get", {
        "pn": 1, "pz": max(n * 2, 20), "po": po, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f3", "fs": fs, "fields": "f3,f12,f14",
    })
    rows = _diff(j)
    out = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        c = x.get("f3")
        if not isinstance(c, (int, float)):
            continue
        out.append({"name": str(x.get("f14") or x.get("f12") or "-"),
                    "change_pct": round(float(c), 2)})
    out.sort(key=lambda z: z["change_pct"], reverse=(po == 1))
    return out[:n]


def _us_sectors(n: int) -> Tuple[List[Dict], List[Dict]]:
    """美股板块榜：11 只 SPDR 行业 ETF 涨跌幅（腾讯，免费、海外可达）。"""
    txt = _get(TX_QUOTE + ",".join("us" + s for s in US_SECTOR_ETFS), gbk=True)
    items: List[Dict] = []
    for line in (txt or "").strip().split("\n"):
        p = line.split("~")
        if len(p) > 32 and len(p) > 1 and p[1]:
            try:
                items.append({"name": p[1], "change_pct": round(float(p[32]), 2)})
            except (TypeError, ValueError):
                continue
    items.sort(key=lambda z: z["change_pct"], reverse=True)
    return items[:n], list(reversed(items[-n:]))


def get_sector_rankings(region: str = "cn", n: int = 5) -> Tuple[List[Dict], List[Dict]]:
    """返回 (领涨板块, 领跌板块)；港美股无涨跌停制度，仅 A 股有行业板块。"""
    try:
        if region == "us":
            return _us_sectors(n)
        if region == "hk":
            # 东财港股「板块」里混有大量衍生/策略指数（2 倍短仓、备兑、波幅等），
            # 过滤掉，只保留可解读的行业/主题；宁可留空也不展示误导性名称。
            bad = ("指数", "期货", "备兑", "短仓", "波幅", "反向", "杠杆",
                   "NTR", "2倍", "杠杆", "对冲")
            def clean(items):
                return [x for x in items if not any(k in x["name"] for k in bad)][:n]
            return (clean(_em_sectors(HK_SECTOR_FS, n * 5, 1)),
                    clean(_em_sectors(HK_SECTOR_FS, n * 5, 0)))
        return _em_sectors(CN_SECTOR_FS, n, 1), _em_sectors(CN_SECTOR_FS, n, 0)
    except Exception as exc:
        logger.warning("[free_breadth] sectors region=%s failed: %s", region, exc)
        return [], []


if __name__ == "__main__":
    for reg, label in (("cn", "A 股"), ("hk", "港股"), ("us", "美股")):
        b = get_breadth(reg)
        print(f"[{label}] 涨/跌/平 = {b['up_count']}/{b['down_count']}/{b['flat_count']}  "
              f"涨停/跌停 = {b['limit_up_count']}/{b['limit_down_count']}  "
              f"成交额 = {b['total_amount']:.2f} 亿")
        top, bottom = get_sector_rankings(reg)
        print(f"        领涨: {[(x['name'], x['change_pct']) for x in top[:3]]}")
        print(f"        领跌: {[(x['name'], x['change_pct']) for x in bottom[:3]]}")
