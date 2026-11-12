"""Part enrichment: canonicalisation, spec sheets, sourcing aggregation, research queries.

Reference endpoints this replaces: /api/parts/canonicalize, /api/parts/enrich-transient,
/api/parts/nexar-sourcing, /api/parts/{amazon,adafruit,affiliate}-links.

* ``canonicalize`` resolves any proposed part (name / MPN / dict) to a catalog entry, or builds a
  *transient* ``CatalogPart`` (classified type, parsed dimensions) when the catalog has no match.
* ``enrich_node`` fills ``node.research`` (specs, sourcing offers, datasheet, docs), ``purchaseUrl`` and
  ``estimatedCost`` from pluggable ``SourcingProvider``s.
* ``NexarProvider`` speaks the Nexar (Octopart) GraphQL API through an injected ``transport`` callable so
  it is testable offline; supply a real HTTP transport + token in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import quote_plus

from hwbp.catalog import Catalog
from hwbp.classify import classify_part
from hwbp.mech import format_dims, parse_dims
from hwbp.models import CatalogPart, DocLink, Node, Offer, PriceBreak, Research, Spec
from hwbp.pricing import best_offer, parse_price

_MPN_RE = re.compile(r"\b[A-Z0-9]{2,}[-_/]?[A-Z0-9-]{2,}\b")


@dataclass
class Canonical:
    part: CatalogPart
    matched_by: str  # id | mpn | name | search | transient
    confidence: float


def canonicalize(proposal: str | dict, catalog: Catalog) -> Canonical:
    if isinstance(proposal, dict):
        name = proposal.get("productName") or proposal.get("name") or ""
        mpn = proposal.get("mpn") or ""
        desc = proposal.get("description") or ""
        dims = proposal.get("dimensions")
    else:
        name, mpn, desc, dims = proposal, "", "", None
    for key, how in ((mpn, "mpn"), (name, "name")):
        if not key:
            continue
        k = key.strip().lower()
        for p in catalog.all():
            if k in (p.id.lower(), (p.mpn or "").lower()):
                return Canonical(p, "id" if k == p.id.lower() else "mpn", 1.0)
            if k == p.name.lower():
                return Canonical(p, how, 0.95)
    # MPN-looking tokens inside the name (e.g. "Waveshare 3.5inch ... ST7796")
    for tok in _MPN_RE.findall(f"{name} {mpn}".upper()):
        for p in catalog.all():
            if p.mpn and tok in p.mpn.upper():
                return Canonical(p, "mpn", 0.85)
    hits = catalog.search(f"{name} {desc}", limit=1)
    if hits and hits[0][1] >= 0.75:
        return Canonical(hits[0][0], "search", round(min(0.8, hits[0][1]), 2))
    cls = classify_part(name, desc)
    d = parse_dims(dims) if isinstance(dims, str) else None
    transient = CatalogPart(
        id=f"transient-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'part'}",
        type=cls.subtype if cls.subtype not in ("sensor", "module", "mechanism", "enclosure") else cls.type,
        category=cls.category,
        name=name or "Unknown part",
        mpn=mpn or None,
        description=desc,
        dims_mm=d,
        tags=["transient"],
        popularity=0.3,
    )
    return Canonical(transient, "transient", cls.confidence * 0.5)


# ------------------------------------------------------------------------------ sourcing providers
class SourcingProvider(Protocol):
    name: str

    def offers(self, part: CatalogPart) -> list[Offer]: ...


@dataclass
class CatalogSourcing:
    name: str = "catalog"

    def offers(self, part: CatalogPart) -> list[Offer]:
        return list(part.offers)


@dataclass
class SearchLinkSourcing:
    """Plain (non-affiliate) marketplace search links — useful fallbacks when no priced offer exists."""

    name: str = "search-links"
    vendors: tuple[str, ...] = ("Amazon", "AliExpress", "eBay")

    def offers(self, part: CatalogPart) -> list[Offer]:
        q = quote_plus(part.mpn or part.name)
        urls = {
            "Amazon": f"https://www.amazon.com/s?k={q}",
            "AliExpress": f"https://www.aliexpress.com/wholesale?SearchText={q}",
            "eBay": f"https://www.ebay.com/sch/i.html?_nkw={q}",
        }
        return [Offer(vendor=v, url=urls[v]) for v in self.vendors]


NEXAR_QUERY = """query Search($q: String!, $limit: Int!) {
  supSearchMpn(q: $q, limit: $limit) {
    results { part { mpn manufacturer { name } shortDescription bestDatasheet { url }
      sellers(authorizedOnly: false) { company { name } offers { clickUrl inventoryLevel moq packaging
        prices { quantity price currency } } } } }
  }
}"""


@dataclass
class NexarProvider:
    """Nexar/Octopart GraphQL sourcing. ``transport(query, variables) -> dict`` performs the HTTP POST."""

    transport: Callable[[str, dict], dict]
    name: str = "nexar"
    limit: int = 3
    max_offers: int = 30

    def offers(self, part: CatalogPart) -> list[Offer]:
        if not (part.mpn or part.name):
            return []
        data = self.transport(NEXAR_QUERY, {"q": part.mpn or part.name, "limit": self.limit})
        return self.parse(data)[: self.max_offers]

    @staticmethod
    def parse(data: dict) -> list[Offer]:
        out: list[Offer] = []
        results = (((data or {}).get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        for r in results:
            for seller in (r.get("part") or {}).get("sellers") or []:
                vendor = (seller.get("company") or {}).get("name") or "unknown"
                for o in seller.get("offers") or []:
                    prices = sorted(o.get("prices") or [], key=lambda p: p.get("quantity") or 0)
                    if not prices:
                        continue
                    cur = prices[0].get("currency") or "USD"
                    out.append(
                        Offer(
                            vendor=vendor,
                            url=o.get("clickUrl"),
                            price=f"{prices[0]['price']}",
                            currency=cur,
                            price_breaks=[
                                PriceBreak(qty=int(p["quantity"]), unit_price=float(p["price"]))
                                for p in prices
                                if p.get("price") is not None
                            ],
                            moq=o.get("moq"),
                            stock=o.get("inventoryLevel"),
                        )  # fmt: skip
                    )
        return out

    @staticmethod
    def datasheet(data: dict) -> str | None:
        for r in (((data or {}).get("data") or {}).get("supSearchMpn") or {}).get("results") or []:
            url = ((r.get("part") or {}).get("bestDatasheet") or {}).get("url")
            if url:
                return url
        return None


# ------------------------------------------------------------------------------ specs + enrichment
def spec_sheet(part: CatalogPart) -> list[Spec]:
    """Structured label/value specs in the reference ``research.specs`` style."""
    s: list[tuple[str, str]] = []
    if part.mcu:
        s.append(("pins", ",".join(p.name for p in part.mcu.pins[:48])))
        s.append(("cpu_mhz", str(part.mcu.cpu_mhz)))
        s.append(("radios", ",".join(part.mcu.radios) or "none"))
        s.append(("logic_v", f"{part.mcu.logic_v:g}"))
    for itf in part.interfaces:
        s.append((f"interface_{itf.protocol}", ",".join(itf.pins) + (f" @{itf.address}" if itf.address else "")))
    for pi in part.power_inputs:
        s.append((f"supply_{pi.pin}", f"{pi.vmin:g}-{pi.vmax:g}V"))
    if part.power_output:
        po = part.power_output
        s.append(("topology", po.kind))
        s.append(("vin", f"{po.vin_min:g}-{po.vin_max:g}V"))
        if po.vout is not None:
            s.append(("vout", f"{po.vout:g}V"))
        s.append(("imax_ma", f"{po.imax_ma:g}"))
    if part.battery:
        b = part.battery
        s += [("chemistry", b.chemistry), ("nominal_v", f"{b.nominal_v:g}"), ("capacity_mah", f"{b.capacity_mah:g}")]
    if part.drive:
        d = part.drive
        s += [("drive", d.kind), ("voltage", f"{d.vmin:g}-{d.vmax:g}V"), ("stall_ma", f"{d.stall_ma:g}")]
    for k in ("typ", "peak", "sleep"):
        if k in part.current_ma:
            s.append((f"current_{k}_ma", f"{part.current_ma[k]:g}"))
    if part.dims_mm:
        w, h, d = part.dims_mm
        s += [("width_mm", f"{w:g}"), ("depth_mm", f"{d:g}"), ("height_mm", f"{h:g}")]
    s += list(part.specs.items())
    return [Spec(label=k, value=v) for k, v in s]


def enrich_node(node: Node, part: CatalogPart, providers: list[SourcingProvider] | None = None) -> Node:
    providers = providers or [CatalogSourcing(), SearchLinkSourcing()]
    offers: list[Offer] = []
    seen: set[str] = set()
    for prov in providers:
        try:
            for o in prov.offers(part):
                key = (o.url or "") + o.vendor
                if key not in seen:
                    seen.add(key)
                    offers.append(o)
        except Exception:  # noqa: BLE001 - one failing provider must not break enrichment
            continue
    priced = [o for o in offers if o.price or o.price_breaks]
    priced.sort(key=lambda o: (parse_price(o.price)[0] if o.price else o.price_breaks[0].unit_price) or 1e9)
    offers = priced + [o for o in offers if o not in priced]
    docs = [DocLink(url=part.datasheet, title="Datasheet")] if part.datasheet else []
    node.research = Research(
        specs=spec_sheet(part), sourcing=offers, datasheet_url=part.datasheet, documentation_urls=docs
    )
    q = best_offer(priced, max(1, node.quantity))
    if q is not None:
        node.purchase_url = q.offer.url
        if node.estimated_cost is None:
            node.estimated_cost = round(q.unit_usd, 2)
    if node.dimensions is None and part.dims_mm:
        node.dimensions = format_dims(*part.dims_mm)
    return node


RESEARCH_QUERIES = {
    "mcu": ["{mpn} datasheet pdf", "{mpn} pinout", "{mpn} platformio board"],
    "sensor": ["{mpn} datasheet", "{mpn} i2c address", "{mpn} arduino library"],
    "display": ["{mpn} datasheet controller", "{mpn} pinout spi", "{mpn} TFT_eSPI setup"],
    "power": ["{mpn} datasheet efficiency", "{mpn} module pinout", "{mpn} max current"],
    "actuator": ["{mpn} stall current", "{mpn} datasheet torque", "{mpn} wiring"],
    "module": ["{mpn} datasheet", "{mpn} pinout", "{mpn} library"],
}


@dataclass
class ResearchPlan:
    queries: list[str] = field(default_factory=list)


def research_queries(node: Node) -> ResearchPlan:
    """Web-research queries per part type (feed to a search API / LLM researcher)."""
    from hwbp.models import canonical_type

    key = node.product_name or node.name
    tmpl = RESEARCH_QUERIES.get(canonical_type(node.type), ["{mpn} specifications", "{mpn} buy"])
    return ResearchPlan([t.format(mpn=key) for t in tmpl])
