#!/usr/bin/env python3
"""
AWS Health Export Script

Exports AWS Health service information including:
- Personal Health Dashboard (PHD) events
- Service Health Dashboard events
- Affected entities and resources
- Event details and timeline
- Organizational health events (if using AWS Organizations)
- Event types and categories

Features:
- Account-specific health events
- Organizational health events (multi-account)
- Affected resources tracking
- Event status and timeline
- Multi-region event aggregation
- Comprehensive multi-worksheet export

Note: Requires health:Describe* permissions
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Standard utils import pattern
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    if script_dir.name.lower() == 'scripts':
        sys.path.append(str(script_dir.parent))
    else:
        sys.path.append(str(script_dir))
    import utils
args = utils.parse_script_args("Export AWS Health events and affected entities to Excel")

@utils.aws_error_handler("Collecting Health events", default_return=[])
def collect_health_events(region: str, time_filter: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect AWS Health events for the account."""
    health = utils.get_boto3_client('health', region_name=region)
    events = []

    try:
        paginator = health.get_paginator('describe_events')
        for page in paginator.paginate(filter=time_filter):
            for event in page.get('events', []):
                events.append({
                    'Region': event.get('region', 'global'),
                    'EventArn': event.get('arn', 'N/A'),
                    'Service': event.get('service', 'N/A'),
                    'EventTypeCode': event.get('eventTypeCode', 'N/A'),
                    'EventTypeCategory': event.get('eventTypeCategory', 'N/A'),
                    'StatusCode': event.get('statusCode', 'N/A'),
                    'StartTime': event.get('startTime'),
                    'EndTime': event.get('endTime', 'N/A'),
                    'LastUpdatedTime': event.get('lastUpdatedTime', 'N/A'),
                    'EventScopeCode': event.get('eventScopeCode', 'N/A'),
                    'AvailabilityZone': event.get('availabilityZone', 'N/A'),
                })
    except Exception as e:
        # Health API might not be available or accessible
        utils.log_warning(f"Could not access Health API in {region}: {str(e)}")
        pass

    return events


@utils.aws_error_handler("Collecting event details", default_return={})
def get_event_details(region: str, event_arn: str) -> dict[str, Any]:
    """Get detailed information for a specific event."""
    health = utils.get_boto3_client('health', region_name=region)

    try:
        response = health.describe_event_details(eventArns=[event_arn])

        if response.get('successfulSet'):
            detail = response['successfulSet'][0]
            event_detail = detail.get('eventDescription', {})

            return {
                'EventArn': event_arn,
                'Description': event_detail.get('latestDescription', 'N/A'),
            }
    except Exception:
        pass

    return {'EventArn': event_arn, 'Description': 'N/A'}


@utils.aws_error_handler("Collecting affected entities", default_return=[])
def collect_affected_entities(region: str, event_arn: str) -> list[dict[str, Any]]:
    """Collect entities affected by a specific health event."""
    health = utils.get_boto3_client('health', region_name=region)
    entities = []

    try:
        paginator = health.get_paginator('describe_affected_entities')
        for page in paginator.paginate(filter={'eventArns': [event_arn]}):
            for entity in page.get('entities', []):
                entities.append({
                    'EventArn': event_arn,
                    'EntityArn': entity.get('entityArn', 'N/A'),
                    'EntityValue': entity.get('entityValue', 'N/A'),
                    'EntityUrl': entity.get('entityUrl', 'N/A'),
                    'AwsAccountId': entity.get('awsAccountId', 'N/A'),
                    'LastUpdatedTime': entity.get('lastUpdatedTime', 'N/A'),
                    'StatusCode': entity.get('statusCode', 'N/A'),
                    'Tags': str(entity.get('tags', {})) if entity.get('tags') else 'N/A',
                })
    except Exception:
        pass

    return entities


@utils.aws_error_handler("Collecting organizational events", default_return=[])
def collect_organizational_events(region: str, time_filter: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect organizational health events (requires AWS Organizations)."""
    health = utils.get_boto3_client('health', region_name=region)
    org_events = []

    try:
        paginator = health.get_paginator('describe_events_for_organization')
        for page in paginator.paginate(filter=time_filter):
            for event in page.get('events', []):
                org_events.append({
                    'Region': event.get('region', 'global'),
                    'EventArn': event.get('arn', 'N/A'),
                    'Service': event.get('service', 'N/A'),
                    'EventTypeCode': event.get('eventTypeCode', 'N/A'),
                    'EventTypeCategory': event.get('eventTypeCategory', 'N/A'),
                    'StatusCode': event.get('statusCode', 'N/A'),
                    'StartTime': event.get('startTime'),
                    'EndTime': event.get('endTime', 'N/A'),
                    'LastUpdatedTime': event.get('lastUpdatedTime', 'N/A'),
                    'EventScopeCode': event.get('eventScopeCode', 'N/A'),
                })
    except Exception as e:
        # Organizations integration might not be enabled
        utils.log_info(f"Organizational events not available: {str(e)}")
        pass

    return org_events


def _run_export(account_id: str, account_name: str) -> None:
    """Collect AWS Health data and write the Excel export."""
    # Health API endpoint: us-east-1 (Commercial global); GovCloud has a single endpoint in us-gov-west-1
    region = utils.get_partition_default_region()
    utils.log_info(f"AWS Health is a global service accessed through {region}.")

    # Ask user for time range
    if utils.is_auto_run():
        choice = 1
    else:
        try:
            choice = utils.prompt_menu(
                "HEALTH EVENT TIME RANGE",
                [
                    "Last 7 days",
                    "Last 30 days",
                    "Last 90 days",
                    "All events  (warning: may be large)",
                ],
            )
        except utils.BackSignal:
            sys.exit(10)
        except (utils.ExitToMainSignal, utils.QuitSignal):
            sys.exit(11)

    # Calculate time filter
    now = datetime.utcnow()
    if choice == 1:
        start_time = now - timedelta(days=7)
        time_desc = "last 7 days"
    elif choice == 2:
        start_time = now - timedelta(days=30)
        time_desc = "last 30 days"
    elif choice == 3:
        start_time = now - timedelta(days=90)
        time_desc = "last 90 days"
    else:
        start_time = now - timedelta(days=365*2)  # 2 years
        time_desc = "all available"

    time_filter = {
        'startTimes': [
            {
                'from': start_time
            }
        ]
    }

    utils.log_info(f"Collecting health events for {time_desc}...")

    # Collect account health events
    utils.log_info("Collecting account-level health events...")
    account_events = collect_health_events(region, time_filter)

    if account_events:
        utils.log_info(f"  Found {len(account_events)} account health event(s)")
    else:
        utils.log_info("  No account health events found")

    # Collect event details and affected entities
    event_details = []
    affected_entities = []

    if account_events:
        utils.log_info("Collecting event details and affected entities...")
        for idx, event in enumerate(account_events[:50], 1):  # Limit to first 50 for performance
            event_arn = event['EventArn']

            # Get details
            details = get_event_details(region, event_arn)
            event_details.append(details)

            # Get affected entities
            entities = collect_affected_entities(region, event_arn)
            affected_entities.extend(entities)

            if idx % 10 == 0:
                utils.log_info(f"  Processed {idx}/{min(len(account_events), 50)} events...")

    # Collect organizational events
    utils.log_info("Attempting to collect organizational health events...")
    org_events = collect_organizational_events(region, time_filter)

    if org_events:
        utils.log_info(f"  Found {len(org_events)} organizational health event(s)")
    else:
        utils.log_info("  No organizational health events found (may not have Organizations enabled)")

    # Create DataFrames
    df_account_events = utils.prepare_dataframe_for_export(pd.DataFrame(account_events))
    df_event_details = utils.prepare_dataframe_for_export(pd.DataFrame(event_details))
    df_affected = utils.prepare_dataframe_for_export(pd.DataFrame(affected_entities))
    df_org_events = utils.prepare_dataframe_for_export(pd.DataFrame(org_events))

    # Merge event details with account events
    df_events_with_details = df_account_events.copy()
    if not df_event_details.empty and not df_account_events.empty:
        df_events_with_details = pd.merge(
            df_account_events,
            df_event_details[['EventArn', 'Description']],
            on='EventArn',
            how='left'
        )

    # Create summary
    summary_data = []
    summary_data.append({'Metric': 'Total Account Events', 'Value': len(account_events)})
    summary_data.append({'Metric': 'Total Organizational Events', 'Value': len(org_events)})
    summary_data.append({'Metric': 'Total Affected Entities', 'Value': len(affected_entities)})
    summary_data.append({'Metric': 'Time Range', 'Value': time_desc})

    if not df_account_events.empty:
        open_events = len(df_account_events[df_account_events['StatusCode'] == 'open'])
        closed_events = len(df_account_events[df_account_events['StatusCode'] == 'closed'])
        upcoming_events = len(df_account_events[df_account_events['StatusCode'] == 'upcoming'])

        summary_data.append({'Metric': 'Open Events', 'Value': open_events})
        summary_data.append({'Metric': 'Closed Events', 'Value': closed_events})
        summary_data.append({'Metric': 'Upcoming Events', 'Value': upcoming_events})

        # Category breakdown
        if 'EventTypeCategory' in df_account_events.columns:
            issue_events = len(df_account_events[df_account_events['EventTypeCategory'] == 'issue'])
            scheduled_events = len(df_account_events[df_account_events['EventTypeCategory'] == 'scheduledChange'])
            account_notif = len(df_account_events[df_account_events['EventTypeCategory'] == 'accountNotification'])

            summary_data.append({'Metric': 'Issues', 'Value': issue_events})
            summary_data.append({'Metric': 'Scheduled Changes', 'Value': scheduled_events})
            summary_data.append({'Metric': 'Account Notifications', 'Value': account_notif})

    df_summary = utils.prepare_dataframe_for_export(pd.DataFrame(summary_data))

    # Create filtered views
    df_open_events = pd.DataFrame()
    df_issues = pd.DataFrame()

    if not df_events_with_details.empty:
        df_open_events = df_events_with_details[df_events_with_details['StatusCode'] == 'open']
        df_issues = df_events_with_details[df_events_with_details['EventTypeCategory'] == 'issue']

    # Export to Excel
    filename = utils.create_export_filename(account_name, 'health', 'all')

    sheets = {
        'Summary': df_summary,
        'All Events': df_events_with_details,
        'Open Events': df_open_events,
        'Issues': df_issues,
        'Affected Entities': df_affected,
        'Organizational Events': df_org_events,
    }

    utils.save_multiple_dataframes_to_excel(sheets, filename)

    # Log summary
    utils.log_info(f"  Account Events: {len(account_events)}")
    utils.log_info(f"  Organizational Events: {len(org_events)}")
    utils.log_info(f"  Affected Entities: {len(affected_entities)}")

    utils.log_success("AWS Health export completed successfully!")


def main():
    """Main function — 3-step state machine with b/x navigation."""
    try:
        if not utils.ensure_dependencies('pandas', 'openpyxl'):
            return
        global pd
        import pandas as pd
        utils.setup_logging("health-export")
        account_id, account_name = utils.print_script_banner("AWS HEALTH EVENTS EXPORT")

        step = 1
        regions = None

        while True:
            if step == 1:
                result = utils.prompt_region_selection(service_name="Health")
                if result == 'back':
                    sys.exit(10)
                if result == 'exit':
                    sys.exit(11)
                regions = result
                step = 2

            elif step == 2:
                region_str = ', '.join(regions) if len(regions) <= 3 else f"{len(regions)} regions"
                msg = f"Ready to export AWS Health events ({region_str})."
                result = utils.prompt_confirmation(msg)
                if result == 'back':
                    step = 1
                    continue
                if result == 'exit':
                    sys.exit(11)
                step = 3

            elif step == 3:
                _run_export(account_id, account_name)
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
