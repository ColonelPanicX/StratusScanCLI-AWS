#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Security Hub Information Collection Script
Date: SEP-25-2025

Description:
This script collects comprehensive Security Hub findings information from AWS
environments including severity levels, compliance status, remediation guidance, and
affected resources. The data is exported to an Excel spreadsheet with AWS-specific
naming convention for security monitoring and compliance reporting.

Collected information includes: Security findings, severity levels, compliance standards,
remediation steps, affected resources, and finding history for comprehensive security analysis.
"""

import datetime
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
args = utils.parse_script_args("Export Security Hub findings and standards to Excel")

# Setup logging
logger = utils.setup_logging('security-hub-export')
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

def get_available_regions():
    """
    Get available regions for Security Hub in AWS.

    Returns:
        list: List of available regions
    """
    # Get regions from config or use defaults
    _, config = utils.get_config()
    default_regions = config.get('default_regions', ['us-east-1', 'us-west-2'])

    # Check for resource preferences for Security Hub
    resource_prefs = config.get('resource_preferences', {})
    securityhub_prefs = resource_prefs.get('security_hub', {})
    aws_regions = securityhub_prefs.get('regions', default_regions)

    available_regions = []

    for region in aws_regions:
        # Validate region is a real AWS region
        if not utils.is_aws_region(region):
            utils.log_warning(f"Skipping invalid region: {region}")
            continue

        try:
            # Test Security Hub availability
            client = utils.get_boto3_client('securityhub', region_name=region)
            client.describe_hub()
            available_regions.append(region)
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code != 'InvalidAccessException':  # Hub might not be enabled but service is available
                available_regions.append(region)
        except Exception:
            # Skip regions where Security Hub is not available
            continue

    return available_regions if available_regions else aws_regions  # Fallback to configured regions

def _build_finding_row(finding, region):
    """Build a single Security Hub finding export row from a get_findings item."""
    remediation = extract_remediation_info(finding)
    resources = extract_resource_info(finding)
    compliance = extract_compliance_info(finding)

    return {
        'Region': region,
        'Finding ID': finding.get('Id', 'N/A'),
        'Product ARN': finding.get('ProductArn', 'N/A'),
        'Product Name': extract_product_name(finding.get('ProductArn', '')),
        'Company Name': finding.get('CompanyName', 'N/A'),
        'Title': finding.get('Title', 'N/A'),
        'Description': finding.get('Description', 'N/A'),
        'Severity Label': finding.get('Severity', {}).get('Label', 'N/A'),
        'Severity Score': finding.get('Severity', {}).get('Normalized', 'N/A'),
        'Confidence': finding.get('Confidence', 'N/A'),
        'Criticality': finding.get('Criticality', 'N/A'),
        'Workflow Status': finding.get('Workflow', {}).get('Status', 'N/A'),
        'Record State': finding.get('RecordState', 'N/A'),
        'Compliance Status': compliance['status'],
        'Compliance Standards': compliance['standards'],
        'Affected Resources': resources['summary'],
        'Resource Types': resources['types'],
        'Remediation Available': 'Yes' if remediation['available'] else 'No',
        'Remediation URL': remediation['url'],
        'Remediation Text': remediation['text'],
        'Source URL': finding.get('SourceUrl', 'N/A'),
        'First Observed': finding.get('FirstObservedAt', 'N/A'),
        'Last Observed': finding.get('LastObservedAt', 'N/A'),
        'Created At': finding.get('CreatedAt', 'N/A'),
        'Updated At': finding.get('UpdatedAt', 'N/A'),
        'Note': extract_note_info(finding),
    }


def collect_security_hub_findings(region):
    """
    Collect Security Hub findings from a specific region.

    This is the primary scope collector. A real API error (throttling, access
    denied, etc.) is allowed to raise so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the region
    as failed instead of silently reporting "no findings" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Security Hub not being enabled in a region (``InvalidAccessException`` /
    ``SubscriptionRequiredException``) is a legitimate, non-failure state and
    is treated as a normal skip, not a failure.

    Individual malformed findings are skipped (logged) rather than aborting
    the whole region.

    Args:
        region: AWS region to collect findings from

    Returns:
        tuple: (findings_data, cap_reached)

    Raises:
        ClientError: Any Security Hub API error other than "not enabled".
    """
    findings_data = []
    cap_reached = False

    client = utils.get_boto3_client('securityhub', region_name=region)

    # Check if Security Hub is enabled. "Not enabled" is a legitimate skip,
    # not a failure — every other error must propagate to the caller.
    try:
        client.describe_hub()
        utils.log_success(f"Security Hub is enabled in {region}")
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code in ('InvalidAccessException', 'SubscriptionRequiredException'):
            utils.log_warning(f"Security Hub is not enabled in {region}. Skipping this region.")
            return [], False
        raise

    # Get findings using pagination
    paginator = client.get_paginator('get_findings')

    # Exclude INFORMATIONAL — too noisy for audits; filter at API level
    filters = {
        'WorkflowStatus': [
            {'Value': 'NEW', 'Comparison': 'EQUALS'},
            {'Value': 'NOTIFIED', 'Comparison': 'EQUALS'},
        ],
        'RecordState': [
            {'Value': 'ACTIVE', 'Comparison': 'EQUALS'},
        ],
        'SeverityLabel': [
            {'Value': 'CRITICAL', 'Comparison': 'EQUALS'},
            {'Value': 'HIGH', 'Comparison': 'EQUALS'},
            {'Value': 'MEDIUM', 'Comparison': 'EQUALS'},
            {'Value': 'LOW', 'Comparison': 'EQUALS'},
        ],
    }

    FINDINGS_CAP = 10_000
    processed = 0

    for page in paginator.paginate(Filters=filters, MaxResults=100):
        for finding in page.get('Findings', []):
            if processed >= FINDINGS_CAP:
                cap_reached = True
                break

            try:
                findings_data.append(_build_finding_row(finding, region))
            except Exception as e:
                # One malformed finding is skipped, not fatal to the region.
                utils.log_error(
                    f"Skipping malformed Security Hub finding in {region}: "
                    f"{finding.get('Id', '<unknown>')}",
                    e,
                )
                continue

            processed += 1
            if processed % 500 == 0:
                utils.log_info(f"Processed {processed} findings in {region}...")

        if cap_reached:
            utils.log_warning(
                f"Finding cap of {FINDINGS_CAP:,} reached in {region} — export truncated. "
                "Re-run with a severity filter or split by region to get remaining findings."
            )
            break

    return findings_data, cap_reached

def extract_product_name(product_arn):
    """Extract readable product name from product ARN."""
    if not product_arn or product_arn == 'N/A':
        return 'N/A'

    try:
        # Extract product name from ARN
        # ARN format: arn:aws:securityhub:region::product/company/product-name
        parts = product_arn.split('/')
        if len(parts) >= 3:
            company = parts[-2]
            product = parts[-1]
            return f"{company}/{product}"
        else:
            return product_arn.split('/')[-1]
    except Exception:
        return 'Unknown'

def extract_remediation_info(finding):
    """Extract remediation information from finding."""
    remediation_info = {
        'available': False,
        'url': 'N/A',
        'text': 'N/A'
    }

    try:
        remediation = finding.get('Remediation', {})
        if remediation:
            remediation_info['available'] = True

            # Get remediation URL
            recommendation = remediation.get('Recommendation', {})
            if recommendation.get('Url'):
                remediation_info['url'] = recommendation['Url']

            # Get remediation text
            if recommendation.get('Text'):
                remediation_info['text'] = recommendation['Text'][:500]  # Limit length

    except Exception:
        pass

    return remediation_info

def extract_resource_info(finding):
    """Extract resource information from finding."""
    resource_info = {
        'summary': 'N/A',
        'types': 'N/A'
    }

    try:
        resources = finding.get('Resources', [])
        if resources:
            resource_list = []
            resource_types = set()

            for resource in resources[:10]:  # Limit to first 10 resources
                resource_id = resource.get('Id', 'Unknown')
                resource_type = resource.get('Type', 'Unknown')
                resource_types.add(resource_type)

                # Shorten resource ID for display
                if len(resource_id) > 40:
                    resource_id = resource_id[:37] + '...'

                resource_list.append(f"{resource_type}: {resource_id}")

            resource_info['summary'] = '; '.join(resource_list)
            resource_info['types'] = ', '.join(sorted(resource_types))

            if len(resources) > 10:
                resource_info['summary'] += f" (and {len(resources) - 10} more)"

    except Exception:
        pass

    return resource_info

def extract_compliance_info(finding):
    """Extract compliance information from finding."""
    compliance_info = {
        'status': 'N/A',
        'standards': 'N/A'
    }

    try:
        compliance = finding.get('Compliance', {})
        if compliance:
            # Get compliance status
            status = compliance.get('Status')
            if status:
                compliance_info['status'] = status

            # Get associated standards
            associated_standards = compliance.get('AssociatedStandards', [])
            if associated_standards:
                standards_list = []
                for standard in associated_standards[:5]:  # Limit to first 5
                    standard_id = standard.get('StandardsId', 'Unknown')
                    standards_list.append(standard_id)

                compliance_info['standards'] = ', '.join(standards_list)

                if len(associated_standards) > 5:
                    compliance_info['standards'] += f" (and {len(associated_standards) - 5} more)"

    except Exception:
        pass

    return compliance_info

def extract_note_info(finding):
    """Extract note information from finding."""
    try:
        note = finding.get('Note', {})
        if note and note.get('Text'):
            note_text = note['Text']
            updated_by = note.get('UpdatedBy', 'Unknown')
            updated_at = note.get('UpdatedAt', 'Unknown')
            return f"Note by {updated_by} at {updated_at}: {note_text[:200]}"
        return 'N/A'
    except Exception:
        return 'N/A'

def export_to_excel(all_findings_data, account_id, account_name, truncated=False):
    """
    Export Security Hub findings data to Excel file with AWS naming convention.

    Args:
        all_findings_data: List of finding information dictionaries from all regions
        account_id: AWS account ID
        account_name: AWS account name

    Returns:
        str: Filename of exported file or None if failed
    """
    if not all_findings_data:
        utils.log_warning("No Security Hub findings data to export.")
        return None

    try:
        # Import pandas after dependency check
        import pandas as pd

        # Generate filename with AWS identifier
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")

        # Use utils module to generate filename and save data with AWS identifier
        filename = utils.create_export_filename(
            account_name,
            "security-hub",
            "",
            current_date
        )

        # Create data frames for multi-sheet export
        data_frames = {}

        # Main findings sheet
        findings_df = pd.DataFrame(all_findings_data)
        data_frames['Security Hub Findings'] = findings_df

        # Summary by severity
        if all_findings_data:
            severity_counts = (findings_df['Severity Label']
                             .value_counts()
                             .rename_axis('Severity Level')
                             .reset_index(name='Count'))
            data_frames['Summary by Severity'] = severity_counts

            # Summary by compliance status
            compliance_counts = (findings_df['Compliance Status']
                               .value_counts()
                               .rename_axis('Compliance Status')
                               .reset_index(name='Count'))
            data_frames['Summary by Compliance'] = compliance_counts

            # Summary by product
            product_counts = (findings_df['Product Name']
                            .value_counts()
                            .rename_axis('Product Name')
                            .reset_index(name='Count'))
            data_frames['Summary by Product'] = product_counts

            # High/Critical findings
            high_critical_df = findings_df[
                findings_df['Severity Label'].isin(['HIGH', 'CRITICAL'])
            ].copy()
            if not high_critical_df.empty:
                data_frames['High & Critical Findings'] = high_critical_df

        # Create overall summary data
        metrics = [
            'Total Findings (exported)',
            'Critical Findings',
            'High Findings',
            'Medium Findings',
            'Low Findings',
            'Failed Compliance',
            'Passed Compliance',
            'Unique Products',
            'Findings with Remediation',
        ]
        counts = [
            len(all_findings_data),
            len([f for f in all_findings_data if f.get('Severity Label') == 'CRITICAL']),
            len([f for f in all_findings_data if f.get('Severity Label') == 'HIGH']),
            len([f for f in all_findings_data if f.get('Severity Label') == 'MEDIUM']),
            len([f for f in all_findings_data if f.get('Severity Label') == 'LOW']),
            len([f for f in all_findings_data if f.get('Compliance Status') == 'FAILED']),
            len([f for f in all_findings_data if f.get('Compliance Status') == 'PASSED']),
            len({f.get('Product Name') for f in all_findings_data if f.get('Product Name', 'N/A') != 'N/A'}),
            len([f for f in all_findings_data if f.get('Remediation Available') == 'Yes']),
        ]

        if truncated:
            metrics.append('NOTICE')
            counts.append(
                'Export truncated at 10,000 findings. INFORMATIONAL severity excluded. '
                'Re-run with a narrower region or severity filter to capture remaining findings.'
            )

        summary_df = pd.DataFrame({'Metric': metrics, 'Count': counts})
        data_frames['Overall Summary'] = summary_df

        # Save using utils function for multi-sheet Excel
        output_path = utils.save_multiple_dataframes_to_excel(data_frames, filename)

        if output_path:
            utils.log_success("AWS Security Hub data exported successfully!")
            utils.log_success(f"File location: {output_path}")

            # Log summary statistics
            total_findings = len(all_findings_data)
            critical_high = len([f for f in all_findings_data if f.get('Severity Label') in ['CRITICAL', 'HIGH']])
            utils.log_info(f"Export contains {total_findings} total findings ({critical_high} critical/high severity)")

            return str(output_path)
        else:
            utils.log_error("Error exporting to Excel. Please check the logs.")
            return None

    except Exception as e:
        utils.log_error("Error exporting to Excel", e)
        return None

def main():
    """
    Main function to orchestrate the Security Hub information collection.
    """
    try:
        # Check dependencies first
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            return

        # Import pandas after dependency check

        # Print title and get account info
        account_id, account_name = utils.print_script_banner("AWS SECURITY HUB INFORMATION COLLECTION EXPORT")

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

        utils.log_info("Starting Security Hub information collection from AWS...")
        print("====================================================================")

        # Get available regions
        available_regions = get_available_regions()

        if not available_regions:
            utils.log_error("No regions available for Security Hub. Exiting.")
            return

        utils.log_info(f"Will scan Security Hub in regions: {', '.join(available_regions)}")

        # Collect findings from all available regions using concurrent scanning.
        # Primary scope collection — region failures must propagate as
        # failed_regions, never collapse into "empty" (see
        # .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
        print("\n=== COLLECTING SECURITY HUB FINDINGS ===")
        results, failed_regions = utils.scan_regions_concurrent(
            available_regions, collect_security_hub_findings, collect_failures=True
        )
        all_findings_data = []
        any_truncated = False
        for findings, cap_reached in results:
            all_findings_data.extend(findings)
            if cap_reached:
                any_truncated = True
        utils.log_success(f"Total Security Hub findings collected: {len(all_findings_data)}")

        if not all_findings_data and not failed_regions:
            # Genuinely empty: every region succeeded (or legitimately had
            # Security Hub disabled) and returned nothing.
            utils.log_warning("No Security Hub findings collected from any region. Exiting.")
            return

        if all_findings_data:
            print("\n====================================================================")
            print("COLLECTION COMPLETE")
            print("====================================================================")

            # Export to Excel
            filename = export_to_excel(all_findings_data, account_id, account_name, truncated=any_truncated)

            if filename:
                utils.log_info("Results exported with AWS compliance markers")
                utils.log_info(f"Total findings processed: {len(all_findings_data)}")

                # Display summary statistics
                critical_high = len([f for f in all_findings_data if f.get('Severity Label') in ['CRITICAL', 'HIGH']])
                failed_compliance = len([f for f in all_findings_data if f.get('Compliance Status') == 'FAILED'])
                with_remediation = len([f for f in all_findings_data if f.get('Remediation Available') == 'Yes'])

                utils.log_info(f"Critical/High severity findings: {critical_high}")
                utils.log_info(f"Failed compliance findings: {failed_compliance}")
                utils.log_info(f"Findings with remediation guidance: {with_remediation}")

                print("\nScript execution completed.")
            else:
                utils.log_error("Export failed. Please check the logs.")

        # If ANY region failed the primary findings-collection scope, make it
        # loud: write a marker and exit non-zero, even if some data was
        # exported. A partial export that looks complete is exactly the
        # failure mode this guards against.
        if failed_regions:
            utils.report_collection_failures(account_name, 'security-hub', failed_regions)
            print(
                "\nERROR: Security Hub export completed with failures — data is incomplete. "
                "See the *-security-hub-FAILED-*.txt marker in the output directory."
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
