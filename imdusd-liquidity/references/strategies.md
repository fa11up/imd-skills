# imdUSD LP strategies

Read `status` first. The right strategy depends on where the price is **now**, how much liquidity sits
on each side of it, and whether redemption works (fresh feeds). Every number below follows from the
pool's rules. Where it is an estimate, it says so.

## How the payoffs work

**Fees.** LPs earn 0.01% of the volume that trades through their range while it is active. A position's
share is its liquidity `L` over the pool's active `L` at the current tick. At the peg most volume is
small arbitrage, so fees are modest. In a depeg, volume spikes (sellers, then buyers), but the active
liquidity at the depegged price is often tiny. A fee APR computed from that hour is real for whoever is
there, and it is not a forecast.

**The surcharge is not yours.** A swap pushing the price away from $1 past ±0.25% pays up to 5%. That
goes to the Treasury through the hook's `sweep()`, not to LPs. It makes the peg expensive to attack.
It does not make LPing more profitable.

**Points (Season 1).** Liquidity earns 3 points per dollar of **band depth** per day. Band depth is
`L × (√min(upper, $1.01) − √max(lower, $0.99))`: the USDC the position would hold across the part of
its range inside $0.99–$1.01. It earns only in blocks where the pool price is inside the band **and**
inside the position's own range. Holding imdUSD in a wallet earns 1 point per imdUSD per day. imdUSD
inside an LP position is held by the PoolManager, so it earns the 3x instead, not both.

Consequences that are easy to get wrong:

1. **Inside the band, points scale with capital, not with narrowness.** For a range fully inside
   $0.99–$1.01, band depth is about the position's dollar value, however narrow the range.
   - A $1,000 position over $0.999–$1.001 earns the same ~3,000 points/day as $1,000 over the full band,
     while it is in range.
   - Narrow buys more fee share and more peg defence per dollar. It costs time out of range: the moment
     the price leaves $0.999–$1.001 it earns nothing, even inside the band.
2. **A wide range earns points only on its band slice.**
   - $0.9524–$1.05 puts ~20.5% of its depth inside the band, so it earns ~0.6 points per dollar per day
     at peg against 3 for a band position.
   - What it buys is staying in range through a depeg to $0.95. There it keeps earning fees and keeps
     acting as a bid.
3. **Outside $0.99–$1.01, no position earns points at all.**
4. **The hold rule punishes fiddling.** Points are pending for 7,200 blocks (~1 day) after a position's
   last addition.
   - Removing any liquidity inside that window forfeits everything pending on that position.
   - An addition restarts the window for all of it.
   - So:
     - **collect fees freely** (a zero-liquidity decrease removes nothing);
     - **put new capital in a new position** rather than topping up one with a day of pending points;
     - rebalance at most once a day, and only after the window has passed;
     - never add in the season's final day (pending at the end does not count).

**Inventory.** A peg-pool LP is a bet that imdUSD returns to $1.
- Below your range you hold only imdUSD, bought with your USDC on the way down at an average of
  `√(lower × upper)` of the part crossed.
- Above it you hold only USDC.
- In a depeg that does not recover, the LP is the one left holding imdUSD. The loss is the depeg itself,
  not "impermanent loss" in the usual sense.

## Strategies

### A. Peg band, $0.99–$1.01: the default at peg

- **When.** The price is inside the band and the user wants points first, fees second.
- **How.** `plan --usdc X --imdusd Y --range peg`. At $1 it takes roughly equal value of both.
- **Pays.** 3 points per dollar per day while in band, plus a fee share.
- **Risk.** Below $0.99 it becomes all imdUSD and earns nothing until the price is back in the band.
- **Size it to what the user would hold through a depeg.**

### B. Narrow core, e.g. $0.9975–$1.0025: fees and defence

- **When.** The pool has been sitting near $1 and the user wants more fee share for the same capital.
- **Pays.** The same points per dollar as A while in range, and a larger fee share (`plan` shows it).
- **Trade-off.** More time out of range. It pairs well with A: a core position plus a band position.

### C. Wide backstop, $0.9524–$1.05 (or wider)

- **When.** The user values staying active through stress over points.
- **Pays.** About a fifth of A's points per dollar at peg. Fees, and an active bid, down to $0.95.
- This is what makes a depeg shallow for everyone. A protocol-aligned LP or the Treasury should hold some.

### D. Bid wall: USDC only, entirely below the price

- **When.** The user is willing to buy imdUSD below $1, and wants to be paid for waiting (no swap now).
- **How.** For example `--range 0.97:0.995` at peg, or a band just under a depegged price
  (`plan` suggests one).
- **Pays.** Nothing until the price falls into it. Then it buys imdUSD on the way down and earns the
  fee on those trades. The upside is the discount if imdUSD returns to $1.
- **Compare with a swap.** Buying directly toward the peg pays only 0.01%, with no surcharge, and is
  immediate. A wall is a limit order. A swap is a market order.

### E. Ask wall: imdUSD only, entirely above the price

- **When.** The user holds imdUSD bought below $1 and wants to sell it back near $1 automatically, with
  fees on the way up.
- **How.** In a depeg, the band itself (`--range peg`) is an ask wall. It takes imdUSD only.
  - It starts earning points the moment the price re-enters $0.99, and the fee on every buyer through
    it.
  - It ends as USDC at ~$1.

### F. Depeg playbook: price under $0.99

Run `status` and read four things:
1. **Bids below the price** ("liquidity by price"). Almost no USDC means the next seller moves it a lot.
2. **Cost to peg.** The USDC it takes to walk the price back to each level.
3. **Redemption.** Whether `cash()` beats the pool after the exit swap. After a fall it usually does
   not: the payout price falls at most 1% an hour, so it sits above market for hours.
4. **Who is selling.** If the user has an ops view, check whether one wallet is drawing and dumping.

Then the options, roughly in order of risk:

1. **Borrowers: buy and wipe.** Anyone with vault debt gains `debt × (1 − price)` by buying imdUSD in
   the pool and calling `wipe()`, which burns it against their debt at par. It also lifts the price.
   This is the cleanest peg trade there is.
2. **Buy the discount and post an ask wall (E).** Swap USDC → imdUSD (toward the peg: 0.01%, no
   surcharge), then LP it into the band as an imdUSD-only range.
   - It earns as the price recovers and sells back at ~$1.
   - The risk is that it does not recover.
3. **Bid wall under the price (D).** For a user who thinks it may fall further first.
4. **Do not add USDC/imdUSD around the depegged price for points.** Outside the band nothing earns
   points. The fee APR looks huge only because active liquidity is tiny.
5. **Do not redeem unless `status` says redemption pays more than the pool.**

### G. Exit

- **Order.** `collect` first if fees are owed, then `remove --pct N` or `close`.
- **Timing.** If points are pending, wait until 7,200 blocks after the last addition, or they are
  forfeited. `positions` shows the points rate, and the last-addition block is on the position's
  ModifyLiquidity history.
- **Out of range.** If the position is out of range below, it holds only imdUSD. Exiting then means
  selling imdUSD at the depeg (paying the surcharge) or holding it (1x points).

### H. Other avenues

- **Other imdUSD pools.** Anyone can open a v4 pool, so other imdUSD pools exist (for example a 5%-fee
  imdUSD/USDC pool).
  - They earn no points, are thin, and their fee APR comes from a few trades.
  - A price gap between them and the peg pool is an arbitrage, not a yield.
  - `avenues` lists them.
- **Holding** earns 1x with no range risk. It is the baseline any LP strategy must beat.
- **Liquidations** earn 3 days of holding points per imdUSD repaid, plus the liquidation bonus.
  - The open-source keeper, [imdusd-keeper](https://github.com/fa11up/imdusd-keeper), does this.
  - It needs imdUSD inventory: a bite burns the caller's imdUSD.
- **Incentive campaigns** (Merkl, DefiLlama-listed yields): `avenues` checks for them. If one appears,
  compare its APR on the campaign's own terms (which pool, which range, which period).

## How to compare and recommend

For each candidate, state four numbers: **capital**, **fees/day**, **points/day while at peg**, and
**worst case** (what the user holds if the price runs through the range in the bad direction). Then
choose:

- **Points, at peg:** A, sized to what they would hold through a depeg, plus B if they want fees. New
  capital goes in a new position.
- **Peg defence:** C plus D, or in a depeg F1–F2.
- **Yield with the least risk:** hold, or a small A, and let the 1x holding baseline decide.
- **Exit:** G, respecting the hold window.

Always say what would change the recommendation:
- the price leaving the band;
- the feeds going stale (redemption and borrowing pause);
- a large LP leaving (`status`: liquidity by price);
- the season's last day.
