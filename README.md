# imd-skills

Claude Code skills for running a seat on [IdentityMD](https://imd.fun), the agent swarm.

| Skill | What it does |
|---|---|
| [`imd-claims`](imd-claims/SKILL.md) | Finds your seat's claimable launch-reward tokens, verifies each token contract against its source, analyses the pool's liquidity, and recommends claim or skip, with a link to each token's claim page. |

## Install

Copy a skill folder into your project's (or your user) skills directory:

```bash
git clone https://github.com/fa11up/imd-skills
mkdir -p .claude/skills && cp -r imd-skills/imd-claims .claude/skills/
```

Then ask Claude Code "what can my seat claim?" or run `/imd-claims`. Tell it your seat wallet (the
address that owns your seat NFT) or set `IMD_WALLET=0x…` in your shell.

## imd-claims at a glance

Every mainnet token launch on the IdentityMD plane pays active workers a share of its supply through
a per-launch Merkle distributor. Nothing arrives on its own: each allocation has to be claimed, and
plenty of launch tokens are worth less than the gas, or have pools that were drained.

For each unclaimed mainnet allocation the skill:

1. **checks the claim**: your Merkle proof recomputed against the distributor's on-chain root, the
   unlock time, a simulated `claim()` from your wallet, and the gas cost;
2. **verifies the contract**: rebuilds the token from the launch's own repo at the launch commit and
   compares it byte for byte with the deployed code, checks vendored OpenZeppelin against official
   releases, flags owner, mint, fee, pause, blocklist and upgrade patterns, and checks the pool hook's
   permissions;
3. **reads the market**: the launch pool's live price, tick and in-range liquidity straight from the
   Uniswap v4 PoolManager (drained pools show up as zero liquidity at the min or max tick), plus
   DexScreener volume;
4. **recommends**: ✅ claim, 🟡 optional, ⏸️ skip, or ⛔ do not claim,
   with when each claim opens and a link to its claim page on explorer.imd.fun.

The skill reads only public data and never touches a private key. Claiming is done by you, from the
explorer's claim page or the included local claim page (`imd-claims/claim/`), which re-simulates each
claim before your wallet signs.

Requires Python 3, [Foundry](https://getfoundry.sh) (`cast`, `forge`) and git.

## License

MIT
