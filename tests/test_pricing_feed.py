"""
Tests for the AWS price feed client and the bundled snapshot it produces.

Three kinds of test live here, and they fail for different reasons:

- Offline invariants over the committed ``reference/ec2-pricing.json`` --
  staleness, provenance completeness, and the fingerprints of synthesized data
  that issue #296 found. These always run, need no network, and are safe in CI
  and in a fresh CloudShell.
- Offline behaviour tests for the fetch/fallback path, driven by injected
  failures rather than by a real socket.
- One feed-agreement regression marked ``integration``. It re-pulls the exact
  feed version named in the snapshot's provenance and asserts every stored
  value matches. That is the only test that can catch a wrong rate, and it is
  deselected by default so the standard suite stays offline.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

import pricing_feed

REFERENCE_FILE = Path(__file__).parent.parent / "reference" / "ec2-pricing.json"

#: A snapshot older than this is treated as stale. The feed publishes roughly
#: weekly; 180 days is a generous outer bound that still fails the build before
#: data drifts far.
MAX_SNAPSHOT_AGE_DAYS = 180

#: AWS's real Windows license adder, per vCPU-hour. The current bundled file
#: was built by adding this to a wrong Linux base for every record, which is
#: how #296 proved the block had been computed rather than retrieved.
WINDOWS_LICENSE_PER_VCPU_HOUR = 0.046


@pytest.fixture(scope="module")
def snapshot() -> dict:
    with REFERENCE_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


# =============================================================================
# Bundled snapshot invariants -- offline, always run
# =============================================================================


class TestBundledProvenance:
    def test_provenance_block_present(self, snapshot):
        assert "provenance" in snapshot, (
            "reference/ec2-pricing.json has no provenance block. Without a feed "
            "version no stored rate is falsifiable after the fact."
        )

    def test_provenance_names_the_feed_version(self, snapshot):
        provenance = snapshot["provenance"]
        version = provenance.get("feed_version")
        assert version, "provenance.feed_version is required"
        assert str(version).isdigit(), f"feed_version is not a version stamp: {version!r}"

    def test_provenance_records_its_sku_filters(self, snapshot):
        filters = snapshot["provenance"].get("sku_filters") or {}
        assert filters, "provenance must record which SKU dimensions were selected"
        on_demand = filters.get("on_demand") or {}
        # CapacityStatus is spelled this way in the CSV header even though the
        # Query API attribute is lowercase; filtering on the lowercase form
        # silently returns zero rows.
        assert on_demand.get("CapacityStatus") == "Used"
        assert on_demand.get("Tenancy") == "Shared"

    def test_provenance_declares_nothing_derived(self, snapshot):
        assert snapshot["provenance"].get("derived_values") == "none"

    def test_snapshot_is_not_stale(self, snapshot):
        retrieved = snapshot["provenance"].get("retrieved_at")
        assert retrieved, "provenance.retrieved_at is required"
        when = datetime.datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        age = datetime.datetime.now(datetime.timezone.utc) - when
        assert age.days <= MAX_SNAPSHOT_AGE_DAYS, (
            f"Bundled pricing is {age.days} days old (limit {MAX_SNAPSHOT_AGE_DAYS}). "
            f"Refresh with: python tools/refresh_pricing.py --offer AmazonEC2 --write"
        )


class TestNoSynthesizedValues:
    """
    The three fingerprints of synthesis that #296 identified.

    These are cheap offline smoke tests, not a substitute for the feed
    agreement test below. A family that is uniformly wrong passes all of them.
    """

    def test_windows_is_not_linux_plus_a_license_formula(self, snapshot):
        """
        Windows must be the published Windows rate.

        In the pre-#297 file the Windows-minus-Linux delta was exactly
        $0.046 x vCPU for every record carrying both -- a license formula
        layered on a Linux base, not a retrieved price.
        """
        formula_matches = 0
        comparable = 0
        for _instance_type, record in snapshot["records"].items():
            vcpu = record.get("vcpu")
            block = (record.get("pricing") or {}).get("us-east-1") or {}
            linux = block.get("linux_on_demand_monthly_usd")
            windows = block.get("windows_on_demand_monthly_usd")
            if not vcpu or linux is None or windows is None:
                continue
            comparable += 1
            expected = WINDOWS_LICENSE_PER_VCPU_HOUR * vcpu * pricing_feed.HOURS_PER_MONTH
            if abs((windows - linux) - expected) < 0.02:
                formula_matches += 1

        assert comparable > 0, "no records carry both Linux and Windows rates"
        ratio = formula_matches / comparable
        assert ratio < 0.5, (
            f"{formula_matches}/{comparable} ({ratio:.0%}) of Windows rates equal "
            f"Linux + ${WINDOWS_LICENSE_PER_VCPU_HOUR}/vCPU-hr exactly. That is a "
            f"computed license adder, not a retrieved Windows price."
        )

    @pytest.mark.parametrize(
        ("instance_type", "vcpu", "memory_gib"),
        [
            # Read from AmazonEC2 us-east-1 feed version 20260917181725 on
            # 2026-09-17. These are exact published values, not thresholds:
            # each one is a case the pre-#297 file got wrong with the
            # (2, 4.0) .medium or (192, 384.0) .metal placeholder.
            ("a1.medium", 1, 2.0),
            ("c6g.medium", 1, 2.0),
            ("c7g.medium", 1, 2.0),
            # t3.medium genuinely is 2 / 4 -- the placeholder's value is right
            # here by coincidence, which is why a ratio test is not enough.
            ("t3.medium", 2, 4.0),
            ("m5.large", 2, 8.0),
            ("u7i-6tb.112xlarge", 448, 6144.0),
        ],
    )
    def test_specs_match_published_values(self, snapshot, instance_type, vcpu, memory_gib):
        record = snapshot["records"].get(instance_type)
        assert record is not None, f"{instance_type} is missing from the snapshot"
        assert record.get("vcpu") == vcpu, (
            f"{instance_type} vCPU: stored {record.get('vcpu')}, AWS publishes {vcpu}"
        )
        assert record.get("memory_gib") == memory_gib, (
            f"{instance_type} memory: stored {record.get('memory_gib')} GiB, "
            f"AWS publishes {memory_gib} GiB"
        )

    def test_bare_metal_types_are_present(self, snapshot):
        """
        Bare-metal instances live under Product Family "Compute Instance
        (bare metal)". An extractor filtering only on "Compute Instance" drops
        all 158 of them and returns a table that looks complete.
        """
        metals = [n for n in snapshot["records"] if n.endswith(".metal")]
        assert len(metals) > 50, (
            f"only {len(metals)} .metal instance types in the snapshot; the feed "
            f"publishes 158 for us-east-1"
        )

    def test_specs_are_populated(self, snapshot):
        missing = [
            name
            for name, record in snapshot["records"].items()
            if record.get("vcpu") is None or record.get("memory_gib") is None
        ]
        assert not missing, f"{len(missing)} records have no vCPU or memory: {missing[:10]}"

    def test_govcloud_is_not_a_constant_multiple_of_commercial(self, snapshot):
        """
        GovCloud must come from the GovCloud feed, not from a premium applied
        to commercial. The real premium varies by family (roughly 1.20-1.26).
        """
        ratios = []
        for record in snapshot["records"].values():
            pricing = record.get("pricing") or {}
            commercial = (pricing.get("us-east-1") or {}).get("linux_on_demand_monthly_usd")
            gov = (pricing.get("us-gov-west-1") or {}).get("linux_on_demand_monthly_usd")
            if commercial and gov:
                ratios.append(gov / commercial)

        assert len(ratios) > 50, "too few paired records to judge"
        assert len({round(r, 4) for r in ratios}) > 5, (
            "GovCloud rates are a near-constant multiple of commercial, which "
            "means they were computed rather than pulled from the GovCloud feed."
        )


# =============================================================================
# Fetch and fallback behaviour -- offline, failures injected
# =============================================================================


class TestFallbackPath:
    def setup_method(self):
        pricing_feed.reset_pricing_cache()

    def teardown_method(self):
        pricing_feed.reset_pricing_cache()

    def test_disabled_live_feed_never_touches_the_network(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise AssertionError("network touched with live_feed_enabled=False")

        monkeypatch.setattr(pricing_feed, "_open", explode)
        records, provenance = pricing_feed.get_pricing("AmazonEC2", live_feed_enabled=False)

        assert records, "bundled snapshot should still supply records"
        assert provenance["source"] == pricing_feed.SOURCE_BUNDLED
        assert "disabled" in provenance["fallback_reason"]

    def test_unreachable_feed_falls_back_to_bundled(self, monkeypatch):
        monkeypatch.setattr(
            pricing_feed,
            "resolve_current_version",
            lambda *a, **k: (_ for _ in ()).throw(pricing_feed.FeedError("no egress")),
        )
        records, provenance = pricing_feed.get_pricing("AmazonEC2", live_feed_enabled=True)

        assert records, "an unreachable feed must not empty the price table"
        assert provenance["source"] == pricing_feed.SOURCE_BUNDLED
        assert provenance["fallback_reason"] == "price feed unreachable"

    def test_oversized_feed_falls_back_without_downloading(self, monkeypatch):
        monkeypatch.setattr(
            pricing_feed, "resolve_current_version", lambda *a, **k: ("20260917181725", "")
        )
        monkeypatch.setattr(pricing_feed, "load_cached_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(pricing_feed, "feed_content_length", lambda *a, **k: 302856576)
        monkeypatch.setattr(
            pricing_feed,
            "iter_feed_rows",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("feed streamed despite ceiling")),
        )

        records, provenance = pricing_feed.get_pricing(
            "AmazonEC2", live_feed_enabled=True, max_feed_bytes=1024
        )
        assert records
        assert provenance["source"] == pricing_feed.SOURCE_BUNDLED
        assert provenance["fallback_reason"] == "feed exceeds configured size ceiling"

    def test_unknown_offer_reports_unavailable_not_silence(self):
        records, provenance = pricing_feed.get_pricing("AmazonMadeUp", live_feed_enabled=False)
        assert records == {}
        assert provenance["source"] == pricing_feed.SOURCE_UNAVAILABLE

    def test_empty_feed_parse_does_not_replace_good_data(self, monkeypatch):
        monkeypatch.setattr(
            pricing_feed, "resolve_current_version", lambda *a, **k: ("20260917181725", "")
        )
        monkeypatch.setattr(pricing_feed, "load_cached_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(pricing_feed, "feed_content_length", lambda *a, **k: 100)
        monkeypatch.setattr(
            pricing_feed,
            "build_snapshot",
            lambda *a, **k: {"records": {}, "provenance": {"feed_version": "20260917181725"}},
        )

        records, provenance = pricing_feed.get_pricing("AmazonEC2", live_feed_enabled=True)
        assert records, "a zero-record parse must fall back, not export an empty price table"
        assert provenance["fallback_reason"] == "price feed returned no records"


class TestProvenanceNote:
    def test_live_feed_note_names_version_and_date(self):
        note = pricing_feed.provenance_note(
            {
                "source": pricing_feed.SOURCE_LIVE_FEED,
                "feed_version": "20260917181725",
                "retrieved_at": "2026-09-17T18:17:25Z",
            }
        )
        assert "20260917181725" in note
        assert "2026-09-17" in note

    def test_cached_note_says_cached(self):
        note = pricing_feed.provenance_note(
            {
                "source": pricing_feed.SOURCE_CACHED_FEED,
                "feed_version": "20260917181725",
                "retrieved_at": "2026-09-17T18:17:25Z",
            }
        )
        assert "cached" in note.lower()

    def test_bundled_note_says_bundled_and_why(self):
        note = pricing_feed.provenance_note(
            {
                "source": pricing_feed.SOURCE_BUNDLED,
                "feed_publication_date": "2026-09-17T18:17:25Z",
                "fallback_reason": "price feed unreachable",
            }
        )
        assert "bundled" in note.lower()
        assert "unreachable" in note

    def test_unavailable_never_renders_as_empty(self):
        assert pricing_feed.provenance_note({}).strip()


class TestVersionParsing:
    """
    The audit harness read the offer code where the version belongs. Pin the
    segment index so that off-by-one cannot come back.
    """

    def test_version_is_path_segment_five(self, monkeypatch):
        payload = json.dumps(
            {
                "publicationDate": "2026-09-17T18:17:25Z",
                "regions": {
                    "us-east-1": {
                        "currentVersionUrl": "/offers/v1.0/aws/AmazonEC2/20260917181725/us-east-1/index.json"
                    }
                },
            }
        ).encode()
        monkeypatch.setattr(pricing_feed, "_open", _fake_response(payload))

        version, published = pricing_feed.resolve_current_version("AmazonEC2", "us-east-1")
        assert version == "20260917181725"
        assert published == "2026-09-17T18:17:25Z"

    def test_offer_code_is_rejected_as_a_version(self, monkeypatch):
        payload = json.dumps(
            {
                "regions": {
                    "us-east-1": {"currentVersionUrl": "/offers/v1.0/aws/AmazonEC2/us-east-1/index.json"}
                }
            }
        ).encode()
        monkeypatch.setattr(pricing_feed, "_open", _fake_response(payload))

        with pytest.raises(pricing_feed.FeedError):
            pricing_feed.resolve_current_version("AmazonEC2", "us-east-1")

    def test_missing_region_is_an_error_not_an_empty_result(self, monkeypatch):
        monkeypatch.setattr(pricing_feed, "_open", _fake_response(json.dumps({"regions": {}}).encode()))
        with pytest.raises(pricing_feed.FeedError):
            pricing_feed.resolve_current_version("AmazonEC2", "us-gov-west-1")


class TestExtractorDerivesNothing:
    def test_absent_windows_row_yields_none_not_a_neighbour(self):
        rows = [
            _ec2_row("m5.large", "Linux", "0.096", vcpu="2", memory="8 GiB"),
        ]
        records = pricing_feed.extract_ec2_records(iter(rows), "us-east-1", {})
        block = records["m5.large"]["pricing"]["us-east-1"]
        assert block["linux_on_demand_monthly_usd"] == round(0.096 * 730, 2)
        assert block["windows_on_demand_monthly_usd"] is None
        assert block["linux_reserved_1yr_monthly_usd"] is None

    def test_spot_and_dedicated_rows_are_excluded(self):
        rows = [
            _ec2_row("m5.large", "Linux", "0.096", vcpu="2", memory="8 GiB", tenancy="Dedicated"),
            _ec2_row("m5.large", "Linux", "0.030", vcpu="2", memory="8 GiB", capacity="UnusedCapacityReservation"),
        ]
        records = pricing_feed.extract_ec2_records(iter(rows), "us-east-1", {})
        assert records == {}, "non-Shared / non-Used rows must not become base rates"

    def test_memory_in_non_gib_units_is_none_not_converted(self):
        rows = [_ec2_row("x1.large", "Linux", "1.0", vcpu="4", memory="NA")]
        records = pricing_feed.extract_ec2_records(iter(rows), "us-east-1", {})
        assert records["x1.large"]["memory_gib"] is None


# =============================================================================
# Feed agreement -- network, deselected by default
# =============================================================================


@pytest.mark.integration
class TestFeedAgreement:
    """
    Re-pull the exact feed version the snapshot names and compare every value.

    This is the real regression test. The offline invariants above catch the
    shapes of past mistakes; only this catches a wrong number.
    """

    def test_stored_rates_match_the_named_feed_version(self, snapshot):
        provenance = snapshot["provenance"]
        version = provenance["feed_version"]
        regions = provenance["regions"]

        rebuilt: dict = {}
        for region in regions:
            pricing_feed.extract_ec2_records(
                pricing_feed.iter_feed_rows("AmazonEC2", version, region, timeout=60),
                region,
                rebuilt,
            )

        assert rebuilt, "feed returned no records"

        mismatches = []
        for name, stored in snapshot["records"].items():
            fresh = rebuilt.get(name)
            if fresh is None:
                mismatches.append(f"{name}: absent from feed version {version}")
                continue
            for field in ("vcpu", "memory_gib", "physical_processor"):
                if stored.get(field) != fresh.get(field):
                    mismatches.append(f"{name}.{field}: {stored.get(field)} != {fresh.get(field)}")
            for region in regions:
                stored_block = (stored.get("pricing") or {}).get(region) or {}
                fresh_block = (fresh.get("pricing") or {}).get(region) or {}
                for field, value in fresh_block.items():
                    if stored_block.get(field) != value:
                        mismatches.append(
                            f"{name}.{region}.{field}: {stored_block.get(field)} != {value}"
                        )

        assert not mismatches, (
            f"{len(mismatches)} disagreements with feed version {version}. "
            f"First 20: {mismatches[:20]}"
        )


# =============================================================================
# Helpers
# =============================================================================


def _fake_response(payload: bytes):
    class _Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    return lambda *_args, **_kwargs: _Response()


def _ec2_row(
    instance_type: str,
    operating_system: str,
    price: str,
    vcpu: str = "2",
    memory: str = "8 GiB",
    tenancy: str = "Shared",
    capacity: str = "Used",
    term: str = "OnDemand",
) -> dict:
    return {
        "TermType": term,
        "Product Family": "Compute Instance",
        "Tenancy": tenancy,
        "CapacityStatus": capacity,
        "Pre Installed S/W": "NA",
        "License Model": "No License required",
        "Unit": "Hrs",
        "Operating System": operating_system,
        "Instance Type": instance_type,
        "PricePerUnit": price,
        "vCPU": vcpu,
        "Memory": memory,
        "Processor Architecture": "64-bit",
    }
