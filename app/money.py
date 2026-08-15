"""Money helpers.

Every rupee amount in this project is a ``Decimal``. Floats are never used for
tax arithmetic: a 0.005 rounding drift is the difference between a return that
matches the department's computation and one that gets a demand notice.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any

ZERO = Decimal("0")
PAISE = Decimal("0.01")
RUPEE = Decimal("1")
TEN = Decimal("10")


def D(value: Any) -> Decimal:
    """Coerce anything sane into a Decimal. Junk becomes zero, never an error."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return ZERO
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return ZERO
    # Strip Indian currency formatting: "Rs. 12,34,567.00", "₹1,50,000/-"
    for token in ("₹", "Rs.", "Rs", "INR", "/-", ",", " "):
        text = text.replace(token, "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return ZERO
    return -result if negative else result


def paise(value: Any) -> Decimal:
    """Round to 2 decimals — used for intermediate working, not for reporting."""
    return D(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def rupees(value: Any) -> Decimal:
    """Round to the nearest rupee (section 288A/288B rounding for reporting)."""
    return D(value).quantize(RUPEE, rounding=ROUND_HALF_UP)


def round_to_ten(value: Any) -> Decimal:
    """Section 288B: net tax payable/refundable is rounded to the nearest ten."""
    amount = D(value)
    sign = -1 if amount < 0 else 1
    amount = abs(amount)
    return sign * (amount / TEN).quantize(RUPEE, rounding=ROUND_HALF_UP) * TEN


def non_negative(value: Any) -> Decimal:
    """Clamp at zero — income heads and deductions never go negative."""
    amount = D(value)
    return amount if amount > ZERO else ZERO


def inr(value: Any) -> str:
    """Format in the Indian grouping system: 12,34,567."""
    amount = rupees(value)
    negative = amount < ZERO
    digits = str(abs(amount).quantize(RUPEE, rounding=ROUND_HALF_UP))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return ("-" if negative else "") + digits
