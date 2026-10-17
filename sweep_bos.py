# Strategy 6: BOS + LIQUIDITY SWEEP - backtest on the top-5 coins (5m entry, 15m confirm, 1h trend).
# Last 200 trades per coin, risk:reward 1:2. SELF-CONTAINED. Data: OKX public candles. NO real money.
#   python sweep_bos.py 365 200      (days of history, trades per coin)
import requests, time, sys, csv
from datetime import datetime, timezone

OKX = "https://www.okx.com/api/v5"
COINS = ["BTC", "ETH", "BNB", "XRP", "SOL"]
LEVS = [1, 3, 5]
RR = 2.0

CFG = dict(
    swing=3,             # swing high/low = extreme of 3 candles each side
    ema_fast=20, ema_slow=50, atr_n=14,
    bos_window=24,       # candles allowed between the sweep and the structure break (2h on 5m)
    atr_buf=0.1,         # stop = sweep extreme -/+ this many ATRs
    cap=288,             # max candles in a trade (24h), then close at market
    warmup=60,           # first candles ignored
    warm_days=5,         # first days ignored (higher-timeframe EMAs need history)
    fee_pct=0.05,        # taker fee per side
    slip_pct=0.02,       # slippage per side
    margin=20.0,         # fake dollars of own money per trade
    start_balance=100.0,
    maint_pct=0.5,
)


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-30:])
            if r.status_code in (429, 403):
                time.sleep(5)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    return None


def parse(c):
    return dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                l=float(c[3]), c=float(c[4]), v=float(c[5]))


def fetch_history(inst, days):
    need = days * 288
    out, after = [], None
    while len(out) < need:
        p = {"instId": inst, "bar": "5m", "limit": 100}
        if after:
            p["after"] = after
        d = (get_json(OKX + "/market/history-candles", p) or {}).get("data") or []
        time.sleep(0.15)
        if not d:
            break
        out.extend(parse(c) for c in d if len(c) >= 9 and c[8] == "1")
        after = d[-1][0]
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            rows.append(c)
    return rows[-need:]


# ---------- indicators ----------
def sma(vals, n):
    out, run = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr(cs, n):
    out = [None] * len(cs)
    tr = [None] + [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                       abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(cs) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def simulate(cs, e, side, sl, rr):
    entry = cs[e]["o"]
    risk = abs(entry - sl)
    if risk <= 0 or (side == 1 and sl >= entry) or (side == -1 and sl <= entry):
        return None
    tp = entry + side * rr * risk
    last = min(e + CFG["cap"], len(cs) - 1)
    ex, reason, xi = None, None, None
    for k in range(e, last + 1):
        c = cs[k]
        if k > e:
            if (side == 1 and c["o"] <= sl) or (side == -1 and c["o"] >= sl):
                ex, reason, xi = c["o"], "SL", k
                break
            if (side == 1 and c["o"] >= tp) or (side == -1 and c["o"] <= tp):
                ex, reason, xi = c["o"], "TP", k
                break
        hit_sl = c["l"] <= sl if side == 1 else c["h"] >= sl
        hit_tp = c["h"] >= tp if side == 1 else c["l"] <= tp
        if hit_sl:
            ex, reason, xi = sl, "SL", k
            break
        if hit_tp:
            ex, reason, xi = tp, "TP", k
            break
        if k == e + CFG["cap"]:
            ex, reason, xi = c["c"], "TIME", k
            break
    if ex is None:
        return {"open": True}
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    gross = side * (ex / entry - 1) * 100
    risk_pct = risk / entry * 100
    return dict(entry=entry, sl=sl, tp=tp, exit=ex, reason=reason, exit_idx=xi,
                risk_pct=risk_pct, gross_pct=gross, net_pct=gross - cost,
                net_R=(gross - cost) / risk_pct)


def lev_pnl(margin, lev, net_pct, risk_pct):
    if lev > 1 and risk_pct >= 100.0 / lev - CFG["maint_pct"]:
        return -margin
    return max(margin * lev * net_pct / 100, -margin)


def equity(ts, lev):
    cash, peak, maxdd, taken, wins = CFG["start_balance"], CFG["start_balance"], 0.0, 0, 0
    m = CFG["margin"]
    opened = []

    def settle(upto):
        nonlocal cash, peak, maxdd, opened
        opened.sort(key=lambda x: x[0])
        keep, closed = [], 0
        total = len(opened)
        for ex, pnl in opened:
            if ex <= upto:
                cash += m + pnl
                closed += 1
                eq = cash + m * (total - closed)
                peak = max(peak, eq)
                maxdd = max(maxdd, (peak - eq) / peak * 100)
            else:
                keep.append((ex, pnl))
        opened = keep

    for t in sorted(ts, key=lambda x: x["entry_ts"]):
        settle(t["entry_ts"])
        if cash >= m:
            cash -= m
            pnl = lev_pnl(m, lev, t["net_pct"], t["risk_pct"])
            opened.append((t["exit_ts"], pnl))
            taken += 1
            wins += 1 if pnl > 0 else 0
    settle(10 ** 12)
    return dict(final=cash, ret=(cash / CFG["start_balance"] - 1) * 100,
                dd=maxdd, taken=taken, wins=wins)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def ema_list(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    e = sum(vals[:n]) / n
    out[n - 1] = e
    k = 2.0 / (n + 1)
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def htf_arrays(cs, minutes):
    """For every 5m candle: state of the last CLOSED higher-timeframe bar (no look-ahead)."""
    per, nb = minutes * 60, minutes // 5
    buckets = {}
    for i, c in enumerate(cs):
        buckets.setdefault(c["ts"] // per * per, []).append(i)
    bars = []
    for st in sorted(buckets):
        idxs = buckets[st]
        if len(idxs) == nb:
            bars.append((st + per, cs[idxs[-1]]["c"]))
    closes = [b[1] for b in bars]
    e_f, e_s = ema_list(closes, CFG["ema_fast"]), ema_list(closes, CFG["ema_slow"])
    states = [dict(close=closes[k], f=e_f[k], s=e_s[k], f_prev=e_f[k - 3] if k >= 3 else None)
              for k in range(len(bars))]
    out, k = [None] * len(cs), -1
    for i, c in enumerate(cs):
        known = c["ts"] + 300
        while k + 1 < len(bars) and bars[k + 1][0] <= known:
            k += 1
        out[i] = states[k] if k >= 0 else None
    return out


def trend_1h(st):
    if not st or st["s"] is None:
        return 0
    if st["close"] > st["s"] and st["f"] > st["s"]:
        return 1
    if st["close"] < st["s"] and st["f"] < st["s"]:
        return -1
    return 0


def confirm_15m(st):
    if not st or st["f"] is None or st["f_prev"] is None:
        return 0
    if st["close"] > st["f"] and st["f"] > st["f_prev"]:
        return 1
    if st["close"] < st["f"] and st["f"] < st["f_prev"]:
        return -1
    return 0


def swing_levels(cs):
    """Last CONFIRMED swing high / low known at the end of each candle."""
    k, n = CFG["swing"], len(cs)
    sh, sl = [None] * n, [None] * n
    cur_h = cur_l = None
    for i in range(n):
        j = i - k
        if j >= k:
            hi, lo = cs[j]["h"], cs[j]["l"]
            if all(hi > cs[x]["h"] for x in range(j - k, j + k + 1) if x != j):
                cur_h = (hi, j)
            if all(lo < cs[x]["l"] for x in range(j - k, j + k + 1) if x != j):
                cur_l = (lo, j)
        sh[i], sl[i] = cur_h, cur_l
    return sh, sl


def find_events(cs):
    """Liquidity sweep of the last swing, then break of structure. Returns raw events."""
    sh, sl = swing_levels(cs)
    a = atr(cs, CFG["atr_n"])
    events, setups = [], {1: None, -1: None}
    for i in range(CFG["warmup"], len(cs)):
        c = cs[i]
        if a[i] is None:
            continue
        for side in (1, -1):
            st = setups[side]
            if st is None:
                continue
            if i - st["s"] > CFG["bos_window"]:
                setups[side] = None
            elif side == 1:
                if c["l"] < st["ext"]:
                    setups[side] = None
                elif c["c"] > st["level"]:
                    events.append(dict(i=i, side=1, sl=st["ext"] - CFG["atr_buf"] * a[i], sweep=st["s"]))
                    setups[side] = None
            else:
                if c["h"] > st["ext"]:
                    setups[side] = None
                elif c["c"] < st["level"]:
                    events.append(dict(i=i, side=-1, sl=st["ext"] + CFG["atr_buf"] * a[i], sweep=st["s"]))
                    setups[side] = None
        L, H = sl[i - 1], sh[i - 1]
        if L and H and c["l"] < L[0] and c["c"] > L[0] and H[0] > c["c"]:
            setups[1] = dict(s=i, ext=c["l"], level=H[0])
        if L and H and c["h"] > H[0] and c["c"] < H[0] and L[0] < c["c"]:
            setups[-1] = dict(s=i, ext=c["h"], level=L[0])
    return events


def find_signals(cs):
    h1, m15 = htf_arrays(cs, 60), htf_arrays(cs, 15)
    out = []
    for ev in find_events(cs):
        i = ev["i"]
        if trend_1h(h1[i]) == ev["side"] and confirm_15m(m15[i]) == ev["side"]:
            out.append(dict(i=i, side=ev["side"], sl=ev["sl"], sweep=ev["sweep"]))
    return out


def coin_trades(cs, coin):
    t_start = cs[0]["ts"] + CFG["warm_days"] * 86400
    trades, busy = [], -1
    for sg in find_signals(cs):
        e = sg["i"] + 1
        if e >= len(cs) or e <= busy or cs[sg["i"]]["ts"] < t_start:
            continue
        t = simulate(cs, e, sg["side"], sg["sl"], RR)
        if t is None or t.get("open"):
            continue
        busy = t["exit_idx"]
        t.update(coin=coin, side=sg["side"], entry_ts=cs[e]["ts"], exit_ts=cs[t["exit_idx"]]["ts"])
        trades.append(t)
    return trades


def summ(ts):
    n = len(ts)
    if n == 0:
        return None
    wins = [t for t in ts if t["net_pct"] > 0]
    gp = sum(t["net_pct"] for t in wins)
    gl = -sum(t["net_pct"] for t in ts if t["net_pct"] <= 0)
    stop = sum(t["risk_pct"] for t in ts) / n
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    return dict(n=n, win=100.0 * len(wins) / n, avg=sum(t["net_pct"] for t in ts) / n,
                pf=gp / gl if gl else 99.0, stop=stop, be=100.0 * (1 + cost / stop) / (RR + 1))


def fmt(s):
    if s is None:
        return "n=0"
    return "n=%-4d win=%2.0f%% avg=%+.3f%% PF=%.2f stop=%.2f%% be=%2.0f%%" % (
        s["n"], s["win"], s["avg"], s["pf"], s["stop"], s["be"])


def report(ts, label, rows):
    ts = sorted(ts, key=lambda t: t["entry_ts"])
    print("== %s ==" % label)
    if not ts:
        print("   no trades")
        return
    print("   %s -> %s" % (iso(ts[0]["entry_ts"]), iso(ts[-1]["exit_ts"])))
    print("   ALL  : " + fmt(summ(ts)))
    print("   LONG : " + fmt(summ([t for t in ts if t["side"] > 0])))
    print("   SHORT: " + fmt(summ([t for t in ts if t["side"] < 0])))
    half = len(ts) // 2
    print("   FIRST half : " + fmt(summ(ts[:half])))
    print("   SECOND half: " + fmt(summ(ts[half:])))
    eq = []
    for lev in LEVS:
        q = equity(ts, lev)
        eq.append("x%d %+.1f%% (dd %.1f%%)" % (lev, q["ret"], q["dd"]))
        rows.append([label, lev, len(ts), round(q["ret"], 1), round(q["dd"], 1)])
    print("   equity: " + "  ".join(eq))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    last_n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    print("BOS + liquidity sweep | RR 1:%.0f | cost %.2f%%/trade | last %d trades per coin | be = break-even win rate" % (
        RR, cost, last_n))
    used, rows = [], []
    for coin in COINS:
        cs = fetch_history(coin + "-USDT-SWAP", days)
        print("%s: %d candles (%s -> %s)" % (coin, len(cs), iso(cs[0]["ts"]) if cs else "-", iso(cs[-1]["ts"]) if cs else "-"))
        if len(cs) < 5000:
            print("   not enough data for", coin)
            continue
        tr = coin_trades(cs, coin)
        use = tr[-last_n:]
        print("   completed trades in the period: %d (analysing %d)" % (len(tr), len(use)))
        if len(use) < last_n:
            print("   WARNING: fewer than %d trades; use more days" % last_n)
        report(use, coin, rows)
        used.extend(use)
    report(used, "ALL COINS", rows)
    with open("sweep_bos_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["coin", "side", "entry_time", "exit_time", "entry", "sl", "tp", "exit",
                    "reason", "stop_pct", "net_pct", "net_R"])
        for t in sorted(used, key=lambda x: (x["coin"], x["entry_ts"])):
            w.writerow([t["coin"], "LONG" if t["side"] > 0 else "SHORT", iso(t["entry_ts"]), iso(t["exit_ts"]),
                        t["entry"], round(t["sl"], 6), round(t["tp"], 6), t["exit"], t["reason"],
                        round(t["risk_pct"], 3), round(t["net_pct"], 3), round(t["net_R"], 3)])
    with open("sweep_bos_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scope", "lev", "trades", "return_pct", "max_dd_pct"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
