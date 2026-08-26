"""Demo portfolio: real assets, real coordinates, plausible financials.

Coordinates are genuine so the hazard lookup returns genuine values. The
financials are illustrative and labelled as such in the API response, because
inventing a balance sheet and presenting it as fact is exactly the thing this
product exists to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Asset:
    id: str
    name: str
    country: str
    lon: float
    lat: float
    sector: str
    occupancy: str          # maps to a JRC/HAZUS damage-curve occupancy class
    region: str             # continent, selects the JRC regional curve
    value: float            # USD
    annual_revenue: float
    debt: float
    annual_debt_service: float

    def as_dict(self) -> dict:
        return asdict(self)


# Region strings match the JRC continental damage-function groups.
DEMO_ASSETS: list[Asset] = [
    Asset("tj-priok", "Tanjung Priok Container Terminal", "Indonesia",
          106.8806, -6.1045, "Transport", "Transport", "Asia",
          420_000_000, 96_000_000, 252_000_000, 21_000_000),
    Asset("cilegon", "Cilegon Steel Complex", "Indonesia",
          106.0640, -5.9800, "Heavy industry", "Industrial", "Asia",
          680_000_000, 310_000_000, 408_000_000, 34_000_000),
    Asset("cat-lai", "Cat Lai Port Terminal", "Vietnam",
          106.7770, 10.7690, "Transport", "Transport", "Asia",
          310_000_000, 74_000_000, 186_000_000, 15_500_000),
    Asset("bangkok-lp", "Bang Na Logistics Park", "Thailand",
          100.6100, 13.6800, "Logistics", "Industrial", "Asia",
          180_000_000, 41_000_000, 117_000_000, 9_800_000),
    Asset("port-klang", "Port Klang West Terminal", "Malaysia",
          101.3900, 3.0000, "Transport", "Transport", "Asia",
          395_000_000, 88_000_000, 217_250_000, 18_100_000),
    Asset("manila-pp", "Manila Bay Power Station", "Philippines",
          120.9500, 14.5400, "Power", "Infrastructure", "Asia",
          520_000_000, 143_000_000, 338_000_000, 28_200_000),
    Asset("chennai-auto", "Chennai Automotive Plant", "India",
          80.2200, 13.0000, "Manufacturing", "Industrial", "Asia",
          340_000_000, 165_000_000, 187_000_000, 15_600_000),
    Asset("osaka-log", "Osaka Bay Distribution Hub", "Japan",
          135.4300, 34.6500, "Logistics", "Industrial", "Asia",
          290_000_000, 62_000_000, 145_000_000, 12_100_000),
    Asset("hcmc-tower", "Thu Thiem Office Tower", "Vietnam",
          106.7009, 10.7769, "Real estate", "Commercial", "Asia",
          215_000_000, 26_000_000, 150_500_000, 12_500_000),
    Asset("rotterdam-chem", "Botlek Chemical Terminal", "Netherlands",
          4.4034, 51.9244, "Chemicals", "Industrial", "Europe",
          740_000_000, 355_000_000, 444_000_000, 37_000_000),
    Asset("hamburg-dc", "Hamburg Distribution Centre", "Germany",
          9.9937, 53.5511, "Logistics", "Commercial", "Europe",
          260_000_000, 58_000_000, 156_000_000, 13_000_000),
    Asset("houston-ref", "Houston Ship Channel Refinery", "United States",
          -95.2637, 29.7355, "Refining", "Industrial", "North America",
          980_000_000, 620_000_000, 588_000_000, 49_000_000),
]

DEMO_PORTFOLIO = {
    "id": "demo",
    "name": "Asia-Pacific Industrial & Infrastructure",
    "currency": "USD",
    "note": "Asset locations are real. Financials are illustrative.",
}


def by_id(asset_id: str) -> Asset | None:
    for a in DEMO_ASSETS:
        if a.id == asset_id:
            return a
    return None


def demo() -> None:
    ids = [a.id for a in DEMO_ASSETS]
    assert len(ids) == len(set(ids)), "asset ids must be unique"
    for a in DEMO_ASSETS:
        assert -180 <= a.lon <= 180, f"{a.id} longitude out of range"
        assert -90 <= a.lat <= 90, f"{a.id} latitude out of range"
        assert a.value > 0, f"{a.id} needs a positive value"
        assert a.debt <= a.value, f"{a.id} is already underwater before any hazard"
        assert a.annual_debt_service > 0
        assert a.region in {"Asia", "Europe", "North America",
                            "Africa", "Oceania", "South America"}
    assert by_id("demo-nope") is None
    assert by_id("tj-priok").country == "Indonesia"
    print(f"portfolio.py self-check passed ({len(DEMO_ASSETS)} assets, "
          f"${sum(a.value for a in DEMO_ASSETS)/1e9:.2f}bn)")


if __name__ == "__main__":
    demo()
