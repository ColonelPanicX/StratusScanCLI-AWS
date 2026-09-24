#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Transfer Family Comprehensive Export Script
Date: NOV-14-2025

Description:
This script performs a comprehensive export of all AWS Transfer Family resources from AWS
environments including servers, users, connectors, workflows, certificates, and agreements.
All data is consolidated into a single Excel workbook with separate sheets for each resource
type, plus summary sheets for comprehensive analysis.

Collected information includes:
- Servers (SFTP, FTPS, FTP, AS2) with endpoint and identity provider details
- Users (per server) with home directory and SSH key configurations
- Connectors (AS2 and SFTP) for outbound file transfers
- Workflows (automated file processing)
- Certificates (for AS2 and FTPS)
- Agreements (AS2 trading partner agreements)
"""

import datetime
import json
import sys
from pathlib import Path
from typing import Any

# Add path to import utils module
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export AWS Transfer Family servers and users to Excel")

def format_protocols(protocols: list[str]) -> str:
    """Format protocol list into a readable string."""
    if not protocols:
        return "None"
    return ", ".join(protocols)


def format_list_field(field_value: Any) -> str:
    """Format list fields into comma-separated strings."""
    if not field_value:
        return "None"
    if isinstance(field_value, list):
        return ", ".join(str(item) for item in field_value)
    return str(field_value)


def format_json_field(field_value: Any, max_length: int = 200) -> str:
    """Format JSON/dict fields into readable strings."""
    if not field_value:
        return "None"
    if isinstance(field_value, (dict, list)):
        json_str = json.dumps(field_value, indent=None)
        if len(json_str) > max_length:
            return json_str[:max_length] + "..."
        return json_str
    return str(field_value)


def _build_server_row(item: dict, region: str) -> dict[str, Any]:
    """
    Build a single Transfer Family server export row.

    ``item`` is the ``describe_server`` response's ``Server`` dict, augmented
    with ``ServerId`` and ``UserCount`` by the caller. Every field is read
    with ``.get()`` and a safe default so a malformed/divergent server
    variant is skipped by the caller's try/except rather than raising here
    with a hard subscript (the RDS-Custom KeyError pattern — see
    .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
    """
    server_id = item.get('ServerId', '')

    return {
        'Server ID': server_id,
        'Region': region,
        'ARN': item.get('Arn', 'N/A'),
        'State': item.get('State', 'N/A'),
        'Protocols': format_protocols(item.get('Protocols', [])),
        'Endpoint Type': item.get('EndpointType', 'N/A'),
        'Identity Provider Type': item.get('IdentityProviderType', 'N/A'),
        'Domain': item.get('Domain', 'N/A'),
        'Logging Role ARN': item.get('LoggingRole', 'None'),
        'Security Policy': item.get('SecurityPolicyName', 'N/A'),
        'Custom Hostname': item.get('HostKeyFingerprint', 'None'),
        'Certificate ARN': item.get('Certificate', 'None'),
        'VPC Endpoint ID': item.get('EndpointDetails', {}).get('VpcEndpointId', 'None'),
        'VPC ID': item.get('EndpointDetails', {}).get('VpcId', 'None'),
        'Subnet IDs': format_list_field(item.get('EndpointDetails', {}).get('SubnetIds', [])),
        'Security Group IDs': format_list_field(item.get('EndpointDetails', {}).get('SecurityGroupIds', [])),
        'Address Allocation IDs': format_list_field(item.get('EndpointDetails', {}).get('AddressAllocationIds', [])),
        'Availability Zone': item.get('EndpointDetails', {}).get('AvailabilityZone', 'N/A'),
        'User Count': item.get('UserCount', 0),
        'Workflow Details': format_json_field(item.get('WorkflowDetails', {})),
        'Structured Availability Zone': item.get('StructuredLogDestinations', 'None'),
        'Tags': format_list_field([f"{tag.get('Key', '')}={tag.get('Value', '')}" for tag in item.get('Tags', [])])
    }


def _scan_transfer_servers_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Transfer Family server information from a single region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the region
    as failed instead of silently reporting "no Transfer Family servers" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed servers are skipped (logged) rather than aborting
    the whole region.

    Args:
        region: AWS region name

    Returns:
        List of dictionaries containing server information

    Raises:
        Exception: Any AWS/pagination error for the region (caller records it
            as a failed region and surfaces it; it is never masked as empty).
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting Transfer Family servers in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    servers_data = []

    # List all servers in the region
    paginator = transfer_client.get_paginator('list_servers')
    server_ids = []

    for page in paginator.paginate():
        for server in page.get('Servers', []):
            sid = server.get('ServerId')
            if sid:
                server_ids.append(sid)

    if not server_ids:
        utils.log_info(f"No Transfer Family servers found in {region}")
        return []

    utils.log_info(f"Found {len(server_ids)} Transfer Family servers in {region}")

    # Get detailed information for each server. One malformed server must not
    # sink the region, so each is built inside try/except; failures are
    # logged and skipped.
    skipped = 0
    for idx, server_id in enumerate(server_ids, 1):
        progress = (idx / len(server_ids)) * 100
        utils.log_info(f"[{progress:.1f}%] Processing server {idx}/{len(server_ids)}: {server_id}")

        try:
            # Get server details
            response = transfer_client.describe_server(ServerId=server_id)
            server = dict(response.get('Server', {}))
            server['ServerId'] = server_id

            # Count users for this server (graceful enrichment — does not
            # fail the row if it errors)
            user_count = 0
            try:
                user_paginator = transfer_client.get_paginator('list_users')
                for user_page in user_paginator.paginate(ServerId=server_id):
                    user_count += len(user_page.get('Users', []))
            except Exception as e:
                utils.log_warning(f"Could not count users for server {server_id}: {e}")
            server['UserCount'] = user_count

            servers_data.append(_build_server_row(server, region))

        except Exception as e:
            skipped += 1
            utils.log_error(
                f"Skipping malformed Transfer Family server '{server_id}' in {region} due to a "
                "processing error", e,
            )
            continue

    if skipped:
        utils.log_warning(
            f"{skipped} of {len(server_ids)} Transfer Family server(s) in {region} were skipped due "
            "to processing errors (see log above); the remaining servers were still collected."
        )

    utils.log_success(f"Collected {len(servers_data)} servers from {region}")

    return servers_data


def collect_transfer_servers_all_regions(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Transfer Family server information across regions, surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Args:
        regions: List of AWS regions to scan

    Returns:
        tuple: ``(servers, failed_regions)`` where ``failed_regions`` is a
        list of ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING TRANSFER FAMILY SERVERS ===")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_transfer_servers_region,
        show_progress=True,
        collect_failures=True,
    )
    all_servers = [server for result in region_results for server in result]

    utils.log_success(f"Total Transfer Family servers collected: {len(all_servers)}")
    return all_servers, failed_regions


@utils.aws_error_handler("Collecting Transfer Family users", default_return=[])
def collect_transfer_users(region: str, server_ids: list[str]) -> list[dict[str, Any]]:
    """
    Collect Transfer Family user information for all servers in a region.

    Args:
        region: AWS region name
        server_ids: List of server IDs to collect users from

    Returns:
        List of dictionaries containing user information
    """
    if not server_ids:
        return []

    utils.log_info(f"Collecting Transfer Family users in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    users_data = []

    for server_id in server_ids:
        try:
            # List users for this server
            paginator = transfer_client.get_paginator('list_users')
            usernames = []

            for page in paginator.paginate(ServerId=server_id):
                for user in page.get('Users', []):
                    usernames.append(user['UserName'])

            if not usernames:
                continue

            utils.log_info(f"Found {len(usernames)} users on server {server_id}")

            # Get detailed information for each user
            for username in usernames:
                try:
                    response = transfer_client.describe_user(ServerId=server_id, UserName=username)
                    user = response['User']

                    # DescribeUser returns the user's SSH public keys; Transfer has no ListSshPublicKeys operation.
                    ssh_key_count = len(user.get('SshPublicKeys', []))

                    user_info = {
                        'Server ID': server_id,
                        'Region': region,
                        'Username': username,
                        'ARN': user.get('Arn', 'N/A'),
                        'Role ARN': user.get('Role', 'N/A'),
                        'Home Directory Type': user.get('HomeDirectoryType', 'PATH'),
                        'Home Directory': user.get('HomeDirectory', 'None'),
                        'Home Directory Mappings': format_json_field(user.get('HomeDirectoryMappings', [])),
                        'SSH Public Key Count': ssh_key_count,
                        'POSIX Profile UID': user.get('PosixProfile', {}).get('Uid', 'None'),
                        'POSIX Profile GID': user.get('PosixProfile', {}).get('Gid', 'None'),
                        'POSIX Secondary GIDs': format_list_field(user.get('PosixProfile', {}).get('SecondaryGids', [])),
                        'Policy': 'Present' if user.get('Policy') else 'None',
                        'Tags': format_list_field([f"{tag['Key']}={tag['Value']}" for tag in user.get('Tags', [])])
                    }

                    users_data.append(user_info)

                except Exception as e:
                    utils.log_error(f"Error collecting user {username} on server {server_id}: {e}")
                    continue

        except Exception as e:
            utils.log_error(f"Error listing users for server {server_id}: {e}")
            continue

    utils.log_success(f"Collected {len(users_data)} users from {region}")
    return users_data


@utils.aws_error_handler("Collecting Transfer Family connectors", default_return=[])
def collect_transfer_connectors(region: str) -> list[dict[str, Any]]:
    """
    Collect Transfer Family connector information from a specific region.

    Args:
        region: AWS region name

    Returns:
        List of dictionaries containing connector information
    """
    utils.log_info(f"Collecting Transfer Family connectors in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    connectors_data = []

    try:
        # List all connectors
        paginator = transfer_client.get_paginator('list_connectors')
        connector_ids = []

        for page in paginator.paginate():
            for connector in page.get('Connectors', []):
                connector_ids.append(connector['ConnectorId'])

        if not connector_ids:
            utils.log_info(f"No Transfer Family connectors found in {region}")
            return []

        utils.log_info(f"Found {len(connector_ids)} connectors in {region}")

        # Get detailed information for each connector
        for connector_id in connector_ids:
            try:
                response = transfer_client.describe_connector(ConnectorId=connector_id)
                connector = response['Connector']

                # Extract AS2 or SFTP config details
                connector_type = "SFTP" if connector.get('SftpConfig') else "AS2"

                if connector_type == "AS2":
                    as2_config = connector.get('As2Config', {})
                    config_details = {
                        'Local Profile ID': as2_config.get('LocalProfileId', 'N/A'),
                        'Partner Profile ID': as2_config.get('PartnerProfileId', 'N/A'),
                        'Message Subject': as2_config.get('MessageSubject', 'N/A'),
                        'MDN Response': as2_config.get('MdnResponse', 'N/A'),
                        'SFTP Config': 'None'
                    }
                else:
                    sftp_config = connector.get('SftpConfig', {})
                    config_details = {
                        'Local Profile ID': 'N/A',
                        'Partner Profile ID': 'N/A',
                        'Message Subject': 'N/A',
                        'MDN Response': 'N/A',
                        'SFTP Config': format_json_field(sftp_config)
                    }

                connector_info = {
                    'Connector ID': connector_id,
                    'Region': region,
                    'ARN': connector.get('Arn', 'N/A'),
                    'Type': connector_type,
                    'URL': connector.get('Url', 'N/A'),
                    'Access Role ARN': connector.get('AccessRole', 'N/A'),
                    'Logging Role ARN': connector.get('LoggingRole', 'None'),
                    'Service Managed EGRESS IPs': format_list_field(connector.get('ServiceManagedEgressIpAddresses', [])),
                    **config_details,
                    'Tags': format_list_field([f"{tag['Key']}={tag['Value']}" for tag in connector.get('Tags', [])])
                }

                connectors_data.append(connector_info)

            except Exception as e:
                utils.log_error(f"Error collecting connector {connector_id} in {region}: {e}")
                continue

        utils.log_success(f"Collected {len(connectors_data)} connectors from {region}")

    except Exception as e:
        utils.log_error(f"Error listing connectors in {region}: {e}")

    return connectors_data


@utils.aws_error_handler("Collecting Transfer Family workflows", default_return=[])
def collect_transfer_workflows(region: str) -> list[dict[str, Any]]:
    """
    Collect Transfer Family workflow information from a specific region.

    Args:
        region: AWS region name

    Returns:
        List of dictionaries containing workflow information
    """
    utils.log_info(f"Collecting Transfer Family workflows in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    workflows_data = []

    try:
        # List all workflows
        paginator = transfer_client.get_paginator('list_workflows')
        workflow_ids = []

        for page in paginator.paginate():
            for workflow in page.get('Workflows', []):
                workflow_ids.append(workflow['WorkflowId'])

        if not workflow_ids:
            utils.log_info(f"No Transfer Family workflows found in {region}")
            return []

        utils.log_info(f"Found {len(workflow_ids)} workflows in {region}")

        # Get detailed information for each workflow
        for workflow_id in workflow_ids:
            try:
                response = transfer_client.describe_workflow(WorkflowId=workflow_id)
                workflow = response['Workflow']

                # Extract step information
                on_upload_steps = workflow.get('OnUpload', {}).get('Steps', [])
                on_exception_steps = workflow.get('OnException', {}).get('Steps', [])

                step_types = []
                for step in on_upload_steps:
                    if 'CopyStepDetails' in step:
                        step_types.append('COPY')
                    elif 'CustomStepDetails' in step:
                        step_types.append('CUSTOM')
                    elif 'TagStepDetails' in step:
                        step_types.append('TAG')
                    elif 'DeleteStepDetails' in step:
                        step_types.append('DELETE')
                    elif 'DecryptStepDetails' in step:
                        step_types.append('DECRYPT')

                workflow_info = {
                    'Workflow ID': workflow_id,
                    'Region': region,
                    'ARN': workflow.get('Arn', 'N/A'),
                    'Description': workflow.get('Description', 'None'),
                    'On Upload Steps': len(on_upload_steps),
                    'On Exception Steps': len(on_exception_steps),
                    'Step Types': format_list_field(step_types),
                    'On Upload Details': format_json_field(on_upload_steps, max_length=300),
                    'On Exception Details': format_json_field(on_exception_steps, max_length=300),
                    'Tags': format_list_field([f"{tag['Key']}={tag['Value']}" for tag in workflow.get('Tags', [])])
                }

                workflows_data.append(workflow_info)

            except Exception as e:
                utils.log_error(f"Error collecting workflow {workflow_id} in {region}: {e}")
                continue

        utils.log_success(f"Collected {len(workflows_data)} workflows from {region}")

    except Exception as e:
        utils.log_error(f"Error listing workflows in {region}: {e}")

    return workflows_data


@utils.aws_error_handler("Collecting Transfer Family certificates", default_return=[])
def collect_transfer_certificates(region: str) -> list[dict[str, Any]]:
    """
    Collect Transfer Family certificate information from a specific region.

    Args:
        region: AWS region name

    Returns:
        List of dictionaries containing certificate information
    """
    utils.log_info(f"Collecting Transfer Family certificates in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    certificates_data = []

    try:
        # List all certificates
        paginator = transfer_client.get_paginator('list_certificates')
        certificate_ids = []

        for page in paginator.paginate():
            for certificate in page.get('Certificates', []):
                certificate_ids.append(certificate['CertificateId'])

        if not certificate_ids:
            utils.log_info(f"No Transfer Family certificates found in {region}")
            return []

        utils.log_info(f"Found {len(certificate_ids)} certificates in {region}")

        # Get detailed information for each certificate
        for certificate_id in certificate_ids:
            try:
                response = transfer_client.describe_certificate(CertificateId=certificate_id)
                certificate = response['Certificate']

                # Format certificate preview (first 100 chars)
                cert_preview = certificate.get('Certificate', '')[:100] + '...' if certificate.get('Certificate') else 'N/A'

                # Format dates
                active_date = 'N/A'
                inactive_date = 'N/A'
                if certificate.get('ActiveDate'):
                    active_date = certificate['ActiveDate'].strftime('%Y-%m-%d %H:%M:%S UTC')
                if certificate.get('InactiveDate'):
                    inactive_date = certificate['InactiveDate'].strftime('%Y-%m-%d %H:%M:%S UTC')

                certificate_info = {
                    'Certificate ID': certificate_id,
                    'Region': region,
                    'ARN': certificate.get('Arn', 'N/A'),
                    'Status': certificate.get('Status', 'N/A'),
                    'Usage': certificate.get('Usage', 'N/A'),
                    'Type': certificate.get('Type', 'N/A'),
                    'Active Date': active_date,
                    'Inactive Date': inactive_date,
                    'Certificate Preview': cert_preview,
                    'Description': certificate.get('Description', 'None'),
                    'Tags': format_list_field([f"{tag['Key']}={tag['Value']}" for tag in certificate.get('Tags', [])])
                }

                certificates_data.append(certificate_info)

            except Exception as e:
                utils.log_error(f"Error collecting certificate {certificate_id} in {region}: {e}")
                continue

        utils.log_success(f"Collected {len(certificates_data)} certificates from {region}")

    except Exception as e:
        utils.log_error(f"Error listing certificates in {region}: {e}")

    return certificates_data


@utils.aws_error_handler("Collecting Transfer Family agreements", default_return=[])
def collect_transfer_agreements(region: str, server_ids: list[str]) -> list[dict[str, Any]]:
    """
    Collect Transfer Family agreement information for all servers in a region.

    Args:
        region: AWS region name
        server_ids: List of server IDs to collect agreements from

    Returns:
        List of dictionaries containing agreement information
    """
    if not server_ids:
        return []

    utils.log_info(f"Collecting Transfer Family agreements in {region}...")

    transfer_client = utils.get_boto3_client('transfer', region_name=region)
    agreements_data = []

    for server_id in server_ids:
        try:
            # List agreements for this server
            paginator = transfer_client.get_paginator('list_agreements')
            agreement_ids = []

            for page in paginator.paginate(ServerId=server_id):
                for agreement in page.get('Agreements', []):
                    agreement_ids.append(agreement['AgreementId'])

            if not agreement_ids:
                continue

            utils.log_info(f"Found {len(agreement_ids)} agreements on server {server_id}")

            # Get detailed information for each agreement
            for agreement_id in agreement_ids:
                try:
                    response = transfer_client.describe_agreement(
                        ServerId=server_id,
                        AgreementId=agreement_id
                    )
                    agreement = response['Agreement']

                    agreement_info = {
                        'Agreement ID': agreement_id,
                        'Server ID': server_id,
                        'Region': region,
                        'ARN': agreement.get('Arn', 'N/A'),
                        'Status': agreement.get('Status', 'N/A'),
                        'Local Profile ID': agreement.get('LocalProfileId', 'N/A'),
                        'Partner Profile ID': agreement.get('PartnerProfileId', 'N/A'),
                        'Base Directory': agreement.get('BaseDirectory', 'N/A'),
                        'Access Role ARN': agreement.get('AccessRole', 'N/A'),
                        'Description': agreement.get('Description', 'None'),
                        'Tags': format_list_field([f"{tag['Key']}={tag['Value']}" for tag in agreement.get('Tags', [])])
                    }

                    agreements_data.append(agreement_info)

                except Exception as e:
                    utils.log_error(f"Error collecting agreement {agreement_id} on server {server_id}: {e}")
                    continue

        except Exception as e:
            utils.log_error(f"Error listing agreements for server {server_id}: {e}")
            continue

    utils.log_success(f"Collected {len(agreements_data)} agreements from {region}")
    return agreements_data


def export_to_excel(
    servers_data: list[dict[str, Any]],
    users_data: list[dict[str, Any]],
    connectors_data: list[dict[str, Any]],
    workflows_data: list[dict[str, Any]],
    certificates_data: list[dict[str, Any]],
    agreements_data: list[dict[str, Any]],
    account_name: str
) -> str:
    """
    Export comprehensive Transfer Family data to Excel file.

    Args:
        servers_data: List of server dictionaries
        users_data: List of user dictionaries
        connectors_data: List of connector dictionaries
        workflows_data: List of workflow dictionaries
        certificates_data: List of certificate dictionaries
        agreements_data: List of agreement dictionaries
        account_name: AWS account name

    Returns:
        Path to exported file or None if export failed
    """
    if not any([servers_data, users_data, connectors_data, workflows_data, certificates_data, agreements_data]):
        utils.log_warning("No Transfer Family data to export.")
        return None

    try:
        import pandas as pd

        # Generate filename
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        filename = utils.create_export_filename(
            account_name,
            "transfer-family",
            "all",
            current_date
        )

        # Create data frames for multi-sheet export
        data_frames = {}

        # Create summary sheet
        summary_data = {
            'Category': [
                'Total Servers',
                'Servers - ONLINE',
                'Servers - OFFLINE',
                'Servers - SFTP Protocol',
                'Servers - FTPS Protocol',
                'Servers - FTP Protocol',
                'Servers - AS2 Protocol',
                'Servers - PUBLIC Endpoint',
                'Servers - VPC Endpoint',
                'Servers - S3 Domain',
                'Servers - EFS Domain',
                '',
                'Total Users',
                'Total Connectors',
                'Connectors - AS2',
                'Connectors - SFTP',
                '',
                'Total Workflows',
                'Total Certificates',
                'Certificates - ACTIVE',
                'Certificates - SIGNING Usage',
                'Certificates - ENCRYPTION Usage',
                '',
                'Total Agreements',
                'Agreements - ACTIVE'
            ],
            'Count': [
                len(servers_data),
                len([s for s in servers_data if s.get('State') == 'ONLINE']),
                len([s for s in servers_data if s.get('State') == 'OFFLINE']),
                len([s for s in servers_data if 'SFTP' in s.get('Protocols', '')]),
                len([s for s in servers_data if 'FTPS' in s.get('Protocols', '')]),
                len([s for s in servers_data if 'FTP' in s.get('Protocols', '') and 'SFTP' not in s.get('Protocols', '')]),
                len([s for s in servers_data if 'AS2' in s.get('Protocols', '')]),
                len([s for s in servers_data if s.get('Endpoint Type') == 'PUBLIC']),
                len([s for s in servers_data if s.get('Endpoint Type') in ['VPC', 'VPC_ENDPOINT']]),
                len([s for s in servers_data if s.get('Domain') == 'S3']),
                len([s for s in servers_data if s.get('Domain') == 'EFS']),
                '',
                len(users_data),
                len(connectors_data),
                len([c for c in connectors_data if c.get('Type') == 'AS2']),
                len([c for c in connectors_data if c.get('Type') == 'SFTP']),
                '',
                len(workflows_data),
                len(certificates_data),
                len([c for c in certificates_data if c.get('Status') == 'ACTIVE']),
                len([c for c in certificates_data if c.get('Usage') == 'SIGNING']),
                len([c for c in certificates_data if c.get('Usage') == 'ENCRYPTION']),
                '',
                len(agreements_data),
                len([a for a in agreements_data if a.get('Status') == 'ACTIVE'])
            ]
        }

        summary_df = pd.DataFrame(summary_data)
        data_frames['Summary'] = summary_df

        # Add Servers sheet
        if servers_data:
            servers_df = pd.DataFrame(servers_data)
            servers_df = utils.prepare_dataframe_for_export(servers_df)
            data_frames['Servers'] = servers_df

        # Add Users sheet (with sanitization for security-sensitive data)
        if users_data:
            users_df = pd.DataFrame(users_data)
            users_df = utils.sanitize_for_export(utils.prepare_dataframe_for_export(users_df))
            data_frames['Users'] = users_df

        # Add Connectors sheet
        if connectors_data:
            connectors_df = pd.DataFrame(connectors_data)
            connectors_df = utils.prepare_dataframe_for_export(connectors_df)
            data_frames['Connectors'] = connectors_df

        # Add Workflows sheet
        if workflows_data:
            workflows_df = pd.DataFrame(workflows_data)
            workflows_df = utils.prepare_dataframe_for_export(workflows_df)
            data_frames['Workflows'] = workflows_df

        # Add Certificates sheet
        if certificates_data:
            certificates_df = pd.DataFrame(certificates_data)
            certificates_df = utils.prepare_dataframe_for_export(certificates_df)
            data_frames['Certificates'] = certificates_df

        # Add Agreements sheet
        if agreements_data:
            agreements_df = pd.DataFrame(agreements_data)
            agreements_df = utils.prepare_dataframe_for_export(agreements_df)
            data_frames['Agreements'] = agreements_df

        # Add Active Servers sheet (filtered)
        if servers_data:
            active_servers = [s for s in servers_data if s.get('State') == 'ONLINE']
            if active_servers:
                active_servers_df = pd.DataFrame(active_servers)
                active_servers_df = utils.prepare_dataframe_for_export(active_servers_df)
                data_frames['Active Servers'] = active_servers_df

        # Save using utils function for multi-sheet Excel
        output_path = utils.save_multiple_dataframes_to_excel(data_frames, filename)

        if output_path:
            utils.log_success("Transfer Family data exported successfully!")
            utils.log_success(f"File location: {output_path}")
            utils.log_info("Export contains:")
            utils.log_info(f"  - {len(servers_data)} servers")
            utils.log_info(f"  - {len(users_data)} users")
            utils.log_info(f"  - {len(connectors_data)} connectors")
            utils.log_info(f"  - {len(workflows_data)} workflows")
            utils.log_info(f"  - {len(certificates_data)} certificates")
            utils.log_info(f"  - {len(agreements_data)} agreements")
            return str(output_path)
        else:
            utils.log_error("Error exporting to Excel. Please check the logs.")
            return None

    except Exception as e:
        utils.log_error("Error exporting to Excel", e)
        return None


def main():
    """Main function to orchestrate Transfer Family information collection."""
    try:
        # Setup logging
        utils.setup_logging("transfer-family-export")

        # Check dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            return

        # Import pandas after dependency check

        # Print title and get account info
        account_id, account_name = utils.print_script_banner("AWS TRANSFER FAMILY COMPREHENSIVE EXPORT")

        # Validate AWS credentials
        is_valid, validated_account_id, error_message = utils.validate_aws_credentials()
        if not is_valid:
            utils.log_error(f"AWS credentials validation failed: {error_message}")
            print("\nPlease configure your credentials using:")
            print("  - AWS CLI: aws configure")
            print("  - Environment variables: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            print("  - IAM role (if running on EC2)")
            return

        utils.log_success("AWS credentials validated")

        # Prompt for region selection
        # Detect partition for region examples
        regions = utils.prompt_region_selection()

        # STEP 1: Collect servers (primary scope — region failures must
        # propagate as failed_regions, never collapse into "empty"). See
        # .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md.
        all_servers, failed_regions = collect_transfer_servers_all_regions(regions)

        # Group server IDs by region for the per-region enrichment steps
        # below (users, agreements). Built from our own row dicts, so
        # ``.get()`` is used defensively rather than a hard subscript.
        servers_by_region: dict[str, list[str]] = {}
        for s in all_servers:
            servers_by_region.setdefault(s.get('Region', ''), []).append(s.get('Server ID', ''))

        all_users = []
        all_connectors = []
        all_workflows = []
        all_certificates = []
        all_agreements = []

        for region in regions:
            utils.log_info(f"\nProcessing region: {region}")
            utils.log_info("=" * 60)

            server_ids = [sid for sid in servers_by_region.get(region, []) if sid]

            # Collect users for each server (enrichment — degrades
            # gracefully; a failure here does not fail the whole export)
            if server_ids:
                users = collect_transfer_users(region, server_ids)
                all_users.extend(users)

            # Collect connectors
            connectors = collect_transfer_connectors(region)
            all_connectors.extend(connectors)

            # Collect workflows
            workflows = collect_transfer_workflows(region)
            all_workflows.extend(workflows)

            # Collect certificates
            certificates = collect_transfer_certificates(region)
            all_certificates.extend(certificates)

            # Collect agreements for each server
            if server_ids:
                agreements = collect_transfer_agreements(region, server_ids)
                all_agreements.extend(agreements)

        print("\n====================================================================")
        print("COLLECTION COMPLETE")
        print("====================================================================")

        # Display summary
        utils.log_info(f"Total servers collected: {len(all_servers)}")
        utils.log_info(f"Total users collected: {len(all_users)}")
        utils.log_info(f"Total connectors collected: {len(all_connectors)}")
        utils.log_info(f"Total workflows collected: {len(all_workflows)}")
        utils.log_info(f"Total certificates collected: {len(all_certificates)}")
        utils.log_info(f"Total agreements collected: {len(all_agreements)}")

        # Export whatever succeeded first — a partial export is required even
        # when some regions failed (see the silent-collection-failure
        # blast-radius audit).
        if any([all_servers, all_users, all_connectors, all_workflows, all_certificates, all_agreements]):
            # Export to Excel
            print("\n====================================================================")
            utils.log_info("Exporting data to Excel...")
            print("====================================================================\n")

            filename = export_to_excel(
                all_servers,
                all_users,
                all_connectors,
                all_workflows,
                all_certificates,
                all_agreements,
                account_name
            )

            if filename:
                print("\n====================================================================")
                print("EXPORT COMPLETE")
                print("====================================================================")
                utils.log_success("Transfer Family export completed successfully!")
                utils.log_info(f"Output file: {filename}")
            else:
                utils.log_error("Export failed. Please check the logs.")
        elif not failed_regions:
            # Genuinely empty account: every region succeeded and returned nothing.
            utils.log_warning("No Transfer Family resources found in selected regions.")
            print("\nNo Transfer Family resources found in the selected region(s).")

        # If ANY region failed the Servers scope collection, make it loud:
        # write a marker and exit non-zero, even if some data was exported. A
        # partial export that looks complete is exactly the failure mode this
        # guards against.
        if failed_regions:
            utils.report_collection_failures(account_name, 'transfer-family', failed_regions)
            print(
                "\nERROR: Transfer Family export completed with failures — data is incomplete. "
                "See the *-transfer-family-FAILED-*.txt marker in the output directory."
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
