"""Deterministic token pricing with integer-cent results.

Rates are expressed as exact Decimal dollar amounts per 1,000 tokens. The
public result is always an integer number of cents; no floating-point money is
used. The final cent value is rounded half-up.
"""

from decimal import Decimal, ROUND_HALF_UP

from app.config import settings


TOKENS_PER_UNIT = Decimal("1000")


def calculate_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    api_calls: int = 0,
) -> int:
    """Return the exact metered usage cost in integer cents.

    Reasoning tokens are billed once at the output rate, together with output
    tokens. Inputs are not rounded to whole thousands before pricing. API-call
    pricing is an integer-cent setting and defaults to zero because the brief
    does not define a numeric per-call rate.
    """
    token_counts = (
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (*token_counts, api_calls)
    ):
        raise ValueError("token counts must be non-negative integers")

    dollars = (
        Decimal(input_tokens) * settings.INPUT_RATE_DOLLARS_PER_1K
        + Decimal(cached_input_tokens) * settings.CACHED_INPUT_RATE_DOLLARS_PER_1K
        + Decimal(output_tokens + reasoning_tokens)
        * settings.OUTPUT_RATE_DOLLARS_PER_1K
    ) / TOKENS_PER_UNIT
    token_cents = (dollars * Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(token_cents) + api_calls * settings.API_CALL_PRICE_CENTS
