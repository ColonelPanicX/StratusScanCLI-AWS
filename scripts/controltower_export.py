#!/usr/bin/env python3
"""
AWS Control Tower Export Script for StratusScan

Exports comprehensive AWS Control Tower configuration including:
- Organizational units under Control Tower management
- Enabled controls with detailed metadata (service, name, description, behavior)
- Control drift detection and compliance status
- Failed and drifted controls in separate tabs

Note: Control Tower is a global service accessed via the management account.
      Requires controlcatalog:GetControl permission for full control metadata.

Output: Multi-worksheet Excel file with:
  - Organizational Units: Complete OU hierarchy
  - Enabled Controls: All enabled controls with metadata and status
  - Drifted Controls: Controls with detected drift
  - Failed Controls: Controls that failed to enable
"""

import json
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError, UnknownServiceError

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export AWS Control Tower landing zone configuration to Excel")

def collect_landing_zone() -> dict[str, Any]:
    """
    Collect AWS Control Tower landing zone information (global service).

    This is a PRIMARY, account-scope collector (see scripts/shield_export.py
    / scripts/organizations_export.py for the account-scope reference
    pattern). It does not swallow errors to an empty dict: a swallowed error
    here would be indistinguishable from Control Tower simply not being set
    up in this account, producing silent data loss (see the 07.15.2026 /
    07.16.2026 silent-collection-failure audits). "No landing zone" /
    AccessDenied on ListLandingZones is treated as the legitimate "Control
    Tower not set up" state and returns ``{}`` gracefully. Any other
    ClientError/exception is allowed to propagate so the caller (main) can
    record this scope as *failed* rather than *empty*.

    Returns:
        dict: Landing zone information, or ``{}`` if Control Tower is not
            set up in this account (a legitimate, non-failure state).

    Raises:
        Exception: Any real AWS/pagination error for the account scope
            (caller records it as a failed scope; it is never masked as
            empty).
    """
    print("\n=== COLLECTING LANDING ZONE INFORMATION ===")

    # Control Tower is a global service - use partition-aware home region
    home_region = utils.get_partition_default_region()
    ct_client = utils.get_boto3_client('controltower', region_name=home_region)

    try:
        landing_zones = ct_client.list_landing_zones()
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ('AccessDeniedException', 'ResourceNotFoundException'):
            utils.log_info(
                f"Control Tower landing zone not accessible ({error_code}); "
                "treating as 'not set up' in this account."
            )
            return {}
        raise

    lz_list = landing_zones.get('landingZones', [])

    if not lz_list:
        utils.log_warning("No landing zone found. Control Tower may not be set up.")
        return {}

    # Get details for the first (and typically only) landing zone
    lz_arn = lz_list[0].get('arn', '')

    if not lz_arn:
        utils.log_warning("Landing zone ARN not found")
        return {}

    # Get detailed landing zone information. A failure past this point means
    # a landing zone genuinely exists but we could not read it -- a real
    # failure, not a "not set up" state.
    lz_response = ct_client.get_landing_zone(landingZoneIdentifier=lz_arn)
    lz_details = lz_response.get('landingZone', {})

    # Parse manifest if available
    manifest = lz_details.get('manifest', {})
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            manifest = {'raw': manifest}

    # Get governed regions
    governed_regions = manifest.get('governedRegions', []) if isinstance(manifest, dict) else []

    # Drift status
    drift_status_summary = lz_details.get('driftStatus', {})
    drift_status = drift_status_summary.get('status', 'N/A')

    landing_zone_info = {
        'ARN': lz_details.get('arn', 'N/A'),
        'Version': lz_details.get('version', 'N/A'),
        'Latest Available Version': lz_details.get('latestAvailableVersion', 'N/A'),
        'Status': lz_details.get('status', 'N/A'),
        'Drift Status': drift_status,
        'Governed Regions': ', '.join(governed_regions) if governed_regions else 'N/A',
        'Number of Governed Regions': len(governed_regions) if governed_regions else 0,
        'Manifest': json.dumps(manifest, indent=2) if isinstance(manifest, dict) else str(manifest)
    }

    utils.log_success(f"Landing zone found: Version {landing_zone_info['Version']}, Status: {landing_zone_info['Status']}")
    return landing_zone_info


def _build_ou_row(ou: dict[str, Any]) -> dict[str, Any]:
    """
    Build the export row for a single organizational unit.

    Extracted so per-OU processing can be wrapped in try/except by the
    caller: a malformed OU entry must not sink the whole recursive branch.
    Required fields are read with ``.get()`` and a safe default for the
    same reason.

    Args:
        ou: A single OrganizationalUnits entry from
            list_organizational_units_for_parent.

    Returns:
        dict: The assembled OU row.
    """
    return {
        'OU ID': ou.get('Id', 'N/A'),
        'OU ARN': ou.get('Arn', 'N/A'),
        'OU Name': ou.get('Name', 'N/A'),
        'Type': 'Organizational Unit'
    }


def collect_organizational_units() -> list[dict[str, Any]]:
    """
    Collect organizational units from AWS Organizations.

    This is a PRIMARY, account-scope collector (see scripts/shield_export.py
    / scripts/organizations_export.py for the account-scope reference
    pattern). It does not swallow errors to an empty list: a swallowed error
    here would be indistinguishable from an organization with no child OUs,
    producing silent data loss (see the 07.15.2026 / 07.16.2026 silent-
    collection-failure audits). AWS Organizations not being in use, or this
    not being the management account, is a legitimate "not set up" state and
    returns ``[]`` gracefully. Any other error (client creation, pagination)
    is allowed to propagate so the caller (main) can record this scope as
    *failed* rather than *empty*. A single malformed OU entry is logged and
    skipped rather than aborting the whole branch.

    Returns:
        list: List of OU information dictionaries.

    Raises:
        Exception: Any real AWS/pagination error for the account scope
            (caller records it as a failed scope; it is never masked as
            empty).
    """
    print("\n=== COLLECTING ORGANIZATIONAL UNITS ===")
    all_ous = []

    # Organizations is a global service - partition-aware
    org_client = utils.get_boto3_client('organizations')

    try:
        roots = org_client.list_roots().get('Roots', [])
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ('AWSOrganizationsNotInUseException', 'AccessDeniedException'):
            utils.log_info(
                f"AWS Organizations not accessible ({error_code}); treating "
                "as 'not set up' for Control Tower OU collection."
            )
            return []
        raise

    if not roots:
        utils.log_warning("No organization root found")
        return []

    root = roots[0]
    root_id = root.get('Id', 'N/A')
    root_arn = root.get('Arn', 'N/A')

    # Add root to the list
    all_ous.append({
        'OU ID': root_id,
        'OU ARN': root_arn,
        'OU Name': root.get('Name', 'N/A'),
        'Type': 'Root'
    })

    # List OUs recursively. Pagination errors propagate to the caller (the
    # recursion is part of this PRIMARY scope); only a single malformed OU
    # entry is contained.
    def list_ous_recursive(parent_id):
        paginator = org_client.get_paginator('list_organizational_units_for_parent')
        for page in paginator.paginate(ParentId=parent_id):
            for ou in page.get('OrganizationalUnits', []):
                try:
                    ou_row = _build_ou_row(ou)
                except Exception as e:
                    ou_id = ou.get('Id', 'Unknown') if isinstance(ou, dict) else 'Unknown'
                    utils.log_error(f"Skipping malformed OU '{ou_id}' due to a processing error", e)
                    continue

                all_ous.append(ou_row)
                # Recursively list child OUs
                list_ous_recursive(ou_row['OU ID'])

    list_ous_recursive(root_id)

    utils.log_success(f"Total organizational units collected: {len(all_ous)}")
    return all_ous


def extract_service_from_control_identifier(control_id: str) -> str:
    """
    Extract service name from control identifier.

    Examples:
        - arn:aws:controltower:us-east-1::control/AWS-GR_CLOUDTRAIL_ENABLED -> CloudTrail
        - arn:aws:controlcatalog:::control/abc123 with alias CT.S3.PR.1 -> S3
        - Control identifier like AWS-GR_EC2_INSTANCE_NO_PUBLIC_IP -> EC2
    """
    # Try extracting from control identifier patterns
    if 'CLOUDTRAIL' in control_id.upper():
        return 'CloudTrail'
    elif 'EC2' in control_id.upper():
        return 'EC2'
    elif 'S3' in control_id.upper():
        return 'S3'
    elif 'IAM' in control_id.upper():
        return 'IAM'
    elif 'LAMBDA' in control_id.upper():
        return 'Lambda'
    elif 'RDS' in control_id.upper():
        return 'RDS'
    elif 'VPC' in control_id.upper():
        return 'VPC'
    elif 'KMS' in control_id.upper():
        return 'KMS'
    elif 'CLOUDWATCH' in control_id.upper():
        return 'CloudWatch'
    elif 'CONFIG' in control_id.upper():
        return 'Config'
    elif 'SNS' in control_id.upper():
        return 'SNS'
    elif 'SQS' in control_id.upper():
        return 'SQS'
    elif 'BACKUP' in control_id.upper():
        return 'Backup'
    elif 'DYNAMODB' in control_id.upper():
        return 'DynamoDB'
    elif 'EBS' in control_id.upper():
        return 'EBS'
    elif 'ELB' in control_id.upper() or 'ELASTICLOADBALANCING' in control_id.upper():
        return 'ELB'
    elif 'REDSHIFT' in control_id.upper():
        return 'Redshift'
    elif 'SAGEMAKER' in control_id.upper():
        return 'SageMaker'
    elif 'SECRETSMANAGER' in control_id.upper():
        return 'Secrets Manager'
    elif 'ECS' in control_id.upper():
        return 'ECS'
    elif 'EKS' in control_id.upper():
        return 'EKS'
    elif 'APIGATEWAY' in control_id.upper() or 'API_GW' in control_id.upper():
        return 'API Gateway'
    elif 'CODEBUILD' in control_id.upper():
        return 'CodeBuild'
    elif 'CODEPIPELINE' in control_id.upper():
        return 'CodePipeline'
    elif 'OPENSEARCH' in control_id.upper() or 'ELASTICSEARCH' in control_id.upper():
        return 'OpenSearch'
    elif 'GUARDDUTY' in control_id.upper():
        return 'GuardDuty'
    elif 'SECURITYHUB' in control_id.upper():
        return 'Security Hub'
    else:
        return 'Other'


# Stated value for catalog-sourced columns when the installed SDK predates the
# Control Catalog API (Issue #213) -- never a blank that reads as "no data".
CATALOG_UNAVAILABLE = f'Unavailable (boto3 below {utils.BOTO3_MIN_VERSION})'


def _make_catalog_client(region: str) -> Any:
    """
    Control Catalog client, or None when the installed SDK cannot call GetControl.

    Measured against botocore releases (Issue #213): the ``controlcatalog``
    service first ships in 1.34.80 and ``GetControl`` in 1.34.152, both below
    the declared floor. The guard only matters when someone runs below the
    floor after declining the upgrade: the export degrades to identifier-only
    metadata instead of losing every control.
    """
    try:
        client = utils.get_boto3_client('controlcatalog', region_name=region)
    except UnknownServiceError:
        client = None
    if client is None or not hasattr(client, 'get_control'):
        utils.log_warning(
            "Control Catalog API not in this boto3/botocore; control name, "
            f"description and behavior will read '{CATALOG_UNAVAILABLE}'. "
            f"Upgrade with: {utils.sdk_upgrade_command_str()}"
        )
        return None
    return client


def _build_control_row(
    control: dict[str, Any],
    ou_name: str,
    ou_arn: str,
    ct_client,
    catalog_client,
) -> dict[str, Any]:
    """
    Build the export row for a single enabled control.

    Extracted so per-control processing can be wrapped in try/except by the
    caller: a malformed control entry must not sink the whole OU's control
    listing. Required fields are read with ``.get()`` and a safe default for
    the same reason. Enrichment calls (GetEnabledControl parameters, control
    catalog metadata) are contained internally and degrade to 'N/A'/'None'
    on failure -- they are not treated as scope failures.

    Args:
        control: A single enabledControls entry from list_enabled_controls.
        ou_name: Name of the OU this control belongs to.
        ou_arn: ARN of the OU this control belongs to.
        ct_client: The boto3 Control Tower client.
        catalog_client: The boto3 Control Catalog client, or None when the
            installed SDK predates it (see _make_catalog_client).

    Returns:
        dict: The assembled control row.
    """
    control_id = control.get('controlIdentifier', 'N/A')
    control_arn = control.get('arn', 'N/A')

    # Status summary
    status_summary = control.get('statusSummary', {})
    status = status_summary.get('status', 'N/A')
    last_operation = status_summary.get('lastOperationIdentifier', 'N/A')

    # Drift status
    drift_summary = control.get('driftStatusSummary', {})
    drift_status = drift_summary.get('driftStatus', 'N/A')

    # Drift types
    drift_types = drift_summary.get('types', {})
    inheritance_drift = drift_types.get('inheritance', {}).get('status', 'N/A')
    resource_drift = drift_types.get('resource', {}).get('status', 'N/A')

    # Initialize control metadata
    control_name = control_id  # Default to identifier
    control_description = 'N/A'
    control_behavior = 'N/A'
    control_guidance = 'N/A'
    service_name = 'N/A'
    params_str = 'None'

    try:
        # Get control details from GetEnabledControl for parameters
        enabled_control_details = ct_client.get_enabled_control(
            enabledControlIdentifier=control_arn
        )

        enabled_control = enabled_control_details.get('enabledControlDetails', {})

        # Get parameters if available
        parameters = enabled_control.get('parameters', [])
        if parameters:
            params_list = []
            for param in parameters:
                key = param.get('key', '')
                value = param.get('value', '')
                if key and value:
                    params_list.append(f"{key}: {value}")
            params_str = ', '.join(params_list) if params_list else 'None'

    except Exception as e:
        utils.log_warning(f"Could not get enabled control details for {control_id}: {str(e)}")

    # Try to get control metadata from control catalog
    if catalog_client is None:
        control_description = CATALOG_UNAVAILABLE
        control_behavior = CATALOG_UNAVAILABLE
        service_name = extract_service_from_control_identifier(control_id)
    else:
        try:
            # Use controlcatalog client to get full metadata
            catalog_response = catalog_client.get_control(ControlArn=control_id)

            # Extract metadata from control catalog response
            control_name = catalog_response.get('Name', control_id)
            control_description = catalog_response.get('Description', 'N/A')
            control_behavior = catalog_response.get('Behavior', 'N/A')

            # Control catalog doesn't have "Guidance" field - this is Control Tower specific
            # We'll need to infer it or mark as N/A
            control_guidance = 'N/A'

            # Extract service from aliases if available
            aliases = catalog_response.get('Aliases', [])
            if aliases:
                # Aliases often have format like "CT.S3.PR.1" or "SH.S3.1"
                for alias in aliases:
                    if '.' in alias:
                        parts = alias.split('.')
                        if len(parts) >= 2:
                            service_name = parts[1]  # e.g., "S3" from "CT.S3.PR.1"
                            break

            # If service not found from alias, try extracting from control identifier
            if service_name == 'N/A':
                service_name = extract_service_from_control_identifier(control_id)

        except Exception as e:
            # Fallback: try extracting service from identifier even if catalog call fails
            service_name = extract_service_from_control_identifier(control_id)
            utils.log_warning(f"Could not get catalog details for control {control_id}: {str(e)}")

    return {
        'OU Name': ou_name,
        'OU ARN': ou_arn,
        'Control Identifier': control_id,
        'Service': service_name,
        'Control Name': control_name,
        'Control ARN': control_arn,
        'Status': status,
        'Drift Status': drift_status,
        'Inheritance Drift': inheritance_drift,
        'Resource Drift': resource_drift,
        'Behavior': control_behavior,
        'Guidance': control_guidance,
        'Description': control_description,
        'Parameters': params_str,
        'Last Operation ID': last_operation
    }


def collect_enabled_controls(ous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collect enabled controls for all organizational units.

    This is a PRIMARY, account-scope collector (see scripts/shield_export.py
    / scripts/organizations_export.py for the account-scope reference
    pattern). It does not swallow errors to an empty list: a swallowed
    client-creation or pagination-setup error here would be indistinguishable
    from an account with no enabled controls, producing silent data loss
    (see the 07.15.2026 / 07.16.2026 silent-collection-failure audits).
    Per-OU listing failures are logged and skipped (one OU's throttling/API
    error does not sink the whole account's control inventory); a single
    malformed control entry is likewise logged and skipped.

    Args:
        ous: List of OU information dictionaries from
            collect_organizational_units().

    Returns:
        list: List of enabled control dictionaries.

    Raises:
        Exception: Any real AWS error setting up the account-scope clients
            (caller records it as a failed scope; it is never masked as
            empty).
    """
    print("\n=== COLLECTING ENABLED CONTROLS ===")
    all_controls = []

    # Control Tower is a global service - use partition-aware home region
    home_region = utils.get_partition_default_region()
    ct_client = utils.get_boto3_client('controltower', region_name=home_region)

    # Control metadata comes from Control Catalog; None when the SDK predates it.
    catalog_client = _make_catalog_client(home_region)

    total_ous = len(ous)
    for idx, ou in enumerate(ous, 1):
        ou_arn = ou.get('OU ARN', '')
        ou_name = ou.get('OU Name', '')
        ou_type = ou.get('Type', '')

        if not ou_arn:
            continue

        # Skip Root OU - Control Tower doesn't apply controls to Root
        if ou_type == 'Root':
            utils.log_info(f"[{idx}/{total_ous}] Skipping OU: {ou_name} (controls cannot be applied to Root)")
            continue

        utils.log_info(f"[{idx}/{total_ous}] Checking controls for OU: {ou_name}")

        try:
            # List enabled controls for this OU
            paginator = ct_client.get_paginator('list_enabled_controls')

            for page in paginator.paginate(targetIdentifier=ou_arn):
                enabled_controls = page.get('enabledControls', [])

                for control in enabled_controls:
                    try:
                        control_row = _build_control_row(control, ou_name, ou_arn, ct_client, catalog_client)
                    except Exception as e:
                        control_id = control.get('controlIdentifier', 'Unknown') if isinstance(control, dict) else 'Unknown'
                        utils.log_error(f"Skipping control '{control_id}' for OU {ou_name} due to a processing error", e)
                        continue

                    all_controls.append(control_row)

        except Exception as e:
            utils.log_warning(f"Error listing controls for OU {ou_name}: {str(e)}")
            continue

    utils.log_success(f"Total enabled controls collected: {len(all_controls)}")
    return all_controls


def generate_summary(landing_zone: dict[str, Any],
                     ous: list[dict[str, Any]],
                     controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for Control Tower resources."""
    utils.log_info("Generating summary statistics...")

    summary = []

    # Landing zone summary
    if landing_zone:
        lz_status = landing_zone.get('Status', 'N/A')
        lz_version = landing_zone.get('Version', 'N/A')
        drift_status = landing_zone.get('Drift Status', 'N/A')

        summary.append({
            'Metric': 'Landing Zone Status',
            'Value': lz_status,
            'Details': f'Version: {lz_version}, Drift: {drift_status}'
        })

        governed_regions = landing_zone.get('Number of Governed Regions', 0)
        summary.append({
            'Metric': 'Governed Regions',
            'Value': governed_regions,
            'Details': landing_zone.get('Governed Regions', 'N/A')
        })

    # OUs summary
    summary.append({
        'Metric': 'Total Organizational Units',
        'Value': len(ous),
        'Details': 'Including Root and all nested OUs'
    })

    # Controls summary
    total_controls = len(controls)
    summary.append({
        'Metric': 'Total Enabled Controls',
        'Value': total_controls,
        'Details': 'Across all organizational units'
    })

    if controls:
        df = pd.DataFrame(controls)

        # Status breakdown
        if 'Status' in df.columns:
            succeeded = len(df[df['Status'] == 'SUCCEEDED'])
            failed = len(df[df['Status'] == 'FAILED'])
            summary.append({
                'Metric': 'Control Status',
                'Value': f'Success: {succeeded}, Failed: {failed}',
                'Details': f'{succeeded} controls successfully enabled'
            })

        # Drift status breakdown
        if 'Drift Status' in df.columns:
            drifted = len(df[df['Drift Status'] == 'DRIFTED'])
            in_sync = len(df[df['Drift Status'] == 'IN_SYNC'])
            summary.append({
                'Metric': 'Control Drift',
                'Value': f'Drifted: {drifted}, In Sync: {in_sync}',
                'Details': 'Drift indicates configuration changes outside Control Tower'
            })

        # Behavior breakdown
        if 'Behavior' in df.columns:
            behaviors = df['Behavior'].value_counts().to_dict()
            for behavior, count in behaviors.items():
                if behavior != 'N/A':
                    summary.append({
                        'Metric': f'{behavior} Controls',
                        'Value': count,
                        'Details': f'Controls with {behavior} behavior'
                    })

        # Guidance breakdown
        if 'Guidance' in df.columns:
            guidance_types = df['Guidance'].value_counts().to_dict()
            for guidance, count in guidance_types.items():
                if guidance != 'N/A':
                    summary.append({
                        'Metric': f'{guidance} Controls',
                        'Value': count,
                        'Details': f'Controls with {guidance} guidance level'
                    })

    return summary


def main():
    """
    Main execution function.

    AWS Control Tower is a global, account-scope service run from the
    management account (there is no region scan) -- failures are tracked
    per account-scope collector rather than via
    ``utils.scan_regions_concurrent`` (see scripts/shield_export.py /
    scripts/organizations_export.py for the account-scope reference
    pattern). Control Tower not being set up in this account (no landing
    zone found, AccessDenied on ListLandingZones) is a legitimate, graceful
    "service not enabled" state (exit 0, no marker) -- handled by
    ``collect_landing_zone()`` -- and must not be confused with a real
    collection failure on the landing-zone, organizational-units, or
    enabled-controls scopes, which are exported as a partial result
    (whatever succeeded, plus the always-written Summary sheet) and always
    surfaced via ``utils.report_collection_failures`` + a non-zero exit. A
    failed scope must never be silently collapsed into "nothing to report"
    (07.15.2026 / 07.16.2026 audits).
    """
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    script_name = Path(__file__).stem
    utils.setup_logging(script_name)
    utils.log_script_start(script_name)

    account_id, account_name = utils.print_script_banner("AWS CONTROL TOWER EXPORT")
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Please check your credentials.")
        return

    utils.log_info(f"AWS Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Detect partition and display appropriate messaging
    partition = utils.detect_partition()
    partition_name = "AWS GovCloud (US)" if partition == 'aws-us-gov' else "AWS Commercial"

    print(f"\nNote: AWS Control Tower is a global service in {partition_name}.")
    print("This script requires Control Tower to be set up and must be run from the management account.")
    print("\nRequired IAM Permissions:")
    print("  - controltower:ListLandingZones, controltower:GetLandingZone")
    print("  - controltower:ListEnabledControls, controltower:GetEnabledControl")
    print("  - controlcatalog:GetControl (for detailed control metadata)")
    print("  - organizations:ListRoots, organizations:ListOrganizationalUnitsForParent")

    if partition == 'aws-us-gov':
        print("\nGovCloud Limitations:")
        print("  - Audit and Log Archive accounts must pre-exist before Landing Zone setup")
        print("  - Account creation only via CreateGovCloudAccount API from Commercial region")
        print("  - Some controls have limited functionality in GovCloud")

    # Collect data
    print("\nCollecting AWS Control Tower configuration...")

    # Account-scope failure tracking (see scripts/shield_export.py).
    failed_scopes = []

    # PRIMARY scope 1/3: landing zone. A real API error here must propagate
    # to failed_scopes, never collapse into "Control Tower not set up".
    try:
        landing_zone = collect_landing_zone()
    except Exception as e:
        failed_scopes.append(('landing-zone', str(e)))
        utils.log_error(f"Control Tower landing zone collection failed: {e}")
        landing_zone = {}

    if not landing_zone and not failed_scopes:
        # Genuine not-set-up state: Control Tower has no landing zone in
        # this account and the collection itself succeeded (no error).
        # Nothing to export; not a failure.
        utils.log_warning(
            "No Control Tower landing zone found. Control Tower is not set "
            "up in this account. Exiting."
        )
        return

    # PRIMARY scope 2/3: organizational units.
    try:
        ous = collect_organizational_units()
    except Exception as e:
        failed_scopes.append(('organizational-units', str(e)))
        utils.log_error(f"Organizational units collection failed: {e}")
        ous = []

    # PRIMARY scope 3/3: enabled controls.
    try:
        controls = collect_enabled_controls(ous)
    except Exception as e:
        failed_scopes.append(('enabled-controls', str(e)))
        utils.log_error(f"Enabled controls collection failed: {e}")
        controls = []

    # Create DataFrames
    utils.log_info("Creating DataFrames...")

    dataframes = {}

    # Summary sheet is ALWAYS written once we reach this point (Control
    # Tower confirmed set up, or a real failure occurred on a scope) -- a
    # workbook must always land so a failed/partial export is never
    # mistaken for "nothing to report" (see
    # .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
    summary_rows = generate_summary(landing_zone, ous, controls)
    if summary_rows:
        dataframes['Summary'] = pd.DataFrame(summary_rows)

    # Add Enabled Controls next (user preference)
    if controls:
        df_controls = pd.DataFrame(controls)

        # Reorder columns for better readability
        column_order = [
            'OU Name',
            'OU ARN',
            'Control Identifier',
            'Control ARN',
            'Control Name',
            'Description',
            'Service',
            'Behavior',
            'Guidance',
            'Status',
            'Drift Status',
            'Inheritance Drift',
            'Resource Drift',
            'Parameters',
            'Last Operation ID'
        ]

        # Reorder columns (only include columns that exist)
        existing_columns = [col for col in column_order if col in df_controls.columns]
        df_controls = df_controls[existing_columns]

        df_controls = utils.prepare_dataframe_for_export(df_controls)
        dataframes['Enabled Controls'] = df_controls

        # Create filtered views for drifted and failed controls only
        df_drifted = df_controls[df_controls['Drift Status'] == 'DRIFTED']
        if not df_drifted.empty:
            dataframes['Drifted Controls'] = df_drifted

        df_failed = df_controls[df_controls['Status'] == 'FAILED']
        if not df_failed.empty:
            dataframes['Failed Controls'] = df_failed

    # Add Organizational Units second (user preference)
    if ous:
        df_ous = pd.DataFrame(ous)
        df_ous = utils.prepare_dataframe_for_export(df_ous)
        dataframes['Organizational Units'] = df_ous

    # Export to Excel. Whatever succeeded is exported -- a partial export
    # (Summary + any scopes that succeeded) is required even when some
    # scopes failed.
    if dataframes:
        filename = utils.create_export_filename(account_name, 'controltower', 'global')

        utils.log_info(f"Exporting to {filename}...")
        utils.save_multiple_dataframes_to_excel(dataframes, filename)

        # Log summary using correct function signature
        total_resources = len(controls)
        utils.log_export_summary('Control Tower Resources', total_resources, filename)
    elif not failed_scopes:
        # Genuinely empty: every scope succeeded and there is simply
        # nothing to export (should not normally happen since Summary is
        # always populated, but guarded defensively).
        utils.log_warning("No Control Tower data found to export")
    else:
        utils.log_error("Control Tower export failed to produce any data.")

    # If any primary scope failed, make it loud: write a marker and exit
    # non-zero, even though a partial export (Summary + whatever succeeded)
    # was written. A partial export that looks complete is exactly the
    # failure mode this guards against.
    if failed_scopes:
        utils.report_collection_failures(account_name, 'controltower', failed_scopes)
        print(
            "\nERROR: Control Tower export completed with failures — data is "
            "incomplete. See the *-controltower-FAILED-*.txt marker in the "
            "output directory."
        )
        sys.exit(1)

    utils.log_success("Control Tower export completed successfully")


if __name__ == "__main__":
    main()
