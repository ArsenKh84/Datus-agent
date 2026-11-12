"""Price parsing, offer selection, BOM costing and roll-ups.

Algorithm summary
-----------------
1. ``parse_price`` normalises vendor price strings ("$5.5961", "US $1.23", "€3,20", "1.234,50 EUR").
2. ``best_offer`` evaluates every sourcing offer for a required quantity:
   order_qty = max(qty, moq) rounded up to the pack multiple; unit price = the price break
   that applies at order_qty; total = order_qty * unit.  Out-of-stock offers are only used
   when nothing is in stock.  Ranking key = total cost * vendor trust penalty.
3. ``consolidate_vendors`` (BOM level) greedily moves lines onto vendors that are already
   being used when the extra line cost is lower than that vendor's shipping fee — fewer
   parcels usually beats a few cents per line.
4. ``build_bom`` produces lines (qty, unit, subtotal, "~" estimate flag) and a
   category -> type -> part roll-up tree like the INFO tab of the reference product.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Iterable

from hwbp.models import Node, Offer, PriceBreak

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₽": "RUB", "₹": "INR"}
# Approximate FX to USD — only used to rank offers in mixed currencies; replace with a live rate provider.
FX_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "RUB": 0.011, "INR": 0.012, "CNY": 0.14}
# Vendor trust multipliers: >1 penalises a vendor when ranking (counterfeit risk, slow shipping, ...).
VENDOR_TRUST = {
    "digikey": 1.0,
    "mouser": 1.0,
    "lcsc": 1.02,
    "adafruit": 1.0,
    "sparkfun": 1.0,
    "amazon": 1.05,
    "aliexpress": 1.12,
    "ebay": 1.15,
}
DEFAULT_SHIPPING = {"digikey": 6.99, "mouser": 7.99, "lcsc": 8.0, "adafruit": 9.0, "amazon": 0.0, "aliexpress": 2.0}

_NUM_RE = re.compile(r"[-+]?\d[\d.,\s]*")


def parse_price(text: str | float | int | None) -> tuple[float | None, str]:
    """Parse a price string into ``(amount, currency)``; amount is None when unparseable."""
    if text is None:
        return None, "USD"
    if isinstance(text, (int, float)):
        return float(text), "USD"
    s = text.strip()
    currency = "USD"
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            currency = code
            break
    m_code = re.search(r"\b(USD|EUR|GBP|JPY|RUB|INR|CNY)\b", s, re.I)
    if m_code:
        currency = m_code.group(1).upper()
    m = _NUM_RE.search(s)
    if not m:
        return None, currency
    num = m.group(0).replace(" ", "").strip()
    num = _normalise_decimal(num)
    try:
        return float(num), currency
    except ValueError:
        return None, currency


def _normalise_decimal(num: str) -> str:
    """Handle both 1,234.56 and 1.234,56 and 3,20 styles."""
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):  # european: 1.234,56
            return num.replace(".", "").replace(",", ".")
        return num.replace(",", "")  # us: 1,234.56
    if "," in num:
        head, _, tail = num.rpartition(",")
        if len(tail) == 3 and head:  # thousands separator: 1,234
            return num.replace(",", "")
        return num.replace(",", ".")  # decimal comma: 3,20
    return num


def to_usd(amount: float, currency: str) -> float:
    return amount * FX_TO_USD.get(currency.upper(), 1.0)


def format_money(amount: float, currency: str = "USD", approx: bool = False) -> str:
    symbol = {v: k for k, v in CURRENCY_SYMBOLS.items()}.get(currency, "")
    prefix = "~" if approx else ""
    return f"{prefix}{symbol}{amount:,.2f}" if symbol else f"{prefix}{amount:,.2f} {currency}"


# ---------------------------------------------------------------------- offers
@dataclass
class OfferQuote:
    offer: Offer
    order_qty: int
    unit_usd: float
    total_usd: float
    in_stock: bool
    score: float
    reasons: list[str] = field(default_factory=list)


def unit_price_at(offer: Offer, qty: int) -> float | None:
    """Unit price for ``qty`` from the offer's price breaks (largest break <= qty)."""
    breaks: list[PriceBreak] = sorted(offer.price_breaks, key=lambda b: b.qty)
    if not breaks:
        amount, _ = parse_price(offer.price)
        return amount
    applicable = [b for b in breaks if b.qty <= qty]
    return (applicable[-1] if applicable else breaks[0]).unit_price


def quote_offer(offer: Offer, qty: int) -> OfferQuote | None:
    qty = max(1, int(qty))
    order_qty = max(qty, offer.moq or 1)
    pack = max(1, offer.pack_size or 1)
    order_qty = int(math.ceil(order_qty / pack) * pack)
    unit = unit_price_at(offer, order_qty)
    if unit is None:
        return None
    unit_usd = to_usd(unit, offer.currency)
    total = unit_usd * order_qty
    in_stock = offer.stock is None or offer.stock >= order_qty
    trust = VENDOR_TRUST.get(offer.vendor.lower().split()[0], 1.08)
    reasons = []
    if order_qty > qty:
        reasons.append(f"order {order_qty} (MOQ/pack) for {qty} needed")
    if not in_stock:
        reasons.append("insufficient stock")
    return OfferQuote(offer, order_qty, unit_usd, total, in_stock, total * trust, reasons)


def best_offer(offers: Iterable[Offer], qty: int) -> OfferQuote | None:
    quotes = [q for q in (quote_offer(o, qty) for o in offers) if q is not None]
    if not quotes:
        return None
    stocked = [q for q in quotes if q.in_stock]
    pool = stocked or quotes
    return min(pool, key=lambda q: (q.score, q.total_usd))


def consolidate_vendors(
    choices: dict[str, list[OfferQuote]], shipping: dict[str, float] | None = None
) -> dict[str, OfferQuote]:
    """Pick one quote per BOM line minimising sum(line totals) + sum(shipping per distinct vendor).

    ``choices`` maps line id -> candidate quotes (any order). Greedy: start from the cheapest quote per
    line, then repeatedly move a line to an already-used vendor if that removes a vendor whose
    shipping fee exceeds the price increase. Converges because every accepted move lowers cost.
    """
    ship = {k.lower(): v for k, v in (shipping or DEFAULT_SHIPPING).items()}

    def vkey(q: OfferQuote) -> str:
        return q.offer.vendor.lower().split()[0]

    picked = {line: min(qs, key=lambda q: q.total_usd) for line, qs in choices.items() if qs}
    improved = True
    while improved:
        improved = False
        vendors_used = {vkey(q) for q in picked.values()}
        for vendor in sorted(vendors_used, key=lambda v: -ship.get(v, 5.0)):
            lines = [ln for ln, q in picked.items() if vkey(q) == vendor]
            others = vendors_used - {vendor}
            moves, extra = {}, 0.0
            for ln in lines:
                alts = [q for q in choices[ln] if vkey(q) in others and q.in_stock]
                if not alts:
                    break
                alt = min(alts, key=lambda q: q.total_usd)
                moves[ln] = alt
                extra += alt.total_usd - picked[ln].total_usd
            else:
                if moves and extra < ship.get(vendor, 5.0):
                    picked.update(moves)
                    improved = True
                    break
    return picked


def shipping_total(picked: Iterable[OfferQuote], shipping: dict[str, float] | None = None) -> float:
    ship = {k.lower(): v for k, v in (shipping or DEFAULT_SHIPPING).items()}
    vendors = {q.offer.vendor.lower().split()[0] for q in picked}
    return sum(ship.get(v, 5.0) for v in vendors)


# ---------------------------------------------------------------------- 3D printing cost
FILAMENTS = {
    # name: (density g/cm3, USD per kg)
    "PLA": (1.24, 20.0),
    "PETG": (1.27, 22.0),
    "ABS": (1.04, 21.0),
    "ASA": (1.07, 28.0),
    "TPU": (1.21, 35.0),
    "CARBON FIBER PETG": (1.30, 45.0),
    "CARBON FIBER PLA": (1.29, 40.0),
    "NYLON": (1.14, 50.0),
}


def filament_for(material: str | None) -> tuple[str, float, float]:
    key = (material or "PLA").upper().replace("-", " ")
    for name in sorted(FILAMENTS, key=len, reverse=True):
        if name in key:
            return (name, *FILAMENTS[name])
    return ("PLA", *FILAMENTS["PLA"])


def parse_infill(print_settings: str | None, default: float = 0.2) -> float:
    m = re.search(r"(\d{1,3})\s*%", print_settings or "")
    return min(1.0, int(m.group(1)) / 100) if m else default


def print_cost(
    solid_volume_mm3: float,
    surface_area_mm2: float,
    material: str | None,
    print_settings: str | None = None,
    wall_mm: float = 1.2,
    machine_rate_usd_h: float = 0.35,
    flow_mm3_s: float = 8.0,
) -> dict:
    """Estimate filament mass, print time and cost of an FDM part.

    Effective printed volume = shell (surface area * perimeter thickness, capped at the part volume)
    + infill fraction of the remaining core volume.
    """
    name, density, usd_kg = filament_for(material)
    infill = parse_infill(print_settings)
    shell = min(solid_volume_mm3, surface_area_mm2 * wall_mm)
    printed = shell + infill * max(0.0, solid_volume_mm3 - shell)
    grams = printed / 1000.0 * density
    hours = printed / flow_mm3_s / 3600.0
    cost = grams / 1000.0 * usd_kg + hours * machine_rate_usd_h
    return {
        "filament": name,
        "infill": infill,
        "grams": round(grams, 1),
        "hours": round(hours, 2),
        "cost_usd": round(cost, 2),
    }


# ---------------------------------------------------------------------- BOM
@dataclass
class BomLine:
    node_id: str
    name: str
    product_name: str | None
    category: str
    type: str
    qty: int
    unit_usd: float
    subtotal_usd: float
    estimate: bool  # True => no live/verified offer, value is an estimate ("~")
    vendor: str | None = None
    url: str | None = None
    order_qty: int | None = None


@dataclass
class Bom:
    lines: list[BomLine]
    build_qty: int
    shipping_usd: float = 0.0

    @property
    def parts_total_usd(self) -> float:
        return round(sum(line.subtotal_usd for line in self.lines), 2)

    @property
    def total_usd(self) -> float:
        return round(self.parts_total_usd + self.shipping_usd, 2)

    def rollup(self) -> list[dict]:
        """category -> type -> lines tree with counts (sum of qty) and costs (INFO tab view)."""
        tree: dict[str, dict[str, list[BomLine]]] = {}
        for line in self.lines:
            tree.setdefault(line.category, {}).setdefault(line.type, []).append(line)
        out = []
        for cat in sorted(tree, key=lambda c: (c != "electrical", c)):
            types = []
            for t, lines in tree[cat].items():
                types.append(
                    {
                        "type": t,
                        "parts": len(lines),  # line count (leaf rows show qty), as in the reference INFO tab
                        "cost": round(sum(ln.subtotal_usd for ln in lines), 2),
                        "lines": [ln.node_id for ln in lines],
                    }
                )
            out.append(
                {
                    "category": cat,
                    "parts": len([ln for ts in tree[cat].values() for ln in ts]),
                    "cost": round(sum(t["cost"] for t in types), 2),
                    "types": types,
                }
            )
        return out

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["node_id", "name", "product", "category", "type", "qty", "order_qty", "unit_usd", "subtotal_usd"])
        for ln in self.lines:
            w.writerow(
                [ln.node_id, ln.name, ln.product_name or "", ln.category, ln.type, ln.qty, ln.order_qty or ln.qty]
                + [f"{ln.unit_usd:.4f}", f"{ln.subtotal_usd:.2f}"]
            )
        w.writerow([])
        w.writerow(["", "", "", "", "", "", "", "parts_total", f"{self.parts_total_usd:.2f}"])
        w.writerow(["", "", "", "", "", "", "", "shipping", f"{self.shipping_usd:.2f}"])
        w.writerow(["", "", "", "", "", "", "", "total", f"{self.total_usd:.2f}"])
        return buf.getvalue()


def build_bom(nodes: list[Node], build_qty: int = 1, consolidate: bool = True, use_offers: bool = True) -> Bom:
    """Cost every node for ``build_qty`` units of the device.

    Unit price source priority: best live offer (research.sourcing / offers) -> node.estimated_cost.
    Lines priced from estimated_cost are flagged ``estimate`` and rendered with "~".
    ``use_offers=False`` reproduces the reference product, which displays estimatedCost only.
    """
    candidates: dict[str, list[OfferQuote]] = {}
    for node in nodes:
        need = node.quantity * build_qty
        offers = node.all_offers()
        quotes = [q for q in (quote_offer(o, need) for o in offers) if q is not None]
        if quotes:
            candidates[node.id] = quotes
    picked = (
        consolidate_vendors(candidates)
        if consolidate
        else {k: min(v, key=lambda q: q.total_usd) for k, v in candidates.items()}
    )
    lines = []
    for node in nodes:
        need = node.quantity * build_qty
        q = picked.get(node.id)
        if q is not None and use_offers:
            lines.append(
                BomLine(
                    node.id, node.name, node.product_name, node.category, node.type, need,
                    round(q.unit_usd, 4), round(q.total_usd, 2), False, q.offer.vendor, q.offer.url, q.order_qty,
                )
            )  # fmt: skip
        else:
            unit = float(node.estimated_cost or 0.0)
            lines.append(
                BomLine(
                    node.id, node.name, node.product_name, node.category, node.type, need,
                    unit, round(unit * need, 2), True, None, node.purchase_url, need,
                )
            )  # fmt: skip
    ship = shipping_total(
        [q for nid, q in picked.items() if any(ln.node_id == nid and not ln.estimate for ln in lines)]
    )
    return Bom(lines, build_qty, round(ship, 2))
