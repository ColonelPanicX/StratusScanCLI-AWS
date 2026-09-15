#!/usr/bin/env python3

"""
===========================
= AWS RESOURCE SCANNER =
===========================

Title: AWS Account Billing Data Export
Date: MAR-04-2025

Description:
Exports AWS billing data for specified time periods (monthly or last 12 months),
organized by service and associated cost. Handles AWS Cost Explorer
limitations for historical data access and provides alternatives
for accessing older billing data.
"""

import datetime
import os
import re
import sys
from pathlib import Path

from botocore.exceptions import ClientError

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
args = utils.parse_script_args("Export AWS billing and cost data to Excel")

# Setup logging
logger = utils.setup_logging('billing-export')

# Cost metric requested from and read back out of Cost Explorer. Both sites
# must use the same key or every group raises KeyError. NetUnblendedCost is
# per-account cost after discounts (not averaged across a consolidated
# billing family the way BlendedCost is).
COST_METRIC = 'NetUnblendedCost'

# Skip-marker naming. Must never match '*billing-last-12-months-export-*.xlsx'
# or carry a 'Summary' sheet -- downstream readers treat those as spend data.
SKIP_MARKER_SUFFIX = 'skipped-no-permission'
GOVCLOUD_SKIP_MARKER_SUFFIX = 'skipped-govcloud'
SKIP_MARKER_SHEET = 'Skipped'

ABOUT_SHEET = 'About'

# About-sheet wording. Kept to what AWS documents:
# - NetUnblendedCost: "reflects the cost after discounts" (Cost Explorer
#   user guide, ce-advanced). The query applies no RECORD_TYPE filter, so
#   credit/refund line items visible to this account are in the totals.
# - Member-account visibility of refunds, credits and discounts is a
#   management-account Cost Explorer preference (user guide, ce-access).
# - GovCloud usage is billed through, and reported in, the associated
#   standard account (GovCloud user guide, usage-and-payment).
METRIC_MEANING = (
    "Net unblended cost: AWS defines it as the cost after discounts. No record-type "
    "filter is applied, so credits and refunds visible to this account are included. "
    "Closest Cost Explorer metric to the amount actually paid."
)
DATA_SCOPE = "Costs visible to this account's Cost Explorer only"
ORG_CAVEAT = (
    "If this account is an AWS Organizations member, visibility of credits, refunds "
    "and discounts is controlled by the management account's Cost Explorer "
    "preferences. If hidden, these figures will not reflect them."
)
GOVCLOUD_NOTE = (
    "GovCloud usage is billed through the associated standard (commercial) account "
    "and is combined into that account's usage reports. Run from that associated "
    "account, these figures may include GovCloud spend; it is not separated out here."
)


class BillingPermissionDenied(Exception):
    """Identity lacks Cost Explorer read permission (skip, not failure)."""

    def __init__(self, error_code, error_message):
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message


def validate_date_input(date_input):
    """
    Validate user input for last 12 months or month-year.

    Args:
        date_input (str): User input string

    Returns:
        tuple: (is_valid, is_year_only, start_date, end_date)
            end_date is EXCLUSIVE (first day after the period), matching the
            Cost Explorer TimePeriod contract.
    """
    # Define regex patterns
    month_year_pattern = r'^(0[1-9]|1[0-2])-\d{4}$'  # MM-YYYY
    last_12_pattern = r'^last\s*12$'  # "last 12" with flexible spacing

    if re.match(last_12_pattern, date_input.lower()):
        # Last 12 complete calendar months, excluding the current month.
        today = datetime.datetime.now()
        # Cost Explorer TimePeriod.End is exclusive: first day of the current
        # month covers the previous month through its final day.
        end_date = datetime.datetime(today.year, today.month, 1)
        # Same month one year earlier: [start, end) spans exactly 12 months
        # (e.g. run 2026-09-15 -> 2025-09-01 .. 2026-09-01 = Sep 2025..Aug 2026).
        start_date = datetime.datetime(end_date.year - 1, end_date.month, 1)
        return True, False, start_date, end_date

    elif re.match(month_year_pattern, date_input):
        # Month-Year format (MM-YYYY)
        month, year = date_input.split('-')
        month = int(month)
        year = int(year)

        start_date = datetime.datetime(year, month, 1)
        # Exclusive end (TimePeriod.End): first day of the following month
        if month == 12:
            end_date = datetime.datetime(year + 1, 1, 1)
        else:
            end_date = datetime.datetime(year, month + 1, 1)

        return True, False, start_date, end_date

    else:
        return False, None, None, None

def check_cost_explorer_data_retention():
    """
    Return Cost Explorer data retention limits.

    The CE preferences API does not exist in boto3 — always assume standard
    14-month retention. Extended retention is enabled in the AWS console under
    Cost Explorer > Settings and does not need to be queried at runtime.

    Returns:
        tuple: (has_extended_retention, max_months)
    """
    return False, 14

def validate_date_range(start_date, end_date):
    """
    Validate the date range against AWS Cost Explorer limitations.

    Args:
        start_date (datetime): Start date
        end_date (datetime): End date (exclusive)

    Returns:
        tuple: (is_valid, message, retention_months)
    """
    today = datetime.datetime.now()

    # Cost Explorer data is available the next day
    latest_available_date = today - datetime.timedelta(days=1)

    # Check if extended data retention is enabled
    has_extended_retention, retention_months = check_cost_explorer_data_retention()

    # Calculate earliest available date based on retention period
    earliest_available_date = today - datetime.timedelta(days=retention_months * 30)

    # Check if the last included day is in the future (end_date is exclusive)
    last_included_day = end_date - datetime.timedelta(days=1)
    if last_included_day > latest_available_date:
        end_date_str = last_included_day.strftime('%Y-%m-%d')
        latest_date_str = latest_available_date.strftime('%Y-%m-%d')
        return False, f"End date ({end_date_str}) is in the future. Latest available data is for {latest_date_str}.", retention_months

    # Check if start date is too far in the past
    if start_date < earliest_available_date:
        start_date_str = start_date.strftime('%Y-%m-%d')
        earliest_date_str = earliest_available_date.strftime('%Y-%m-%d')
        retention_text = f"{retention_months} months"
        if has_extended_retention:
            retention_text += " (with extended retention enabled)"
        else:
            retention_text += " (standard retention)"

        return False, f"Start date ({start_date_str}) is too far in the past. AWS Cost Explorer only provides data for {retention_text}, available from {earliest_date_str}.", retention_months

    return True, "Date range is valid.", retention_months

def get_billing_data(start_date, end_date):
    """
    Get billing data from AWS Cost Explorer API.

    Follows NextPageToken until exhausted: GetCostAndUsage has no boto3
    paginator, and a grouped query can split one month's groups across pages.

    Args:
        start_date (datetime): Start date (inclusive)
        end_date (datetime): End date (exclusive, per the TimePeriod contract)

    Returns:
        dict: Billing data organized by month and service

    Raises:
        BillingPermissionDenied: identity lacks Cost Explorer read permission.
    """
    # Convert dates to string format required by AWS API
    start_date_str = start_date.strftime('%Y-%m-%d')
    end_date_str = end_date.strftime('%Y-%m-%d')
    last_day_str = (end_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"Fetching billing data from {start_date_str} through {last_day_str}...")

    # Create a Cost Explorer client
    ce_client = utils.get_boto3_client('ce')

    billing_data = {}
    next_token = None

    try:
        while True:
            params = {
                'TimePeriod': {
                    'Start': start_date_str,
                    'End': end_date_str
                },
                'Granularity': 'MONTHLY',
                'Metrics': [COST_METRIC],
                'GroupBy': [
                    {
                        'Type': 'DIMENSION',
                        'Key': 'SERVICE'
                    }
                ]
            }
            if next_token:
                params['NextPageToken'] = next_token

            response = ce_client.get_cost_and_usage(**params)

            for result in response.get('ResultsByTime', []):
                period_start = result['TimePeriod']['Start']
                month = datetime.datetime.strptime(period_start, '%Y-%m-%d').strftime('%Y-%m')
                month_data = billing_data.setdefault(month, {})

                for group in result.get('Groups', []):
                    service_name = group['Keys'][0]
                    cost = float(group['Metrics'][COST_METRIC]['Amount'])
                    # The same (month, service) can appear on more than one
                    # page, so accumulate rather than overwrite.
                    month_data[service_name] = month_data.get(service_name, 0.0) + cost

            next_token = response.get('NextPageToken')
            if not next_token:
                break

        return billing_data

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        error_message = e.response.get('Error', {}).get('Message', str(e))

        if error_code == 'ValidationException' and 'historical data' in error_message:
            print("\nError: AWS Cost Explorer cannot access historical data beyond the default 14 months.")
            print("This is a limitation of AWS Cost Explorer. To access older data:")
            print("1. Enable extended data retention in Cost Explorer settings (up to 14 additional months)")
            print("   - Sign in to AWS Management Console")
            print("   - Go to AWS Cost Management > Cost Explorer > Settings")
            print("   - Under 'Data retention', enable 'Extended retention'")
            print("2. For data older than 28 months, set up AWS Cost and Usage Reports (CUR)")
            print("   - Go to AWS Cost Management > Cost & Usage Reports")
            print("   - Set up a report to be delivered to an S3 bucket")
            sys.exit(1)
        elif error_code in ('AccessDeniedException', 'AccessDenied', 'UnauthorizedOperation'):
            raise BillingPermissionDenied(error_code, error_message) from e
        else:
            print(f"\nError accessing Cost Explorer: {error_message}")
            sys.exit(1)


def write_skip_marker(account_name, suffix, rows):
    """
    Write a small workbook recording that the billing export was skipped.

    Billing is a mandatory Smart Scan script, so a skip must not fail the run
    -- but a bare exit 0 with no artifact makes the skip invisible in the
    zipped audit bundle. Markers use a distinct filename suffix and a
    'Skipped' sheet so nothing that reads real billing exports (sheet
    'Summary', '*billing-last-12-months-export-*.xlsx') mistakes them for
    spend data.

    Args:
        account_name (str): account name for the filename
        suffix (str): filename suffix, e.g. SKIP_MARKER_SUFFIX
        rows (list[tuple[str, str]]): (Field, Value) rows after Status

    Returns:
        Path: path to the marker workbook
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SKIP_MARKER_SHEET
    ws.append(('Status', 'SKIPPED'))
    for row in rows:
        ws.append(row)
    ws.append(('Recorded', datetime.datetime.now().strftime('%m.%d.%Y %H:%M:%S')))

    # Always .xlsx: this is an openpyxl workbook, and Smart Scan only
    # attributes/zips *.xlsx outputs.
    filename = utils.create_export_filename(
        account_name, "billing", suffix,
        datetime.datetime.now().strftime("%m.%d.%Y"), fmt="xlsx"
    )
    output_path = utils.get_output_filepath(filename)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    wb.save(output_path)
    return output_path


def write_permission_skip_marker(account_name, error_code, error_message):
    """Marker for an identity lacking Cost Explorer permissions."""
    return write_skip_marker(account_name, SKIP_MARKER_SUFFIX, [
        ('Reason', 'Identity lacks Cost Explorer permissions'),
        ('Error Code', error_code),
        ('Error Message', error_message),
        ('Required Permission', 'ce:GetCostAndUsage'),
    ])


def write_govcloud_skip_marker(account_name, account_id):
    """Marker for a run in the aws-us-gov partition (no Cost Explorer)."""
    return write_skip_marker(account_name, GOVCLOUD_SKIP_MARKER_SUFFIX, [
        ('Reason', 'Cost Explorer is unavailable in the aws-us-gov partition. GovCloud '
                   'usage is billed through the associated standard (commercial) account; '
                   'run the billing export there.'),
        ('Account ID', account_id),
        ('Partition', 'aws-us-gov'),
    ])


def create_excel_report(billing_data, account_name, date_suffix,
                        account_id=None, start_date=None, end_date=None):
    """
    Create an Excel report with monthly billing data.

    Sheet order: 'Summary' (first, contract unchanged), 'About' (provenance:
    metric, period, scope caveats), then one sheet per month.

    Args:
        billing_data (dict): Billing data organized by month and service
        account_name (str): Name of AWS account for file naming
        date_suffix (str): Date suffix for filename
        account_id (str): AWS account ID, shown on the About sheet
        start_date (datetime): Period start (inclusive)
        end_date (datetime): Period end (exclusive, Cost Explorer contract)

    Returns:
        str: Path to the created Excel file
    """
    # Import required modules here
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    # Create a new workbook
    wb = Workbook()

    # Remove the default sheet
    default_sheet = wb.active
    wb.remove(default_sheet)

    # Define styles
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="D3D3D3", end_color="D3D3D3", fill_type="solid")

    # Sort months chronologically
    sorted_months = sorted(billing_data.keys())

    # Create a summary sheet
    summary_sheet = wb.create_sheet("Summary")
    # Created now so it sits directly after Summary. Month sheets are named
    # '%b %Y' (e.g. 'Aug 2026'), which always contains a space and a year, so
    # they cannot collide with 'About'.
    about_sheet = wb.create_sheet(ABOUT_SHEET)
    summary_sheet['A1'] = 'Month'
    summary_sheet['B1'] = 'Total Cost (USD)'

    # Apply header styles to summary sheet
    summary_sheet['A1'].font = header_font
    summary_sheet['B1'].font = header_font
    summary_sheet['A1'].fill = header_fill
    summary_sheet['B1'].fill = header_fill

    summary_row = 2
    total_all_months = 0

    # Process each month
    for month in sorted_months:
        # Create a sheet for the month
        sheet_name = datetime.datetime.strptime(month, '%Y-%m').strftime('%b %Y')
        ws = wb.create_sheet(sheet_name)

        # Create headers
        ws['A1'] = 'Service'
        ws['B1'] = 'Cost (USD)'

        # Apply header styles
        ws['A1'].font = header_font
        ws['B1'].font = header_font
        ws['A1'].fill = header_fill
        ws['B1'].fill = header_fill

        # Get monthly data
        month_data = billing_data[month]

        # Sort services by cost (descending)
        sorted_services = sorted(month_data.items(), key=lambda x: x[1], reverse=True)

        # Add data rows
        row = 2
        total_cost = 0

        for service, cost in sorted_services:
            ws[f'A{row}'] = service
            ws[f'B{row}'] = cost
            ws[f'B{row}'].number_format = '$#,##0.00'
            total_cost += cost
            row += 1

        # Add total row
        row += 1
        ws[f'A{row}'] = 'Total'
        ws[f'A{row}'].font = header_font
        ws[f'B{row}'] = total_cost
        ws[f'B{row}'].font = header_font
        ws[f'B{row}'].number_format = '$#,##0.00'

        # Adjust column widths
        for col in range(1, 3):
            column_letter = get_column_letter(col)
            max_length = 0
            for cell in ws[column_letter]:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            adjusted_width = max_length + 2
            ws.column_dimensions[column_letter].width = adjusted_width

        # Add entry to summary sheet
        display_month = datetime.datetime.strptime(month, '%Y-%m').strftime('%B %Y')
        summary_sheet[f'A{summary_row}'] = display_month
        summary_sheet[f'B{summary_row}'] = total_cost
        summary_sheet[f'B{summary_row}'].number_format = '$#,##0.00'
        summary_row += 1
        total_all_months += total_cost

    # Provenance sheet
    about_rows = [
        ('Account ID', account_id or 'UNKNOWN'),
        ('Account Name', account_name),
        ('Cost Metric', COST_METRIC),
        ('Metric Meaning', METRIC_MEANING),
        ('Period Start (inclusive)', start_date.strftime('%Y-%m-%d') if start_date else 'N/A'),
        ('Period End (inclusive)',
         (end_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d') if end_date else 'N/A'),
        ('Generated', datetime.datetime.now().strftime('%m.%d.%Y %H:%M:%S')),
        ('Tool Version', utils.get_version()),
        ('Data Scope', DATA_SCOPE),
        ('Org Caveat', ORG_CAVEAT),
        ('GovCloud Note', GOVCLOUD_NOTE),
    ]
    about_sheet['A1'] = 'Field'
    about_sheet['B1'] = 'Value'
    for cell in (about_sheet['A1'], about_sheet['B1']):
        cell.font = header_font
        cell.fill = header_fill
    for idx, (field, value) in enumerate(about_rows, start=2):
        about_sheet[f'A{idx}'] = field
        about_sheet[f'B{idx}'] = value
    about_sheet.column_dimensions['A'].width = max(len(f) for f, _ in about_rows) + 2
    about_sheet.column_dimensions['B'].width = 100

    # Add total row to summary
    summary_sheet[f'A{summary_row}'] = 'Total All Months'
    summary_sheet[f'B{summary_row}'] = total_all_months
    summary_sheet[f'A{summary_row}'].font = header_font
    summary_sheet[f'B{summary_row}'].font = header_font
    summary_sheet[f'B{summary_row}'].number_format = '$#,##0.00'

    # Adjust summary sheet column widths
    for col in range(1, 3):
        column_letter = get_column_letter(col)
        max_length = 0
        for cell in summary_sheet[column_letter]:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        adjusted_width = max_length + 2
        summary_sheet.column_dimensions[column_letter].width = adjusted_width

    # Generate filename using utils
    filename = utils.create_export_filename(
        account_name,
        "billing",
        date_suffix,
        datetime.datetime.now().strftime("%m.%d.%Y")
    )

    # Get the full output path
    output_path = utils.get_output_filepath(filename)

    # Ensure the output directory exists
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    # Save the workbook
    wb.save(output_path)
    print(f"Excel report saved as: {output_path}")
    return output_path

def main():
    """
    Main function to run the script.
    """
    try:
        # Check partition availability
        partition = utils.detect_partition()
        if not utils.is_service_available_in_partition("ce", partition):
            utils.log_warning(
                "Cost Explorer (billing) is not available in AWS GovCloud. GovCloud usage "
                "is billed through the associated commercial account. Skipping."
            )
            # Account lookup (STS) must not turn a clean skip into a failure.
            try:
                gov_account_id, gov_account_name = utils.get_account_info()
            except Exception as lookup_err:
                utils.log_warning(f"Could not resolve account for GovCloud skip marker: {lookup_err}")
                gov_account_id, gov_account_name = "UNKNOWN", "UNKNOWN-ACCOUNT"
            try:
                marker_path = write_govcloud_skip_marker(gov_account_name, gov_account_id)
                utils.log_warning(f"Billing skip recorded in: {marker_path}")
            except Exception as marker_err:
                utils.log_warning(f"Could not write billing skip marker: {marker_err}")
            sys.exit(0)

        # Print title and get account info
        account_id, account_name = utils.print_script_banner("AWS ACCOUNT BILLING DATA EXPORT")

        # Check dependencies
        if not utils.ensure_dependencies('pandas', 'openpyxl', 'dateutil'):
            sys.exit(1)

        # Get user input for date range — fully menu-driven (single-voice):
        # "Last 12 months" plus the 12 most recent complete months, no free-text.
        if utils.is_auto_run():
            date_input = "last 12"
            _, _, start_date, end_date = validate_date_input(date_input)
        else:
            today = datetime.datetime.now()
            month_options = []  # (value "MM-YYYY", label "Month YYYY"), most recent first
            y, m = today.year, today.month
            for _ in range(12):
                m -= 1
                if m == 0:
                    m, y = 12, y - 1
                month_options.append(
                    (f"{m:02d}-{y}", datetime.datetime(y, m, 1).strftime("%B %Y"))
                )

            menu_labels = ["Last 12 months"] + [label for _, label in month_options]
            try:
                choice = utils.prompt_menu("BILLING PERIOD", menu_labels)
            except utils.BackSignal:
                sys.exit(10)
            except (utils.ExitToMainSignal, utils.QuitSignal):
                sys.exit(11)

            date_input = "last 12" if choice == 1 else month_options[choice - 2][0]
            _, _, start_date, end_date = validate_date_input(date_input)

            # Menu values are always well-formed; still guard the retention range.
            date_valid, message, _ = validate_date_range(start_date, end_date)
            if not date_valid:
                print(f"{utils.GLYPH_FAIL} {message}")
                sys.exit(1)

        # Get billing data
        try:
            billing_data = get_billing_data(start_date, end_date)
        except BillingPermissionDenied as denied:
            # Mandatory script: a missing permission must not fail the run,
            # but it must leave a visible trace in the log and the output.
            utils.log_warning(
                "Skipping billing export: this identity lacks Cost Explorer "
                f"permissions ({denied.error_code}). Grant 'ce:GetCostAndUsage' "
                "(read-only) to include billing in the audit."
            )
            try:
                marker_path = write_permission_skip_marker(
                    account_name, denied.error_code, denied.error_message
                )
                utils.log_warning(f"Billing skip recorded in: {marker_path}")
            except Exception as marker_err:
                utils.log_warning(f"Could not write billing skip marker: {marker_err}")
            sys.exit(0)

        if not billing_data:
            print("\nNo billing data found for the specified period.")
            sys.exit(0)

        # Determine output file name suffix
        if date_input.lower().startswith('last'):
            date_suffix = "last-12-months"
        else:
            date_suffix = start_date.strftime('%m-%Y')

        # Create Excel report
        output_file = create_excel_report(
            billing_data, account_name, date_suffix,
            account_id=account_id, start_date=start_date, end_date=end_date,
        )

        print("\nBilling data export completed successfully.")
        print(f"File saved to: {output_file}")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
