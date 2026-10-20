from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Line:
    line_no: int
    raw: str
    part: Optional[str]
    qty: Optional[int]
    price: Optional[float] = None
    description: Optional[str] = None


@dataclass
class Extracted:
    lines: list
    po_number: Optional[str] = None
    requested_date: Optional[str] = None      # ISO yyyy-mm-dd
    ship_to_text: Optional[str] = None
    customer_hint: dict = field(default_factory=dict)   # email / account_no
    extractor: str = "rules"
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "Extracted":
        d = json.loads(s)
        d["lines"] = [Line(**l) for l in d["lines"]]
        return Extracted(**d)


@dataclass
class Exc:
    """A validation finding. severity: block|warn. path: customer|internal|info."""
    code: str
    severity: str
    path: str
    message: str
    question: Optional[str] = None     # customer-facing sentence (customer path only)
    line_no: Optional[int] = None
    fix: dict = field(default_factory=dict)   # {"type": text|number|select|bool, "options": [...], "label": str}
