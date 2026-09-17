"""
Shared pytest configuration.

The default suite is offline. Pricing now fetches from AWS's public price feed
at runtime (Issue #297), so without this the ec2_export smoke test would reach
out to pricing.us-east-1.amazonaws.com on every run -- slow, flaky on a
restricted network, and a surprise in CI.

Tests that want the network ask for it explicitly: the feed-agreement
regression in test_pricing_feed.py is marked ``integration`` and calls the feed
client directly rather than through this path.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_pricing(monkeypatch, tmp_path):
    """Force the bundled snapshot and keep the cache out of the real $HOME."""
    monkeypatch.setenv("STRATUSSCAN_PRICING_LIVE_FEED", "0")
    monkeypatch.setenv("STRATUSSCAN_PRICING_CACHE_DIR", str(tmp_path / "pricing-cache"))

    # The client memoises per process; a leaked entry would carry one test's
    # provenance into the next.
    import pricing_feed

    pricing_feed.reset_pricing_cache()
    yield
    pricing_feed.reset_pricing_cache()
