#!/usr/bin/env python3
"""
Title: StratusScan Bundled Pricing Snapshot Generator
Version: v0.1.0
Date: SEP-17-2026

Description:
Regenerates a bundled ``reference/*-pricing.json`` snapshot from the AWS Price
List Bulk API public feed. Developer tool -- not on the CloudShell runtime path,
not imported by any exporter. Standard library only regardless.

It shares its extractor with the runtime fetch in ``pricing_feed.py``, so the
committed snapshot and a live fetch of the same feed version produce the same
records. That is deliberate: a fallback that is generated differently from the
live path is a second source of truth wearing the first one's name.

It derives nothing. No multipliers, no scaling across sizes within a family, no
extrapolation from an anchor SKU, no computing GovCloud from commercial, no
computing Windows from Linux. A price with no feed row is written as ``null``.
Issue #296 is what the alternative looks like: 80% of the EC2 commercial block
was synthesized from per-family base rates and overstated cost by 8.6% on
average for seven months.

The feed needs no credentials and no IAM grant.

Usage:
    python tools/refresh_pricing.py --offer AmazonEC2 --dry-run
    python tools/refresh_pricing.py --offer AmazonEC2 --write

Expect a few minutes and roughly 500 MB of download for AmazonEC2: the feed
host serves no compression, and the us-east-1 and us-gov-west-1 CSVs are
302,856,576 and 205,737,309 bytes. Neither is written to disk -- both are
streamed and discarded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pricing_feed  # noqa: E402  (path shim must run first)

REFERENCE_DIR = _REPO_ROOT / "reference"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_existing(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _region_rate(record: dict[str, Any], region: str, field: str) -> Any:
    return (record.get("pricing") or {}).get(region, {}).get(field)


def diff_summary(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """
    Compare an existing snapshot against a freshly generated one.

    A refresh that nobody can review is how bad data survives, so this prints
    what actually changed rather than a record count.
    """
    old_records = old.get("records", {})
    new_records = new.get("records", {})
    old_keys, new_keys = set(old_records), set(new_records)

    lines = [
        f"records: {len(old_keys)} -> {len(new_keys)}",
        f"added:   {len(new_keys - old_keys)}",
        f"removed: {len(old_keys - new_keys)}",
    ]
    if new_keys - old_keys:
        lines.append("  + " + ", ".join(sorted(new_keys - old_keys)[:12]))
    if old_keys - new_keys:
        lines.append("  - " + ", ".join(sorted(old_keys - new_keys)[:12]))

    for region in new.get("provenance", {}).get("regions", []):
        for field in (
            "linux_on_demand_monthly_usd",
            "windows_on_demand_monthly_usd",
            "linux_reserved_1yr_monthly_usd",
        ):
            same = changed = filled = cleared = 0
            worst: list[tuple[float, str, Any, Any]] = []
            for key in sorted(old_keys & new_keys):
                before = _region_rate(old_records[key], region, field)
                after = _region_rate(new_records[key], region, field)
                if before == after:
                    same += 1
                elif before is None:
                    filled += 1
                elif after is None:
                    cleared += 1
                else:
                    changed += 1
                    worst.append((abs(after - before), key, before, after))
            lines.append(
                f"{region} {field}: unchanged={same} changed={changed} "
                f"newly-priced={filled} now-null={cleared}"
            )
            for delta, key, before, after in sorted(worst, reverse=True)[:5]:
                lines.append(f"    {key}: {before} -> {after}  (delta {delta:+.2f})")

    for label, checker in (
        ("vcpu", lambda r: r.get("vcpu")),
        ("memory_gib", lambda r: r.get("memory_gib")),
    ):
        differing = sum(
            1 for key in old_keys & new_keys if checker(old_records[key]) != checker(new_records[key])
        )
        lines.append(f"{label}: {differing} of {len(old_keys & new_keys)} values corrected")

    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate a bundled pricing snapshot from the AWS price feed."
    )
    parser.add_argument(
        "--offer",
        default="AmazonEC2",
        choices=sorted(pricing_feed.EXTRACTORS),
        help="AWS offer code to regenerate.",
    )
    parser.add_argument(
        "--regions",
        default=",".join(pricing_feed.FEED_REGIONS),
        help="Comma-separated Region codes, each pulled from its own feed.",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="Per-socket timeout in seconds."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="Write the regenerated snapshot.")
    group.add_argument(
        "--dry-run", action="store_true", help="Fetch and diff without writing."
    )
    args = parser.parse_args(argv)

    regions = tuple(r.strip() for r in args.regions.split(",") if r.strip())
    target = REFERENCE_DIR / pricing_feed.EXTRACTORS[args.offer][0]

    print(f"Offer:   {args.offer}")
    print(f"Regions: {', '.join(regions)}")
    print(f"Target:  {target}")
    print("Streaming the feed. The body is never retained on disk.\n")

    try:
        snapshot = pricing_feed.build_snapshot(
            args.offer, regions, timeout=args.timeout, progress=lambda line: print(f"  {line}")
        )
    except pricing_feed.FeedError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1

    snapshot["provenance"]["generator_sha256"] = _sha256(Path(__file__))

    print(f"\nExtracted {snapshot['record_count']:,} records "
          f"at feed version {snapshot['provenance']['feed_version']}.\n")

    print("Diff against the committed snapshot:")
    for line in diff_summary(_load_existing(target), snapshot):
        print(f"  {line}")

    if args.dry_run:
        print("\nDry run. Nothing written.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=False)
        handle.write("\n")
    print(f"\nWrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
