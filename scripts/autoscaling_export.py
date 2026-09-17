#!/usr/bin/env python3
"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Auto Scaling Groups Export Tool
Date: NOV-15-2025

Description:
This script exports AWS Auto Scaling Group information from all regions into an Excel file with
multiple worksheets. The output includes Auto Scaling Group configurations, instances,
launch configurations/templates, scaling policies, scheduled actions, and lifecycle hooks.

Features:
- Auto Scaling Group overview with desired/min/max capacity
- Resolved per-instance specs (instance type, AMI, volumes) and capacity totals
  at minimum, desired, and maximum size
- Instance information with health status and lifecycle state
- Launch Templates referenced by a group, resolved to the version in use
- Launch Configurations referenced by a group
- Scaling policies (target tracking, step scaling, simple scaling, predictive),
  with step boundaries and the CloudWatch alarm metric and threshold that
  trigger them
- Scheduled actions
- Lifecycle hooks
- Warm pools
- Recent scaling activity (what the group actually did, bounded per group by
  the scaling_activity_limit advanced setting)
- Instance refresh history
- Tags and metadata

Launch template and launch configuration coverage is deliberately
referenced-only: objects no Auto Scaling Group uses are out of scope here.

Phase 4B Update:
- Concurrent region scanning (4x-10x performance improvement)
- Automatic fallback to sequential on errors
"""

import datetime
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

    try:
        import utils
    except ImportError:
        print("ERROR: Could not import the utils module. Make sure utils.py is in the StratusScan directory.")
        sys.exit(1)
args = utils.parse_script_args("Export Auto Scaling Groups to Excel")


def _format_timestamp(value: Any) -> str:
    """Render an AWS timestamp as ``YYYY-MM-DD HH:MM:SS``, or ``'N/A'`` if absent."""
    if not value:
        return 'N/A'
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def _scan_asgs_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Auto Scaling Groups from a single region.

    Thin wrapper over :func:`_scan_launch_data_region`, which does the actual
    scan and additionally resolves the launch templates and launch
    configurations the groups reference. Retained because ASG rows are the
    primary scope and several callers and tests want them on their own.

    Region-level failures propagate; individual malformed ASGs are skipped.
    """
    return _scan_launch_data_region(region)['asgs']


def _build_asg_row(asg: dict, region: str) -> dict[str, Any]:
    """Build a single Auto Scaling Group export row from a describe response."""
    asg_name = asg.get('AutoScalingGroupName', '')
    print(f"  Processing ASG: {asg_name}")

    # Basic information
    asg_arn = asg.get('AutoScalingGroupARN', '')
    min_size = asg.get('MinSize', 0)
    max_size = asg.get('MaxSize', 0)
    desired_capacity = asg.get('DesiredCapacity', 0)
    default_cooldown = asg.get('DefaultCooldown', 0)
    health_check_type = asg.get('HealthCheckType', 'N/A')
    health_check_grace_period = asg.get('HealthCheckGracePeriod', 0)

    # Launch configuration or template. The referenced object itself is
    # resolved separately (see _collect_launch_templates /
    # _collect_launch_configurations); this is the display string only.
    launch_source = _format_launch_source(_launch_source_ref(asg))

    # VPC and subnets
    vpc_zone_identifier = asg.get('VPCZoneIdentifier', '')
    subnet_ids = vpc_zone_identifier.split(',') if vpc_zone_identifier else []
    subnet_count = len(subnet_ids)
    availability_zones = asg.get('AvailabilityZones', [])
    az_list = ', '.join(availability_zones) if availability_zones else 'N/A'

    # Load balancers
    load_balancer_names = asg.get('LoadBalancerNames', [])
    target_group_arns = asg.get('TargetGroupARNs', [])
    lb_count = len(load_balancer_names) + len(target_group_arns)

    # Instance information
    instances = asg.get('Instances', [])
    instance_count = len(instances)
    healthy_count = sum(1 for i in instances if i.get('HealthStatus') == 'Healthy')
    unhealthy_count = instance_count - healthy_count

    # Service-linked role
    service_linked_role_arn = asg.get('ServiceLinkedRoleARN', 'N/A')

    # New instances protected from scale in
    new_instances_protected = asg.get('NewInstancesProtectedFromScaleIn', False)

    # Capacity rebalance
    capacity_rebalance = asg.get('CapacityRebalance', False)

    # Creation time
    created_time = _format_timestamp(asg.get('CreatedTime'))

    # Tags
    tags = asg.get('Tags', [])
    tag_dict = {tag['Key']: tag['Value'] for tag in tags if 'Key' in tag and 'Value' in tag}
    tags_str = ', '.join([f"{k}={v}" for k, v in tag_dict.items()]) if tag_dict else 'N/A'

    return {
        'Region': region,
        'ASG Name': asg_name,
        'Min Size': min_size,
        'Max Size': max_size,
        'Desired Capacity': desired_capacity,
        'Current Instances': instance_count,
        'Healthy Instances': healthy_count,
        'Unhealthy Instances': unhealthy_count,
        'Launch Source': launch_source,
        'Availability Zones': az_list,
        'Subnet Count': subnet_count,
        'Load Balancer Count': lb_count,
        'Health Check Type': health_check_type,
        'Health Check Grace Period (s)': health_check_grace_period,
        'Default Cooldown (s)': default_cooldown,
        'New Instance Protection': new_instances_protected,
        'Capacity Rebalance': capacity_rebalance,
        'Service Linked Role': service_linked_role_arn,
        'Created Time': created_time,
        'Tags': tags_str,
        'ASG ARN': asg_arn
    }


# ---------------------------------------------------------------------------
# Launch source resolution (Issue #257)
#
# An Auto Scaling Group's launch template or launch configuration *is* the
# group in every sense that matters downstream: it defines what every instance
# the group ever creates will be. The exporter historically recorded only the
# name and version string and never fetched the referenced object, so instance
# type, AMI, volumes, and IMDS posture were absent from the export while the
# module docstring claimed to cover them.
#
# Scope is deliberately *referenced-only*: a launch template that no ASG uses
# is an EC2-side concern, not an auto-scaling one.
# ---------------------------------------------------------------------------

# Device names AWS uses for the root volume, in preference order.
_ROOT_DEVICE_NAMES = ('/dev/xvda', '/dev/sda1', 'xvda', 'sda1')


def _launch_source_ref(asg: dict) -> dict[str, Any]:
    """
    Describe what an Auto Scaling Group launches from, as structured data.

    Returns a dict with ``kind`` (``'lt'``, ``'mixed'``, or ``'lc'``) plus the
    identifiers needed to resolve the referenced object. ``version_raw`` is
    what the ASG literally records (possibly empty); ``version_query`` is what
    to ask the API for — an omitted version means ``$Default``.
    """
    launch_template = asg.get('LaunchTemplate') or {}
    mixed_instances_policy = asg.get('MixedInstancesPolicy') or {}
    launch_config_name = asg.get('LaunchConfigurationName') or ''

    if launch_template:
        version = launch_template.get('Version') or ''
        return {
            'kind': 'lt',
            'lt_id': launch_template.get('LaunchTemplateId', ''),
            'lt_name': launch_template.get('LaunchTemplateName', ''),
            'version_raw': version,
            'version_query': version or '$Default',
            'lc_name': '',
            'overrides': [],
            'distribution': {},
        }

    if mixed_instances_policy:
        mip_lt = mixed_instances_policy.get('LaunchTemplate', {}) or {}
        spec = mip_lt.get('LaunchTemplateSpecification', {}) or {}
        version = spec.get('Version') or ''
        overrides = [
            override.get('InstanceType')
            for override in mip_lt.get('Overrides', []) or []
            if override.get('InstanceType')
        ]
        return {
            'kind': 'mixed',
            'lt_id': spec.get('LaunchTemplateId', ''),
            'lt_name': spec.get('LaunchTemplateName', ''),
            'version_raw': version,
            'version_query': version or '$Default',
            'lc_name': '',
            'overrides': overrides,
            'distribution': mixed_instances_policy.get('InstancesDistribution', {}) or {},
        }

    return {
        'kind': 'lc',
        'lt_id': '',
        'lt_name': '',
        'version_raw': '',
        'version_query': '',
        'lc_name': launch_config_name or 'N/A',
        'overrides': [],
        'distribution': {},
    }


def _format_launch_source(ref: dict[str, Any]) -> str:
    """Render the human-readable ``Launch Source`` column from a launch ref."""
    if ref['kind'] == 'lt':
        return f"LT: {ref['lt_name']} ({ref['version_raw']})"
    if ref['kind'] == 'mixed':
        return f"Mixed: {ref['lt_name']} ({ref['version_raw']})"
    return f"LC: {ref['lc_name']}"


def _summarize_block_devices(mappings: list[dict]) -> dict[str, Any]:
    """
    Summarize launch-template / launch-configuration block device mappings.

    Returns root volume size and type, total EBS size across all mappings, and
    whether every EBS mapping is encrypted. Values are ``None`` when the
    mapping list is absent — which is common, since a template that omits
    block devices inherits the AMI's. ``None`` is reported as ``'N/A'`` rather
    than guessed at.
    """
    summary: dict[str, Any] = {
        'root_size': None,
        'root_type': None,
        'total_size': None,
        'encrypted': None,
    }
    if not mappings:
        return summary

    ebs_mappings = [m for m in mappings if (m.get('Ebs') or {}).get('VolumeSize')]
    if not ebs_mappings:
        return summary

    total = 0
    encrypted_flags = []
    for mapping in ebs_mappings:
        ebs = mapping.get('Ebs') or {}
        total += int(ebs.get('VolumeSize') or 0)
        encrypted_flags.append(bool(ebs.get('Encrypted')))

    root = None
    for candidate in _ROOT_DEVICE_NAMES:
        root = next((m for m in ebs_mappings if m.get('DeviceName') == candidate), None)
        if root:
            break
    if root is None:
        root = ebs_mappings[0]

    root_ebs = root.get('Ebs') or {}
    summary['root_size'] = int(root_ebs.get('VolumeSize') or 0)
    summary['root_type'] = root_ebs.get('VolumeType') or 'N/A'
    summary['total_size'] = total
    summary['encrypted'] = all(encrypted_flags)
    return summary


def _resolve_instance_specs(region: str, instance_types: set) -> dict[str, dict[str, Any]]:
    """
    Resolve vCPU and memory for a set of instance types.

    Reads the static reference data first (``utils.load_instance_type_specs``,
    which covers ~1,200 types) and falls back to ``ec2:DescribeInstanceTypes``
    in chunks for anything absent — the same two-tier approach ec2_export.py
    uses for memory, extended to carry vCPU.

    vCPU comes from ``VCpuInfo.DefaultVCpus``, never ``CpuOptions.CoreCount``:
    the latter is physical cores and under-reports by the SMT factor.
    """
    specs: dict[str, dict[str, Any]] = {}
    wanted = sorted({t for t in instance_types if t})
    if not wanted:
        return specs

    reference = utils.load_instance_type_specs()
    unknown = []
    for instance_type in wanted:
        record = reference.get(instance_type) or {}
        if record.get('vcpu') is not None and record.get('memory_gib') is not None:
            specs[instance_type] = {
                'vcpu': record['vcpu'],
                'memory_gib': float(record['memory_gib']),
            }
        else:
            unknown.append(instance_type)

    if unknown:
        try:
            ec2_client = utils.get_boto3_client('ec2', region_name=region)
            for index in range(0, len(unknown), 100):
                chunk = unknown[index:index + 100]
                response = ec2_client.describe_instance_types(InstanceTypes=chunk)
                for entry in response.get('InstanceTypes', []):
                    memory_mib = (entry.get('MemoryInfo') or {}).get('SizeInMiB')
                    specs[entry['InstanceType']] = {
                        'vcpu': (entry.get('VCpuInfo') or {}).get('DefaultVCpus'),
                        'memory_gib': round(memory_mib / 1024, 2) if memory_mib else None,
                    }
        except Exception as e:
            # Spec resolution is enrichment, not primary scope: an unresolved
            # type yields 'N/A' columns rather than failing the region.
            utils.log_warning(f"Could not resolve instance type specs in {region}: {e}")

    return specs


def _collect_launch_templates(
    region: str,
    refs_by_asg: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    Resolve every launch template referenced by an ASG in this region.

    Returns ``(rows, details_by_asg)`` where ``rows`` is one record per
    referenced (template, version) pair and ``details_by_asg`` maps ASG name →
    resolved template data for enriching the primary sheet.

    Raises when there are referenced templates but *none* could be resolved —
    that shape means a permission or API failure, not an empty account, and
    must surface as a failed region rather than an empty sheet.
    """
    referenced = {
        name: ref for name, ref in refs_by_asg.items()
        if ref['kind'] in ('lt', 'mixed') and (ref['lt_id'] or ref['lt_name'])
    }
    if not referenced:
        return [], {}

    ec2_client = utils.get_boto3_client('ec2', region_name=region)

    # Group ASGs by the (template, version) pair they reference.
    by_pair: dict[tuple, list[str]] = {}
    for asg_name, ref in referenced.items():
        key = (ref['lt_id'], ref['lt_name'], ref['version_query'])
        by_pair.setdefault(key, []).append(asg_name)

    # Resolve template metadata once per template (not per pair) so $Default
    # and $Latest can be turned into concrete version numbers.
    template_meta: dict[tuple, dict] = {}
    for lt_id, lt_name, _version in by_pair:
        meta_key = (lt_id, lt_name)
        if meta_key in template_meta:
            continue
        try:
            if lt_id:
                response = ec2_client.describe_launch_templates(LaunchTemplateIds=[lt_id])
            else:
                response = ec2_client.describe_launch_templates(LaunchTemplateNames=[lt_name])
            templates = response.get('LaunchTemplates', [])
            template_meta[meta_key] = templates[0] if templates else {}
        except Exception as e:
            # A template deleted out from under a live ASG is a real state we
            # must report, not crash on.
            utils.log_warning(
                f"Could not describe launch template {lt_name or lt_id} in {region}: {e}"
            )
            template_meta[meta_key] = {}

    rows = []
    details_by_asg: dict[str, dict[str, Any]] = {}
    resolved_pairs = 0

    for (lt_id, lt_name, version_query), asg_names in by_pair.items():
        meta = template_meta.get((lt_id, lt_name), {})
        latest_version = meta.get('LatestVersionNumber')
        default_version = meta.get('DefaultVersionNumber')

        # Resolve the alias to a concrete version number where possible;
        # describe_launch_template_versions accepts the aliases on real AWS but
        # numeric versions work everywhere including moto.
        if version_query == '$Default' and default_version is not None:
            version_lookup = str(default_version)
        elif version_query == '$Latest' and latest_version is not None:
            version_lookup = str(latest_version)
        else:
            version_lookup = version_query

        version_data = {}
        resolved_version = version_lookup
        try:
            kwargs: dict[str, Any] = {'Versions': [version_lookup]}
            if lt_id:
                kwargs['LaunchTemplateId'] = lt_id
            else:
                kwargs['LaunchTemplateName'] = lt_name
            response = ec2_client.describe_launch_template_versions(**kwargs)
            versions = response.get('LaunchTemplateVersions', [])
            if versions:
                version_entry = versions[0]
                version_data = version_entry.get('LaunchTemplateData', {}) or {}
                resolved_version = str(version_entry.get('VersionNumber', version_lookup))
                resolved_pairs += 1
        except Exception as e:
            utils.log_warning(
                f"Could not describe launch template version "
                f"{lt_name or lt_id} ({version_lookup}) in {region}: {e}"
            )

        block_devices = _summarize_block_devices(version_data.get('BlockDeviceMappings', []))
        metadata_options = version_data.get('MetadataOptions', {}) or {}
        iam_profile = version_data.get('IamInstanceProfile', {}) or {}

        security_groups = list(version_data.get('SecurityGroupIds', []) or [])
        security_groups += list(version_data.get('SecurityGroups', []) or [])
        for interface in version_data.get('NetworkInterfaces', []) or []:
            security_groups += list(interface.get('Groups', []) or [])

        # A mixed-instances policy carries its own type list and spot/on-demand
        # split; both live on the ASG, not the template.
        overrides = []
        distribution = {}
        for asg_name in asg_names:
            ref = referenced[asg_name]
            if ref['overrides']:
                overrides = ref['overrides']
            if ref['distribution']:
                distribution = ref['distribution']

        detail = {
            'instance_type': version_data.get('InstanceType'),
            'overrides': overrides,
            'ami_id': version_data.get('ImageId'),
            'root_size': block_devices['root_size'],
            'total_size': block_devices['total_size'],
            'source': 'Launch Template',
            'source_name': lt_name or lt_id,
        }
        for asg_name in asg_names:
            details_by_asg[asg_name] = detail

        rows.append({
            'Region': region,
            'Used By ASGs': ', '.join(sorted(asg_names)),
            'Launch Template Name': lt_name or 'N/A',
            'Launch Template ID': lt_id or meta.get('LaunchTemplateId', 'N/A'),
            'Version In Use': version_query,
            'Resolved Version': resolved_version,
            'Default Version': default_version if default_version is not None else 'N/A',
            'Latest Version': latest_version if latest_version is not None else 'N/A',
            'Version Is Latest': (
                str(resolved_version) == str(latest_version)
                if latest_version is not None else 'N/A'
            ),
            'Instance Type': version_data.get('InstanceType', 'N/A'),
            'Instance Type Overrides': ', '.join(overrides) if overrides else 'N/A',
            'On-Demand Base Capacity': distribution.get('OnDemandBaseCapacity', 'N/A'),
            'On-Demand % Above Base': distribution.get('OnDemandPercentageAboveBaseCapacity', 'N/A'),
            'Spot Allocation Strategy': distribution.get('SpotAllocationStrategy', 'N/A'),
            'AMI ID': version_data.get('ImageId', 'N/A'),
            'Key Pair': version_data.get('KeyName', 'N/A'),
            'IAM Instance Profile': iam_profile.get('Arn') or iam_profile.get('Name') or 'N/A',
            'Security Groups': ', '.join(security_groups) if security_groups else 'N/A',
            'IMDSv2 Required': metadata_options.get('HttpTokens', 'N/A'),
            'Metadata Hop Limit': metadata_options.get('HttpPutResponseHopLimit', 'N/A'),
            'Root Volume Size (GiB)': (
                block_devices['root_size'] if block_devices['root_size'] is not None else 'N/A'
            ),
            'Root Volume Type': block_devices['root_type'] or 'N/A',
            'Total EBS Size (GiB)': (
                block_devices['total_size'] if block_devices['total_size'] is not None else 'N/A'
            ),
            'EBS Encrypted': (
                block_devices['encrypted'] if block_devices['encrypted'] is not None else 'N/A'
            ),
            'EBS Optimized': version_data.get('EbsOptimized', 'N/A'),
            'Detailed Monitoring': (version_data.get('Monitoring', {}) or {}).get('Enabled', 'N/A'),
            'User Data Present': bool(version_data.get('UserData')),
            'Created By': meta.get('CreatedBy', 'N/A'),
            'Created Time': _format_timestamp(meta.get('CreateTime')),
        })

    if by_pair and resolved_pairs == 0:
        # Every referenced template failed to resolve — a permission or API
        # problem, not an empty account. Fail loud so the region is recorded
        # as failed instead of exporting an empty sheet that reads as complete.
        raise RuntimeError(
            f"Referenced {len(by_pair)} launch template version(s) in {region} but resolved "
            f"none — check ec2:DescribeLaunchTemplates / ec2:DescribeLaunchTemplateVersions"
        )

    return rows, details_by_asg


def _collect_launch_configurations(
    region: str,
    refs_by_asg: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    Resolve every launch configuration referenced by an ASG in this region.

    Launch configurations are deprecated and closed to new AWS customers, but
    they remain the majority launch path in long-lived estates (a sampled
    commercial org showed 123 of 178 groups on LCs), so this path is not
    optional.

    Raises when there are referenced configurations but none resolved, for the
    same fail-loud reason as ``_collect_launch_templates``.
    """
    referenced = {
        name: ref for name, ref in refs_by_asg.items()
        if ref['kind'] == 'lc' and ref['lc_name'] and ref['lc_name'] != 'N/A'
    }
    if not referenced:
        return [], {}

    wanted_names = {ref['lc_name'] for ref in referenced.values()}
    asgs_by_lc: dict[str, list[str]] = {}
    for asg_name, ref in referenced.items():
        asgs_by_lc.setdefault(ref['lc_name'], []).append(asg_name)

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    paginator = asg_client.get_paginator('describe_launch_configurations')

    rows = []
    details_by_asg: dict[str, dict[str, Any]] = {}
    resolved = 0

    for page in paginator.paginate():
        for config in page.get('LaunchConfigurations', []):
            config_name = config.get('LaunchConfigurationName', '')
            if config_name not in wanted_names:
                continue

            resolved += 1
            block_devices = _summarize_block_devices(config.get('BlockDeviceMappings', []))
            metadata_options = config.get('MetadataOptions', {}) or {}
            asg_names = asgs_by_lc.get(config_name, [])

            detail = {
                'instance_type': config.get('InstanceType'),
                'overrides': [],
                'ami_id': config.get('ImageId'),
                'root_size': block_devices['root_size'],
                'total_size': block_devices['total_size'],
                'source': 'Launch Configuration',
                'source_name': config_name,
            }
            for asg_name in asg_names:
                details_by_asg[asg_name] = detail

            rows.append({
                'Region': region,
                'Used By ASGs': ', '.join(sorted(asg_names)),
                'Launch Configuration Name': config_name,
                'Instance Type': config.get('InstanceType', 'N/A'),
                'AMI ID': config.get('ImageId', 'N/A'),
                'Key Pair': config.get('KeyName', 'N/A'),
                'IAM Instance Profile': config.get('IamInstanceProfile', 'N/A'),
                'Security Groups': ', '.join(config.get('SecurityGroups', []) or []) or 'N/A',
                'IMDSv2 Required': metadata_options.get('HttpTokens', 'N/A'),
                'Metadata Hop Limit': metadata_options.get('HttpPutResponseHopLimit', 'N/A'),
                'Root Volume Size (GiB)': (
                    block_devices['root_size'] if block_devices['root_size'] is not None else 'N/A'
                ),
                'Root Volume Type': block_devices['root_type'] or 'N/A',
                'Total EBS Size (GiB)': (
                    block_devices['total_size'] if block_devices['total_size'] is not None else 'N/A'
                ),
                'EBS Encrypted': (
                    block_devices['encrypted'] if block_devices['encrypted'] is not None else 'N/A'
                ),
                'EBS Optimized': config.get('EbsOptimized', 'N/A'),
                'Detailed Monitoring': (config.get('InstanceMonitoring', {}) or {}).get('Enabled', 'N/A'),
                'Associate Public IP': config.get('AssociatePublicIpAddress', 'N/A'),
                'Spot Price': config.get('SpotPrice', 'N/A'),
                'User Data Present': bool(config.get('UserData')),
                'Created Time': _format_timestamp(config.get('CreatedTime')),
                'Launch Configuration ARN': config.get('LaunchConfigurationARN', 'N/A'),
            })

    if wanted_names and resolved == 0:
        raise RuntimeError(
            f"Referenced {len(wanted_names)} launch configuration(s) in {region} but resolved "
            f"none — check autoscaling:DescribeLaunchConfigurations"
        )

    return rows, details_by_asg


def _launch_enrichment(
    detail: dict[str, Any],
    specs: dict[str, dict[str, Any]],
    min_size: int,
    desired_capacity: int,
    max_size: int,
) -> dict[str, Any]:
    """
    Build the resolved-launch and capacity-envelope columns for the ASG sheet.

    Capacity is reported as an envelope rather than a scalar: an ASG's total
    CPU, memory, and storage is per-instance spec x a capacity range, so a
    single number is wrong the moment the group scales.

    A mixed-instances policy with no single template-level instance type has no
    determinable per-instance spec; those columns report ``'N/A'`` rather than
    picking an override arbitrarily.
    """
    detail = detail or {}
    instance_type = detail.get('instance_type')
    overrides = detail.get('overrides') or []
    spec = specs.get(instance_type, {}) if instance_type else {}

    vcpu = spec.get('vcpu')
    memory_gib = spec.get('memory_gib')
    storage_gib = detail.get('total_size')

    def envelope(per_instance):
        if per_instance is None:
            return 'N/A', 'N/A', 'N/A'
        return (
            round(per_instance * min_size, 2),
            round(per_instance * desired_capacity, 2),
            round(per_instance * max_size, 2),
        )

    vcpu_min, vcpu_desired, vcpu_max = envelope(vcpu)
    mem_min, mem_desired, mem_max = envelope(memory_gib)
    storage_min, storage_desired, storage_max = envelope(storage_gib)

    if instance_type:
        instance_type_display = instance_type
    elif overrides:
        instance_type_display = f"Mixed: {', '.join(overrides)}"
    else:
        instance_type_display = 'N/A'

    return {
        'Launch Source Type': detail.get('source', 'N/A'),
        'Launch Source Name': detail.get('source_name', 'N/A'),
        'Instance Type': instance_type_display,
        'AMI ID': detail.get('ami_id') or 'N/A',
        'Root Volume Size (GiB)': (
            detail.get('root_size') if detail.get('root_size') is not None else 'N/A'
        ),
        'vCPU per Instance': vcpu if vcpu is not None else 'N/A',
        'Memory GiB per Instance': memory_gib if memory_gib is not None else 'N/A',
        'Storage GiB per Instance': storage_gib if storage_gib is not None else 'N/A',
        'vCPU (Min)': vcpu_min,
        'vCPU (Desired)': vcpu_desired,
        'vCPU (Max)': vcpu_max,
        'Memory GiB (Min)': mem_min,
        'Memory GiB (Desired)': mem_desired,
        'Memory GiB (Max)': mem_max,
        'Storage GiB (Min)': storage_min,
        'Storage GiB (Desired)': storage_desired,
        'Storage GiB (Max)': storage_max,
    }


def _scan_launch_data_region(region: str) -> dict[str, list[dict[str, Any]]]:
    """
    Collect Auto Scaling Groups and their resolved launch sources for a region.

    This is the primary scope collector. Like ``_scan_asgs_region`` before it,
    it deliberately does NOT swallow region-level errors: an API or permission
    failure must propagate so ``scan_regions_concurrent(..., collect_failures=True)``
    records the region as failed instead of silently reporting "no Auto Scaling
    Groups" (see .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).

    Individual malformed ASGs are skipped (logged) rather than aborting the
    whole region.

    Returns:
        dict with keys ``asgs``, ``launch_templates``, ``launch_configurations``.
    """
    empty: dict[str, list[dict[str, Any]]] = {
        'asgs': [], 'launch_templates': [], 'launch_configurations': []
    }
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return empty

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    paginator = asg_client.get_paginator('describe_auto_scaling_groups')

    raw_asgs = []
    for page in paginator.paginate():
        raw_asgs.extend(page.get('AutoScalingGroups', []))

    print(f"  Found {len(raw_asgs)} Auto Scaling Groups")
    if not raw_asgs:
        return empty

    # Resolve what each group launches from before building rows, so the
    # primary sheet can carry instance type, AMI, and capacity forward.
    refs_by_asg: dict[str, dict[str, Any]] = {}
    skipped: set = set()
    for asg in raw_asgs:
        asg_name = asg.get('AutoScalingGroupName', '')
        try:
            refs_by_asg[asg_name] = _launch_source_ref(asg)
        except Exception as e:
            utils.log_error(
                f"Skipping Auto Scaling Group with unreadable launch source in "
                f"{region}: {asg_name or '<unknown>'}",
                e,
            )
            skipped.add(asg_name)

    lt_rows, lt_details = _collect_launch_templates(region, refs_by_asg)
    lc_rows, lc_details = _collect_launch_configurations(region, refs_by_asg)

    details_by_asg = {**lc_details, **lt_details}
    specs = _resolve_instance_specs(
        region,
        {detail.get('instance_type') for detail in details_by_asg.values()},
    )

    asg_rows = []
    for asg in raw_asgs:
        asg_name = asg.get('AutoScalingGroupName', '')
        if asg_name in skipped:
            continue
        try:
            row = _build_asg_row(asg, region)
        except Exception as e:
            # One malformed ASG is skipped, not fatal to the region.
            utils.log_error(
                f"Skipping malformed Auto Scaling Group in {region}: "
                f"{asg_name or '<unknown>'}",
                e,
            )
            continue

        row.update(_launch_enrichment(
            details_by_asg.get(asg_name, {}),
            specs,
            int(asg.get('MinSize') or 0),
            int(asg.get('DesiredCapacity') or 0),
            int(asg.get('MaxSize') or 0),
        ))
        asg_rows.append(row)

    return {
        'asgs': asg_rows,
        'launch_templates': lt_rows,
        'launch_configurations': lc_rows,
    }


def _scan_scheduled_actions_region(region: str) -> list[dict[str, Any]]:
    """
    Collect scheduled scaling actions from a single region.

    Scheduled scaling is one of the two mechanisms by which an ASG changes
    capacity; without it the export can say how a group reacts but not when it
    is configured to scale. Errors propagate so a failed region is recorded as
    failed rather than reported as "no scheduled actions" (Issue #258).
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    paginator = asg_client.get_paginator('describe_scheduled_actions')

    rows = []
    for page in paginator.paginate():
        for action in page.get('ScheduledUpdateGroupActions', []):
            min_size = action.get('MinSize')
            max_size = action.get('MaxSize')
            desired = action.get('DesiredCapacity')
            rows.append({
                'Region': region,
                'ASG Name': action.get('AutoScalingGroupName', 'N/A'),
                'Action Name': action.get('ScheduledActionName', 'N/A'),
                'Recurrence': action.get('Recurrence', 'N/A'),
                'Time Zone': action.get('TimeZone', 'N/A'),
                'Start Time': _format_timestamp(action.get('StartTime')),
                'End Time': _format_timestamp(action.get('EndTime')),
                'Min Size': min_size if min_size is not None else 'N/A',
                'Max Size': max_size if max_size is not None else 'N/A',
                'Desired Capacity': desired if desired is not None else 'N/A',
                'Action ARN': action.get('ScheduledActionARN', 'N/A'),
            })

    print(f"  Found {len(rows)} scheduled actions")
    return rows


def collect_scheduled_actions(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect scheduled scaling actions across regions.

    Returns:
        list: One dictionary per scheduled action.
    """
    region_results = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_scheduled_actions_region,
        show_progress=True,
    )
    return [action for result in region_results for action in result]


def collect_launch_data(
    regions: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list]:
    """
    Collect Auto Scaling Groups plus their resolved launch templates/configs.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(asgs, launch_templates, launch_configurations, failed_regions)``
        where ``failed_regions`` is a list of ``(region, error_message)`` tuples.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_launch_data_region,
        show_progress=True,
        collect_failures=True,
    )
    asgs, launch_templates, launch_configurations = [], [], []
    for result in region_results:
        asgs.extend(result.get('asgs', []))
        launch_templates.extend(result.get('launch_templates', []))
        launch_configurations.extend(result.get('launch_configurations', []))
    return asgs, launch_templates, launch_configurations, failed_regions


def collect_autoscaling_groups(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Auto Scaling Group information across regions, surfacing failures.

    Thin wrapper over :func:`collect_launch_data` for callers that want only
    the ASG rows. Failure semantics are unchanged: a region whose collection
    errors is reported in ``failed_regions`` rather than silently collapsed
    into an empty result.

    Returns:
        tuple: ``(asgs, failed_regions)`` where ``failed_regions`` is a list of
        ``(region, error_message)`` tuples.
    """
    asgs, _launch_templates, _launch_configs, failed_regions = collect_launch_data(regions)
    return asgs, failed_regions


@utils.aws_error_handler("Collecting ASG instances", default_return=[])
def collect_asg_instances(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect instance information from Auto Scaling Groups.

    Args:
        regions: List of AWS regions to scan

    Returns:
        list: List of dictionaries with instance information
    """
    all_instances = []

    for region in regions:
        if not utils.is_aws_region(region):
            continue

        print(f"\nProcessing region: {region}")

        try:
            asg_client = utils.get_boto3_client('autoscaling', region_name=region)
            paginator = asg_client.get_paginator('describe_auto_scaling_groups')

            for page in paginator.paginate():
                asgs = page.get('AutoScalingGroups', [])

                for asg in asgs:
                    asg_name = asg.get('AutoScalingGroupName', '')
                    instances = asg.get('Instances', [])

                    for instance in instances:
                        instance_id = instance.get('InstanceId', '')
                        az = instance.get('AvailabilityZone', '')
                        lifecycle_state = instance.get('LifecycleState', '')
                        health_status = instance.get('HealthStatus', '')
                        launch_config_name = instance.get('LaunchConfigurationName', 'N/A')
                        launch_template = instance.get('LaunchTemplate', {})

                        if launch_template:
                            launch_source = f"LT: {launch_template.get('LaunchTemplateName', '')} ({launch_template.get('Version', '')})"
                        else:
                            launch_source = f"LC: {launch_config_name}"

                        protected_from_scale_in = instance.get('ProtectedFromScaleIn', False)

                        all_instances.append({
                            'Region': region,
                            'ASG Name': asg_name,
                            'Instance ID': instance_id,
                            'Availability Zone': az,
                            'Lifecycle State': lifecycle_state,
                            'Health Status': health_status,
                            'Launch Source': launch_source,
                            'Protected from Scale In': protected_from_scale_in
                        })

        except Exception as e:
            utils.log_error(f"Error collecting instances in region {region}", e)

    return all_instances


def _format_step_adjustments(steps: list[dict]) -> str:
    """
    Render a step policy's adjustments as readable interval → adjustment pairs.

    The step boundaries are the policy's entire content; a step policy exported
    without them says nothing about what the policy does (Issue #260). Bounds
    are offsets from the alarm threshold, and an absent bound means unbounded
    in that direction.
    """
    if not steps:
        return 'N/A'

    parts = []
    for step in steps:
        lower = step.get('MetricIntervalLowerBound')
        upper = step.get('MetricIntervalUpperBound')
        adjustment = step.get('ScalingAdjustment')

        if lower is None and upper is None:
            interval = 'any'
        elif lower is None:
            interval = f"< {upper}"
        elif upper is None:
            interval = f">= {lower}"
        else:
            interval = f"{lower} to {upper}"

        if isinstance(adjustment, (int, float)) and adjustment > 0:
            rendered = f"+{adjustment}"
        else:
            rendered = str(adjustment) if adjustment is not None else 'N/A'

        parts.append(f"{interval}: {rendered}")

    return '; '.join(parts)


def _fetch_policy_alarms(region: str, alarm_names: set) -> dict[str, dict[str, Any]]:
    """
    Fetch the CloudWatch alarms bound to step and simple scaling policies.

    ``describe_policies`` returns only alarm names and ARNs, so without this
    join the metric and threshold that actually trigger scaling are invisible
    in the export.

    This is enrichment, not primary scope: a CloudWatch permission gap yields
    'N/A' alarm columns rather than failing the region, since the policies
    themselves were collected successfully.
    """
    alarms: dict[str, dict[str, Any]] = {}
    names = sorted(n for n in alarm_names if n)
    if not names:
        return alarms

    try:
        cw_client = utils.get_boto3_client('cloudwatch', region_name=region)
        # describe_alarms returns at most MaxRecords per response (default 50)
        # and pages via NextToken. Reading only the first response silently
        # drops every alarm past that page — a 100-name chunk resolved 50
        # (Issue #268). Chunk the names AND paginate each chunk.
        paginator = cw_client.get_paginator('describe_alarms')
        for index in range(0, len(names), 100):
            chunk = names[index:index + 100]
            for page in paginator.paginate(AlarmNames=chunk):
                for alarm in page.get('MetricAlarms', []):
                    alarms[alarm.get('AlarmName', '')] = alarm
    except Exception as e:
        utils.log_warning(
            f"Could not resolve scaling policy alarms in {region} "
            f"(metric and threshold will read N/A): {e}"
        )

    return alarms


def _summarize_alarms(policy_alarms: list[dict], alarms_by_name: dict) -> dict[str, Any]:
    """Flatten the alarms bound to one policy into export columns."""
    summary = {
        'Alarm Names': 'N/A',
        'Alarm Metric': 'N/A',
        'Alarm Statistic': 'N/A',
        'Alarm Condition': 'N/A',
        'Alarm Period (s)': 'N/A',
    }
    names = [a.get('AlarmName') for a in policy_alarms or [] if a.get('AlarmName')]
    if not names:
        return summary

    summary['Alarm Names'] = ', '.join(names)

    metrics, statistics, conditions, periods = [], [], [], []
    for name in names:
        alarm = alarms_by_name.get(name)
        if not alarm:
            continue
        namespace = alarm.get('Namespace', '')
        metric_name = alarm.get('MetricName', '')
        if namespace or metric_name:
            metrics.append(f"{namespace}/{metric_name}" if namespace else metric_name)

        statistic = alarm.get('Statistic') or alarm.get('ExtendedStatistic')
        if statistic:
            statistics.append(statistic)

        operator = alarm.get('ComparisonOperator')
        threshold = alarm.get('Threshold')
        if operator is not None and threshold is not None:
            conditions.append(
                f"{operator} {threshold} for {alarm.get('EvaluationPeriods', '?')} period(s)"
            )

        if alarm.get('Period') is not None:
            periods.append(str(alarm['Period']))

    if metrics:
        summary['Alarm Metric'] = ', '.join(metrics)
    if statistics:
        summary['Alarm Statistic'] = ', '.join(statistics)
    if conditions:
        summary['Alarm Condition'] = '; '.join(conditions)
    if periods:
        summary['Alarm Period (s)'] = ', '.join(periods)

    return summary


def _describe_predictive_metric(spec: dict) -> str:
    """Name the metric a predictive scaling specification is built on."""
    if spec.get('PredefinedMetricPairSpecification'):
        return spec['PredefinedMetricPairSpecification'].get('PredefinedMetricType', 'Predefined pair')
    if spec.get('PredefinedScalingMetricSpecification'):
        return spec['PredefinedScalingMetricSpecification'].get('PredefinedMetricType', 'Predefined scaling')
    if spec.get('PredefinedLoadMetricSpecification'):
        return spec['PredefinedLoadMetricSpecification'].get('PredefinedMetricType', 'Predefined load')
    if spec.get('CustomizedScalingMetricSpecification'):
        return 'Customized scaling metric'
    if spec.get('CustomizedLoadMetricSpecification'):
        return 'Customized load metric'
    if spec.get('CustomizedCapacityMetricSpecification'):
        return 'Customized capacity metric'
    return 'N/A'


def _build_policy_row(
    policy: dict,
    region: str,
    alarms_by_name: dict,
) -> dict[str, Any]:
    """
    Build one scaling policy export row.

    Each policy type gets its own branch. Predictive scaling previously fell
    through to the simple-scaling branch and exported as
    ``Adjustment: N/A, Type: N/A`` — a row that read as a malformed simple
    policy rather than what it was (Issue #260).
    """
    policy_type = policy.get('PolicyType', '')
    adjustment_type = policy.get('AdjustmentType', 'N/A')
    scaling_adjustment = policy.get('ScalingAdjustment')
    step_adjustments = policy.get('StepAdjustments', []) or []
    target_tracking = policy.get('TargetTrackingConfiguration', {}) or {}
    predictive = policy.get('PredictiveScalingConfiguration', {}) or {}

    row: dict[str, Any] = {
        'Region': region,
        'ASG Name': policy.get('AutoScalingGroupName', ''),
        'Policy Name': policy.get('PolicyName', ''),
        'Policy Type': policy_type or 'N/A',
        'Policy Detail': 'N/A',
        'Adjustment Type': adjustment_type,
        'Scaling Adjustment': (
            scaling_adjustment if scaling_adjustment is not None else 'N/A'
        ),
        'Step Adjustments': 'N/A',
        'Target Value': 'N/A',
        'Predictive Mode': 'N/A',
        'Predictive Scheduling Buffer (s)': 'N/A',
        'Max Capacity Breach Behavior': 'N/A',
        'Max Capacity Buffer (%)': 'N/A',
        'Metric Aggregation': policy.get('MetricAggregationType', 'N/A'),
        'Cooldown (s)': policy.get('Cooldown', 'N/A'),
        'Estimated Instance Warmup (s)': policy.get('EstimatedInstanceWarmup', 'N/A'),
        'Min Adjustment Magnitude': policy.get('MinAdjustmentMagnitude', 'N/A'),
        'Enabled': policy.get('Enabled', True),
        'Policy ARN': policy.get('PolicyARN', 'N/A'),
    }

    if target_tracking:
        target_value = target_tracking.get('TargetValue', 'N/A')
        predefined_metric = target_tracking.get('PredefinedMetricSpecification', {}) or {}
        custom_metric = target_tracking.get('CustomizedMetricSpecification', {}) or {}

        row['Target Value'] = target_value
        if predefined_metric:
            metric_type = predefined_metric.get('PredefinedMetricType', 'N/A')
            row['Policy Detail'] = f"Target: {target_value}, Metric: {metric_type}"
        elif custom_metric:
            metric_name = custom_metric.get('MetricName', 'N/A')
            namespace = custom_metric.get('Namespace', 'N/A')
            row['Policy Detail'] = f"Target: {target_value}, Custom: {namespace}/{metric_name}"
        else:
            row['Policy Detail'] = f"Target: {target_value}"
        row['Disable Scale In'] = target_tracking.get('DisableScaleIn', 'N/A')
    else:
        row['Disable Scale In'] = 'N/A'

    if predictive:
        specs = predictive.get('MetricSpecifications', []) or []
        first_spec = specs[0] if specs else {}
        metric_label = _describe_predictive_metric(first_spec)
        target_value = first_spec.get('TargetValue', 'N/A')

        row['Target Value'] = target_value
        row['Predictive Mode'] = predictive.get('Mode', 'ForecastOnly')
        row['Predictive Scheduling Buffer (s)'] = predictive.get('SchedulingBufferTime', 'N/A')
        row['Max Capacity Breach Behavior'] = predictive.get(
            'MaxCapacityBreachBehavior', 'HonorMaxCapacity'
        )
        row['Max Capacity Buffer (%)'] = predictive.get('MaxCapacityBuffer', 'N/A')
        row['Policy Detail'] = (
            f"Predictive ({row['Predictive Mode']}): target {target_value}, "
            f"metric {metric_label}"
        )

    if step_adjustments:
        row['Step Adjustments'] = _format_step_adjustments(step_adjustments)
        row['Policy Detail'] = (
            f"Steps: {row['Step Adjustments']}, Type: {adjustment_type}"
        )

    if row['Policy Detail'] == 'N/A':
        # Simple scaling, or a shape with nothing more specific to say.
        row['Policy Detail'] = (
            f"Adjustment: {row['Scaling Adjustment']}, Type: {adjustment_type}"
        )

    row.update(_summarize_alarms(policy.get('Alarms', []), alarms_by_name))
    return row


def _scan_scaling_policies_region(region: str) -> list[dict[str, Any]]:
    """
    Collect scaling policies from a single region.

    Errors propagate so ``scan_regions_concurrent(..., collect_failures=True)``
    records a failed region rather than reporting "no scaling policies" — this
    collector previously swallowed per-region errors into an empty list, the
    Tier-3 PARTIAL pattern from the silent-collection-failure sweep.

    The CloudWatch alarm join is enrichment and degrades to 'N/A' rather than
    failing the region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    paginator = asg_client.get_paginator('describe_policies')

    policies = []
    for page in paginator.paginate():
        policies.extend(page.get('ScalingPolicies', []))

    alarm_names = {
        alarm.get('AlarmName')
        for policy in policies
        for alarm in policy.get('Alarms', []) or []
    }
    alarms_by_name = _fetch_policy_alarms(region, alarm_names)

    rows = []
    for policy in policies:
        try:
            rows.append(_build_policy_row(policy, region, alarms_by_name))
        except Exception as e:
            # One malformed policy is skipped, not fatal to the region.
            utils.log_error(
                f"Skipping malformed scaling policy in {region}: "
                f"{policy.get('PolicyName', '<unknown>')}",
                e,
            )

    print(f"  Found {len(rows)} scaling policies")
    return rows


def collect_scaling_policies(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect scaling policy information across regions, surfacing failures.

    Returns:
        tuple: ``(policies, failed_regions)`` where ``failed_regions`` is a list
        of ``(region, error_message)`` tuples.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_scaling_policies_region,
        show_progress=True,
        collect_failures=True,
    )
    return [policy for result in region_results for policy in result], failed_regions


def _scan_lifecycle_hooks_region(region: str) -> list[dict[str, Any]]:
    """Scan a single region for ASG lifecycle hooks."""
    hooks_data = []

    if not utils.is_aws_region(region):
        return hooks_data

    try:
        asg_client = utils.get_boto3_client('autoscaling', region_name=region)
        asg_paginator = asg_client.get_paginator('describe_auto_scaling_groups')

        for asg_page in asg_paginator.paginate():
            for asg in asg_page.get('AutoScalingGroups', []):
                asg_name = asg.get('AutoScalingGroupName', '')

                try:
                    hooks_response = asg_client.describe_lifecycle_hooks(
                        AutoScalingGroupName=asg_name
                    )
                    for hook in hooks_response.get('LifecycleHooks', []):
                        hooks_data.append({
                            'Region': region,
                            'ASG Name': asg_name,
                            'Hook Name': hook.get('LifecycleHookName', ''),
                            'Transition': hook.get('LifecycleTransition', 'N/A'),
                            'Heartbeat Timeout (s)': hook.get('HeartbeatTimeout', 'N/A'),
                            'Default Result': hook.get('DefaultResult', 'N/A'),
                            'Notification Target ARN': hook.get('NotificationTargetARN', 'N/A'),
                            'Role ARN': hook.get('RoleARN', 'N/A'),
                        })
                except Exception as e:
                    utils.log_warning(f"Could not get lifecycle hooks for ASG {asg_name}: {e}")

    except Exception as e:
        utils.log_error(f"Error scanning lifecycle hooks in {region}", e)

    return hooks_data


@utils.aws_error_handler("Collecting lifecycle hooks", default_return=[])
def collect_lifecycle_hooks(regions: list[str]) -> list[dict[str, Any]]:
    """
    Collect ASG lifecycle hook information from AWS regions.

    Args:
        regions: List of AWS regions to scan

    Returns:
        list: List of dictionaries with lifecycle hook information
    """
    print("\n=== COLLECTING LIFECYCLE HOOKS ===")

    results = utils.scan_regions_concurrent(regions, _scan_lifecycle_hooks_region)
    all_hooks = [hook for result in results for hook in result]

    utils.log_success(f"Total lifecycle hooks collected: {len(all_hooks)}")
    return all_hooks


# ---------------------------------------------------------------------------
# Warm pools, scaling activity, and instance refreshes (Issue #261)
#
# Every other sheet in this workbook describes an Auto Scaling Group's
# *intent* — how it is configured to react and when it is configured to scale.
# Scaling activity is the only source for when a group actually scaled.
# ---------------------------------------------------------------------------

# Fallback when the config carries no limit. AWS returns activity history
# newest-first and unbounded, so an unbounded pull would turn one export into
# thousands of calls on a busy group.
_DEFAULT_SCALING_ACTIVITY_LIMIT = 100


def _scaling_activity_limit() -> int:
    """
    Read the per-group scaling activity cap from advanced settings.

    Configured via ``python advanced_settings.py`` → Performance. A missing,
    malformed, or non-positive value falls back to the default rather than
    pulling unbounded history.
    """
    _, config = utils.get_config()
    performance = (config.get('advanced_settings', {}) or {}).get('performance', {}) or {}
    limit = performance.get('scaling_activity_limit', _DEFAULT_SCALING_ACTIVITY_LIMIT)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_SCALING_ACTIVITY_LIMIT
    return limit if limit > 0 else _DEFAULT_SCALING_ACTIVITY_LIMIT


def _list_asg_names(asg_client) -> list[str]:
    """
    List every Auto Scaling Group name in the client's region.

    Warm pools, scaling activities, and instance refreshes are all per-group
    APIs, so each collector needs the group list first. Errors propagate — an
    unreadable group list is a failed region, not an empty one.
    """
    paginator = asg_client.get_paginator('describe_auto_scaling_groups')
    names = []
    for page in paginator.paginate():
        for asg in page.get('AutoScalingGroups', []):
            name = asg.get('AutoScalingGroupName')
            if name:
                names.append(name)
    return names


def _scan_warm_pools_region(region: str) -> list[dict[str, Any]]:
    """
    Collect warm pool configuration and instances for a single region.

    A group with no warm pool returns an empty configuration; that is a normal
    state, not a failure, and yields no row.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    rows = []

    for asg_name in _list_asg_names(asg_client):
        try:
            response = asg_client.describe_warm_pool(AutoScalingGroupName=asg_name)
        except Exception as e:
            # One group's warm pool failing must not discard the region.
            utils.log_warning(f"Could not describe warm pool for {asg_name} in {region}: {e}")
            continue

        configuration = response.get('WarmPoolConfiguration') or {}
        if not configuration:
            continue

        instances = response.get('Instances', []) or []
        reuse_policy = configuration.get('InstanceReusePolicy') or {}

        rows.append({
            'Region': region,
            'ASG Name': asg_name,
            'Pool State': configuration.get('PoolState', 'N/A'),
            'Min Size': configuration.get('MinSize', 'N/A'),
            'Max Group Prepared Capacity': configuration.get(
                'MaxGroupPreparedCapacity', 'N/A'
            ),
            'Reuse On Scale In': reuse_policy.get('ReuseOnScaleIn', 'N/A'),
            'Status': configuration.get('Status', 'N/A'),
            'Warm Instances': len(instances),
        })

    print(f"  Found {len(rows)} warm pools")
    return rows


def _build_activity_row(activity: dict, region: str, asg_name: str) -> dict[str, Any]:
    """
    Build a single scaling activity export row.

    Extracted for testability: moto returns an empty activity list for every
    operation, so this is the only part of the activity path that can be
    exercised without live credentials.
    """
    return {
        'Region': region,
        'ASG Name': activity.get('AutoScalingGroupName', asg_name),
        'Activity ID': activity.get('ActivityId', 'N/A'),
        'Status': activity.get('StatusCode', 'N/A'),
        'Status Message': activity.get('StatusMessage', 'N/A'),
        'Description': activity.get('Description', 'N/A'),
        'Cause': activity.get('Cause', 'N/A'),
        'Start Time': _format_timestamp(activity.get('StartTime')),
        'End Time': _format_timestamp(activity.get('EndTime')),
        'Progress (%)': activity.get('Progress', 'N/A'),
        'Details': activity.get('Details', 'N/A'),
    }


def _scan_scaling_activities_region(region: str) -> list[dict[str, Any]]:
    """
    Collect recent scaling activity for a single region.

    Bounded per group by the ``scaling_activity_limit`` advanced setting: AWS
    returns activities newest-first with no upper bound, and this is the only
    collector in this exporter with a real runtime cost.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    limit = _scaling_activity_limit()
    paginator = asg_client.get_paginator('describe_scaling_activities')
    rows = []

    for asg_name in _list_asg_names(asg_client):
        try:
            pages = paginator.paginate(
                AutoScalingGroupName=asg_name,
                PaginationConfig={'MaxItems': limit},
            )
            for page in pages:
                for activity in page.get('Activities', []):
                    rows.append(_build_activity_row(activity, region, asg_name))
        except Exception as e:
            utils.log_warning(
                f"Could not describe scaling activities for {asg_name} in {region}: {e}"
            )
            continue

    print(f"  Found {len(rows)} scaling activities (limit {limit} per group)")
    return rows


def _build_instance_refresh_row(refresh: dict, region: str, asg_name: str) -> dict[str, Any]:
    """Build a single instance refresh export row."""
    preferences = refresh.get('Preferences') or {}
    return {
        'Region': region,
        'ASG Name': refresh.get('AutoScalingGroupName', asg_name),
        'Instance Refresh ID': refresh.get('InstanceRefreshId', 'N/A'),
        'Status': refresh.get('Status', 'N/A'),
        'Status Reason': refresh.get('StatusReason', 'N/A'),
        'Percentage Complete': refresh.get('PercentageComplete', 'N/A'),
        'Instances To Update': refresh.get('InstancesToUpdate', 'N/A'),
        'Start Time': _format_timestamp(refresh.get('StartTime')),
        'End Time': _format_timestamp(refresh.get('EndTime')),
        'Min Healthy Percentage': preferences.get('MinHealthyPercentage', 'N/A'),
        'Instance Warmup (s)': preferences.get('InstanceWarmup', 'N/A'),
        'Checkpoint Percentages': ', '.join(
            str(p) for p in preferences.get('CheckpointPercentages', []) or []
        ) or 'N/A',
        'Skip Matching': preferences.get('SkipMatching', 'N/A'),
    }


def _scan_instance_refreshes_region(region: str) -> list[dict[str, Any]]:
    """
    Collect instance refresh history for a single region.

    ``describe_instance_refreshes`` has no boto3 paginator and pages via a
    native NextToken; calling ``get_paginator`` on it raises before any AWS
    call (Issues #223 / #214).

    Note: moto does not implement this operation, so the collector cannot be
    exercised end to end under mocking — only :func:`_build_instance_refresh_row`
    is unit-tested. The per-group loop means an empty account never reaches the
    call, which keeps the smoke test meaningful.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    print(f"\nProcessing region: {region}")

    asg_client = utils.get_boto3_client('autoscaling', region_name=region)
    rows = []

    for asg_name in _list_asg_names(asg_client):
        try:
            # describe_instance_refreshes has NO boto3 paginator — get_paginator
            # raises OperationNotPageableError before any AWS call. It pages via
            # a native NextToken instead. See Issues #223 / #214.
            next_token = None
            while True:
                params = {'AutoScalingGroupName': asg_name}
                if next_token:
                    params['NextToken'] = next_token
                response = asg_client.describe_instance_refreshes(**params)
                for refresh in response.get('InstanceRefreshes', []):
                    rows.append(_build_instance_refresh_row(refresh, region, asg_name))
                next_token = response.get('NextToken')
                if not next_token:
                    break
        except Exception as e:
            utils.log_warning(
                f"Could not describe instance refreshes for {asg_name} in {region}: {e}"
            )
            continue

    print(f"  Found {len(rows)} instance refreshes")
    return rows


def collect_warm_pools(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect warm pool information across regions, surfacing failures.

    Returns:
        tuple: ``(warm_pools, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_warm_pools_region,
        show_progress=True,
        collect_failures=True,
    )
    return [pool for result in region_results for pool in result], failed_regions


def collect_scaling_activities(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect recent scaling activity across regions, surfacing failures.

    Returns:
        tuple: ``(activities, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_scaling_activities_region,
        show_progress=True,
        collect_failures=True,
    )
    return [activity for result in region_results for activity in result], failed_regions


def collect_instance_refreshes(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect instance refresh history across regions, surfacing failures.

    Returns:
        tuple: ``(refreshes, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_instance_refreshes_region,
        show_progress=True,
        collect_failures=True,
    )
    return [refresh for result in region_results for refresh in result], failed_regions


def _merge_failed_regions(*failure_lists) -> list:
    """
    Combine ``(region, error)`` failure lists, keeping the first error per region.

    Several scopes are collected independently; a region that fails more than
    one of them should still be reported once.
    """
    merged: dict[str, str] = {}
    for failures in failure_lists:
        for region, error in failures or []:
            merged.setdefault(region, error)
    return list(merged.items())


def export_autoscaling_data(account_id: str, account_name: str):
    """
    Export Auto Scaling Group information to an Excel file.

    Args:
        account_id: The AWS account ID
        account_name: The AWS account name
    """
    # Detect partition and set partition-aware example regions
    regions = utils.prompt_region_selection()
    region_suffix = 'all'
    # Import pandas for DataFrame handling
    import pandas as pd

    # Dictionary to hold all DataFrames for export
    data_frames = {}

    # STEP 1: Collect Auto Scaling Groups (primary scope — region failures must
    # propagate as failed_regions, never collapse into "empty").
    print("\n=== COLLECTING AUTO SCALING GROUPS AND LAUNCH SOURCES ===")
    asgs, launch_templates, launch_configs, failed_regions = collect_launch_data(regions)
    utils.log_success(f"Total Auto Scaling Groups collected: {len(asgs)}")
    utils.log_success(f"Total launch templates resolved: {len(launch_templates)}")
    utils.log_success(f"Total launch configurations resolved: {len(launch_configs)}")
    if asgs:
        data_frames['Auto Scaling Groups'] = pd.DataFrame(asgs)
    if launch_templates:
        data_frames['Launch Templates'] = pd.DataFrame(launch_templates)
    if launch_configs:
        data_frames['Launch Configurations'] = pd.DataFrame(launch_configs)

    # STEP 2: Collect instances (Phase 4B: concurrent)
    print("\n=== COLLECTING AUTO SCALING GROUP INSTANCES ===")
    instance_results = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=lambda r: collect_asg_instances([r]),
        show_progress=True
    )
    instances = []
    for result in instance_results:
        instances.extend(result)
    utils.log_success(f"Total ASG instances collected: {len(instances)}")
    if instances:
        data_frames['Instances'] = pd.DataFrame(instances)

    # STEP 3: Collect scaling policies (Issue #260 — enriched and fail-loud).
    # Policy collection is a tracked scope: a region that fails here is
    # reported as failed rather than exporting a sheet that reads as complete.
    print("\n=== COLLECTING SCALING POLICIES ===")
    policies, policy_failures = collect_scaling_policies(regions)
    utils.log_success(f"Total scaling policies collected: {len(policies)}")
    if policies:
        data_frames['Scaling Policies'] = pd.DataFrame(policies)
    failed_regions = _merge_failed_regions(failed_regions, policy_failures)

    # STEP 4: Collect scheduled actions (Issue #258)
    print("\n=== COLLECTING SCHEDULED ACTIONS ===")
    scheduled_actions = collect_scheduled_actions(regions)
    utils.log_success(f"Total scheduled actions collected: {len(scheduled_actions)}")
    if scheduled_actions:
        data_frames['Scheduled Actions'] = pd.DataFrame(scheduled_actions)

    # STEP 5: Collect lifecycle hooks (Phase F: API coverage fix)
    hooks = collect_lifecycle_hooks(regions)
    if hooks:
        data_frames['Lifecycle Hooks'] = pd.DataFrame(hooks)

    # STEP 6: Warm pools, scaling activity, instance refreshes (Issue #261).
    # Scaling activity is the only source for when a group *actually* scaled,
    # as distinct from when it is configured to.
    print("\n=== COLLECTING WARM POOLS ===")
    warm_pools, warm_pool_failures = collect_warm_pools(regions)
    utils.log_success(f"Total warm pools collected: {len(warm_pools)}")
    if warm_pools:
        data_frames['Warm Pools'] = pd.DataFrame(warm_pools)

    print("\n=== COLLECTING SCALING ACTIVITY ===")
    activities, activity_failures = collect_scaling_activities(regions)
    utils.log_success(f"Total scaling activities collected: {len(activities)}")
    if activities:
        data_frames['Scaling Activity'] = pd.DataFrame(activities)

    print("\n=== COLLECTING INSTANCE REFRESHES ===")
    refreshes, refresh_failures = collect_instance_refreshes(regions)
    utils.log_success(f"Total instance refreshes collected: {len(refreshes)}")
    if refreshes:
        data_frames['Instance Refreshes'] = pd.DataFrame(refreshes)

    failed_regions = _merge_failed_regions(
        failed_regions, warm_pool_failures, activity_failures, refresh_failures
    )

    # Export whatever succeeded first — a partial export is required even when
    # some regions failed (see the silent-collection-failure blast-radius audit).
    if data_frames:
        # STEP 4: Prepare all DataFrames for export
        for sheet_name in data_frames:
            data_frames[sheet_name] = utils.prepare_dataframe_for_export(data_frames[sheet_name])

        # STEP 5: Create filename and export
        current_date = datetime.datetime.now().strftime("%m.%d.%Y")
        final_excel_file = utils.create_export_filename(
            account_name,
            'autoscaling',
            region_suffix,
            current_date
        )

        # Save using utils module for consistent formatting
        try:
            output_path = utils.save_multiple_dataframes_to_excel(data_frames, final_excel_file)

            if output_path:
                utils.log_success("Auto Scaling data exported successfully!")
                utils.log_success(f"File location: {output_path}")
                utils.log_info(f"Export contains data from {len(regions)} AWS region(s)")

                # Summary of exported data
                for sheet_name, df in data_frames.items():
                    utils.log_info(f"  - {sheet_name}: {len(df)} records")
                    print(f"  - {sheet_name}: {len(df)} records")
            else:
                utils.log_error("Error creating Excel file. Please check the logs.")

        except Exception as e:
            utils.log_error("Error creating Excel file", e)
    elif not failed_regions:
        # Genuinely empty account: every region succeeded and returned nothing.
        utils.log_warning("No Auto Scaling Group data was collected. Nothing to export.")
        print("\nNo Auto Scaling Groups found in the selected region(s).")

    # If ANY region failed the primary ASG scope collection, make it loud:
    # write a marker and exit non-zero, even if some data was exported. A
    # partial export that looks complete is exactly the failure mode this guards.
    if failed_regions:
        utils.report_collection_failures(account_name, 'autoscaling', failed_regions)
        print(
            "\nERROR: Auto Scaling export completed with failures — data is incomplete. "
            "See the *-autoscaling-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


def main():
    # Initialize logging
    utils.setup_logging("autoscaling-export")
    SCRIPT_START_TIME = datetime.datetime.now()
    utils.log_script_start("autoscaling-export.py", "AWS Auto Scaling Groups Export Tool")

    try:
        # Print title and get account information
        account_id, account_name = utils.print_script_banner("AWS AUTO SCALING GROUPS EXPORT")

        # Check and install dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            sys.exit(1)

        # Check if account name is unknown
        if account_name == "unknown" and not utils.prompt_for_confirmation("Unable to determine account name. Proceed anyway?", default=False):
            print("Exiting script...")
            sys.exit(0)

        # Export Auto Scaling data
        export_autoscaling_data(account_id, account_name)

        print("\nAuto Scaling export script execution completed.")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        utils.log_info("Script cancelled by user")
        sys.exit(1)
    except Exception as e:
        utils.log_error("An unexpected error occurred", e)
        sys.exit(1)
    finally:
        utils.log_script_end("autoscaling-export.py", SCRIPT_START_TIME)


if __name__ == "__main__":
    main()
