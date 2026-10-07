"""
Service-to-Script Mapping Database

Maps AWS service names from services-in-use export to their corresponding
StratusScan export scripts. Includes service aliases and categorization.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Special category: Always recommended for security/compliance audits
ALWAYS_RUN_SCRIPTS = [
    "iam_export.py",
    "cloudtrail_export.py",
    "config_export.py",
    "guardduty_export.py",
    "vpc_data_export.py",
    "security_groups_export.py",
    "nacl_export.py",
    "route_tables_export.py",
    "trusted_advisor_cost_optimization_export.py",
    "budgets_export.py",
    # Every commercial account has a bill, so billing is always worth pulling —
    # it doubles as a ground-truth cross-check on what is actually running.
    # The script skips itself cleanly in GovCloud (no Cost Explorer) and on
    # missing Cost Explorer permissions, so making it mandatory is safe.
    "billing_export.py",
]

# Service name aliases and variations
# Maps common service name variations to canonical names
SERVICE_ALIASES: dict[str, str] = {
    # EC2 and Compute
    "ec2": "Amazon Elastic Compute Cloud",
    "amazon ec2": "Amazon Elastic Compute Cloud",
    "elastic compute cloud": "Amazon Elastic Compute Cloud",
    "lambda": "AWS Lambda",
    "eks": "Amazon Elastic Kubernetes Service",
    "kubernetes": "Amazon Elastic Kubernetes Service",
    "ecs": "Amazon Elastic Container Service",
    "fargate": "AWS Fargate",
    "app runner": "AWS App Runner",
    "apprunner": "AWS App Runner",
    "workspaces": "Amazon WorkSpaces",
    "amazon workspaces": "Amazon WorkSpaces",
    "workspaces personal": "Amazon WorkSpaces",

    # Storage
    "s3": "Amazon Simple Storage Service",
    "amazon s3": "Amazon Simple Storage Service",
    "simple storage service": "Amazon Simple Storage Service",
    "ebs": "Amazon Elastic Block Store",
    "elastic block store": "Amazon Elastic Block Store",
    "efs": "Amazon Elastic File System",
    "elastic file system": "Amazon Elastic File System",
    "fsx": "Amazon FSx",
    "glacier": "Amazon S3 Glacier",
    "s3 glacier": "Amazon S3 Glacier",
    "storage gateway": "AWS Storage Gateway",
    "backup": "AWS Backup",

    # Database
    "rds": "Amazon Relational Database Service",
    "relational database service": "Amazon Relational Database Service",
    "dynamodb": "Amazon DynamoDB",
    "elasticache": "Amazon ElastiCache",
    "redshift": "Amazon Redshift",
    "neptune": "Amazon Neptune",
    "documentdb": "Amazon DocumentDB",
    "aurora": "Amazon Aurora",

    # Networking
    "vpc": "Amazon Virtual Private Cloud",
    "virtual private cloud": "Amazon Virtual Private Cloud",
    "cloudfront": "Amazon CloudFront",
    "route 53": "Amazon Route 53",
    "route53": "Amazon Route 53",
    "elb": "Elastic Load Balancing",
    "elastic load balancing": "Elastic Load Balancing",
    "alb": "Elastic Load Balancing",
    "nlb": "Elastic Load Balancing",
    "direct connect": "AWS Direct Connect",
    "directconnect": "AWS Direct Connect",
    "vpn": "AWS Virtual Private Network",
    "transit gateway": "AWS Transit Gateway",
    "global accelerator": "AWS Global Accelerator",
    "api gateway": "Amazon API Gateway",
    "apigateway": "Amazon API Gateway",

    # Security & Identity
    "iam": "AWS Identity and Access Management",
    "identity and access management": "AWS Identity and Access Management",
    "cognito": "Amazon Cognito",
    "secrets manager": "AWS Secrets Manager",
    "secretsmanager": "AWS Secrets Manager",
    "kms": "AWS Key Management Service",
    "key management service": "AWS Key Management Service",
    "acm": "AWS Certificate Manager",
    "certificate manager": "AWS Certificate Manager",
    "guardduty": "Amazon GuardDuty",
    "macie": "Amazon Macie",
    "security hub": "AWS Security Hub",
    "securityhub": "AWS Security Hub",
    "waf": "AWS WAF",
    "shield": "AWS Shield",
    "network firewall": "AWS Network Firewall",
    "detective": "Amazon Detective",
    "access analyzer": "AWS IAM Access Analyzer",

    # Management & Governance
    "cloudwatch": "Amazon CloudWatch",
    "amazon cloudwatch logs": "Amazon CloudWatch",
    "cloudtrail": "AWS CloudTrail",
    "config": "AWS Config",
    "systems manager": "AWS Systems Manager",
    "ssm": "AWS Systems Manager",
    "organizations": "AWS Organizations",
    "control tower": "AWS Control Tower",
    "service catalog": "AWS Service Catalog",
    "trusted advisor": "AWS Trusted Advisor",

    # Application Integration
    "sns": "Amazon Simple Notification Service",
    "simple notification service": "Amazon Simple Notification Service",
    "sqs": "Amazon Simple Queue Service",
    "simple queue service": "Amazon Simple Queue Service",
    "eventbridge": "Amazon EventBridge",
    "step functions": "AWS Step Functions",
    "stepfunctions": "AWS Step Functions",

    # Analytics
    "athena": "Amazon Athena",
    "glue": "AWS Glue",
    "opensearch": "Amazon OpenSearch Service",
    "elasticsearch": "Amazon OpenSearch Service",

    # Developer Tools
    "codecommit": "AWS CodeCommit",
    "codebuild": "AWS CodeBuild",
    "codedeploy": "AWS CodeDeploy",
    "codepipeline": "AWS CodePipeline",

    # Machine Learning
    "sagemaker": "Amazon SageMaker",
    "bedrock": "Amazon Bedrock",
    "comprehend": "Amazon Comprehend",
    "rekognition": "Amazon Rekognition",

    # Containers
    "ecr": "Amazon Elastic Container Registry",
    "elastic container registry": "Amazon Elastic Container Registry",
    "elastic container service": "Amazon Elastic Container Service",
    "elastic kubernetes service": "Amazon Elastic Kubernetes Service",

    # Short "marketing" names emitted by the service-discovery catalog in
    # services_in_use_export.py. The catalog keys on friendly names (e.g.
    # "Amazon RDS") for human-readable reports, while SERVICE_SCRIPT_MAP keys
    # on full canonical names. Alias lookup is case-insensitive (input is
    # lowercased), so these MUST be lowercase. Without them, discovered
    # services map to zero scripts and are silently dropped from Deep Scan.
    # The test_discovery_catalog_resolves guard keeps this list in sync.
    "amazon rds": "Amazon Relational Database Service",
    "amazon ecs": "Amazon Elastic Container Service",
    "amazon eks": "Amazon Elastic Kubernetes Service",
    "amazon ebs": "Amazon Elastic Block Store",
    "amazon efs": "Amazon Elastic File System",
    "amazon glacier": "Amazon S3 Glacier",
    "amazon vpc": "Amazon Virtual Private Cloud",
    "aws vpn": "AWS Virtual Private Network",
    "aws iam": "AWS Identity and Access Management",
    "aws kms": "AWS Key Management Service",
    "amazon opensearch": "Amazon OpenSearch Service",
    "amazon sns": "Amazon Simple Notification Service",
    "amazon sqs": "Amazon Simple Queue Service",
    "amazon appsync": "AWS AppSync",
}

# Primary mapping: Service name → list of export scripts
# Only references scripts that exist on disk. Entries for services whose
# scripts have not yet been created are omitted until those scripts are added.
SERVICE_SCRIPT_MAP: dict[str, list[str]] = {
    # Compute Services
    "Amazon Elastic Compute Cloud": [
        "ec2_export.py",
        "ami_export.py",
        "autoscaling_export.py",
        "ebs_volumes_export.py",
        "ebs_snapshots_export.py",
        "ec2_capacity_reservations_export.py",
        "ec2_dedicated_hosts_export.py",
    ],
    "AWS Lambda": ["lambda_export.py"],
    "Amazon Elastic Kubernetes Service": ["eks_export.py"],
    "Amazon Elastic Container Service": ["ecs_export.py"],
    "Amazon Elastic Container Registry": ["ecr_export.py"],
    "AWS Fargate": ["ecs_export.py"],  # Fargate is ECS launch type
    "Amazon EC2 Auto Scaling": ["autoscaling_export.py"],
    "AWS Elastic Beanstalk": ["elasticbeanstalk_export.py"],
    "AWS App Runner": ["apprunner_export.py"],
    "Amazon WorkSpaces": ["workspaces_export.py"],

    # Storage Services
    "Amazon Simple Storage Service": [
        "s3_export.py",
        "s3_accesspoints_export.py",
    ],
    "Amazon Elastic Block Store": [
        "ebs_volumes_export.py",
        "ebs_snapshots_export.py",
    ],
    "Amazon Elastic File System": ["efs_export.py"],
    "Amazon FSx": ["fsx_export.py"],
    "Amazon S3 Glacier": ["glacier_export.py"],
    "AWS Storage Gateway": ["storagegateway_export.py"],
    "AWS Backup": ["backup_export.py"],
    "AWS DataSync": ["datasync_export.py"],
    "AWS Transfer Family": ["transfer_family_export.py"],

    # Database Services
    "Amazon Relational Database Service": ["rds_export.py"],
    "Amazon Aurora": ["rds_export.py"],  # Aurora is part of RDS
    "Amazon DynamoDB": ["dynamodb_export.py"],
    "Amazon ElastiCache": ["elasticache_export.py"],
    "Amazon Redshift": ["redshift_export.py"],
    "Amazon Neptune": ["neptune_export.py"],
    "Amazon DocumentDB": ["documentdb_export.py"],

    # Networking & Content Delivery
    "Amazon Virtual Private Cloud": [
        "vpc_data_export.py",
        "security_groups_export.py",
        "nacl_export.py",
        "route_tables_export.py",
    ],
    "Amazon CloudFront": ["cloudfront_export.py"],
    "Amazon Route 53": ["route53_export.py"],
    "Elastic Load Balancing": ["elb_export.py"],
    "AWS Direct Connect": ["directconnect_export.py"],
    "AWS Virtual Private Network": ["vpn_export.py"],
    "AWS Transit Gateway": ["transit_gateway_export.py"],
    "AWS Global Accelerator": ["globalaccelerator_export.py"],
    "Amazon API Gateway": ["api_gateway_export.py"],
    "AWS Cloud Map": ["cloudmap_export.py"],
    "AWS PrivateLink": ["vpc_data_export.py"],
    "AWS Client VPN": ["vpn_export.py"],
    "AWS Verified Access": ["verifiedaccess_export.py"],
    "AWS Network Manager": ["network_manager_export.py"],

    # Security, Identity & Compliance
    "AWS Identity and Access Management": [
        "iam_export.py",
        "iam_identity_providers_export.py",
        "iam_rolesanywhere_export.py",
    ],
    "AWS IAM Identity Center": [
        "iam_identity_center_export.py",
    ],
    "Amazon Cognito": ["cognito_export.py"],
    "AWS Secrets Manager": ["secrets_manager_export.py"],
    "AWS Key Management Service": ["kms_export.py"],
    "AWS Certificate Manager": ["acm_export.py"],
    "AWS Certificate Manager Private Certificate Authority": ["acm_privateca_export.py"],
    "Amazon GuardDuty": ["guardduty_export.py"],
    "Amazon Macie": ["macie_export.py"],
    "AWS Security Hub": ["security_hub_export.py"],
    "AWS WAF": ["waf_export.py"],
    "AWS Shield": ["shield_export.py"],
    "AWS Network Firewall": ["network_firewall_export.py"],
    "Amazon Detective": ["detective_export.py"],
    "AWS IAM Access Analyzer": ["access_analyzer_export.py"],
    "Amazon Verified Permissions": ["verifiedpermissions_export.py"],

    # Management & Governance
    "Amazon CloudWatch": ["cloudwatch_export.py"],
    "AWS CloudTrail": ["cloudtrail_export.py"],
    "AWS Config": ["config_export.py"],
    "AWS Systems Manager": ["ssm_fleet_export.py"],
    "AWS Organizations": ["organizations_export.py"],
    "AWS Control Tower": ["controltower_export.py"],
    "AWS Service Catalog": ["service_catalog_export.py"],
    "AWS Trusted Advisor": ["trusted_advisor_cost_optimization_export.py"],
    "AWS Budgets": ["budgets_export.py"],
    "AWS License Manager": ["license_manager_export.py"],
    "AWS Health": ["health_export.py"],
    "AWS CloudFormation": ["cloudformation_export.py"],
    "AWS Compute Optimizer": ["compute_optimizer_export.py"],
    "AWS Cost Anomaly Detection": ["cost_anomaly_detection_export.py"],
    "AWS Cost Categories": ["cost_categories_export.py"],
    "Cost Optimization Hub": ["cost_optimization_hub_export.py"],
    "Savings Plans": ["savings_plans_export.py"],
    "AWS Marketplace": ["marketplace_export.py"],
    "Amazon EC2 Image Builder": ["image_builder_export.py"],

    # Cost & Billing
    "AWS Billing": ["billing_export.py"],
    "AWS Reserved Instances": ["reserved_instances_export.py"],
    "AWS Services In Use": ["services_in_use_export.py"],

    # Application Integration
    "Amazon Simple Notification Service": ["sqs_sns_export.py"],
    "Amazon Simple Queue Service": ["sqs_sns_export.py"],
    "Amazon EventBridge": ["eventbridge_export.py"],
    "AWS Step Functions": ["stepfunctions_export.py"],

    # Analytics
    "Amazon Athena": ["glue_athena_export.py"],
    "AWS Glue": ["glue_athena_export.py"],
    "Amazon OpenSearch Service": ["opensearch_export.py"],
    "AWS Lake Formation": ["lakeformation_export.py"],

    # Developer Tools
    "AWS CodeCommit": ["codecommit_export.py"],
    "AWS CodeBuild": ["codebuild_export.py"],
    "AWS CodeDeploy": ["codedeploy_export.py"],
    "AWS CodePipeline": ["codepipeline_export.py"],
    "AWS X-Ray": ["xray_export.py"],

    # Machine Learning
    "Amazon SageMaker": ["sagemaker_export.py"],
    "Amazon Bedrock": ["bedrock_export.py"],
    "Amazon Comprehend": ["comprehend_export.py"],
    "Amazon Rekognition": ["rekognition_export.py"],

    # Customer Engagement
    "Amazon Connect": ["connect_export.py"],
    "Amazon Simple Email Service": ["ses_export.py", "ses_pinpoint_export.py"],
    "Amazon Pinpoint": ["ses_pinpoint_export.py"],

    # Front-End Web & Mobile
    "AWS AppSync": ["appsync_export.py"],
}

# Script categories for organization
SCRIPT_CATEGORIES: dict[str, list[str]] = {
    "Security & Compliance": [
        "iam_export.py",
        "guardduty_export.py",
        "security_hub_export.py",
        "cloudtrail_export.py",
        "config_export.py",
        "macie_export.py",
        "access_analyzer_export.py",
        "detective_export.py",
        "waf_export.py",
        "network_firewall_export.py",
        "shield_export.py",
        "security_groups_export.py",
        "nacl_export.py",
        "verifiedpermissions_export.py",
        "verifiedaccess_export.py",
        "iam_identity_center_export.py",
    ],
    "Compute": [
        "ec2_export.py",
        "lambda_export.py",
        "eks_export.py",
        "ecs_export.py",
        "autoscaling_export.py",
        "elasticbeanstalk_export.py",
        "apprunner_export.py",
        "workspaces_export.py",
        "compute_resources.py",
        "ec2_capacity_reservations_export.py",
        "ec2_dedicated_hosts_export.py",
    ],
    "Storage": [
        "s3_export.py",
        "ebs_volumes_export.py",
        "ebs_snapshots_export.py",
        "efs_export.py",
        "fsx_export.py",
        "glacier_export.py",
        "storagegateway_export.py",
        "backup_export.py",
        "datasync_export.py",
        "storage_resources.py",
    ],
    "Database": [
        "rds_export.py",
        "dynamodb_export.py",
        "elasticache_export.py",
        "redshift_export.py",
        "neptune_export.py",
        "documentdb_export.py",
    ],
    "Networking": [
        "vpc_data_export.py",
        "elb_export.py",
        "cloudfront_export.py",
        "route53_export.py",
        "directconnect_export.py",
        "vpn_export.py",
        "transit_gateway_export.py",
        "api_gateway_export.py",
        "route_tables_export.py",
        "globalaccelerator_export.py",
        "network_resources.py",
        "network_manager_export.py",
    ],
    "Cost Management": [
        "budgets_export.py",
        "trusted_advisor_cost_optimization_export.py",
        "cost_anomaly_detection_export.py",
        "cost_categories_export.py",
        "cost_optimization_hub_export.py",
        "savings_plans_export.py",
        "billing_export.py",
        "reserved_instances_export.py",
    ],
    "Management & Monitoring": [
        "cloudwatch_export.py",
        "ssm_fleet_export.py",
        "organizations_export.py",
        "controltower_export.py",
        "service_catalog_export.py",
        "cloudformation_export.py",
        "compute_optimizer_export.py",
        "services_in_use_export.py",
        "health_export.py",
        "license_manager_export.py",
        "marketplace_export.py",
    ],
    "Analytics": [
        "glue_athena_export.py",
        "opensearch_export.py",
        "lakeformation_export.py",
    ],
    "Machine Learning": [
        "sagemaker_export.py",
        "bedrock_export.py",
        "comprehend_export.py",
        "rekognition_export.py",
    ],
    "Developer Tools": [
        "codecommit_export.py",
        "codebuild_export.py",
        "codedeploy_export.py",
        "codepipeline_export.py",
        "xray_export.py",
    ],
    "Application Integration": [
        "sqs_sns_export.py",
        "eventbridge_export.py",
        "stepfunctions_export.py",
    ],
    "Business Applications": [
        "ses_export.py",
        "ses_pinpoint_export.py",
        "connect_export.py",
    ],
}


def get_all_scripts() -> set[str]:
    """Get a set of all unique script names from the mapping."""
    scripts = set()
    for script_list in SERVICE_SCRIPT_MAP.values():
        scripts.update(script_list)
    return scripts


def get_canonical_service_name(service_name: str) -> str:
    """
    Convert a service name to its canonical form.

    Args:
        service_name: Service name (may be an alias or variation)

    Returns:
        Canonical service name, or original if no alias found
    """
    # Try exact match first
    if service_name in SERVICE_SCRIPT_MAP:
        return service_name

    # Try case-insensitive lookup in aliases
    normalized = service_name.lower().strip()
    if normalized in SERVICE_ALIASES:
        return SERVICE_ALIASES[normalized]

    # Return original if no match
    return service_name


def get_scripts_for_service(service_name: str) -> list[str]:
    """
    Get export scripts for a given service name.

    Args:
        service_name: AWS service name (canonical or alias)

    Returns:
        List of export script filenames, empty list if service not found
    """
    canonical = get_canonical_service_name(service_name)
    return SERVICE_SCRIPT_MAP.get(canonical, [])


def get_category_for_script(script_name: str) -> str:
    """
    Get the category for a given script.

    Args:
        script_name: Export script filename

    Returns:
        Category name, or "Other" if not categorized
    """
    for category, scripts in SCRIPT_CATEGORIES.items():
        if script_name in scripts:
            return category
    return "Other"


def validate_script_mappings(scripts_dir: Path = None) -> dict[str, list[str]]:
    """
    Validate that all scripts referenced in SERVICE_SCRIPT_MAP exist on disk.

    Logs a warning for each script referenced in the map that cannot be found
    in the scripts directory. This function is safe to call at import time.

    Args:
        scripts_dir: Path to the scripts directory. If None, resolves relative
                     to this file's grandparent (i.e., stratusscancli-aws/scripts/).

    Returns:
        Dict with keys 'missing' and 'found', each containing a list of
        script filenames.
    """
    if scripts_dir is None:
        # mapping.py lives at scripts/smart_scan/mapping.py
        # so the scripts dir is two levels up from this file
        scripts_dir = Path(__file__).parent.parent

    result: dict[str, list[str]] = {"missing": [], "found": []}

    for script in get_all_scripts():
        script_path = scripts_dir / script
        if script_path.exists():
            result["found"].append(script)
        else:
            result["missing"].append(script)
            logger.debug(
                "SERVICE_SCRIPT_MAP references script that does not exist on disk: %s "
                "(expected at %s)",
                script,
                script_path,
            )

    if result["missing"]:
        logger.warning(
            "validate_script_mappings: %d script(s) missing from disk out of %d mapped. "
            "These entries will not produce results during smart scan.",
            len(result["missing"]),
            len(result["found"]) + len(result["missing"]),
        )
    else:
        logger.debug(
            "validate_script_mappings: all %d mapped scripts found on disk.",
            len(result["found"]),
        )

    return result


# Run validation at import time so stale mappings surface immediately in logs.
# This is a non-fatal check — warnings are emitted but execution continues.
_validation_result = validate_script_mappings()
