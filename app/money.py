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

# No individual's return contains a figure this large — India's entire annual
# output is around 3e14 rupees. Anything beyond it is a typo or a probe, and it
# is treated as junk rather than allowed through: Decimal.quantize raises once a
# value needs more than the context's 28 significant digits, and that exception
# would surface in a template, where it is fatal.
MAX_MONEY = Decimal("1e15")


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
    if negative:
        result = -result
    return result if _is_sane(result) else ZERO


def _is_sane(amount: Decimal) -> bool:
    """Reject anything that cannot be a rupee figure."""
    if not amount.is_finite():        # NaN, Infinity, sNaN
        return False
    return -MAX_MONEY <= amount <= MAX_MONEY


def paise(value: Any) -> Decimal:
    """Round to 2 decimals — used for intermediate working, not for reporting."""
    return _quantize(D(value), PAISE)


def rupees(value: Any) -> Decimal:
    """Round to the nearest rupee (section 288A/288B rounding for reporting)."""
    return _quantize(D(value), RUPEE)


def _quantize(amount: Decimal, exponent: Decimal) -> Decimal:
    """Quantize without ever raising.

    ``D`` already screens out the values that cannot be quantized, but a figure
    computed from several sane ones can still overflow. These helpers are called
    from Jinja templates, where an exception means a 500 rather than a wrong
    number, so they degrade instead of throwing.
    """
    try:
        return amount.quantize(exponent, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return ZERO


def round_to_ten(value: Any) -> Decimal:
    """Section 288B: net tax payable/refundable is rounded to the nearest ten."""
    amount = D(value)
    sign = -1 if amount < 0 else 1
    amount = abs(amount)
    return sign * _quantize(amount / TEN, RUPEE) * TEN


def non_negative(value: Any) -> Decimal:
    """Clamp at zero — income heads and deductions never go negative."""
    amount = D(value)
    return amount if amount > ZERO else ZERO


def inr(value: Any) -> str:
    """Format in the Indian grouping system: 12,34,567."""
    amount = rupees(value)
    negative = amount < ZERO
    digits = str(_quantize(abs(amount), RUPEE))
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
