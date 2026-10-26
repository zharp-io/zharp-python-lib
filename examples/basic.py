"""Every kind of log, then a crash. Run with your ingest-only key in the environment:

    ZHARP_API_KEY=your-ingest-only-key python examples/basic.py
"""

import logging

import zharp_logs

zharp_logs.init(service="python-sample", environment="development", app_version="1.0.0")

zharp_logs.info("Checkout opened", {"cart_items": 3, "currency": "GHS"})
zharp_logs.warn("Slow network detected", {"latency_ms": 3200})

try:
    raise ValueError("Card declined")
except ValueError:
    zharp_logs.exception("Payment failed", {"order_id": 99})

# Python's own logging, through the handler.
logger = logging.getLogger("shop.inventory")
logger.addHandler(zharp_logs.ZharpHandler())
logger.setLevel(logging.INFO)
logger.warning("Stock low for %s", "SKU-1", extra={"sku": "SKU-1"})


def charge(order_id: int) -> None:
    raise RuntimeError("Sample crash from Python")


# Uncaught: sent as fatal with its traceback, then printed and the program ends as usual.
charge(99)
