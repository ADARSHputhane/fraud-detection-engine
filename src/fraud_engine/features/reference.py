"""Static reference data: datacenter IP ranges and the BIN risk table.

Both ship as plain text files next to this module so they can be reviewed in a diff and
swapped for a maintained feed without touching code.
"""

from __future__ import annotations

import csv
import ipaddress
from functools import lru_cache
from importlib import resources

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Used when a BIN isn't in the table. Prepaid cards are anonymous and cheap to buy in bulk,
# which is why card-testing rings favour them.
DEFAULT_BIN_RISK = {"prepaid": 0.6, "credit": 0.25, "debit": 0.2}
UNKNOWN_CARD_TYPE_RISK = 0.3


def _reference_lines(name: str) -> list[str]:
    text = resources.files(__package__).joinpath("reference", name).read_text(encoding="utf-8")
    return [line.split("#", 1)[0].strip() for line in text.splitlines()]


class DatacenterRanges:
    """Cloud / hosting IP ranges. Real customers rarely pay from these; fraud tooling does."""

    def __init__(self, networks: list[IPNetwork]) -> None:
        self.networks = networks
        self.contains = lru_cache(maxsize=65_536)(self._contains)

    @classmethod
    def default(cls) -> DatacenterRanges:
        lines = _reference_lines("datacenter_cidrs.txt")
        return cls([ipaddress.ip_network(line) for line in lines if line])

    def _contains(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr.version == net.version and addr in net for net in self.networks)


class BinRiskTable:
    """BIN prefix -> risk in [0, 1], longest prefix wins; falls back to a card-type default."""

    def __init__(self, entries: dict[str, float]) -> None:
        self.entries = entries
        self.lengths = sorted({len(p) for p in entries}, reverse=True)

    @classmethod
    def default(cls) -> BinRiskTable:
        rows = csv.DictReader(line for line in _reference_lines("bin_risk.csv") if line)
        return cls({row["prefix"]: float(row["risk"]) for row in rows})

    def risk(self, bin_: str | None, card_type: str | None) -> float:
        if bin_:
            for n in self.lengths:
                hit = self.entries.get(bin_[:n])
                if hit is not None:
                    return hit
        return DEFAULT_BIN_RISK.get(card_type or "", UNKNOWN_CARD_TYPE_RISK)
