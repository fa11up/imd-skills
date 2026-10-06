---
name: imd-claims
description: Check an IdentityMD seat's claimable launch-reward tokens, verify each token contract is safe, analyse its liquidity, and recommend claim or skip. Use when the user asks what their seat has earned, what's claimable, whether a launch token is safe, or whether to claim.
---

# IMD launch-reward claims: scan, verify, recommend

Every token launch on the IdentityMD plane gives active workers a share of its supply through a
per-launch `MerkleDistributor`, paid to the wallet that owns the seat's NFT. Nothing arrives by itself:
each must be **claimed**
with a mainnet transaction. Most allocations are Sepolia (worthless); the mainnet ones are worth
anything from $0 to tens of dollars, and some launch tokens are traps or dead markets. This skill
decides which are worth the gas.

**The wallet.** Every command takes the seat owner's wallet via `--wallet 0x…` or the `IMD_WALLET`
environment variable. If neither is set, ask the user for it (it's the address that owns the seat's
NFT; the explorer shows it at `explorer.imd.fun/wallet/<address>/earned`). It's public; no key is needed.

**Requirements:** Python 3, Foundry (`cast`, `forge`), git, and network access to api.imd.fun,
a public Ethereum RPC, GitHub and DexScreener.

Paths below are relative to this skill's folder (`.claude/skills/imd-claims/`).

## 1. Scan

```bash
python3 .claude/skills/imd-claims/scan.py --wallet 0xYourSeatWallet               # every unclaimed mainnet allocation
python3 .claude/skills/imd-claims/scan.py --wallet 0xYourSeatWallet --launch 757 756  # just these launches
```

About a minute per eight launches. It prints a block per launch and writes the full JSON to
`imd-claims-reports/scan-<time>.json` (change with `--out`). Per launch it checks:

- **claim**: our leaf from `api.imd.fun/launches/<id>?claims=1`, its proof recomputed against the
  distributor's on-chain root, already claimed, unlocked (one hour after launch), a simulated
  `claim()` from our wallet, gas cost in USD, and the sweep date (unclaimed tokens go to the treasury
  one year after launch).
- **contract**: clones `sourceRepoUrl` at `sourceCommit`, rebuilds with forge, compares runtime
  bytecode with the chain (immutables masked), checks each vendored OpenZeppelin file against the
  official release (or `master`), and greps the token's own source for risk patterns.
- **pool**: rebuilds the launch's v4 pool key and reads price, tick and in-range liquidity from the
  PoolManager, plus the hook's permission bits; then DexScreener liquidity, volume and trades.

## 2. Verify safety: read the source yourself

The scanner flags; you judge. For every launch still in play:

1. **Read the token's own source in full** (the `sources` list in the report, under the cloned
   `repo`; launch tokens are 15–250 lines). Read `notes` from `launch.json` too, but don't trust it
   over the code.
2. **Bytecode must match** (`bytecodeMatch.equal: true`). If it doesn't, or the build failed, the
   deployed contract is unverified: treat it as unsafe.
3. **Vendored OpenZeppelin must match** a release or `master`. "DIFFERS from every checked release"
   means someone edited library code. Diff it before trusting it.
4. **Run down every risk flag**. A flag is a reason to read, not a verdict:
   - fee/tax: who's exempt? Confirm on chain that our claim (distributor → us) and a pool sale
     (us → PoolManager `0x000000000004444c5dc75cB358380D2e3dE08A90`) are fee-free, e.g. via an
     `isFeeExempt(from,to)` view. Note that router or aggregator sells may pay the fee.
   - owner, mint, pause, blocklist, max-tx or trading toggles, upgrade, external calls: what can the
     privileged party do to OUR balance or our ability to sell? Redirecting fees is tolerable;
     freezing, minting or blocking sells is not.
5. **Hook permissions** should be `['beforeInitialize']` only: the platform's
   `PoolInitializationGuard`, which can't touch swaps or liquidity. Anything else (beforeSwap,
   ReturnsDelta, …) means the pool can tax or block trades: read the hook before claiming.
6. The **launch liquidity can't be pulled**: the factory holds it and only ever adds liquidity or
   collects fees with a zero delta. See `PoolFees.sol` and `LaunchLiquidity.sol` in
   [Identity-md/protocol](https://github.com/Identity-md/protocol/tree/master/packages/contracts/src);
   re-check only if the plane's factory address changes from
   `0xfF03410d0Fe5fa8f7F59F743de35E333D9857120`.

## 3. Liquidity analysis

From the report's `pool` and `market` sections:

- **`inRangeLiquidity: 0` or `atMinOrMaxTick: true`** means there's nothing to sell into right now,
  whatever DexScreener's price says. A token at tick ±887272 has been sold through every position (a
  drain). Look at the last swaps to see who drained it:
  `cast logs --address <PoolManager> <Swap topic> <poolId>`.
- **Value** = `ourValueUsd` (on-chain price × our amount). DexScreener under-prices IMD-paired pools
  and lists thin side pools; prefer the on-chain price from the launch pool.
- **Exit capacity**: our sale should be small next to the launch pool's liquidity. If our allocation
  is a meaningful share of it, the realised value will be well below `ourValueUsd`.
- **Churn**: volume far above liquidity (e.g. 20×+) means bots and a volatile price; mention it,
  since the value can move a lot before the user acts.
- The paired asset matters: IMD-paired proceeds arrive in IMD, ETH-paired in ETH.

## 4. Recommend

Give the user one table, then a verdict per token:

| Token | Launch | Contract | Pool | Liquidity | Our value | Claim gas | Claimable | Verdict | Claim |

The **Claimable** column says when the claim opens, from the report's `claimableIn`: "now", or
"in 47m (01:57Z)" for a launch still inside its one-hour lock. Add the claim-by date (`sweepableFrom`,
one year after launch) once, under the table, rather than repeating it per row.

The **Claim** column links each token's IdentityMD claim page, `https://explorer.imd.fun/token/<token address>`
(the report's `claimPage`), e.g. `[claim](https://explorer.imd.fun/token/0xaa6b…)`. Always include it.

Verdicts, each led by its emoji. The emoji goes in the **Verdict** column only; the Contract column
states the finding in words (e.g. "plain OZ ERC-20, exact match", "8% fee, claim + Uniswap sell exempt"):
- ✅ **Claim**: safe contract, liquid pool, value clearly above gas (say 5× the claim gas). Mention
  any fee path to avoid.
- 🟡 **Optional**: safe and liquid, but the value is close to gas.
- ⏸️ **Skip**: dead or drained pool, or value below gas. Still claimable until the sweep date, so
  note it.
- ⛔ **Do not claim**: the contract can freeze, tax or block the sale, or its bytecode doesn't match
  its source.

Use exactly these four labels; don't add advice about when to sell.

Be concrete about why: the line of code, the on-chain read, or the swap that decided it.

## 5. If the user wants to claim

```bash
python3 .claude/skills/imd-claims/claim/build_claims.py --wallet 0xYourSeatWallet <launch> [<launch> ...]
cd .claude/skills/imd-claims/claim && python3 -m http.server 3334 --bind 127.0.0.1   # if not already serving
```

Then the user claims on each token's explorer.imd.fun claim page (the link in the table), or opens
http://localhost:3334, connects the seat wallet on mainnet, and claims there. The page
re-simulates each claim before MetaMask signs. Anyone may submit a claim (tokens always go to our
wallet), but the wallet pays gas. **Never** read or use a private key to send a claim.

## Gotchas

- publicnode refuses Python's default user agent: the scripts send `Mozilla/5.0`. It also refuses
  archive and long log ranges; keep `cast logs` within about 10,000 blocks.
- `/launches/<id>` can 503 under load; the scanner retries.
- Two community launches have used the ticker **IMD**. The real IMD is
  `0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7`; name launch tokens by launch number in reports.
- A claim simulates as reverting before its unlock hour (`StillLocked`). That's not a problem with
  the claim.
