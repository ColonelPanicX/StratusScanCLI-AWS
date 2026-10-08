#!/usr/bin/env python3
"""
Amazon WorkSpaces Export Script for StratusScan

Exports one row per WorkSpace (WorkSpaces Personal) with identity, operating
system, power / connection state, and the associated configuration.

Sources (all verified against the WorkSpaces API Reference):
- workspaces:DescribeWorkspaces            -> the WorkSpace record (paginated)
- workspaces:DescribeWorkspacesConnectionStatus -> connection state and last
  known user connection (paginated, max 25 WorkSpace IDs per call)
- workspaces:DescribeWorkspaceBundles      -> bundle name / owner (paginated,
  max 25 bundle IDs per call)
- workspaces:DescribeWorkspaceDirectories  -> directory name / type (paginated,
  max 25 directory IDs per call)
- workspaces:DescribeTags                  -> tags (one call per WorkSpace)

Created date: the WorkSpaces API exposes NO creation timestamp for a WorkSpace
(the Workspace data type has no such member). The bundle's CreationTime and
LastUpdatedTime describe the BUNDLE, not the WorkSpace, and are deliberately not
exported. The 'Created Date' column therefore states this explicitly rather than
carrying a stand-in value.

Power state: 'State' is the raw API value. 'Power State' is mapped only where
the API documentation defines the state as running or stopped (AVAILABLE,
STARTING, STOPPING, STOPPED); every other state is reported as not determinable
rather than guessed.

Failure contract: DescribeWorkspaces failures raise so the region is recorded
as FAILED (marker file + non-zero exit), never collapsed into "empty".
Enrichment lookups (connection status, bundle, directory, tags) that fail do
not discard the WorkSpace rows; the affected cells read 'Unavailable' and the
failure is listed on the 'Collection Warnings' sheet and in the log.

Output: Excel workbook with Summary, WorkSpaces and Collection Warnings sheets.
"""

import datetime
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import EndpointConnectionError

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export Amazon WorkSpaces to Excel")

utils.setup_logging('workspaces-export')

# Regions where WorkSpaces Personal is offered, per "Availability Zones for
# WorkSpaces Personal" (checked 10.07.2026):
# https://docs.aws.amazon.com/workspaces/latest/adminguide/azs-workspaces.html
# This list is NOT used to skip anything. A region outside it is still tried
# (so a newly launched region is never silently missed); only if that attempt
# fails with EndpointConnectionError is it reported as "Not offered in region".
# Inside the list, EndpointConnectionError is a real failure and stays FAILED.
DOCUMENTED_REGIONS = frozenset({
    'us-east-1', 'us-east-2', 'us-west-2', 'ap-south-1', 'ap-northeast-2',
    'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-5', 'ap-northeast-1',
    'ca-central-1', 'eu-central-1', 'eu-west-1', 'eu-west-2', 'eu-west-3',
    'sa-east-1', 'af-south-1', 'il-central-1', 'us-gov-west-1', 'us-gov-east-1',
})

# DescribeWorkspaces/DescribeWorkspaceBundles/DescribeWorkspaceDirectories/
# DescribeWorkspacesConnectionStatus all cap their ID-list parameter at 25.
ID_BATCH_SIZE = 25

CREATED_DATE_NOT_EXPOSED = "Not available (WorkSpaces API exposes no creation date)"
UNAVAILABLE = "Unavailable"

# State -> power state, only where the API reference defines the state in terms
# of running / stopped. https://docs.aws.amazon.com/workspaces/latest/api/API_Workspace.html
POWER_STATE_MAP = {
    'AVAILABLE': 'Powered On',
    'STARTING': 'Starting',
    'STOPPING': 'Stopping',
    'STOPPED': 'Powered Off',
}

STATE_FILTERS = {
    1: (None, 'all'),
    2: ('AVAILABLE', 'available'),
    3: ('STOPPED', 'stopped'),
}


def _chunks(items: list[str], size: int):
    """Yield successive ``size``-length slices of ``items``."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def format_tags(tags: list[dict[str, str]] | None) -> str:
    """Format a tag list as ``Key:Value, Key:Value`` (same style as the EC2 export)."""
    if not tags:
        return 'N/A'
    formatted = [f"{t['Key']}:{t['Value']}" for t in tags if 'Key' in t and 'Value' in t]
    return ', '.join(formatted) if formatted else 'N/A'


def map_power_state(state: str | None) -> str:
    """Map a WorkSpace State to a power state without inventing values."""
    if not state:
        return 'N/A'
    mapped = POWER_STATE_MAP.get(state)
    if mapped:
        return mapped
    return f"Not determinable from State ({state})"


def _fmt_ts(value: Any) -> str:
    """
    Format a boto3 timestamp as ``YYYY-MM-DD HH:MM:SS UTC``; 'N/A' when absent.

    boto3 returns tz-aware datetimes. They must not reach the DataFrame raw:
    utils.prepare_dataframe_for_export only samples the first non-null value of
    an object column, so a leading 'N/A' hides later tz-aware values from its
    tz-stripping and Excel rejects the save (Issue #310). Strings are immune.
    """
    if not value:
        return 'N/A'
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc)
        return value.strftime('%Y-%m-%d %H:%M:%S UTC')
    return str(value)


# ---------------------------------------------------------------------------
# Enrichment lookups. Each returns (data, error_message_or_None) so the caller
# can state the failure instead of swallowing it.
# ---------------------------------------------------------------------------

def _fetch_connection_status(client, workspace_ids: list[str]) -> tuple[dict[str, dict], str | None]:
    result: dict[str, dict] = {}
    try:
        paginator = client.get_paginator('describe_workspaces_connection_status')
        for chunk in _chunks(workspace_ids, ID_BATCH_SIZE):
            for page in paginator.paginate(WorkspaceIds=chunk):
                for item in page.get('WorkspacesConnectionStatus', []):
                    if item.get('WorkspaceId'):
                        result[item['WorkspaceId']] = item
    except Exception as e:
        return result, f"DescribeWorkspacesConnectionStatus failed: {e}"
    return result, None


def _fetch_bundles(client, bundle_ids: list[str]) -> tuple[dict[str, dict], str | None]:
    result: dict[str, dict] = {}
    try:
        paginator = client.get_paginator('describe_workspace_bundles')
        for chunk in _chunks(bundle_ids, ID_BATCH_SIZE):
            for page in paginator.paginate(BundleIds=chunk):
                for item in page.get('Bundles', []):
                    if item.get('BundleId'):
                        result[item['BundleId']] = item
    except Exception as e:
        return result, f"DescribeWorkspaceBundles failed: {e}"
    return result, None


def _fetch_directories(client, directory_ids: list[str]) -> tuple[dict[str, dict], str | None]:
    result: dict[str, dict] = {}
    try:
        paginator = client.get_paginator('describe_workspace_directories')
        for chunk in _chunks(directory_ids, ID_BATCH_SIZE):
            for page in paginator.paginate(DirectoryIds=chunk):
                for item in page.get('Directories', []):
                    if item.get('DirectoryId'):
                        result[item['DirectoryId']] = item
    except Exception as e:
        return result, f"DescribeWorkspaceDirectories failed: {e}"
    return result, None


def _fetch_tags(client, workspace_id: str) -> tuple[list[dict[str, str]] | None, str | None]:
    # DescribeTags is not paginated (no NextToken in its request or response).
    try:
        resp = client.describe_tags(ResourceId=workspace_id)
        return resp.get('TagList', []), None
    except Exception as e:
        return None, f"DescribeTags failed for {workspace_id}: {e}"


def _build_workspace_row(
    ws: dict[str, Any],
    region: str,
    conn: dict | None,
    conn_ok: bool,
    bundle: dict | None,
    bundles_ok: bool,
    directory: dict | None,
    dirs_ok: bool,
    tags: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """Build the export row for one WorkSpace from its describe responses."""
    props = ws.get('WorkspaceProperties') or {}
    state = ws.get('State')

    if conn_ok:
        # A WorkSpace absent from the status response is stated, not defaulted.
        conn_state = (conn or {}).get('ConnectionState', 'Not returned by API')
        conn_checked = _fmt_ts((conn or {}).get('ConnectionStateCheckTimestamp'))
        last_conn = _fmt_ts((conn or {}).get('LastKnownUserConnectionTimestamp'))
    else:
        conn_state = conn_checked = last_conn = UNAVAILABLE

    if not ws.get('BundleId'):
        bundle_name = bundle_owner = 'N/A'
    elif bundles_ok:
        bundle_name = (bundle or {}).get('Name', 'Not returned by API')
        bundle_owner = (bundle.get('Owner') or 'N/A') if bundle else 'Not returned by API'
    else:
        bundle_name = bundle_owner = UNAVAILABLE

    if not ws.get('DirectoryId'):
        dir_name = dir_type = 'N/A'
    elif dirs_ok:
        dir_name = (directory or {}).get('DirectoryName', 'Not returned by API')
        dir_type = (directory.get('DirectoryType') or 'N/A') if directory else 'Not returned by API'
    else:
        dir_name = dir_type = UNAVAILABLE

    related = ws.get('RelatedWorkspaces') or []
    related_str = ', '.join(
        f"{r.get('Type', '?')}:{r.get('WorkspaceId', '?')}({r.get('Region', '?')},{r.get('State', '?')})"
        for r in related
    ) or 'N/A'

    mods = ws.get('ModificationStates') or []
    mods_str = ', '.join(f"{m.get('Resource', '?')}:{m.get('State', '?')}" for m in mods) or 'N/A'

    return {
        'Computer Name': ws.get('ComputerName', 'N/A'),
        'WorkSpace Name': ws.get('WorkspaceName', 'N/A'),
        'WorkSpace ID': ws.get('WorkspaceId', 'N/A'),
        'User Name': ws.get('UserName', 'N/A'),
        'State': state or 'N/A',
        'Power State': map_power_state(state),
        'Running Mode': props.get('RunningMode', 'N/A'),
        'Auto-Stop Timeout (min)': props.get('RunningModeAutoStopTimeoutInMinutes', 'N/A'),
        'Connection State': conn_state,
        'Last Known User Connection': last_conn,
        'Connection State Checked': conn_checked,
        'Created Date': CREATED_DATE_NOT_EXPOSED,
        'Operating System': props.get('OperatingSystemName', 'N/A'),
        'Compute Type': props.get('ComputeTypeName', 'N/A'),
        'Root Volume (GiB)': props.get('RootVolumeSizeGib', 'N/A'),
        'User Volume (GiB)': props.get('UserVolumeSizeGib', 'N/A'),
        'Protocols': ', '.join(props.get('Protocols') or []) or 'N/A',
        'Root Volume Encrypted': ws.get('RootVolumeEncryptionEnabled', 'N/A'),
        'User Volume Encrypted': ws.get('UserVolumeEncryptionEnabled', 'N/A'),
        'KMS Key': ws.get('VolumeEncryptionKey', 'N/A'),
        'Bundle ID': ws.get('BundleId', 'N/A'),
        'Bundle Name': bundle_name,
        'Bundle Owner': bundle_owner,
        'Directory ID': ws.get('DirectoryId', 'N/A'),
        'Directory Name': dir_name,
        'Directory Type': dir_type,
        'IP Address': ws.get('IpAddress', 'N/A'),
        'Subnet ID': ws.get('SubnetId', 'N/A'),
        'Modification States': mods_str,
        'Related WorkSpaces': related_str,
        'Error Code': ws.get('ErrorCode', 'N/A'),
        'Error Message': ws.get('ErrorMessage', 'N/A'),
        'Region': region,
        'Tags': format_tags(tags) if tags is not None else UNAVAILABLE,
    }


def _scan_region(region: str) -> dict[str, list]:
    """
    Collect all WorkSpaces in one region.

    DescribeWorkspaces is the primary scope and deliberately NOT guarded: any
    error propagates so ``scan_regions_concurrent(collect_failures=True)``
    records the region as failed rather than empty. Enrichment failures are
    returned as warnings and reflected in the affected cells.

    Returns:
        ``{'rows': [...], 'warnings': [{'Region', 'Detail'}, ...]}``
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return {'rows': [], 'warnings': []}

    partition = utils.detect_partition(region)
    if not utils.is_service_available_in_partition('workspaces', partition):
        utils.log_warning(f"WorkSpaces is not available in partition {partition}; skipping {region}")
        return {'rows': [], 'warnings': []}

    client = utils.get_boto3_client('workspaces', region_name=region)

    workspaces: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator('describe_workspaces')
        for page in paginator.paginate():
            workspaces.extend(page.get('Workspaces', []))
    except EndpointConnectionError as e:
        if region in DOCUMENTED_REGIONS:
            raise  # offered here, so an unreachable endpoint is a real failure
        msg = (f"Not offered in region {region}: no reachable WorkSpaces endpoint "
               f"(region is not in the documented WorkSpaces region list): {e}")
        utils.log_warning(msg)
        return {'rows': [], 'warnings': [{'Region': region, 'Detail': msg}]}

    if not workspaces:
        return {'rows': [], 'warnings': []}

    utils.log_info(f"Found {len(workspaces)} WorkSpace(s) in {region}")
    warnings: list[dict[str, str]] = []

    def note(msg: str) -> None:
        utils.log_warning(f"[{region}] {msg}")
        warnings.append({'Region': region, 'Detail': msg})

    ws_ids = [w['WorkspaceId'] for w in workspaces if w.get('WorkspaceId')]
    bundle_ids = sorted({w['BundleId'] for w in workspaces if w.get('BundleId')})
    dir_ids = sorted({w['DirectoryId'] for w in workspaces if w.get('DirectoryId')})

    conn_map, err = _fetch_connection_status(client, ws_ids)
    conn_ok = err is None
    if err:
        note(err)
    bundle_map, err = _fetch_bundles(client, bundle_ids)
    bundles_ok = err is None
    if err:
        note(err)
    dir_map, err = _fetch_directories(client, dir_ids)
    dirs_ok = err is None
    if err:
        note(err)

    rows = []
    for ws in workspaces:
        ws_id = ws.get('WorkspaceId', '<unknown>')
        tags, err = _fetch_tags(client, ws_id) if ws.get('WorkspaceId') else (None, None)
        if err:
            note(err)
        try:
            rows.append(_build_workspace_row(
                ws, region,
                conn_map.get(ws_id), conn_ok,
                bundle_map.get(ws.get('BundleId')), bundles_ok,
                dir_map.get(ws.get('DirectoryId')), dirs_ok,
                tags,
            ))
        except Exception as e:
            # One malformed record must not sink the region; say so loudly.
            utils.log_error(f"Skipping malformed WorkSpace {ws_id} in {region}", e)
            warnings.append({'Region': region, 'Detail': f"Skipped malformed WorkSpace {ws_id}: {e}"})
    return {'rows': rows, 'warnings': warnings}


def _count_rows(df, column: str, category: str) -> list[dict[str, Any]]:
    """Summary rows: one per distinct value of ``column``."""
    if df.empty or column not in df.columns:
        return []
    counts = df[column].astype(str).value_counts().sort_index()
    return [{'Category': category, 'Metric': str(k), 'Value': int(v)} for k, v in counts.items()]


def build_summary(df, regions: list[str], failed_regions: list, warnings: list) -> list[dict[str, Any]]:
    """Summary sheet rows: totals plus counts by state, power state, OS, mode, compute type."""
    summary = [
        {'Category': 'Totals', 'Metric': 'Total WorkSpaces', 'Value': len(df)},
        {'Category': 'Totals', 'Metric': 'Regions Scanned', 'Value': len(regions)},
        {'Category': 'Totals', 'Metric': 'Regions Failed', 'Value': len(failed_regions)},
        {'Category': 'Totals', 'Metric': 'Collection Warnings', 'Value': len(warnings)},
        {'Category': 'Notes', 'Metric': 'Created Date', 'Value': CREATED_DATE_NOT_EXPOSED},
    ]
    summary += _count_rows(df, 'State', 'By State')
    summary += _count_rows(df, 'Power State', 'By Power State')
    summary += _count_rows(df, 'Operating System', 'By Operating System')
    summary += _count_rows(df, 'Running Mode', 'By Running Mode')
    summary += _count_rows(df, 'Compute Type', 'By Compute Type')
    summary += _count_rows(df, 'Connection State', 'By Connection State')
    summary += _count_rows(df, 'Region', 'By Region')
    return summary


def _prompt_state_filter() -> tuple[str | None, str]:
    """Prompt for a State filter. Raises BackSignal / ExitToMainSignal / QuitSignal."""
    choice = utils.prompt_menu(
        "WORKSPACE FILTER",
        [
            "All WorkSpaces",
            "Available (powered on) only",
            "Stopped (powered off) only",
        ],
    )
    return STATE_FILTERS[choice]


def _run_export(account_id: str, account_name: str, regions: list[str],
                state_filter: str | None, filter_desc: str) -> None:
    """Collect WorkSpaces across regions and write the Excel export."""
    import pandas as pd

    utils.log_info(f"Scanning {len(regions)} region(s) for WorkSpaces...")
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_region,
        show_progress=True,
        collect_failures=True,
    )

    all_rows: list[dict[str, Any]] = []
    all_warnings: list[dict[str, str]] = []
    for result in region_results:
        all_rows.extend(result['rows'])
        all_warnings.extend(result['warnings'])

    if state_filter:
        all_rows = [r for r in all_rows if r.get('State') == state_filter]

    if not all_rows and not failed_regions:
        # Every region succeeded and returned nothing: genuinely empty.
        utils.log_warning("No WorkSpaces found in any selected region. Exiting...")
        sys.exit(0)

    if all_rows:
        utils.log_success(f"Total WorkSpaces found: {len(all_rows)}")
        df = utils.sanitize_for_export(utils.prepare_dataframe_for_export(pd.DataFrame(all_rows)))
        df_summary = utils.prepare_dataframe_for_export(
            pd.DataFrame(build_summary(df, regions, failed_regions, all_warnings))
        )
        df_warnings = utils.prepare_dataframe_for_export(
            pd.DataFrame(all_warnings, columns=['Region', 'Detail'])
        )

        region_desc = regions[0] if len(regions) == 1 else 'all'
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        filename = utils.create_export_filename(
            account_name, 'workspaces', f"{filter_desc}-{region_desc}", current_date
        )
        output_path = utils.save_multiple_dataframes_to_excel(
            {'Summary': df_summary, 'WorkSpaces': df, 'Collection Warnings': df_warnings},
            filename,
        )
        if not output_path:
            utils.log_error("Error exporting data. Please check the logs.")
            sys.exit(1)
        utils.log_success("Amazon WorkSpaces data exported successfully!")
        utils.log_success(f"File location: {output_path}")
        if all_warnings:
            utils.log_warning(
                f"{len(all_warnings)} enrichment warning(s); see the 'Collection Warnings' sheet."
            )

    # Loud on any failed region, even if some data exported: a partial export
    # that looks complete is the failure mode this guards against.
    if failed_regions:
        utils.report_collection_failures(account_name, 'workspaces', failed_regions)
        print(
            "\nERROR: WorkSpaces export completed with failures - data is incomplete. "
            "See the *-workspaces-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    """Main execution function - 4-step state machine with b/x navigation."""
    try:
        if not utils.ensure_dependencies('pandas', 'openpyxl', 'boto3'):
            return
        account_id, account_name = utils.print_script_banner("AMAZON WORKSPACES DATA EXPORT")

        if account_name == "UNKNOWN-ACCOUNT" and not utils.prompt_for_confirmation(
            "Unable to determine account name. Proceed anyway?", default=False
        ):
            print("Exiting script...")
            sys.exit(0)

        step = 1
        regions: list[str] = []
        state_filter: str | None = None
        filter_desc = 'all'

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="WorkSpaces")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                try:
                    state_filter, filter_desc = _prompt_state_filter()
                except utils.BackSignal:
                    step = 1
                    continue
                except (utils.ExitToMainSignal, utils.QuitSignal):
                    sys.exit(11)
                step = 3

            elif step == 3:
                region_str = ', '.join(regions) if len(regions) <= 3 else f"{len(regions)} regions"
                result = utils.prompt_confirmation(
                    f"Ready to export WorkSpaces data ({filter_desc}, {region_str})."
                )
                if result == 'back':
                    step = 2
                    continue
                if result == 'exit':
                    sys.exit(11)
                step = 4

            elif step == 4:
                _run_export(account_id, account_name, regions, state_filter, filter_desc)
                break

    except KeyboardInterrupt:
        print("\n\nScript interrupted by user. Exiting...")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        utils.log_error("Unexpected error occurred", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
