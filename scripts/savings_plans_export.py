#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Savings Plans Export Tool
Date: NOV-09-2025

Description:
Exports AWS Savings Plans inventory and, when enabled, Cost Explorer
utilization into an Excel workbook.

Always collected (savingsplans:DescribeSavingsPlans):
- Active and queued Savings Plans: type, state, payment option, term,
  hourly commitment, currency, upfront/recurring payment, start/end, tags
- Summary: plan counts and hourly commitment totals by plan type

Collected only when Cost Explorer queries are enabled (paid API, opt-in):
- Monthly utilization for all Savings Plans (ce:GetSavingsPlansUtilization):
  utilization %, total/used/unused commitment, net savings, on-demand cost
  equivalent, amortized commitment -- last N complete months
- Per-plan monthly utilization (ce:GetSavingsPlansUtilizationDetails), one
  query per month because that operation returns no per-month breakdown

Cost Explorer queries are skipped in aws-us-gov (no Cost Explorer there) and
are off by default because AWS charges $0.01 per paginated request. Enable
with ``python advanced_settings.py`` -> Configure Cost Explorer Queries, or
for one run with ``STRATUSSCAN_CE_UTILIZATION=1``. Every Cost Explorer sheet
states whether data was returned, AWS had none, the query was never made
(disabled / GovCloud), or the lookup failed.
"""

import datetime
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# Add path to import utils module
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()

    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))

    try:
        import utils
    except ImportError:
        print("ERROR: Could not import the utils module. Make sure utils.py is in the StratusScan directory.")
        sys.exit(1)
args = utils.parse_script_args("Export AWS Savings Plans to Excel")


def _build_savings_plan_row(plan: dict[str, Any]) -> dict[str, Any]:
    """
    Build the export row for a single Savings Plan.

    Extracted so per-plan processing can be wrapped in try/except by the
    caller: a malformed plan entry must not sink the whole account-scope
    collection. All fields are read with ``.get()`` and a safe default for
    the same reason (the ``plan['Hourly Commitment']`` style hard subscript
    flagged in .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md
    is a deterministic ``KeyError`` candidate on divergent plan variants).

    Args:
        plan: A single savingsPlans entry from describe_savings_plans.

    Returns:
        dict: The assembled savings plan row.
    """
    plan_id = plan.get('savingsPlanId', 'N/A')
    plan_arn = plan.get('savingsPlanArn', 'N/A')

    print(f"  Processing savings plan: {plan_id}")

    # Basic info
    plan_type = plan.get('savingsPlanType', 'N/A')
    payment_option = plan.get('paymentOption', 'N/A')
    state_val = plan.get('state', 'N/A')

    # Commitment
    commitment = plan.get('commitment', '0')
    currency = plan.get('currency', 'USD')

    # Convert Decimal to float for Excel
    if isinstance(commitment, Decimal):
        commitment = float(commitment)

    # Term
    term_duration = plan.get('termDurationInSeconds', 0)
    # Convert seconds to years
    term_years = term_duration / (365.25 * 24 * 60 * 60)

    # Dates
    start = plan.get('start', '')
    if start:
        start = start.strftime('%Y-%m-%d %H:%M:%S') if isinstance(start, datetime.datetime) else str(start)

    end = plan.get('end', '')
    if end:
        end = end.strftime('%Y-%m-%d %H:%M:%S') if isinstance(end, datetime.datetime) else str(end)

    # EC2 instance family (if applicable)
    ec2_instance_family = plan.get('ec2InstanceFamily', 'N/A')

    # Region (if applicable)
    region = plan.get('region', 'N/A')

    # Upfront payment
    upfront = plan.get('upfrontPaymentAmount', '0')
    if isinstance(upfront, Decimal):
        upfront = float(upfront)

    # Recurring payment
    recurring = plan.get('recurringPaymentAmount', '0')
    if isinstance(recurring, Decimal):
        recurring = float(recurring)

    # Description/offering ID
    offering_id = plan.get('offeringId', 'N/A')

    # Tags
    tags = plan.get('tags', {}) or {}
    tags_str = ', '.join([f"{k}={v}" for k, v in tags.items()]) if tags else 'None'

    return {
        'Savings Plan ID': plan_id,
        'State': state_val,
        'Savings Plan Type': plan_type,
        'Payment Option': payment_option,
        'Hourly Commitment': commitment,
        'Currency': currency,
        'Term (Years)': round(term_years, 1),
        'Start Date': start if start else 'N/A',
        'End Date': end if end else 'N/A',
        'EC2 Instance Family': ec2_instance_family,
        'Region': region,
        'Upfront Payment': upfront,
        'Recurring Payment': recurring,
        'Offering ID': offering_id,
        'Tags': tags_str,
        'Savings Plan ARN': plan_arn
    }


def collect_savings_plans(states: list[str]) -> list[dict[str, Any]]:
    """
    Collect Savings Plans information.

    Not wrapped in ``aws_error_handler`` and does not swallow errors to an
    empty list: a swallowed error here would be indistinguishable from a
    genuinely empty account (no savings plans purchased), producing silent
    data loss (see the 07.15.2026 / 07.16.2026 silent-collection-failure
    audits). Savings Plans is a global, account-scope service (not
    multi-region — see scripts/shield_export.py for the account-scope
    reference pattern this follows). Account-scope failures (client
    creation, pagination) are allowed to raise so the caller (main) can
    record this scope as *failed* rather than *empty*. Per-plan errors are
    contained internally (logged and skipped).

    Args:
        states: List of states to filter (e.g., ['active', 'queued'])

    Returns:
        list: List of dictionaries with savings plan information.

    Raises:
        Exception: Any AWS/pagination error for the account scope (caller
            records it as a failed scope; it is never masked as empty).
    """
    print(f"\n=== COLLECTING SAVINGS PLANS (States: {', '.join(states)}) ===")
    all_plans = []
    total_processed = 0
    skipped = 0

    # Savings Plans is a global service but requires a region
    # Savings Plans is a global service - use partition-aware home region
    home_region = utils.get_partition_default_region()
    sp_client = utils.get_boto3_client('savingsplans', region_name=home_region)

    for state in states:
        print(f"\nProcessing state: {state}")

        # describe_savings_plans has no boto3 paginator; page manually via nextToken.
        next_token = None
        while True:
            params: dict[str, Any] = {'states': [state], 'maxResults': 100}
            if next_token:
                params['nextToken'] = next_token

            page = sp_client.describe_savings_plans(**params)
            savings_plans = page.get('savingsPlans', [])

            # Process each plan. One malformed plan must not sink the whole
            # scope, so each is built inside try/except; failures are logged
            # and skipped.
            for plan in savings_plans:
                total_processed += 1

                try:
                    all_plans.append(_build_savings_plan_row(plan))
                except Exception as e:
                    skipped += 1
                    plan_id = plan.get('savingsPlanId', 'Unknown') if isinstance(plan, dict) else 'Unknown'
                    utils.log_error(f"Skipping savings plan '{plan_id}' due to a processing error", e)
                    continue

            next_token = page.get('nextToken')
            if not next_token:
                break

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_processed} savings plan(s) were skipped due to "
            "processing errors (see log above); the remaining plans were still collected."
        )

    utils.log_success(f"Total savings plans collected: {len(all_plans)}")
    return all_plans


# ---------------------------------------------------------------------------
# Cost Explorer utilization (Issue #285)
# ---------------------------------------------------------------------------

SHEET_CE_MONTHLY = 'SP Utilization (Monthly)'
SHEET_CE_PER_PLAN = 'SP Utilization (Per Plan)'
SHEET_CE_STATUS = 'SP Utilization Status'

OP_MONTHLY = 'GetSavingsPlansUtilization'
OP_PER_PLAN = 'GetSavingsPlansUtilizationDetails'

# DataType values documented for GetSavingsPlansUtilizationDetails. Requested
# explicitly: the API reference does not state what an omitted DataType returns.
_DETAIL_DATA_TYPES = ['ATTRIBUTES', 'UTILIZATION', 'AMORTIZED_COMMITMENT', 'SAVINGS']


def _ce_number(value: Any) -> Any:
    """Cost Explorer returns amounts as strings; convert when numeric, else None."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sp_metric_columns(block: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten the Utilization / Savings / AmortizedCommitment blocks shared by
    SavingsPlansUtilizationByTime, SavingsPlansUtilizationDetail and
    SavingsPlansUtilizationAggregates. Values are read as returned; nothing
    is computed.
    """
    util = block.get('Utilization', {}) or {}
    savings = block.get('Savings', {}) or {}
    amort = block.get('AmortizedCommitment', {}) or {}
    return {
        'Utilization %': _ce_number(util.get('UtilizationPercentage')),
        'Total Commitment': _ce_number(util.get('TotalCommitment')),
        'Used Commitment': _ce_number(util.get('UsedCommitment')),
        'Unused Commitment': _ce_number(util.get('UnusedCommitment')),
        'Net Savings': _ce_number(savings.get('NetSavings')),
        'On-Demand Cost Equivalent': _ce_number(savings.get('OnDemandCostEquivalent')),
        'Amortized Recurring Commitment': _ce_number(amort.get('AmortizedRecurringCommitment')),
        'Amortized Upfront Commitment': _ce_number(amort.get('AmortizedUpfrontCommitment')),
        'Total Amortized Commitment': _ce_number(amort.get('TotalAmortizedCommitment')),
    }


def query_sp_utilization_monthly(
    ce_client, periods: list[tuple[str, str]], counter: dict[str, int]
) -> list[dict[str, Any]]:
    """
    One GetSavingsPlansUtilization call over the whole window, MONTHLY.

    The operation has no pagination token in its request or response (API
    reference), so a single request returns every month.

    Returns:
        Rows, one per month plus a period TOTAL row when AWS returned one.
        Raises on any API error (caller classifies).
    """
    counter['requests'] += 1
    response = ce_client.get_savings_plans_utilization(
        TimePeriod={'Start': periods[0][0], 'End': periods[-1][1]},
        Granularity='MONTHLY',
    )
    rows: list[dict[str, Any]] = []
    for entry in response.get('SavingsPlansUtilizationsByTime', []) or []:
        tp = entry.get('TimePeriod', {}) or {}
        start = tp.get('Start', '')
        row = {
            'Month': start[:7] if start else 'N/A',
            'Period Start': start or 'N/A',
            'Period End (exclusive)': tp.get('End', 'N/A'),
        }
        row.update(_sp_metric_columns(entry))
        rows.append(row)
    total = response.get('Total')
    if rows and total:
        row = {
            'Month': 'TOTAL (period)',
            'Period Start': periods[0][0],
            'Period End (exclusive)': periods[-1][1],
        }
        row.update(_sp_metric_columns(total))
        rows.append(row)
    return rows


def _plan_id_from_arn(arn: str) -> str:
    """Savings Plan ARNs end in 'savingsplan/<id>'; return the id or 'N/A'."""
    marker = 'savingsplan/'
    return arn.split(marker, 1)[1] if marker in arn else 'N/A'


def query_sp_utilization_per_plan(
    ce_client,
    periods: list[tuple[str, str]],
    inventory: dict[str, dict[str, Any]],
    counter: dict[str, int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    GetSavingsPlansUtilizationDetails, one call-chain per month.

    The operation returns one aggregate per plan for the whole TimePeriod and
    "doesn't support granular or grouped data (daily/monthly)" (API
    reference), so each month is its own query. Paginated by NextToken; there
    is no boto3 paginator for it.

    Args:
        inventory: Savings Plan rows keyed by ARN (from DescribeSavingsPlans)
            used only to label rows with ID/type/commitment.
        counter: ``{'requests': n}``, incremented before every request so the
            count stays right even when a request raises.

    Returns:
        (rows, months_unavailable). A month that raises
        DataUnavailableException is recorded and skipped; any other error
        raises (caller classifies).
    """
    rows: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for start, end in periods:
        next_token = None
        try:
            while True:
                params: dict[str, Any] = {
                    'TimePeriod': {'Start': start, 'End': end},
                    'DataType': _DETAIL_DATA_TYPES,
                }
                if next_token:
                    params['NextToken'] = next_token
                counter['requests'] += 1
                response = ce_client.get_savings_plans_utilization_details(**params)
                for detail in response.get('SavingsPlansUtilizationDetails', []) or []:
                    arn = detail.get('SavingsPlanArn', 'N/A') or 'N/A'
                    inv = inventory.get(arn, {})
                    attributes = detail.get('Attributes', {}) or {}
                    row = {
                        'Month': start[:7],
                        'Savings Plan ID': inv.get('Savings Plan ID') or _plan_id_from_arn(arn),
                        'Savings Plan Type': inv.get('Savings Plan Type', 'Not in active/queued inventory'),
                        'Payment Option': inv.get('Payment Option', 'N/A'),
                        'Hourly Commitment': inv.get('Hourly Commitment', 'N/A'),
                    }
                    row.update(_sp_metric_columns(detail))
                    row['Attributes'] = (
                        '; '.join(f"{k}={v}" for k, v in sorted(attributes.items())) or 'None'
                    )
                    row['Savings Plan ARN'] = arn
                    rows.append(row)
                next_token = response.get('NextToken')
                if not next_token:
                    break
        except Exception as exc:  # noqa: BLE001 - classified below or re-raised
            _status, _detail, is_failure = utils.classify_cost_explorer_error(exc)
            if is_failure:
                raise
            unavailable.append(start[:7])
            continue
    rows.sort(key=lambda r: (r['Month'], r['Savings Plan ARN']))
    return rows, unavailable


def _status_frame_rows(status: str, detail: str) -> list[dict[str, Any]]:
    """Single-row body for a Cost Explorer sheet that holds no data."""
    return [{'Status': status, 'Detail': detail}]


def collect_sp_cost_explorer(
    partition: str,
    settings: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
    today: Optional[datetime.date] = None,
) -> dict[str, Any]:
    """
    Run (or decline to run) the Savings Plans Cost Explorer queries.

    Never raises. Returns a result describing each query's stated outcome:
    ``{'monthly': {...}, 'per_plan': {...}, 'requests': int, 'periods': [...],
    'failed_scopes': [(scope, message), ...], 'settings': settings,
    'partition': partition}``. Each query dict has ``status``, ``detail`` and
    ``rows``.
    """
    periods = utils.cost_explorer_month_window(settings['lookback_months'], today)
    result: dict[str, Any] = {
        'periods': periods,
        'requests': 0,
        'failed_scopes': [],
        'settings': settings,
        'partition': partition,
    }

    if not utils.is_service_available_in_partition('ce', partition):
        detail = (
            "Cost Explorer does not exist in the aws-us-gov partition. Savings Plans that "
            "apply to GovCloud usage are purchased in, and reported by, the associated "
            "standard (commercial) account -- run this export there."
        )
        for key in ('monthly', 'per_plan'):
            result[key] = {'status': utils.CE_STATUS_GOVCLOUD, 'detail': detail, 'rows': []}
        return result

    if not settings['enabled']:
        detail = f"Setting source: {settings['source']}. {utils.cost_explorer_enable_hint()}"
        for key in ('monthly', 'per_plan'):
            result[key] = {'status': utils.CE_STATUS_DISABLED, 'detail': detail, 'rows': []}
        return result

    # Cost Explorer's API endpoint is ce.us-east-1.amazonaws.com (Cost
    # Management user guide, ce-api.html); GovCloud never reaches this line.
    try:
        ce_client = utils.get_boto3_client('ce', region_name='us-east-1')
    except Exception as exc:  # noqa: BLE001 - stated on the sheet, never raised
        detail = f"Could not create Cost Explorer client: {exc}"
        for key, scope in (('monthly', 'cost_explorer_sp_utilization'),
                           ('per_plan', 'cost_explorer_sp_utilization_details')):
            result[key] = {'status': utils.CE_STATUS_FAILED, 'detail': detail, 'rows': []}
            result['failed_scopes'].append((scope, detail))
        return result
    counter = {'requests': 0}

    # Monthly aggregate.
    denied = False
    try:
        rows = query_sp_utilization_monthly(ce_client, periods, counter)
        if rows:
            result['monthly'] = {'status': utils.CE_STATUS_DATA, 'detail': f"{len(rows)} row(s).", 'rows': rows}
        else:
            result['monthly'] = {
                'status': utils.CE_STATUS_NO_DATA,
                'detail': f"{OP_MONTHLY} returned no utilization periods for this window.",
                'rows': [],
            }
    except Exception as exc:  # noqa: BLE001 - every outcome is stated on the sheet
        status, detail, is_failure = utils.classify_cost_explorer_error(exc)
        detail = f"ce:{OP_MONTHLY} -- {detail}"
        result['monthly'] = {'status': status, 'detail': detail, 'rows': []}
        if is_failure:
            result['failed_scopes'].append(('cost_explorer_sp_utilization', detail))
            denied = utils.is_cost_explorer_access_denied(exc)

    # Per-plan, one query per month. After a denial, skip it: every month
    # would be another denied request.
    if denied:
        detail = (
            f"Not attempted: ce:{OP_MONTHLY} was denied, so ce:{OP_PER_PLAN} was skipped. "
            "Grant both actions."
        )
        result['per_plan'] = {'status': utils.CE_STATUS_FAILED, 'detail': detail, 'rows': []}
        result['failed_scopes'].append(('cost_explorer_sp_utilization_details', detail))
        result['requests'] = counter['requests']
        return result

    try:
        rows, unavailable = query_sp_utilization_per_plan(ce_client, periods, inventory, counter)
        note = f" Months with DataUnavailableException: {', '.join(unavailable)}." if unavailable else ""
        if rows:
            result['per_plan'] = {
                'status': utils.CE_STATUS_DATA,
                'detail': f"{len(rows)} plan-month row(s).{note}",
                'rows': rows,
            }
        elif unavailable and len(unavailable) == len(periods):
            result['per_plan'] = {
                'status': utils.CE_STATUS_UNAVAILABLE,
                'detail': f"AWS reported data unavailable for every month queried.{note}",
                'rows': [],
            }
        else:
            result['per_plan'] = {
                'status': utils.CE_STATUS_NO_DATA,
                'detail': f"{OP_PER_PLAN} returned no Savings Plans for any month queried.{note}",
                'rows': [],
            }
    except Exception as exc:  # noqa: BLE001 - every outcome is stated on the sheet
        _status, detail, _ = utils.classify_cost_explorer_error(exc)
        detail = f"ce:{OP_PER_PLAN} -- {detail} Partial per-plan rows were discarded."
        result['per_plan'] = {'status': utils.CE_STATUS_FAILED, 'detail': detail, 'rows': []}
        result['failed_scopes'].append(('cost_explorer_sp_utilization_details', detail))

    result['requests'] = counter['requests']
    return result


def build_sp_cost_explorer_sheets(ce_result: dict[str, Any]) -> dict[str, Any]:
    """
    Turn a collect_sp_cost_explorer() result into DataFrames.

    Every sheet is always present. A query that produced no rows gets a
    single Status/Detail row naming why -- never a blank sheet.
    """
    import pandas as pd

    sheets: dict[str, Any] = {}
    for key, sheet in (('monthly', SHEET_CE_MONTHLY), ('per_plan', SHEET_CE_PER_PLAN)):
        q = ce_result[key]
        if q['status'] == utils.CE_STATUS_DATA and q['rows']:
            sheets[sheet] = pd.DataFrame(q['rows'])
        else:
            sheets[sheet] = pd.DataFrame(_status_frame_rows(q['status'], q['detail']))

    periods = ce_result['periods']
    settings = ce_result['settings']
    requests = ce_result['requests']
    status_rows = [
        {'Field': 'Cost Explorer queries', 'Value': 'Enabled' if settings['enabled'] else 'Disabled'},
        {'Field': 'Setting source', 'Value': settings['source']},
        {'Field': 'How to enable / disable', 'Value': utils.cost_explorer_enable_hint()},
        {'Field': 'Partition', 'Value': ce_result['partition']},
        {
            'Field': 'Period',
            'Value': (
                f"{periods[0][0]} to {periods[-1][1]} (end exclusive): the last "
                f"{len(periods)} complete month(s). The current month is not included."
            ),
        },
        {'Field': f'Monthly utilization (ce:{OP_MONTHLY})', 'Value': ce_result['monthly']['status']},
        {'Field': 'Monthly utilization detail', 'Value': ce_result['monthly']['detail']},
        {'Field': f'Per-plan utilization (ce:{OP_PER_PLAN})', 'Value': ce_result['per_plan']['status']},
        {'Field': 'Per-plan utilization detail', 'Value': ce_result['per_plan']['detail']},
        {'Field': 'Cost Explorer API requests issued', 'Value': requests},
        {
            'Field': 'Cost Explorer API charge',
            'Value': (
                f"${utils.CE_REQUEST_COST_USD:.2f} per paginated request "
                f"({utils.CE_REQUEST_COST_SOURCE}); this run issued {requests} request(s) "
                f"= ${requests * utils.CE_REQUEST_COST_USD:.2f}. SDK retries after "
                "throttling are not counted here."
            ),
        },
        {
            'Field': 'Scope',
            'Value': (
                "Utilization is what this account's Cost Explorer reports. A management "
                "account sees its member accounts; a member account may not see plans "
                "held elsewhere in the organization."
            ),
        },
        {
            'Field': 'Amounts',
            'Value': "As returned by Cost Explorer, unconverted. Nothing on these sheets is computed.",
        },
    ]
    sheets[SHEET_CE_STATUS] = pd.DataFrame(status_rows)
    return sheets


def run_sp_cost_explorer(plan_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Resolve partition and settings, then run collect_sp_cost_explorer().

    Partition detection runs first so a GovCloud run reports "unavailable in
    aws-us-gov" rather than "disabled" -- enabling it there would not help.
    """
    settings = utils.cost_explorer_utilization_settings()
    partition = utils.detect_partition()
    inventory = {
        row.get('Savings Plan ARN'): row for row in plan_rows if row.get('Savings Plan ARN')
    }
    if settings['enabled'] and utils.is_service_available_in_partition('ce', partition):
        months = settings['lookback_months']
        utils.log_info(
            f"Querying Cost Explorer for Savings Plans utilization: {months} month(s), "
            f"about {months + 1} paginated request(s) at ${utils.CE_REQUEST_COST_USD:.2f} each."
        )
    return collect_sp_cost_explorer(partition, settings, inventory)


def export_savings_plans_data(account_id: str, account_name: str):
    """
    Export Savings Plans information to an Excel file.

    Savings Plans is a global, account-scope service (not multi-region), so
    failures are tracked per account-scope collector call rather than via
    ``utils.scan_regions_concurrent`` (see scripts/shield_export.py for the
    account-scope reference pattern). Each ``collect_savings_plans`` call
    (PRIMARY scope) is allowed to raise; a real API error is recorded in
    ``failed_scopes`` and the export continues with whatever data was
    already collected — it is never silently collapsed into "no savings
    plans" (see .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).

    Args:
        account_id: The AWS account ID
        account_name: The AWS account name
    """
    print("\nStarting Savings Plans export process...")
    print("This may take some time depending on the number of savings plans...")

    # Import pandas for DataFrame handling
    import pandas as pd

    # Dictionary to hold all DataFrames for export
    data_frames = {}

    # Account-scope failure tracking (see scripts/shield_export.py).
    failed_scopes: list[tuple[str, str]] = []

    # STEP 1: Collect active savings plans (PRIMARY scope — a real API error
    # here must propagate to failed_scopes, never collapse into an empty
    # list that reads as "no active savings plans").
    try:
        active_plans = collect_savings_plans(['active'])
    except Exception as e:
        failed_scopes.append(('savings_plans', str(e)))
        utils.log_error(f"Active savings plans collection failed: {e}")
        active_plans = []
    if active_plans:
        data_frames['Active Savings Plans'] = pd.DataFrame(active_plans)

    # STEP 2: Collect queued (pending) savings plans (PRIMARY scope — same
    # failure handling as STEP 1).
    try:
        queued_plans = collect_savings_plans(['queued'])
    except Exception as e:
        failed_scopes.append(('savings_plans', str(e)))
        utils.log_error(f"Queued savings plans collection failed: {e}")
        queued_plans = []
    if queued_plans:
        data_frames['Queued Savings Plans'] = pd.DataFrame(queued_plans)

    # STEP 3: Create summary
    if active_plans or queued_plans:
        summary_data = []

        # Total active plans
        total_active = len(active_plans)
        total_queued = len(queued_plans)

        # Commitment totals by type
        compute_commitment = sum(float(p.get('Hourly Commitment', 0)) for p in active_plans if p.get('Savings Plan Type') == 'Compute')
        ec2_commitment = sum(float(p.get('Hourly Commitment', 0)) for p in active_plans if p.get('Savings Plan Type') == 'EC2Instance')
        sagemaker_commitment = sum(float(p.get('Hourly Commitment', 0)) for p in active_plans if p.get('Savings Plan Type') == 'SageMaker')

        summary_data.append({
            'Metric': 'Total Active Savings Plans',
            'Value': total_active
        })
        summary_data.append({
            'Metric': 'Total Queued Savings Plans',
            'Value': total_queued
        })
        summary_data.append({
            'Metric': 'Compute Savings Plans Hourly Commitment (USD)',
            'Value': round(compute_commitment, 2)
        })
        summary_data.append({
            'Metric': 'EC2 Instance Savings Plans Hourly Commitment (USD)',
            'Value': round(ec2_commitment, 2)
        })
        summary_data.append({
            'Metric': 'SageMaker Savings Plans Hourly Commitment (USD)',
            'Value': round(sagemaker_commitment, 2)
        })
        summary_data.append({
            'Metric': 'Total Hourly Commitment (USD)',
            'Value': round(compute_commitment + ec2_commitment + sagemaker_commitment, 2)
        })

        data_frames['Summary'] = pd.DataFrame(summary_data)

    # STEP 3b: Cost Explorer utilization (opt-in, paid, not in GovCloud).
    # collect_sp_cost_explorer never raises; each query's outcome is a stated
    # status. A failed lookup the operator opted into is a failed scope.
    ce_result = run_sp_cost_explorer(active_plans + queued_plans)
    failed_scopes.extend(ce_result['failed_scopes'])
    ce_has_data = any(
        ce_result[key]['status'] == utils.CE_STATUS_DATA for key in ('monthly', 'per_plan')
    )
    # CE sheets ride along whenever a workbook is written. With no plans in
    # inventory, a workbook is still written if Cost Explorer returned data
    # (e.g. a plan that retired inside the window).
    if data_frames or ce_has_data:
        data_frames.update(build_sp_cost_explorer_sheets(ce_result))
    for key, label in (('monthly', 'Monthly utilization'), ('per_plan', 'Per-plan utilization')):
        utils.log_info(f"Cost Explorer {label}: {ce_result[key]['status']}")

    # Check if we have any data. A genuinely empty result (no data AND no
    # failed scopes) gets a plain warning; a failed scope is handled below
    # regardless of whether a partial export was written.
    if not data_frames:
        if not failed_scopes:
            utils.log_warning("No Savings Plans data was collected. Nothing to export.")
            print("\nNo Savings Plans found in this account.")
    else:
        # STEP 4: Prepare all DataFrames for export
        for sheet_name in data_frames:
            data_frames[sheet_name] = utils.prepare_dataframe_for_export(data_frames[sheet_name])

        # STEP 5: Create filename and export
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        final_excel_file = utils.create_export_filename(
            account_name,
            'savings-plans',
            '',
            current_date
        )

        # Save using utils module for consistent formatting
        try:
            output_path = utils.save_multiple_dataframes_to_excel(data_frames, final_excel_file)

            if output_path:
                utils.log_success("Savings Plans data exported successfully!")
                utils.log_success(f"File location: {output_path}")

                # Summary of exported data
                for sheet_name, df in data_frames.items():
                    utils.log_info(f"  - {sheet_name}: {len(df)} records")
                    print(f"  - {sheet_name}: {len(df)} records")
            else:
                utils.log_error("Error creating Excel file. Please check the logs.")

        except Exception as e:
            utils.log_error("Error creating Excel file", e)

    # If any primary-scope collection failed, make it loud: write a marker
    # and exit non-zero, even if a partial export (the other scope, or the
    # Summary sheet) was written. A partial export that looks complete is
    # exactly the failure mode this guards against.
    if failed_scopes:
        utils.report_collection_failures(account_name, 'savings-plans', failed_scopes)
        print(
            "\nERROR: Savings Plans export completed with failures — data is "
            "incomplete. See the *-savings-plans-FAILED-*.txt marker in the "
            "output directory."
        )
        sys.exit(1)


def main():
    # Initialize logging
    utils.setup_logging("savings-plans-export")
    SCRIPT_START_TIME = datetime.datetime.now()
    utils.log_script_start("savings-plans-export.py", "AWS Savings Plans Export Tool")

    try:
        # Print title and get account information
        account_id, account_name = utils.print_script_banner("AWS SAVINGS PLANS EXPORT")

        # Check and install dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Check if account name is unknown
        if account_name == "unknown" and not utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False):
            print("Exiting script...")
            sys.exit(0)

        # Export Savings Plans data
        export_savings_plans_data(account_id, account_name)

        print("\nSavings Plans export script execution completed.")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        utils.log_info("Script cancelled by user")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
    finally:
        utils.log_script_end("savings-plans-export.py", SCRIPT_START_TIME)


if __name__ == "__main__":
    main()
