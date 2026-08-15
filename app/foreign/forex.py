"""Currency conversion under Rule 115.

Rule 115 does not let you pick a rate. For every head of income it names one
specific date, and that date is almost never the date of the transaction:

    "the telegraphic transfer buying rate of such currency as on the last day
     of the month immediately preceding the month in which the income is
     received or paid"

So an RSU vesting on 15 September is converted at the SBI TT buying rate of
31 August. Three tranches vesting in March, June and September use three
different rates. Getting this wrong shifts the perquisite value, the TDS
reconciliation against Form 16, and the capital-gains cost basis all at once.

The rate table below is a convenience, not an authority. SBI publishes the
TTBR daily and the department expects the published figure. Every built-in rate
is flagged ``provisional`` and the user is told, in the interface and on the
computation sheet, exactly which months relied on one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..money import D

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "sbi_ttbr.json"

# Heads of income, and which date Rule 115 attaches to each.
SALARY_AND_OTHER_SOURCES = "income"      # month preceding receipt
CAPITAL_GAINS = "capital_gains"          # month preceding the transfer


@dataclass(frozen=True)
class Rate:
    """One conversion rate, and how much to trust it."""

    currency: str
    month: str                # "2025-08", the month the rate is quoted for
    value: Decimal
    provisional: bool
    note: str = ""

    @property
    def source(self) -> str:
        if self.provisional:
            return (
                f"Built-in reference rate for {self.month} — verify against the "
                "SBI TT buying rate before filing"
            )
        return f"SBI TT buying rate, {self.month}"


class RateUnavailable(Exception):
    """No rate for that month, and none was supplied."""

    def __init__(self, currency: str, month: str) -> None:
        self.currency = currency
        self.month = month
        super().__init__(
            f"No {currency} rate is on file for {month}. Enter the SBI TT "
            f"buying rate as on the last day of {month} on the Foreign income "
            "page — Rule 115 requires that specific rate, not the rate on the "
            "day of the transaction."
        )


class ForexTable:
    """Month-end rates, built in and user-supplied.

    A rate the user enters always beats a built-in one, and is never flagged
    provisional — they looked it up.
    """

    def __init__(self, overrides: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._builtin = _load_builtin()
        self._overrides = overrides or {}

    # -- lookup ----------------------------------------------------------

    def rate_for_month(self, month: str, currency: str = "USD") -> Rate:
        """The TT buying rate quoted for the last day of ``month``."""
        currency = currency.upper()
        user = self._overrides.get(month, {}).get(currency)
        # A blank, a zero or anything unreadable is not an override. Taking a
        # zero at face value would convert every foreign figure to nil and look
        # like the income simply was not there.
        if user not in (None, ""):
            value = D(user)
            if value > 0:
                return Rate(currency, month, value, provisional=False,
                            note="Entered by you")

        entry = self._builtin.get(month, {}).get(currency)
        if entry is None:
            raise RateUnavailable(currency, month)
        return Rate(currency, month, D(entry["value"]),
                    provisional=bool(entry.get("provisional", True)))

    def rate_for(
        self, when: date, currency: str = "USD", head: str = SALARY_AND_OTHER_SOURCES
    ) -> Rate:
        """The Rule 115 rate for a transaction on ``when``.

        Both heads resolve to the same arithmetic — the month immediately
        preceding — but they are named separately because the *event* differs:
        for salary it is the month the income fell due, and for capital gains
        the month the asset was transferred.
        """
        return self.rate_for_month(preceding_month(when), currency)

    # -- conversion ------------------------------------------------------

    def to_inr(
        self,
        amount: Decimal | str | float,
        when: date,
        currency: str = "USD",
        head: str = SALARY_AND_OTHER_SOURCES,
    ) -> Tuple[Decimal, Rate]:
        rate = self.rate_for(when, currency, head)
        return D(amount) * rate.value, rate

    # Both of the reports below accept either bare dates or ``(date,
    # currency)`` pairs. Assuming US dollars for every date meant a euro or
    # sterling holding was never reported as needing a rate at all — the
    # lookup then failed silently at computation time instead.

    def months_needing_a_rate(self, entries, currency: str = "USD") -> list[str]:
        """Which months the user still has to supply a rate for."""
        missing = []
        for when, ccy in _pairs(entries, currency):
            month = preceding_month(when)
            label = month if ccy == "USD" else f"{month} ({ccy})"
            try:
                self.rate_for_month(month, ccy)
            except RateUnavailable:
                if label not in missing:
                    missing.append(label)
        return sorted(missing)

    def provisional_months(self, entries, currency: str = "USD") -> list[str]:
        """Which months fell back on a built-in rate and want checking."""
        flagged = []
        for when, ccy in _pairs(entries, currency):
            month = preceding_month(when)
            label = month if ccy == "USD" else f"{month} ({ccy})"
            try:
                rate = self.rate_for_month(month, ccy)
            except RateUnavailable:
                continue
            if rate.provisional and label not in flagged:
                flagged.append(label)
        return sorted(flagged)


# --------------------------------------------------------------------------


def _pairs(entries, default_currency: str):
    """Normalise a mixed list of dates and ``(date, currency)`` pairs."""
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, tuple):
            when, currency = entry
        else:
            when, currency = entry, default_currency
        if when is None:
            continue
        yield when, (currency or default_currency).upper()


def preceding_month(when: date) -> str:
    """The month immediately preceding ``when``, as "2025-08"."""
    year, month = when.year, when.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def _load_builtin() -> Dict[str, Dict[str, dict]]:
    try:
        with DATA_FILE.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("rates", {})
