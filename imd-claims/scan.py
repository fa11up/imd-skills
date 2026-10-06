#!/usr/bin/env python3
"""Scan a seat's IdentityMD launch-reward allocations: what is claimable, is the contract what its
source says, and is there a market to sell into.

    python3 scan.py --wallet 0xYourSeatWallet [--all] [--launch N ...] [--out DIR] [--workdir DIR]
    (or set IMD_WALLET instead of --wallet)

For every allocation not yet claimed on a supported value chain (Ethereum, Robinhood Chain; Sepolia
tokens have no value and are only counted):
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
WALLET = None  # the seat owner's wallet, from --wallet or IMD_WALLET
NATIVE = "0x0000000000000000000000000000000000000000"
CHAINLINK_ETH_USD = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"  # mainnet; ETH is priced once for every chain
MAINNET_IMD = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"

# Chains IdentityMD launches on with real value. Addresses from the plane's deployment records
# (Identity-md/protocol packages/contracts/deployments/*.json) and its explorer's Uniswap table
# (apps/explorer/lib/uniswap.ts). Add a chain here when the plane opens launches on it.
CHAINS = {
    1: {
        "name": "Ethereum", "slug": "ethereum", "rpc": "https://ethereum-rpc.publicnode.com",
        "poolManager": "0x000000000004444c5dc75cB358380D2e3dE08A90",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
        "factory": "0xff03410d0fe5fa8f7f59f743de35e333d9857120",
        "guardHook": "0x784ff9a3ac5d88a30bfff6f7f2a270161fbe6000",
        "imd": MAINNET_IMD, "explorer": "https://etherscan.io", "blockscout": "https://eth.blockscout.com",
    },
    4663: {
        "name": "Robinhood Chain", "slug": "robinhood", "rpc": "https://rpc.mainnet.chain.robinhood.com",
        "poolManager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "factory": "0x9c9d2fcb75c2c132c0ac0c42df3819a42347265e",
        "guardHook": "0x19bec7c2e1b2aadaf67b259744751a9960d66000",
        "imd": "0x5f7bb59365ce557c26dbcaa4ee9d39a4b95b7127", "explorer": "https://robin.etherscan.io",
        "blockscout": "https://robinhoodchain.blockscout.com",  # Cloudflare-protected; other pools may not resolve here
    },
}
TESTNETS = {11155111}

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


def claim_check(ch, launch, dist):
    RPC = ch["rpc"]
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


def _canon(b):
    """Source with formatting removed: whitespace, and the _ separators solc allows inside hex"" literals."""
    b = re.sub(rb'hex"([0-9a-fA-F_]*)"', lambda m: b'hex"' + m.group(1).replace(b"_", b"") + b'"', b)
    return re.sub(rb"\s+", b"", b)


def oz_check(repo):
    """Each OpenZeppelin file the build actually compiled must equal an official release (or master), or
    differ from it only in formatting. Vendored files nothing compiles are ignored."""
    used = set()
    for f in (repo / "out").rglob("*.json"):
        try:
            md = json.loads(f.read_text()).get("metadata")
        except Exception:
            continue
        if isinstance(md, str):
            md = json.loads(md)
        for src in (md or {}).get("sources") or {}:
            if "openzeppelin-contracts/" in src:
                used.add(src.split("openzeppelin-contracts/", 1)[1])
    base = repo / "lib" / "openzeppelin-contracts"
    res = []
    for rel in sorted(used):
        f = base / rel
        if not f.exists():
            continue
        local = f.read_bytes()
        m = re.search(rb"\(last updated (v\d+\.\d+\.\d+)\)", local[:300])
        tags = ([m.group(1).decode()] if m else []) + ["v5.5.0", "v5.4.0", "v5.3.0", "v5.2.0", "v5.1.0", "v5.0.2", "v5.0.0", "master"]
        verdict = "DIFFERS from every checked release"
        fmt_match = None
        for t in dict.fromkeys(tags):
            try:
                body = urllib.request.urlopen(f"https://raw.githubusercontent.com/OpenZeppelin/openzeppelin-contracts/{t}/{rel}", timeout=20).read()
            except Exception:
                continue
            if body == local:
                verdict = f"identical to {t}" + (" (unreleased development branch, genuine OZ code)" if t == "master" else "")
                break
            if fmt_match is None and _canon(body) == _canon(local):
                fmt_match = t
        else:
            if fmt_match:
                verdict = f"identical to {fmt_match} apart from formatting"
        res.append({"file": rel, "verdict": verdict})
    return res


def contract_check(ch, launch, token_addr, token_name, workdir):
    RPC = ch["rpc"]
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


def pool_check(ch, launch, token_addr, hook):
    """The launch's own v4 pool: key rebuilt from launch.json (paired currency, tick spacing) and the
    launch record (fee, hook), then slot0 and in-range liquidity read straight from the PoolManager."""
    RPC, POOL_MANAGER = ch["rpc"], ch["poolManager"]
    other = ((launch.get("_spec_pool") or {}).get("pairedCurrency") or ch["imd"]).lower()  # 0x0 = native ETH
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
    return {"poolId": pid, "_other": other, "pairedWith": "ETH" if other == NATIVE else ("IMD" if other == ch["imd"] else other),
            "fee": fee, "tickSpacing": tick_spacing, "hook": hook, "hookPermissions": perms,
            "initialized": sqrtp != 0, "tick": tick, "atMinOrMaxTick": abs(tick) >= 887200,
            "inRangeLiquidity": liq, "pairedPerToken": other_per_token}


def sell_quote(ch, pool, token_addr, amount_raw):
    """What selling `amount_raw` of the token into the launch pool actually pays, from Uniswap's v4 Quoter.

    Launch pools are seeded one-sided with the new token, so the only IMD/ETH inside is what buyers have
    put in. Price x amount says nothing about that; this does. If the whole amount can't be filled
    (NotEnoughLiquidity), binary-search the largest amount that can be and report its proceeds."""
    c0, c1 = sorted([token_addr.lower(), pool["_other"]])
    zero_for_one = token_addr.lower() == c0
    key = f"(({c0},{c1},{pool['fee']},{pool['tickSpacing']},{pool['hook']}),{str(zero_for_one).lower()},{{}},0x)"

    def q(raw):
        try:
            out = cast("call", "--rpc-url", ch["rpc"], ch["quoter"],
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


INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"


def pool_key_from_log(ch, pool_id):
    """A v4 pool's key (currencies, fee, tick spacing, hook) from its Initialize event, via Blockscout's
    logs API (indexed by poolId, so no block range is needed). None if it can't be read."""
    url = (f"{ch['blockscout']}/api?module=logs&action=getLogs&fromBlock=0&toBlock=latest&address={ch['poolManager']}"
           f"&topic0={INITIALIZE_TOPIC}&topic1={pool_id}&topic0_1_opr=and")
    r = None
    for i in range(4):  # Blockscout rate-limits bursts with a non-list "result"
        try:
            r = get(url).get("result")
        except Exception:
            r = None
        if isinstance(r, list):
            break
        time.sleep(2 * (i + 1))
    if not isinstance(r, list) or not r:
        return None
    l = r[0]
    w = [l["data"][2 + i:2 + i + 64] for i in range(0, len(l["data"]) - 2, 64)]
    return {"currency0": "0x" + l["topics"][2][-40:], "currency1": "0x" + l["topics"][3][-40:],
            "fee": int(w[0], 16), "tickSpacing": int(w[1], 16) - (1 << 256 if int(w[1], 16) >= 1 << 255 else 0),
            "hook": "0x" + w[2][-40:]}


STABLES = {"USDC", "USDT", "DAI", "USDS", "USDE", "PYUSD", "FRAX", "LUSD", "USD0", "RLUSD"}


def paired_decimals(ch, addr, cache):
    addr = addr.lower()
    if addr == NATIVE:
        return 18
    key = ("dec", ch["slug"], addr)
    if key not in cache:
        try:
            cache[key] = int(cast("call", "--rpc-url", ch["rpc"], addr, "decimals()(uint8)").split()[0])
        except Exception:
            cache[key] = 18
    return cache[key]


def paired_usd(ch, addr, eth_usd, cache):
    """USD per whole paired token: ETH from Chainlink; any ERC-20 from its own deepest DexScreener pool on
    that chain; a chain's bridged IMD falls back to mainnet IMD if it has no pool of its own."""
    addr = addr.lower()
    if addr == NATIVE:
        return eth_usd, "ETH (Chainlink)"
    key = (ch["slug"], addr)
    if key not in cache:
        pairs = get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}").get("pairs") or []
        mine = [p for p in pairs if p["baseToken"]["address"].lower() == addr and p.get("chainId") == ch["slug"] and p.get("priceUsd")]
        best = max(mine, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
        if best:
            cache[key] = (float(best["priceUsd"]), f"{best['baseToken']['symbol']} (DexScreener, {ch['name']})")
        elif any(p_["quoteToken"]["address"].lower() == addr and p_["quoteToken"]["symbol"].upper() in STABLES for p_ in pairs) or \
                (lambda sym: sym.upper() in STABLES)(next((p_["quoteToken"]["symbol"] for p_ in pairs if p_["quoteToken"]["address"].lower() == addr), "")):
            sym_ = next(p_["quoteToken"]["symbol"] for p_ in pairs if p_["quoteToken"]["address"].lower() == addr)
            cache[key] = (1.0, f"{sym_} (dollar stablecoin, taken as $1)")
        elif addr == ch["imd"] and addr != MAINNET_IMD:
            usd, _ = paired_usd(CHAINS[1], MAINNET_IMD, eth_usd, cache)
            cache[key] = (usd, "IMD (no local pool: priced at mainnet IMD, an assumption)")
        else:
            cache[key] = (None, "unpriced")
    return cache[key]


def discover(eth_usd_unused=None):
    """Every allocation for WALLET on a supported chain, from two sources merged by launch number:
    the wallet earnings index (fast, but an index), and a sweep of every live launch on those chains
    whose frozen claim tree names WALLET (the source of truth: catches anything the index misses or lags).
    Returns (allocations, launch_details_by_id) so details aren't fetched twice."""
    allocs = {x["launchNumber"]: x for x in get(f"{API}/wallets/{WALLET}/earnings?limit=200")["earnings"]}
    details = {}
    launches, before = [], None
    while True:
        page = get(f"{API}/launches?limit=500" + (f"&before={before}" if before else ""))["launches"]
        launches += page
        if len(page) < 500:
            break
        before = min(l["launchNumber"] for l in page)
    live = [l for l in launches if l.get("chainId") in CHAINS and l.get("status") == "live"]
    for l in live:
        if not any(a.get("role") == "distributor" or a.get("name") == "MerkleDistributor" for a in l.get("artifacts") or []):
            continue  # contracts-only launches pay no token
        d = get(f"{API}/launches/{l['id']}?claims=1")
        details[l["id"]] = d
        leaf = next((x for x in ((d.get("claims") or {}).get("leaves") or []) if x["wallet"].lower() == WALLET), None)
        if not leaf or l["launchNumber"] in allocs:
            continue
        tok = next(a["address"].lower() for a in d["artifacts"] if a.get("role") == "token")
        ch = CHAINS[l["chainId"]]
        sym = cast("call", "--rpc-url", ch["rpc"], tok, "symbol()(string)").strip('"')
        name = cast("call", "--rpc-url", ch["rpc"], tok, "name()(string)").strip('"')
        dec = int(cast("call", "--rpc-url", ch["rpc"], tok, "decimals()(uint8)").split()[0])
        allocs[l["launchNumber"]] = {"launchId": l["id"], "launchNumber": l["launchNumber"], "chainId": l["chainId"],
                                     "kind": l.get("kind"), "amount": leaf["amount"], "_fromClaimTree": True,
                                     "token": {"address": tok, "symbol": sym, "name": name, "decimals": dec}}
    return list(allocs.values()), details


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

    earnings, details = discover()
    extra = [x for x in earnings if x.get("_fromClaimTree")]
    if extra:
        print(f"found {len(extra)} allocation(s) in claim trees that the earnings index doesn't list: "
              + ", ".join(f"#{x['launchNumber']} {x['token']['symbol']}" for x in extra))
    valued = [x for x in earnings if x["chainId"] in CHAINS]
    unknown = sorted({x["chainId"] for x in earnings if x["chainId"] not in CHAINS and x["chainId"] not in TESTNETS})
    per = ", ".join(f"{sum(1 for x in valued if x['chainId'] == c)} {CHAINS[c]['name']}" for c in CHAINS)
    print(f"{len(earnings)} allocations: {per}, {sum(1 for x in earnings if x['chainId'] in TESTNETS)} testnet (no value, skipped)")
    if unknown:
        print(f"WARNING: allocations on chains this scanner doesn't know yet: {unknown}. Add them to CHAINS.")
    if a.launch:
        valued = [x for x in valued if x["launchNumber"] in a.launch]

    eth_usd = int(cast("call", "--rpc-url", CHAINS[1]["rpc"], CHAINLINK_ETH_USD, "latestAnswer()(int256)").split()[0]) / 1e8
    gas_price = {c: int(cast("gas-price", "--rpc-url", CHAINS[c]["rpc"])) for c in {x["chainId"] for x in valued} | {1}}
    price_cache = {}
    imd_usd, _ = paired_usd(CHAINS[1], MAINNET_IMD, eth_usd, price_cache)
    ds = []
    addrs = [x["token"]["address"] for x in valued]
    for i in range(0, len(addrs), 30):  # DexScreener takes up to 30 addresses per call
        ds += get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(addrs[i:i + 30])).get("pairs") or []

    rows = []
    for x in valued:
        n, sym, tok = x["launchNumber"], x["token"]["symbol"], x["token"]["address"].lower()
        ch = CHAINS[x["chainId"]]
        print(f"\n== #{n} {sym} ({x['token']['name']}) {tok} [{ch['name']}]\n   claim page: https://explorer.imd.fun/token/{tok}", flush=True)
        L = details.get(x["launchId"]) or get(f"{API}/launches/{x['launchId']}?claims=1")
        arts = {a_["name"]: a_["address"].lower() for a_ in L["artifacts"]}
        role = {a_.get("role"): a_["address"].lower() for a_ in L["artifacts"] if a_.get("role")}
        # By role, not name: a univ4_hook launch's pool uses its OWN hook, not the platform guard.
        dist = role.get("distributor") or arts.get("MerkleDistributor")
        hook = role.get("hook") or arts.get("PoolInitializationGuard")
        token_name = next((k for k, v in arts.items() if v == tok), None)
        row = {"launch": n, "symbol": sym, "name": x["token"]["name"], "token": tok, "launchId": x["launchId"],
               "kind": L.get("kind"), "hook": hook, "platformGuard": hook == ch["guardHook"],
               "chainId": x["chainId"], "chain": ch["name"], "explorer": f"{ch['explorer']}/token/{tok}",
               "claimPage": f"https://explorer.imd.fun/token/{tok}",
               "requester": L.get("requester"), "economics": L.get("economics"), "artifacts": arts,
               "sourceRepoUrl": L.get("sourceRepoUrl"), "sourceCommit": L.get("sourceCommit")}
        try:
            row["claim"] = claim_check(ch, L, dist)
        except Exception as ex:
            row["claim"] = {"error": str(ex)}
        if row["claim"].get("claimed") and not a.all:
            print("   already claimed, skipping")
            continue
        try:
            row["contract"] = contract_check(ch, L, tok, token_name, workdir)
            L["_spec_pool"] = (row["contract"]["spec"] or {}).get("pool") or {}
        except Exception as ex:
            row["contract"] = {"error": str(ex)}
        try:
            row["pool"] = pool_check(ch, L, tok, hook or ch["guardHook"])
        except Exception as ex:
            row["pool"] = {"error": str(ex)}
        # DexScreener: the launch pool if listed, else the deepest pool for the token
        pairs = [p for p in ds if p["baseToken"]["address"].lower() == tok and p.get("chainId") == ch["slug"]]
        own = next((p for p in pairs if p["pairAddress"].lower() == (row.get("pool") or {}).get("poolId", "").lower()), None)
        best = own or max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
        pool = row.get("pool") or {}
        unit, unit_src = paired_usd(ch, pool.get("_other") or NATIVE, eth_usd, price_cache) if pool.get("_other") is not None else (None, "unpriced")
        decimals_paired = paired_decimals(ch, pool.get("_other") or NATIVE, price_cache)
        price_onchain = pool.get("pairedPerToken", 0) * unit if unit else None
        amt = int(x["amount"]) / 10 ** x["token"]["decimals"]
        sq = None
        if pool.get("initialized") and unit and not row["claim"].get("error"):
            try:
                sq = sell_quote(ch, pool, tok, int(x["amount"]))
                sq["proceedsPaired"] = sq["proceeds"] / 10 ** decimals_paired
                sq["proceedsUsd"] = sq["proceedsPaired"] * unit
            except Exception as ex:
                sq = {"error": str(ex)}
        # Other Uniswap v4 pools of the token on this chain (people open their own after a launch pool
        # drains): resolve each key from its Initialize log and quote our whole allocation there too.
        others = []
        launch_pid = (pool.get("poolId") or "").lower()
        for p in pairs:
            if "v4" not in (p.get("labels") or []) or p["pairAddress"].lower() == launch_pid:
                continue
            key = pool_key_from_log(ch, p["pairAddress"])
            if not key:
                others.append({"poolId": p["pairAddress"], "error": "pool key not resolvable"})
                continue
            other_cur = key["currency1"] if key["currency0"].lower() == tok else key["currency0"]
            o_unit, o_src = paired_usd(ch, other_cur, eth_usd, price_cache)
            alt = {"_other": other_cur.lower(), "fee": key["fee"], "tickSpacing": key["tickSpacing"], "hook": key["hook"]}
            entry = {"poolId": p["pairAddress"], "pairedWith": p["quoteToken"]["symbol"], "fee": key["fee"],
                     "tickSpacing": key["tickSpacing"], "hook": key["hook"],
                     "hookPermissions": [n_ for b_, n_ in HOOK_FLAGS if (int(key["hook"], 16) & 0x3FFF) >> b_ & 1]}
            if o_unit:
                try:
                    q_ = sell_quote(ch, alt, tok, int(x["amount"]))
                    q_["proceedsPaired"] = q_["proceeds"] / 10 ** paired_decimals(ch, other_cur, price_cache)
                    q_["proceedsUsd"] = q_["proceedsPaired"] * o_unit
                    entry["sellQuote"] = q_
                except Exception as ex:
                    entry["error"] = str(ex)[-160:]
            others.append(entry)
        best_other = max((o for o in others if (o.get("sellQuote") or {}).get("proceedsUsd")),
                         key=lambda o: o["sellQuote"]["proceedsUsd"], default=None)
        if best_other and best_other["sellQuote"]["proceedsUsd"] > ((sq or {}).get("proceedsUsd") or 0):
            best_route = {"pool": "other", **best_other}
        else:
            best_route = {"pool": "launch", "poolId": pool.get("poolId"), "sellQuote": sq}
        row["market"] = {
            "otherPools": others,
            "bestRoute": best_route,
            "pairedPriceSource": unit_src,
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
            # the best single pool to sell our whole allocation into right now
            "ourValueUsd": ((best_route.get("sellQuote") or {}).get("proceedsUsd")),
            # L2s (Robinhood Chain is an Arbitrum chain) also charge for L1 data, which this omits.
            "claimGasUsd": (row["claim"].get("gas", 120000) * gas_price[x["chainId"]] / 1e18 * eth_usd),
        }
        # What the user should care about. Drained: the paired side was sold out of the pool (price
        # pinned at a tick bound, or nothing in range after trading), so a sale pays nothing. Waiting:
        # a fresh launch nobody has bought into yet; it could still become worth something. Sellable:
        # a sale of our allocation pays something now.
        sold = row["market"]["ourValueUsd"] or 0
        traded = ((best or {}).get("volume") or {}).get("h24") or 0
        # One-sided launch pools keep a token-only range after their IMD/ETH side is sold out, so in-range
        # liquidity alone can't tell drained from fresh: a pool that has traded and now pays nothing is drained.
        if pool.get("atMinOrMaxTick") or (traded and sold == 0):
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
        oz = [o for o in k.get("openzeppelin") or [] if o["verdict"].startswith("DIFFERS")]
        if k.get("openzeppelin"):
            print(f"   openzeppelin: {len(k['openzeppelin'])} compiled files, {'all genuine (official release or formatting-only)' if not oz else str(len(oz)) + ' DIFFER: ' + ', '.join(o['file'] for o in oz)}")
        if hook and hook != ch["guardHook"]:
            print(f"   CUSTOM HOOK {hook} ({next((k for k, v in arts.items() if v == hook), '?')}): read its source before any verdict")
        print(f"   pool: vs {pool.get('pairedWith')} hook perms {pool.get('hookPermissions')} tick {pool.get('tick')} in-range liquidity {pool.get('inRangeLiquidity')}{' AT MIN/MAX TICK' if pool.get('atMinOrMaxTick') else ''}")
        d = m["dexscreener"] or {}
        pv = m["priceUsdOnchain"]
        print(f"   market: price ${pv if pv is None else f'{pv:.10f}'} mcap ${(m['marketCapUsd'] or 0):,.0f} | ds liq ${d.get('liquidityUsd') or 0:,.0f} vol24 ${d.get('volume24h') or 0:,.0f} | price x ours ${(m['priceTimesAmountUsd'] or 0):,.2f}")
        if sq and "error" not in sq:
            print(f"   SALE QUOTE: selling all {amt:,.0f} pays {sq['proceedsPaired']:.6f} {pool.get('pairedWith')} (~${sq['proceedsUsd']:,.2f}, {unit_src}); pool absorbs {sq['fillPct']:.1f}% of our allocation")
        elif sq:
            print(f"   sale quote failed: {sq['error'][-120:]}")
        for o in others:
            oq = o.get("sellQuote") or {}
            hk = "no hook" if int(o.get("hook", "0x0"), 16) == 0 else f"hook {o.get('hook')} {o.get('hookPermissions')}"
            print(f"   OTHER POOL {o['poolId'][:12]}… vs {o.get('pairedWith')} fee {o.get('fee')} {hk}: " +
                  (f"pays ~${oq.get('proceedsUsd', 0):,.2f} ({oq.get('fillPct', 0):.1f}% fill)" if oq else o.get("error", "unpriced")))
        if best_route.get("pool") == "other":
            print(f"   BEST ROUTE: the other pool {best_route['poolId'][:12]}… (~${row['market']['ourValueUsd']:,.2f})")

    REPORTS = Path(a.out)
    by = {k: [f"#{r['launch']} {r['symbol']}" + ("" if r["chainId"] == 1 else f" ({r['chain']})") for r in rows if r.get("status") == k]
          for k in ("sellable", "waiting", "drained")}
    print(f"\nSELLABLE: {', '.join(by['sellable']) or 'none'}")
    print(f"WAITING (no buyers yet): {', '.join(by['waiting']) or 'none'}")
    print(f"DRAINED (leave out of the summary): {', '.join(by['drained']) or 'none'}")
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    report = {"at": stamp, "wallet": WALLET, "ethUsd": eth_usd, "imdUsd": imd_usd,
              "gasPriceGwei": {CHAINS[c]["name"]: g / 1e9 for c, g in gas_price.items()}, "rows": rows}
    path = REPORTS / f"scan-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    gas = ", ".join(f"{CHAINS[c]['name']} {g / 1e9:.3f}" for c, g in gas_price.items())
    print(f"\nETH ${eth_usd:,.2f} | IMD ${imd_usd or 0:,.3f} | gas (gwei) {gas} | report {path}")


if __name__ == "__main__":
    main()
