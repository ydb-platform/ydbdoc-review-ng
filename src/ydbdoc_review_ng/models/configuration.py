"""Shared production model and versioned tariff configuration for translation and review."""

from decimal import Decimal

from ydbdoc_review_ng.models.types import ModelTokenPrice, PerModelPricing

PRODUCTION_MODEL = "deepseek-v4-flash"
PRICING_VERSION = "production-2026-10-04"
DEEPSEEK_PRICE = ModelTokenPrice(
    Decimal("0.0003"), Decimal("0.0005"), Decimal(0), Decimal("0.000075")
)
_YANDEXGPT_PRICE = ModelTokenPrice(*(Decimal("0.0012") for _ in range(3)))
PRODUCTION_PRICING = PerModelPricing({
    "deepseek-v4-flash": DEEPSEEK_PRICE,
    "deepseek-v4-flash/latest": DEEPSEEK_PRICE,
    "yandexgpt-5.1": _YANDEXGPT_PRICE,
    "yandexgpt-5.1/latest": _YANDEXGPT_PRICE,
})
