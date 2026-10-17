#!/usr/bin/env python3
"""imdUSD liquidity: read the peg pool, value LP positions, plan a deposit, and find every venue.

    python3 liquidity.py status                       # peg pool: price, fee now, depth, cost to peg, where liquidity sits, redemption
    python3 liquidity.py positions --wallet 0x…       # a wallet's peg-pool positions: range, amounts, fees owed, points rate
    python3 liquidity.py plan --usdc 1000 --imdusd 1000 [--range peg|wide|0.995:1.005]
    python3 liquidity.py avenues                      # every imdUSD pool on every DEX indexer, plus the protocol's own routes
    python3 liquidity.py tx mint --owner 0x… --usdc 500 --range 0.90:0.92   # unsigned txs, simulated (also increase/remove/collect/close/sweep)
    add --json to any command for machine-readable output; --rpc URL to use your own node

Read-only: `tx` builds and simulates transactions but never signs or sends. Needs Python 3.9+ and network access to a public Ethereum RPC, Blockscout and DexScreener. No key, no
dependencies. Every number is read from the chain at the head block except 24h volume (DexScreener).
"""
import argparse, json, math, os, sys, urllib.request

RPCS = [u for u in os.environ.get("RPC_URLS", "").split(",") if u] or [
    "https://ethereum-rpc.publicnode.com", "https://eth.drpc.org", "https://rpc.mevblocker.io"]
UA = {"user-agent": "Mozilla/5.0 imdusd-liquidity", "content-type": "application/json", "accept": "application/json"}

# --- mainnet addresses (imdUSD docs: imdusd.com/docs/overview/the-peg-pool) ---
IMDUSD = "0x61aaf8a992a3143e2b0c9cb0030b9fe702854a25"   # currency0, 18 decimals
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"     # currency1, 6 decimals
HOOK = "0x33ca5c16208ec08ac97d4f6315aafa792a916044"
POOL_MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
STATE_VIEW = "0x7ffe42c4a5deea5b0fec41c94c136cf115597227"
POSM = "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e"
VAULT = "0x0b69f8da8d8ce521d74ea69a7a3475b77302ef82"
PRICE_FEED = "0x070e0603737242b01abc1d223f7c92d63e86ec97"
SPOT_FEED = "0x77e1684402869bf2b7c70b8bf21e1c7ca7104951"
COLLATERAL_FEED = "0xf065c8b65aa9ac6fd9cbad15301738e91389d9e1"
PEG_POOL = "0x3cb8cc9c22f7da5c3169d4059131ddba9e61f68230977797c03a2f1214992035"
ETH_IMD_POOL = "0xb07d640fd9e2eb9dc81b953c8e4fd006bdfeaf276010fb5418eb763ca15abfb3"  # the pool the price feeds read
PEG_KEY = (IMDUSD, USDC, 100, 1, HOOK)  # fee 0.01%, tickSpacing 1

PEG_TICK = -276325                      # the tick holding $1
BAND = (-276424, -276224)               # Season 1 points band, $0.99005-$1.01005, half-open
WIDE = (PEG_TICK - 488, PEG_TICK + 488)  # the first position's range, $0.9524-$1.0500
BASE_FEE_PIPS = 100                      # LPs earn 0.01%; the hook's surcharge goes to the Treasury
LP_POINTS_PER_DAY, HOLD_POINTS_PER_DAY = 3, 1
VEST_BLOCKS = 7200                       # one day: removing liquidity inside it forfeits pending points

Q96, Q128, Q192 = 1 << 96, 1 << 128, 1 << 192
SEL = {  # 4-byte selectors (cast sig)
    "getSlot0": "c815641c", "getLiquidity": "fa6793d5", "getTickBitmap": "1c7ccb4c", "getTickLiquidity": "caedab54",
    "getPositionInfo": "dacf1d2f", "getFeeGrowthInside": "53e9c1fb", "getPoolAndPositionInfo": "7ba03aad",
    "getPositionLiquidity": "1efeed33", "ownerOf": "6352211e", "balanceOf2": "00fdd58e", "feeFor": "c7bbf9a9",
    "payoutPrice": "cdcca9dd", "backingPerUnit": "a741eea6", "redemptionFeeBps": "f2fd284a", "skew": "b27979ca",
    "isStale": "1a26f447", "latestValue": "4ab5cc82", "balanceOf": "70a08231", "redemptionReserve": "c05fb724",
}


# ---------------------------------------------------------------- RPC
def http(url, body=None, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rpc(method, params):
    err = None
    for url in RPCS:
        try:
            out = http(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in out:
                raise RuntimeError(out["error"].get("message", out["error"]))
            return out["result"]
        except Exception as e:  # next RPC
            err = e
    raise RuntimeError(f"{method}: every RPC failed ({err})")


def batch(calls, block):
    """eth_call many (to, data) at one block; one JSON-RPC batch per 100. Returns hex results (None on revert)."""
    out = []
    for i in range(0, len(calls), 100):
        chunk = calls[i:i + 100]
        body = [{"jsonrpc": "2.0", "id": j, "method": "eth_call", "params": [{"to": to, "data": "0x" + data}, block]}
                for j, (to, data) in enumerate(chunk)]
        err = None
        for url in RPCS:
            try:
                res = http(url, body)
                if not isinstance(res, list):
                    raise RuntimeError(res.get("error", res))
                by = {r["id"]: r.get("result") for r in res}
                out += [by.get(j) for j in range(len(chunk))]
                break
            except Exception as e:
                err = e
        else:
            raise RuntimeError(f"eth_call batch failed on every RPC ({err})")
    return out


def call(to, data, block):
    return batch([(to, data)], block)[0]


# ---------------------------------------------------------------- ABI
def word(x):
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, str):
        return x.lower().replace("0x", "").rjust(64, "0")
    return (x % (1 << 256)).to_bytes(32, "big").hex()


def enc(name, *args):
    return SEL[name] + "".join(word(a) for a in args)


def words(hexstr):
    h = (hexstr or "0x")[2:]
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def signed(x, bits):
    x &= (1 << bits) - 1
    return x - (1 << bits) if x >> (bits - 1) else x


# ---------------------------------------------------------------- v4 math (TickMath port, exact)
def sqrt_at_tick(tick):
    a = abs(tick)
    r = 0xfffcb933bd6fad37aa2d162d1a594001 if a & 0x1 else 0x100000000000000000000000000000000
    for bit, m in [(0x2, 0xfff97272373d413259a46990580e213a), (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
                   (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0), (0x10, 0xffcb9843d60f6159c9db58835c926644),
                   (0x20, 0xff973b41fa98c081472e6896dfb254c0), (0x40, 0xff2ea16466c96a3843ec78b326b52861),
                   (0x80, 0xfe5dee046a99a2a811c461f1969c3053), (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
                   (0x200, 0xf987a7253ac413176f2b074cf7815e54), (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
                   (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9), (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
                   (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5), (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
                   (0x8000, 0x31be135f97d08fd981231505542fcfa6), (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
                   (0x20000, 0x5d6af8dedb81196699c329225ee604), (0x40000, 0x2216e584f5fa1ea926041bedfe98),
                   (0x80000, 0x48a170391f7dc42444e8fa2)]:
        if a & bit:
            r = (r * m) >> 128
    if tick > 0:
        r = ((1 << 256) - 1) // r
    return (r >> 32) + (1 if r % (1 << 32) else 0)


def usd(sqrt_p):
    """$ per imdUSD at a sqrtPriceX96 (USDC raw per imdUSD raw, times 1e12)."""
    return sqrt_p * sqrt_p * 10 ** 12 / Q192


def usd_at_tick(t):
    return 1.0001 ** t * 1e12


def tick_at_usd(p):
    return math.floor(math.log(p / 1e12) / math.log(1.0001))


def amounts(L, sp, sa, sb):
    """(imdUSD raw, USDC raw) held by liquidity L over [sa, sb] at sqrt price sp."""
    if sp <= sa:
        return L * Q96 * (sb - sa) // (sa * sb), 0
    if sp >= sb:
        return 0, L * (sb - sa) // Q96
    return L * Q96 * (sb - sp) // (sp * sb), L * (sp - sa) // Q96


def liquidity_for(sp, sa, sb, a0, a1):
    l0 = (a0 * sp * sb // Q96) // (sb - sp) if sp < sb else None
    l1 = a1 * Q96 // (sp - sa) if sp > sa else None
    if sp <= sa:
        return a0 * sa * sb // Q96 // (sb - sa)
    if sp >= sb:
        return a1 * Q96 // (sb - sa)
    return min(l0, l1)


def band_depth(L, lo, hi):
    """Season 1 depth: USDC value of L between $0.99 and $1.01, L x (sqrt(min(hi, band)) - sqrt(max(lo, band))), in $."""
    a, b = max(lo, BAND[0]), min(hi, BAND[1])
    if a >= b:
        return 0.0
    return L * (sqrt_at_tick(b) - sqrt_at_tick(a)) / Q96 / 1e6


# ---------------------------------------------------------------- reads
def head():
    return rpc("eth_blockNumber", [])


def slot0(pool, block):
    w = words(call(STATE_VIEW, enc("getSlot0", pool), block))
    return w[0], signed(w[1], 24), w[3]


def read_ticks(pool, tick, block, span=40000):
    """Every initialised tick within +-span of `tick`: {tick: liquidityNet}. tickSpacing 1, so word = tick >> 8."""
    ws = list(range((tick - span) >> 8, ((tick + span) >> 8) + 1))
    bitmaps = batch([(STATE_VIEW, enc("getTickBitmap", pool, w))
                     for w in ws], block)
    ticks = []
    for w, bm in zip(ws, bitmaps):
        b = int(bm or "0x0", 16)
        for bit in range(256):
            if b >> bit & 1:
                ticks.append((w << 8) + bit)
    nets = batch([(STATE_VIEW, enc("getTickLiquidity", pool, t)) for t in ticks], block)
    return {t: signed(words(n)[1], 128) for t, n in zip(ticks, nets)}


def segments(L_now, tick, sp, nets):
    """Liquidity per interval between initialised ticks: [(lo, hi, L)], ordered by tick."""
    ts = sorted(nets)
    below = [t for t in ts if t <= tick]
    above = [t for t in ts if t > tick]
    segs = []
    L = L_now
    prev = tick
    # walk down from the current tick: crossing t downward removes net(t)
    lo_edges = list(reversed(below))
    upper = above[0] if above else tick + 1
    cur_lo = lo_edges[0] if lo_edges else tick
    segs.append((cur_lo, upper, L))
    for i, t in enumerate(lo_edges):
        L -= nets[t]
        nxt = lo_edges[i + 1] if i + 1 < len(lo_edges) else None
        if nxt is None:
            break
        segs.append((nxt, t, L))
    L = L_now
    for i, t in enumerate(above):
        L += nets[t]
        nxt = above[i + 1] if i + 1 < len(above) else None
        if nxt is None:
            break
        segs.append((t, nxt, L))
    return sorted([s for s in segs if s[2] > 0 and s[0] < s[1]])


def swap_in(segs, sp, target):
    """Raw input (no fees) to move the price from sp to target: imdUSD in to go down, USDC in to go up."""
    total = 0
    if target > sp:
        for lo, hi, L in segs:
            a, b = max(sqrt_at_tick(lo), sp), min(sqrt_at_tick(hi), target)
            if b > a:
                total += L * (b - a) // Q96
    else:
        for lo, hi, L in segs:
            a, b = max(sqrt_at_tick(lo), target), min(sqrt_at_tick(hi), sp)
            if b > a:
                total += L * Q96 * (b - a) // (a * b)
    return total


def dexscreener(token=IMDUSD):
    try:
        req = urllib.request.Request(f"https://api.dexscreener.com/token-pairs/v1/ethereum/{token}", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return []


def geckoterminal(token=IMDUSD):
    try:
        req = urllib.request.Request(f"https://api.geckoterminal.com/api/v2/networks/eth/tokens/{token}/pools", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("data", [])
    except Exception:
        return []


def volume_24h(pool_id):
    for p in dexscreener():
        if p.get("pairAddress", "").lower() == pool_id.lower():
            return float(p.get("volume", {}).get("h24") or 0), float(p.get("liquidity", {}).get("usd") or 0)
    return None, None


# ---------------------------------------------------------------- status
def status(args):
    block = head()
    sp, tick, lp_fee = slot0(PEG_POOL, block)
    L = words(call(STATE_VIEW, enc("getLiquidity", PEG_POOL), block))[0]
    nets = read_ticks(PEG_POOL, tick, block)
    segs = segments(L, tick, sp, nets)
    price = usd(sp)
    peg_sqrt = Q96 // 10 ** 6

    def depth(x):
        up = swap_in(segs, sp, int(sp * math.sqrt(1 + x))) / 1e6
        down = swap_in(segs, sp, int(sp * math.sqrt(1 - x))) / 1e18
        return up, down

    depths = {f"{x * 100:g}%": dict(zip(("usdcToPushUp", "imdusdToPushDown"), depth(x))) for x in (0.0025, 0.005, 0.01, 0.02)}
    ladder = []
    for target in (0.95, 0.97, 0.98, 0.99, 0.995, 0.9975, 1.0, 1.0025, 1.005, 1.01, 1.02):
        t_sqrt = int(math.isqrt(int(target * 1e18 * Q192 // 10 ** 30)))
        if (target > price and target <= 1.0) or (target < price and target >= 1.0):
            amt = swap_in(segs, sp, t_sqrt)
            ladder.append({"to": target, "in": amt / (1e6 if target > price else 1e18), "token": "USDC" if target > price else "imdUSD"})
    # where the pool's liquidity sits, by price bucket
    edges = [0.01, 0.5, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 0.9975, 1.0025, 1.005, 1.01, 1.02, 1.05, 1.2, 2.0, 100.0]
    buckets = [{"from": edges[i], "to": edges[i + 1], "imdusd": 0.0, "usdc": 0.0} for i in range(len(edges) - 1)]
    in_band_depth = 0.0
    total_value = 0.0
    for lo, hi, Lseg in segs:
        a0, a1 = amounts(Lseg, sp, sqrt_at_tick(lo), sqrt_at_tick(hi))
        total_value += a0 / 1e18 * price + a1 / 1e6
        in_band_depth += band_depth(Lseg, lo, hi)
        for bk in buckets:  # the segment's slice inside each price bucket
            x, y = max(lo, tick_at_usd(bk["from"])), min(hi, tick_at_usd(bk["to"]))
            if x < y:
                b0, b1 = amounts(Lseg, sp, sqrt_at_tick(x), sqrt_at_tick(y))
                bk["imdusd"] += b0 / 1e18
                bk["usdc"] += b1 / 1e6
    buckets = [b for b in buckets if b["imdusd"] + b["usdc"] > 0.5]
    # fee now, both directions (the hook prices its surcharge from the END price; this is the marginal swap)
    fee_sell = words(call(HOOK, enc("feeFor", sp, True), block))[0] / 1e4
    fee_buy = words(call(HOOK, enc("feeFor", sp, False), block))[0] / 1e4
    unswept = batch([(POOL_MANAGER, enc("balanceOf2", HOOK, int(IMDUSD, 16))), (POOL_MANAGER, enc("balanceOf2", HOOK, int(USDC, 16)))], block)
    vol, _ = volume_24h(PEG_POOL)
    in_range_value = 0.0
    # active liquidity's dollar value in a +-0.5% window around the price: the capital that actually earns the fee
    win = (int(sp * math.sqrt(0.995)), int(sp * math.sqrt(1.005)))
    for lo, hi, Lseg in segs:
        a, b = max(sqrt_at_tick(lo), win[0]), min(sqrt_at_tick(hi), win[1])
        if b > a:
            x0, x1 = amounts(Lseg, sp, a, b)
            in_range_value += x0 / 1e18 * price + x1 / 1e6
    out = {
        "block": int(block, 16), "price": price, "tick": tick, "offPegBps": round((price - 1) * 1e4),
        "atPeg": BAND[0] <= tick < BAND[1], "activeLiquidity": L, "lpFeePips": lp_fee,
        "feeNowPct": {"sellImdusd": fee_sell, "buyImdusd": fee_buy},
        "depth": depths, "costToPeg": ladder, "liquidityByPrice": buckets, "poolValueUsd": total_value,
        "seasonBandDepthUsd": in_band_depth, "volume24hUsd": vol,
        "lpFees24hUsd": vol * BASE_FEE_PIPS / 1e6 if vol is not None else None,
        "unsweptSurcharge": {"imdusd": words(unswept[0])[0] / 1e18, "usdc": words(unswept[1])[0] / 1e6},
        "redemption": redemption(block, price),
    }
    if vol and in_range_value:
        out["feeAprAtPricePct"] = vol * BASE_FEE_PIPS / 1e6 * 365 / in_range_value * 100
    if args.json:
        return print(json.dumps(out, indent=2))
    print(f"imdUSD/USDC peg pool  block {out['block']}")
    print(f"price      ${price:.5f}  ({out['offPegBps']:+d} bps)  tick {tick}  {'IN the $0.99-$1.01 points band' if out['atPeg'] else 'OUTSIDE the points band: no LP position earns points now'}")
    print(f"fee now    selling imdUSD {fee_sell:.2f}%   buying imdUSD {fee_buy:.2f}%   (LPs keep 0.01%; the rest is the hook's surcharge to the Treasury)")
    print(f"pool       ${total_value:,.0f} of liquidity in the pool; ${in_band_depth:,.0f} of Season 1 band depth")
    if vol is not None:
        print(f"volume     ${vol:,.0f} in 24h (DexScreener) -> ${out['lpFees24hUsd']:,.2f} to LPs" + (f"; ~{out['feeAprAtPricePct']:.1f}% APR on the ${in_range_value:,.0f} within +-0.5% of the price" if out.get('feeAprAtPricePct') else ""))
    print("depth      to move the price   up: USDC in     down: imdUSD in")
    for k, v in depths.items():
        print(f"           {k:>6}             {v['usdcToPushUp']:>10,.0f}       {v['imdusdToPushDown']:>10,.0f}")
    if ladder:
        print("to peg     " + "   ".join(f"${s['to']}: {s['in']:,.0f} {s['token']}" for s in ladder))
    print("liquidity by price (what LPs hold there now)")
    for b in buckets:
        print(f"           ${b['from']:.4f}-${b['to']:.4f}   {b['imdusd']:>10,.0f} imdUSD   {b['usdc']:>10,.0f} USDC")
    u = out["unsweptSurcharge"]
    print(f"hook       unswept surcharge {u['imdusd']:,.2f} imdUSD + {u['usdc']:,.2f} USDC (anyone may sweep() it to the Treasury)")
    r = out["redemption"]
    if r:
        print(f"redeem     backing ${r['backing']:.4f}  fee {r['feeBps'] / 100:.2f}%  payout price {r['payoutPremiumPct']:+.1f}% vs market  feeds {'FRESH' if r['fresh'] else 'STALE: cash() reverts'}")
        print(f"           a redeemed imdUSD is worth ~${r['valueAtMarket']:.4f} after the exit swap, vs ${price:.4f} in the pool: {'redeem' if r['valueAtMarket'] > price else 'redemption loses'}")


def redemption(block, pool_price):
    try:
        res = batch([(VAULT, enc("backingPerUnit")), (VAULT, enc("payoutPrice")), (VAULT, enc("redemptionFeeBps", 1000 * 10 ** 18)),
                     (PRICE_FEED, enc("isStale")), (SPOT_FEED, enc("isStale")), (PRICE_FEED, enc("latestValue")),
                     (COLLATERAL_FEED, enc("latestValue"))], block)
        backing, payout, fee, stale_p, stale_s = (words(x)[0] for x in res[:5])
        imd_feed = words(res[5])[0]
        coll = words(res[6])[0]
        sp_imd, _, _ = slot0(ETH_IMD_POOL, block)
        imd_live = 10 ** 18 * Q192 // (sp_imd * sp_imd)  # wei ETH per 1e18 IMD, the feeds' direction
        market_coll = coll * imd_live / imd_feed if imd_feed else coll
        value = backing / 1e18 * (1 - fee / 1e4) * market_coll / payout * (1 - 0.0105)  # exit: unstake, IMD->ETH 1%, ETH->USDC
        return {"backing": backing / 1e18, "feeBps": fee, "payoutPremiumPct": (payout / market_coll - 1) * 100,
                "fresh": not (stale_p or stale_s), "valueAtMarket": value}
    except Exception:
        return None


# ---------------------------------------------------------------- positions
def blockscout_ids(wallet):
    ids = []
    url = f"https://eth.blockscout.com/api/v2/addresses/{wallet}/nft?type=ERC-721"
    for _ in range(10):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read())
        for it in d.get("items", []):
            if (it.get("token") or {}).get("address_hash", it.get("token", {}).get("address", "")).lower() == POSM:
                ids.append(int(it["id"]))
        nxt = d.get("next_page_params")
        if not nxt:
            break
        url = f"https://eth.blockscout.com/api/v2/addresses/{wallet}/nft?type=ERC-721&" + "&".join(f"{k}={v}" for k, v in nxt.items())
    return ids


def position(pid, block, sp, tick):
    info_hex, liq_hex = batch([(POSM, enc("getPoolAndPositionInfo", pid)), (POSM, enc("getPositionLiquidity", pid))], block)
    w = words(info_hex)
    key = ("0x" + format(w[0], "040x"), "0x" + format(w[1], "040x"), w[2], signed(w[3], 24), "0x" + format(w[4], "040x"))
    if key != PEG_KEY:
        return None
    info = w[5]
    lo, hi = signed(info >> 8, 24), signed(info >> 32, 24)
    L = words(liq_hex)[0]
    a0, a1 = amounts(L, sp, sqrt_at_tick(lo), sqrt_at_tick(hi))
    pi, fg = batch([(STATE_VIEW, enc("getPositionInfo", PEG_POOL, POSM, lo, hi, pid)),
                    (STATE_VIEW, enc("getFeeGrowthInside", PEG_POOL, lo, hi))], block)
    _, last0, last1 = words(pi)
    cur0, cur1 = words(fg)
    owed0 = ((cur0 - last0) % (1 << 256)) * L >> 128
    owed1 = ((cur1 - last1) % (1 << 256)) * L >> 128
    depth = band_depth(L, lo, hi)
    in_range = lo <= tick < hi
    earning = in_range and BAND[0] <= tick < BAND[1] and depth > 0
    return {"id": pid, "tickLower": lo, "tickUpper": hi, "range": [usd_at_tick(lo), usd_at_tick(hi)], "liquidity": L,
            "imdusd": a0 / 1e18, "usdc": a1 / 1e6, "valueUsd": a0 / 1e18 * usd(sp) + a1 / 1e6,
            "feesOwed": {"imdusd": owed0 / 1e18, "usdc": owed1 / 1e6}, "inRange": in_range, "bandDepthUsd": depth,
            "pointsPerDay": LP_POINTS_PER_DAY * depth if earning else 0.0, "earningPoints": earning}


def positions(args):
    wallet = (args.wallet or os.environ.get("IMD_WALLET") or "").lower()
    if not wallet:
        sys.exit("positions needs --wallet 0x… (or IMD_WALLET): the address that holds the position NFTs")
    block = head()
    sp, tick, _ = slot0(PEG_POOL, block)
    ids = sorted(set(blockscout_ids(wallet) + [int(x) for x in (args.ids or [])]))
    rows = []
    for pid in ids:
        owner = call(POSM, enc("ownerOf", pid), block)
        if not owner or "0x" + owner[-40:] != wallet:
            continue
        p = position(pid, block, sp, tick)
        if p:
            rows.append(p)
    if args.json:
        return print(json.dumps({"block": int(block, 16), "price": usd(sp), "tick": tick, "positions": rows}, indent=2))
    print(f"{wallet}: {len(rows)} peg-pool position(s) at ${usd(sp):.5f} (tick {tick})")
    for p in rows:
        lo, hi = p["range"]
        state = "EARNING points + fees" if p["earningPoints"] else ("in range, earning fees; price outside the points band" if p["inRange"] else "OUT OF RANGE: earns nothing")
        print(f"  #{p['id']}  ${lo:.4f}-${hi:.4f}  {state}")
        print(f"        holds {p['imdusd']:,.2f} imdUSD + {p['usdc']:,.2f} USDC = ${p['valueUsd']:,.2f}   fees owed {p['feesOwed']['imdusd']:,.4f} imdUSD + {p['feesOwed']['usdc']:,.4f} USDC")
        print(f"        Season 1 depth ${p['bandDepthUsd']:,.2f} -> {p['pointsPerDay']:,.1f} points/day now")
    if not rows:
        print("  none found (Blockscout lists the wallet's NFTs; pass --ids N … if it lags)")


# ---------------------------------------------------------------- plan
def parse_range(r, tick):
    if r == "peg":
        return BAND
    if r == "wide":
        return WIDE
    lo, hi = (float(x) for x in r.split(":"))
    return tick_at_usd(lo), tick_at_usd(hi) + 1


def plan(args):
    block = head()
    sp, tick, _ = slot0(PEG_POOL, block)
    L_now = words(call(STATE_VIEW, enc("getLiquidity", PEG_POOL), block))[0]
    lo, hi = parse_range(args.range, tick)
    sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
    a0 = int(args.imdusd * 1e18)
    a1 = int(args.usdc * 1e6)
    L = liquidity_for(sp, sa, sb, a0, a1)
    u0, u1 = amounts(L, sp, sa, sb)
    price = usd(sp)
    depth = band_depth(L, lo, hi)
    in_range = lo <= tick < hi
    share = L / (L_now + L) if in_range else 0.0
    vol, _ = volume_24h(PEG_POOL)
    fees_day = (vol or 0) * BASE_FEE_PIPS / 1e6 * share
    capital = u0 / 1e18 * price + u1 / 1e6
    at_lo = amounts(L, sa, sa, sb)
    at_hi = amounts(L, sb, sa, sb)
    out = {"range": [usd_at_tick(lo), usd_at_tick(hi)], "ticks": [lo, hi], "liquidity": L, "uses": {"imdusd": u0 / 1e18, "usdc": u1 / 1e6},
           "leftover": {"imdusd": (a0 - u0) / 1e18, "usdc": (a1 - u1) / 1e6}, "capitalUsd": capital, "inRangeNow": in_range,
           "shareOfActiveLiquidity": share, "lpFeesPerDayUsd": fees_day, "bandDepthUsd": depth,
           "pointsPerDayWhileAtPeg": LP_POINTS_PER_DAY * depth, "pointsPerDayIfHeldInstead": HOLD_POINTS_PER_DAY * (u0 / 1e18),
           "atRangeLow": {"imdusd": at_lo[0] / 1e18, "usdc": at_lo[1] / 1e6}, "atRangeHigh": {"imdusd": at_hi[0] / 1e18, "usdc": at_hi[1] / 1e6}}
    if args.json:
        return print(json.dumps(out, indent=2))
    r0, r1 = out["range"]
    print(f"plan  ${r0:.5f}-${r1:.5f}  (ticks {lo}..{hi})  at ${price:.5f}")
    print(f"uses  {u0 / 1e18:,.2f} imdUSD + {u1 / 1e6:,.2f} USDC = ${capital:,.2f}; left over {(a0 - u0) / 1e18:,.2f} imdUSD + {(a1 - u1) / 1e6:,.2f} USDC")
    if not in_range:
        side = "USDC only (a buy wall under the price)" if tick >= hi else "imdUSD only (a sell wall above the price)"
        print(f"      the range is out of the price: it takes {side} and earns nothing until the price enters it")
    if L == 0 and (a0 or a1):
        if in_range:
            # the range straddles the price: it needs both tokens in a fixed ratio
            if a1:
                need = amounts(a1 * Q96 // (sp - sa), sp, sa, sb)[0] / 1e18
                print(f"      a range around the price needs both tokens: {args.usdc:,.2f} USDC goes with {need:,.2f} imdUSD")
            else:
                need = amounts((a0 * sp * sb // Q96) // (sb - sp), sp, sa, sb)[1] / 1e6
                print(f"      a range around the price needs both tokens: {args.imdusd:,.2f} imdUSD goes with {need:,.2f} USDC")
        one = f"--range {usd_at_tick(tick - 400):.4f}:{usd_at_tick(tick):.4f}" if a1 else f"--range {usd_at_tick(tick + 1):.4f}:{max(1.0, usd_at_tick(tick + 400)):.4f}"
        print(f"      one-sided instead: {'USDC' if a1 else 'imdUSD'} alone fits a range entirely {'below' if a1 else 'above'} the price, e.g. {one}")
    print(f"fees  {share * 100:.2f}% of active liquidity -> ~${fees_day:,.2f}/day at 24h volume ${vol or 0:,.0f}")
    print(f"pts   {LP_POINTS_PER_DAY * depth:,.1f} points/day while the price is inside both the band and this range (vs {u0 / 1e18:,.1f}/day holding the imdUSD)")
    print(f"ends  at ${r0:.4f}: {at_lo[0] / 1e18:,.2f} imdUSD (all bought with your USDC)   at ${r1:.4f}: {at_hi[1] / 1e6:,.2f} USDC")
    print("hold  pending points vest 7,200 blocks (~1 day) after the LAST addition; removing any liquidity sooner forfeits them all")


# ---------------------------------------------------------------- avenues
def avenues(args):
    rows = {}
    for p in dexscreener():
        rows[p["pairAddress"].lower()] = {"pool": p["pairAddress"], "dex": f"{p.get('dexId')} {','.join(p.get('labels') or [])}".strip(),
                                          "pair": f"{p['baseToken']['symbol']}/{p['quoteToken']['symbol']}",
                                          "liquidityUsd": float((p.get("liquidity") or {}).get("usd") or 0),
                                          "volume24hUsd": float((p.get("volume") or {}).get("h24") or 0), "priceUsd": p.get("priceUsd"), "fee": None}
    for g in geckoterminal():
        a = g["attributes"]
        k = a["address"].lower()
        name = a["name"]
        fee = name.rsplit(" ", 1)[-1] if name.endswith("%") else None
        r = rows.setdefault(k, {"pool": a["address"], "dex": g["relationships"]["dex"]["data"]["id"], "pair": name.rsplit(" ", 1)[0],
                                "liquidityUsd": float(a.get("reserve_in_usd") or 0), "volume24hUsd": float((a.get("volume_usd") or {}).get("h24") or 0),
                                "priceUsd": a.get("base_token_price_usd"), "fee": None})
        r["fee"] = fee
    for r in rows.values():
        r["points"] = "3x LP (in band), the only pool that earns" if r["pool"].lower() == PEG_POOL else "none"
        if r["fee"]:
            pct = float(r["fee"].rstrip("%"))
            r["lpFees24hUsd"] = r["volume24hUsd"] * pct / 100 if r["pool"].lower() != PEG_POOL else r["volume24hUsd"] * 0.0001
            if r["liquidityUsd"] > 0:
                r["feeAprPct"] = r["lpFees24hUsd"] * 365 / r["liquidityUsd"] * 100
    try:
        y = [p for p in http("https://yields.llama.fi/pools")["data"] if "IMDUSD" in p["symbol"].upper()]
    except Exception:
        y = []
    try:
        merkl = http("https://api.merkl.xyz/v4/opportunities?search=imdUSD&chainId=1")
    except Exception:
        merkl = []
    out = {"pools": sorted(rows.values(), key=lambda r: -r["liquidityUsd"]), "defillamaYields": y, "merklCampaigns": merkl}
    if args.json:
        return print(json.dumps(out, indent=2))
    print("imdUSD pools (DexScreener + GeckoTerminal)")
    for r in out["pools"]:
        apr = f"  ~{r['feeAprPct']:.0f}% fee APR" if r.get("feeAprPct") is not None else ""
        print(f"  {r['pair']:<16} {r['dex']:<22} fee {r['fee'] or '?':<6} liq ${r['liquidityUsd']:>10,.0f}  vol24h ${r['volume24hUsd']:>10,.0f}  price ${float(r['priceUsd'] or 0):.4f}{apr}")
        print(f"  {'':16} {r['pool']}  points: {r['points']}")
    print(f"DefiLlama yield pools listing imdUSD: {len(y)}   Merkl campaigns: {len(merkl) if isinstance(merkl, list) else '?'}")
    print("protocol routes (no pool needed): hold imdUSD in a wallet (1 point/day per imdUSD); liquidate (3 days of holding")
    print("points per imdUSD repaid); a borrower buying below $1 and wipe()-ing debt at par; cash() redemption when it pays (see status)")


# ---------------------------------------------------------------- tx: unsigned transactions, simulated
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
KEY_ARG = f"({IMDUSD},{USDC},100,1,{HOOK})"
MINT, INCREASE, DECREASE, BURN, SETTLE_PAIR, TAKE_PAIR, CLOSE_CURRENCY = 0x02, 0x00, 0x01, 0x03, 0x0d, 0x11, 0x12
SLIPPAGE_BPS = 50


def cast(*a):
    import subprocess
    env = dict(os.environ, FOUNDRY_DISABLE_NIGHTLY_WARNING="1")
    try:
        return subprocess.run(["cast", *map(str, a)], capture_output=True, text=True, check=True, env=env).stdout.strip()
    except FileNotFoundError:
        sys.exit("tx needs Foundry's cast on PATH (https://getfoundry.sh) for ABI encoding")


def modify(actions, params, deadline):
    unlock = cast("abi-encode", "f(bytes,bytes[])", "0x" + bytes(actions).hex(), "[" + ",".join(params) + "]")
    return cast("calldata", "modifyLiquidities(bytes,uint256)", unlock, deadline)


def approvals(owner, need, block, now):
    """ERC20 -> Permit2 and Permit2 -> PositionManager approvals the owner still lacks, for {token: raw amount}."""
    txs = []
    for token, amt in need.items():
        if amt <= 0:
            continue
        erc = words(call(token, "dd62ed3e" + word(owner) + word(PERMIT2), block))[0]
        if erc < amt:
            txs.append({"label": f"approve Permit2 for {'imdUSD' if token == IMDUSD else 'USDC'}", "to": token,
                        "data": cast("calldata", "approve(address,uint256)", PERMIT2, amt)})
        p2 = words(call(PERMIT2, "927da105" + word(owner) + word(token) + word(POSM), block))
        if p2[0] < amt or p2[1] < now + 300:
            txs.append({"label": f"Permit2: let the PositionManager pull {'imdUSD' if token == IMDUSD else 'USDC'}", "to": PERMIT2,
                        "data": cast("calldata", "approve(address,address,uint160,uint48)", token, POSM, amt, now + 3600)})
    return txs


def simulate(owner, txs):
    calls = [{"from": owner, "to": t["to"], "data": t["data"]} for t in txs]
    try:
        res = rpc("eth_simulateV1", [{"blockStateCalls": [{"calls": calls}], "validation": False}, "latest"])
        return [("ok" if int(c["status"], 16) == 1 else f"REVERTS {c.get('error', {}).get('message') or c.get('returnData', '')[:74]}")
                for c in res[0]["calls"]]
    except Exception as e:
        return [f"not simulated ({str(e)[:80]})"] * len(txs)


def build_tx(args):
    owner = (args.owner or args.wallet or os.environ.get("IMD_WALLET") or "").lower()
    if not owner:
        sys.exit("tx needs --owner 0x… (the wallet or Safe that will sign; it receives the position and the tokens)")
    block = head()
    sp, tick, _ = slot0(PEG_POOL, block)
    now = int(rpc("eth_getBlockByNumber", [block, False])["timestamp"], 16)
    deadline = now + 1800
    up = lambda x: x * (10_000 + SLIPPAGE_BPS) // 10_000 + 1
    down = lambda x: x * (10_000 - SLIPPAGE_BPS) // 10_000
    txs = []
    if args.action in ("mint", "increase"):
        if args.action == "mint":
            lo, hi = parse_range(args.range, tick)
        else:
            p = position(args.id, block, sp, tick)
            if not p:
                sys.exit(f"#{args.id} is not a peg-pool position")
            lo, hi = p["tickLower"], p["tickUpper"]
        sa, sb = sqrt_at_tick(lo), sqrt_at_tick(hi)
        L = liquidity_for(sp, sa, sb, int(args.imdusd * 1e18), int(args.usdc * 1e6))
        if L == 0:
            sys.exit("that deposit adds no liquidity here: run `plan` with the same amounts and range to see why")
        u0, u1 = amounts(L, sp, sa, sb)
        m0, m1 = up(u0) if u0 else 0, up(u1) if u1 else 0
        for token, amt, name, dec in ((IMDUSD, m0, "imdUSD", 1e18), (USDC, m1, "USDC", 1e6)):
            bal = words(call(token, enc("balanceOf", owner), block))[0]
            if bal < amt:
                print(f"note: {owner} holds {bal / dec:,.2f} {name} and this needs up to {amt / dec:,.2f}: the deposit will revert until it is funded")
        txs += approvals(owner, {IMDUSD: m0, USDC: m1}, block, now)
        if args.action == "mint":
            p0 = cast("abi-encode", "f((address,address,uint24,int24,address),int24,int24,uint256,uint128,uint128,address,bytes)",
                      KEY_ARG, lo, hi, L, m0, m1, owner, "0x")
            data = modify([MINT, SETTLE_PAIR], [p0, cast("abi-encode", "f(address,address)", IMDUSD, USDC)], deadline)
            label = f"mint ${usd_at_tick(lo):.4f}-${usd_at_tick(hi):.4f} with {u0 / 1e18:,.2f} imdUSD + {u1 / 1e6:,.2f} USDC"
        else:
            p0 = cast("abi-encode", "f(uint256,uint256,uint128,uint128,bytes)", args.id, L, m0, m1, "0x")
            data = modify([INCREASE, CLOSE_CURRENCY, CLOSE_CURRENCY], [p0, cast("abi-encode", "f(address)", IMDUSD), cast("abi-encode", "f(address)", USDC)], deadline)
            label = f"add {u0 / 1e18:,.2f} imdUSD + {u1 / 1e6:,.2f} USDC to #{args.id} (restarts its 1-day points hold)"
        txs.append({"label": label, "to": POSM, "data": data})
    elif args.action in ("remove", "collect", "close"):
        p = position(args.id, block, sp, tick)
        if not p:
            sys.exit(f"#{args.id} is not a peg-pool position")
        take = cast("abi-encode", "f(address,address,address)", IMDUSD, USDC, owner)
        if args.action == "close":
            p0 = cast("abi-encode", "f(uint256,uint128,uint128,bytes)", args.id, down(int(p["imdusd"] * 1e18)), down(int(p["usdc"] * 1e6)), "0x")
            txs.append({"label": f"close #{args.id}: withdraw everything + fees, burn the NFT", "to": POSM, "data": modify([BURN, TAKE_PAIR], [p0, take], deadline)})
        else:
            pct = 0 if args.action == "collect" else args.pct
            L = p["liquidity"] * pct // 100
            p0 = cast("abi-encode", "f(uint256,uint256,uint128,uint128,bytes)", args.id, L,
                      down(int(p["imdusd"] * 1e18 * pct / 100)), down(int(p["usdc"] * 1e6 * pct / 100)), "0x")
            label = f"collect fees of #{args.id} (no liquidity removed: points are safe)" if not pct else f"remove {pct}% of #{args.id} + its fees"
            txs.append({"label": label, "to": POSM, "data": modify([DECREASE, TAKE_PAIR], [p0, take], deadline)})
    elif args.action == "sweep":
        for t, name in ((USDC, "USDC"), (IMDUSD, "imdUSD")):
            txs.append({"label": f"sweep the hook's {name} surcharge to the Treasury", "to": HOOK, "data": cast("calldata", "sweep(address)", t)})
    sims = simulate(owner, txs)
    out = {"owner": owner, "block": int(block, 16), "deadline": deadline, "transactions": [dict(t, value="0", simulation=s) for t, s in zip(txs, sims)]}
    if args.safe:
        safe = {"version": "1.0", "chainId": "1", "meta": {"name": f"imdUSD peg pool: {args.action}"},
                "transactions": [{"to": t["to"], "value": "0", "data": t["data"]} for t in txs]}
        with open(args.safe, "w") as f:
            json.dump(safe, f, indent=2)
    if args.json:
        return print(json.dumps(out, indent=2))
    print(f"{len(txs)} transaction(s) for {owner}, simulated in order at the head block (nothing signed, nothing sent):")
    for i, (t, s) in enumerate(zip(txs, sims), 1):
        print(f"  {i}. {t['label']}\n     to {t['to']}  simulation: {s}")
    if any(s != "ok" for s in sims):
        print("  a step does not simulate cleanly: fix it (balances, amounts, range) before signing anything")
    print(f"deadline {deadline} (30 minutes): sign within it or rebuild")
    if args.safe:
        print(f"Safe Transaction Builder batch written to {args.safe}")
    print("sign with your own wallet, e.g. for each step:  cast send <to> <data> --rpc-url <rpc> --ledger   (--json prints the data)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rpc", help="your own RPC URL (tried first)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("positions")
    p.add_argument("--wallet")
    p.add_argument("--ids", nargs="*", help="position NFT ids to include if Blockscout lags")
    p = sub.add_parser("plan")
    p.add_argument("--usdc", type=float, default=0)
    p.add_argument("--imdusd", type=float, default=0)
    p.add_argument("--range", default="peg", help="peg ($0.99-$1.01, the points band), wide ($0.9524-$1.05), or LOW:HIGH in $")
    sub.add_parser("avenues")
    p = sub.add_parser("tx", help="build + simulate unsigned transactions: mint, increase, remove, collect, close, sweep")
    p.add_argument("action", choices=["mint", "increase", "remove", "collect", "close", "sweep"])
    p.add_argument("--owner", help="the signing wallet or Safe")
    p.add_argument("--wallet", help=argparse.SUPPRESS)
    p.add_argument("--id", type=int, help="position NFT id (increase/remove/collect/close)")
    p.add_argument("--usdc", type=float, default=0)
    p.add_argument("--imdusd", type=float, default=0)
    p.add_argument("--range", default="peg")
    p.add_argument("--pct", type=int, default=100, help="remove: percent of the liquidity")
    p.add_argument("--safe", help="also write a Safe Transaction Builder JSON here")
    for s in sub.choices.values():
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.rpc:
        RPCS.insert(0, args.rpc)
    {"status": status, "positions": positions, "plan": plan, "avenues": avenues, "tx": build_tx}[args.cmd](args)


if __name__ == "__main__":
    main()
