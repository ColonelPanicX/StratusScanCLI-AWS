#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Organizations Information Collection Script
Date: SEP-25-2025

Description:
This script collects comprehensive AWS Organizations information from AWS
environments including organizational units (OUs), accounts, service control policies
(SCPs), policy attachments, and organizational hierarchy. The data is exported to
an Excel spreadsheet with AWS-specific naming convention for governance
auditing and compliance reporting.

Collected information includes:
- Organizational structure with complete OU hierarchy
- Account details with status, email, and organizational placement
- Service Control Policies with content analysis and risk assessment
- Policy attachments showing which policies apply to which OUs/accounts
- Root organization configuration and settings
- Comprehensive summary analytics for governance oversight
"""

import datetime
import json
import sys
from pathlib import Path

from botocore.exceptions import ClientError, NoCredentialsError

# Add path to import utils module
try:
    # Try to import directly (if utils.py is in Python path)
    import utils
except ImportError:
    # If import fails, try to find the module relative to this script
    script_dir = Path(__file__).parent.absolute()

    # Check if we're in the scripts directory
    if script_dir.name.lower() == 'scripts':
        # Add the parent directory (StratusScan root) to the path
        sys.path.append(str(script_dir.parent))
    else:
        # Add the current directory to the path
        sys.path.append(str(script_dir))

    # Try import again
    try:
        import utils
    except ImportError:
        print("ERROR: Could not import the utils module. Make sure utils.py is in the StratusScan directory.")
        sys.exit(1)
args = utils.parse_script_args("Export AWS Organizations structure and SCPs to Excel")

def convert_datetime_to_string(dt_obj):
    """
    Convert datetime object to timezone-unaware string for Excel compatibility.

    Args:
        dt_obj: datetime object (can be timezone-aware or unaware)

    Returns:
        str: Formatted datetime string or 'N/A' if None/invalid
    """
    if dt_obj is None:
        return 'N/A'

    try:
        if hasattr(dt_obj, 'strftime'):
            # If timezone-aware, convert to UTC and make timezone-naive
            if hasattr(dt_obj, 'tzinfo') and dt_obj.tzinfo is not None:
                # Convert to UTC and remove timezone info
                utc_dt = dt_obj.utctimetuple()
                dt_obj = datetime.datetime(*utc_dt[:6])

            return dt_obj.strftime('%Y-%m-%d %H:%M:%S UTC')
        else:
            return str(dt_obj)
    except Exception:
        return 'N/A'
@utils.aws_error_handler("Getting account information", default_return=("Unknown", "Unknown-AWS-Account"))
def get_account_info():
    """
    Get the current AWS account ID and name with AWS validation.

    Returns:
        tuple: (account_id, account_name)
    """
    sts = utils.get_boto3_client('sts')
    account_id = sts.get_caller_identity()['Account']

    account_name = utils.get_account_name(account_id, default=f"AWS-ACCOUNT-{account_id}")

    return account_id, account_name

def get_organization_info():
    """
    Get basic organization information.

    Returns:
        dict: Organization information or None if not available
    """
    # Keep try-except for business logic: specific error handling for Organizations errors
    try:
        org_client = utils.get_boto3_client('organizations')

        # Describe organization
        org_response = org_client.describe_organization()
        organization = org_response.get('Organization', {})

        utils.log_success(f"Found AWS Organization: {organization.get('Id', 'Unknown')}")
        return organization

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'AWSOrganizationsNotInUseException':
            utils.log_warning("AWS Organizations is not enabled in this account.")
        elif error_code == 'AccessDeniedException':
            utils.log_error("Access denied to AWS Organizations. This script must be run from the management account with appropriate permissions.")
        else:
            utils.log_error(f"Error accessing AWS Organizations: {e}")
        return None
    except Exception as e:
        utils.log_error("Error getting organization information", e)
        return None

def collect_organizational_units():
    """
    Collect all organizational units (OUs) in the organization.

    This is a PRIMARY, account-scope collector (see
    scripts/shield_export.py for the account-scope reference pattern). It
    does not swallow errors to an empty list: a swallowed error here would
    be indistinguishable from an organization with no child OUs, producing
    silent data loss (see the 07.15.2026 / 07.16.2026 silent-collection-
    failure audits). Account-scope failures (client creation, root lookup,
    pagination) are allowed to raise so the caller (main) can record this
    scope as *failed* rather than *empty*. Per-OU errors are contained in
    ``collect_ous_recursive`` (logged and skipped).

    Returns:
        list: List of OU information dictionaries

    Raises:
        Exception: Any AWS/pagination error for the account scope (caller
            records it as a failed scope; it is never masked as empty).
    """
    ou_data = []

    org_client = utils.get_boto3_client('organizations')

    # Get root first
    roots = org_client.list_roots().get('Roots', [])
    if not roots:
        utils.log_error("No organization root found")
        return []

    root = roots[0]  # There should only be one root
    root_id = root.get('Id')

    utils.log_info("Collecting organizational units...")

    # Process root as an OU
    root_info = {
        'OU ID': root_id or 'N/A',
        'OU Name': root.get('Name', 'Root'),
        'OU Type': 'ROOT',
        'Parent ID': 'N/A',
        'Parent Name': 'N/A',
        'Level': 0,
        'Path': 'Root',
        'Policy Types': ', '.join([pt.get('Type', 'Unknown') for pt in root.get('PolicyTypes', [])]),
        'ARN': root.get('Arn', 'N/A')
    }
    ou_data.append(root_info)

    # Recursively collect all OUs
    collected_ous = collect_ous_recursive(org_client, root_id, "Root", 1)
    ou_data.extend(collected_ous)

    utils.log_success(f"Collected {len(ou_data)} organizational units")

    return ou_data

def _build_ou_row(org_client, ou, parent_id, parent_path, level):
    """
    Build the export row for a single organizational unit.

    Extracted so per-OU processing can be wrapped in try/except by the
    caller: a malformed OU entry must not sink the whole recursive branch.
    Required fields are read with ``.get()`` and a safe default for the
    same reason.

    Args:
        org_client: boto3 organizations client
        ou (dict): A single OrganizationalUnits entry.
        parent_id: Parent OU/root ID.
        parent_path: Full path to parent.
        level: Current nesting level.

    Returns:
        tuple: (ou_info dict, ou_id, ou_path) for the caller to use when
            recursing into this OU's children.
    """
    ou_id = ou.get('Id')
    ou_name = ou.get('Name', 'Unnamed OU')
    ou_path = f"{parent_path}/{ou_name}"

    # Get parent name
    try:
        if parent_id.startswith('r-'):  # Root
            parent_name = 'Root'
        else:
            parent_response = org_client.describe_organizational_unit(OrganizationalUnitId=parent_id)
            parent_name = parent_response.get('OrganizationalUnit', {}).get('Name', 'Unknown')
    except Exception:
        parent_name = 'Unknown'

    ou_info = {
        'OU ID': ou_id or 'N/A',
        'OU Name': ou_name,
        'OU Type': 'ORGANIZATIONAL_UNIT',
        'Parent ID': parent_id,
        'Parent Name': parent_name,
        'Level': level,
        'Path': ou_path,
        'Policy Types': 'N/A',  # Will be filled later if needed
        'ARN': ou.get('Arn', 'N/A')
    }

    return ou_info, ou_id, ou_path

def collect_ous_recursive(org_client, parent_id, parent_path, level):
    """
    Recursively collect OUs under a parent.

    Pagination/API errors propagate to the caller (the recursion is part of
    the ``collect_organizational_units`` PRIMARY scope). A single malformed
    OU entry is logged and skipped rather than aborting the whole branch.

    Args:
        org_client: boto3 organizations client
        parent_id: Parent OU ID
        parent_path: Full path to parent
        level: Current nesting level

    Returns:
        list: List of OU information dictionaries

    Raises:
        Exception: Any AWS/pagination error (caller records the parent
            scope as failed; it is never masked as empty).
    """
    ou_data = []

    # Get direct children OUs
    paginator = org_client.get_paginator('list_organizational_units_for_parent')

    for page in paginator.paginate(ParentId=parent_id):
        for ou in page.get('OrganizationalUnits', []):
            try:
                ou_info, ou_id, ou_path = _build_ou_row(org_client, ou, parent_id, parent_path, level)
            except Exception as e:
                ou_ref = ou.get('Id', 'Unknown') if isinstance(ou, dict) else 'Unknown'
                utils.log_warning(f"Skipping malformed OU '{ou_ref}' under parent {parent_id} due to a processing error: {e}")
                continue

            ou_data.append(ou_info)

            utils.log_info(f"Found OU: {ou_path}")

            # Recursively get children
            children = collect_ous_recursive(org_client, ou_id, ou_path, level + 1)
            ou_data.extend(children)

    return ou_data

def _build_account_row(org_client, account, processed, total_accounts):
    """
    Build the export row for a single organization account.

    Extracted so per-account processing can be wrapped in try/except by the
    caller: a malformed account entry must not sink the whole account-scope
    collection. Required fields are read with ``.get()`` and a safe default
    for the same reason.

    Args:
        org_client: boto3 organizations client
        account (dict): A single Accounts entry from list_accounts.
        processed: 1-based index of this account within the current run.
        total_accounts: Total account count, for progress logging.

    Returns:
        dict: The assembled account row.
    """
    progress = (processed / total_accounts) * 100 if total_accounts > 0 else 0
    account_id = account.get('Id')
    account_name = account.get('Name', 'Unnamed Account')

    utils.log_info(f"[{progress:.1f}%] Processing account {processed}/{total_accounts}: {account_name}")

    # Get account's parent OU
    parent_info = get_account_parent(org_client, account_id)

    # Get account tags if available
    account_tags = get_account_tags(org_client, account_id)

    return {
        'Account ID': account_id or 'N/A',
        'Account Name': account_name,
        'Email': account.get('Email', 'N/A'),
        'Status': account.get('Status', 'N/A'),
        'Join Method': account.get('JoinedMethod', 'N/A'),
        'Joined Date': convert_datetime_to_string(account.get('JoinedTimestamp')),
        'Parent ID': parent_info['parent_id'],
        'Parent Name': parent_info['parent_name'],
        'Parent Type': parent_info['parent_type'],
        'Full Path': parent_info['full_path'],
        'ARN': account.get('Arn', 'N/A'),
        'Tags': account_tags
    }

def collect_accounts():
    """
    Collect all accounts in the organization.

    This is a PRIMARY, account-scope collector (see
    scripts/shield_export.py for the account-scope reference pattern). It
    does not swallow errors to an empty list: a swallowed error here would
    be indistinguishable from an organization with no member accounts,
    producing silent data loss (see the 07.15.2026 / 07.16.2026 silent-
    collection-failure audits). Account-scope failures (client creation,
    pagination) are allowed to raise so the caller (main) can record this
    scope as *failed* rather than *empty*. Per-account errors are contained
    internally (logged and skipped).

    Returns:
        list: List of account information dictionaries

    Raises:
        Exception: Any AWS/pagination error for the account scope (caller
            records it as a failed scope; it is never masked as empty).
    """
    accounts_data = []

    org_client = utils.get_boto3_client('organizations')

    utils.log_info("Collecting organization accounts...")

    # Get all accounts
    paginator = org_client.get_paginator('list_accounts')
    total_accounts = 0

    # Count accounts first
    for page in paginator.paginate():
        total_accounts += len(page.get('Accounts', []))

    utils.log_info(f"Found {total_accounts} accounts to process")

    # Reset paginator and process accounts
    paginator = org_client.get_paginator('list_accounts')
    processed = 0
    skipped = 0

    for page in paginator.paginate():
        accounts = page.get('Accounts', [])

        for account in accounts:
            processed += 1

            try:
                account_info = _build_account_row(org_client, account, processed, total_accounts)
            except Exception as e:
                skipped += 1
                account_ref = account.get('Id', 'Unknown') if isinstance(account, dict) else 'Unknown'
                utils.log_error(f"Skipping account '{account_ref}' due to a processing error", e)
                continue

            accounts_data.append(account_info)

    if skipped:
        utils.log_warning(
            f"{skipped} of {total_accounts} account(s) were skipped due to "
            "processing errors (see log above); the remaining accounts were still collected."
        )

    return accounts_data

def get_account_parent(org_client, account_id):
    """
    Get the parent OU information for an account.

    Args:
        org_client: boto3 organizations client
        account_id: Account ID

    Returns:
        dict: Parent information
    """
    try:
        response = org_client.list_parents(ChildId=account_id)
        parents = response.get('Parents', [])

        if not parents:
            return {
                'parent_id': 'N/A',
                'parent_name': 'N/A',
                'parent_type': 'N/A',
                'full_path': 'N/A'
            }

        parent = parents[0]  # Should only be one parent
        parent_id = parent['Id']
        parent_type = parent['Type']

        # Get parent name and build full path
        if parent_type == 'ROOT':
            parent_name = 'Root'
            full_path = 'Root'
        else:
            try:
                parent_response = org_client.describe_organizational_unit(OrganizationalUnitId=parent_id)
                parent_name = parent_response['OrganizationalUnit'].get('Name', 'Unknown OU')

                # Build full path by walking up the hierarchy
                full_path = build_ou_path(org_client, parent_id)
            except Exception:
                parent_name = 'Unknown OU'
                full_path = 'Unknown Path'

        return {
            'parent_id': parent_id,
            'parent_name': parent_name,
            'parent_type': parent_type,
            'full_path': full_path
        }

    except Exception:
        return {
            'parent_id': 'Unknown',
            'parent_name': 'Unknown',
            'parent_type': 'Unknown',
            'full_path': 'Unknown'
        }

def build_ou_path(org_client, ou_id):
    """
    Build the full path for an OU by walking up the hierarchy.

    Args:
        org_client: boto3 organizations client
        ou_id: OU ID

    Returns:
        str: Full path string
    """
    path_parts = []
    current_id = ou_id

    try:
        while current_id:
            if current_id.startswith('r-'):  # Root
                path_parts.append('Root')
                break

            # Get current OU info
            ou_response = org_client.describe_organizational_unit(OrganizationalUnitId=current_id)
            ou_name = ou_response['OrganizationalUnit'].get('Name', 'Unknown')
            path_parts.append(ou_name)

            # Get parent
            parents_response = org_client.list_parents(ChildId=current_id)
            parents = parents_response.get('Parents', [])

            if not parents:
                break

            current_id = parents[0]['Id']

        # Reverse to get root-to-leaf order
        path_parts.reverse()
        return '/'.join(path_parts)

    except Exception:
        return f"Error building path for {ou_id}"

def get_account_tags(org_client, account_id):
    """
    Get tags for an account.

    Args:
        org_client: boto3 organizations client
        account_id: Account ID

    Returns:
        str: Formatted tags string
    """
    try:
        tags = []
        for page in org_client.get_paginator('list_tags_for_resource').paginate(ResourceId=account_id):
            tags.extend(page.get('Tags', []))

        if tags:
            tag_strings = [f"{tag.get('Key', 'Unknown')}={tag.get('Value', 'Unknown')}" for tag in tags]
            return ', '.join(tag_strings)
        else:
            return 'No Tags'

    except Exception:
        return 'Unable to retrieve tags'

def _build_policy_row(org_client, policy, policy_type):
    """
    Build the export row for a single policy, including content analysis
    and attachment targets.

    Extracted so per-policy processing can be wrapped in try/except by the
    caller: a malformed policy entry (or a transient describe_policy
    failure) must not sink collection of the remaining policies. Required
    fields are read with ``.get()`` and a safe default for the same reason.

    Args:
        org_client: boto3 organizations client
        policy (dict): A single Policies summary entry from list_policies.
        policy_type: The policy type being processed (e.g. SERVICE_CONTROL_POLICY).

    Returns:
        dict: The assembled policy row.
    """
    policy_id = policy.get('Id')
    policy_name = policy.get('Name', 'Unnamed Policy')

    utils.log_info(f"Processing {policy_type}: {policy_name}")

    policy_response = org_client.describe_policy(PolicyId=policy_id)
    policy_details = policy_response.get('Policy', {})

    # Get policy content if it's an SCP
    policy_content = ''
    policy_summary = ''
    if policy_type == 'SERVICE_CONTROL_POLICY':
        policy_content = policy_details.get('Content', '')
        policy_summary = analyze_scp_content(policy_content)

    # Get policy targets (what it's attached to)
    targets = get_policy_targets(org_client, policy_id)

    return {
        'Policy ID': policy_id or 'N/A',
        'Policy Name': policy_name,
        'Policy Type': policy_type,
        'Description': policy_details.get('PolicySummary', {}).get('Description', 'N/A'),
        'AWS Managed': 'Yes' if policy_details.get('PolicySummary', {}).get('AwsManaged', False) else 'No',
        'Content Size': len(policy_content) if policy_content else 0,
        'Policy Summary': policy_summary[:500] if policy_summary else 'N/A',
        'Attached To Count': len(targets),
        'Attached To': ', '.join(targets)[:500] if targets else 'Not Attached',
        'ARN': policy_details.get('PolicySummary', {}).get('Arn', 'N/A')
    }

def collect_policies():
    """
    Collect all service control policies (SCPs) and other policies.

    This is a PRIMARY, account-scope collector (see
    scripts/shield_export.py for the account-scope reference pattern). It
    does not swallow errors to an empty list: a swallowed error here would
    be indistinguishable from an organization with no custom policies,
    producing silent data loss (see the 07.15.2026 / 07.16.2026 silent-
    collection-failure audits). ``PolicyTypeNotEnabledException`` for a
    single policy type is a legitimate, expected condition (that policy
    type simply is not turned on for this organization) and is skipped
    gracefully; any other API/pagination error propagates so the caller
    (main) can record this scope as *failed* rather than *empty*.
    Per-policy errors are contained internally (logged and skipped).

    Returns:
        list: List of policy information dictionaries

    Raises:
        Exception: Any AWS/pagination error other than a not-enabled
            policy type (caller records it as a failed scope; it is never
            masked as empty).
    """
    policies_data = []
    skipped = 0

    org_client = utils.get_boto3_client('organizations')

    utils.log_info("Collecting organization policies...")

    # Get all policy types
    policy_types = ['SERVICE_CONTROL_POLICY', 'TAG_POLICY', 'BACKUP_POLICY', 'AISERVICES_OPT_OUT_POLICY']

    for policy_type in policy_types:
        try:
            paginator = org_client.get_paginator('list_policies')

            for page in paginator.paginate(Filter=policy_type):
                policies = page.get('Policies', [])

                for policy in policies:
                    try:
                        policy_info = _build_policy_row(org_client, policy, policy_type)
                    except Exception as e:
                        skipped += 1
                        policy_ref = policy.get('Id', 'Unknown') if isinstance(policy, dict) else 'Unknown'
                        utils.log_warning(f"Skipping policy '{policy_ref}' due to a processing error: {e}")
                        continue

                    policies_data.append(policy_info)

        except ClientError as e:
            if 'PolicyTypeNotEnabledException' in str(e):
                # Legitimate, expected condition: this policy type is not
                # enabled for the organization. Skip it, keep collecting
                # the remaining policy types.
                utils.log_info(f"Policy type {policy_type} is not enabled in this organization")
            else:
                raise

    if skipped:
        utils.log_warning(
            f"{skipped} policy(ies) were skipped due to processing errors "
            "(see log above); the remaining policies were still collected."
        )

    utils.log_success(f"Collected {len(policies_data)} policies")

    return policies_data

def analyze_scp_content(policy_content):
    """
    Analyze SCP content to provide a summary.

    Args:
        policy_content: Policy JSON content

    Returns:
        str: Summary of the policy
    """
    try:
        if not policy_content:
            return 'No content available'

        policy_doc = json.loads(policy_content)
        statements = policy_doc.get('Statement', [])

        if not isinstance(statements, list):
            statements = [statements]

        summary_parts = []

        for i, stmt in enumerate(statements):
            effect = stmt.get('Effect', 'Unknown')
            actions = stmt.get('Action', [])
            if isinstance(actions, str):
                actions = [actions]

            resources = stmt.get('Resource', [])
            if isinstance(resources, str):
                resources = [resources]

            conditions = stmt.get('Condition', {})

            stmt_summary = f"Statement {i+1}: {effect}"

            if actions:
                if len(actions) <= 3:
                    stmt_summary += f" on {', '.join(actions)}"
                else:
                    stmt_summary += f" on {len(actions)} actions"

            if resources and resources != ['*']:
                if len(resources) <= 2:
                    stmt_summary += f" for {', '.join(resources[:2])}"
                else:
                    stmt_summary += f" for {len(resources)} resources"

            if conditions:
                stmt_summary += " with conditions"

            summary_parts.append(stmt_summary)

        return '; '.join(summary_parts)

    except Exception as e:
        return f"Error analyzing policy: {str(e)}"

def get_policy_targets(org_client, policy_id):
    """
    Get the targets (OUs/accounts) that a policy is attached to.

    Args:
        org_client: boto3 organizations client
        policy_id: Policy ID

    Returns:
        list: List of target descriptions
    """
    targets = []

    try:
        paginator = org_client.get_paginator('list_targets_for_policy')

        for page in paginator.paginate(PolicyId=policy_id):
            for target in page.get('Targets', []):
                target_id = target.get('TargetId')
                target_type = target.get('Type')

                if target_type == 'ROOT':
                    targets.append('Root')
                elif target_type == 'ORGANIZATIONAL_UNIT':
                    try:
                        ou_response = org_client.describe_organizational_unit(OrganizationalUnitId=target_id)
                        ou_name = ou_response.get('OrganizationalUnit', {}).get('Name', target_id)
                        targets.append(f"OU: {ou_name}")
                    except Exception:
                        targets.append(f"OU: {target_id}")
                elif target_type == 'ACCOUNT':
                    try:
                        account_response = org_client.describe_account(AccountId=target_id)
                        account_name = account_response.get('Account', {}).get('Name', target_id)
                        targets.append(f"Account: {account_name}")
                    except Exception:
                        targets.append(f"Account: {target_id}")

    except Exception as e:
        utils.log_warning(f"Error getting targets for policy {policy_id}: {e}")

    return targets

def clean_dataframe_datetimes(df):
    """
    Clean timezone-aware datetimes in a pandas DataFrame for Excel compatibility.

    Args:
        df: pandas DataFrame

    Returns:
        pandas DataFrame: DataFrame with timezone-unaware datetime strings
    """
    try:
        import pandas as pd

        # Convert any datetime columns to strings
        for col in df.columns:
            if df[col].dtype == 'datetime64[ns, UTC]' or any(isinstance(val, datetime.datetime) for val in df[col].dropna()):
                # Convert datetime objects to strings
                df[col] = df[col].apply(lambda x: convert_datetime_to_string(x) if pd.notna(x) else 'N/A')

        return df
    except Exception as e:
        utils.log_warning(f"Error cleaning datetime objects in dataframe: {e}")
        return df

def export_to_excel(org_info, ou_data, accounts_data, policies_data, account_id, account_name):
    """
    Export Organizations data to Excel file with AWS naming convention.

    Args:
        org_info: Organization information dictionary
        ou_data: List of OU information dictionaries
        accounts_data: List of account information dictionaries
        policies_data: List of policy information dictionaries
        account_id: AWS account ID
        account_name: AWS account name

    Returns:
        str: Filename of exported file or None if failed
    """
    if not org_info and not ou_data and not accounts_data and not policies_data:
        utils.log_warning("No Organizations data to export.")
        return None

    try:
        # Import pandas after dependency check
        import pandas as pd

        # Generate filename with AWS identifier
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")

        # Use utils module to generate filename and save data with AWS identifier
        filename = utils.create_export_filename(
            account_name,
            "organizations",
            "",
            current_date
        )

        # Create data frames for multi-sheet export
        data_frames = {}

        # Organization overview sheet
        if org_info:
            org_overview = {
                'Property': [
                    'Organization ID',
                    'Master Account ID',
                    'Master Account Email',
                    'Feature Set',
                    'Available Policy Types',
                    'ARN'
                ],
                'Value': [
                    org_info.get('Id', 'N/A'),
                    org_info.get('MasterAccountId', 'N/A'),
                    org_info.get('MasterAccountEmail', 'N/A'),
                    org_info.get('FeatureSet', 'N/A'),
                    ', '.join([pt['Type'] for pt in org_info.get('AvailablePolicyTypes', [])]),
                    org_info.get('Arn', 'N/A')
                ]
            }
            org_df = pd.DataFrame(org_overview)
            data_frames['Organization Overview'] = org_df

        # Organizational Units sheet
        if ou_data:
            ou_df = pd.DataFrame(ou_data)
            ou_df = clean_dataframe_datetimes(ou_df)
            data_frames['Organizational Units'] = ou_df

        # Accounts sheet
        if accounts_data:
            accounts_df = pd.DataFrame(accounts_data)
            accounts_df = clean_dataframe_datetimes(accounts_df)
            data_frames['Accounts'] = accounts_df

        # Policies sheet
        if policies_data:
            policies_df = pd.DataFrame(policies_data)
            policies_df = clean_dataframe_datetimes(policies_df)
            data_frames['Policies'] = policies_df

        # Create comprehensive summary data
        summary_data = {
            'Metric': [
                'Total Organizational Units',
                'Total Accounts',
                'Active Accounts',
                'Suspended Accounts',
                'Total Policies',
                'Service Control Policies',
                'AWS Managed Policies',
                'Custom Policies',
                'Maximum OU Depth',
                'Accounts in Root',
                'Policies Attached to Root'
            ],
            'Count': [
                len([ou for ou in ou_data if ou.get('OU Type') == 'ORGANIZATIONAL_UNIT']),
                len(accounts_data),
                len([acc for acc in accounts_data if acc.get('Status') == 'ACTIVE']),
                len([acc for acc in accounts_data if acc.get('Status') == 'SUSPENDED']),
                len(policies_data),
                len([pol for pol in policies_data if pol.get('Policy Type') == 'SERVICE_CONTROL_POLICY']),
                len([pol for pol in policies_data if pol.get('AWS Managed') == 'Yes']),
                len([pol for pol in policies_data if pol.get('AWS Managed') == 'No']),
                max([ou.get('Level', 0) for ou in ou_data] + [0]),
                len([acc for acc in accounts_data if acc.get('Parent Type') == 'ROOT']),
                len([pol for pol in policies_data if 'Root' in pol.get('Attached To', '')])
            ]
        }

        summary_df = pd.DataFrame(summary_data)
        data_frames['Summary'] = summary_df

        # Save using utils function for multi-sheet Excel
        output_path = utils.save_multiple_dataframes_to_excel(data_frames, filename)

        if output_path:
            utils.log_success("AWS Organizations data exported successfully!")
            utils.log_success(f"File location: {output_path}")

            # Log summary statistics
            total_ous = len([ou for ou in ou_data if ou.get('OU Type') == 'ORGANIZATIONAL_UNIT'])
            total_accounts = len(accounts_data)
            total_policies = len(policies_data)
            utils.log_info(f"Export contains {total_ous} OUs, {total_accounts} accounts, and {total_policies} policies")

            return str(output_path)
        else:
            utils.log_error("Error exporting to Excel. Please check the logs.")
            return None

    except Exception as e:
        utils.log_error("Error exporting to Excel", e)
        return None

def main():
    """
    Main function to orchestrate the Organizations information collection.

    AWS Organizations is a global, account-scope service run from the
    management account (there is no region scan) -- failures are tracked
    per account-scope collector rather than via
    ``utils.scan_regions_concurrent`` (see scripts/shield_export.py /
    scripts/lambda_export.py for the account-scope reference pattern).
    "AWS Organizations is not in use" / "this is not the management
    account" is a legitimate, graceful "service not enabled" state (exit 0,
    no marker) -- handled by ``get_organization_info()`` -- and must not be
    confused with a real collection failure on the organizational-units,
    accounts, or policies scopes, which are exported as a partial result
    (whatever succeeded) and always surfaced via
    ``utils.report_collection_failures`` + a non-zero exit. A failed scope
    must never be silently collapsed into "nothing to report" (07.15.2026 /
    07.16.2026 audits).
    """
    try:
        # Check dependencies first
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            return

        # Import pandas after dependency check

        # Print title and get account info
        utils.setup_logging("organizations-export")
        account_id, account_name = utils.print_script_banner("AWS ORGANIZATIONS INFORMATION COLLECTION EXPORT")

        try:
            # Test AWS credentials
            sts = utils.get_boto3_client('sts')
            sts.get_caller_identity()
            utils.log_success("AWS credentials validated")

        except NoCredentialsError:
            utils.log_error("AWS credentials not found. Please configure your credentials using:")
            print("  - AWS CLI: aws configure")
            print("  - Environment variables: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            print("  - IAM role (if running on EC2)")
            sys.exit(1)
        except Exception as e:
            utils.log_error("Error validating AWS credentials", e)
            sys.exit(1)

        utils.log_info("Starting Organizations information collection from AWS...")
        print("====================================================================")

        # Get organization information
        utils.log_info("Getting organization information...")
        org_info = get_organization_info()

        if not org_info:
            # Graceful skip: AWS Organizations is not enabled in this
            # account, or this is not the management account. This is a
            # legitimate, expected state -- NOT a collection failure -- so
            # no failure marker is written and the process exits 0.
            utils.log_error("Could not access AWS Organizations. This script must be run from the management account.")
            utils.log_info("Please ensure you have the necessary permissions and are running from the management account.")
            return

        # Account-scope failure tracking (see scripts/shield_export.py).
        failed_scopes = []

        # PRIMARY scope 1/3: organizational units. A real API error here
        # must propagate to failed_scopes, never collapse into an empty
        # list that reads as "no OUs configured".
        utils.log_info("Collecting organizational units...")
        try:
            ou_data = collect_organizational_units()
        except Exception as e:
            failed_scopes.append(('organizational_units', str(e)))
            utils.log_error(f"Organizational units collection failed: {e}")
            ou_data = []

        # PRIMARY scope 2/3: accounts.
        utils.log_info("Collecting accounts...")
        try:
            accounts_data = collect_accounts()
        except Exception as e:
            failed_scopes.append(('accounts', str(e)))
            utils.log_error(f"Accounts collection failed: {e}")
            accounts_data = []

        # PRIMARY scope 3/3: policies.
        utils.log_info("Collecting policies...")
        try:
            policies_data = collect_policies()
        except Exception as e:
            failed_scopes.append(('policies', str(e)))
            utils.log_error(f"Policies collection failed: {e}")
            policies_data = []

        print("\n====================================================================")
        print("COLLECTION COMPLETE")
        print("====================================================================")

        # Export whatever succeeded -- a partial export is required even
        # when some scopes failed (see
        # .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
        filename = export_to_excel(org_info, ou_data, accounts_data, policies_data, account_id, account_name)

        if filename:
            utils.log_info("Results exported with AWS compliance markers")
            utils.log_info(f"Organization ID: {org_info.get('Id', 'Unknown')}")
            utils.log_info(f"Total OUs processed: {len([ou for ou in ou_data if ou.get('OU Type') == 'ORGANIZATIONAL_UNIT'])}")
            utils.log_info(f"Total accounts processed: {len(accounts_data)}")
            utils.log_info(f"Total policies processed: {len(policies_data)}")

            # Display summary statistics
            if accounts_data:
                active_accounts = len([acc for acc in accounts_data if acc.get('Status') == 'ACTIVE'])
                utils.log_info(f"Active accounts: {active_accounts}")

            if policies_data:
                scps = len([pol for pol in policies_data if pol.get('Policy Type') == 'SERVICE_CONTROL_POLICY'])
                utils.log_info(f"Service Control Policies: {scps}")

            print("\nScript execution completed.")
        elif not failed_scopes:
            # Genuinely empty: every scope succeeded and there is simply
            # nothing to export.
            utils.log_warning("No Organizations data collected. Nothing to export.")
        else:
            utils.log_error("Export failed. Please check the logs.")

        # If any primary scope failed, make it loud: write a marker and
        # exit non-zero, even if a partial export was written. A partial
        # export that looks complete is exactly the failure mode this
        # guards against.
        if failed_scopes:
            utils.report_collection_failures(account_name, 'organizations', failed_scopes)
            print(
                "\nERROR: Organizations export completed with failures — data is "
                "incomplete. See the *-organizations-FAILED-*.txt marker in the "
                "output directory."
            )
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        utils.log_error("Unexpected error occurred", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
