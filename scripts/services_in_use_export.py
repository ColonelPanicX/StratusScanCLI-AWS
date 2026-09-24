#!/usr/bin/env python3
"""
AWS Services In Use Discovery Script for StratusScan

Discovers all AWS services currently in use by checking for actual resources
across your AWS environment. Provides categorized, detailed inventory with
resource counts and regional distribution.

Features:
- Leverages all 105+ StratusScan export scripts for accurate detection
- Categorized output (Compute, Storage, Network, Security, etc.)
- Resource counts (e.g., "15 EC2 instances" not just "EC2: Yes")
- Regional distribution for each service
- Fast concurrent scanning
- Human-readable categorized output

Output: Multi-worksheet Excel file with services categorized by type
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from botocore.config import Config as BotocoreConfig

try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Discover AWS services in use and export inventory to Excel")

# Setup logging
logger = utils.setup_logging('services-in-use-export')


# ---------------------------------------------------------------------------
# Detector helpers
#
# A detector's job is only to answer "is this service in use, and roughly how
# much?" — not to collect data (that's the exporter's job). Counts are capped
# for display by DISCOVERY_CAP, so these helpers stop early once the cap is
# exceeded. Reference DISCOVERY_CAP (defined below) lazily at call time.
# ---------------------------------------------------------------------------
def _count_paginated(client, operation: str, key: str) -> int:
    """Count items across all pages of a list/describe operation (cap-aware)."""
    total = 0
    for page in client.get_paginator(operation).paginate():
        total += len(page.get(key, []) or [])
        if total > DISCOVERY_CAP:
            break
    return total


def _count_simple(client, operation: str, key: str) -> int:
    """Count items from a single (non-paginated) list/describe call."""
    return len(getattr(client, operation)().get(key, []) or [])


def _shield_check(c, r) -> int:
    """Shield Advanced is in use only when an active subscription exists.

    describe_subscription raises ResourceNotFoundException when Shield Advanced
    has never been subscribed — that is a clean 'not in use', not an error.
    """
    try:
        return 1 if c.describe_subscription().get('Subscription') else 0
    except Exception as e:
        if 'resourcenotfound' in str(e).lower():
            return 0
        raise


def _compute_optimizer_check(c, r) -> int:
    """Compute Optimizer is in use only when the account is opted in (Active)."""
    return 1 if c.get_enrollment_status().get('status') == 'Active' else 0


def _cost_optimization_hub_check(c, r) -> int:
    """Count accounts with an Active Cost Optimization Hub enrollment."""
    total = 0
    for page in c.get_paginator('list_enrollment_statuses').paginate():
        total += sum(1 for i in page.get('items', []) if i.get('status') == 'Active')
    return total


def _marketplace_check(c, r) -> int:
    """Best-effort detection of accepted AWS Marketplace agreements.

    The Marketplace agreement API is restrictive and easy to mis-query; any
    failure is treated as 'not detected' here. The bill cross-check is the
    authoritative signal for Marketplace spend (see follow-up).
    """
    try:
        resp = c.search_agreements(
            catalog='AWSMarketplace',
            filters=[{'name': 'PartyType', 'values': ['Proposer']}],
            maxResults=20,
        )
        return len(resp.get('agreementViewSummaries', []) or [])
    except Exception as e:
        utils.log_debug(f"Marketplace agreement check skipped: {e}")
        return 0


# Service detection configuration - maps to your export scripts
SERVICE_CHECKS = {
    'Compute Resources': {
        'Amazon Elastic Container Registry': {
            'client': 'ecr',
            'check': lambda c, r: _count_paginated(c, 'describe_repositories', 'repositories'),
            'unit': 'repositories',
            'regional': True
        },
        'AWS Elastic Beanstalk': {
            'client': 'elasticbeanstalk',
            'check': lambda c, r: _count_simple(c, 'describe_applications', 'Applications'),
            'unit': 'applications',
            'regional': True
        },
        'Amazon EC2 Image Builder': {
            'client': 'imagebuilder',
            'check': lambda c, r: _count_simple(c, 'list_image_pipelines', 'imagePipelineList'),
            'unit': 'pipelines',
            'regional': True
        },
        'Amazon EC2': {
            'client': 'ec2',
            'check': lambda c, r: sum(
                len(inst)
                for page in c.get_paginator('describe_instances').paginate(
                    Filters=[{'Name': 'instance-state-name', 'Values': ['running', 'stopped']}]
                )
                for inst in [res['Instances'] for res in page['Reservations']]
            ),
            'detail': lambda c, r: {
                state: sum(
                    1 for page in c.get_paginator('describe_instances').paginate(
                        Filters=[{'Name': 'instance-state-name', 'Values': [state]}]
                    )
                    for res in page['Reservations']
                    for _ in res['Instances']
                )
                for state in ['running', 'stopped']
            },
            'unit': 'instances',
            'regional': True
        },
        'Amazon RDS': {
            'client': 'rds',
            'check': lambda c, r: sum(
                len(page['DBInstances'])
                for page in c.get_paginator('describe_db_instances').paginate()
            ),
            'detail': lambda c, r: {
                status: count
                for status, count in {
                    s: sum(
                        1 for page in c.get_paginator('describe_db_instances').paginate()
                        for db in page['DBInstances']
                        if db['DBInstanceStatus'] == s
                    )
                    for s in ['available', 'stopped']
                }.items()
                if count > 0
            },
            'unit': 'databases',
            'regional': True
        },
        'AWS Lambda': {
            'client': 'lambda',
            'check': lambda c, r: sum(
                len(page['Functions'])
                for page in c.get_paginator('list_functions').paginate()
            ),
            'detail': lambda c, r: {
                rt: count
                for rt, count in {
                    'Python': sum(
                        1 for page in c.get_paginator('list_functions').paginate()
                        for fn in page['Functions']
                        if fn.get('Runtime', '').startswith('python')
                    ),
                    'Node.js': sum(
                        1 for page in c.get_paginator('list_functions').paginate()
                        for fn in page['Functions']
                        if fn.get('Runtime', '').startswith('nodejs')
                    ),
                    'Java': sum(
                        1 for page in c.get_paginator('list_functions').paginate()
                        for fn in page['Functions']
                        if fn.get('Runtime', '').startswith('java')
                    ),
                    'Go': sum(
                        1 for page in c.get_paginator('list_functions').paginate()
                        for fn in page['Functions']
                        if fn.get('Runtime', '').startswith('go')
                    ),
                    'Other': sum(
                        1 for page in c.get_paginator('list_functions').paginate()
                        for fn in page['Functions']
                        if not any(
                            fn.get('Runtime', '').startswith(p)
                            for p in ('python', 'nodejs', 'java', 'go')
                        )
                    ),
                }.items()
                if count > 0
            },
            'unit': 'functions',
            'regional': True
        },
        'Amazon ECS': {
            'client': 'ecs',
            'check': lambda c, r: len([cl for cl in c.list_clusters()['clusterArns'] if cl]),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon EKS': {
            'client': 'eks',
            'check': lambda c, r: len(c.list_clusters()['clusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon Lightsail': {
            'client': 'lightsail',
            'check': lambda c, r: len(c.get_instances()['instances']),
            'unit': 'instances',
            'regional': True
        },
        'AWS Batch': {
            'client': 'batch',
            'check': lambda c, r: len(c.describe_compute_environments()['computeEnvironments']),
            'unit': 'compute environments',
            'regional': True
        },
    },
    'Storage Resources': {
        'AWS DataSync': {
            'client': 'datasync',
            'check': lambda c, r: _count_paginated(c, 'list_tasks', 'Tasks'),
            'unit': 'tasks',
            'regional': True
        },
        'AWS Transfer Family': {
            'client': 'transfer',
            'check': lambda c, r: _count_paginated(c, 'list_servers', 'Servers'),
            'unit': 'servers',
            'regional': True
        },
        'Amazon S3': {
            'client': 's3',
            'check': lambda c, r: len(c.list_buckets()['Buckets']),
            'unit': 'buckets',
            'regional': False
        },
        'Amazon EBS': {
            'client': 'ec2',
            'check': lambda c, r: len(c.describe_volumes()['Volumes']),
            'detail': lambda c, r: {
                'in-use': sum(
                    1 for page in c.get_paginator('describe_volumes').paginate(
                        Filters=[{'Name': 'status', 'Values': ['in-use']}]
                    )
                    for _ in page['Volumes']
                ),
                'available': sum(
                    1 for page in c.get_paginator('describe_volumes').paginate(
                        Filters=[{'Name': 'status', 'Values': ['available']}]
                    )
                    for _ in page['Volumes']
                ),
            },
            'unit': 'volumes',
            'regional': True
        },
        'Amazon EFS': {
            'client': 'efs',
            'check': lambda c, r: len(c.describe_file_systems()['FileSystems']),
            'unit': 'file systems',
            'regional': True
        },
        'Amazon FSx': {
            'client': 'fsx',
            'check': lambda c, r: len(c.describe_file_systems()['FileSystems']),
            'unit': 'file systems',
            'regional': True
        },
        'AWS Backup': {
            'client': 'backup',
            'check': lambda c, r: len(c.list_backup_vaults()['BackupVaultList']),
            'unit': 'vaults',
            'regional': True
        },
        'Amazon Glacier': {
            'client': 'glacier',
            'check': lambda c, r: len(c.list_vaults()['VaultList']),
            'unit': 'vaults',
            'regional': True
        },
        'AWS Storage Gateway': {
            'client': 'storagegateway',
            'check': lambda c, r: len(c.list_gateways()['Gateways']),
            'unit': 'gateways',
            'regional': True
        },
    },
    'Network Resources': {
        'AWS Cloud Map': {
            'client': 'servicediscovery',
            'check': lambda c, r: _count_paginated(c, 'list_namespaces', 'Namespaces'),
            'unit': 'namespaces',
            'regional': True
        },
        'AWS Network Manager': {
            'client': 'networkmanager',
            # Global service with a single partition endpoint — boto3 routes to
            # it regardless of the client region, so a single check suffices.
            'check': lambda c, r: _count_paginated(c, 'describe_global_networks', 'GlobalNetworks'),
            'unit': 'global networks',
            'regional': False
        },
        'Amazon VPC': {
            'client': 'ec2',
            'check': lambda c, r: len([vpc for vpc in c.describe_vpcs()['Vpcs'] if not vpc.get('IsDefault', False)]),
            'unit': 'VPCs',
            'regional': True
        },
        'Elastic Load Balancing': {
            'client': 'elbv2',
            'check': lambda c, r: len(c.describe_load_balancers()['LoadBalancers']),
            'detail': lambda c, r: {
                lb_type: count
                for lb_type, count in {
                    t: sum(
                        1 for page in c.get_paginator('describe_load_balancers').paginate()
                        for lb in page['LoadBalancers']
                        if lb['Type'] == t
                    )
                    for t in ['application', 'network', 'gateway']
                }.items()
                if count > 0
            },
            'unit': 'load balancers',
            'regional': True
        },
        'Amazon CloudFront': {
            'client': 'cloudfront',
            'check': lambda c, r: len(c.list_distributions().get('DistributionList', {}).get('Items', [])),
            'unit': 'distributions',
            'regional': False
        },
        'Amazon Route 53': {
            'client': 'route53',
            'check': lambda c, r: len(c.list_hosted_zones()['HostedZones']),
            'unit': 'hosted zones',
            'regional': False
        },
        'AWS Direct Connect': {
            'client': 'directconnect',
            'check': lambda c, r: len(c.describe_connections()['connections']),
            'unit': 'connections',
            'regional': True
        },
        'AWS VPN': {
            'client': 'ec2',
            'check': lambda c, r: len(c.describe_vpn_connections()['VpnConnections']),
            'unit': 'VPN connections',
            'regional': True
        },
        'AWS Transit Gateway': {
            'client': 'ec2',
            'check': lambda c, r: len(c.describe_transit_gateways()['TransitGateways']),
            'unit': 'transit gateways',
            'regional': True
        },
        'AWS Global Accelerator': {
            'client': 'globalaccelerator',
            'check': lambda c, r: len(c.list_accelerators()['Accelerators']),
            'unit': 'accelerators',
            'regional': False
        },
        'AWS Network Firewall': {
            'client': 'network-firewall',
            'check': lambda c, r: len(c.list_firewalls()['Firewalls']),
            'unit': 'firewalls',
            'regional': True
        },
    },
    'Database Resources': {
        'Amazon DynamoDB': {
            'client': 'dynamodb',
            'check': lambda c, r: len(c.list_tables()['TableNames']),
            'unit': 'tables',
            'regional': True
        },
        'Amazon ElastiCache': {
            'client': 'elasticache',
            'check': lambda c, r: len(c.describe_cache_clusters()['CacheClusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon Redshift': {
            'client': 'redshift',
            'check': lambda c, r: len(c.describe_clusters()['Clusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon DocumentDB': {
            'client': 'docdb',
            'check': lambda c, r: len(c.describe_db_clusters()['DBClusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon Neptune': {
            'client': 'neptune',
            'check': lambda c, r: len(c.describe_db_clusters()['DBClusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon Timestream': {
            'client': 'timestream-write',
            # list_databases avoids the hanging DescribeEndpoints call.
            # Endpoint-discovery errors are caught by _is_not_in_use_error.
            'check': lambda c, r: len(
                c.list_databases(MaxResults=10).get('Databases', [])
            ),
            'unit': 'databases',
            'regional': True
        },
    },
    'Security & Identity': {
        'AWS IAM Access Analyzer': {
            'client': 'accessanalyzer',
            'check': lambda c, r: _count_paginated(c, 'list_analyzers', 'analyzers'),
            'unit': 'analyzers',
            'regional': True
        },
        'AWS Certificate Manager Private Certificate Authority': {
            'client': 'acm-pca',
            'check': lambda c, r: _count_paginated(c, 'list_certificate_authorities', 'CertificateAuthorities'),
            'unit': 'CAs',
            'regional': True
        },
        'Amazon Detective': {
            'client': 'detective',
            'check': lambda c, r: _count_simple(c, 'list_graphs', 'GraphList'),
            'unit': 'behavior graphs',
            'regional': True
        },
        'AWS Shield': {
            'client': 'shield',
            'check': _shield_check,
            'unit': 'subscription',
            'regional': False,
            'global_region': True
        },
        'AWS Verified Access': {
            # Verified Access APIs live under EC2 — there is no 'verifiedaccess'
            # boto3 client.
            'client': 'ec2',
            'check': lambda c, r: _count_paginated(c, 'describe_verified_access_instances', 'VerifiedAccessInstances'),
            'unit': 'instances',
            'regional': True
        },
        'Amazon Verified Permissions': {
            'client': 'verifiedpermissions',
            'check': lambda c, r: _count_paginated(c, 'list_policy_stores', 'policyStores'),
            'unit': 'policy stores',
            'regional': True
        },
        'AWS IAM': {
            'client': 'iam',
            'check': lambda c, r: len(c.list_users()['Users']),
            'detail': lambda c, r: {
                'roles': sum(len(page['Roles']) for page in c.get_paginator('list_roles').paginate()),
                'groups': sum(len(page['Groups']) for page in c.get_paginator('list_groups').paginate()),
            },
            'unit': 'users',
            'regional': False
        },
        'AWS IAM Identity Center': {
            'client': 'sso-admin',
            'check': lambda c, r: len(c.list_instances()['Instances']),
            'unit': 'instances',
            'regional': False
        },
        'AWS Security Hub': {
            'client': 'securityhub',
            'check': lambda c, r: 1 if c.describe_hub() else 0,
            'unit': 'enabled',
            'regional': True
        },
        'Amazon GuardDuty': {
            'client': 'guardduty',
            'check': lambda c, r: len(c.list_detectors()['DetectorIds']),
            'unit': 'detectors',
            'regional': True
        },
        'AWS WAF': {
            'client': 'wafv2',
            'check': lambda c, r: len(c.list_web_acls(Scope='REGIONAL')['WebACLs']),
            'unit': 'web ACLs',
            'regional': True
        },
        'AWS KMS': {
            'client': 'kms',
            'check': lambda c, r: len(list(c.list_keys()['Keys'])),
            'unit': 'keys',
            'regional': True
        },
        'AWS Secrets Manager': {
            'client': 'secretsmanager',
            'check': lambda c, r: len(c.list_secrets().get('SecretList', [])),
            'unit': 'secrets',
            'regional': True
        },
        'Amazon Cognito': {
            'client': 'cognito-idp',
            'check': lambda c, r: len(c.list_user_pools(MaxResults=60)['UserPools']),
            'unit': 'user pools',
            'regional': True
        },
        'AWS Certificate Manager': {
            'client': 'acm',
            'check': lambda c, r: len(c.list_certificates()['CertificateSummaryList']),
            'unit': 'certificates',
            'regional': True
        },
        'Amazon Macie': {
            'client': 'macie2',
            'check': lambda c, r: 1 if c.get_macie_session()['status'] == 'ENABLED' else 0,
            'unit': 'enabled',
            'regional': True
        },
    },
    'Management & Governance': {
        'AWS License Manager': {
            'client': 'license-manager',
            'check': lambda c, r: _count_paginated(c, 'list_license_configurations', 'LicenseConfigurations'),
            'unit': 'license configurations',
            'regional': True
        },
        'AWS Health': {
            'client': 'health',
            # Global endpoint; requires Business/Enterprise Support — a missing
            # support plan surfaces as 'subscription required' and is treated as
            # not-in-use by _is_not_in_use_error.
            'check': lambda c, r: _count_paginated(c, 'describe_events', 'events'),
            'unit': 'events',
            'regional': False,
            'global_region': True
        },
        'AWS Marketplace': {
            'client': 'marketplace-agreement',
            'check': _marketplace_check,
            'unit': 'agreements',
            'regional': False,
            'global_region': True
        },
        'AWS CloudTrail': {
            'client': 'cloudtrail',
            'check': lambda c, r: len(c.describe_trails()['trailList']),
            'unit': 'trails',
            'regional': True
        },
        'AWS Config': {
            'client': 'config',
            'check': lambda c, r: len(c.describe_configuration_recorders()['ConfigurationRecorders']),
            'unit': 'recorders',
            'regional': True
        },
        'AWS CloudFormation': {
            'client': 'cloudformation',
            'check': lambda c, r: len([s for s in c.list_stacks()['StackSummaries'] if s['StackStatus'] != 'DELETE_COMPLETE']),
            'unit': 'stacks',
            'regional': True
        },
        'AWS Systems Manager': {
            'client': 'ssm',
            'check': lambda c, r: len(c.describe_instance_information()['InstanceInformationList']),
            'unit': 'managed instances',
            'regional': True
        },
        'AWS Organizations': {
            'client': 'organizations',
            'check': lambda c, r: len(c.list_accounts()['Accounts']),
            'unit': 'accounts',
            'regional': False
        },
        'AWS Control Tower': {
            'client': 'controltower',
            'check': lambda c, r: len(c.list_enabled_controls()['enabledControls']),
            'unit': 'controls',
            'regional': False
        },
        'AWS Service Catalog': {
            'client': 'servicecatalog',
            'check': lambda c, r: len(c.list_portfolios()['PortfolioDetails']),
            'unit': 'portfolios',
            'regional': True
        },
    },
    'Analytics & Data': {
        'Amazon Athena': {
            'client': 'athena',
            'check': lambda c, r: len(c.list_work_groups()['WorkGroups']),
            'unit': 'workgroups',
            'regional': True
        },
        'AWS Glue': {
            'client': 'glue',
            'check': lambda c, r: len(c.get_databases()['DatabaseList']),
            'unit': 'databases',
            'regional': True
        },
        'Amazon EMR': {
            'client': 'emr',
            'check': lambda c, r: len(c.list_clusters()['Clusters']),
            'unit': 'clusters',
            'regional': True
        },
        'Amazon Kinesis': {
            'client': 'kinesis',
            'check': lambda c, r: len(c.list_streams()['StreamNames']),
            'unit': 'streams',
            'regional': True
        },
        'Amazon OpenSearch': {
            'client': 'opensearch',
            'check': lambda c, r: len(c.list_domain_names()['DomainNames']),
            'unit': 'domains',
            'regional': True
        },
        'AWS Lake Formation': {
            'client': 'lakeformation',
            'check': lambda c, r: len(c.list_resources()['ResourceInfoList']),
            'unit': 'resources',
            'regional': True
        },
    },
    'Integration & Messaging': {
        'Amazon Simple Email Service': {
            'client': 'sesv2',
            'check': lambda c, r: _count_simple(c, 'list_email_identities', 'EmailIdentities'),
            'unit': 'identities',
            'regional': True
        },
        'Amazon SNS': {
            'client': 'sns',
            'check': lambda c, r: len(c.list_topics()['Topics']),
            'unit': 'topics',
            'regional': True
        },
        'Amazon SQS': {
            'client': 'sqs',
            'check': lambda c, r: len(c.list_queues().get('QueueUrls', [])),
            'unit': 'queues',
            'regional': True
        },
        'Amazon EventBridge': {
            'client': 'events',
            'check': lambda c, r: len(c.list_event_buses()['EventBuses']),
            'unit': 'event buses',
            'regional': True
        },
        'AWS Step Functions': {
            'client': 'stepfunctions',
            'check': lambda c, r: len(c.list_state_machines()['stateMachines']),
            'unit': 'state machines',
            'regional': True
        },
        'Amazon API Gateway': {
            'client': 'apigateway',
            'check': lambda c, r: len(c.get_rest_apis()['items']),
            'unit': 'APIs',
            'regional': True
        },
        'Amazon AppSync': {
            'client': 'appsync',
            'check': lambda c, r: len(c.list_graphql_apis()['graphqlApis']),
            'unit': 'GraphQL APIs',
            'regional': True
        },
    },
    'Monitoring & Logging': {
        'Amazon CloudWatch': {
            'client': 'cloudwatch',
            # list_metrics returns thousands of entries and is very slow.
            # Count alarms instead — fast, meaningful, and paginator-safe.
            'check': lambda c, r: sum(
                len(page['MetricAlarms'])
                for page in c.get_paginator('describe_alarms').paginate()
            ),
            'unit': 'alarms',
            'regional': True
        },
        'AWS X-Ray': {
            'client': 'xray',
            'check': lambda c, r: len(c.get_sampling_rules()['SamplingRuleRecords']),
            'unit': 'sampling rules',
            'regional': True
        },
        'Amazon CloudWatch Logs': {
            'client': 'logs',
            'check': lambda c, r: len(c.describe_log_groups()['logGroups']),
            'unit': 'log groups',
            'regional': True
        },
    },
    'AI & Machine Learning': {
        'Amazon SageMaker': {
            'client': 'sagemaker',
            'check': lambda c, r: len(c.list_notebook_instances()['NotebookInstances']),
            'unit': 'notebook instances',
            'regional': True
        },
        'Amazon Bedrock': {
            'client': 'bedrock',
            'check': lambda c, r: len(c.list_custom_models()['modelSummaries']),
            'unit': 'custom models',
            'regional': True
        },
        'Amazon Comprehend': {
            'client': 'comprehend',
            'check': lambda c, r: len(c.list_endpoints()['EndpointPropertiesList']),
            'unit': 'endpoints',
            'regional': True
        },
        'Amazon Rekognition': {
            'client': 'rekognition',
            'check': lambda c, r: len(c.list_collections()['CollectionIds']),
            'unit': 'collections',
            'regional': True
        },
    },
    'Application Services': {
        'AWS App Runner': {
            'client': 'apprunner',
            'check': lambda c, r: len(c.list_services()['ServiceSummaryList']),
            'unit': 'services',
            'regional': True
        },
        'Amazon Connect': {
            'client': 'connect',
            'check': lambda c, r: len(c.list_instances()['InstanceSummaryList']),
            'unit': 'instances',
            'regional': True
        },
        'AWS Amplify': {
            'client': 'amplify',
            'check': lambda c, r: len(c.list_apps()['apps']),
            'unit': 'apps',
            'regional': True
        },
    },
    'Developer Tools': {
        'AWS CodeBuild': {
            'client': 'codebuild',
            'check': lambda c, r: _count_paginated(c, 'list_projects', 'projects'),
            'unit': 'projects',
            'regional': True
        },
        'AWS CodeCommit': {
            'client': 'codecommit',
            'check': lambda c, r: _count_paginated(c, 'list_repositories', 'repositories'),
            'unit': 'repositories',
            'regional': True
        },
        'AWS CodeDeploy': {
            'client': 'codedeploy',
            'check': lambda c, r: _count_paginated(c, 'list_applications', 'applications'),
            'unit': 'applications',
            'regional': True
        },
        'AWS CodePipeline': {
            'client': 'codepipeline',
            'check': lambda c, r: _count_paginated(c, 'list_pipelines', 'pipelines'),
            'unit': 'pipelines',
            'regional': True
        },
    },
    'Cost & Billing': {
        'AWS Compute Optimizer': {
            'client': 'compute-optimizer',
            'check': _compute_optimizer_check,
            'unit': 'enrollment',
            'regional': False,
            'global_region': True
        },
        'AWS Cost Anomaly Detection': {
            'client': 'ce',
            'check': lambda c, r: _count_simple(c, 'get_anomaly_monitors', 'AnomalyMonitors'),
            'unit': 'monitors',
            'regional': False,
            'global_region': True
        },
        'AWS Cost Categories': {
            'client': 'ce',
            'check': lambda c, r: _count_simple(c, 'list_cost_category_definitions', 'CostCategoryReferences'),
            'unit': 'cost categories',
            'regional': False,
            'global_region': True
        },
        'Cost Optimization Hub': {
            'client': 'cost-optimization-hub',
            'check': _cost_optimization_hub_check,
            'unit': 'enrollment',
            'regional': False,
            'global_region': True
        },
        'Savings Plans': {
            'client': 'savingsplans',
            'check': lambda c, r: len(
                c.describe_savings_plans(states=['active']).get('savingsPlans', []) or []
            ),
            'unit': 'savings plans',
            'regional': False,
            'global_region': True
        },
        'AWS Reserved Instances': {
            'client': 'ec2',
            'check': lambda c, r: len(
                c.describe_reserved_instances(
                    Filters=[{'Name': 'state', 'Values': ['active']}]
                ).get('ReservedInstances', []) or []
            ),
            'unit': 'reserved instances',
            'regional': True
        },
    },
}


# Flat map from service name → config dict for O(1) lookup in _enrich_with_detail.
_SERVICE_CONFIG_FLAT: dict[str, dict] = {
    service_name: config
    for category_services in SERVICE_CHECKS.values()
    for service_name, config in category_services.items()
}


# Per-service resource count cap for discovery display.
# Counts above this value are shown as "500+" in the console and reports.
# The actual count is still stored internally and used for totals.
DISCOVERY_CAP = 500

# Error message fragments that indicate a service is simply not in use or not
# available in this region — expected states, not genuine unexpected errors.
_NOT_IN_USE_FRAGMENTS = (
    "could not connect to the endpoint url",      # endpoint absent in this region
    "unknownoperationexception",                  # service/op not in this region
    "unknown operation",                          # Bedrock, others: region gap
    "unsupported_operation",                      # Comprehend: region gap
    "this operation is not supported in this region",
    "not subscribed to",                          # Security Hub: not enabled
    "not enabled",                                # Macie, others: not enabled
    "must create a landing zone",                 # Control Tower: not deployed
    "subscriptionrequiredexception",              # Health: no Business/Enterprise Support
    "subscription required",                       # Health: no premium support plan
    "endpoint discovery failed",                  # Timestream: endpoint issue
    "read timeout",                               # service took too long — treat as not detected
    "connect timeout",                            # connection too slow — treat as not detected
    "readtimeouterror",                           # botocore ReadTimeoutError class name
    "connecttimeouterror",                        # botocore ConnectTimeoutError class name
)

# Boto3 client config for service discovery checks: fast failure over resilience.
# Discovery checks need to know quickly whether a service has resources — if a
# service doesn't respond within 15s it's almost certainly not in use or the
# endpoint is unavailable. Export scripts still use the default 60s timeout.
_DISCOVERY_CLIENT_CONFIG = BotocoreConfig(
    connect_timeout=5,
    read_timeout=15,
    retries={'max_attempts': 2, 'mode': 'standard'},
)


def _get_discovery_client(service: str, region: str):
    """Create a boto3 client configured for fast service discovery checks."""
    session = utils.get_aws_session(region)  # honours STRATUSSCAN_ROLE_ARN / --profile
    config = _DISCOVERY_CLIENT_CONFIG
    # FIPS belongs on the botocore Config, not as a client() kwarg (boto3
    # rejects it there). GovCloud requires FIPS endpoints.
    if region and region.startswith("us-gov-"):
        config = config.merge(BotocoreConfig(use_fips_endpoint=True))
    return session.client(service, config=config)


def _is_not_in_use_error(exc: Exception) -> bool:
    """
    Return True when an exception indicates a service is not in use or not
    available in this region — not a genuine unexpected error.
    """
    msg = str(exc).lower()
    if any(frag in msg for frag in _NOT_IN_USE_FRAGMENTS):
        return True
    # Timestream: "Only existing ... customers can access the service"
    if "only existing" in msg and "customers" in msg:
        return True
    # Organizations: AccessDeniedException on ListAccounts = not an org master account
    return bool("accessdeniedexception" in msg and "listaccounts" in msg)


def _start_heartbeat(service_name: str):
    """
    Start a background thread that prints a 'still scanning' line every 5 seconds.

    Only fires if the check actually takes longer than the interval, so fast
    services never see any output. Returns (stop_event, thread) — caller must
    call stop_event.set() and thread.join() when done.
    """
    stop_event = threading.Event()
    start = time.monotonic()

    def _beat():
        while not stop_event.wait(timeout=5):
            elapsed = int(time.monotonic() - start)
            print(f"    ... {service_name}: still scanning ({elapsed}s elapsed)", flush=True)

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    return stop_event, t


def _stop_heartbeat(stop_event, thread) -> None:
    """Signal the heartbeat thread to stop and wait for it to exit."""
    stop_event.set()
    thread.join()


def check_service_in_region(service_name: str, config: dict, region: str) -> tuple[str, Any, str, Any]:
    """
    Check if a service has resources in a specific region.

    Returns:
        Tuple of (service_name, count, region, error_msg)
        count is None on a genuine unexpected failure; error_msg is None on
        success or on a recognized "not in use / not available" condition.
    """
    try:
        client = _get_discovery_client(config['client'], region)
        count = config['check'](client, region)
        return (service_name, count, region, None)
    except Exception as e:
        if _is_not_in_use_error(e):
            utils.log_debug(f"Service not available/not in use: {service_name} in {region}")
            return (service_name, 0, region, None)
        utils.log_warning(f"Service check failed for {service_name} in {region}: {e}")
        return (service_name, None, region, str(e))


def _enrich_with_detail(
    services: dict[str, dict[str, Any]],
    regions: list[str],
    errors: dict[str, list[str]],
) -> None:
    """
    Second pass (Deep Scan only): add asset breakdown to detected services.

    Runs only for services that were found in the count pass and have a
    'detail' lambda defined. Failures are logged at DEBUG and skipped —
    they never break the scan.
    """
    for service_name, data in services.items():
        config = _SERVICE_CONFIG_FLAT.get(service_name)
        if config is None or 'detail' not in config:
            continue

        aggregated: dict[str, int] = {}
        check_regions = list(data['regions'].keys()) if data['regional'] else regions[:1]

        for region in check_regions:
            try:
                client = utils.get_boto3_client(config['client'], region_name=region)
                result = config['detail'](client, region)
                for k, v in result.items():
                    aggregated[k] = aggregated.get(k, 0) + v
            except Exception as e:
                utils.log_debug(f"Detail check skipped for {service_name} in {region}: {e}")

        if aggregated:
            data['detail'] = aggregated


def discover_services(
    regions: list[str],
    mode: str = 'quick',
    errors_out=None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """
    Discover all services in use across regions using concurrent scanning.

    Args:
        regions: List of AWS region names to scan.
        mode: Scan mode — 'quick' (counts only) or 'deep' (counts + detail breakdown).
        errors_out: Optional dict to update with errors (for backward-compat callers).
                    If provided, it is updated in-place with the same data as the
                    returned errors dict.

    Returns:
        Tuple of (services, errors) where services maps service names to their
        details and errors maps service names to lists of regional error strings.
    """
    utils.log_info("Starting concurrent service discovery across all categories...")

    all_services = {}
    errors: dict[str, list[str]] = {}
    total_services = sum(len(services) for services in SERVICE_CHECKS.values())
    completed = 0

    for category, services in SERVICE_CHECKS.items():
        utils.log_info(f"\n{'='*60}")
        utils.log_info(f"Scanning {category}...")
        utils.log_info(f"{'='*60}")

        for service_name, config in services.items():
            completed += 1
            progress = (completed / total_services) * 100

            # Decide which region(s) to probe for this service.
            if config['regional']:
                check_regions = regions
            elif config.get('global_region'):
                # Account-global service that only answers in the partition's
                # default region (Cost Explorer, Shield, Savings Plans, Health,
                # etc.). Probing it in an arbitrary scanned region yields false
                # negatives, so pin to the partition default.
                partition = utils.detect_partition(regions[0])
                check_regions = [utils.get_partition_default_region(partition)]
            else:
                check_regions = [regions[0]]

            # Use concurrent scanning for regional services
            if len(check_regions) > 1:
                utils.log_info(f"[{progress:5.1f}%] Checking {service_name} across {len(check_regions)} regions...")

                regional_counts = {}
                total_count = 0

                concurrent_config = utils.config_value('advanced_settings.concurrent_scanning', default={})
                max_workers = concurrent_config.get('max_workers', 3)
                stop_hb, hb_thread = _start_heartbeat(service_name)
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {
                            executor.submit(check_service_in_region, service_name, config, region): region
                            for region in check_regions
                        }

                        for future in as_completed(futures):
                            try:
                                svc_name, count, region, err_msg = future.result(timeout=30)
                            except Exception:
                                region = futures[future]
                                errors.setdefault(service_name, []).append(f"{region}: check timed out")
                                continue
                            if count is not None and count > 0:
                                regional_counts[region] = count
                                total_count += count
                            elif count is None:
                                errors.setdefault(service_name, []).append(f"{region}: {err_msg}")
                finally:
                    _stop_heartbeat(stop_hb, hb_thread)

                if total_count > 0:
                    capped = total_count > DISCOVERY_CAP
                    all_services[service_name] = {
                        'category': category,
                        'count': total_count,
                        'capped': capped,
                        'unit': config['unit'],
                        'regions': regional_counts,
                        'regional': True
                    }
                    count_str = "500+" if capped else str(total_count)
                    utils.log_success(f"  ✓ {service_name}: {count_str} {config['unit']} across {len(regional_counts)} region(s)")
            else:
                # Global service - single check
                utils.log_info(f"[{progress:5.1f}%] Checking {service_name} (global service)...")
                stop_hb, hb_thread = _start_heartbeat(service_name)
                _, count, _, err_msg = check_service_in_region(service_name, config, check_regions[0])
                _stop_heartbeat(stop_hb, hb_thread)

                if count is not None and count > 0:
                    capped = count > DISCOVERY_CAP
                    all_services[service_name] = {
                        'category': category,
                        'count': count,
                        'capped': capped,
                        'unit': config['unit'],
                        'regions': {},
                        'regional': False
                    }
                    count_str = "500+" if capped else str(count)
                    utils.log_success(f"  ✓ {service_name}: {count_str} {config['unit']}")
                elif count is None:
                    errors.setdefault(service_name, []).append(f"{check_regions[0]}: {err_msg}")

            # Brief pause between services to avoid blasting all regions simultaneously
            time.sleep(0.1)

    if mode == 'deep':
        utils.log_info("Deep Scan: collecting asset detail...")
        _enrich_with_detail(all_services, regions, errors)

    if errors_out is not None:
        errors_out.update(errors)

    return all_services, errors


def generate_summary(services: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics."""
    summary = []

    # Overall stats
    total_services = len(services)
    total_resources = sum(s['count'] for s in services.values())

    summary.append({
        'Metric': 'Total Services In Use',
        'Value': total_services,
        'Details': f'{total_resources:,} total resources across all services'
    })

    # By category
    category_counts = {}
    for service_data in services.values():
        category = service_data['category']
        category_counts[category] = category_counts.get(category, 0) + 1

    for category in sorted(category_counts.keys()):
        count = category_counts[category]
        category_services = [s for s, d in services.items() if d['category'] == category]
        resource_count = sum(services[s]['count'] for s in category_services)

        summary.append({
            'Metric': category,
            'Value': count,
            'Details': f'{resource_count:,} resources'
        })

    return summary


def create_detailed_export(services: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Create detailed services DataFrame."""
    rows = []

    for service_name, data in sorted(services.items()):
        regional_dist = 'Global' if not data['regional'] else ', '.join([
            f"{region}: {count}" for region, count in sorted(data['regions'].items())
        ])

        rows.append({
            'Category': data['category'],
            'Service Name': service_name,
            'Resource Count': data['count'],
            'Unit': data['unit'],
            'Type': 'Global' if not data['regional'] else 'Regional',
            'Regional Distribution': regional_dist if data['regional'] else 'N/A'
        })

    return pd.DataFrame(rows)


def create_category_sheets(services: dict[str, dict[str, Any]]) -> dict[str, pd.DataFrame]:
    """Create separate sheets for each category."""
    sheets = {}

    for category in sorted({s['category'] for s in services.values()}):
        category_services = {
            name: data for name, data in services.items()
            if data['category'] == category
        }

        rows = []
        for service_name, data in sorted(category_services.items()):
            regional_dist = 'Global' if not data['regional'] else ', '.join([
                f"{region}: {count}" for region, count in sorted(data['regions'].items())
            ])

            rows.append({
                'Service Name': service_name,
                'Resource Count': data['count'],
                'Unit': data['unit'],
                'Regional Distribution': regional_dist if data['regional'] else 'N/A'
            })

        if rows:
            sheets[category] = pd.DataFrame(rows)

    return sheets


def create_recommendations_sheet(services: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """
    Generate Smart Scan recommendations based on discovered services.

    Args:
        services: Dictionary of discovered services

    Returns:
        DataFrame with recommended export scripts
    """
    try:
        from smart_scan.mapping import ALWAYS_RUN_SCRIPTS, get_category_for_script

        from smart_scan import map_services_to_scripts

        # Extract just service names
        service_names = set(services.keys())

        # Map services to scripts
        service_script_mapping = map_services_to_scripts(service_names)

        # Build recommendations list
        recommendations = []

        # Add always-run scripts (security/compliance baseline)
        for script in sorted(ALWAYS_RUN_SCRIPTS):
            category = get_category_for_script(script)
            recommendations.append({
                'Script Name': script,
                'Category': category,
                'Priority': 'Always Run',
                'Reason': 'Security & compliance baseline - recommended for all accounts'
            })

        # Add service-based recommendations
        for service_name, scripts in sorted(service_script_mapping.items()):
            for script in sorted(scripts):
                # Skip if already added as always-run
                if script in ALWAYS_RUN_SCRIPTS:
                    continue

                category = get_category_for_script(script)
                resource_info = services.get(service_name, {})
                count = resource_info.get('count', 0)
                unit = resource_info.get('unit', 'resources')

                recommendations.append({
                    'Script Name': script,
                    'Category': category,
                    'Priority': 'Service-Based',
                    'Reason': f'{service_name} detected ({count} {unit})'
                })

        if recommendations:
            df = pd.DataFrame(recommendations)
            # Sort by priority (Always Run first) then by category
            df['_sort_priority'] = df['Priority'].map({'Always Run': 0, 'Service-Based': 1})
            df = df.sort_values(['_sort_priority', 'Category', 'Script Name'])
            df = df.drop('_sort_priority', axis=1)
            return df
        else:
            # Return empty DataFrame with proper structure
            return pd.DataFrame(columns=['Script Name', 'Category', 'Priority', 'Reason'])

    except ImportError:
        # Smart Scan not available - return empty DataFrame with note
        utils.log_warning("Smart Scan module not available - skipping recommendations")
        return pd.DataFrame([{
            'Script Name': 'N/A',
            'Category': 'N/A',
            'Priority': 'N/A',
            'Reason': 'Smart Scan module not installed'
        }])
    except Exception as e:
        utils.log_warning(f"Error generating recommendations: {e}")
        return pd.DataFrame([{
            'Script Name': 'Error',
            'Category': 'N/A',
            'Priority': 'N/A',
            'Reason': f'Error: {str(e)}'
        }])


def main():
    """Main execution function."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='AWS Services In Use Discovery with Smart Scan Integration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (default)
  python3 services-in-use-export.py

  # Auto-launch Smart Scan with interactive selection
  python3 services-in-use-export.py --smart-scan

  # Skip Smart Scan prompt entirely
  python3 services-in-use-export.py --no-smart-scan

  # Run Quick Scan (all recommended scripts) automatically
  python3 services-in-use-export.py --smart-scan --quick-scan
        """
    )

    parser.add_argument(
        '--smart-scan',
        action='store_true',
        help='Automatically launch Smart Scan after service discovery'
    )

    parser.add_argument(
        '--no-smart-scan',
        action='store_true',
        help='Skip Smart Scan prompt (discovery only)'
    )

    parser.add_argument(
        '--quick-scan',
        action='store_true',
        help='Run Quick Scan (all recommended scripts) without interactive selection'
    )

    parser.parse_args()

    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    script_name = Path(__file__).stem
    utils.setup_logging(script_name)
    utils.log_script_start(script_name)

    account_id, account_name = utils.print_script_banner("AWS SERVICES IN USE DISCOVERY EXPORT")
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Please check your credentials.")
        return

    utils.log_info(f"AWS Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Region selection — handle back/exit navigation (was previously ignored).
    regions = utils.prompt_region_selection()
    if regions in ('back', 'exit'):
        sys.exit(0)

    # Prompt for scan mode (skipped in auto-run — defaults to quick)
    if utils.is_auto_run():
        scan_mode = 'quick'
    else:
        try:
            mode_choice = utils.prompt_menu(
                "SCAN MODE",
                [
                    "Quick Scan  — service presence and resource counts",
                    "Deep Scan   — counts + asset breakdown (slower)",
                ],
            )
        except utils.BackSignal:
            sys.exit(10)
        except (utils.ExitToMainSignal, utils.QuitSignal):
            sys.exit(11)
        scan_mode = 'deep' if mode_choice == 2 else 'quick'

    # Discover services
    print(f"\nScanning {len(regions)} region(s) for services in use ({scan_mode} mode)...")
    services, errors = discover_services(regions, mode=scan_mode)

    if not services:
        utils.log_warning("No services with resources found")
        return

    utils.log_success(f"\nDiscovered {len(services)} services in use!")

    if errors:
        print(f"\n  Note: {len(errors)} service(s) had check failures (see log):")
        for svc in sorted(errors):
            utils.log_warning(f"  {svc}: {'; '.join(errors[svc])}")

    # Generate summary and export
    utils.log_info("Generating reports...")

    summary = generate_summary(services)
    df_summary = pd.DataFrame(summary)
    df_summary = utils.prepare_dataframe_for_export(df_summary)

    df_details = create_detailed_export(services)
    df_details = utils.prepare_dataframe_for_export(df_details)

    category_sheets = create_category_sheets(services)

    # Generate Smart Scan recommendations
    utils.log_info("Generating Smart Scan script recommendations...")
    df_recommendations = create_recommendations_sheet(services)
    df_recommendations = utils.prepare_dataframe_for_export(df_recommendations)

    # Combine all sheets
    dataframes = {
        'Summary': df_summary,
        'Recommended Scripts': df_recommendations,  # Add recommendations as 2nd sheet
        'All Services': df_details,
    }

    # Add category sheets
    for category, df in category_sheets.items():
        df = utils.prepare_dataframe_for_export(df)
        # Shorten sheet names to fit Excel's 31-char limit
        sheet_name = category.replace(' Resources', '').replace('&', 'and')[:31]
        dataframes[sheet_name] = df

    # Export to Excel
    region_suffix = 'all-regions' if len(regions) > 1 else regions[0]
    filename = utils.create_export_filename(account_name, 'services-in-use', region_suffix)

    utils.log_info(f"Exporting to {filename}...")
    utils.save_multiple_dataframes_to_excel(dataframes, filename)

    # Log summary
    # Print summary to console
    print("\n" + "="*60)
    print("SERVICES IN USE SUMMARY")
    print("="*60)
    for item in summary[:6]:  # Show first 6 items
        print(f"{item['Metric']:.<40} {item['Value']}")
        if item.get('Details'):
            print(f"  └─ {item['Details']}")

    # Show recommendations summary
    recommendation_count = len(df_recommendations)
    if recommendation_count > 0:
        print()
        print("="*60)
        print("SMART SCAN RECOMMENDATIONS")
        print("="*60)
        print(f"{utils.GLYPH_OK} {recommendation_count} export scripts recommended")
        print("  └─ See 'Recommended Scripts' worksheet in Excel export")
        print()
        always_run = len([r for r in df_recommendations.to_dict('records') if r.get('Priority') == 'Always Run'])
        service_based = recommendation_count - always_run
        if always_run > 0:
            print(f"  • {always_run} Always-Run scripts (security baseline)")
        if service_based > 0:
            print(f"  • {service_based} Service-Based scripts (for discovered services)")

    utils.log_success("Services discovery completed successfully")


if __name__ == "__main__":
    main()
