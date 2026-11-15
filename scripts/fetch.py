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

官方字段实测（2026-11-14）：
  - /events 在 limit>50 时强制 gzip，需声明 Accept-Encoding: gzip 并自行解压；
  - 不接受显式 Accept: application/json（406）；
  - offset 上限约 2100，更深需 /events/keyset，此处按「分页到顶」正常收尾；
  - 布尔字段是字符串 "True"/"False"，不可直接 bool()；
  - event 无 category 字段（只有 tags），market 无 groupItemTitle。
"""

import datetime as dt
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
HEADER_SETS = [
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    },
    {"User-Agent": "pm-monitor/1.0 (+github actions)"},
    {"User-Agent": "curl/8.5.0", "Accept": "application/json, text/plain, */*"},
]
PAGE_SIZE = 100
MAX_PAGES = 30
TIMEOUT = 40
RETRY = 3
OUT_DIR = "data"
TOP_OPTIONS = 3          # 报告只展示概率最高的 3 个活跃选项，勿全量塞入

# 官方对尚未确定的候选人使用代号占位（Candidate T / Team H / Company A / Other…），
# 概率恒为 0.5（未开盘默认值）。另有单字母/字母+编号代号（A / B / P4 …）同样是占位。
# 这类选项没有实际押注含义，且恒 0.5 会在「按概率取前三」时挤掉概率较低的真实候选，
# 因此必须在精简前先剔除，并把占位数量如实回传给报告端。
_PLACEHOLDER_OPT = re.compile(
    r"^(?:candidate|team|person|party|player|company|option|nominee|proposition|"
    r"country|state|entity|unknown|tbd|tba|other|none of the above)\b", re.I)
_PLACEHOLDER_CODE = re.compile(r"^[A-Za-z]\d{0,2}$")


def is_placeholder(name, prob) -> bool:
    n = (name or "").strip()
    if not (_PLACEHOLDER_OPT.match(n) or _PLACEHOLDER_CODE.match(n)):
        return False
    return isinstance(prob, (int, float)) and abs(prob - 0.5) < 1e-9

# 官方 tags 标签 -> 报告 CAT_ZH 的 8 类。取不到如实留空（报告显示「其他」）。
CAT_MAP = {
    "politics": "politics", "elections": "politics", "us politics": "politics",
    "geopolitics": "geopolitics", "world": "geopolitics", "war": "geopolitics",
    "sports": "sports", "nba": "sports", "nfl": "sports", "soccer": "sports",
    "football": "sports", "baseball": "sports", "tennis": "sports", "esports": "sports",
    "economy": "economy", "economics": "economy", "macro": "economy",
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "tech": "tech", "ai": "tech", "technology": "tech", "science": "tech",
    "culture": "culture", "entertainment": "culture", "pop culture": "culture",
}


def log(*a):
    print(*a, flush=True)


def get(path, params):
    url = GAMMA + path + "?" + urllib.parse.urlencode(params)
    last = "无尝试"
    for i in range(RETRY):
        for hi, headers in enumerate(HEADER_SETS):
            try:
                h = dict(headers)
                h["Accept-Encoding"] = "gzip"
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = r.read()
                    if raw[:2] == b"\x1f\x8b" or "gzip" in (
                            r.headers.get("Content-Encoding") or "").lower():
                        raw = gzip.decompress(raw)
                    body = raw.decode("utf-8", "replace")
                    if i or hi:
                        log("  成功：第 %d 轮 / 第 %d 套请求头" % (i + 1, hi + 1))
                    return json.loads(body)
            except Exception as e:
                detail = ""
                err_body = b""
                if hasattr(e, "read"):
                    try:
                        err_body = e.read()
                        detail = " | body=" + err_body.decode("utf-8", "replace")[:200]
                    except Exception:
                        pass
                if b"offset too large" in err_body or b"keyset" in err_body:
                    log("  分页到顶（offset=%s），按已抓取数据收尾"
                        % params.get("offset"))
                    return []
                last = "%s: %s%s" % (type(e).__name__, str(e)[:100], detail)
                log("  失败 轮%d/头%d offset=%s -> %s"
                    % (i + 1, hi + 1, params.get("offset"), last[:240]))
        time.sleep(2 * (i + 1))
    raise RuntimeError("Gamma 请求失败: %s -> %s" % (url, last))


def jlist(x):
    """Gamma 的 outcomes / outcomePrices 是 JSON 字符串形式的数组。"""
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x) if x else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def truthy(v):
    """官方布尔字段是字符串 'True'/'False'，bool('False') 为 True，必须显式解析。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1", "yes")


def num(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
            if f == f:
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


def sub_label(m, event_title):
    """子市场档位名：优先 groupItemThreshold/档位字段，其次 question 去掉事件名前缀。"""
    for k in ("groupItemTitle", "groupItemThreshold", "shortTitle"):
        v = strv(m, k)
        if v and v not in ("0", "1", "0.0", "1.0"):
            return v
    q = strv(m, "question", "title")
    if q and event_title and q.lower().startswith(event_title.lower()):
        rest = q[len(event_title):].strip(" ?:：-")
        if rest:
            return rest
    return q or event_title or ""


def market_outcomes(e):
    """返回 (精简后的真实候选列表, 全部活跃选项总数, 未开盘占位项数)。

    多档位 event：每个活跃子市场取 Yes 价作为该档概率，档位名取子标签；
    单市场二元 event：直接展开 outcomes/outcomePrices。
    先剔除官方未开盘占位项（代号 + 恒 0.5），再按概率保留 TOP_OPTIONS 个真实候选；
    占位项数量单独如实回传，供报告端标注「已略去 N 个」或「候选尚未确定」。
    n_active 语义不变：仍为全部活跃选项总数（含占位）。
    """
    title = strv(e, "title")
    ms = [m for m in (e.get("markets") or []) if isinstance(m, dict)]
    active = [m for m in ms if not truthy(m.get("closed"))]
    out = []
    if len(active) >= 2:
        for m in active:
            p = yes_price(m)
            if p is None:
                continue
            out.append({"name": sub_label(m, title), "probability": p,
                        "closed": False})
    else:
        m = active[0] if active else (ms[0] if ms else None)
        if m:
            outs = jlist(m.get("outcomes"))
            prices = jlist(m.get("outcomePrices"))
            for o, p in zip(outs, prices):
                try:
                    out.append({"name": str(o), "probability": float(p),
                                "closed": truthy(m.get("closed"))})
                except Exception:
                    continue
    out = [y for y in out if isinstance(y["probability"], (int, float))
           and not y["closed"]]
    n_active = len(out)
    eff = [y for y in out if not is_placeholder(y["name"], y["probability"])]
    n_ph = n_active - len(eff)
    eff.sort(key=lambda y: y["probability"], reverse=True)
    return eff[:TOP_OPTIONS], n_active, n_ph


def tag_labels(e):
    tags = e.get("tags")
    out = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                v = t.get("label") or t.get("slug") or t.get("name")
                if v:
                    out.append(str(v))
            elif isinstance(t, str):
                out.append(t)
    return out


def category_of(labels):
    for lb in labels:
        k = lb.strip().lower()
        if k in CAT_MAP:
            return CAT_MAP[k]
        for word, cat in CAT_MAP.items():
            if word in k:
                return cat
    return ""


def is_single_game(e):
    """单场赛事特征：官方 event 带具体比赛日 / 赛事 id / 队伍字段。如实标记，不擅自过滤。"""
    for k in ("eventDate", "gameId", "sport", "teams", "eventWeek"):
        v = e.get(k)
        if v not in (None, "", [], {}, "False", False):
            return True
    return False


def to_market(e):
    outs, n_active, n_ph = market_outcomes(e)
    leader, prob = "", None
    if outs:
        leader, prob = outs[0]["name"], outs[0]["probability"]
    labels = tag_labels(e)
    slug = strv(e, "slug") or strv(e, "id")
    return {
        "slug": slug,
        "title": strv(e, "title", "question"),
        "category": category_of(labels),
        "url": "https://polymarket.com/event/" + slug if slug else "",
        "leader": leader,
        "probability": prob,
        "volume": num(e, "volumeNum", "volume"),
        "volume_24h": num(e, "volume24hr", "volume24Hr", "volume_24hr"),
        "end_date": strv(e, "endDate", "endDateIso"),
        "outcomes": outs,
        "n_active": n_active,
        "n_placeholder": n_ph,
        "tags": labels[:6],
        "single_game": is_single_game(e),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat()

    events, seen = [], set()
    pages = 0
    for page in range(MAX_PAGES):
        d = get("/events", {
            "limit": PAGE_SIZE, "offset": page * PAGE_SIZE,
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
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
    n_game = sum(1 for m in markets if m["single_game"])
    stats = {
        "date": now.strftime("%Y-%m-%d"),
        "updated": ts,
        "events": len(events),
        "markets": n_sub,
        "single_game_events": n_game,
        "volume_24h": sum(vol24) if vol24 else None,
        # 以下字段 Gamma 无对应口径，如实留空，报告端显示「未获取」
        "open_interest": None,
        "volume_open_events": None,
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
        "| 单场赛事 =", n_game, "| 24h 合计 =", stats["volume_24h"])

    # ---- 校准样本：字段名、tags 原文、多档位子市场样本，不落任何凭据 ----
    first_e = events[0]
    e_keys = sorted(first_e.keys()) if isinstance(first_e, dict) else []
    sub = first_e.get("markets") or []
    m_keys = sorted(sub[0].keys()) if sub and isinstance(sub[0], dict) else []
    def trim(o, n=40):
        if isinstance(o, dict):
            return {k: (str(v)[:160] if not isinstance(v, (dict, list)) else
                        ("<list %d>" % len(v) if isinstance(v, list) else "<dict>"))
                    for k, v in list(o.items())[:n]}
        return str(o)[:200]
    # 找一个多档位（活跃子市场 >= 3）的非单场 event，看档位名字段真实形态
    multi = None
    for e in events:
        ms = [m for m in (e.get("markets") or [])
              if isinstance(m, dict) and not truthy(m.get("closed"))]
        if len(ms) >= 3:
            multi = {"title": strv(e, "title"), "slug": strv(e, "slug"),
                     "n_sub": len(ms),
                     "sub_markets": [
                         {k: str(m.get(k))[:90] for k in
                          ("question", "groupItemTitle", "groupItemThreshold",
                           "shortTitle", "outcomes", "outcomePrices", "closed")}
                         for m in ms[:5]]}
            break
    cat_dist = {}
    tag_samples = []
    for m in markets:
        cat_dist[m["category"] or "(空)"] = cat_dist.get(m["category"] or "(空)", 0) + 1
    for e in events[:8]:
        tag_samples.append({"title": strv(e, "title"), "tags": tag_labels(e)[:6]})
    probe = {
        "ts": ts,
        "pages_fetched": pages,
        "events_total": len(events),
        "sub_markets_total": n_sub,
        "single_game_events": n_game,
        "event_keys": e_keys,
        "market_keys": m_keys,
        "sample_event": trim(first_e),
        "sample_market": trim(sub[0]) if sub and isinstance(sub[0], dict) else {},
        "multi_option_event": multi,
        "category_distribution": cat_dist,
        "tag_samples": tag_samples,
        "top_titles": [strv(e, "title") for e in events[:25]],
    }
    with open(os.path.join(OUT_DIR, "probe.json"), "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False, indent=1)
    log("已写 data/probe.json | event 字段数 =", len(e_keys),
        "| market 字段数 =", len(m_keys), "| category 分布 =", cat_dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
