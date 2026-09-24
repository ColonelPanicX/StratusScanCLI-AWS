#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Compute Optimizer Recommendations Export
Date: MAR-05-2025

Description:
This script exports AWS Compute Optimizer recommendations for EC2 instances,
Auto Scaling groups, EBS volumes, Lambda functions, and ECS services on Fargate.
The data is exported to an Excel file with separate tabs for each recommendation type.
"""

import datetime
import sys
from pathlib import Path
from typing import Any, Callable

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
args = utils.parse_script_args("Export Compute Optimizer recommendations to Excel")
@utils.aws_error_handler("Getting available regions", default_return=[
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'ca-central-1', 'eu-west-1', 'eu-west-2', 'eu-central-1',
    'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-south-1'
])
def get_all_regions():
    """
    Get a list of all available AWS regions.

    Returns:
        list: List of region names
    """
    ec2_client = utils.get_boto3_client('ec2')
    regions = [region['RegionName'] for region in ec2_client.describe_regions()['Regions']]
    return regions

@utils.aws_error_handler("Checking Compute Optimizer availability", default_return=False)
def check_compute_optimizer_availability(region):
    """
    Check if Compute Optimizer is available and has recommendations in the specified region.

    Args:
        region (str): AWS region name

    Returns:
        bool: True if Compute Optimizer is available, False otherwise
    """
    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)
    enrollment = compute_optimizer.get_enrollment_status()
    status = enrollment.get('status', 'NOT_ENROLLED')

    if status == 'ACTIVE':
        print(f"Compute Optimizer is enrolled and active in {region}")
        return True
    else:
        print(f"Compute Optimizer is not active in {region} (Status: {status})")
        return False

def _build_ec2_recommendation_row(recommendation: dict[str, Any], region: str) -> dict[str, Any]:
    """Build a single EC2 instance recommendation export row."""
    current_instance = recommendation.get('currentInstanceType', 'Unknown')
    instance_id = recommendation.get('instanceArn', 'Unknown').split('/')[-1]

    # Process recommendation options
    rec_options = recommendation.get('recommendationOptions', [])
    if rec_options:
        top_recommendation = rec_options[0]
        recommended_type = top_recommendation.get('instanceType', 'Unknown')
        savings_opportunity = top_recommendation.get('savingsOpportunity', {})
        savings_percentage = savings_opportunity.get('savingsPercentage', 0) * 100
        estimated_monthly_savings = savings_opportunity.get('estimatedMonthlySavings', {}).get('value', 0)
        performance_risk = top_recommendation.get('performanceRisk', 'Unknown')
    else:
        recommended_type = 'No recommendation'
        savings_percentage = 0
        estimated_monthly_savings = 0
        performance_risk = 'Unknown'

    # Get utilization metrics
    metrics = recommendation.get('utilizationMetrics', [])
    cpu_utilization = next((m.get('value') for m in metrics if m.get('name') == 'CPU'), 0)
    memory_utilization = next((m.get('value') for m in metrics if m.get('name') == 'MEMORY'), 0)

    return {
        'Region': region,
        'Instance ID': instance_id,
        'Current Instance Type': current_instance,
        'Recommended Instance Type': recommended_type,
        'Finding': recommendation.get('finding', 'Unknown'),
        'Performance Risk': performance_risk,
        'CPU Utilization (%)': cpu_utilization,
        'Memory Utilization (%)': memory_utilization,
        'Savings Percentage (%)': round(savings_percentage, 2),
        'Estimated Monthly Savings ($)': round(estimated_monthly_savings, 2),
        'Reason': recommendation.get('findingReasonCodes', ['Unknown'])[0] if recommendation.get('findingReasonCodes') else 'Unknown'
    }


def get_ec2_recommendations(region: str) -> list[dict[str, Any]]:
    """
    Get EC2 instance recommendations from Compute Optimizer for a single region.

    This is a primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the
    region as failed instead of silently reporting "no recommendations" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed recommendations are skipped (logged) rather than
    aborting the whole region.

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing EC2 recommendations
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Fetching EC2 instance recommendations for region {region}")
    recommendations = []

    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)

    # Use pagination to handle large number of recommendations
    next_token = None
    while True:
        params = {}
        params['maxResults'] = 100
        if next_token:
            params['nextToken'] = next_token
        page = compute_optimizer.get_ec2_instance_recommendations(**params)
        for recommendation in page.get('instanceRecommendations', []):
            try:
                recommendations.append(_build_ec2_recommendation_row(recommendation, region))
            except Exception as e:
                utils.log_error(f"Skipping malformed EC2 recommendation in {region}", e)
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_success(f"Found {len(recommendations)} EC2 instance recommendations in {region}")
    return recommendations

def _build_asg_recommendation_row(recommendation: dict[str, Any], region: str) -> dict[str, Any]:
    """Build a single Auto Scaling Group recommendation export row."""
    asg_name = recommendation.get('autoScalingGroupName', 'Unknown')
    current_instances = recommendation.get('currentInstanceType', ['Unknown'])

    # Process recommendation options
    rec_options = recommendation.get('recommendationOptions', [])
    if rec_options:
        top_recommendation = rec_options[0]
        recommended_types = top_recommendation.get('instanceType', ['Unknown'])
        savings_opportunity = top_recommendation.get('savingsOpportunity', {})
        savings_percentage = savings_opportunity.get('savingsPercentage', 0) * 100
        estimated_monthly_savings = savings_opportunity.get('estimatedMonthlySavings', {}).get('value', 0)
    else:
        recommended_types = ['No recommendation']
        savings_percentage = 0
        estimated_monthly_savings = 0

    # Get current configuration
    current_config = recommendation.get('currentConfiguration', {})
    min_size = current_config.get('desiredCapacity', 'Unknown')
    max_size = current_config.get('maxSize', 'Unknown')

    return {
        'Region': region,
        'Auto Scaling Group Name': asg_name,
        'Current Instance Types': ', '.join(current_instances),
        'Recommended Instance Types': ', '.join(recommended_types),
        'Desired Capacity': min_size,
        'Max Size': max_size,
        'Finding': recommendation.get('finding', 'Unknown'),
        'Savings Percentage (%)': round(savings_percentage, 2),
        'Estimated Monthly Savings ($)': round(estimated_monthly_savings, 2),
        'Reason': recommendation.get('findingReasonCodes', ['Unknown'])[0] if recommendation.get('findingReasonCodes') else 'Unknown'
    }


def get_asg_recommendations(region: str) -> list[dict[str, Any]]:
    """
    Get Auto Scaling Group recommendations from Compute Optimizer for a
    single region.

    This is a primary scope collector — see ``get_ec2_recommendations`` for
    the silent-collection-loss rationale. Region-level failures propagate;
    individual malformed recommendations are skipped (logged) instead of
    aborting the whole region.

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing ASG recommendations
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Fetching Auto Scaling Group recommendations for region {region}")
    recommendations = []

    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)

    # Use pagination to handle large number of recommendations
    next_token = None
    while True:
        params = {}
        params['maxResults'] = 100
        if next_token:
            params['nextToken'] = next_token
        page = compute_optimizer.get_auto_scaling_group_recommendations(**params)
        for recommendation in page.get('autoScalingGroupRecommendations', []):
            try:
                recommendations.append(_build_asg_recommendation_row(recommendation, region))
            except Exception as e:
                utils.log_error(f"Skipping malformed Auto Scaling Group recommendation in {region}", e)
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_success(f"Found {len(recommendations)} Auto Scaling Group recommendations in {region}")
    return recommendations

def _build_ebs_recommendation_row(recommendation: dict[str, Any], region: str) -> dict[str, Any]:
    """Build a single EBS volume recommendation export row."""
    volume_arn = recommendation.get('volumeArn', 'Unknown')
    volume_id = volume_arn.split('/')[-1]
    current_config = recommendation.get('currentConfiguration', {})
    current_volume_type = current_config.get('volumeType', 'Unknown')
    current_volume_size = current_config.get('volumeSize', 0)
    current_volume_iops = current_config.get('volumeBaselineIOPS', 0)

    # Process recommendation options
    rec_options = recommendation.get('volumeRecommendationOptions', [])
    if rec_options:
        top_recommendation = rec_options[0]
        recommended_config = top_recommendation.get('configuration', {})
        recommended_type = recommended_config.get('volumeType', 'Unknown')
        recommended_size = recommended_config.get('volumeSize', 0)
        recommended_iops = recommended_config.get('volumeBaselineIOPS', 0)
        savings_opportunity = top_recommendation.get('savingsOpportunity', {})
        savings_percentage = savings_opportunity.get('savingsPercentage', 0) * 100
        estimated_monthly_savings = savings_opportunity.get('estimatedMonthlySavings', {}).get('value', 0)
    else:
        recommended_type = 'No recommendation'
        recommended_size = current_volume_size
        recommended_iops = current_volume_iops
        savings_percentage = 0
        estimated_monthly_savings = 0

    # Get utilization metrics
    if 'utilizationMetrics' in recommendation:
        metrics = recommendation.get('utilizationMetrics', [])
        read_ops_per_second = next((m.get('value') for m in metrics if m.get('name') == 'VolumeReadOpsPerSecond'), 0)
        write_ops_per_second = next((m.get('value') for m in metrics if m.get('name') == 'VolumeWriteOpsPerSecond'), 0)
    else:
        read_ops_per_second = 0
        write_ops_per_second = 0

    return {
        'Region': region,
        'Volume ID': volume_id,
        'Current Volume Type': current_volume_type,
        'Current Size (GB)': current_volume_size,
        'Current IOPS': current_volume_iops,
        'Recommended Volume Type': recommended_type,
        'Recommended Size (GB)': recommended_size,
        'Recommended IOPS': recommended_iops,
        'Read Ops/Sec': read_ops_per_second,
        'Write Ops/Sec': write_ops_per_second,
        'Finding': recommendation.get('finding', 'Unknown'),
        'Savings Percentage (%)': round(savings_percentage, 2),
        'Estimated Monthly Savings ($)': round(estimated_monthly_savings, 2),
        'Reason': recommendation.get('findingReasonCodes', ['Unknown'])[0] if recommendation.get('findingReasonCodes') else 'Unknown'
    }


def get_ebs_recommendations(region: str) -> list[dict[str, Any]]:
    """
    Get EBS volume recommendations from Compute Optimizer for a single
    region.

    This is a primary scope collector — see ``get_ec2_recommendations`` for
    the silent-collection-loss rationale. Region-level failures propagate;
    individual malformed recommendations are skipped (logged) instead of
    aborting the whole region.

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing EBS recommendations
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Fetching EBS volume recommendations for region {region}")
    recommendations = []

    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)

    # Use pagination to handle large number of recommendations
    next_token = None
    while True:
        params = {}
        params['maxResults'] = 100
        if next_token:
            params['nextToken'] = next_token
        page = compute_optimizer.get_ebs_volume_recommendations(**params)
        for recommendation in page.get('volumeRecommendations', []):
            try:
                recommendations.append(_build_ebs_recommendation_row(recommendation, region))
            except Exception as e:
                utils.log_error(f"Skipping malformed EBS volume recommendation in {region}", e)
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_success(f"Found {len(recommendations)} EBS volume recommendations in {region}")
    return recommendations

def _build_lambda_recommendation_row(recommendation: dict[str, Any], region: str) -> dict[str, Any]:
    """Build a single Lambda function recommendation export row."""
    function_arn = recommendation.get('functionArn', 'Unknown')
    function_name = function_arn.split(':')[-1]

    # Get current configuration
    current_config = recommendation.get('currentConfiguration', {})
    current_memory = current_config.get('memorySize', 0)

    # Process recommendation options
    rec_options = recommendation.get('functionRecommendationOptions', [])
    if rec_options:
        top_recommendation = rec_options[0]
        recommended_config = top_recommendation.get('configuration', {})
        recommended_memory = recommended_config.get('memorySize', 0)
        savings_opportunity = top_recommendation.get('savingsOpportunity', {})
        savings_percentage = savings_opportunity.get('savingsPercentage', 0) * 100
        estimated_monthly_savings = savings_opportunity.get('estimatedMonthlySavings', {}).get('value', 0)
    else:
        recommended_memory = current_memory
        savings_percentage = 0
        estimated_monthly_savings = 0

    # Get utilization metrics
    metrics = recommendation.get('utilizationMetrics', [])
    memory_utilization = next((m.get('value') for m in metrics if m.get('name') == 'Memory'), 0)

    return {
        'Region': region,
        'Function Name': function_name,
        'Current Memory (MB)': current_memory,
        'Recommended Memory (MB)': recommended_memory,
        'Memory Utilization (%)': memory_utilization,
        'Finding': recommendation.get('finding', 'Unknown'),
        'Savings Percentage (%)': round(savings_percentage, 2),
        'Estimated Monthly Savings ($)': round(estimated_monthly_savings, 2),
        'Last Invocation Time': recommendation.get('lastRefreshTimestamp', 'Unknown')
    }


def get_lambda_recommendations(region: str) -> list[dict[str, Any]]:
    """
    Get Lambda function recommendations from Compute Optimizer for a single
    region.

    This is a primary scope collector — see ``get_ec2_recommendations`` for
    the silent-collection-loss rationale. Region-level failures propagate;
    individual malformed recommendations are skipped (logged) instead of
    aborting the whole region.

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing Lambda recommendations
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Fetching Lambda function recommendations for region {region}")
    recommendations = []

    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)

    # Use pagination to handle large number of recommendations
    paginator = compute_optimizer.get_paginator('get_lambda_function_recommendations')

    for page in paginator.paginate():
        for recommendation in page.get('lambdaFunctionRecommendations', []):
            try:
                recommendations.append(_build_lambda_recommendation_row(recommendation, region))
            except Exception as e:
                utils.log_error(f"Skipping malformed Lambda function recommendation in {region}", e)
                continue

    utils.log_success(f"Found {len(recommendations)} Lambda function recommendations in {region}")
    return recommendations

def _build_ecs_recommendation_row(recommendation: dict[str, Any], region: str) -> dict[str, Any]:
    """Build a single ECS service recommendation export row."""
    service_arn = recommendation.get('serviceArn', 'Unknown')
    service_name = service_arn.split('/')[-1]
    cluster_name = service_arn.split('/')[-2]

    # Get current configuration
    current_config = recommendation.get('currentServiceConfiguration', {})
    current_cpu = current_config.get('cpu', 'Unknown')
    current_memory = current_config.get('memory', 'Unknown')

    # Process recommendation options
    rec_options = recommendation.get('serviceRecommendationOptions', [])
    if rec_options:
        top_recommendation = rec_options[0]
        recommended_config = top_recommendation.get('serviceConfiguration', {})
        recommended_cpu = recommended_config.get('cpu', 'Unknown')
        recommended_memory = recommended_config.get('memory', 'Unknown')
        savings_opportunity = top_recommendation.get('savingsOpportunity', {})
        savings_percentage = savings_opportunity.get('savingsPercentage', 0) * 100
        estimated_monthly_savings = savings_opportunity.get('estimatedMonthlySavings', {}).get('value', 0)
    else:
        recommended_cpu = current_cpu
        recommended_memory = current_memory
        savings_percentage = 0
        estimated_monthly_savings = 0

    # Get utilization metrics
    metrics = recommendation.get('utilizationMetrics', [])
    cpu_utilization = next((m.get('value') for m in metrics if m.get('name') == 'CPU'), 0)
    memory_utilization = next((m.get('value') for m in metrics if m.get('name') == 'MEMORY'), 0)

    return {
        'Region': region,
        'Cluster Name': cluster_name,
        'Service Name': service_name,
        'Current CPU': current_cpu,
        'Current Memory': current_memory,
        'Recommended CPU': recommended_cpu,
        'Recommended Memory': recommended_memory,
        'CPU Utilization (%)': cpu_utilization,
        'Memory Utilization (%)': memory_utilization,
        'Finding': recommendation.get('finding', 'Unknown'),
        'Savings Percentage (%)': round(savings_percentage, 2),
        'Estimated Monthly Savings ($)': round(estimated_monthly_savings, 2),
        'Reason': recommendation.get('findingReasonCodes', ['Unknown'])[0] if recommendation.get('findingReasonCodes') else 'Unknown'
    }


def get_ecs_recommendations(region: str) -> list[dict[str, Any]]:
    """
    Get ECS service recommendations from Compute Optimizer for a single
    region.

    This is a primary scope collector — see ``get_ec2_recommendations`` for
    the silent-collection-loss rationale. Region-level failures propagate;
    individual malformed recommendations are skipped (logged) instead of
    aborting the whole region.

    Args:
        region (str): AWS region name

    Returns:
        list: List of dictionaries containing ECS recommendations
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Fetching ECS service recommendations for region {region}")
    recommendations = []

    compute_optimizer = utils.get_boto3_client('compute-optimizer', region_name=region)

    # Use pagination to handle large number of recommendations
    next_token = None
    while True:
        params = {}
        params['maxResults'] = 100
        if next_token:
            params['nextToken'] = next_token
        page = compute_optimizer.get_ecs_service_recommendations(**params)
        for recommendation in page.get('ecsServiceRecommendations', []):
            try:
                recommendations.append(_build_ecs_recommendation_row(recommendation, region))
            except Exception as e:
                utils.log_error(f"Skipping malformed ECS service recommendation in {region}", e)
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_success(f"Found {len(recommendations)} ECS service recommendations in {region}")
    return recommendations


def _scope_collect(
    label: str,
    regions: list[str],
    scan_function: Callable[[str], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """
    Shared scope wrapper: run ``scan_function`` concurrently across
    ``regions`` and surface failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result. The scope ``label`` is tagged onto each failure's error message
    (not the region name) so a combined ``failed_regions`` list — built by
    merging the five recommendation scopes in this file — still reads
    unambiguously in the failure marker.

    Args:
        label: Short scope label used in progress output and failure tags
            (e.g. ``"EC2"``, ``"Lambda"``).
        regions: List of AWS regions to scan (already filtered to
            Compute-Optimizer-active regions).
        scan_function: Per-region collector, e.g. ``get_ec2_recommendations``.

    Returns:
        tuple: ``(recommendations, failed_regions)`` where ``failed_regions``
        is a list of ``(region, error_message)`` tuples.
    """
    print(f"\n=== COLLECTING {label.upper()} RECOMMENDATIONS ===")
    region_results, raw_failed = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=scan_function,
        show_progress=True,
        collect_failures=True,
    )
    all_recs = [rec for result in region_results for rec in result]
    utils.log_success(f"Total {label} recommendations collected: {len(all_recs)}")
    failed_regions = [(region, f"[{label}] {err}") for region, err in raw_failed]
    return all_recs, failed_regions


def collect_ec2_recommendations(regions: list[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Scope wrapper: EC2 instance recommendations across regions."""
    return _scope_collect("EC2", regions, get_ec2_recommendations)


def collect_asg_recommendations(regions: list[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Scope wrapper: Auto Scaling Group recommendations across regions."""
    return _scope_collect("ASG", regions, get_asg_recommendations)


def collect_ebs_recommendations(regions: list[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Scope wrapper: EBS volume recommendations across regions."""
    return _scope_collect("EBS", regions, get_ebs_recommendations)


def collect_lambda_recommendations(regions: list[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Scope wrapper: Lambda function recommendations across regions."""
    return _scope_collect("Lambda", regions, get_lambda_recommendations)


def collect_ecs_recommendations(regions: list[str]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Scope wrapper: ECS service recommendations across regions."""
    return _scope_collect("ECS", regions, get_ecs_recommendations)

def export_recommendations_to_excel(all_recommendations, account_name):
    """
    Export recommendations to an Excel file with separate tabs for each resource type.

    Args:
        all_recommendations (dict): Dictionary containing recommendations for each resource type
        account_name (str): AWS account name for file naming

    Returns:
        str: Path to the created Excel file
    """
    # Create DataFrames for each resource type
    dfs = {}

    # Check if there are any recommendations for each resource type
    if all_recommendations.get('EC2'):
        dfs['EC2 Instances'] = pd.DataFrame(all_recommendations['EC2'])

    if all_recommendations.get('ASG'):
        dfs['Auto Scaling Groups'] = pd.DataFrame(all_recommendations['ASG'])

    if all_recommendations.get('EBS'):
        dfs['EBS Volumes'] = pd.DataFrame(all_recommendations['EBS'])

    if all_recommendations.get('Lambda'):
        dfs['Lambda Functions'] = pd.DataFrame(all_recommendations['Lambda'])

    if all_recommendations.get('ECS'):
        dfs['ECS Services'] = pd.DataFrame(all_recommendations['ECS'])

    # If no recommendations found for any resource type
    if not dfs:
        print("No recommendations found for any resource type.")
        return None

    # Generate filename with current date
    current_date = datetime.datetime.now().strftime("%m.%d.%Y")

    # Use utils module to generate filename
    filename = utils.create_export_filename(
        account_name,
        "compute-optimizer",
        "",
        current_date
    )

    # Use utils module to save multiple DataFrames to Excel
    output_path = utils.save_multiple_dataframes_to_excel(dfs, filename)

    if output_path:
        print(f"\nRecommendations exported successfully to: {output_path}")
        return output_path
    else:
        print("Error exporting recommendations to Excel.")
        return None

def get_recommendations_for_all_regions() -> tuple[dict[str, list], list[tuple[str, str]]]:
    """
    Get Compute Optimizer recommendations for all supported regions.

    Runs each of the five recommendation types (EC2, ASG, EBS, Lambda, ECS)
    as its own scope collector via ``scan_regions_concurrent(...,
    collect_failures=True)``, then merges all five scopes' failed regions
    into a single combined list. This is what lets the caller (``main`` /
    ``export_recommendations_to_excel``) tell a FAILED collection apart from
    a genuinely empty account — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``.

    Returns:
        tuple: ``(all_recommendations, failed_regions)`` where
        ``all_recommendations`` is a dict keyed by resource type, and
        ``failed_regions`` is the combined list of ``(region, error_message)``
        tuples across all five scopes.
    """
    # Dictionary to store recommendations for each resource type
    all_recommendations = {
        'EC2': [],
        'ASG': [],
        'EBS': [],
        'Lambda': [],
        'ECS': []
    }

    # Get all available regions
    regions = get_all_regions()
    print(f"Found {len(regions)} AWS regions.")

    # Determine which regions have Compute Optimizer active. A region that
    # is simply not enrolled is not a collection failure, so this gating
    # step stays outside the failed_regions contract.
    active_regions = []
    for region in regions:
        print(f"\nChecking Compute Optimizer availability in region: {region}")
        if check_compute_optimizer_availability(region):
            active_regions.append(region)

    if not active_regions:
        print("\nCompute Optimizer is not active in any checked region.")
        return all_recommendations, []

    failed_regions: list[tuple[str, str]] = []

    ec2_recommendations, ec2_failed = collect_ec2_recommendations(active_regions)
    all_recommendations['EC2'] = ec2_recommendations
    failed_regions.extend(ec2_failed)

    asg_recommendations, asg_failed = collect_asg_recommendations(active_regions)
    all_recommendations['ASG'] = asg_recommendations
    failed_regions.extend(asg_failed)

    ebs_recommendations, ebs_failed = collect_ebs_recommendations(active_regions)
    all_recommendations['EBS'] = ebs_recommendations
    failed_regions.extend(ebs_failed)

    lambda_recommendations, lambda_failed = collect_lambda_recommendations(active_regions)
    all_recommendations['Lambda'] = lambda_recommendations
    failed_regions.extend(lambda_failed)

    ecs_recommendations, ecs_failed = collect_ecs_recommendations(active_regions)
    all_recommendations['ECS'] = ecs_recommendations
    failed_regions.extend(ecs_failed)

    # Print summary
    print("\n=== RECOMMENDATIONS SUMMARY ===")
    print(f"EC2 Instance recommendations: {len(all_recommendations['EC2'])}")
    print(f"Auto Scaling Group recommendations: {len(all_recommendations['ASG'])}")
    print(f"EBS Volume recommendations: {len(all_recommendations['EBS'])}")
    print(f"Lambda Function recommendations: {len(all_recommendations['Lambda'])}")
    print(f"ECS Service recommendations: {len(all_recommendations['ECS'])}")

    return all_recommendations, failed_regions

def main():
    """
    Main function to run the script.
    """
    try:
        # Check partition availability
        partition = utils.detect_partition()
        if not utils.is_service_available_in_partition("compute-optimizer", partition):
            utils.log_warning("Compute Optimizer is not available in AWS GovCloud. Skipping.")
            sys.exit(0)

        # Check dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)
        global pd
        import pandas as pd

        # Print title and get account info
        utils.setup_logging("compute-optimizer-export")
        account_id, account_name = utils.print_script_banner("AWS COMPUTE OPTIMIZER RECOMMENDATIONS EXPORT")

        # Validate AWS credentials
        try:
            # Test AWS credentials
            sts = utils.get_boto3_client('sts')
            sts.get_caller_identity()
            utils.log_success("AWS credentials validated")

        except Exception:
            utils.log_error("AWS credentials not found or invalid. Please configure your credentials.")
            print("  - AWS CLI: aws configure")
            print("  - Environment variables: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
            print("  - IAM role (if running on EC2)")
            sys.exit(1)

        # Check if Compute Optimizer is enabled. This is a courtesy pre-flight
        # gate for interactive users only — under AUTO_RUN (bundle / --run-all /
        # --org-scan) we proceed and let the API surface a real failure via the
        # fail-loud collection path, rather than silently skipping the export.
        if not utils.is_auto_run():
            print("\n" + "="*70)
            print("IMPORTANT: AWS Compute Optimizer must be opted-in to get recommendations.")
            print("Visit the AWS Compute Optimizer console to enable it if not already enabled.")
            print("="*70)

            if not utils.prompt_for_confirmation("Have you enabled Compute Optimizer?", default=False):
                print("Please enable Compute Optimizer first, then run this script again.")
                return

        # Get recommendations for all regions. Region failures across any of
        # the five recommendation scopes are collected into failed_regions
        # rather than silently collapsed into "no recommendations".
        print("\nGetting AWS Compute Optimizer recommendations...")
        utils.log_info("Starting Compute Optimizer recommendations collection")
        all_recommendations, failed_regions = get_recommendations_for_all_regions()

        # Export whatever succeeded — a partial export is required even when
        # some regions failed (see the silent-collection-failure blast-radius
        # audit).
        print("\nExporting recommendations to Excel...")
        output_path = export_recommendations_to_excel(all_recommendations, account_name)

        if output_path:
            print("\nExport completed successfully!")
            print(f"Recommendations exported to: {output_path}")
            utils.log_success(f"Compute Optimizer recommendations exported to: {output_path}")
        elif not failed_regions:
            # Genuinely empty account: every scanned region succeeded (or
            # none were active) and returned nothing.
            print("\nNo recommendations were exported. Please check if Compute Optimizer is enabled for your account.")
            utils.log_warning("No Compute Optimizer recommendations found")

        # If ANY region failed any of the five recommendation scopes, make it
        # loud: write a marker and exit non-zero, even if some data was
        # exported. A partial export that looks complete is exactly the
        # failure mode this guards against.
        if failed_regions:
            utils.report_collection_failures(account_name, 'compute-optimizer', failed_regions)
            print(
                "\nERROR: Compute Optimizer export completed with failures — data is incomplete. "
                "See the *-compute-optimizer-FAILED-*.txt marker in the output directory."
            )
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An error occurred during script execution", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
