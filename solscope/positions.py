"""Phase 3 — position reconstruction (doc 07 Phase 3, doc 04 §3).

Turns one wallet's swap history in one token into **position episodes** with a
FIFO cost basis, realized P&L, and an explicit record of everything that makes
that P&L less than complete.

The rule this module exists to enforce
--------------------------------------
`token_transfers` and `swaps` are separate tables, and `lot_matches` only ever
links a buy swap to a sell swap. **A transfer can never acquire a cost basis**,
so the mistake that manufactures fake geniuses — receive tokens free, dump them,
appear to earn an infinite return — is structurally impossible rather than
merely discouraged.

In practice, when a wallet sells more than it bought, the excess is *free
inventory*: consumed for quantity accounting, its proceeds **excluded from
realized P&L**, and the episode stamped `includes_transfer_in` /
`includes_airdrop` with `pnl_completeness='partial'`. When there is no evidence
where the excess came from — no transfer rows, no earlier buys in the window —
the basis is unknowable and realized P&L is **None with a reason**, never 0.
A pure dump of free inventory reports **None**, not a fabricated break-even.

Episode boundaries
------------------
An episode opens on the first buy with nothing open and closes when the
swap-acquired quantity returns to zero. A later re-entry is a second episode
with its own entry facts, as doc 07 requires.

Not in this module
------------------
* Mark-to-market equity curves need a price at every snapshot instant. Unrealized
  P&L is reported only when the caller supplies a mark *and* a SOL/USD rate.
* USD conversion is the caller's rate; without it P&L stays in lamports and USD
  columns stay NULL rather than being invented.
"""
from __future__ import annotations

import datetime as _dt
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN

NATIVE_LAMPORT_DECIMALS = 9
LAMPORTS_PER_SOL = Decimal(10 ** NATIVE_LAMPORT_DECIMALS)

#: `completeness_kind` values used for P&L quality.
COMPLETE = "complete"
PARTIAL = "partial"
UNKNOWN = "unknown"

TRANSFER_KINDS_AIRDROP = ("airdrop_claim", "mint")


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwapLeg:
    """One `swaps` row, reduced to what reconstruction needs."""
    swap_id: int | None
    side: str                       # 'buy' | 'sell'
    base_amount_raw: int            # tokens moved (absolute)
    quote_amount_raw: int           # SOL/USDC moved (absolute)
    base_decimals: int
    quote_decimals: int
    slot: int
    block_time: _dt.datetime | None = None
    fee_lamports: int = 0
    price_usd: float | None = None       # exec price at this swap, when known
    mcap_usd: float | None = None
    liquidity_usd: float | None = None
    token_age_seconds: int | None = None


@dataclass(frozen=True)
class TransferIn:
    """A transfer the wallet *received*: free inventory, never a cost basis."""
    amount_raw: int
    kind: str = "spl_transfer"      # 'spl_transfer' | 'airdrop_claim' | ...
    slot: int | None = None
    event_time: _dt.datetime | None = None


@dataclass(frozen=True)
class LotMatch:
    """FIFO link between one buy lot and one sell."""
    buy_swap_id: int | None
    sell_swap_id: int | None
    matched_qty_raw: int
    cost_lamports: int
    proceeds_lamports: int
    fee_lamports: int
    pnl_lamports: int
    hold_seconds: int | None


@dataclass
class Episode:
    """One position episode: an open-to-close stretch of holding a token."""
    wallet: str
    mint: str
    opened_at: _dt.datetime | None
    opened_slot: int | None
    base_decimals: int = 0
    closed_at: _dt.datetime | None = None
    closed_slot: int | None = None
    is_closed: bool = False

    total_buy_qty_raw: int = 0
    total_sell_qty_raw: int = 0
    open_qty_raw: int = 0
    cost_basis_lamports: int = 0
    proceeds_lamports: int = 0
    fees_lamports: int = 0

    realized_pnl_lamports: int | None = None
    realized_pnl_usd: float | None = None
    roi: float | None = None
    hold_seconds: int | None = None

    # Entry facts, captured at the first buy (point-in-time, never backfilled).
    entry_price_usd: float | None = None
    entry_mcap_usd: float | None = None
    entry_liquidity_usd: float | None = None
    entry_token_age_seconds: int | None = None
    entry_slot: int | None = None

    # Inventory provenance.
    inventory_origin: str = "swap_only"
    transfer_in_qty_raw: int = 0
    free_inventory_sold_raw: int = 0
    free_proceeds_lamports: int = 0
    uncovered_sell_qty_raw: int = 0
    basis_incomplete: bool = False       # a buy's quote was not native SOL

    pnl_completeness: str = COMPLETE
    pnl_limitation: str | None = None

    unrealized_pnl_usd: float | None = None
    unrealized_price_usd: float | None = None
    unrealized_as_of: _dt.datetime | None = None

    lot_matches: list[LotMatch] = field(default_factory=list)
    buy_swap_ids: list[int | None] = field(default_factory=list)
    sell_swap_ids: list[int | None] = field(default_factory=list)

    @property
    def realized_pnl(self) -> Decimal | None:
        """Realized P&L in SOL, or None when it cannot honestly be computed."""
        if self.realized_pnl_lamports is None:
            return None
        return Decimal(self.realized_pnl_lamports) / LAMPORTS_PER_SOL

    @property
    def realized_pnl_known(self) -> bool:
        return self.realized_pnl_lamports is not None

    def notes(self) -> list[str]:
        out = []
        if self.pnl_limitation:
            out.append(self.pnl_limitation)
        if self.free_inventory_sold_raw:
            out.append(
                f"sold {self.free_inventory_sold_raw} raw of free inventory "
                f"({self.inventory_origin}); {self.free_proceeds_lamports} lamports "
                f"of proceeds excluded from P&L")
        return out


@dataclass
class PositionBook:
    """Every episode for one (wallet, mint), in time order."""
    wallet: str
    mint: str
    episodes: list[Episode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def open_episode(self) -> Episode | None:
        for ep in self.episodes:
            if not ep.is_closed:
                return ep
        return None

    @property
    def realized_total_lamports(self) -> int | None:
        """Sum over episodes; None if any episode's P&L is unknowable."""
        if any(ep.realized_pnl_lamports is None for ep in self.episodes):
            return None
        return sum(ep.realized_pnl_lamports or 0 for ep in self.episodes)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _lamports(quote_raw: int, decimals: int) -> int | None:
    """Quote units -> lamports, or None when the quote is not native SOL.

    A USDC-quoted trade cannot fill a lamport column without a conversion rate,
    and silently mixing units would corrupt every downstream P&L.
    """
    return int(quote_raw) if decimals == NATIVE_LAMPORT_DECIMALS else None


def _share(total: int, part_qty: int, whole_qty: int) -> int:
    """Integer share of ``total`` by quantity, exact under repeated use."""
    if whole_qty <= 0:
        return 0
    return int((Decimal(total) * Decimal(part_qty) / Decimal(whole_qty))
               .to_integral_value(rounding=ROUND_HALF_EVEN))


def _seconds_between(start: _dt.datetime | None, end: _dt.datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _sort_key(slot: int | None, when: _dt.datetime | None, tiebreak: int) -> tuple:
    """Slot is the chain's own ordering; time breaks ties; id breaks the rest."""
    ts = when.timestamp() if when is not None else float("-inf")
    return (slot if slot is not None else -1, ts, tiebreak)


def _usd(lamports: int | None, sol_usd: Decimal | None) -> float | None:
    if lamports is None or sol_usd is None:
        return None
    return float((Decimal(lamports) / LAMPORTS_PER_SOL) * sol_usd)


# ---------------------------------------------------------------------------
# reconstruction
# ---------------------------------------------------------------------------

def reconstruct(
    wallet: str,
    mint: str,
    legs: list[SwapLeg] | tuple[SwapLeg, ...],
    transfers: list[TransferIn] | tuple[TransferIn, ...] = (),
    *,
    mark_price_usd: float | None = None,
    mark_as_of: _dt.datetime | None = None,
    sol_usd: Decimal | None = None,
) -> PositionBook:
    """Build position episodes for one wallet in one token.

    ``legs`` may arrive in any order; they are sorted by slot then time.
    ``mark_price_usd`` marks still-open inventory; without it an open episode's
    unrealized P&L stays None rather than treating the last trade as a mark.
    """
    book = PositionBook(wallet=wallet, mint=mint)

    events: list[tuple[tuple, str, object]] = []
    for i, leg in enumerate(legs):
        if leg.side not in ("buy", "sell"):
            book.warnings.append(f"swap {leg.swap_id}: unknown side {leg.side!r} — skipped")
            continue
        if leg.base_amount_raw <= 0:
            book.warnings.append(f"swap {leg.swap_id}: non-positive base amount — skipped")
            continue
        events.append((_sort_key(leg.slot, leg.block_time, i), "swap", leg))
    for i, tr in enumerate(transfers):
        if tr.amount_raw <= 0:
            continue
        events.append((_sort_key(tr.slot, tr.event_time, 10_000 + i), "transfer", tr))
    events.sort(key=lambda e: e[0])

    # Free inventory belongs to no lot, hence to no episode's basis — whichever
    # episode ends up selling it. Tracked by origin so the flag can say which.
    free_qty = {"transfer": 0, "airdrop": 0}
    lots: deque[dict] = deque()
    current: Episode | None = None

    def open_episode(leg: SwapLeg) -> Episode:
        return Episode(
            wallet=wallet, mint=mint,
            opened_at=leg.block_time, opened_slot=leg.slot,
            base_decimals=leg.base_decimals,
            entry_price_usd=leg.price_usd, entry_mcap_usd=leg.mcap_usd,
            entry_liquidity_usd=leg.liquidity_usd,
            entry_token_age_seconds=leg.token_age_seconds,
            entry_slot=leg.slot,
        )

    def close_episode(ep: Episode, leg: SwapLeg) -> None:
        ep.is_closed = True
        ep.closed_at = leg.block_time
        ep.closed_slot = leg.slot
        ep.hold_seconds = _seconds_between(ep.opened_at, leg.block_time)
        ep.realized_pnl_lamports = sum(m.pnl_lamports for m in ep.lot_matches)

        if ep.uncovered_sell_qty_raw > 0:
            # Unknown basis anywhere in the episode makes the whole number a lie.
            ep.realized_pnl_lamports = None
            ep.realized_pnl_usd = None
            ep.roi = None
            ep.pnl_completeness = UNKNOWN
            ep.pnl_limitation = (
                f"{ep.uncovered_sell_qty_raw} raw sold with no swap-acquired basis and "
                f"no recorded transfer-in: inventory from before the observed window, "
                f"or an unrecorded transfer — P&L is not computable")
        elif ep.basis_incomplete:
            ep.realized_pnl_lamports = None
            ep.realized_pnl_usd = None
            ep.roi = None
            ep.pnl_completeness = UNKNOWN
            ep.pnl_limitation = (
                "a buy settled in a quote asset other than native SOL, so its cost "
                "basis cannot be expressed in lamports")
        elif ep.free_inventory_sold_raw > 0 and not ep.lot_matches:
            # Pure dump of free inventory: no purchase P&L exists. Reporting 0
            # would read as break-even instead of "never bought".
            ep.realized_pnl_lamports = None
            ep.realized_pnl_usd = None
            ep.roi = None
            ep.pnl_completeness = PARTIAL
            ep.pnl_limitation = (
                f"nothing was bought: all {ep.free_inventory_sold_raw} raw sold arrived "
                f"by {ep.inventory_origin}, so there is no cost basis and no purchase "
                f"P&L — {ep.free_proceeds_lamports} lamports of proceeds are free-of-cost")
        else:
            if ep.free_inventory_sold_raw > 0:
                ep.pnl_completeness = PARTIAL
                ep.pnl_limitation = (
                    "realized P&L covers swap-acquired inventory only; "
                    f"{ep.free_inventory_sold_raw} raw arrived by {ep.inventory_origin} "
                    "and has no cost basis by design")
            ep.realized_pnl_usd = _usd(ep.realized_pnl_lamports, sol_usd)
            if ep.cost_basis_lamports > 0:
                ep.roi = ep.realized_pnl_lamports / ep.cost_basis_lamports

    for _key, kind, obj in events:
        if kind == "transfer":
            tr: TransferIn = obj  # type: ignore[assignment]
            bucket = "airdrop" if tr.kind in TRANSFER_KINDS_AIRDROP else "transfer"
            free_qty[bucket] += int(tr.amount_raw)
            continue

        leg: SwapLeg = obj  # type: ignore[assignment]

        if leg.side == "buy":
            quote_l = _lamports(leg.quote_amount_raw, leg.quote_decimals)
            if current is None:
                current = open_episode(leg)
                book.episodes.append(current)
            current.total_buy_qty_raw += leg.base_amount_raw
            current.open_qty_raw += leg.base_amount_raw
            current.buy_swap_ids.append(leg.swap_id)
            current.fees_lamports += leg.fee_lamports
            if quote_l is None:
                current.basis_incomplete = True
                book.warnings.append(
                    f"swap {leg.swap_id}: quote has {leg.quote_decimals} decimals "
                    f"(not native SOL) — cost basis cannot be expressed in lamports")
                quote_l = 0
            cost = quote_l + leg.fee_lamports      # a fee paid to acquire is basis
            current.cost_basis_lamports += cost
            lots.append({
                "qty": leg.base_amount_raw, "cost": cost, "swap_id": leg.swap_id,
                "at": leg.block_time, "slot": leg.slot,
            })
            continue

        # ---- a sell -------------------------------------------------------
        if current is None:
            # Sold inventory this window never saw bought. It happened, so it
            # gets an episode; whether its basis is knowable is decided below.
            current = open_episode(leg)
            book.episodes.append(current)

        sell_qty = leg.base_amount_raw
        proceeds_l = _lamports(leg.quote_amount_raw, leg.quote_decimals)
        current.total_sell_qty_raw += sell_qty
        current.sell_swap_ids.append(leg.swap_id)
        current.fees_lamports += leg.fee_lamports
        if proceeds_l is None:
            current.basis_incomplete = True
            proceeds_l = 0
        current.proceeds_lamports += proceeds_l

        remaining = sell_qty
        allocated = 0
        pending: list[LotMatch] = []

        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot["qty"])
            cost = lot["cost"] if take == lot["qty"] else _share(lot["cost"], take, lot["qty"])
            # Proceeds split by quantity; the final slice takes the remainder so
            # the parts always sum to the whole.
            if take == remaining:
                share = proceeds_l - allocated
            else:
                share = _share(proceeds_l, take, sell_qty)
            allocated += share
            pending.append(LotMatch(
                buy_swap_id=lot["swap_id"], sell_swap_id=leg.swap_id,
                matched_qty_raw=take, cost_lamports=cost, proceeds_lamports=share,
                fee_lamports=0, pnl_lamports=share - cost,
                hold_seconds=_seconds_between(lot["at"], leg.block_time),
            ))
            lot["qty"] -= take
            lot["cost"] -= cost
            remaining -= take
            if lot["qty"] == 0:
                lots.popleft()

        # Free inventory covers the rest, at zero basis, proceeds excluded.
        if remaining > 0:
            for bucket in ("transfer", "airdrop"):
                if remaining <= 0 or free_qty[bucket] <= 0:
                    continue
                take = min(remaining, free_qty[bucket])
                free_qty[bucket] -= take
                remaining -= take
                current.free_inventory_sold_raw += take
                current.transfer_in_qty_raw += take
                if bucket == "airdrop":
                    current.inventory_origin = "includes_airdrop"
                elif current.inventory_origin == "swap_only":
                    current.inventory_origin = "includes_transfer_in"
                current.free_proceeds_lamports += _share(proceeds_l, take, sell_qty)

        if remaining > 0:
            current.uncovered_sell_qty_raw += remaining

        # Sell fees land on the matches, remainder to the last (exact total).
        if pending and leg.fee_lamports:
            left = leg.fee_lamports
            total_qty = sum(m.matched_qty_raw for m in pending)
            for i, m in enumerate(pending):
                fee = left if i == len(pending) - 1 else _share(leg.fee_lamports,
                                                               m.matched_qty_raw, total_qty)
                left -= fee
                pending[i] = LotMatch(
                    buy_swap_id=m.buy_swap_id, sell_swap_id=m.sell_swap_id,
                    matched_qty_raw=m.matched_qty_raw, cost_lamports=m.cost_lamports,
                    proceeds_lamports=m.proceeds_lamports, fee_lamports=fee,
                    pnl_lamports=m.proceeds_lamports - m.cost_lamports - fee,
                    hold_seconds=m.hold_seconds)

        current.lot_matches.extend(pending)
        current.open_qty_raw = sum(l["qty"] for l in lots)

        if current.open_qty_raw == 0:
            close_episode(current, leg)
            current = None

    # ---- still open at the end -------------------------------------------
    if current is not None:
        current.is_closed = False
        if mark_price_usd is not None and sol_usd is not None and not current.basis_incomplete:
            qty_ui = Decimal(current.open_qty_raw) / (Decimal(10) ** current.base_decimals)
            basis_usd = (Decimal(current.cost_basis_lamports) / LAMPORTS_PER_SOL) * sol_usd
            mark_usd = qty_ui * Decimal(str(mark_price_usd))
            current.unrealized_pnl_usd = float(mark_usd - basis_usd)
            current.unrealized_price_usd = mark_price_usd
            current.unrealized_as_of = mark_as_of
        else:
            missing = []
            if mark_price_usd is None:
                missing.append("no mark price")
            if sol_usd is None:
                missing.append("no SOL/USD rate")
            if current.basis_incomplete:
                missing.append("incomplete cost basis")
            current.pnl_limitation = ("position is open; unrealized P&L not computed ("
                                      + ", ".join(missing) + ")")

    return book


# ---------------------------------------------------------------------------
# database access
# ---------------------------------------------------------------------------

def load_legs(conn, wallet: str, mint: str) -> list[SwapLeg]:
    """Read one (wallet, mint) swap history in chain order."""
    rows = conn.execute(
        "SELECT id, side, base_amount_raw, quote_amount_raw, base_decimals, "
        "       quote_decimals, slot, event_time, COALESCE(fee_lamports, 0), "
        "       exec_price_usd, mcap_usd, liquidity_usd, token_age_seconds "
        "FROM swaps WHERE trader = %s AND base_mint = %s "
        "ORDER BY slot, id", (wallet, mint)).fetchall()
    return [
        SwapLeg(swap_id=r[0], side=r[1], base_amount_raw=int(r[2]),
                quote_amount_raw=int(r[3]), base_decimals=int(r[4]),
                quote_decimals=int(r[5]), slot=int(r[6]), block_time=r[7],
                fee_lamports=int(r[8]),
                price_usd=float(r[9]) if r[9] is not None else None,
                mcap_usd=float(r[10]) if r[10] is not None else None,
                liquidity_usd=float(r[11]) if r[11] is not None else None,
                token_age_seconds=r[12])
        for r in rows
    ]


def load_transfers(conn, wallet: str, mint: str) -> list[TransferIn]:
    """Token transfers the wallet received (never a cost basis)."""
    rows = conn.execute(
        "SELECT amount_raw, transfer_kind, slot, event_time FROM token_transfers "
        "WHERE to_address = %s AND mint = %s ORDER BY slot",
        (wallet, mint)).fetchall()
    return [TransferIn(amount_raw=int(r[0]), kind=r[1], slot=r[2], event_time=r[3])
            for r in rows]


def persist(conn, episode: Episode) -> int:
    """Write one episode and its FIFO matches; returns position_id."""
    row = conn.execute(
        "INSERT INTO positions (address, chain_id, mint, opened_at, opened_slot, "
        "  closed_at, is_closed, total_buy_qty_raw, total_sell_qty_raw, open_qty_raw, "
        "  cost_basis_lamports, proceeds_lamports, fees_lamports, "
        "  realized_pnl_lamports, realized_pnl_usd, unrealized_pnl_usd, "
        "  unrealized_price_usd, unrealized_as_of, roi, hold_seconds, "
        "  entry_price_usd, entry_mcap_usd, entry_liquidity_usd, "
        "  entry_token_age_seconds, entry_slot, inventory_origin, "
        "  transfer_in_qty_raw, pnl_completeness, pnl_limitation) "
        "VALUES (%s,'solana',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "        %s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING position_id",
        (episode.wallet, episode.mint, episode.opened_at, episode.opened_slot,
         episode.closed_at, episode.is_closed, episode.total_buy_qty_raw,
         episode.total_sell_qty_raw, episode.open_qty_raw,
         episode.cost_basis_lamports, episode.proceeds_lamports,
         episode.fees_lamports, episode.realized_pnl_lamports,
         episode.realized_pnl_usd, episode.unrealized_pnl_usd,
         episode.unrealized_price_usd, episode.unrealized_as_of, episode.roi,
         episode.hold_seconds, episode.entry_price_usd, episode.entry_mcap_usd,
         episode.entry_liquidity_usd, episode.entry_token_age_seconds,
         episode.entry_slot, episode.inventory_origin,
         episode.transfer_in_qty_raw, episode.pnl_completeness,
         episode.pnl_limitation)).fetchone()
    position_id = int(row[0])
    for m in episode.lot_matches:
        if m.buy_swap_id is None or m.sell_swap_id is None:
            continue
        conn.execute(
            "INSERT INTO lot_matches (position_id, buy_swap_id, sell_swap_id, "
            "  matched_qty_raw, cost_lamports, proceeds_lamports, fee_lamports, "
            "  pnl_lamports, hold_seconds) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (buy_swap_id, sell_swap_id, matched_qty_raw) DO NOTHING",
            (position_id, m.buy_swap_id, m.sell_swap_id, m.matched_qty_raw,
             m.cost_lamports, m.proceeds_lamports, m.fee_lamports, m.pnl_lamports,
             m.hold_seconds))
    return position_id


def rebuild(conn, *, wallet: str | None = None, mint: str | None = None,
            limit: int | None = None, log=print) -> tuple[int, int, list[str]]:
    """Re-derive episodes for the (wallet, mint) pairs present in `swaps`.

    Returns (pairs processed, episodes written, warnings). Existing rows for a
    pair are deleted first: `positions`/`lot_matches` are derived, and a late
    swap landing mid-history invalidates the old FIFO assignment, which an
    update-in-place would silently keep.
    """
    where, args = [], []
    if wallet:
        where.append("trader = %s")
        args.append(wallet)
    if mint:
        where.append("base_mint = %s")
        args.append(mint)
    sql = ("SELECT trader, base_mint, count(*) AS n FROM swaps "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "GROUP BY trader, base_mint ORDER BY n DESC")
    if limit:
        sql += " LIMIT %s"
        args.append(limit)
    pairs = conn.execute(sql, tuple(args)).fetchall()

    written, warnings = 0, []
    for addr, token, _n in pairs:
        book = reconstruct(addr, token, load_legs(conn, addr, token),
                           load_transfers(conn, addr, token))
        warnings.extend(f"{addr[:8]}/{token[:8]}: {w}" for w in book.warnings)
        if not book.episodes:
            continue
        # `positions.opened_at` is NOT NULL, and doc 04 is explicit that a row
        # with no event_time is "not yet usable" rather than "now". Skip the
        # pair instead of inventing a timestamp or wiping its existing rows.
        undated = [i for i, ep in enumerate(book.episodes, 1) if ep.opened_at is None]
        if undated:
            warnings.append(
                f"{addr[:8]}/{token[:8]}: episode(s) {undated} have no event_time — "
                f"left as-is, because an invented timestamp would make them "
                f"indistinguishable from measured data")
            continue
        conn.execute("DELETE FROM positions WHERE address = %s AND mint = %s",
                     (addr, token))
        for ep in book.episodes:
            persist(conn, ep)
            written += 1
        conn.commit()
    log(f"rebuilt {written} episode(s) across {len(pairs)} wallet-token pair(s)")
    return len(pairs), written, warnings


def format_book(book: PositionBook) -> str:
    """Human-readable episode table for one wallet + token."""
    lines = [f"{book.wallet} / {book.mint}"]
    if not book.episodes:
        lines.append("  no position episodes (no swaps and no transfers observed)")
    for i, ep in enumerate(book.episodes, 1):
        state = "OPEN" if not ep.is_closed else "closed"
        if ep.realized_pnl_lamports is None:
            pnl = "P&L unknown"
        else:
            pnl = f"{ep.realized_pnl_lamports / 10 ** NATIVE_LAMPORT_DECIMALS:+.9f} SOL"
        lines.append(
            f"  #{i} {state:6} opened {ep.opened_at}  buys={ep.total_buy_qty_raw} "
            f"sells={ep.total_sell_qty_raw} open_qty={ep.open_qty_raw} "
            f"realized={pnl} [{ep.pnl_completeness}/{ep.inventory_origin}]")
        for note in ep.notes():
            lines.append(f"       note: {note}")
    if book.warnings:
        lines.append("  warnings:")
        for w in book.warnings:
            lines.append(f"    - {w}")
    return "\n".join(lines)
