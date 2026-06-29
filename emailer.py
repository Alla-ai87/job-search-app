"""
Sponsorship status detection.

Returns one of five values:
  SponsorshipStatus.AVAILABLE      – employer explicitly offers sponsorship
  SponsorshipStatus.POSSIBLE       – corp-to-corp / contract signals possible flexibility
  SponsorshipStatus.NOT_MENTIONED  – no sponsorship language found
  SponsorshipStatus.NOT_AVAILABLE  – employer explicitly declines to sponsor
  SponsorshipStatus.UNKNOWN        – empty or unparseable input

Evaluation order (highest priority first):
  1. NOT_AVAILABLE  – explicit denial wins over everything
  2. AVAILABLE      – explicit offer
  3. POSSIBLE       – indirect signals (C2C, contract, flexible)
  4. NOT_MENTIONED  – nothing found
"""

from app.schemas import SponsorshipStatus


# Explicit denial – checked first so "we do not sponsor visas" is never
# misclassified as AVAILABLE by the "sponsor visas" available term.
_NOT_AVAILABLE: tuple[str, ...] = (
    "no visa sponsorship",
    "not sponsor",
    "not able to sponsor",
    "unable to sponsor",
    "cannot sponsor",
    "can not sponsor",
    "do not sponsor",
    "does not sponsor",
    "without sponsorship",
    "must be authorized to work",
    "must be legally authorized",
    "must have work authorization",
    "must have legal right to work",
    "must be eligible to work",
    "no sponsorship",
    "sponsorship is not available",
    "sponsorship not provided",
    "no employment visa",
    "no work visa",
    "u.s. citizens only",
    "us citizens only",
    "citizens and permanent residents",
    "green card holders only",
    "requires permanent work authorization",
    "must already have authorization",
    "must already be authorized",
    "able to obtain work authorization",  # "must be able to obtain" = NOT_AVAILABLE
    "not eligible for sponsorship",
    "not provide sponsorship",
)

# Explicit offer
_AVAILABLE: tuple[str, ...] = (
    "visa sponsorship available",
    "visa sponsorship provided",
    "visa sponsorship offered",
    "will sponsor",
    "we sponsor",
    "sponsorship available",
    "sponsorship is available",
    "sponsorship provided",
    "h-1b sponsorship",
    "h1b sponsorship",
    "h1-b sponsorship",
    "eligible for sponsorship",
    "sponsor work visas",
    "sponsor h-1b",
    "sponsor h1b",
    "support visa",
    "support work authorization",
    "provide visa sponsorship",
    "offer sponsorship",
    "offers sponsorship",
    "open to sponsorship",
)

# Indirect positive signals – corp-to-corp, 1099, or contract roles often
# accept non-permanent-resident workers.
_POSSIBLE: tuple[str, ...] = (
    "corp to corp",
    "corp-to-corp",
    "c2c",
    "1099",
    "contract to hire",
    "contract-to-hire",
    "w2 or c2c",
    "w-2 or c2c",
)


def detect_sponsorship_status(*parts: str | None) -> SponsorshipStatus:
    """
    Combine all text parts and classify sponsorship status.

    Pass as many text fields as relevant, e.g.::

        detect_sponsorship_status(description, title, company_name, extensions_text)
    """
    if not any(parts):
        return SponsorshipStatus.UNKNOWN

    text = " ".join(part or "" for part in parts).lower()

    if not text.strip():
        return SponsorshipStatus.UNKNOWN

    # Priority 1: explicit denial
    if any(term in text for term in _NOT_AVAILABLE):
        return SponsorshipStatus.NOT_AVAILABLE

    # Priority 2: explicit offer
    if any(term in text for term in _AVAILABLE):
        return SponsorshipStatus.AVAILABLE

    # Priority 3: indirect signal
    if any(term in text for term in _POSSIBLE):
        return SponsorshipStatus.POSSIBLE

    return SponsorshipStatus.NOT_MENTIONED
