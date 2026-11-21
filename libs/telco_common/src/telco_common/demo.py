"""Demo subscriber population (shared by the emulator and core-api seed).

Each persona tells a story the bill explainer should surface:
  streamer        - data grows every month, hits 80%/100% alerts late in the cycle
  traveller       - roams outside the EU in July and October -> bill spikes, "why did it go up"
  intl_caller     - Polish customer calling abroad, PLN + 23% VAT
  addon_buyer     - Flex plan, buys a 5 GB add-on in some months instead of paying overage
  light           - stays inside the plan, flat bill
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Country


@dataclass(frozen=True)
class DemoSubscriber:
    subscriber_id: str
    name: str
    email: str
    country: Country
    plan_code: str
    persona: str


DEMO_SUBSCRIBERS: list[DemoSubscriber] = [
    DemoSubscriber("4915100000001", "Lena Fischer", "lena.fischer@example.com", Country.DE, "DE-M", "streamer"),
    DemoSubscriber("4915100000002", "Jonas Weber", "jonas.weber@example.com", Country.DE, "DE-FLEX", "traveller"),
    DemoSubscriber("4915100000003", "Mia Schulz", "mia.schulz@example.com", Country.DE, "DE-FLEX", "addon_buyer"),
    DemoSubscriber("4915100000004", "Paul Becker", "paul.becker@example.com", Country.DE, "DE-S", "light"),
    DemoSubscriber("48600000001", "Zofia Nowak", "zofia.nowak@example.com", Country.PL, "PL-FLEX", "intl_caller"),
    DemoSubscriber("48600000002", "Piotr Kowalski", "piotr.kowalski@example.com", Country.PL, "PL-S", "streamer"),
]

BY_ID = {s.subscriber_id: s for s in DEMO_SUBSCRIBERS}
