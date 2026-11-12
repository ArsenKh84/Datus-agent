import pytest

from hwbp.models import Node, Offer, PriceBreak, ProjectContent, Research
from hwbp.pricing import (
    best_offer,
    build_bom,
    consolidate_vendors,
    format_money,
    parse_infill,
    parse_price,
    print_cost,
    quote_offer,
    shipping_total,
    unit_price_at,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$5.5961", (5.5961, "USD")),
        ("US $1.23", (1.23, "USD")),
        ("€3,20", (3.2, "EUR")),
        ("1.234,50 EUR", (1234.5, "EUR")),
        ("£1,234.5", (1234.5, "GBP")),
        ("1,234", (1234.0, "USD")),
        (7, (7.0, "USD")),
        (None, (None, "USD")),
        ("call us", (None, "USD")),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_format_money():
    assert format_money(1234.5, approx=True) == "~$1,234.50"
    assert format_money(2, "CHF") == "2.00 CHF"


def test_price_breaks_moq_and_pack():
    o = Offer(
        vendor="DigiKey",
        price_breaks=[PriceBreak(qty=1, unit_price=1.0), PriceBreak(qty=10, unit_price=0.5)],
        moq=5,
        pack_size=5,
    )
    assert unit_price_at(o, 1) == 1.0 and unit_price_at(o, 12) == 0.5
    q = quote_offer(o, 7)  # moq 5, pack 5 -> order 10 at the 10-break
    assert q.order_qty == 10 and q.unit_usd == 0.5 and q.total_usd == 5.0
    assert "MOQ/pack" in q.reasons[0]
    assert quote_offer(Offer(vendor="x", price="n/a"), 1) is None
    assert unit_price_at(Offer(vendor="x", price="$2"), 3) == 2.0


def test_best_offer_prefers_stock_then_trust_adjusted_price():
    cheap_no_stock = Offer(vendor="AliExpress", price="$1.00", stock=0)
    stocked = Offer(vendor="DigiKey", price="$1.50", stock=100)
    assert best_offer([cheap_no_stock, stocked], 2).offer.vendor == "DigiKey"
    assert best_offer([cheap_no_stock], 2).offer.vendor == "AliExpress"  # nothing in stock -> still quote
    assert best_offer([], 1) is None


def test_consolidate_vendors_moves_lines_when_shipping_saves_money():
    a = quote_offer(Offer(vendor="DigiKey", price="$1.00"), 1)
    a2 = quote_offer(Offer(vendor="Mouser", price="$0.90"), 1)
    b = quote_offer(Offer(vendor="DigiKey", price="$2.00"), 1)
    picked = consolidate_vendors({"l1": [a, a2], "l2": [b]})
    assert {q.offer.vendor for q in picked.values()} == {"DigiKey"}  # +$0.10 beats $7.99 Mouser shipping
    assert shipping_total(picked.values()) == pytest.approx(6.99)


def test_print_cost_and_infill():
    assert parse_infill("40% infill, 0.15mm") == 0.4 and parse_infill(None) == 0.2
    est = print_cost(20000, 8000, "Carbon Fiber PETG", "40% infill")
    assert est["filament"] == "CARBON FIBER PETG" and est["grams"] > 0 and est["cost_usd"] > 0
    assert print_cost(1000, 100, "unobtainium")["filament"] == "PLA"


def test_bom_rollup_matches_reference_semantics(reference_content):
    bom = build_bom(reference_content.nodes, use_offers=False)
    assert bom.parts_total_usd == pytest.approx(12.5 + 3.0 + 6.0 + 1.0 + 0.8 + 4.0)
    roll = {r["category"]: r for r in bom.rollup()}
    assert roll["electrical"]["parts"] == 2 and roll["electrical"]["cost"] == pytest.approx(10.0)
    misc = next(t for t in roll["mechanical"]["types"] if t["type"] == "misc")
    assert misc["parts"] == 1 and misc["cost"] == pytest.approx(0.8)  # line count, not quantity
    assert all(ln.estimate for ln in bom.lines)
    csv = bom.to_csv()
    assert csv.splitlines()[0].startswith("node_id,name") and "total" in csv


def test_bom_uses_offers_and_build_quantity():
    n = Node(id="r", name="Res", type="module", quantity=2, estimated_cost=9.0,
             research=Research(sourcing=[Offer(vendor="LCSC", price_breaks=[PriceBreak(qty=1, unit_price=0.5), PriceBreak(qty=100, unit_price=0.1)])]))  # fmt: skip
    bom = build_bom([n], build_qty=50)
    line = bom.lines[0]
    assert line.qty == 100 and line.unit_usd == pytest.approx(0.1) and not line.estimate
    assert bom.shipping_usd == pytest.approx(8.0) and bom.total_usd == pytest.approx(10.0 + 8.0)
    assert build_bom([n], consolidate=False).lines[0].vendor == "LCSC"


def test_reference_doc_roundtrip_keeps_unknown_fields(reference_doc):
    content = ProjectContent.model_validate(reference_doc)
    out = content.dump()
    assert out["unknownFutureField"] == {"kept": True}
    assert out["nodes"][2]["research"]["sourcing"][0]["price"] == "$5.5961"
    assert out["mechanicalConnections"][0]["delta"] == {"x": 0.0, "y": -11.5, "z": 0.0}
