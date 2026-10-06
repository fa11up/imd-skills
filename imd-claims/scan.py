#!/usr/bin/env python3
"""Scan a seat's IdentityMD launch-reward allocations: what is claimable, is the contract what its
source says, and is there a market to sell into.

    python3 scan.py --wallet 0xYourSeatWallet [--all] [--launch N ...] [--out DIR] [--workdir DIR]
    (or set IMD_WALLET instead of --wallet)

For every MAINNET allocation not yet claimed (Sepolia tokens have no value and are only counted):
  1. claim     our leaf from api.imd.fun, proof recomputed against the distributor's on-chain root,
               claimed? unlocked? eth_call of claim() from our wallet, and its gas cost now
  2. contract  clones the launch's sourceRepoUrl at sourceCommit, compiles with forge, compares the
               runtime bytecode with the chain (immutables masked), compares vendored OpenZeppelin
               files with the official release, and greps the token source for risk patterns
  3. market    the launch's own v4 pool (key rebuilt from the launch record), its live price, tick and
               in-range liquidity from the PoolManager, plus DexScreener liquidity/volume and the
               value of our allocation

Writes a JSON report to --out (default ./imd-claims-reports) and prints a summary. The
verdict on safety and on claiming is the reader's: the script flags, it does not decide.
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

os.environ["FOUNDRY_DISABLE_NIGHTLY_WARNING"] = "1"
API = "https://api.imd.fun"
RPC = "https://ethereum-rpc.publicnode.com"
WALLET = None  # the seat owner's wallet, from --wallet or IMD_WALLET
IMD = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"
V4_QUOTER = "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203"  # Uniswap v4 Quoter, mainnet

# v4 hook permission bits (lowest 14 bits of the hook address)
HOOK_FLAGS = [(13, "beforeInitialize"), (12, "afterInitialize"), (11, "beforeAddLiquidity"), (10, "afterAddLiquidity"),
              (9, "beforeRemoveLiquidity"), (8, "afterRemoveLiquidity"), (7, "beforeSwap"), (6, "afterSwap"),
              (5, "beforeDonate"), (4, "afterDonate"), (3, "beforeSwapReturnsDelta"), (2, "afterSwapReturnsDelta"),
              (1, "afterAddLiquidityReturnsDelta"), (0, "afterRemoveLiquidityReturnsDelta")]

# Patterns worth a human look in a token's own source (not in vendored libraries).
RISK_PATTERNS = {
    "owner/admin": r"\bonlyOwner\b|\bOwnable\b|\bowner\(\)|\badmin\b|AccessControl|onlyRole",
    "mint after launch": r"function\s+\w*mint\w*\s*\(",
    "burn": r"function\s+\w*burn\w*\s*\(",
    "fee/tax": r"\bfee\w*\b|\btax\w*\b|FEE_BPS|feeRecipient",
    "blocklist": r"blacklist|blocklist|isBlocked|_blocked|denylist",
    "pause": r"\bpause\w*\b|whenNotPaused",
    "upgrade/proxy": r"delegatecall|upgradeTo|Proxy|implementation\(",
    "selfdestruct": r"selfdestruct",
    "external call": r"\.call\(|\.staticcall\(|\.call\{",
    "max wallet/tx limit": r"maxWallet|maxTx|_maxTransaction|tradingEnabled|tradingOpen",
}


def cast(*a):
    return subprocess.check_output(["cast", *a], stderr=subprocess.STDOUT).decode().strip()


def get(url):
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as ex:  # the plane throttles; DexScreener 429s
            last = ex
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def leaf_root(amount, proof):
    h = cast("keccak", cast("keccak", cast("abi-encode", "f(address,uint256)", WALLET, str(amount))))
    for p in proof:
        a, b = sorted([h.lower(), p.lower()])
        h = cast("keccak", a + b[2:])
    return h.lower()


def claim_check(launch, dist):
    leaves = launch["claims"]["leaves"]
    leaf = next((l for l in leaves if l["wallet"].lower() == WALLET), None)
    if not leaf:
        return {"error": "no leaf for our wallet"}
    amount, proof = int(leaf["amount"]), leaf["proof"]
    rnd = cast("call", "--rpc-url", RPC, dist, "roundOf(uint256)((bytes32,uint256,uint256,uint64))", "0")
    root, funded, claimed_total, unlocks = [x.strip("() ").split(" ")[0] for x in rnd.split(",")]
    now = int(cast("block", "--rpc-url", RPC, "latest", "--field", "timestamp"))
    claimed = cast("call", "--rpc-url", RPC, dist, "claimed(uint256,address)(bool)", "0", WALLET) == "true"
    data = cast("calldata", "claim(uint256,address,uint256,bytes32[])", "0", WALLET, str(amount), "[" + ",".join(proof) + "]")
    out = {"amount": amount, "rootVerified": leaf_root(amount, proof) == root.lower(), "claimed": claimed,
           "unlocksAt": int(unlocks), "unlocked": now >= int(unlocks), "sweepableFrom": None, "to": dist, "data": data}
    try:
        delay = int(cast("call", "--rpc-url", RPC, dist, "sweepDelay()(uint64)").split()[0])
        opened = int(cast("call", "--rpc-url", RPC, dist, "openedAt(uint256)(uint64)", "0").split()[0])
        out["sweepableFrom"] = max(opened + delay, int(unlocks))
    except Exception:
        pass
    if claimed:
        out["simulation"] = "already claimed"
    elif not out["unlocked"]:
        out["simulation"] = "locked"
    else:
        try:
            cast("call", "--rpc-url", RPC, "--from", WALLET, dist, data)
            out["simulation"] = "ok"
            out["gas"] = int(cast("estimate", "--rpc-url", RPC, "--from", WALLET, dist, data))
        except subprocess.CalledProcessError as ex:
            out["simulation"] = "revert: " + ex.output.decode()[-160:]
    return out


def oz_check(repo):
    """Each vendored OpenZeppelin file must equal the official release named in its own header."""
    res = []
    base = repo / "lib" / "openzeppelin-contracts"
    if not base.exists():
        return res
    for f in sorted(base.rglob("*.sol")):
        rel = f.relative_to(base).as_posix()
        m = re.search(r"\(last updated (v\d+\.\d+\.\d+)\)", f.read_text(errors="ignore")[:300])
        tags = [m.group(1)] if m else []
        tags += ["v5.5.0", "v5.4.0", "v5.3.0", "v5.2.0", "v5.1.0", "v5.0.2", "v5.0.0", "master"]
        verdict = "DIFFERS from every checked release"
        for t in dict.fromkeys(tags):
            try:
                body = urllib.request.urlopen(f"https://raw.githubusercontent.com/OpenZeppelin/openzeppelin-contracts/{t}/{rel}", timeout=20).read()
            except Exception:
                continue
            if body == f.read_bytes():
                verdict = f"identical to {t}" + (" (unreleased development branch, genuine OZ code)" if t == "master" else "")
                break
        res.append({"file": rel, "verdict": verdict})
    return res


def contract_check(launch, token_addr, token_name, workdir):
    repo = workdir / f"launch-{launch['launchNumber']}"
    if not repo.exists():
        subprocess.run(["git", "clone", "-q", launch["sourceRepoUrl"], str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", launch["sourceCommit"]], check=True)
    subprocess.run(["git", "-C", str(repo), "submodule", "update", "-q", "--init", "--depth", "1"], capture_output=True)
    spec = json.loads((repo / "launch.json").read_text()) if (repo / "launch.json").exists() else {}
    srcs = [p for p in repo.rglob("*.sol") if not any(x in p.parts for x in ("lib", "test", "script", "node_modules", "out", "cache"))]
    flags = {}
    for p in srcs:
        text = p.read_text(errors="ignore")
        for name, pat in RISK_PATTERNS.items():
            hits = [i + 1 for i, line in enumerate(text.splitlines()) if re.search(pat, line, re.I) and not line.strip().startswith(("//", "*", "/*"))]
            if hits:
                flags.setdefault(name, []).append(f"{p.relative_to(repo)}:{','.join(map(str, hits[:8]))}")
    built = subprocess.run(["forge", "build", "-q"], cwd=repo, capture_output=True)
    contract = spec.get("token", {}).get("contract") or token_name
    match = None
    if built.returncode == 0:
        arts = list((repo / "out").rglob(f"{contract}.json"))
        if arts:
            art = json.loads(arts[0].read_text())
            loc = bytearray.fromhex(art["deployedBytecode"]["object"][2:])
            on = bytearray.fromhex(cast("code", "--rpc-url", RPC, token_addr)[2:])
            masked = 0
            for refs in (art["deployedBytecode"].get("immutableReferences") or {}).values():
                for r in refs:
                    s, l = r["start"], r["length"]
                    loc[s:s + l] = bytes(l)
                    on[s:s + l] = bytes(l)
                    masked += 1
            match = {"equal": loc == on, "bytes": len(on), "immutablesMasked": masked}
    return {"repo": str(repo), "spec": {k: spec.get(k) for k in ("token", "pool", "economics")},
            "notes": spec.get("notes", "")[:1200], "sources": [str(p.relative_to(repo)) for p in srcs],
            "sourceLines": sum(len(p.read_text(errors="ignore").splitlines()) for p in srcs),
            "riskFlags": flags, "bytecodeMatch": match, "build": built.returncode == 0,
            "openzeppelin": oz_check(repo)}


def pool_check(launch, token_addr, hook):
    """The launch's own v4 pool: key rebuilt from launch.json (paired currency, tick spacing) and the
    launch record (fee, hook), then slot0 and in-range liquidity read straight from the PoolManager."""
    other = ((launch.get("_spec_pool") or {}).get("pairedCurrency") or IMD).lower()  # 0x0 = native ETH
    c0, c1 = sorted([token_addr.lower(), other])
    tick_spacing = int((launch.get("_spec_pool") or {}).get("tickSpacing") or 60)
    fee = int(launch.get("poolFee") or 12500)
    pid = cast("keccak", cast("abi-encode", "f(address,address,uint24,int24,address)", c0, c1, str(fee), str(tick_spacing), hook))
    slot = int(cast("keccak", cast("abi-encode", "f(bytes32,uint256)", pid, "6")), 16)
    s0 = int(cast("call", "--rpc-url", RPC, POOL_MANAGER, "extsload(bytes32)(bytes32)", "0x%064x" % slot), 16)
    liq = int(cast("call", "--rpc-url", RPC, POOL_MANAGER, "extsload(bytes32)(bytes32)", "0x%064x" % (slot + 3)), 16) & ((1 << 128) - 1)
    sqrtp = s0 & ((1 << 160) - 1)
    tick = (s0 >> 160) & ((1 << 24) - 1)
    tick = tick - (1 << 24) if tick >= 1 << 23 else tick
    p01 = (sqrtp / 2 ** 96) ** 2 if sqrtp else 0.0
    other_per_token = (p01 if token_addr.lower() == c0 else (1 / p01 if p01 else 0.0))
    hook_bits = int(hook, 16) & 0x3FFF
    perms = [name for bit, name in HOOK_FLAGS if hook_bits >> bit & 1]
    return {"poolId": pid, "_other": other, "pairedWith": "ETH" if other.endswith("0" * 40) else ("IMD" if other == IMD else other),
            "fee": fee, "tickSpacing": tick_spacing, "hook": hook, "hookPermissions": perms,
            "initialized": sqrtp != 0, "tick": tick, "atMinOrMaxTick": abs(tick) >= 887200,
            "inRangeLiquidity": liq, "pairedPerToken": other_per_token}


def sell_quote(pool, token_addr, amount_raw):
    """What selling `amount_raw` of the token into the launch pool actually pays, from Uniswap's v4 Quoter.

    Launch pools are seeded one-sided with the new token, so the only IMD/ETH inside is what buyers have
    put in. Price x amount says nothing about that; this does. If the whole amount can't be filled
    (NotEnoughLiquidity), binary-search the largest amount that can be and report its proceeds."""
    c0, c1 = sorted([token_addr.lower(), pool["_other"]])
    zero_for_one = token_addr.lower() == c0
    key = f"(({c0},{c1},{pool['fee']},{pool['tickSpacing']},{pool['hook']}),{str(zero_for_one).lower()},{{}},0x)"

    def q(raw):
        try:
            out = cast("call", "--rpc-url", RPC, V4_QUOTER,
                       "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))(uint256,uint256)",
                       key.format(raw))
            return int(out.split()[0])
        except subprocess.CalledProcessError:
            return None

    full = q(amount_raw)
    if full is not None:
        return {"fillable": amount_raw, "fillPct": 100.0, "proceeds": full}
    lo, hi, best = 0, amount_raw, (0, 0)
    for _ in range(14):  # ~0.01% resolution
        mid = (lo + hi) // 2
        if mid == 0:
            break
        r = q(mid)
        if r is None:
            hi = mid
        else:
            lo, best = mid, (mid, r)
    return {"fillable": best[0], "fillPct": 100.0 * best[0] / amount_raw if amount_raw else 0.0, "proceeds": best[1]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include already-claimed allocations")
    ap.add_argument("--launch", type=int, nargs="*", help="only these launch numbers")
    ap.add_argument("--wallet", default=os.environ.get("IMD_WALLET"), help="seat owner wallet (or IMD_WALLET)")
    ap.add_argument("--out", default="imd-claims-reports", help="where to write the JSON report")
    ap.add_argument("--workdir", default=os.environ.get("CLAUDE_JOB_DIR", "/tmp") + "/imd-claims")
    a = ap.parse_args()
    global WALLET
    if not a.wallet or not re.fullmatch(r"0x[0-9a-fA-F]{40}", a.wallet):
        sys.exit("pass your seat's wallet: --wallet 0x... (or set IMD_WALLET)")
    WALLET = a.wallet.lower()
    workdir = Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    earnings = get(f"{API}/wallets/{WALLET}/earnings?limit=200")["earnings"]
    mainnet = [x for x in earnings if x["chainId"] == 1]
    print(f"{len(earnings)} allocations: {len(mainnet)} mainnet, {len(earnings) - len(mainnet)} testnet (no value, skipped)")
    if a.launch:
        mainnet = [x for x in mainnet if x["launchNumber"] in a.launch]

    eth_usd = int(cast("call", "--rpc-url", RPC, CHAINLINK_ETH_USD, "latestAnswer()(int256)").split()[0]) / 1e8
    gas_price = int(cast("gas-price", "--rpc-url", RPC))
    imd_pairs = get(f"https://api.dexscreener.com/latest/dex/tokens/{IMD}").get("pairs") or []
    imd_main = max((p for p in imd_pairs if p["baseToken"]["address"].lower() == IMD), key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
    imd_usd = float(imd_main["priceUsd"]) if imd_main else None
    ds = get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(x["token"]["address"] for x in mainnet)).get("pairs") or [] if mainnet else []

    rows = []
    for x in mainnet:
        n, sym, tok = x["launchNumber"], x["token"]["symbol"], x["token"]["address"].lower()
        print(f"\n== #{n} {sym} ({x['token']['name']}) {tok}\n   claim page: https://explorer.imd.fun/token/{tok}", flush=True)
        L = get(f"{API}/launches/{x['launchId']}?claims=1")
        arts = {a_["name"]: a_["address"].lower() for a_ in L["artifacts"]}
        dist = arts.get("MerkleDistributor")
        hook = arts.get("PoolInitializationGuard")
        token_name = next((k for k, v in arts.items() if v == tok), None)
        row = {"launch": n, "symbol": sym, "name": x["token"]["name"], "token": tok, "launchId": x["launchId"],
               "claimPage": f"https://explorer.imd.fun/token/{tok}",
               "requester": L.get("requester"), "economics": L.get("economics"), "artifacts": arts,
               "sourceRepoUrl": L.get("sourceRepoUrl"), "sourceCommit": L.get("sourceCommit")}
        try:
            row["claim"] = claim_check(L, dist)
        except Exception as ex:
            row["claim"] = {"error": str(ex)}
        if row["claim"].get("claimed") and not a.all:
            print("   already claimed, skipping")
            continue
        try:
            row["contract"] = contract_check(L, tok, token_name, workdir)
            L["_spec_pool"] = (row["contract"]["spec"] or {}).get("pool") or {}
        except Exception as ex:
            row["contract"] = {"error": str(ex)}
        try:
            row["pool"] = pool_check(L, tok, hook)
        except Exception as ex:
            row["pool"] = {"error": str(ex)}
        # DexScreener: the launch pool if listed, else the deepest pool for the token
        pairs = [p for p in ds if p["baseToken"]["address"].lower() == tok]
        own = next((p for p in pairs if p["pairAddress"].lower() == (row.get("pool") or {}).get("poolId", "").lower()), None)
        best = own or max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
        pool = row.get("pool") or {}
        unit = imd_usd if pool.get("pairedWith") == "IMD" else (eth_usd if pool.get("pairedWith") == "ETH" else None)
        price_onchain = pool.get("pairedPerToken", 0) * unit if unit else None
        amt = int(x["amount"]) / 10 ** x["token"]["decimals"]
        sq = None
        if pool.get("initialized") and unit and not row["claim"].get("error"):
            try:
                sq = sell_quote(pool, tok, int(x["amount"]))
                sq["proceedsPaired"] = sq["proceeds"] / 1e18
                sq["proceedsUsd"] = sq["proceedsPaired"] * unit
            except Exception as ex:
                sq = {"error": str(ex)}
        row["market"] = {
            "priceUsdOnchain": price_onchain,
            "marketCapUsd": price_onchain * 1e9 if price_onchain else None,
            "dexscreener": None if not best else {
                "pool": best["pairAddress"], "isLaunchPool": best is own, "priceUsd": best.get("priceUsd"),
                "liquidityUsd": (best.get("liquidity") or {}).get("usd"), "volume24h": (best.get("volume") or {}).get("h24"),
                "txns24h": (best.get("txns") or {}).get("h24"), "fdv": best.get("fdv"), "url": best.get("url"),
                "otherPools": len(pairs) - 1},
            "ourAmount": amt,
            "priceTimesAmountUsd": amt * price_onchain if price_onchain else None,
            "sellQuote": sq,
            # what a sale of our whole allocation would actually pay now (the part the pool can absorb)
            "ourValueUsd": (sq or {}).get("proceedsUsd") if sq and "error" not in sq else None,
            "claimGasUsd": (row["claim"].get("gas", 120000) * gas_price / 1e18 * eth_usd),
        }
        # What the user should care about. Drained: the paired side was sold out of the pool (price
        # pinned at a tick bound, or nothing in range after trading), so a sale pays nothing. Waiting:
        # a fresh launch nobody has bought into yet; it could still become worth something. Sellable:
        # a sale of our allocation pays something now.
        sold = (sq or {}).get("proceedsUsd") or 0
        traded = ((best or {}).get("volume") or {}).get("h24") or 0
        if pool.get("atMinOrMaxTick") or (not pool.get("inRangeLiquidity") and traded and sold == 0):
            row["status"] = "drained"
        elif sold > 0:
            row["status"] = "sellable"
        else:
            row["status"] = "waiting"
        rows.append(row)
        c, k, m = row["claim"], row.get("contract", {}), row["market"]
        bm = k.get("bytecodeMatch") or {}
        now = time.time()
        ua, sw = c.get("unlocksAt"), c.get("sweepableFrom")
        when = "unknown" if not ua else ("claimable now" if now >= ua else
               f"claimable in {int((ua - now) // 3600)}h{int((ua - now) % 3600 // 60):02d}m ({time.strftime('%H:%MZ', time.gmtime(ua))})")
        c["claimableIn"] = when
        print(f"   status: {row['status'].upper()}")
        print(f"   claim: {amt:,.0f} {sym} | {when} | claim by {time.strftime('%Y-%m-%d', time.gmtime(sw)) if sw else '?'} | root ok {c.get('rootVerified')} | claimed {c.get('claimed')} | {c.get('simulation')} | gas ~${m['claimGasUsd']:.2f}")
        print(f"   contract: {k.get('sourceLines')} lines, bytecode match {bm.get('equal')} ({bm.get('immutablesMasked', 0)} immutables masked), flags {list((k.get('riskFlags') or {}).keys()) or 'none'}")
        oz = [o for o in k.get("openzeppelin") or [] if not o["verdict"].startswith("identical")]
        if k.get("openzeppelin"):
            print(f"   openzeppelin: {len(k['openzeppelin'])} files, {'ALL identical to an official release' if not oz else str(len(oz)) + ' DIFFER'}")
        print(f"   pool: vs {pool.get('pairedWith')} hook perms {pool.get('hookPermissions')} tick {pool.get('tick')} in-range liquidity {pool.get('inRangeLiquidity')}{' AT MIN/MAX TICK' if pool.get('atMinOrMaxTick') else ''}")
        d = m["dexscreener"] or {}
        pv = m["priceUsdOnchain"]
        print(f"   market: price ${pv if pv is None else f'{pv:.10f}'} mcap ${(m['marketCapUsd'] or 0):,.0f} | ds liq ${d.get('liquidityUsd') or 0:,.0f} vol24 ${d.get('volume24h') or 0:,.0f} | price x ours ${(m['priceTimesAmountUsd'] or 0):,.2f}")
        if sq and "error" not in sq:
            print(f"   SALE QUOTE: selling all {amt:,.0f} pays {sq['proceedsPaired']:.6f} {pool.get('pairedWith')} (~${sq['proceedsUsd']:,.2f}); pool absorbs {sq['fillPct']:.1f}% of our allocation")
        elif sq:
            print(f"   sale quote failed: {sq['error'][-120:]}")

    REPORTS = Path(a.out)
    by = {k: [f"#{r['launch']} {r['symbol']}" for r in rows if r.get("status") == k] for k in ("sellable", "waiting", "drained")}
    print(f"\nSELLABLE: {', '.join(by['sellable']) or 'none'}")
    print(f"WAITING (no buyers yet): {', '.join(by['waiting']) or 'none'}")
    print(f"DRAINED (leave out of the summary): {', '.join(by['drained']) or 'none'}")
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    report = {"at": stamp, "wallet": WALLET, "ethUsd": eth_usd, "imdUsd": imd_usd, "gasPriceGwei": gas_price / 1e9, "rows": rows}
    path = REPORTS / f"scan-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"\nETH ${eth_usd:,.2f} | IMD ${imd_usd or 0:,.3f} | gas {gas_price / 1e9:.3f} gwei | report {path}")


if __name__ == "__main__":
    main()
