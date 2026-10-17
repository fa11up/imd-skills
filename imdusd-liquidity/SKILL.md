---
name: imdusd-liquidity
description: Manage imdUSD liquidity on Ethereum. Reads the imdUSD/USDC peg pool (price, fee now, depth, cost to peg, where liquidity sits, redemption value), values a wallet's LP positions (fees owed, Season 1 points rate), plans deposits for a range, finds every venue that lists imdUSD, recommends an LP strategy, and builds simulated, unsigned transactions to mint, add, remove, collect or close. Use when the user asks about imdUSD liquidity, the peg pool, LP positions or ranges, where to provide imdUSD liquidity, LP yield or points, defending the peg, or whether to add or pull liquidity.
---

# imdUSD liquidity: read, plan, recommend, build

imdUSD is a CDP stablecoin on Ethereum. Its liquidity lives mainly in one **Uniswap v4 imdUSD/USDC pool
with a PegFeeHook**: LPs earn a 0.01% fee, and a swap that pushes the price away from $1 and ends outside
±0.25% pays a surcharge (up to 5% total) that goes to the Treasury, not to LPs. That pool is also the
**only** place liquidity earns Season 1 points: 3 points per dollar of depth per day, and only while the
price sits inside $0.99–$1.01.

This skill reads that pool exactly (tick by tick, from the chain), compares it with every other venue,
and turns a goal ("earn on 2k USDC", "defend the peg", "pull my LP") into a plan and transactions the
user signs themselves.

**Never ask for or handle a private key or seed phrase.** `tx` builds and simulates; the user signs with
their own wallet, a hardware wallet (`cast send … --ledger`) or a Safe (`--safe batch.json`).

**Requirements:** Python 3.9+, network access to a public Ethereum RPC, Blockscout and DexScreener.
`tx` also needs Foundry's `cast` for ABI encoding. Paths below are relative to this skill's folder
(`.claude/skills/imdusd-liquidity/`). Set `RPC_URLS=url1,url2` or pass `--rpc` to use your own node.
Add `--json` to any command for machine-readable output.

## 1. Read the pool

```bash
python3 .claude/skills/imdusd-liquidity/liquidity.py status
```

It reports:

- the **price**, its distance from $1, and whether it is inside the points band;
- the **fee now** each way: selling imdUSD below the peg pays the surcharge, buying back costs 0.01%;
- **depth**: the USDC it takes to push the price up, and the imdUSD to push it down, by 0.25/0.5/1/2%;
- **cost to peg**: the USDC (or imdUSD) to walk the price to each level up to $1. Swaps toward the peg
  pay no surcharge;
- **liquidity by price**: what LPs hold in each price bucket. USDC below the price is the bid that
  catches sellers. imdUSD above it is supply that buyers must absorb on the way back to $1;
- 24h volume and LP fees, the hook's unswept surcharge, and **redemption**: backing, fee, payout price
  against market, whether the feeds are fresh, and what a redeemed imdUSD is worth after the exit swap.

## 2. Value positions

```bash
python3 .claude/skills/imdusd-liquidity/liquidity.py positions --wallet 0xTheirWallet   # or IMD_WALLET=0x…
```

This lists each PositionManager NFT the wallet holds in the peg pool, with its range, current holdings,
fees owed, Season 1 band depth and points per day now. It also says whether the position is earning,
in range but outside the band, or out of range. Positions come from Blockscout; pass `--ids N …` if it
lags. A position held by a Safe is found the same way (pass the Safe's address).

## 3. Find the avenues

```bash
python3 .claude/skills/imdusd-liquidity/liquidity.py avenues
```

This merges DexScreener and GeckoTerminal for every pool holding imdUSD (liquidity, 24h volume, fee tier,
fee APR on TVL, points) and checks DefiLlama yields and Merkl for incentive campaigns. Then it lists the
protocol's own routes, which need no pool. A pool that is not the peg pool earns **no points** and is
usually thin. Say so, and say whose pool it is if known. Anyone can open a v4 pool, so a second
imdUSD/USDC pool at a high fee tier can exist; it is not the protocol's.

## 4. Plan, then recommend

```bash
python3 .claude/skills/imdusd-liquidity/liquidity.py plan --usdc 1000 --imdusd 1000 --range peg
python3 .claude/skills/imdusd-liquidity/liquidity.py plan --usdc 1000 --range 0.95:0.99
```

`--range` is `peg` ($0.99–$1.01, the points band), `wide` ($0.9524–$1.05) or `LOW:HIGH` in dollars.
For that range the plan shows:

- how much of each token it would take, and the leftover;
- the share of active liquidity it would hold, and its fees per day at the current volume;
- its points per day while at peg, against simply holding the imdUSD;
- what it ends up holding at each edge of the range.

A range entirely below the price takes USDC only (a bid wall). A range entirely above it takes imdUSD
only.

Read `references/strategies.md` before recommending. It holds the strategies, the scoring rules that
change which one wins, and the traps. Pick from the user's goal (yield, points, peg defence, exit) and
from what `status` shows **now**. A strategy that is right at peg is wrong in a depeg. Give one
recommendation with its numbers: amounts, range, expected fees and points per day, and what happens if
the price moves through the range. Then say what would change your mind.

## 5. Build the transactions

```bash
L=.claude/skills/imdusd-liquidity/liquidity.py
python3 $L tx mint     --owner 0xSigner --usdc 1000 --imdusd 1000 --range peg
python3 $L tx increase --owner 0xSigner --id 123456 --usdc 500        # same range as the position
python3 $L tx collect  --owner 0xSigner --id 123456                   # fees only; points stay safe
python3 $L tx remove   --owner 0xSigner --id 123456 --pct 25
python3 $L tx close    --owner 0xSigner --id 123456                   # everything + burn the NFT
python3 $L tx sweep    --owner 0xAnyone                               # hook surcharge -> Treasury (public good)
```

Each command prints the transactions in order. That includes the ERC-20 → Permit2 and Permit2 →
PositionManager approvals the owner still lacks, sized exactly to the deposit with a 0.5% margin. The
whole bundle is simulated from the owner at the head block with `eth_simulateV1`, and each step's result
is printed.

- **Present the result.** Show the user the labels and simulation results, never just the hex. If any
  step does not simulate `ok`, stop and explain it. The usual cause is balances, which the tool notes
  before simulating.
- **Signing.** `--safe file.json` writes a Safe Transaction Builder batch. `--json` prints each step's
  `to` and `data` for `cast send <to> <data> --rpc-url <rpc> --ledger` or any wallet.
- **Deadline.** The deadline is 30 minutes by default; a Safe collecting signatures needs `--deadline-hours 24`. Rebuild after it, or after the price moves.
- **Ranges.** For the peg band or the wide range, the imdusd.com terminal's *imdUSD liquidity* window
  does the same with its own UI. Custom ranges need `tx`.

Never send anything yourself, and never suggest pasting a key into a command.

## Facts worth having in hand

| | |
|---|---|
| Peg pool | id `0x3cb8cc9c22f7da5c3169d4059131ddba9e61f68230977797c03a2f1214992035`; imdUSD `0x61aA…4a25` (currency0, 18 dec), USDC (currency1, 6 dec), fee 100 (0.01%), tickSpacing 1, hook `0x33cA…6044` |
| Ticks | $ per imdUSD = 1.0001^tick × 1e12. $1 is tick −276325. Points band [−276424, −276224) = $0.99005–$1.01005 |
| Hook fee | 0.01% inside $0.9975–$1.0025. Moving away from $1: ~0.72% ending at $0.995 or $1.005, ~2.15% at $0.99 or $1.01, 5% at or beyond ±2%. Priced from the END price. The surcharge goes to the Treasury via `sweep()` (anyone) |
| Season 1 points | Holding 1x (wallets only: EOAs, 7702, Safes). Peg-pool LP 3x on band depth `L × (√min(hi,$1.01) − √max(lo,$0.99))`, only in blocks where the price is in the band AND the position's range. Liquidators get 3 days of holding points per imdUSD repaid |
| Hold rule | LP points are pending for 7,200 blocks (~1 day) after a position's LAST addition. Removing any liquidity inside that window forfeits all pending points. Collecting fees removes none. Points still pending at the season's end do not count |
| Redemption | `cash(amount, minGemOut, candidate)` pays sIMD at `max(attested, paced)` price: the paced price falls ≤1%/hour, so after a fall redemption pays well under market. Fee 0.5–5%. Needs fresh, agreeing feeds |
| Contracts | PositionManager `0xbD21…ee9e`, StateView `0x7fFE…7227`, Permit2 `0x0000…78BA3`, Quoter `0x52F0…1203`, vault `0x0b69…EF82` |
