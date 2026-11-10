"""Mint-scoped retrospective anchor history.

Rebuilds one anchor's on-chain trade history from Helius and turns it into the
two rows that Phase 1b actually needs: ``swaps`` (which carries
``price_provenance`` and therefore the confidence factor) and ``positions``
(which comes from ``rebuild``).

Oracle asymmetry, and it matters when reading a failure
------------------------------------------------------
The verification cache holds ~20 entry legs per anchor. Reconstruction sees
every decoded leg (up to a few hundred). So:

* a **cache signature missing from ``entry_legs``** is a hard failure — the
  decoder missed a swap the verification spike found;
* an **``entry_legs`` signature absent from the cache** is expected — those are
  the non-buyer legs the cache was never meant to hold.

Index both sides **by signature**, never by list position: decode order follows
pagination, so positional comparison invents mismatches.

Persistence is ``rebuild(conn, wallet=…, mint=…)``, not ``reconstruct``:
``reconstruct`` takes no connection and writes nothing, so calling it directly
would compute PositionBooks that never reach the database. ``rebuild`` loads
legs, reconstructs, deletes the (address, mint) pair and persists — once per
wallet, because the delete is scoped to that pair.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Mapping

from .decode import derive_swaps
from .derive import exec_price_quote
from .positions import SwapLeg, rebuild

__all__ = ["FetchReport", "fetch_anchor_history"]

_SIGNATURE_PAGE = 1000

#: Swaps are the anchor's history, so every decoded leg is written; positions
#: are only built for the wallets the caller asked about.
_PAGE_LIMIT = _SIGNATURE_PAGE


@dataclass
class FetchReport:
    anchor_mint: str
    signatures_fetched: int = 0
    transactions_decoded: int = 0
    transactions_skipped: int = 0
    swaps_written: int = 0
    credits_used: int = 0
    failures: list[dict] = field(default_factory=list)
    entry_legs: list[dict] = field(default_factory=list)


def _rpc_for(config: Mapping[str, Any]):
    """The caller may inject ``config["rpc"]``; tests always do."""
    rpc = config.get("rpc") if config else None
    if rpc is not None:
        return rpc
    from .adapters.helius import HeliusRpc, api_key_from_env

    key = api_key_from_env()
    if not key:
        raise RuntimeError("no Helius API key configured (config['rpc'] or env)")
    return HeliusRpc(api_key=key, ledger=config.get("ledger"), job="retro")


def _block_time(row: dict) -> _dt.datetime | None:
    stamps = row.get("blockTime")
    if not stamps:
        return None
    return _dt.datetime.fromtimestamp(int(stamps), _dt.timezone.utc)


def _upsert_parents(conn, swap) -> None:
    conn.execute("INSERT INTO chains (id, display_name) VALUES ('solana','Solana') "
                 "ON CONFLICT DO NOTHING")
    for mint in {swap.base_mint, swap.quote_mint}:
        if mint:
            conn.execute("INSERT INTO tokens (mint, chain_id) VALUES (%s,'solana') "
                         "ON CONFLICT DO NOTHING", (mint,))
    conn.execute("INSERT INTO wallets (address, chain_id) VALUES (%s,'solana') "
                 "ON CONFLICT DO NOTHING", (swap.trader,))
    if swap.block_time:
        when = _dt.datetime.fromtimestamp(int(swap.block_time), _dt.timezone.utc)
    else:
        when = None
    conn.execute(
        "INSERT INTO transactions (signature, chain_id, slot, block_time, err, "
        "  fee_lamports, priority_fee_lamports, compute_units, signer, fee_payer, "
        "  commitment, decoded_at) "
        "VALUES (%s,'solana',%s,%s,false,%s,%s,%s,%s,%s,'confirmed',now()) "
        "ON CONFLICT (signature) DO NOTHING",
        (swap.signature, swap.slot, when, swap.fees.fee_lamports,
         swap.fees.priority_fee_lamports, swap.fees.compute_units,
         swap.trader, swap.trader))


def _insert_swap(conn, swap) -> int | None:
    """Insert one swap and return its ``swaps.id``, including on conflict.

    ``ON CONFLICT DO NOTHING`` returns no row, so a re-run must look the id up.
    ``inner_index`` is NULL for top-level swaps, and plain ``=`` never matches
    NULL — hence ``IS NOT DISTINCT FROM``.
    """
    when = (_dt.datetime.fromtimestamp(int(swap.block_time), _dt.timezone.utc)
            if swap.block_time else None)
    ix = swap.instruction
    row = conn.execute(
        "INSERT INTO swaps (signature, instruction_index, inner_index, slot, event_time, "
        "  pool_address, chain_id, trader, base_mint, quote_mint, side, base_amount_raw, "
        "  quote_amount_raw, base_decimals, quote_decimals, exec_price_quote, "
        "  price_provenance, is_failed, decoder_version) "
        "VALUES (%s,%s,%s,%s,%s,%s,'solana',%s,%s,%s,%s,%s,%s,%s,%s,%s, "
        "  'swap_derived', false, %s) "
        "ON CONFLICT (signature, instruction_index, inner_index) DO NOTHING "
        "RETURNING id",
        (swap.signature, ix.ix_index, ix.inner_index, swap.slot, when,
         ix.pool_address, swap.trader, swap.base_mint, swap.quote_mint, swap.side,
         str(swap.base_amount_raw), str(swap.quote_amount_raw),
         swap.base_decimals, swap.quote_decimals, str(exec_price_quote(swap)),
         "retro/1"),
    ).fetchone()
    if row is not None:
        return int(row[0])
    found = conn.execute(
        "SELECT id FROM swaps WHERE signature = %s AND instruction_index = %s "
        "AND inner_index IS NOT DISTINCT FROM %s",
        (swap.signature, ix.ix_index, ix.inner_index),
    ).fetchone()
    return int(found[0]) if found is not None else None


def _leg(swap, swap_id: int | None) -> SwapLeg:
    """``swap_id`` feeds lot_matches, ``signature`` feeds entry_swap_signature."""
    return SwapLeg(
        swap_id=swap_id,
        side=swap.side,
        base_amount_raw=int(swap.base_amount_raw),
        quote_amount_raw=int(swap.quote_amount_raw),
        base_decimals=int(swap.base_decimals),
        quote_decimals=int(swap.quote_decimals),
        slot=int(swap.slot),
        block_time=(_dt.datetime.fromtimestamp(int(swap.block_time), _dt.timezone.utc)
                    if swap.block_time else None),
        fee_lamports=int(getattr(swap.fees, "fee_lamports", 0) or 0),
        price_usd=None,
        mcap_usd=None,
        signature=swap.signature,
    )


def fetch_anchor_history(
    conn,
    *,
    anchor_mint: str,
    genesis_time: _dt.datetime,
    window_days: int = 30,
    buyer_wallets: set[str],
    config: Mapping[str, Any],
    dry_run: bool = False,
) -> FetchReport:
    """Walk one anchor's signatures backwards and (unless dry) materialise them."""
    report = FetchReport(anchor_mint=anchor_mint)
    rpc = _rpc_for(config)
    window_end = genesis_time + _dt.timedelta(days=window_days)

    pending: list[Any] = []
    before: str | None = None
    done = False
    while not done:
        payload, _ = rpc.signatures_for_address(anchor_mint, limit=_PAGE_LIMIT,
                                                before=before)
        report.credits_used += 1  # one credit per RPC call, mirroring the ledger charge
        rows = payload.get("result") or []
        if not rows:
            break
        for row in rows:
            signature = row.get("signature")
            when = _block_time(row)
            if not signature:
                continue
            if when is not None and when < genesis_time:
                done = True
                break
            if when is not None and when > window_end:
                continue
            report.signatures_fetched += 1
            try:
                tx, _ = rpc.transaction(signature)
                report.credits_used += 1
                swaps = derive_swaps(tx, signature=signature)
            except Exception as exc:  # per-signature: never abort the walk
                report.failures.append({"signature": signature, "reason": str(exc)})
                continue
            kept = [s for s in swaps if s.base_mint == anchor_mint]
            if not kept:
                report.transactions_skipped += 1
                continue
            report.transactions_decoded += 1
            for swap in kept:
                pending.append(swap)
        before = rows[-1].get("signature")
        if done or not before:
            break

    if dry_run:
        for swap in pending:
            report.entry_legs.append({
                "signature": swap.signature,
                "slot": int(swap.slot),
                "block_time": swap.block_time,
                "wallet": swap.trader,
                "sol_per_token": float(exec_price_quote(swap)),
            })
        return report

    for swap in pending:
        try:
            _upsert_parents(conn, swap)
            swap_id = _insert_swap(conn, swap)
            if swap_id is not None:
                report.swaps_written += 1
            report.entry_legs.append({
                "signature": swap.signature,
                "slot": int(swap.slot),
                "block_time": swap.block_time,
                "wallet": swap.trader,
                "sol_per_token": float(exec_price_quote(swap)),
            })
        except Exception as exc:
            conn.rollback()
            report.failures.append({"signature": swap.signature, "reason": str(exc)})
    conn.commit()

    for wallet in sorted(buyer_wallets):
        try:
            rebuild(conn, wallet=wallet, mint=anchor_mint)
        except Exception as exc:
            report.failures.append({"signature": f"rebuild:{wallet}",
                                    "reason": str(exc)})
    return report
