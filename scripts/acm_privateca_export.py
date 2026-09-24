#!/usr/bin/env python3
"""
ACM Private CA Export Script for StratusScan

Exports comprehensive AWS Certificate Manager Private Certificate Authority information including:
- Private Certificate Authorities with configuration details
- Certificate revocation list (CRL) configuration
- CA permissions

Not collected: issued certificates (ACM-PCA has no list API) and audit reports.

Output: Multi-worksheet Excel file with ACM Private CA resources
"""

import json
import sys
from pathlib import Path
from typing import Any

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export ACM Private Certificate Authority resources to Excel")

def _build_ca_row(item: dict[str, Any], region: str) -> dict[str, Any]:
    """
    Build a single Private CA export row from a ``list_certificate_authorities``
    entry.

    ``ListCertificateAuthorities`` already returns the full ``CertificateAuthority``
    shape (same fields as ``DescribeCertificateAuthority``), so no secondary
    per-item describe call is needed. Every field is read with ``.get()`` and a
    safe default so a malformed/partial entry raises predictably and is caught by
    the caller rather than aborting the whole region.
    """
    ca_arn = item.get('Arn', 'N/A')
    ca_type = item.get('Type', 'N/A')
    status = item.get('Status', 'N/A')

    config = item.get('CertificateAuthorityConfiguration', {}) or {}
    key_algorithm = config.get('KeyAlgorithm', 'N/A')
    signing_algorithm = config.get('SigningAlgorithm', 'N/A')

    # Subject information
    subject = config.get('Subject', {}) or {}
    common_name = subject.get('CommonName', 'N/A')
    organization = subject.get('Organization', 'N/A')
    organizational_unit = subject.get('OrganizationalUnit', 'N/A')
    country = subject.get('Country', 'N/A')
    state = subject.get('State', 'N/A')
    locality = subject.get('Locality', 'N/A')

    # Dates
    created_at = item.get('CreatedAt', 'N/A')
    if created_at != 'N/A':
        created_at = created_at.strftime('%Y-%m-%d %H:%M:%S')

    not_before = item.get('NotBefore', 'N/A')
    if not_before != 'N/A':
        not_before = not_before.strftime('%Y-%m-%d %H:%M:%S')

    not_after = item.get('NotAfter', 'N/A')
    if not_after != 'N/A':
        not_after = not_after.strftime('%Y-%m-%d %H:%M:%S')

    last_state_change = item.get('LastStateChangeAt', 'N/A')
    if last_state_change != 'N/A':
        last_state_change = last_state_change.strftime('%Y-%m-%d %H:%M:%S')

    # Revocation configuration
    revocation_config = item.get('RevocationConfiguration', {}) or {}
    crl_config = revocation_config.get('CrlConfiguration', {}) or {}
    crl_enabled = crl_config.get('Enabled', False)
    crl_s3_bucket = crl_config.get('S3BucketName', 'N/A')
    crl_s3_object_acl = crl_config.get('S3ObjectAcl', 'N/A')
    crl_expiration_days = crl_config.get('ExpirationInDays', 'N/A')

    ocsp_config = revocation_config.get('OcspConfiguration', {}) or {}
    ocsp_enabled = ocsp_config.get('Enabled', False)
    ocsp_custom_cname = ocsp_config.get('OcspCustomCname', 'N/A')

    # Key storage security standard
    key_storage = item.get('KeyStorageSecurityStandard', 'N/A')

    # Usage mode
    usage_mode = item.get('UsageMode', 'N/A')

    # Owner account
    owner_account = item.get('OwnerAccount', 'N/A')

    # Failure reason
    failure_reason = item.get('FailureReason', 'N/A')

    # Serial number
    serial = item.get('Serial', 'N/A')

    return {
        'Region': region,
        'CA ARN': ca_arn,
        'Type': ca_type,
        'Status': status,
        'Common Name': common_name,
        'Organization': organization,
        'Organizational Unit': organizational_unit,
        'Country': country,
        'State': state,
        'Locality': locality,
        'Key Algorithm': key_algorithm,
        'Signing Algorithm': signing_algorithm,
        'Key Storage Security Standard': key_storage,
        'Usage Mode': usage_mode,
        'Serial Number': serial,
        'Created At': created_at,
        'Not Before': not_before,
        'Not After': not_after,
        'Last State Change': last_state_change,
        'CRL Enabled': crl_enabled,
        'CRL S3 Bucket': crl_s3_bucket,
        'CRL S3 Object ACL': crl_s3_object_acl,
        'CRL Expiration Days': crl_expiration_days,
        'OCSP Enabled': ocsp_enabled,
        'OCSP Custom CNAME': ocsp_custom_cname,
        'Owner Account': owner_account,
        'Failure Reason': failure_reason,
        'Tags': 'N/A',
    }


def _scan_private_cas_region(region: str) -> list[dict[str, Any]]:
    """
    Scan Private CAs in a single region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the region
    as failed instead of silently reporting "no Private CAs" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed CAs are skipped (logged) rather than aborting the
    whole region. Tag lookup is best-effort enrichment: a failure there does
    not drop the CA row, it just leaves ``Tags`` as ``'N/A'``.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    regional_cas = []
    acmpca_client = utils.get_boto3_client('acm-pca', region_name=region)

    paginator = acmpca_client.get_paginator('list_certificate_authorities')
    for page in paginator.paginate():
        cas = page.get('CertificateAuthorities', [])

        for ca in cas:
            ca_arn = ca.get('Arn', '<unknown>')

            try:
                row = _build_ca_row(ca, region)
            except Exception as e:
                utils.log_warning(f"Could not process CA {ca_arn} in {region}: {str(e)}")
                continue

            # Get tags (best-effort — no policy/tags is normal, don't fail the row)
            try:
                tags_response = acmpca_client.list_tags(
                    CertificateAuthorityArn=ca_arn
                )
                tags = tags_response.get('Tags', [])
                if tags:
                    row['Tags'] = ', '.join(
                        f"{tag.get('Key')}={tag.get('Value')}" for tag in tags
                    )
            except Exception:
                pass

            regional_cas.append(row)

    return regional_cas


def collect_private_cas(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect ACM Private CA certificate authority information from AWS regions,
    surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(cas, failed_regions)`` where ``failed_regions`` is a list of
        ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING PRIVATE CERTIFICATE AUTHORITIES ===")
    results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_private_cas_region,
        show_progress=True,
        collect_failures=True,
    )
    all_cas = [ca for result in results for ca in result]
    utils.log_success(f"Total Private CAs collected: {len(all_cas)}")
    return all_cas, failed_regions


@utils.aws_error_handler("Collecting issued certificates", default_return=[])
def collect_issued_certificates(regions: list[str]) -> list[dict[str, Any]]:
    """Collect issued certificates from Private CAs.

    NOTE: ACM Private CA exposes no API to enumerate the certificates a CA has
    issued — there is no ``list_certificates`` operation. The only supported way
    to obtain issued-certificate inventory is an asynchronous audit report
    (``CreateCertificateAuthorityAuditReport``) delivered to a caller-supplied S3
    bucket, which requires write access and is out of scope for this read-only
    export. This collector therefore returns nothing; CA-level detail is captured
    by the Private CA collector instead.
    """
    print("\n=== COLLECTING ISSUED CERTIFICATES ===")
    utils.log_warning(
        "ACM-PCA has no API to list issued certificates; skipping this sheet. "
        "Use CreateCertificateAuthorityAuditReport for issued-certificate inventory."
    )
    return []



def _scan_ca_permissions_region(region: str) -> list[dict[str, Any]]:
    """Scan CA permissions in a single region."""
    regional_permissions = []
    acmpca_client = utils.get_boto3_client('acm-pca', region_name=region)

    try:
        # First get all CAs
        ca_paginator = acmpca_client.get_paginator('list_certificate_authorities')
        for ca_page in ca_paginator.paginate():
            cas = ca_page.get('CertificateAuthorities', [])

            for ca in cas:
                ca_arn = ca.get('Arn', 'N/A')

                try:
                    # Get policy for this CA
                    policy_response = acmpca_client.get_policy(
                        ResourceArn=ca_arn
                    )
                    policy_str = policy_response.get('Policy', 'N/A')

                    if policy_str != 'N/A':
                        try:
                            policy_json = json.loads(policy_str)
                            statements = policy_json.get('Statement', [])

                            for idx, statement in enumerate(statements):
                                sid = statement.get('Sid', f'Statement{idx}')
                                effect = statement.get('Effect', 'N/A')
                                principal = statement.get('Principal', {})

                                # Extract principal information
                                if isinstance(principal, dict):
                                    service = principal.get('Service', 'N/A')
                                    aws = principal.get('AWS', 'N/A')
                                    if isinstance(service, list):
                                        service = ', '.join(service)
                                    if isinstance(aws, list):
                                        aws = ', '.join(aws)
                                    principal_str = f"Service: {service}, AWS: {aws}"
                                else:
                                    principal_str = str(principal)

                                # Extract actions
                                actions = statement.get('Action', [])
                                if isinstance(actions, str):
                                    actions = [actions]
                                actions_str = ', '.join(actions)

                                # Extract conditions
                                conditions = statement.get('Condition', {})
                                conditions_str = json.dumps(conditions) if conditions else 'None'

                                regional_permissions.append({
                                    'Region': region,
                                    'CA ARN': ca_arn,
                                    'Statement ID': sid,
                                    'Effect': effect,
                                    'Principal': principal_str,
                                    'Actions': actions_str,
                                    'Conditions': conditions_str
                                })

                        except Exception as e:
                            utils.log_warning(f"Could not parse policy for CA {ca_arn}: {str(e)}")
                            continue

                except Exception as e:
                    # No policy attached is normal, skip
                    if 'ResourceNotFoundException' not in str(e):
                        utils.log_warning(f"Could not get policy for CA {ca_arn}: {str(e)}")
                    continue

    except Exception as e:
        utils.log_warning(f"Error collecting CA permissions in {region}: {str(e)}")

    return regional_permissions


@utils.aws_error_handler("Collecting CA permissions", default_return=[])
def collect_ca_permissions(regions: list[str]) -> list[dict[str, Any]]:
    """Collect permission policies for Private CAs."""
    print("\n=== COLLECTING CA PERMISSIONS ===")
    results = utils.scan_regions_concurrent(regions, _scan_ca_permissions_region)
    all_permissions = [perm for result in results for perm in result]
    utils.log_success(f"Total CA permission statements collected: {len(all_permissions)}")
    return all_permissions


def generate_summary(cas: list[dict[str, Any]],
                     certificates: list[dict[str, Any]],
                     permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for ACM Private CA resources."""
    utils.log_info("Generating summary statistics...")

    summary = []

    # CAs summary
    total_cas = len(cas)
    active_cas = sum(1 for ca in cas if ca.get('Status', '') == 'ACTIVE')
    root_cas = sum(1 for ca in cas if ca.get('Type', '') == 'ROOT')
    subordinate_cas = sum(1 for ca in cas if ca.get('Type', '') == 'SUBORDINATE')

    summary.append({
        'Metric': 'Total Private CAs',
        'Count': total_cas,
        'Details': f'Root: {root_cas}, Subordinate: {subordinate_cas}'
    })

    summary.append({
        'Metric': 'Active CAs',
        'Count': active_cas,
        'Details': 'Certificate authorities in ACTIVE status'
    })

    # CRL enabled
    crl_enabled = sum(1 for ca in cas if ca.get('CRL Enabled', False))
    summary.append({
        'Metric': 'CAs with CRL Enabled',
        'Count': crl_enabled,
        'Details': 'Certificate Revocation Lists configured'
    })

    # OCSP enabled
    ocsp_enabled = sum(1 for ca in cas if ca.get('OCSP Enabled', False))
    summary.append({
        'Metric': 'CAs with OCSP Enabled',
        'Count': ocsp_enabled,
        'Details': 'Online Certificate Status Protocol configured'
    })

    # Certificates summary
    total_certificates = len(certificates)
    issued_certs = sum(1 for cert in certificates if cert.get('Status', '') == 'ISSUED')
    revoked_certs = sum(1 for cert in certificates if cert.get('Status', '') == 'REVOKED')

    summary.append({
        'Metric': 'Total Certificates (Sample)',
        'Count': total_certificates,
        'Details': f'Issued: {issued_certs}, Revoked: {revoked_certs} (Limited to 50 per CA)'
    })

    # Permissions summary
    total_permissions = len(permissions)
    summary.append({
        'Metric': 'Total Permission Statements',
        'Count': total_permissions,
        'Details': 'Resource-based policy statements across all CAs'
    })

    # Security standards
    if cas:
        df = pd.DataFrame(cas)
        if 'Key Storage Security Standard' in df.columns:
            fips_count = sum(1 for std in df['Key Storage Security Standard'] if 'FIPS' in str(std))
            summary.append({
                'Metric': 'CAs with FIPS 140-2 Level 3',
                'Count': fips_count,
                'Details': 'CAs using FIPS-certified hardware security modules'
            })

    # Regional distribution
    if cas:
        df = pd.DataFrame(cas)
        regions = df['Region'].value_counts().to_dict()
        for region, count in regions.items():
            summary.append({
                'Metric': f'CAs in {region}',
                'Count': count,
                'Details': 'Regional distribution'
            })

    return summary


def main():
    """Main execution function."""
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    script_name = Path(__file__).stem
    utils.setup_logging(script_name)
    utils.log_script_start(script_name)

    account_id, account_name = utils.print_script_banner("AWS ACM PRIVATE CA EXPORT")
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Please check your credentials.")
        return

    utils.log_info(f"AWS Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Detect partition for region examples
    regions = utils.prompt_region_selection()
    # Collect data
    print("\nCollecting ACM Private CA data...")

    # Primary scope — region failures must propagate as failed_regions, never
    # collapse into "empty" (see .collab/audit/07.16.2026-...).
    cas, failed_regions = collect_private_cas(regions)
    certificates = collect_issued_certificates(regions)
    permissions = collect_ca_permissions(regions)
    summary = generate_summary(cas, certificates, permissions)

    # Create DataFrames
    utils.log_info("Creating DataFrames...")

    dataframes = {}

    if cas:
        df_cas = pd.DataFrame(cas)
        df_cas = utils.prepare_dataframe_for_export(df_cas)
        dataframes['Private CAs'] = df_cas

    if certificates:
        df_certificates = pd.DataFrame(certificates)
        df_certificates = utils.prepare_dataframe_for_export(df_certificates)
        dataframes['Certificates'] = df_certificates

    if permissions:
        df_permissions = pd.DataFrame(permissions)
        df_permissions = utils.prepare_dataframe_for_export(df_permissions)
        dataframes['CA Permissions'] = df_permissions

    if summary:
        df_summary = pd.DataFrame(summary)
        df_summary = utils.prepare_dataframe_for_export(df_summary)
        dataframes['Summary'] = df_summary

    # Export to Excel. The Summary sheet is always non-empty (generate_summary
    # always returns at least the top-level counters), so ``dataframes`` is
    # always truthy and a workbook always lands here — this is the Tier-3
    # PARTIAL behavior we preserve. It does NOT mean the export succeeded;
    # see the failed_regions check below.
    if dataframes:
        region_suffix = 'all-regions' if len(regions) > 1 else regions[0]
        filename = utils.create_export_filename(account_name, 'acm-privateca', region_suffix)

        utils.log_info(f"Exporting to {filename}...")
        utils.save_multiple_dataframes_to_excel(dataframes, filename)

        # Log summary
    else:
        utils.log_warning("No ACM Private CA data found to export")

    # If any region failed the primary Private CA scope collection, make it
    # loud: write a marker and exit non-zero, even though the forced Summary
    # sheet means a workbook still landed. A complete-looking file that is
    # silently missing data is exactly the failure mode this guards against.
    # A genuinely empty result (every region succeeded, none had CAs) stays
    # exit 0 with no marker.
    if failed_regions:
        utils.report_collection_failures(account_name, 'acm-privateca', failed_regions)
        print(
            "\nERROR: ACM Private CA export completed with failures — data is incomplete. "
            "See the *-acm-privateca-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)

    utils.log_success("ACM Private CA export completed successfully")


if __name__ == "__main__":
    main()
