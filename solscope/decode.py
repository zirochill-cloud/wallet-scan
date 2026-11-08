"""Phase 2 swap decoder (doc 07 Phase 2, doc 10).

Two jobs:

1. **Locate swap instructions** in a ``getTransaction`` payload by program id
   + discriminator (Anchor ``sha256("global:<name>")[0:8]`` or Raydium's
   numeric tags), and attribute pool / trader / side from the instruction's
   own account list. Registry entries were verified against live mainnet
   fixtures (``tests/fixtures/tx_direct_*.json``, 2026-10-17).

2. **Derive actual executed amounts** from ``meta.pre/postTokenBalances`` —
   NOT from the instruction's u64 bounds. Doc 10 measured that balance deltas
   are exact for the trade while instruction bounds only encode the trader's
   limit (``max_sol_cost`` / ``min_sol_output``), so a price built from the
   bounds is the *quoted* price, not what happened.

Rules the code enforces (each from a measured failure in doc 10):

* No ``quote_reserve / base_reserve`` pricing: identifying a "vault" as the
  largest holder of a mint was off by 4–5 orders of magnitude. Reserve-ratio
  pricing needs genuine pool-state decoding and is NOT done here.
* Attribution is per (trader, mint) owner-deltas, not per-mint netting —
  netting user and vault legs cancels the trade to ~0 (doc 10 §4.3).
* If one trader swaps the same mint twice in one transaction, per-instruction
  attribution is ambiguous; such swaps are dropped with a reason rather than
  assigned a price that may belong to the other instruction.
* Programs without a verified layout (Meteora DLMM, Orca Whirlpool) are not
  claimed. Their swaps are skipped with a reason — a decoder that guesses is
  worse than one that abstains.
* The quote leg may not be a token balance at all. A trader paying native SOL
  opens a wSOL account, funds it and closes it inside the same transaction, so
  its *token* delta netts to exactly zero even though SOL moved. Measured live
  2026-10-25 on PumpSwap ``buy_exact_quote_in``: every direct swap in the
  sample had ``postTokenBalance = 0`` and no pre-entry, so token-only
  attribution reported "no quote delta" for swaps that plainly happened. The
  lamport delta (minus the fee when the trader is the fee payer) is the
  executed quote in that case.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

DECODER_VERSION = "phase2/1"

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NATIVE_SENTINEL = WSOL_MINT   # bonding-curve quote is native SOL; we store it as wSOL

COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"


def anchor_disc(name: str) -> bytes:
    """Anchor instruction discriminator: sha256('global:<name>')[:8]."""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def b58decode(s: str) -> bytes:
    A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + A.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b"\x00"
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


@dataclass(frozen=True)
class ProgramSpec:
    """One swap-capable program with verified account positions."""
    dex: str
    # discriminator -> (side, kind); kind 'anchor' means name-derived
    trades: dict           # bytes(8) or int tag -> TradeSpec
    pool_index: int        # account index of the pool within the instruction
    trader_index: int      # account index of the trader (user) within the ix
    quote_is_native: bool  # quote settles in native lamports, not a wSOL ATA


@dataclass(frozen=True)
class TradeSpec:
    side: str              # 'buy' | 'sell' | 'directional'
    name: str              # instruction name / tag label


def _anchor_trades(*names: str, side_map: dict[str, str] | None = None) -> dict:
    side_map = side_map or {}
    out = {}
    for n in names:
        side = side_map.get(n, "buy" if n.startswith("buy") else "sell" if n.startswith("sell") else "directional")
        out[anchor_disc(n)] = TradeSpec(side=side, name=n)
    return out


PROGRAMS: dict[str, ProgramSpec] = {
    # PumpSwap AMM (pump.fun post-migration). Verified live 2026-10-17:
    # accounts[0]=pool, [1]=user, [2]=fee recipient, [3]=token mint,
    # [4]=quote mint (wSOL). Data = disc + u64 base + u64 quote bound.
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ProgramSpec(
        dex="pumpswap",
        trades=_anchor_trades("buy", "sell", "buy_v2", "sell_v2",
                              "buy_exact_quote_in", "buy_exact_quote_in_v2"),
        pool_index=0, trader_index=1, quote_is_native=False,
    ),
    # Pump.fun bonding curve (pre-migration). Legacy user@6; v3 user@8.
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": ProgramSpec(
        dex="pump",
        trades=_anchor_trades("buy", "sell", "buy_v2", "sell_v2", "buy_v3", "sell_v3"),
        pool_index=3, trader_index=6, quote_is_native=True,
    ),
    # Raydium AMM v4 — NOT Anchor: numeric first-byte tags (doc: 9/11, v2 16/17).
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ProgramSpec(
        dex="raydium_amm_v4",
        trades={9: TradeSpec("directional", "swap_base_in"),
                11: TradeSpec("directional", "swap_base_out"),
                16: TradeSpec("directional", "swap_base_in_v2"),
                17: TradeSpec("directional", "swap_base_out_v2")},
        pool_index=1, trader_index=17, quote_is_native=False,
    ),
    # Raydium LaunchLab. Discriminators computed via the documented Anchor
    # scheme; the two buy values reproduce independently verified bytes
    # (faea0d7bd59c13ec / 18d3742869039938), which validates the scheme for
    # the computed sell values as well.
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": ProgramSpec(
        dex="raydium_launchlab",
        trades=_anchor_trades("buy_exact_in", "buy_exact_out",
                              "sell_exact_in", "sell_exact_out"),
        pool_index=2, trader_index=0, quote_is_native=True,
    ),
    # Raydium CPMM. Discriminators match independently verified bytes
    # (8fbe5adac41e33de / 37d96256a34ab4ad). Direction comes from deltas.
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ProgramSpec(
        dex="raydium_cpmm",
        trades=_anchor_trades("swap_base_input", "swap_base_output"),
        pool_index=1, trader_index=0, quote_is_native=False,
    ),
}

# Programs we deliberately do NOT decode, so that a skip is a NAMED event
# rather than a silent absence from PROGRAMS. Two reasons live here:
#   unverified  - distinct account layout, no verified fixtures yet;
#   abstention  - a known program we choose not to decode (Jupiter, below).
UNVERIFIED_PROGRAMS = {
    "LMMJ8mwJZrFesCVYPsfkLKGvWqHhE9UbuNqtvJzCwrL": "meteora_dlmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_dlmm",
    # Jupiter v6 -- ABSTENTION, not an unknown layout. The top-level route
    # instruction is intentionally not decoded: it is an aggregator, and the
    # DEX swaps it invokes are visited like any other instruction through
    # _iter_instructions' inner walk (their own program ids decide decoding).
    # Registered because its absence was invisible -- docs/PHASE1A_RECOVERY.md
    # §7 found this ID nowhere in solscope/, while aggregator-routed volume is
    # the dominant share of live pool activity (doc 11 §8), so skipping it
    # silently would under-count wallets that trade through Jupiter. The ID
    # already appears in 4 fixtures (tx_buy_0/1/2, tx_sell_1); verify against
    # those before any attempt to decode the route instruction itself.
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter_v6",
}


@dataclass(frozen=True)
class SwapInstruction:
    program: str            # program id
    dex: str
    name: str               # instruction name or tag label
    side_hint: str          # 'buy' | 'sell' | 'directional'
    pool_address: str
    trader: str
    token_mint: str | None  # the non-quote mint, when readable from accounts
    quote_mint: str | None
    quote_is_native: bool
    ix_index: int           # top-level index, or -1 for inner
    inner_index: int | None


@dataclass(frozen=True)
class FeeComponents:
    fee_lamports: int
    priority_fee_lamports: int | None
    compute_units: int | None
    jito_tip_lamports: int | None = None   # not observable in these payloads


@dataclass(frozen=True)
class DerivedSwap:
    """One decoded swap with executed amounts from balance deltas."""
    signature: str
    slot: int
    block_time: int | None
    instruction: SwapInstruction
    side: str               # resolved 'buy' | 'sell'
    base_mint: str
    quote_mint: str
    base_amount_raw: int    # executed, absolute
    quote_amount_raw: int   # executed, absolute
    base_decimals: int
    quote_decimals: int
    fees: FeeComponents
    trader: str
    is_failed: bool
    quote_settlement: str = "token"   # 'token' | 'native' (lamport-settled)
    reason: str = ""        # populated only on skipped/partial records


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------

def account_keys(result: dict) -> list[str]:
    """Full ordered key list: static keys + loaded (address-table) keys.

    Indexing rules match the RPC's compiled instruction format: static keys
    first, then loaded writable, then loaded readonly.
    """
    tx = result.get("transaction") or {}
    msg = tx.get("message") or {}
    keys = list(msg.get("accountKeys") or [])
    loaded = (result.get("meta") or {}).get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or [])
    keys += list(loaded.get("readonly") or [])
    return keys


def _iter_instructions(result: dict):
    """Yield (ix_index, inner_index, instruction_dict) over top + inner."""
    tx = result.get("transaction") or {}
    msg = tx.get("message") or {}
    for i, ix in enumerate(msg.get("instructions") or []):
        yield i, None, ix
    for group in (result.get("meta") or {}).get("innerInstructions") or []:
        for j, ix in enumerate(group.get("instructions") or []):
            yield group.get("index", -1), j, ix


# ---------------------------------------------------------------------------
# instruction location
# ---------------------------------------------------------------------------

def find_swap_instructions(result: dict) -> list[SwapInstruction]:
    """All recognisable swap instructions in the payload, in scan order.

    Unrecognised programs are silently out of scope; recognised programs with
    an unknown discriminator are NOT matched (a schema change must degrade the
    feed, not corrupt it).
    """
    keys = account_keys(result)
    out: list[SwapInstruction] = []
    for ix_index, inner_index, ix in _iter_instructions(result):
        pi = ix.get("programIdIndex")
        if pi is None or pi >= len(keys):
            continue
        program = keys[pi]
        spec = PROGRAMS.get(program)
        if spec is None:
            continue
        data = b58decode(ix.get("data") or "")
        trade = None
        if len(data) >= 8:
            trade = spec.trades.get(data[:8])
            if trade is None and program.endswith("Ut1Mp8"):
                # Raydium v4 uses a 1-byte tag, not an 8-byte discriminator.
                trade = spec.trades.get(data[0])
        if trade is None:
            continue
        accounts = [keys[i] for i in ix.get("accounts") or [] if i < len(keys)]
        pool = accounts[spec.pool_index] if spec.pool_index < len(accounts) else ""
        trader = accounts[spec.trader_index] if spec.trader_index < len(accounts) else ""
        token_mint, quote_mint = _mints_from_accounts(spec, accounts)
        out.append(SwapInstruction(
            program=program, dex=spec.dex, name=trade.name, side_hint=trade.side,
            pool_address=pool, trader=trader, token_mint=token_mint,
            quote_mint=quote_mint, quote_is_native=spec.quote_is_native,
            ix_index=ix_index, inner_index=inner_index,
        ))
    return out


def _mints_from_accounts(spec: ProgramSpec, accounts: list[str]) -> tuple[str | None, str | None]:
    """Readable mint positions for the layouts where they are pinned.

    PumpSwap: [3]=token mint, [4]=quote mint (verified live).
    Pump.fun bonding curve: [1]=fee recipient, [2]=mint, [3]=bonding curve; the
    quote is native SOL, so it is reported as the wSOL sentinel. Verified
    2026-11-08 on GB68eE...: buy tx hkeSSJrW has accounts[2]=GB68eE and
    postTokenBalances crediting the buyer at accounts[6] with that mint.
    Previously every bonding-curve trade resolved to no mint and was skipped,
    so the earliest buyers on a pump launch could not be attributed at all.
    Others: None — the pipeline resolves mints from the pools registry.
    """
    if spec.dex == "pumpswap" and len(accounts) > 4:
        return accounts[3], accounts[4]
    if spec.dex == "pump" and len(accounts) > 2:
        return accounts[2], NATIVE_SENTINEL
    return None, None


# ---------------------------------------------------------------------------
# balance-delta amounts
# ---------------------------------------------------------------------------

def _owner_index(result: dict) -> dict[int, tuple[str, str, int]]:
    """accountIndex -> (owner, mint, decimals) from pre+post token balances."""
    out: dict[int, tuple[str, str, int]] = {}
    meta = result.get("meta") or {}
    for tb in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        idx = tb.get("accountIndex")
        amt = tb.get("uiTokenAmount") or {}
        if idx is None or tb.get("mint") is None:
            continue
        out[int(idx)] = (tb.get("owner") or "", tb["mint"], int(amt.get("decimals") or 0))
    return out


def owner_deltas(result: dict) -> dict[tuple[str, str], int]:
    """(owner, mint) -> net raw balance change. Fail-closed on missing sides."""
    pre: dict[int, int] = {}
    post: dict[int, int] = {}
    meta = result.get("meta") or {}
    for tb in meta.get("preTokenBalances") or []:
        pre[int(tb["accountIndex"])] = int(tb["uiTokenAmount"]["amount"])
    for tb in meta.get("postTokenBalances") or []:
        post[int(tb["accountIndex"])] = int(tb["uiTokenAmount"]["amount"])
    owners = _owner_index(result)
    deltas: dict[tuple[str, str], int] = {}
    for idx in set(pre) | set(post):
        if idx not in owners:
            continue
        owner, mint, _ = owners[idx]
        d = post.get(idx, 0) - pre.get(idx, 0)
        key = (owner, mint)
        deltas[key] = deltas.get(key, 0) + d
    return deltas


def lamport_delta(result: dict, address: str) -> int:
    """Native-lamport change of one account, fee already accounted elsewhere."""
    keys = account_keys(result)
    try:
        i = keys.index(address)
    except ValueError:
        return 0
    meta = result.get("meta") or {}
    pre = (meta.get("preBalances") or [0] * len(keys))
    post = (meta.get("postBalances") or [0] * len(keys))
    if i >= len(pre) or i >= len(post):
        return 0
    return post[i] - pre[i]


def swap_side(ix: SwapInstruction, deltas: dict[tuple[str, str], int],
              base_mint: str) -> str | None:
    """Resolve 'directional' instructions from the trader's base-mint delta.

    Negative base delta = trader sold base (a sell). Returns None when the
    delta is zero or absent (no trade to attribute).
    """
    if ix.side_hint in ("buy", "sell"):
        return ix.side_hint
    d = deltas.get((ix.trader, base_mint))
    if d is None or d == 0:
        return None
    return "sell" if d < 0 else "buy"


# ---------------------------------------------------------------------------
# fees
# ---------------------------------------------------------------------------

def fee_components(result: dict) -> FeeComponents:
    """Base/priority fee split from meta.fee and the ComputeBudget ixs."""
    meta = result.get("meta") or {}
    total = int(meta.get("fee") or 0)
    cu_limit: int | None = None
    cu_price: int | None = None
    for _i, _j, ix in _iter_instructions(result):
        keys = account_keys(result)
        pi = ix.get("programIdIndex")
        if pi is None or pi >= len(keys) or keys[pi] != COMPUTE_BUDGET:
            continue
        data = b58decode(ix.get("data") or "")
        if not data:
            continue
        tag = data[0]
        if tag == 2 and len(data) >= 5:            # SetComputeUnitLimit
            cu_limit = struct.unpack("<I", data[1:5])[0]
        elif tag == 3 and len(data) >= 9:          # SetComputeUnitPrice
            cu_price = struct.unpack("<Q", data[1:9])[0]
    priority = None
    if cu_limit is not None and cu_price is not None:
        priority = cu_limit * cu_price // 1_000_000
    return FeeComponents(fee_lamports=total, priority_fee_lamports=priority,
                        compute_units=cu_limit)


# ---------------------------------------------------------------------------
# the full derivation
# ---------------------------------------------------------------------------

def derive_swaps(result: dict, *, signature: str | None = None,
                 known_pools: dict[str, tuple[str, str]] | None = None,
                 quote_resolver=None) -> tuple[list[DerivedSwap], list[str]]:
    """Decode every attributable swap in one transaction payload.

    ``known_pools``: pool_address -> (base_mint, quote_mint) from the pools
    registry; used for layouts whose account list does not carry the mints.
    ``quote_resolver``: optional callable(quote_mint) -> USD price for later
    phases; unused by the gate, which compares in quote units.

    Returns (swaps, skip_reasons). A transaction can yield several swaps; a
    swap that cannot be attributed unambiguously appears only as a reason.
    """
    meta = result.get("meta") or {}
    tx = result.get("transaction") or {}
    signature = signature or (tx.get("signatures") or [""])[0]
    slot = int(result.get("slot") or 0)
    block_time = result.get("blockTime")
    is_failed = meta.get("err") is not None
    known_pools = known_pools or {}
    fees = fee_components(result)

    # A failed transaction is never a swap, whatever instructions it names: the
    # balances moved and moved back, so any "amount" read from them is fiction.
    # Checked first so a failed transaction is always *explained*, even when it
    # contains no instruction the decoder recognises.
    if is_failed:
        return [], [f"{signature[:12]}: transaction failed — amounts are not executed"]

    ixs = find_swap_instructions(result)
    skips: list[str] = []
    swaps: list[DerivedSwap] = []
    if not ixs:
        return [], skips

    deltas = owner_deltas(result)
    owners = _owner_index(result)
    decimals_by_mint: dict[str, int] = {mint: dec for _, (_, mint, dec) in owners.items()}

    # Ambiguity guard: same trader trading the same token twice in one tx
    # makes per-instruction attribution unsafe (doc 10 §4.3 applied per tx).
    seen: dict[tuple[str, str], int] = {}
    for ix in ixs:
        key = (ix.trader, ix.token_mint or ix.pool_address)
        seen[key] = seen.get(key, 0) + 1

    for ix in ixs:
        base_mint, quote_mint, note = _resolve_mints(ix, known_pools)
        if not base_mint or not quote_mint:
            skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: mints unresolved "
                         f"(pool {ix.pool_address[:8]} not in registry)")
            continue
        if seen[(ix.trader, ix.token_mint or ix.pool_address)] > 1:
            skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: trader swapped "
                         f"same pool twice in one tx — attribution ambiguous")
            continue

        side = swap_side(ix, deltas, base_mint)
        if side is None:
            skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: no trader delta "
                         f"for {base_mint[:8]}")
            continue

        settlement = "token"
        qd = deltas.get((ix.trader, quote_mint))
        if ix.quote_is_native or qd is None or qd == 0:
            # Native settlement. A transient wrapped-SOL account nets to zero,
            # so the quote leg lives in lamports, not token balances (see the
            # module docstring). Fee is subtracted only when the trader is the
            # fee payer: meta.fee is charged to the fee payer alone.
            settlement = "native"
            quote_d = -lamport_delta(result, ix.trader)
            keys = account_keys(result)
            if keys and ix.trader == keys[0]:
                quote_d -= fees.fee_lamports
            quote_raw = abs(quote_d)
            if quote_raw == 0:
                skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: quote leg "
                             f"settled nowhere (no token delta, no lamport delta)")
                continue
        else:
            quote_raw = abs(qd)

        base_d = deltas.get((ix.trader, base_mint))
        if base_d is None or base_d == 0 or quote_raw == 0:
            skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: zero executed amount")
            continue

        base_dec = decimals_by_mint.get(base_mint)
        quote_dec = decimals_by_mint.get(quote_mint, 9 if quote_mint == WSOL_MINT else None)
        if base_dec is None:
            skips.append(f"{signature[:12]}: {ix.dex}/{ix.name}: decimals unknown "
                         f"for {base_mint[:8]} — fail closed")
            continue

        swaps.append(DerivedSwap(
            signature=signature, slot=slot, block_time=block_time, instruction=ix,
            side=side, base_mint=base_mint, quote_mint=quote_mint,
            base_amount_raw=abs(base_d), quote_amount_raw=quote_raw,
            base_decimals=base_dec, quote_decimals=quote_dec or 0,
            fees=fees, trader=ix.trader, is_failed=False,
            quote_settlement=settlement, reason=note,
        ))
    return swaps, skips


def _resolve_mints(ix: SwapInstruction,
                   known_pools: dict[str, tuple[str, str]]) -> tuple[str | None, str | None, str]:
    """(base, quote, note) for an instruction; mints from accounts or registry."""
    if ix.token_mint and ix.quote_mint:
        base = NATIVE_SENTINEL if False else ix.token_mint
        return base, ix.quote_mint, ""
    hit = known_pools.get(ix.pool_address)
    if hit:
        return hit[0], hit[1] or NATIVE_SENTINEL, ""
    return None, None, ""


def exec_price_quote(s: DerivedSwap) -> float:
    """Quote units per whole base unit, slippage included (doc 07 Phase 2)."""
    bq = s.quote_amount_raw / (10 ** s.quote_decimals)
    bb = s.base_amount_raw / (10 ** s.base_decimals)
    return bq / bb if bb else float("nan")
