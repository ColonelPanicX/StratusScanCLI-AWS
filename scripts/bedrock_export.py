#!/usr/bin/env python3
"""
Amazon Bedrock Export Script for StratusScan

Exports comprehensive Amazon Bedrock generative AI information including:
- Foundation models available in the region
- Custom models (fine-tuned models)
- Model invocation logging configurations
- Guardrails for responsible AI
- Knowledge bases for RAG applications
- Agents for task automation

Output: Multi-worksheet Excel file with Bedrock resources
"""

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
args = utils.parse_script_args("Export AWS Bedrock models and usage to Excel")


def _build_foundation_model_row(model: dict[str, Any], region: str) -> dict[str, Any]:
    """Build the export row for a single Bedrock foundation model."""
    model_id = model.get('modelId', 'N/A')
    model_arn = model.get('modelArn', 'N/A')
    model_name = model.get('modelName', 'N/A')
    provider_name = model.get('providerName', 'N/A')

    # Input/output modalities
    input_modalities = model.get('inputModalities', [])
    output_modalities = model.get('outputModalities', [])
    input_str = ', '.join(input_modalities) if input_modalities else 'N/A'
    output_str = ', '.join(output_modalities) if output_modalities else 'N/A'

    # Response streaming
    response_streaming = model.get('responseStreamingSupported', False)

    # Customization supported
    customization_supported = model.get('customizationsSupported', [])
    customization_str = ', '.join(customization_supported) if customization_supported else 'None'

    # Inference types
    inference_types = model.get('inferenceTypesSupported', [])
    inference_str = ', '.join(inference_types) if inference_types else 'N/A'

    # Model lifecycle status
    model_lifecycle = model.get('modelLifecycle', {}) or {}
    lifecycle_status = model_lifecycle.get('status', 'N/A')

    return {
        'Region': region,
        'Model ID': model_id,
        'Model Name': model_name,
        'Provider': provider_name,
        'ARN': model_arn,
        'Lifecycle Status': lifecycle_status,
        'Input Modalities': input_str,
        'Output Modalities': output_str,
        'Response Streaming': response_streaming,
        'Customization Supported': customization_str,
        'Inference Types': inference_str
    }


def _scan_foundation_models_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Bedrock foundation models from a single region.

    This is a primary scope collector. It deliberately does NOT swallow
    errors: an API/permission failure here must propagate so
    ``scan_regions_concurrent(..., collect_failures=True)`` records the region
    as failed instead of silently reporting "no foundation models" (the
    silent-collection-loss bug — see
    ``.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md``).

    Individual malformed models are skipped (logged) rather than aborting the
    whole region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting foundation models in {region}...")
    bedrock_client = utils.get_boto3_client('bedrock', region_name=region)

    # list_foundation_models is not paginated (no token) — single call.
    response = bedrock_client.list_foundation_models()
    models = response.get('modelSummaries', [])

    region_models = []
    for model in models:
        try:
            region_models.append(_build_foundation_model_row(model, region))
        except Exception as e:
            utils.log_error(
                f"Skipping malformed foundation model in {region}: "
                f"{model.get('modelId', '<unknown>')}",
                e,
            )
            continue

    utils.log_info(f"Collected {len(region_models)} foundation models in {region}")
    return region_models


def collect_foundation_models(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock foundation model information across regions, surfacing
    failures.

    Uses ``collect_failures=True`` so a region whose collection errors is
    reported as a failed scope rather than silently collapsed into an empty
    result.

    Returns:
        tuple: ``(models, failed_regions)`` where ``failed_regions`` is a list
        of ``(region, error_message)`` tuples.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_foundation_models_region,
        show_progress=True,
        collect_failures=True,
    )
    all_models = [m for result in region_results for m in result]
    utils.log_info(f"Collected {len(all_models)} foundation models")
    return all_models, failed_regions


def _build_custom_model_row(model: dict[str, Any], region: str) -> dict[str, Any]:
    """Build the export row for a single Bedrock custom (fine-tuned) model."""
    model_arn = model.get('modelArn', 'N/A')
    model_name = model.get('modelName', 'N/A')
    base_model_arn = model.get('baseModelArn', 'N/A')

    creation_time = model.get('creationTime', 'N/A')
    if creation_time != 'N/A':
        creation_time = creation_time.strftime('%Y-%m-%d %H:%M:%S')

    # Extract base model name from ARN
    base_model_name = 'N/A'
    if base_model_arn != 'N/A' and '/' in base_model_arn:
        base_model_name = base_model_arn.split('/')[-1]

    customization_type = model.get('customizationType', 'N/A')

    return {
        'Region': region,
        'Model Name': model_name,
        'Model ARN': model_arn,
        'Base Model': base_model_name,
        'Base Model ARN': base_model_arn,
        'Customization Type': customization_type,
        'Created': creation_time
    }


def _scan_custom_models_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Bedrock custom models from a single region.

    Primary scope collector — raises on genuine API error so the caller can
    record the region as failed rather than empty. Individual malformed
    models are skipped (logged).
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting custom models in {region}...")
    bedrock_client = utils.get_boto3_client('bedrock', region_name=region)

    region_custom_models = []
    paginator = bedrock_client.get_paginator('list_custom_models')
    for page in paginator.paginate():
        models = page.get('modelSummaries', [])
        for model in models:
            try:
                region_custom_models.append(_build_custom_model_row(model, region))
            except Exception as e:
                utils.log_error(
                    f"Skipping malformed custom model in {region}: "
                    f"{model.get('modelArn', '<unknown>')}",
                    e,
                )
                continue

    utils.log_info(f"Collected {len(region_custom_models)} custom models in {region}")
    return region_custom_models


def collect_custom_models(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock custom model information across regions, surfacing
    failures via ``collect_failures=True``.

    Returns:
        tuple: ``(models, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_custom_models_region,
        show_progress=True,
        collect_failures=True,
    )
    all_custom_models = [m for result in region_results for m in result]
    utils.log_info(f"Collected {len(all_custom_models)} custom models")
    return all_custom_models, failed_regions


def _build_logging_config_row(logging_config: dict[str, Any], region: str) -> dict[str, Any]:
    """Build the export row for a single region's model invocation logging config."""
    cloudwatch_config = logging_config.get('cloudWatchConfig', {}) or {}
    cloudwatch_enabled = cloudwatch_config.get('logGroupName') is not None
    cloudwatch_log_group = cloudwatch_config.get('logGroupName', 'N/A')
    cloudwatch_role = cloudwatch_config.get('roleArn', 'N/A')

    s3_config = logging_config.get('s3Config', {}) or {}
    s3_enabled = s3_config.get('bucketName') is not None
    s3_bucket = s3_config.get('bucketName', 'N/A')
    s3_prefix = s3_config.get('keyPrefix', 'N/A')

    text_data_delivery = logging_config.get('textDataDeliveryEnabled', False)
    image_data_delivery = logging_config.get('imageDataDeliveryEnabled', False)
    embedding_data_delivery = logging_config.get('embeddingDataDeliveryEnabled', False)

    return {
        'Region': region,
        'CloudWatch Enabled': cloudwatch_enabled,
        'CloudWatch Log Group': cloudwatch_log_group,
        'CloudWatch Role ARN': cloudwatch_role,
        'S3 Enabled': s3_enabled,
        'S3 Bucket': s3_bucket,
        'S3 Key Prefix': s3_prefix,
        'Text Data Delivery': text_data_delivery,
        'Image Data Delivery': image_data_delivery,
        'Embedding Data Delivery': embedding_data_delivery
    }


def _scan_model_invocation_logging_region(region: str) -> list[dict[str, Any]]:
    """
    Collect the Bedrock model invocation logging configuration for a single
    region.

    Primary scope collector — raises on genuine API error so the caller can
    record the region as failed rather than empty.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting model invocation logging config in {region}...")
    bedrock_client = utils.get_boto3_client('bedrock', region_name=region)

    response = bedrock_client.get_model_invocation_logging_configuration()
    logging_config = response.get('loggingConfig', {})

    if not logging_config:
        return []

    try:
        return [_build_logging_config_row(logging_config, region)]
    except Exception as e:
        utils.log_error(f"Skipping malformed logging config in {region}", e)
        return []


def collect_model_invocation_logging(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock model invocation logging configurations across regions,
    surfacing failures via ``collect_failures=True``.

    Returns:
        tuple: ``(logging_configs, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_model_invocation_logging_region,
        show_progress=True,
        collect_failures=True,
    )
    all_logging_configs = [c for result in region_results for c in result]
    utils.log_info(f"Collected {len(all_logging_configs)} logging configurations")
    return all_logging_configs, failed_regions


def _build_guardrail_row(guardrail: dict[str, Any], region: str) -> dict[str, Any]:
    """Build the export row for a single Bedrock guardrail."""
    guardrail_id = guardrail.get('id', 'N/A')
    guardrail_arn = guardrail.get('arn', 'N/A')
    guardrail_name = guardrail.get('name', 'N/A')
    description = guardrail.get('description', 'N/A')
    version = guardrail.get('version', 'N/A')
    status = guardrail.get('status', 'N/A')

    created_at = guardrail.get('createdAt', 'N/A')
    if created_at != 'N/A':
        created_at = created_at.strftime('%Y-%m-%d %H:%M:%S')

    updated_at = guardrail.get('updatedAt', 'N/A')
    if updated_at != 'N/A':
        updated_at = updated_at.strftime('%Y-%m-%d %H:%M:%S')

    return {
        'Region': region,
        'Guardrail Name': guardrail_name,
        'Guardrail ID': guardrail_id,
        'ARN': guardrail_arn,
        'Version': version,
        'Status': status,
        'Description': description,
        'Created': created_at,
        'Updated': updated_at
    }


def _scan_guardrails_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Bedrock guardrails from a single region.

    Primary scope collector — raises on genuine API error so the caller can
    record the region as failed rather than empty. The exception is the
    op-availability probe below: ``list_guardrails`` is absent at the
    botocore floor in some environments (see Issue #224). A missing
    operation is a graceful skip, NOT a failed region — only errors raised
    by an actually-present operation propagate as failures.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting guardrails in {region}...")
    bedrock_client = utils.get_boto3_client('bedrock', region_name=region)

    # ListGuardrails first ships in botocore 1.34.90, below the declared floor
    # (Issue #213); the probe only fires when running below the floor. It checks
    # the client method, not service_model.operation_names -- those are
    # CamelCase ('ListGuardrails'), so a snake_case membership test was always
    # False and silently skipped guardrails on every SDK. Pages manually via
    # nextToken. A missing operation is a graceful skip, not a failed region.
    if not hasattr(bedrock_client, 'list_guardrails'):
        utils.log_warning(
            f"Bedrock guardrails API not in this boto3/botocore ({region}); guardrails "
            f"not collected. Upgrade with: {utils.sdk_upgrade_command_str()}"
        )
        return []

    region_guardrails = []
    next_token = None
    while True:
        params: dict[str, Any] = {'maxResults': 100}
        if next_token:
            params['nextToken'] = next_token
        page = bedrock_client.list_guardrails(**params)
        guardrails = page.get('guardrails', [])

        for guardrail in guardrails:
            try:
                region_guardrails.append(_build_guardrail_row(guardrail, region))
            except Exception as e:
                utils.log_error(
                    f"Skipping malformed guardrail in {region}: "
                    f"{guardrail.get('id', '<unknown>')}",
                    e,
                )
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_info(f"Collected {len(region_guardrails)} guardrails in {region}")
    return region_guardrails


def collect_guardrails(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock guardrail information across regions, surfacing failures
    via ``collect_failures=True``. The op-availability probe (missing
    ``list_guardrails`` operation) is a graceful per-region skip and never
    contributes to ``failed_regions``.

    Returns:
        tuple: ``(guardrails, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_guardrails_region,
        show_progress=True,
        collect_failures=True,
    )
    all_guardrails = [g for result in region_results for g in result]
    utils.log_info(f"Collected {len(all_guardrails)} guardrails")
    return all_guardrails, failed_regions


def _build_kb_row(kb: dict[str, Any], region: str, bedrock_agent_client) -> dict[str, Any]:
    """
    Build the export row for a single Bedrock knowledge base.

    Fetches enrichment details via ``get_knowledge_base``; that call is
    best-effort (a single knowledge base's detail lookup failing should not
    discard the base from the export) so it degrades to basic info on error
    rather than raising.
    """
    kb_id = kb.get('knowledgeBaseId', 'N/A')
    kb_name = kb.get('name', 'N/A')
    description = kb.get('description', 'N/A')
    status = kb.get('status', 'N/A')

    created_at = kb.get('createdAt', 'N/A')
    if created_at != 'N/A':
        created_at = created_at.strftime('%Y-%m-%d %H:%M:%S')

    updated_at = kb.get('updatedAt', 'N/A')
    if updated_at != 'N/A':
        updated_at = updated_at.strftime('%Y-%m-%d %H:%M:%S')

    kb_arn = 'N/A'
    role_arn = 'N/A'
    storage_type = 'N/A'

    try:
        kb_details = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=kb_id)
        kb_data = kb_details.get('knowledgeBase', {})

        kb_arn = kb_data.get('knowledgeBaseArn', 'N/A')
        role_arn = kb_data.get('roleArn', 'N/A')

        storage_config = kb_data.get('storageConfiguration', {})
        storage_type = storage_config.get('type', 'N/A')
    except Exception as e:
        utils.log_warning(f"Could not get details for knowledge base {kb_id}: {str(e)}")

    return {
        'Region': region,
        'Knowledge Base Name': kb_name,
        'Knowledge Base ID': kb_id,
        'ARN': kb_arn,
        'Status': status,
        'Description': description,
        'Storage Type': storage_type,
        'Role ARN': role_arn,
        'Created': created_at,
        'Updated': updated_at
    }


def _scan_knowledge_bases_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Bedrock knowledge bases from a single region.

    Primary scope collector — raises on genuine API error from the
    ``list_knowledge_bases`` listing call so the caller can record the region
    as failed rather than empty. Per-knowledge-base detail enrichment
    (``get_knowledge_base``) degrades gracefully inside ``_build_kb_row`` and
    does not fail the region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting knowledge bases in {region}...")
    bedrock_agent_client = utils.get_boto3_client('bedrock-agent', region_name=region)

    region_knowledge_bases = []
    paginator = bedrock_agent_client.get_paginator('list_knowledge_bases')
    for page in paginator.paginate():
        knowledge_bases = page.get('knowledgeBaseSummaries', [])
        for kb in knowledge_bases:
            try:
                region_knowledge_bases.append(_build_kb_row(kb, region, bedrock_agent_client))
            except Exception as e:
                utils.log_error(
                    f"Skipping malformed knowledge base in {region}: "
                    f"{kb.get('knowledgeBaseId', '<unknown>')}",
                    e,
                )
                continue

    utils.log_info(f"Collected {len(region_knowledge_bases)} knowledge bases in {region}")
    return region_knowledge_bases


def collect_knowledge_bases(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock knowledge base information across regions, surfacing
    failures via ``collect_failures=True``.

    Returns:
        tuple: ``(knowledge_bases, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_knowledge_bases_region,
        show_progress=True,
        collect_failures=True,
    )
    all_knowledge_bases = [kb for result in region_results for kb in result]
    utils.log_info(f"Collected {len(all_knowledge_bases)} knowledge bases")
    return all_knowledge_bases, failed_regions


def _build_agent_row(agent: dict[str, Any], region: str, bedrock_agent_client) -> dict[str, Any]:
    """
    Build the export row for a single Bedrock agent.

    Fetches enrichment details via ``get_agent``; that call is best-effort (a
    single agent's detail lookup failing should not discard the agent from
    the export) so it degrades to basic info on error rather than raising.
    """
    agent_id = agent.get('agentId', 'N/A')
    agent_name = agent.get('agentName', 'N/A')
    agent_status = agent.get('agentStatus', 'N/A')
    description = agent.get('description', 'N/A')
    latest_agent_version = agent.get('latestAgentVersion', 'N/A')

    created_at = agent.get('createdAt', 'N/A')
    if created_at != 'N/A':
        created_at = created_at.strftime('%Y-%m-%d %H:%M:%S')

    updated_at = agent.get('updatedAt', 'N/A')
    if updated_at != 'N/A':
        updated_at = updated_at.strftime('%Y-%m-%d %H:%M:%S')

    agent_arn = 'N/A'
    agent_resource_role_arn = 'N/A'
    foundation_model = 'N/A'
    idle_session_ttl = 'N/A'

    try:
        agent_details = bedrock_agent_client.get_agent(agentId=agent_id)
        agent_data = agent_details.get('agent', {})

        agent_arn = agent_data.get('agentArn', 'N/A')
        agent_resource_role_arn = agent_data.get('agentResourceRoleArn', 'N/A')
        foundation_model = agent_data.get('foundationModel', 'N/A')
        idle_session_ttl = agent_data.get('idleSessionTTLInSeconds', 'N/A')
    except Exception as e:
        utils.log_warning(f"Could not get details for agent {agent_id}: {str(e)}")

    return {
        'Region': region,
        'Agent Name': agent_name,
        'Agent ID': agent_id,
        'ARN': agent_arn,
        'Status': agent_status,
        'Latest Version': latest_agent_version,
        'Foundation Model': foundation_model,
        'Description': description,
        'Role ARN': agent_resource_role_arn,
        'Idle Session TTL (seconds)': idle_session_ttl,
        'Created': created_at,
        'Updated': updated_at
    }


def _scan_agents_region(region: str) -> list[dict[str, Any]]:
    """
    Collect Bedrock agents from a single region.

    Primary scope collector — raises on genuine API error from the
    ``list_agents`` listing call so the caller can record the region as
    failed rather than empty. Per-agent detail enrichment (``get_agent``)
    degrades gracefully inside ``_build_agent_row`` and does not fail the
    region.
    """
    if not utils.is_aws_region(region):
        utils.log_error(f"Skipping invalid AWS region: {region}")
        return []

    utils.log_info(f"Collecting agents in {region}...")
    bedrock_agent_client = utils.get_boto3_client('bedrock-agent', region_name=region)

    region_agents = []
    paginator = bedrock_agent_client.get_paginator('list_agents')
    for page in paginator.paginate():
        agents = page.get('agentSummaries', [])
        for agent in agents:
            try:
                region_agents.append(_build_agent_row(agent, region, bedrock_agent_client))
            except Exception as e:
                utils.log_error(
                    f"Skipping malformed agent in {region}: "
                    f"{agent.get('agentId', '<unknown>')}",
                    e,
                )
                continue

    utils.log_info(f"Collected {len(region_agents)} agents in {region}")
    return region_agents


def collect_agents(regions: list[str]) -> tuple[list[dict[str, Any]], list]:
    """
    Collect Bedrock agent information across regions, surfacing failures via
    ``collect_failures=True``.

    Returns:
        tuple: ``(agents, failed_regions)``.
    """
    region_results, failed_regions = utils.scan_regions_concurrent(
        regions=regions,
        scan_function=_scan_agents_region,
        show_progress=True,
        collect_failures=True,
    )
    all_agents = [a for result in region_results for a in result]
    utils.log_info(f"Collected {len(all_agents)} agents")
    return all_agents, failed_regions


def generate_summary(foundation_models: list[dict[str, Any]],
                     custom_models: list[dict[str, Any]],
                     logging_configs: list[dict[str, Any]],
                     guardrails: list[dict[str, Any]],
                     knowledge_bases: list[dict[str, Any]],
                     agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for Bedrock resources."""
    utils.log_info("Generating summary statistics...")

    summary = []

    # Foundation models summary
    total_foundation = len(foundation_models)
    summary.append({
        'Metric': 'Total Foundation Models',
        'Count': total_foundation,
        'Details': 'Available pre-trained models from various providers'
    })

    # Model providers
    if foundation_models:
        df = pd.DataFrame(foundation_models)
        providers = df['Provider'].value_counts().to_dict()
        for provider, count in providers.items():
            summary.append({
                'Metric': f'Models from {provider}',
                'Count': count,
                'Details': 'Foundation model provider'
            })

    # Custom models
    total_custom = len(custom_models)
    summary.append({
        'Metric': 'Total Custom Models',
        'Count': total_custom,
        'Details': 'Fine-tuned models based on foundation models'
    })

    # Logging configs
    total_logging = len(logging_configs)
    cloudwatch_enabled = sum(1 for c in logging_configs if c.get('CloudWatch Enabled', False))
    s3_enabled = sum(1 for c in logging_configs if c.get('S3 Enabled', False))

    summary.append({
        'Metric': 'Regions with Logging Configured',
        'Count': total_logging,
        'Details': f'CloudWatch: {cloudwatch_enabled}, S3: {s3_enabled}'
    })

    # Guardrails
    total_guardrails = len(guardrails)
    summary.append({
        'Metric': 'Total Guardrails',
        'Count': total_guardrails,
        'Details': 'Responsible AI guardrails for content filtering'
    })

    # Knowledge bases
    total_kb = len(knowledge_bases)
    summary.append({
        'Metric': 'Total Knowledge Bases',
        'Count': total_kb,
        'Details': 'Knowledge bases for RAG (Retrieval Augmented Generation)'
    })

    # Agents
    total_agents = len(agents)
    summary.append({
        'Metric': 'Total Agents',
        'Count': total_agents,
        'Details': 'Bedrock agents for autonomous task execution'
    })

    # Regional distribution
    if foundation_models:
        df = pd.DataFrame(foundation_models)
        regions = df['Region'].value_counts().to_dict()
        for region, count in regions.items():
            summary.append({
                'Metric': f'Foundation Models in {region}',
                'Count': count,
                'Details': 'Regional model availability'
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

    partition = utils.detect_partition()
    if not utils.is_service_available_in_partition("bedrock", partition):
        utils.log_warning("Amazon Bedrock is not available in AWS GovCloud. Skipping.")
        sys.exit(0)

    account_id, account_name = utils.print_script_banner("AWS AMAZON BEDROCK EXPORT")
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Please check your credentials.")
        return

    utils.log_info(f"AWS Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Detect partition for region examples
    regions = utils.prompt_region_selection()
    # Collect data
    print("\nCollecting Amazon Bedrock data...")

    # Each scope collector surfaces its own (region, error) failures; region
    # failures must propagate as failed scopes, never collapse into "empty"
    # (see .collab/audit/07.16.2026-silent-collection-failure-blast-radius.md).
    # The guardrails op-availability probe (missing operation) is a graceful
    # skip and never contributes to its failed_regions list.
    foundation_models, failed_fm = collect_foundation_models(regions)
    custom_models, failed_cm = collect_custom_models(regions)
    logging_configs, failed_log = collect_model_invocation_logging(regions)
    guardrails, failed_gr = collect_guardrails(regions)
    knowledge_bases, failed_kb = collect_knowledge_bases(regions)
    agents, failed_ag = collect_agents(regions)
    summary = generate_summary(foundation_models, custom_models, logging_configs,
                                guardrails, knowledge_bases, agents)

    # Merge every scope's failed regions into one combined list, tagged with
    # the scope that failed, so a single marker + exit covers all of them.
    failed_regions = (
        [(f"foundation_models/{region}", err) for region, err in failed_fm]
        + [(f"custom_models/{region}", err) for region, err in failed_cm]
        + [(f"model_invocation_logging/{region}", err) for region, err in failed_log]
        + [(f"guardrails/{region}", err) for region, err in failed_gr]
        + [(f"knowledge_bases/{region}", err) for region, err in failed_kb]
        + [(f"agents/{region}", err) for region, err in failed_ag]
    )

    # Create DataFrames
    utils.log_info("Creating DataFrames...")

    dataframes = {}

    if foundation_models:
        df_foundation = pd.DataFrame(foundation_models)
        df_foundation = utils.prepare_dataframe_for_export(df_foundation)
        dataframes['Foundation Models'] = df_foundation

    if custom_models:
        df_custom = pd.DataFrame(custom_models)
        df_custom = utils.prepare_dataframe_for_export(df_custom)
        dataframes['Custom Models'] = df_custom

    if logging_configs:
        df_logging = pd.DataFrame(logging_configs)
        df_logging = utils.prepare_dataframe_for_export(df_logging)
        dataframes['Invocation Logging'] = df_logging

    if guardrails:
        df_guardrails = pd.DataFrame(guardrails)
        df_guardrails = utils.prepare_dataframe_for_export(df_guardrails)
        dataframes['Guardrails'] = df_guardrails

    if knowledge_bases:
        df_kb = pd.DataFrame(knowledge_bases)
        df_kb = utils.prepare_dataframe_for_export(df_kb)
        dataframes['Knowledge Bases'] = df_kb

    if agents:
        df_agents = pd.DataFrame(agents)
        df_agents = utils.prepare_dataframe_for_export(df_agents)
        dataframes['Agents'] = df_agents

    if summary:
        df_summary = pd.DataFrame(summary)
        df_summary = utils.prepare_dataframe_for_export(df_summary)
        dataframes['Summary'] = df_summary

    # Export whatever succeeded first — a partial export is required even
    # when some regions failed (see the silent-collection-failure blast-radius
    # audit).
    if dataframes:
        region_suffix = 'all-regions' if len(regions) > 1 else regions[0]
        filename = utils.create_export_filename(account_name, 'bedrock', region_suffix)

        utils.log_info(f"Exporting to {filename}...")
        utils.save_multiple_dataframes_to_excel(dataframes, filename)

        utils.log_success("Amazon Bedrock export completed successfully")
    elif not failed_regions:
        # Genuinely empty account: every scope/region succeeded and returned
        # nothing.
        utils.log_warning("No Amazon Bedrock data found to export")

    # If ANY scope failed collection in ANY region, make it loud: write a
    # marker and exit non-zero, even if some data was exported. A partial
    # export that looks complete is exactly the failure mode this guards
    # against.
    if failed_regions:
        utils.report_collection_failures(account_name, 'bedrock', failed_regions)
        print(
            "\nERROR: Bedrock export completed with failures — data is incomplete. "
            "See the *-bedrock-FAILED-*.txt marker in the output directory."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
