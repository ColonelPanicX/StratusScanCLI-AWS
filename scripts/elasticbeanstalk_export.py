#!/usr/bin/env python3
"""
AWS Elastic Beanstalk Export Script for StratusScan
Date: NOV-16-2025

Exports comprehensive AWS Elastic Beanstalk PaaS information including applications,
environments, application versions, and configuration settings.

Features:
- Applications: Beanstalk application containers
- Environments: Application environments with platform branch/version, lifecycle state and health
- Application Versions: Deployable application versions and source bundles
- Configuration Templates: Saved environment configurations
- Phase 4B: Concurrent region scanning (4x-10x performance improvement)
- Summary: Application and environment counts with status distribution

Output: Excel file with 5 worksheets
"""

import sys
import threading
from pathlib import Path
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export Elastic Beanstalk applications and environments to Excel")

NOT_AVAILABLE = 'N/A'
STATE_NOT_FOUND = 'Not Found'
_PLATFORM_ARN_MARKER = ':platform/'

# Region -> (branch name -> LifecycleState, lookup error state or None).
# ListPlatformBranches is called at most once per region per run; the
# environment and configuration-template collectors share the result.
_BRANCH_STATE_CACHE: dict[str, tuple[dict[str, str], Optional[str]]] = {}
_BRANCH_STATE_LOCK = threading.Lock()


def parse_platform_arn(platform_arn: str, solution_stack_name: str) -> dict[str, str]:
    """
    Split an Elastic Beanstalk PlatformArn into branch name and version.

    ARN form: ``arn:aws:elasticbeanstalk:<region>:<acct>:platform/<Branch Name>/<version>``.
    Branch names contain spaces and are never split on whitespace; only the
    final ``/`` separates the version. Nothing is guessed: a missing or
    unparseable ARN yields ``N/A`` for branch and version, and the ``Platform``
    value falls back to the SolutionStackName (older environments) or ``N/A``.
    """
    branch = NOT_AVAILABLE
    version = NOT_AVAILABLE
    if platform_arn and platform_arn != NOT_AVAILABLE and _PLATFORM_ARN_MARKER in platform_arn:
        resource = platform_arn.split(_PLATFORM_ARN_MARKER, 1)[1]
        if '/' in resource:
            parsed_branch, parsed_version = resource.rsplit('/', 1)
            if parsed_branch and parsed_version:
                branch, version = parsed_branch, parsed_version
    platform = branch
    if platform == NOT_AVAILABLE and solution_stack_name and solution_stack_name != NOT_AVAILABLE:
        platform = solution_stack_name
    return {
        'Platform': platform,
        'Platform Branch': branch,
        'Platform Version': version,
        'Platform ARN': platform_arn or NOT_AVAILABLE,
        'Solution Stack Name': solution_stack_name or NOT_AVAILABLE,
    }


def _fetch_branch_states(region: str) -> tuple[dict[str, str], Optional[str]]:
    """Call ListPlatformBranches (NextToken pagination; botocore has no paginator for it)."""
    eb_client = utils.get_boto3_client('elasticbeanstalk', region_name=region)
    states: dict[str, str] = {}
    token = None
    try:
        while True:
            kwargs = {'NextToken': token} if token else {}
            response = eb_client.list_platform_branches(**kwargs)
            for branch in response.get('PlatformBranchSummaryList', []):
                name = branch.get('BranchName')
                if name:
                    states[name] = str(branch.get('LifecycleState', '')).capitalize() or NOT_AVAILABLE
            token = response.get('NextToken')
            if not token:
                break
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', 'Unknown')
        utils.log_warning(f"ListPlatformBranches failed in {region}: {code}")
        return {}, f"Lookup Failed ({code})"
    except BotoCoreError as e:
        utils.log_warning(f"ListPlatformBranches failed in {region}: {type(e).__name__}")
        return {}, f"Lookup Failed ({type(e).__name__})"
    return states, None


def get_branch_states(region: str) -> tuple[dict[str, str], Optional[str]]:
    """Return the cached (states, error) for a region, fetching once on first use."""
    with _BRANCH_STATE_LOCK:
        if region not in _BRANCH_STATE_CACHE:
            _BRANCH_STATE_CACHE[region] = _fetch_branch_states(region)
        return _BRANCH_STATE_CACHE[region]


def platform_columns(region: str, platform_arn: str, solution_stack_name: str) -> dict[str, str]:
    """
    Build the Platform column set for one row, including lifecycle state.

    Lifecycle is only looked up when a branch was parsed from an ARN. A row
    with no ARN gets ``N/A`` (the SolutionStackName is not a branch name, so
    matching it would be a guess).
    """
    cols = parse_platform_arn(platform_arn, solution_stack_name)
    branch = cols['Platform Branch']
    if branch == NOT_AVAILABLE:
        cols['Platform Branch State'] = NOT_AVAILABLE
        return cols
    states, error = get_branch_states(region)
    if error:
        cols['Platform Branch State'] = error
    else:
        cols['Platform Branch State'] = states.get(branch, STATE_NOT_FOUND)
    return cols


def _build_application_row(app: dict, region: str) -> dict[str, Any]:
    """Build a single Elastic Beanstalk application export row from a describe response."""
    app_name = app.get('ApplicationName', 'N/A')
    description = app.get('Description', 'N/A')

    # Resource lifecycle config
    resource_lifecycle_config = app.get('ResourceLifecycleConfig', {})
    service_role = resource_lifecycle_config.get('ServiceRole', 'N/A')
    version_lifecycle_config = resource_lifecycle_config.get('VersionLifecycleConfig', {})
    max_count = version_lifecycle_config.get('MaxCountRule', {}).get('MaxCount', 'N/A')
    max_age_days = version_lifecycle_config.get('MaxAgeRule', {}).get('MaxAgeInDays', 'N/A')

    # Extract role name
    role_name = 'N/A'
    if service_role != 'N/A' and '/' in service_role:
        role_name = service_role.split('/')[-1]

    # Date created
    date_created = app.get('DateCreated')
    date_created_str = date_created.strftime('%Y-%m-%d %H:%M:%S') if date_created else 'N/A'

    # Date updated
    date_updated = app.get('DateUpdated')
    date_updated_str = date_updated.strftime('%Y-%m-%d %H:%M:%S') if date_updated else 'N/A'

    # Versions count
    versions = app.get('Versions', [])
    version_count = len(versions)

    # Configuration templates
    config_templates = app.get('ConfigurationTemplates', [])
    config_template_count = len(config_templates)

    return {
        'Region': region,
        'Application Name': app_name,
        'Description': description,
        'Version Count': version_count,
        'Config Template Count': config_template_count,
        'Service Role': role_name,
        'Max Versions': max_count,
        'Max Age (Days)': max_age_days,
        'Created': date_created_str,
        'Updated': date_updated_str,
    }


def collect_applications_from_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Elastic Beanstalk applications from a single region.

    This is the primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no applications" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed applications are skipped (logged) rather than
    aborting the whole region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    applications = []
    eb_client = utils.get_boto3_client('elasticbeanstalk', region_name=region)

    response = eb_client.describe_applications()
    apps = response.get('Applications', [])

    for app in apps:
        try:
            applications.append(_build_application_row(app, region))
        except Exception as e:
            # One malformed application is skipped, not fatal to the region.
            utils.log_error(
                f"Skipping malformed Elastic Beanstalk application in {region}: "
                f"{app.get('ApplicationName', '<unknown>')}",
                e,
            )
            continue

    return applications


def collect_applications(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Elastic Beanstalk application information across regions, surfacing failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(applications, failed_regions)`` where ``failed_regions`` is
        a list of ``(region, error_message)`` tuples.
    """
    print("\n=== COLLECTING ELASTIC BEANSTALK APPLICATIONS ===")
    utils.log_info(f"Scanning {len(regions)} regions...")

    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_applications_from_region,
        show_progress=True,
        collect_failures=True,
    )

    all_applications = []
    for apps_in_region in region_results:
        all_applications.extend(apps_in_region)

    utils.log_success(f"Total applications collected: {len(all_applications)}")
    return all_applications, failed_regions


@utils.aws_error_handler("Collecting Elastic Beanstalk environments from region", default_return=[])
def collect_environments_from_region(region: str) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk environment information from a single AWS region."""
    environments = []
    eb_client = utils.get_boto3_client('elasticbeanstalk', region_name=region)

    paginator = eb_client.get_paginator('describe_environments')
    envs = []
    for page in paginator.paginate():
        envs.extend(page.get('Environments', []))

    for env in envs:
        env_name = env.get('EnvironmentName', 'N/A')
        env_id = env.get('EnvironmentId', 'N/A')
        app_name = env.get('ApplicationName', 'N/A')

        # Status and health
        status = env.get('Status', 'N/A')
        health = env.get('Health', 'N/A')
        health_status = env.get('HealthStatus', 'N/A')

        # Platform
        platform_arn = env.get('PlatformArn', 'N/A')
        solution_stack_name = env.get('SolutionStackName', 'N/A')

        platform_cols = platform_columns(region, platform_arn, solution_stack_name)

        # Tier (WebServer or Worker)
        tier = env.get('Tier', {})
        tier_name = tier.get('Name', 'N/A')
        tier_type = tier.get('Type', 'N/A')

        # Endpoint URL
        endpoint_url = env.get('EndpointURL', 'N/A')
        cname = env.get('CNAME', 'N/A')

        # Version label
        version_label = env.get('VersionLabel', 'N/A')

        # Template name
        template_name = env.get('TemplateName', 'N/A')

        # Description
        description = env.get('Description', 'N/A')

        # Date created and updated
        date_created = env.get('DateCreated')
        date_created_str = date_created.strftime('%Y-%m-%d %H:%M:%S') if date_created else 'N/A'

        date_updated = env.get('DateUpdated')
        date_updated_str = date_updated.strftime('%Y-%m-%d %H:%M:%S') if date_updated else 'N/A'

        # Resources (load balancer info)
        resources = env.get('Resources', {})
        load_balancer = resources.get('LoadBalancer', {})
        lb_name = load_balancer.get('LoadBalancerName', 'N/A') if load_balancer else 'N/A'

        # Environment links (for composite environments)
        env_links = env.get('EnvironmentLinks', [])
        linked_env_names = [link.get('LinkName', '') for link in env_links]
        linked_envs_str = ', '.join(linked_env_names) if linked_env_names else 'None'

        # Abortable operation in progress
        abortable_operation_in_progress = env.get('AbortableOperationInProgress', False)

        environments.append({
            'Region': region,
            'Environment Name': env_name,
            'Environment ID': env_id,
            'Application': app_name,
            'Status': status,
            'Health': health,
            'Health Status': health_status,
            'Platform': platform_cols['Platform'],
            'Tier Name': tier_name,
            'Tier Type': tier_type,
            'Endpoint URL': endpoint_url,
            'CNAME': cname,
            'Version Label': version_label,
            'Template Name': template_name,
            'Load Balancer': lb_name,
            'Linked Environments': linked_envs_str,
            'Operation In Progress': 'Yes' if abortable_operation_in_progress else 'No',
            'Description': description,
            'Created': date_created_str,
            'Updated': date_updated_str,
            # Appended at the end: consumers key on column positions.
            'Platform Branch': platform_cols['Platform Branch'],
            'Platform Version': platform_cols['Platform Version'],
            'Platform ARN': platform_cols['Platform ARN'],
            'Solution Stack Name': platform_cols['Solution Stack Name'],
            'Platform Branch State': platform_cols['Platform Branch State'],
        })

    return environments


def collect_environments(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk environment information using concurrent scanning."""
    print("\n=== COLLECTING ELASTIC BEANSTALK ENVIRONMENTS ===")
    utils.log_info(f"Scanning {len(regions)} regions...")

    region_results = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_environments_from_region,
        show_progress=True
    )

    all_environments = []
    for envs_in_region in region_results:
        all_environments.extend(envs_in_region)

    utils.log_success(f"Total environments collected: {len(all_environments)}")
    return all_environments


@utils.aws_error_handler("Collecting Elastic Beanstalk application versions from region", default_return=[])
def collect_application_versions_from_region(region: str) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk application version information from a single AWS region."""
    versions = []
    eb_client = utils.get_boto3_client('elasticbeanstalk', region_name=region)

    # First get all applications
    response = eb_client.describe_applications()
    applications = response.get('Applications', [])

    for app in applications:
        app_name = app.get('ApplicationName', '')

        # Get versions for this application
        try:
            versions_paginator = eb_client.get_paginator('describe_application_versions')
            app_versions = []
            for vpage in versions_paginator.paginate(ApplicationName=app_name):
                app_versions.extend(vpage.get('ApplicationVersions', []))

            for version in app_versions:
                version_label = version.get('VersionLabel', 'N/A')
                description = version.get('Description', 'N/A')
                status = version.get('Status', 'N/A')

                # Source bundle
                source_bundle = version.get('SourceBundle', {})
                s3_bucket = source_bundle.get('S3Bucket', 'N/A')
                s3_key = source_bundle.get('S3Key', 'N/A')
                source_location = f"s3://{s3_bucket}/{s3_key}" if s3_bucket != 'N/A' else 'N/A'

                # Build ARN (for CodeBuild)
                build_arn = version.get('BuildArn', 'N/A')

                # Date created
                date_created = version.get('DateCreated')
                if date_created:
                    date_created_str = date_created.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    date_created_str = 'N/A'

                # Date updated
                date_updated = version.get('DateUpdated')
                if date_updated:
                    date_updated_str = date_updated.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    date_updated_str = 'N/A'

                versions.append({
                    'Region': region,
                    'Application': app_name,
                    'Version Label': version_label,
                    'Status': status,
                    'Source Location': source_location,
                    'Build ARN': build_arn,
                    'Description': description,
                    'Created': date_created_str,
                    'Updated': date_updated_str,
                })

        except Exception as e:
            utils.log_warning(f"Could not get versions for application {app_name}: {str(e)}")
            continue

    return versions


def collect_application_versions(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk application version information using concurrent scanning."""
    print("\n=== COLLECTING ELASTIC BEANSTALK APPLICATION VERSIONS ===")
    utils.log_info(f"Scanning {len(regions)} regions...")

    region_results = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_application_versions_from_region,
        show_progress=True
    )

    all_versions = []
    for versions_in_region in region_results:
        all_versions.extend(versions_in_region)

    utils.log_success(f"Total application versions collected: {len(all_versions)}")
    return all_versions


@utils.aws_error_handler("Collecting Elastic Beanstalk configuration templates from region", default_return=[])
def collect_configuration_templates_from_region(region: str) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk configuration template information from a single AWS region."""
    templates = []
    eb_client = utils.get_boto3_client('elasticbeanstalk', region_name=region)

    # First get all applications
    response = eb_client.describe_applications()
    applications = response.get('Applications', [])

    for app in applications:
        app_name = app.get('ApplicationName', '')
        config_templates = app.get('ConfigurationTemplates', [])

        for template_name in config_templates:
            try:
                # Get template details
                template_response = eb_client.describe_configuration_settings(
                    ApplicationName=app_name,
                    TemplateName=template_name
                )
                config_settings = template_response.get('ConfigurationSettings', [])

                for config in config_settings:
                    solution_stack_name = config.get('SolutionStackName', 'N/A')
                    platform_arn = config.get('PlatformArn', 'N/A')
                    description = config.get('Description', 'N/A')
                    deployment_status = config.get('DeploymentStatus', 'N/A')

                    # Date created and updated
                    date_created = config.get('DateCreated')
                    if date_created:
                        date_created_str = date_created.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        date_created_str = 'N/A'

                    date_updated = config.get('DateUpdated')
                    if date_updated:
                        date_updated_str = date_updated.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        date_updated_str = 'N/A'

                    platform_cols = platform_columns(region, platform_arn, solution_stack_name)

                    templates.append({
                        'Region': region,
                        'Application': app_name,
                        'Template Name': template_name,
                        'Platform': platform_cols['Platform'],
                        'Deployment Status': deployment_status,
                        'Description': description,
                        'Created': date_created_str,
                        'Updated': date_updated_str,
                        # Appended at the end: consumers key on column positions.
                        'Platform Branch': platform_cols['Platform Branch'],
                        'Platform Version': platform_cols['Platform Version'],
                        'Platform ARN': platform_cols['Platform ARN'],
                        'Solution Stack Name': platform_cols['Solution Stack Name'],
                        'Platform Branch State': platform_cols['Platform Branch State'],
                    })

            except Exception as e:
                utils.log_warning(f"Could not get template {template_name} for application {app_name}: {str(e)}")
                continue

    return templates


def collect_configuration_templates(regions: list[str]) -> list[dict[str, Any]]:
    """Collect Elastic Beanstalk configuration template information using concurrent scanning."""
    print("\n=== COLLECTING ELASTIC BEANSTALK CONFIGURATION TEMPLATES ===")
    utils.log_info(f"Scanning {len(regions)} regions...")

    region_results = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=collect_configuration_templates_from_region,
        show_progress=True
    )

    all_templates = []
    for templates_in_region in region_results:
        all_templates.extend(templates_in_region)

    utils.log_success(f"Total configuration templates collected: {len(all_templates)}")
    return all_templates


def generate_summary(applications: list[dict[str, Any]],
                     environments: list[dict[str, Any]],
                     versions: list[dict[str, Any]],
                     templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for Elastic Beanstalk resources."""
    summary = []

    # Overall counts
    summary.append({
        'Metric': 'Total Applications',
        'Count': len(applications),
        'Details': f"{len(applications)} Elastic Beanstalk applications"
    })

    summary.append({
        'Metric': 'Total Environments',
        'Count': len(environments),
        'Details': f"{len([e for e in environments if e['Status'] == 'Ready'])} ready"
    })

    summary.append({
        'Metric': 'Total Application Versions',
        'Count': len(versions),
        'Details': f"{len(versions)} deployable versions"
    })

    summary.append({
        'Metric': 'Total Configuration Templates',
        'Count': len(templates),
        'Details': f"{len(templates)} saved configurations"
    })

    # Environment health distribution
    if environments:
        health_statuses = {}
        for env in environments:
            health = env['Health Status']
            health_statuses[health] = health_statuses.get(health, 0) + 1

        health_details = ', '.join([f"{health}: {count}" for health, count in sorted(health_statuses.items())])
        summary.append({
            'Metric': 'Environment Health Distribution',
            'Count': len(health_statuses),
            'Details': health_details
        })

    # Environment tiers
    if environments:
        tiers = {}
        for env in environments:
            tier = env['Tier Name']
            tiers[tier] = tiers.get(tier, 0) + 1

        tier_details = ', '.join([f"{tier}: {count}" for tier, count in sorted(tiers.items())])
        summary.append({
            'Metric': 'Environment Tier Distribution',
            'Count': len(tiers),
            'Details': tier_details
        })

    # Platforms used
    if environments:
        platforms = {}
        for env in environments:
            # Group by branch; rows without a PlatformArn fall back to their
            # Platform value (SolutionStackName or N/A) rather than a guess.
            branch = env.get('Platform Branch', 'N/A')
            platform_key = branch if branch != 'N/A' else env['Platform']
            platforms[platform_key] = platforms.get(platform_key, 0) + 1

        top_platforms = sorted(platforms.items(), key=lambda x: x[1], reverse=True)[:5]
        platform_details = ', '.join([f"{plat}: {count}" for plat, count in top_platforms])
        summary.append({
            'Metric': 'Top 5 Platforms',
            'Count': len(platforms),
            'Details': platform_details
        })

    # Environments by region
    if environments:
        regions = {}
        for env in environments:
            region = env['Region']
            regions[region] = regions.get(region, 0) + 1

        region_details = ', '.join([f"{region}: {count}" for region, count in sorted(regions.items())])
        summary.append({
            'Metric': 'Environments by Region',
            'Count': len(regions),
            'Details': region_details
        })

    return summary


def _run_export(account_id: str, account_name: str, regions: list[str]) -> list:
    """
    Collect Elastic Beanstalk data and write the Excel export.

    Returns:
        list: ``failed_regions`` — ``(region, error_message)`` tuples for
        regions whose Applications scope collection failed. The Summary
        sheet (and workbook) is always written regardless; the caller
        decides whether to also report a FAILED marker and exit non-zero.
    """
    # Collect data
    print("\n=== Collecting Elastic Beanstalk Data ===")
    # Applications are the primary scope — region failures must propagate as
    # failed_regions, never collapse into "empty".
    applications, failed_regions = collect_applications(regions)
    environments = collect_environments(regions)
    versions = collect_application_versions(regions)
    templates = collect_configuration_templates(regions)

    # Generate summary
    summary = generate_summary(applications, environments, versions, templates)

    # Convert to DataFrames
    applications_df = pd.DataFrame(applications) if applications else pd.DataFrame()
    environments_df = pd.DataFrame(environments) if environments else pd.DataFrame()
    versions_df = pd.DataFrame(versions) if versions else pd.DataFrame()
    templates_df = pd.DataFrame(templates) if templates else pd.DataFrame()
    summary_df = pd.DataFrame(summary)

    # Prepare DataFrames for export
    if not applications_df.empty:
        applications_df = utils.prepare_dataframe_for_export(applications_df)
    if not environments_df.empty:
        environments_df = utils.prepare_dataframe_for_export(environments_df)
    if not versions_df.empty:
        versions_df = utils.prepare_dataframe_for_export(versions_df)
    if not templates_df.empty:
        templates_df = utils.prepare_dataframe_for_export(templates_df)
    if not summary_df.empty:
        summary_df = utils.prepare_dataframe_for_export(summary_df)

    # Create export filename
    region_suffix = regions[0] if len(regions) == 1 else 'all-regions'
    filename = utils.create_export_filename(account_name, 'elasticbeanstalk', region_suffix)

    # Save to Excel with multiple sheets
    print("\n=== Exporting to Excel ===")
    dataframes = {
        'Applications': applications_df,
        'Environments': environments_df,
        'Application Versions': versions_df,
        'Config Templates': templates_df,
        'Summary': summary_df
    }

    utils.save_multiple_dataframes_to_excel(dataframes, filename)

    return failed_regions


def main():
    """Main execution function — 3-step state machine (region -> confirm -> export)."""
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    utils.setup_logging("elasticbeanstalk-export")

    try:
        account_id, account_name = utils.print_script_banner("AWS ELASTIC BEANSTALK EXPORT")

        step = 1
        regions = None

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="Elastic Beanstalk")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                region_str = regions[0] if len(regions) == 1 else f"{len(regions)} regions"
                msg = f"Ready to export Elastic Beanstalk data ({region_str})."
                result = utils.prompt_confirmation(msg)
                if result == 'back':
                    step = 1
                    continue
                if result == 'exit':
                    sys.exit(11)
                step = 3

            elif step == 3:
                failed_regions = _run_export(account_id, account_name, regions)
                # If ANY region failed the Applications scope collection,
                # make it loud: write a marker and exit non-zero, even
                # though a workbook was still written (the forced Summary
                # sheet). A complete-looking file that hides a failed scope
                # is exactly the failure mode this guards against.
                if failed_regions:
                    utils.report_collection_failures(account_name, 'elasticbeanstalk', failed_regions)
                    print(
                        "\nERROR: Elastic Beanstalk export completed with failures — data is incomplete. "
                        "See the *-elasticbeanstalk-FAILED-*.txt marker in the output directory."
                    )
                    sys.exit(1)
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
