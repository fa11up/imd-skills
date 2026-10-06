#!/usr/bin/env python3
"""Build, verify and simulate our IMD launch-reward claims. Writes claims.json.

For each launch: fetch our leaf (amount + proof) from api.imd.fun, recompute the Merkle root from the
leaf the way MerkleDistributor does (OZ double-hash, sorted pairs) and compare with the root on chain,
then eth_call claim() from our wallet. A claim is only listed as ready if all three pass.
Usage: python3 build_claims.py --wallet 0xYourSeatWallet 737 741 747   (or set IMD_WALLET)
"""
import json, os, subprocess, sys, time, urllib.request

os.environ["FOUNDRY_DISABLE_NIGHTLY_WARNING"] = "1"
# RPC per chain IdentityMD launches on with value; keep in step with CHAINS in ../scan.py.
RPCS = {1: "https://ethereum-rpc.publicnode.com", 4663: "https://rpc.mainnet.chain.robinhood.com"}
args = sys.argv[1:]
W = os.environ.get("IMD_WALLET", "")
if args[:1] == ["--wallet"]:
    W, args = args[1], args[2:]
W = W.lower()
if len(W) != 42 or not W.startswith("0x"):
    raise SystemExit("pass your seat's wallet: --wallet 0x... (or set IMD_WALLET)")


def cast(*a):
    return subprocess.check_output(["cast", *a], stderr=subprocess.STDOUT).decode().strip()


def get(url):
    for i in range(5):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
        except Exception:
            time.sleep(3 * (i + 1))
    raise SystemExit(f"could not fetch {url}")


launches = {x["launchNumber"]: x for x in get(f"https://api.imd.fun/wallets/{W}/earnings?limit=200")["earnings"] if x["chainId"] in RPCS}
out = []
for n in map(int, args):
    if n not in launches:
        print(f"#{n}: no allocation on a supported chain for this wallet")
        continue
    chain_id = launches[n]["chainId"]
    R = RPCS[chain_id]
    d = get(f"https://api.imd.fun/launches/{launches[n]['launchId']}?claims=1")
    dist = next(a["address"] for a in d["artifacts"] if a["name"] == "MerkleDistributor")
    tok = next(a["address"] for a in d["artifacts"] if a["name"] not in ("MerkleDistributor", "PoolInitializationGuard"))
    sym = launches[n]["token"]["symbol"]
    leaf = next(l for l in d["claims"]["leaves"] if l["wallet"].lower() == W)
    amount, proof = int(leaf["amount"]), leaf["proof"]
    h = cast("keccak", cast("keccak", cast("abi-encode", "f(address,uint256)", W, str(amount))))
    for p in proof:
        a, b = sorted([h.lower(), p.lower()])
        h = cast("keccak", a + b[2:])
    root, funded, claimed_total, unlocks = [x.strip("() ").split(" ")[0] for x in cast("call", "--rpc-url", R, dist, "roundOf(uint256)((bytes32,uint256,uint256,uint64))", "0").split(",")]
    data = cast("calldata", "claim(uint256,address,uint256,bytes32[])", "0", W, str(amount), "[" + ",".join(proof) + "]")
    now = int(cast("block", "--rpc-url", R, "latest", "--field", "timestamp"))
    already = cast("call", "--rpc-url", R, dist, "claimed(uint256,address)(bool)", "0", W)
    if now < int(unlocks):
        sim = f"LOCKED until {time.strftime('%H:%M:%SZ', time.gmtime(int(unlocks)))}"
    else:
        try:
            cast("call", "--rpc-url", R, "--from", W, dist, data)
            sim = "OK"
        except subprocess.CalledProcessError as ex:
            sim = "REVERT " + ex.output.decode()[-200:]
    root_ok = h.lower() == root.lower()
    print(f"{sym:5} #{n} distributor {dist} amount {amount / 1e18:,.2f} | root matches chain: {root_ok} | already claimed: {already} | simulated: {sim}")
    out.append({"symbol": sym, "launch": n, "chainId": chain_id, "token": tok, "account": W, "to": dist, "data": data, "amount": str(amount),
                "proof": proof, "rootVerified": root_ok, "unlocksAt": int(unlocks), "simulation": sim})
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims.json"), "w"), indent=2)
