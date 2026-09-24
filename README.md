# StratusScanCLI-AWS

[![Version: 0.8.0](https://img.shields.io/badge/version-0.8.0-blue.svg)](https://github.com/ColonelPanicX/StratusScanCLI-AWS/releases)
[![Status: Beta](https://img.shields.io/badge/status-beta-yellow.svg)](#project-status)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![AWS Commercial](https://img.shields.io/badge/AWS-Commercial-orange.svg)](https://aws.amazon.com/)
[![AWS GovCloud](https://img.shields.io/badge/AWS-GovCloud%20(US)-blue.svg)](https://aws.amazon.com/govcloud-us/)

A Python CLI tool for exporting AWS resource inventories to Excel workbooks. Supports 100+ AWS services across Commercial and GovCloud partitions, targeting infrastructure audits, FedRAMP evidence collection, and cost analysis.

[Quick Start](#quick-start) • [Features](#features) • [Installation](#installation) • [Usage](#usage) • [Permissions](#aws-permissions)

---

## Quick Start

```bash
# Clone the repository
git clone https://github.com/ColonelPanicX/StratusScanCLI-AWS.git
cd StratusScanCLI-AWS

# Install dependencies
pip install boto3 pandas openpyxl

# Configure AWS credentials
aws configure

# Run the configuration wizard (recommended)
python configure.py

# Launch StratusScan
python stratusscan.py
```

Select a resource category from the menu and follow the prompts. Exports are saved to the `output/` directory as `.xlsx` files.

---

## Features

### Core Capabilities

- **Hierarchical menu interface**: Organized by resource category with consistent navigation
- **Multi-region scanning**: Scan specific regions or all regions; concurrent execution for performance
- **Dual-partition support**: Full AWS Commercial and AWS GovCloud (US) support with automatic FIPS endpoint injection
- **Account mapping**: Translate AWS account IDs to friendly names via `config.json`
- **Standardized Excel output**: Consistent multi-sheet workbooks with timestamp-based filenames
- **Read-only operations**: No write permissions required; safe for production environments

### Smart Scan

**Smart Scan** automates the discovery-to-export workflow. Launch it from the StratusScan main menu:

1. **Service Discovery**: Scans your AWS account to identify which services are actively in use
2. **Script Recommendations**: Maps discovered services to relevant export scripts
3. **Scope Selection**: Choose Quick Scan (all recommended scripts) or Deep Scan (full catalog)
4. **Batch Execution**: Quick Scan generates a Markdown summary report only (no individual exports). Deep Scan runs all selected scripts with real-time progress output and bundles all export files into a single zip archive on completion.

### Cost Estimation

Built-in cost reference data stored as static JSON files in `reference/`. No live pricing API calls at runtime. Cost columns (`Monthly Cost (On-Demand)` and `Cost Note`) are included in exports for:

- **Compute**: EC2 instances, EKS node groups, EC2 Dedicated Hosts, SageMaker notebook instances and real-time endpoints
- **Compute (actual cost)**: SageMaker training jobs — actual billed cost derived from `BillableTimeInSeconds`, not a monthly estimate
- **Storage**: S3 (Standard storage), EBS volumes, EFS file systems, FSx file systems
- **Database**: RDS, ElastiCache, OpenSearch, Redshift, Neptune, DocumentDB
- **Network**: NAT Gateways

### GovCloud Support

- Automatic partition detection from caller ARN or region
- FIPS endpoints injected automatically for `us-gov-west-1` and `us-gov-east-1`
- Service availability checks guard against calling services unavailable in GovCloud (Cost Explorer, Global Accelerator, Rekognition, Cognito, Comprehend, Connect, Bedrock, and others)
- Four IAM policy files covering Commercial/GovCloud × required/optional permission sets

---

## Installation

### Requirements

- Python 3.10 or higher
- AWS credentials configured (CLI, environment variables, or IAM instance profile)
- Read-only AWS permissions ([see details](#aws-permissions))

### Runtime Dependencies

```bash
pip install boto3 pandas openpyxl
```

Missing packages are detected at script startup with an install prompt, but pre-installing is recommended.

### Developer Installation

```bash
pip install -e ".[dev]"
```

Includes pytest, moto, ruff, black, and mypy.

### AWS Authentication

**AWS CLI (recommended)**
```bash
aws configure
```

**Environment variables**
```bash
export AWS_ACCESS_KEY_ID="your-key"
export AWS_SECRET_ACCESS_KEY="your-secret"
export AWS_DEFAULT_REGION="us-east-1"
```

**IAM instance profile**: Credentials are picked up automatically when running on EC2.

---

## Configuration

StratusScan reads `config.json` for account mappings and default regions. The file is created automatically from `config-template.json` on first run.

### Configuration Wizard

```bash
python configure.py            # Interactive setup
python configure.py --validate  # Full validation (non-interactive)
python configure.py --perms     # Permissions check only
```

The wizard covers account ID-to-name mappings, default region selection (with presets for common setups), and AWS permission validation.

### Manual Configuration

```json
{
  "account_mappings": {
    "123456789012": "PROD-ACCOUNT",
    "234567890123": "DEV-ACCOUNT"
  },
  "organization_name": "YOUR-ORGANIZATION",
  "default_regions": ["us-east-1", "us-west-2"],
  "enabled_services": {
    "trusted_advisor": {
      "enabled": true,
      "note": "Requires Business or Enterprise support plan"
    }
  }
}
```

### CI / Unattended Execution

```bash
STRATUSSCAN_AUTO_RUN=1 STRATUSSCAN_REGIONS=us-east-1,us-west-2 python scripts/ec2_export.py
```

---

## Usage

### Main Menu

```bash
python stratusscan.py
```

Navigate the hierarchical menu by category (Compute, Storage, Network, IAM, Security, Cost, Smart Scan). Each exporter prompts for region selection and saves its output to `output/`.

### Direct Script Execution

Individual exporters can be run standalone:

```bash
python scripts/ec2_export.py
python scripts/iam_export.py
python scripts/s3_export.py
```

Each script self-contains its region prompting and output logic.

### Smart Scan

Access Smart Scan from the main menu. The workflow:

1. **Discovery phase**: StratusScan scans the account and identifies active AWS services
2. **Analysis**: Discovered services are mapped to relevant export scripts
3. **Selection**: Choose Quick Scan (Markdown report only, no individual exports) or Deep Scan (runs all selected scripts), with optional manual deselection
4. **Execution**: Scripts run sequentially with live progress output. Quick Scan writes a Markdown summary; Deep Scan bundles all export files into a zip archive on completion.

### Region Selection

When prompted for regions:
- Enter a specific region code: `us-east-1`, `eu-west-1`, `ap-southeast-2`
- Enter `all` to scan all opted-in regions for the current partition
- GovCloud sessions default to `us-gov-west-1` and `us-gov-east-1`

### Output Archives

From the main menu, **Output Management > Create Output Archive** zips all files in `output/` to `ACCOUNT-NAME-export-MM.DD.YYYY.zip`.

---

## AWS Permissions

StratusScan requires read-only access. Four purpose-built IAM policies are provided in `policies/`:

### AWS Commercial

| Policy file | Purpose |
|---|---|
| `commercial-required-permissions.json` | Core StratusScan functionality (~230 actions) |
| `commercial-optional-permissions.json` | ML/AI, CloudFront, Global Accelerator (~38 additional actions) |

### AWS GovCloud (US)

| Policy file | Purpose |
|---|---|
| `govcloud-required-permissions.json` | Core functionality, FedRAMP-compatible |
| `govcloud-optional-permissions.json` | Available ML/AI services in GovCloud |

**GovCloud service availability notes:**
- Global Accelerator, Cost Explorer, Trusted Advisor, Rekognition, Cognito, Comprehend, Connect, and Bedrock are not available in GovCloud — StratusScan skips these automatically
- CloudFront operates outside the ITAR boundary and is excluded from GovCloud policy files

All policies are compatible with both IAM (attach to users/roles) and IAM Identity Center (use as Permission Set inline policies).

See [`policies/README.md`](policies/README.md) for full details.

---

## Supported AWS Resources

StratusScan ships with **100+ export scripts** across these categories:

<details>
<summary><b>Compute</b></summary>

- EC2 Instances — instance details, OS, network config, cost estimates
- ECS — clusters, services, tasks, container instances
- EKS — clusters, node groups, configurations
- RDS — engines, storage, connection details
- Lambda — functions, runtimes, configurations
- Auto Scaling Groups — ASGs, instances, scaling policies, lifecycle hooks
- Elastic Beanstalk, App Runner, Batch, Lightsail

</details>

<details>
<summary><b>Storage</b></summary>

- S3 — buckets, encryption, versioning, object counts via CloudWatch metrics
- EBS — volumes, snapshots, encryption status
- EFS — file systems, mount targets, access points
- FSx, Glacier/Archive, Storage Gateway

</details>

<details>
<summary><b>Network</b></summary>

- VPC — VPCs, subnets, NAT gateways, peering, Elastic IPs
- Elastic Load Balancers — Classic, ALB, NLB, target groups, listeners
- Route 53 — hosted zones, records, resolver endpoints
- Security Groups, Network ACLs, Route Tables
- VPN, Direct Connect, Global Accelerator (Commercial only), Transit Gateway

</details>

<details>
<summary><b>IAM and Identity</b></summary>

- IAM — users, roles, policies, MFA devices, access keys, group memberships
- IAM Identity Center — users, groups, permission sets, assignments
- AWS Organizations — structure, accounts, OUs, SCPs
- AWS Access Analyzer — findings (external access + unused access analyzers)

</details>

<details>
<summary><b>Security</b></summary>

- Security Hub — findings, standards, compliance status
- GuardDuty — detectors, findings
- CloudTrail — trails, event selectors
- Config — rules, compliance status
- KMS — keys, key policies
- WAF — Web ACLs, rules, IP sets, rule groups
- Certificate Manager, Secrets Manager, Macie

</details>

<details>
<summary><b>Data and Analytics</b></summary>

- DynamoDB, Redshift, ElastiCache, MemoryDB
- Kinesis, MSK (Kafka), OpenSearch
- Glue, Athena
- Neptune — clusters, instances, snapshots, Neptune Analytics graphs

</details>

<details>
<summary><b>Cost and Governance</b></summary>

- AWS Backup — vaults, plans, selections, jobs
- Cost Optimization Hub (Commercial only)
- Compute Optimizer (Commercial only)
- Trusted Advisor (Commercial only; requires Business+ support)
- Reserved Instances, Savings Plans

</details>

---

## Output Files

All exports use a consistent naming convention:

```
{ACCOUNT-NAME}-{resource-type}-{suffix}-export-{MM.DD.YYYY}.xlsx
```

Examples:
```
PROD-ACCOUNT-ec2-all-export-03.03.2026.xlsx
DEV-ACCOUNT-iam-all-export-03.03.2026.xlsx
PROD-ACCOUNT-waf-all-export-03.03.2026.xlsx
```

Most exports produce multi-sheet workbooks — one sheet per resource type (e.g., the ELB export has Load Balancers, Target Groups, and Listeners sheets).

---

## Troubleshooting

**Missing dependencies**
```bash
pip install boto3 pandas openpyxl
```

**AWS credentials not found**
```bash
aws configure
# or
export AWS_ACCESS_KEY_ID="..." AWS_SECRET_ACCESS_KEY="..."
```

**Permission denied errors**
- Verify read-only policies are attached ([see permissions](#aws-permissions))
- Trusted Advisor requires AWS Business or Enterprise Support
- Cost Explorer requires opt-in and appropriate permissions

**GovCloud connection issues**
- StratusScan auto-injects FIPS endpoints; no manual configuration needed
- Verify credentials are for a GovCloud partition (`aws-us-gov`) profile

**Getting help**
1. Check `logs/` for detailed error messages
2. Validate credentials: `aws sts get-caller-identity`
3. Validate permissions: `python configure.py --perms`
4. Open an issue on GitHub with the relevant log excerpt

---

## Contributing

```bash
# Fork, clone, and install dev deps
git clone https://github.com/yourusername/StratusScanCLI-AWS.git
cd StratusScanCLI-AWS
pip install -e ".[dev]"

# Run tests
pytest
pytest -m "not slow"   # Skip slow integration tests

# Code quality
ruff check .
black .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the exporter script template and contribution guidelines.

---

## Project Status

**Current version: 0.8.0-beta**

StratusScanCLI-AWS is in active beta development. The API and output format may change before the 1.0.0 stable release. All active development occurs on the `dev` branch; `main` is release snapshots only.

### What's new in v0.8.0

- **Pricing rates now come from AWS, not a bundled guess**: Cost estimates are sourced from AWS's public Price List Bulk feed — fetched at runtime when reachable, cached by feed version, and falling back to a bundled snapshot otherwise. An audit found that 80% of the previous EC2 commercial records had been synthesized from a per-family base rate rather than retrieved, averaging 8.6% above published rates; GovCloud rates were accurate. `reference/ec2-pricing.json` is regenerated from the feed and now agrees with it on every field (1,403/1,403 records), which also corrected 90 vCPU and 155 memory values that had served as the authoritative instance-spec source. Rates are never derived: no multipliers, no scaling across sizes, no computing Windows from Linux. A type with no feed row reports N/A instead of a fabricated number (48 types, including dedicated-host-only EC2 Mac and sizes that do not exist). Capacity Block rows priced at $0.00 no longer shadow real on-demand rates — one GPU type would have exported at $0.00/month instead of $83,171. The `Cost Note` column records which source priced each row. `tools/refresh_pricing.py` regenerates snapshots; hand-editing is out (#296, #297).
- **Billing export corrected**: The Cost Explorer query now follows pagination instead of reading only the first page, and accumulates costs per month and service rather than overwriting them. `TimePeriod.End` is exclusive, so every prior export silently dropped the final day of its last month — fixed. The metric moved from `BlendedCost`, which averages rates across a consolidated billing family, to `NetUnblendedCost`, which reflects what an account was actually charged. "Last 12 months" now spans 12 calendar months rather than 13. A new `About` sheet records the metric, period, account and tool version, and missing Cost Explorer permissions or a GovCloud run now leave a visible skip marker workbook instead of nothing at all. **Behavior change:** totals are higher and differently allocated than prior exports; earlier billing workbooks are not comparable (#289).
- **Service Discovery archives explain themselves**: Sweep archives land in `output/service-discovery/` instead of alongside one-off exports, and each zip now carries a Markdown scan report — services in use, what ran, what was skipped and why, missing prior outputs, and the bill cross-check. A recipient holding the archive can tell what happened without the operator's terminal. Resumed sessions now archive the whole session rather than only the scripts from the resumed run, and no longer re-prompt for regions (#291, #294).
- **RDS deployment placement**: `Status`, `Multi-AZ` and `Availability Zone(s)` columns, read from responses the exporter already fetched — no new API calls or IAM. Cluster members report their own Availability Zone; the cluster's `AvailabilityZones` list is deliberately not exported, since it records where instances *can* be created rather than where they run (#292).

### Previously in v0.7.0

- **Unattended audit mode (manual half)**: The pieces needed to run a full audit without a human at the keyboard. `--run-all` executes every exporter in one headless pass and writes a spreadsheet run report; `--org-scan` extends that across every account in an AWS Organization via `--scan-role`; exports can be delivered straight to S3 instead of the local `output/` directory; and each run emits a manifest recording what ran, what it found, and what failed. Empty exports are suppressed by default — an exporter that finds nothing writes no spreadsheet, but the run report still records it as "ran, 0 assets" (#175, #199, #202, #203, #204).
- **One interaction voice across the CLI**: Every interactive surface — menus, multi-selects, confirmations, the config wizards — now speaks a single contract. Numbered `1..N` menus, `b` = back / `x` = main menu / `q` = quit everywhere, two-tier confirmations, and consistent ✅/❌ status glyphs. This replaces five competing prompt dialects that had accumulated across the codebase; the Cost Management all-in-one menu in particular no longer changes input style between steps. Three latent bugs surfaced and were fixed in the process: the Cost bundle silently skipped Compute Optimizer, Smart Scan ran everything when `questionary` was absent instead of offering a real fallback, and the services-in-use exporter ignored the region back/exit signal (#252).
- **Path-handling hardening**: File paths built from caller-supplied values are now validated against a single containment helper, so a crafted account name in `config.json` cannot steer an export outside its intended directory (CWE-73). Closes a real bug found alongside it — the Smart Scan checklist was written to the current working directory rather than `output/` (#253).
- **Smart Scan discovery**: `Amazon CloudWatch Logs` now resolves to the CloudWatch exporter instead of being misclassified as having no exporter (#251).

### Previously in v0.6.0

- **Fail-loud collection (reliability)**: All 96 region- and account-scoped exporters now surface collection failures instead of silently emitting empty results. On an API failure an exporter writes a `*-FAILED-*.txt` marker and exits non-zero; a genuinely empty account still exits 0. This makes partial or failed audits detectable rather than indistinguishable from an empty account. **Behavior change:** automation keying on exit codes will now see a non-zero exit on collection errors.
- **Security hardening**: Python floor raised to 3.10 (pulls the patched `urllib3 2.7.0`); subprocess launcher containment guard (only known, validated scripts can run); log-forging (CWE-117) and file-path (CWE-73) cleanup; `os.system` screen-clear replaced with an ANSI escape.
- **Smart Scan discovery**: 27 new service detectors close the discovery coverage gap; a Cost Explorer bill cross-check reconciles discovered services against actual spend; discovery-catalog name resolution stops Deep Scan from dropping services.
- **Correctness**: fixed non-pageable `get_paginator` calls across 18 exporters; added "All \<Category\>" bundle exports for every menu category; fixed the cross-account session env-var path; added a static API-contract smoke net.
- **Modernization**: ruff CI gate added; type annotations modernized to PEP 585/604.

### Previously in v0.5.0

- **Multi-account org-scan**: run any exporter across every account in an AWS Organization via STS role assumption (#128).
- **CLI flags and non-interactive args**: `--version`, `--dry-run`, `--verbose`, plus per-exporter CLI arguments for unattended execution (#10, #169).
- **Signal-based navigation**: back / exit-to-main / quit flow control across all interactive menus (#170).
- **Export format choice**: xlsx (default) or universal CSV (#174).
- **GovCloud FIPS fix**: `use_fips_endpoint` set on the client `Config`, not passed as a `client()` kwarg.
- **Scan sessions**: progress tracking and resume for org-scan and smart-scan (#189).
- **Internals**: shared helpers consolidated into a single `utils.py` (#177); moto smoke suite covering 101 exporter scripts.

### Previously in v0.4.0

- **Cost columns for 14 exporters**: `Monthly Cost (On-Demand)` and `Cost Note` columns added to ElastiCache, OpenSearch, Redshift, Neptune, DocumentDB, S3, EFS, FSx, NAT Gateways, EKS node groups, SageMaker notebook instances and real-time endpoints, and EC2 Dedicated Hosts. EC2 and RDS already had cost columns. SageMaker training jobs show actual billed job cost (not a monthly estimate) derived from `BillableTimeInSeconds`.
- **Pricing reference overhaul**: 11 static JSON pricing files in `reference/` replace the legacy CSV approach. Covers `ml.*` SageMaker instances, dedicated host families, FSx storage types, EFS tier rates, Neptune, DocumentDB, ElastiCache, OpenSearch, Redshift, NAT Gateways, and S3.
- **Smart Scan Deep Scan archive**: After a Deep Scan completes, all export files are bundled into a single zip archive for easy transport.
- **Service discovery improvements**: Short per-future boto3 timeouts prevent Timestream and other slow-service hangs; progress heartbeat added for the discovery phase.

### Previously in v0.3.0

- **Smart Scan redesign**: Fully integrated orchestrator in the main menu with Quick Scan and Deep Scan modes.
- **API correctness audit**: 6-PR remediation pass across all 107 exporter scripts covering runtime crashes, pagination gaps, GovCloud guard improvements, and missing API coverage.
- **Configure.py refactor**: Preset-based region selection, streamlined 4-step flow, improved GovCloud detection.
- **Main menu styling**: Box-drawing UI consistent with configure.py design standard applied throughout.

### Roadmap

| Version | Target | Notes |
|---|---|---|
| `0.7.x` | Patch releases | Coverage gaps, pricing data refresh, live-account hardening |
| `1.0.0` | Planned | Stable API; scheduled unattended audit mode — one-command deployment of the recurring runner (the manual cross-account and S3-delivery half shipped in `0.7.0`) |

---

## Versioning

StratusScanCLI-AWS uses [Semantic Versioning](https://semver.org/).

| Series | Status | Notes |
|---|---|---|
| `3.x.x` | Deprecated | Superseded by governance reset at `0.1.0` |
| `0.1.x` | Superseded | Initial relaunch |
| `0.2.x` | Superseded | Backend stabilization, pricing data v2 |
| `0.3.x` | Superseded | Smart Scan orchestrator, full API correctness audit |
| `0.4.x` | Superseded | Cost columns for 14 exporters, pricing JSON overhaul |
| `0.5.x` | Superseded | Multi-account org-scan, CLI flags, signal-based navigation, export formats |
| `0.6.x` | Superseded | Fail-loud collection sweep, security hardening, smart-scan discovery |
| `0.7.x` | Current (Beta) | Unattended audit mode (manual half), single-voice CLI, path-handling hardening |
| `1.0.0` | Planned | Stable API; scheduled unattended audit mode |

---

## Branch Workflow

| Branch | Purpose |
|---|---|
| `dev` | Primary development — all PRs target `dev` first |
| `main` | Release snapshots only — no direct commits |
| `feat/*`, `fix/*` | Short-lived topic branches targeting `dev` |

All work originates from a GitHub Issue. PRs must reference issues using closing keywords (`Closes #N`, `Fixes #N`).

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

Built with assistance from [Claude Code](https://claude.ai/code). Powered by [AWS SDK for Python (Boto3)](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html). Excel export via [pandas](https://pandas.pydata.org/) and [openpyxl](https://openpyxl.readthedocs.io/). AWS mocking in tests via [moto](https://github.com/getmoto/moto).
