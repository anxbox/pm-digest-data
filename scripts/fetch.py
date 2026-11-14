#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Gamma 全量快照抓取器（由 GitHub Actions 运行）

产出：
  data/latest.json —— 单一、可被覆盖、公开只读的快照，字段对齐 WorkBuddy
                      「Polymarket 热点简报」报告脚本的数据层。
  data/probe.json  —— 官方接口真实字段名与样本，供字段映射校准核对。

原则：
  - 只做字段映射与格式转换，不推断、不编造源站未给出的数值；
  - 取不到就如实留空（null / ""），由报告端显示「未获取」；
  - 不含任何凭据；commit 由 workflow 使用 GitHub 自动注入的 GITHUB_TOKEN 完成。
"""

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
UA = {
    "User-Agent": "pm-digest-snapshot/1.0 (+github actions)",
    "Accept": "application/json",
}
PAGE_SIZE = 100
MAX_PAGES = 30          # 上限 3000 个活跃 event
TIMEOUT = 40
RETRY = 3
OUT_DIR = "data"


def log(*a):
    print(*a, flush=True)


def get(path, params):
    url = GAMMA + path + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            log("  retry %d/%d offset=%s: %s"
                % (i + 1, RETRY, params.get("offset"), type(e).__name__))
            time.sleep(2 * (i + 1))
    raise RuntimeError("Gamma 请求失败: %s -> %s: %s"
                       % (url, type(last).__name__, last))


def jlist(x):
    """Gamma 的 outcomes / outcomePrices 常为 JSON 字符串。"""
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x) if x else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def num(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
            if f == f:            # 排除 NaN
                return f
        except Exception:
            continue
    return None


def strv(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def yes_price(m):
    """二元子市场的 Yes 概率（0-1）；取不到返回 None。"""
    outs = jlist(m.get("outcomes"))
    prices = jlist(m.get("outcomePrices"))
    if not outs or not prices or len(outs) != len(prices):
        return None
    for o, p in zip(outs, prices):
        if str(o).strip().lower() == "yes":
            try:
                return float(p)
            except Exception:
                return None
    try:
        return float(prices[0])
    except Exception:
        return None


def market_outcomes(e):
    """把一个 event 下的子市场压成 outcomes 列表。

    多档位 event（同一问题的多个日期/阈值档）：每个档位一个选项，
    名称取 groupItemTitle，概率取该档 Yes 价 —— 与镜像 odds/{slug}.json 同构。
    单一二元 event：直接展开 outcomes/outcomePrices 为 Yes/No。
    """
    ms = [m for m in (e.get("markets") or []) if isinstance(m, dict)]
    active = [m for m in ms if not m.get("closed")]

    if len(active) >= 2:
        out = []
        for m in active:
            p = yes_price(m)
            if p is None:
                continue
            out.append({
                "name": strv(m, "groupItemTitle", "question", "title", "shortTitle"),
                "probability": p,
                "change_24h": num(m, "oneDayPriceChange", "oneDayPriceChangePct"),
                "volume": num(m, "volume24hr", "volume24Hr", "volume_24hr"),
                "closed": bool(m.get("closed")),
            })
        if out:
            return out

    m = active[0] if active else (ms[0] if ms else None)
    if m:
        outs = jlist(m.get("outcomes"))
        prices = jlist(m.get("outcomePrices"))
        out = []
        for o, p in zip(outs, prices):
            try:
                out.append({
                    "name": str(o),
                    "probability": float(p),
                    "change_24h": None,
                    "volume": None,
                    "closed": bool(m.get("closed")),
                })
            except Exception:
                continue
        if out:
            return out
    return []


def category_of(e):
    """源站分类：优先 category 字段，其次 tags 标签；都没有则留空。"""
    c = strv(e, "category").strip().lower()
    if c:
        return c
    tags = e.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                lb = str(t.get("label") or t.get("slug") or "").strip().lower()
                if lb:
                    return lb
    return ""


def to_market(e):
    """event -> 报告所需的单条 market 记录。"""
    outs = market_outcomes(e)
    leader, prob = "", None
    if outs:
        top = max(outs, key=lambda y: y["probability"])
        leader, prob = top["name"], top["probability"]
    return {
        "slug": strv(e, "slug") or strv(e, "id"),
        "title": strv(e, "title", "question"),
        "category": category_of(e),
        "url": strv(e, "url"),
        "leader": leader,
        "probability": prob,
        "volume": num(e, "volumeNum", "volume"),
        "volume_24h": num(e, "volume24hr", "volume24Hr", "volume_24hr"),
        "end_date": strv(e, "endDate", "endDateIso"),
        "outcomes": outs,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat()

    events, seen = [], set()
    pages = 0
    for page in range(MAX_PAGES):
        d = get("/events", {
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        })
        if isinstance(d, dict):
            d = d.get("data") or d.get("events") or []
        if not d:
            log("page %d 无返回，停止" % page)
            break
        pages += 1
        log("page %d: %d events" % (page, len(d)))
        for e in d:
            if not isinstance(e, dict):
                continue
            eid = e.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            events.append(e)
        if len(d) < PAGE_SIZE:
            break
        time.sleep(0.4)

    if not events:
        log("FATAL: 一个 event 都没拿到，不写快照")
        return 1
    log("共抓取 %d 个活跃 event（%d 页）" % (len(events), pages))

    markets = [to_market(e) for e in events]
    markets = [m for m in markets if m["title"] and m["slug"]]
    log("转换后有效 market 条数 =", len(markets))

    vol24 = [m["volume_24h"] for m in markets if m["volume_24h"] is not None]
    n_sub = sum(len(e.get("markets") or []) for e in events)
    stats = {
        "date": now.strftime("%Y-%m-%d"),
        "updated": ts,
        "events": len(events),
        "markets": n_sub,
        "volume_24h": sum(vol24) if vol24 else None,
        # 以下字段 Gamma 无对应口径，如实留空，报告端显示「未获取」
        "open_interest": None,
        "volume_open_events": None,
        "single_game_events": None,
        "top10_share_24h": None,
        "recurring_events": None,
    }

    snapshot = {
        "ts": ts,
        "updated": ts,
        "source": "polymarket-gamma",
        "count": len(markets),
        "markets": markets,
        "stats": stats,
    }
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    log("已写 data/latest.json | count =", len(markets),
        "| 24h 合计 =", stats["volume_24h"])

    # 校准样本：字段名 + 截断后的样本值，不落任何凭据
    first_e = events[0]
    e_keys = sorted(first_e.keys()) if isinstance(first_e, dict) else []
    sub = (first_e.get("markets") or [])
    m_keys = sorted(sub[0].keys()) if sub and isinstance(sub[0], dict) else []
    def trim(o):
        if isinstance(o, dict):
            return {k: (str(v)[:160] if not isinstance(v, (dict, list)) else
                        ("<list %d>" % len(v) if isinstance(v, list) else "<dict>"))
                    for k, v in list(o.items())[:40]}
        return str(o)[:200]
    probe = {
        "ts": ts,
        "pages_fetched": pages,
        "events_total": len(events),
        "sub_markets_total": n_sub,
        "event_keys": e_keys,
        "market_keys": m_keys,
        "sample_event": trim(first_e),
        "sample_market": trim(sub[0]) if sub and isinstance(sub[0], dict) else {},
        "top_titles": [strv(e, "title") for e in events[:25]],
    }
    with open(os.path.join(OUT_DIR, "probe.json"), "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=1)
    log("已写 data/probe.json | event 字段数 =", len(e_keys),
        "| market 字段数 =", len(m_keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
