#!/usr/bin/env python3
"""
Title: StratusScan AWS Price List Bulk Feed Client
Version: v0.1.0
Date: SEP-17-2026

Description:
Single source of pricing rates for StratusScan. Every rate served by this
module comes from a row in AWS's published Price List Bulk API feed -- either
fetched at runtime, replayed from a version-keyed local cache, or read from the
bundled snapshot in ``reference/`` that ``tools/refresh_pricing.py`` generated
from that same feed with this same extractor.

Nothing here derives a price. There are no multipliers, no linear scaling
across instance sizes within a family, and no extrapolation from an anchor SKU.
A rate that has no feed row is recorded as ``None``, never as a neighbour's
value. Issue #296 documented what synthesis costs: 80% of the EC2 commercial
block was computed rather than retrieved, overstating cost by 8.6% on average
for seven months without anyone noticing.

The feed is public and unauthenticated -- no credentials, no IAM grant, no
``pricing:GetProducts``. That matters because the Price List Query API has no
endpoint in the ``aws-us-gov`` partition, so a GovCloud CloudShell run could
never call it (verified against botocore: ``get_available_regions('pricing',
partition_name='aws-us-gov')`` returns ``[]``).

Feed layout (verified 2026-09-17):
    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<Offer>/current/region_index.json
    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<Offer>/<version>/<region>/index.csv

Operational limits this module is built around, all measured:
- The feed host serves no compression. ``Accept-Encoding: gzip`` is ignored and
  no ``Content-Encoding`` header comes back, so raw bytes are wire bytes. The
  ``AmazonEC2`` ``us-east-1`` CSV is 302,856,576 bytes; ``us-gov-west-1`` is
  205,737,309.
- CloudShell gives 1 vCPU, 2 GiB RAM and 1 GB of persistent ``$HOME`` storage
  (AWS CloudShell User Guide, "Service quotas and restrictions").
- The raw body is therefore streamed and discarded, never written to disk.
  Peak RSS measured at 24 MB; the distilled EC2 artifact is ~112 KB, five
  thousand times under the $HOME quota.

Standard library only. This module is on the CloudShell runtime path.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

FEED_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"

# The CSV carries five metadata lines before the header row. The header row is
# the first line beginning with the SKU column.
_HEADER_FIRST_FIELD = "SKU"

# Hours-per-month convention used by every reference file in this repo.
HOURS_PER_MONTH = 730

SCHEMA_VERSION = "3.0.0"

# Defaults mirrored in advanced_settings.get_default_settings()['pricing'].
DEFAULT_LIVE_FEED_ENABLED = True
DEFAULT_CACHE_TTL_HOURS = 168  # 7 days
DEFAULT_MAX_FEED_BYTES = 402653184  # 384 MiB -- above the 302 MB EC2 us-east-1 CSV
DEFAULT_TIMEOUT_SECONDS = 30  # per-socket connect/read timeout
DEFAULT_MAX_SECONDS = 180  # hard wall-clock budget for one offer's fetch

_REFERENCE_DIR = Path(__file__).parent / "reference"


class FeedError(Exception):
    """Raised when the feed cannot be reached, parsed, or trusted."""


# =============================================================================
# PROVENANCE
# =============================================================================

#: ``source`` values recorded in provenance and surfaced in export output.
SOURCE_LIVE_FEED = "live-feed"
SOURCE_CACHED_FEED = "cached-feed"
SOURCE_BUNDLED = "bundled-snapshot"
SOURCE_UNAVAILABLE = "unavailable"


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def provenance_note(provenance: dict[str, Any]) -> str:
    """
    Render a provenance block as one short phrase for a workbook cell.

    This is the string that goes in an exporter's ``Cost Note`` column, so a
    reader can tell where a cost figure came from without opening the repo.

    Args:
        provenance: A provenance dict from :func:`get_pricing`.

    Returns:
        A phrase such as ``"AWS price feed 20260917181725, retrieved
        2026-09-17"`` or ``"bundled snapshot 2026-09-17"``. Never empty.
    """
    source = provenance.get("source", SOURCE_UNAVAILABLE)
    version = provenance.get("feed_version")
    retrieved = (provenance.get("retrieved_at") or "")[:10]

    if source in (SOURCE_LIVE_FEED, SOURCE_CACHED_FEED) and version:
        label = "AWS price feed" if source == SOURCE_LIVE_FEED else "AWS price feed (cached)"
        return f"{label} {version}, retrieved {retrieved}" if retrieved else f"{label} {version}"

    if source == SOURCE_BUNDLED:
        snapshot = provenance.get("feed_publication_date") or provenance.get("snapshot_date") or ""
        snapshot = snapshot[:10]
        note = f"bundled snapshot {snapshot}" if snapshot else "bundled snapshot"
        reason = provenance.get("fallback_reason")
        return f"{note} ({reason})" if reason else note

    return "pricing source unavailable"


# =============================================================================
# FEED ACCESS
# =============================================================================


def _open(url: str, timeout: float, method: str = "GET"):
    """Open a feed URL with an explicit timeout, raising FeedError on failure."""
    request = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": "StratusScan/pricing-feed", "Accept": "*/*"},
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (https literal)
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise FeedError(f"Cannot reach {url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise FeedError(f"Network error for {url}: {exc}") from exc


def resolve_current_version(
    offer_code: str, region: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[str, str]:
    """
    Resolve the feed version currently published for one offer and Region.

    Reads ``<offer>/current/region_index.json`` (~18 KB) and pulls the version
    out of the Region's ``currentVersionUrl``.

    The version is path segment 5 of
    ``/offers/v1.0/aws/AmazonEC2/20260917181725/us-east-1/index.json``. The
    audit harness that preceded this module read segment 4 -- the offer code --
    and that off-by-one is the reason this is spelled out here rather than
    inlined. Do not "simplify" it to ``split('/')[4]``.

    Args:
        offer_code: AWS offer code, e.g. ``"AmazonEC2"``.
        region: AWS Region code, e.g. ``"us-east-1"``.
        timeout: Per-socket timeout in seconds.

    Returns:
        ``(version, publication_date)`` -- e.g. ``("20260917181725",
        "2026-09-17T18:17:25Z")``.

    Raises:
        FeedError: The index is unreachable, malformed, or has no such Region.
    """
    url = f"{FEED_BASE}/{offer_code}/current/region_index.json"
    with _open(url, timeout) as response:
        try:
            index = json.loads(response.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FeedError(f"Malformed region index for {offer_code}: {exc}") from exc

    entry = index.get("regions", {}).get(region)
    if not entry:
        raise FeedError(f"Offer {offer_code} publishes no feed for Region {region}")

    version_url = entry.get("currentVersionUrl", "")
    segments = version_url.split("/")
    # ['', 'offers', 'v1.0', 'aws', '<OfferCode>', '<version>', '<region>', 'index.json']
    if len(segments) < 7 or segments[4] != offer_code:
        raise FeedError(f"Unexpected currentVersionUrl for {offer_code}/{region}: {version_url!r}")
    version = segments[5]
    if not version.isdigit():
        raise FeedError(f"Feed version is not a version stamp: {version!r}")

    return version, index.get("publicationDate", "")


def feed_content_length(
    offer_code: str, version: str, region: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> int:
    """
    Return the exact byte size of one Region's CSV, without downloading it.

    The feed host serves no compression, so this is also the number of bytes
    that will cross the wire. Checked before every fetch so an operator is
    never surprised by a 302 MB transfer.

    Raises:
        FeedError: The HEAD request fails or returns no usable Content-Length.
    """
    url = f"{FEED_BASE}/{offer_code}/{version}/{region}/index.csv"
    with _open(url, timeout, method="HEAD") as response:
        raw = response.headers.get("Content-Length")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise FeedError(f"No usable Content-Length for {url}") from exc


def iter_feed_rows(
    offer_code: str,
    version: str,
    region: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> Iterator[dict[str, str]]:
    """
    Stream one Region's price CSV a row at a time.

    The body is never buffered and never written to disk -- the us-east-1 EC2
    CSV is 302 MB against a 1 GB CloudShell ``$HOME``, and 2 GiB of RAM.
    Measured peak RSS for a full EC2 pass is 24 MB.

    Args:
        offer_code: AWS offer code.
        version: Feed version stamp from :func:`resolve_current_version`.
        region: AWS Region code.
        timeout: Per-socket timeout in seconds.
        deadline: Optional ``time.monotonic()`` value; the stream aborts once
            passed, so a slow link degrades to the bundled snapshot instead of
            hanging an export.

    Yields:
        One dict per CSV row, keyed by the feed's own column names.

    Raises:
        FeedError: Network failure, missing header row, or deadline exceeded.
    """
    url = f"{FEED_BASE}/{offer_code}/{version}/{region}/index.csv"
    response = _open(url, timeout)
    try:
        text = io.TextIOWrapper(response, encoding="utf-8", newline="")

        header: list[str] | None = None
        for _ in range(32):  # five metadata lines today; a small margin, not a guess
            line = text.readline()
            if not line:
                break
            if line.lstrip('"').startswith(_HEADER_FIRST_FIELD):
                header = next(csv.reader([line]))
                break
        if not header:
            raise FeedError(f"No header row found in {url}")

        reader = csv.DictReader(text, fieldnames=header)
        for checked, row in enumerate(reader, start=1):
            # Checking the clock every row would cost more than the parse.
            if deadline is not None and checked % 5000 == 0 and time.monotonic() > deadline:
                raise FeedError(f"Time budget exhausted while streaming {url}")
            yield row
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeedError(f"Stream failed for {url}: {exc}") from exc
    finally:
        response.close()


# =============================================================================
# EXTRACTORS -- the only place a feed row becomes a stored value
# =============================================================================

#: Bare-metal instances are published under their own Product Family. Filtering
#: on "Compute Instance" alone silently drops all 158 ``.metal`` types with no
#: error -- an empty result that looks exactly like a correct one. Both values
#: are required.
EC2_PRODUCT_FAMILIES = ("Compute Instance", "Compute Instance (bare metal)")

#: On-demand SKU dimensions. Every one of these is required; dropping any of
#: them pulls in Spot, Dedicated, BYOL or bundled-software rows that look like
#: base rates and are not.
EC2_ONDEMAND_FILTERS: dict[str, str] = {
    "TermType": "OnDemand",
    "Tenancy": "Shared",
    "CapacityStatus": "Used",
    "Pre Installed S/W": "NA",
    "License Model": "No License required",
    "Unit": "Hrs",
}

#: Reserved SKU dimensions, for ``linux_reserved_1yr_monthly_usd``.
EC2_RESERVED_FILTERS: dict[str, str] = {
    "TermType": "Reserved",
    "Tenancy": "Shared",
    "CapacityStatus": "Used",
    "Pre Installed S/W": "NA",
    "License Model": "No License required",
    "Unit": "Hrs",
    "LeaseContractLength": "1yr",
    "PurchaseOption": "No Upfront",
    "OfferingClass": "standard",
}

_EC2_OPERATING_SYSTEMS = ("Linux", "Windows")


def _matches(row: dict[str, str], filters: dict[str, str]) -> bool:
    return all(row.get(key) == value for key, value in filters.items())


def _parse_price(raw: str | None) -> float | None:
    """Parse a feed PricePerUnit. Returns None for absent or unparseable values."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_vcpu(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parse_memory_gib(raw: str | None) -> float | None:
    """
    Parse the feed's ``Memory`` column, e.g. ``"8 GiB"``.

    Returns None for ``"NA"`` and for any unit that is not GiB, rather than
    guessing at a conversion.
    """
    if not raw:
        return None
    parts = raw.strip().split()
    if len(parts) != 2 or parts[1] != "GiB":
        return None
    try:
        return float(parts[0].replace(",", ""))
    except ValueError:
        return None


def extract_ec2_records(
    rows: Iterator[dict[str, str]], region_key: str, records: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """
    Fold one Region's EC2 feed rows into the ``reference/ec2-pricing.json`` shape.

    Used unchanged by both the runtime fetch and ``tools/refresh_pricing.py``,
    so a cached artifact and a bundled snapshot of the same feed version are
    byte-identical in their record block. That equivalence is the point: one
    extractor means the fallback path cannot drift from the live path.

    Every field written here is read from a feed row. Specifically:

    - Windows is the published Windows rate, never Linux plus a license
      formula. The current bundled file was built the wrong way -- its
      Windows-minus-Linux delta is exactly ``$0.046 x vCPU`` throughout, which
      is how issue #296 proved the block had been computed.
    - ``vcpu`` and ``memory_gib`` come from the feed's ``vCPU`` and ``Memory``
      columns, not from a per-size default. The bundled file records every
      ``.medium`` as 2 vCPU / 4 GiB and every ``.metal`` as 192 vCPU / 384 GiB;
      92 vCPU and 155 memory values disagree with AWS.
    - An instance type with no matching row for an OS gets ``None``.

    Args:
        rows: Row iterator from :func:`iter_feed_rows`.
        region_key: Region key to write under ``record['pricing']``.
        records: Accumulator, so several Regions fold into one record set.

    Returns:
        The same ``records`` dict, mutated in place.
    """
    for row in rows:
        instance_type = row.get("Instance Type")
        if not instance_type:
            continue
        if row.get("Product Family") not in EC2_PRODUCT_FAMILIES:
            continue

        is_ondemand = _matches(row, EC2_ONDEMAND_FILTERS)
        is_reserved = _matches(row, EC2_RESERVED_FILTERS)
        if not (is_ondemand or is_reserved):
            continue

        operating_system = row.get("Operating System")
        if operating_system not in _EC2_OPERATING_SYSTEMS:
            continue

        hourly = _parse_price(row.get("PricePerUnit"))
        if hourly is None:
            continue

        record = records.setdefault(
            instance_type,
            {
                "vcpu": None,
                "memory_gib": None,
                # The feed cannot distinguish arm64 from x86_64: its
                # "Processor Architecture" column only ever reads "64-bit" or
                # "32-bit or 64-bit". Inferring arm64 from the processor name
                # would be derivation, which this module does not do, so the
                # field stays null and the raw processor string is carried
                # instead. Callers needing a real architecture must use
                # ec2:DescribeInstanceTypes.
                "architecture": None,
                "physical_processor": None,
                "pricing": {},
            },
        )

        # Hardware specs are partition-independent; take them from whichever
        # Region supplies them first and leave them alone thereafter.
        if record["vcpu"] is None:
            record["vcpu"] = _parse_vcpu(row.get("vCPU"))
        if record["memory_gib"] is None:
            record["memory_gib"] = _parse_memory_gib(row.get("Memory"))
        if record["physical_processor"] is None:
            processor = (row.get("Physical Processor") or "").strip()
            record["physical_processor"] = processor or None

        block = record["pricing"].setdefault(
            region_key,
            {
                "linux_on_demand_monthly_usd": None,
                "linux_reserved_1yr_monthly_usd": None,
                "windows_on_demand_monthly_usd": None,
            },
        )

        monthly = round(hourly * HOURS_PER_MONTH, 2)
        if is_ondemand and operating_system == "Linux":
            block["linux_on_demand_monthly_usd"] = monthly
        elif is_ondemand and operating_system == "Windows":
            block["windows_on_demand_monthly_usd"] = monthly
        elif is_reserved and operating_system == "Linux":
            block["linux_reserved_1yr_monthly_usd"] = monthly

    return records


#: Offer code -> (reference filename, extractor). Only offers listed here can
#: be fetched live. The other reference files still carry hand-entered data and
#: are regenerated under their own issues (see audit sections 3.1-3.3); serving
#: them from this module would imply a provenance they do not have.
EXTRACTORS: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
    "AmazonEC2": ("ec2-pricing.json", extract_ec2_records),
}

#: Regions pulled for each offer: commercial and GovCloud, each from its own
#: feed. GovCloud is never computed from commercial -- per the #296 audit the
#: GovCloud block is the accurate half of the current file precisely because
#: whoever built it pulled it separately.
FEED_REGIONS = ("us-east-1", "us-gov-west-1")


def build_snapshot(
    offer_code: str,
    regions: tuple[str, ...] = FEED_REGIONS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_seconds: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Build a complete snapshot for one offer by streaming each Region's feed.

    This is the single code path behind both the runtime cache fill and
    ``tools/refresh_pricing.py``.

    Args:
        offer_code: An offer code present in :data:`EXTRACTORS`.
        regions: Region codes to pull, each from its own feed.
        timeout: Per-socket timeout in seconds.
        max_seconds: Optional wall-clock budget across all Regions.
        progress: Optional callback for human-readable progress lines. The
            library never prints (Issue #171); callers decide.

    Returns:
        A snapshot dict ready to serialise to ``reference/<file>``.

    Raises:
        FeedError: Any Region fails to resolve, fetch, or parse. A partial
            snapshot is never returned -- half a price table looks exactly like
            a whole one to an exporter.
    """
    if offer_code not in EXTRACTORS:
        raise FeedError(f"No extractor registered for offer {offer_code}")

    _, extractor = EXTRACTORS[offer_code]
    deadline = None if max_seconds is None else time.monotonic() + max_seconds

    records: dict[str, dict[str, Any]] = {}
    versions: dict[str, str] = {}
    publication_dates: dict[str, str] = {}
    sizes: dict[str, int] = {}

    for region in regions:
        version, publication_date = resolve_current_version(offer_code, region, timeout)
        versions[region] = version
        publication_dates[region] = publication_date
        sizes[region] = feed_content_length(offer_code, version, region, timeout)
        if progress:
            progress(
                f"{offer_code} {region}: feed version {version}, "
                f"{sizes[region]:,} bytes (uncompressed on the wire)"
            )
        extractor(
            iter_feed_rows(offer_code, version, region, timeout, deadline),
            region,
            records,
        )
        if progress:
            progress(f"{offer_code} {region}: {len(records):,} records so far")

    distinct = set(versions.values())
    if len(distinct) != 1:
        # Regions normally publish under one stamp. If they diverge mid-refresh,
        # say so rather than silently stitching two feeds together.
        _LOG.warning(
            "Offer %s published different feed versions per Region: %s", offer_code, versions
        )
    feed_version = sorted(distinct)[-1]

    return {
        "schema_version": SCHEMA_VERSION,
        "currency": "USD",
        "pricing_basis": "monthly",
        "record_count": len(records),
        "provenance": {
            "method": "aws-price-list-bulk-api",
            "offer_code": offer_code,
            "feed_version": feed_version,
            "feed_versions_by_region": versions,
            "feed_publication_date": publication_dates.get(regions[0], ""),
            "retrieved_at": _utc_now_iso(),
            "regions": list(regions),
            "feed_bytes_by_region": sizes,
            "sku_filters": {
                "on_demand": dict(EC2_ONDEMAND_FILTERS),
                "reserved_1yr": dict(EC2_RESERVED_FILTERS),
                "operating_systems": list(_EC2_OPERATING_SYSTEMS),
            },
            "hours_per_month": HOURS_PER_MONTH,
            "generator": "tools/refresh_pricing.py",
            "derived_values": "none",
        },
        "records": records,
    }


# =============================================================================
# VERSION-KEYED CACHE
# =============================================================================


def cache_dir() -> Path:
    """
    Directory holding fetched pricing artifacts.

    Defaults to ``$HOME/.stratusscan/pricing-cache``; ``STRATUSSCAN_PRICING_CACHE_DIR``
    overrides it. ``$HOME`` persists between CloudShell sessions, and an EC2
    artifact is ~112 KB against the 1 GB quota.
    """
    override = os.environ.get("STRATUSSCAN_PRICING_CACHE_DIR")
    base = Path(override) if override else Path.home() / ".stratusscan" / "pricing-cache"
    return base


def _cache_file(offer_code: str, feed_version: str) -> Path:
    return cache_dir() / f"{offer_code}-{feed_version}.json"


def load_cached_snapshot(
    offer_code: str, feed_version: str, ttl_hours: float
) -> dict[str, Any] | None:
    """
    Return a cached snapshot for this exact feed version, or None.

    Keyed by feed version, so a newly published feed can never be served from a
    stale entry regardless of TTL. The TTL only bounds how long a still-current
    version is reused without re-checking.
    """
    path = _cache_file(offer_code, feed_version)
    try:
        if not path.is_file():
            return None
        if ttl_hours > 0:
            age_hours = (time.time() - path.stat().st_mtime) / 3600.0
            if age_hours > ttl_hours:
                _LOG.info("Pricing cache for %s is %.1fh old (TTL %.1fh)", offer_code, age_hours, ttl_hours)
                return None
        with path.open(encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        _LOG.warning("Unusable pricing cache %s: %s", path, exc)
        return None

    if snapshot.get("provenance", {}).get("feed_version") != feed_version:
        _LOG.warning("Pricing cache %s does not carry feed version %s", path, feed_version)
        return None
    return snapshot


def store_cached_snapshot(offer_code: str, snapshot: dict[str, Any]) -> None:
    """Write a snapshot to the cache. A cache failure is never fatal."""
    feed_version = snapshot.get("provenance", {}).get("feed_version")
    if not feed_version:
        return
    path = _cache_file(offer_code, feed_version)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
        temp.replace(path)
    except OSError as exc:
        _LOG.warning("Could not cache pricing snapshot at %s: %s", path, exc)


# =============================================================================
# BUNDLED SNAPSHOT
# =============================================================================


def load_bundled_snapshot(offer_code: str) -> dict[str, Any] | None:
    """Read the committed ``reference/`` snapshot for one offer, or None."""
    entry = EXTRACTORS.get(offer_code)
    if not entry:
        return None
    path = _REFERENCE_DIR / entry[0]
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        _LOG.warning("Cannot read bundled pricing snapshot %s: %s", path, exc)
        return None


def _bundled_provenance(snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
    """Normalise a bundled snapshot's provenance, old schema or new."""
    existing = dict(snapshot.get("provenance") or {})
    existing.setdefault("feed_version", None)
    existing.setdefault("feed_publication_date", snapshot.get("snapshot_date", ""))
    existing.setdefault("retrieved_at", snapshot.get("generated_at", ""))
    existing["source"] = SOURCE_BUNDLED
    existing["snapshot_date"] = snapshot.get("snapshot_date", "")
    existing["fallback_reason"] = reason
    return existing


# =============================================================================
# SINGLE ENTRY POINT
# =============================================================================

_PRICING_LOCK = threading.Lock()
_PRICING_CACHE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}


def get_pricing(
    offer_code: str,
    live_feed_enabled: bool = DEFAULT_LIVE_FEED_ENABLED,
    cache_ttl_hours: float = DEFAULT_CACHE_TTL_HOURS,
    max_feed_bytes: int = DEFAULT_MAX_FEED_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Return ``(records, provenance)`` for one offer -- the only pricing entry point.

    Resolution order, all in one code path so live and bundled data are served
    identically and differ only in what provenance says:

    1. Process cache, if this offer was already resolved this run.
    2. Bundled ``reference/`` snapshot, loaded first so a fallback is always in
       hand before any network call is attempted.
    3. Live feed, when enabled: resolve the current version, serve a cached
       artifact for that exact version if one is fresh, otherwise check the
       feed's byte size against ``max_feed_bytes`` and stream it.

    Any failure at step 3 -- no egress, DNS blocked, proxy denial, timeout,
    budget exhausted, oversized feed -- falls back to the bundled snapshot with
    the reason recorded in provenance. An export is never blocked or delayed
    past ``max_seconds`` by pricing.

    Args:
        offer_code: An offer code present in :data:`EXTRACTORS`.
        live_feed_enabled: Attempt the network at all.
        cache_ttl_hours: How long a cached artifact for a still-current version
            is reused. 0 means no expiry.
        max_feed_bytes: Refuse to stream a feed larger than this. The host
            serves no compression, so this is literal wire bytes.
        timeout_seconds: Per-socket connect/read timeout.
        max_seconds: Wall-clock budget for the whole fetch.

    Returns:
        ``(records, provenance)``. ``records`` is ``{}`` only when the bundled
        snapshot is also unreadable, in which case provenance says so -- callers
        must treat an empty map as "no pricing", never as "nothing costs
        anything".
    """
    with _PRICING_LOCK:
        if offer_code in _PRICING_CACHE:
            return _PRICING_CACHE[offer_code]

        result = _resolve_pricing(
            offer_code,
            live_feed_enabled,
            cache_ttl_hours,
            max_feed_bytes,
            timeout_seconds,
            max_seconds,
        )
        _PRICING_CACHE[offer_code] = result
        return result


def _resolve_pricing(
    offer_code: str,
    live_feed_enabled: bool,
    cache_ttl_hours: float,
    max_feed_bytes: int,
    timeout_seconds: float,
    max_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundled = load_bundled_snapshot(offer_code)

    if not live_feed_enabled:
        return _bundled_result(bundled, "live feed disabled in advanced settings")

    deadline = time.monotonic() + max_seconds
    try:
        version, publication_date = resolve_current_version(
            offer_code, FEED_REGIONS[0], timeout_seconds
        )
    except FeedError as exc:
        _LOG.info("Price feed unreachable for %s (%s) -- using bundled snapshot", offer_code, exc)
        return _bundled_result(bundled, "price feed unreachable")

    cached = load_cached_snapshot(offer_code, version, cache_ttl_hours)
    if cached is not None:
        provenance = dict(cached.get("provenance") or {})
        provenance["source"] = SOURCE_CACHED_FEED
        return cached.get("records", {}), provenance

    try:
        total_bytes = sum(
            feed_content_length(offer_code, version, region, timeout_seconds)
            for region in FEED_REGIONS
        )
    except FeedError as exc:
        _LOG.info("Cannot size price feed for %s (%s) -- using bundled snapshot", offer_code, exc)
        return _bundled_result(bundled, "price feed unreachable")

    if total_bytes > max_feed_bytes:
        _LOG.info(
            "Price feed for %s is %d bytes, over the %d byte ceiling -- using bundled snapshot",
            offer_code,
            total_bytes,
            max_feed_bytes,
        )
        return _bundled_result(bundled, "feed exceeds configured size ceiling")

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _bundled_result(bundled, "pricing time budget exhausted")

    try:
        snapshot = build_snapshot(
            offer_code, FEED_REGIONS, timeout_seconds, max_seconds=remaining
        )
    except FeedError as exc:
        _LOG.info("Price feed fetch failed for %s (%s) -- using bundled snapshot", offer_code, exc)
        return _bundled_result(bundled, "price feed fetch failed")

    if not snapshot.get("records"):
        # An empty feed parse is a defect, not an account with no instances.
        _LOG.warning("Price feed for %s parsed to zero records -- using bundled snapshot", offer_code)
        return _bundled_result(bundled, "price feed returned no records")

    store_cached_snapshot(offer_code, snapshot)
    provenance = dict(snapshot["provenance"])
    provenance["source"] = SOURCE_LIVE_FEED
    provenance.setdefault("feed_publication_date", publication_date)
    return snapshot["records"], provenance


def _bundled_result(
    bundled: dict[str, Any] | None, reason: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not bundled:
        return {}, {"source": SOURCE_UNAVAILABLE, "fallback_reason": reason}
    return bundled.get("records", {}), _bundled_provenance(bundled, reason)


def reset_pricing_cache() -> None:
    """Clear the in-process cache. For tests; not part of the export path."""
    with _PRICING_LOCK:
        _PRICING_CACHE.clear()
