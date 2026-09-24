#!/usr/bin/env python3
"""
AWS Marketplace Subscriptions Export Script for StratusScan

Exports comprehensive AWS Marketplace subscription information including:
- Active and historical agreements (private offers, public subscriptions)
- Agreement terms (pricing, legal, support, renewal details)
- Cost and payment tracking

Output: Multi-worksheet Excel file with Marketplace resources
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
args = utils.parse_script_args("Export AWS Marketplace subscriptions to Excel")

def _build_agreement_row(mp_client, agreement_summary: dict[str, Any]) -> dict[str, Any]:
    """
    Build the export row for a single Marketplace agreement.

    Extracted so per-agreement processing can be wrapped in try/except by
    the caller: a malformed/inaccessible agreement must not sink the whole
    account-scope collection. Required fields are read with ``.get()`` and a
    safe default for the same reason.

    Args:
        mp_client: The boto3 marketplace-agreement client.
        agreement_summary (dict): A single agreementViewSummaries entry from
            search_agreements.

    Returns:
        dict: The assembled agreement row.
    """
    agreement_id = agreement_summary.get('agreementId', 'N/A')

    # Get detailed agreement information
    agreement_details = mp_client.describe_agreement(agreementId=agreement_id)

    proposer = agreement_details.get('proposer', {})
    acceptor = agreement_details.get('acceptor', {})

    agreement_type = agreement_details.get('agreementType', 'N/A')
    status = agreement_details.get('status', 'N/A')

    acceptance_time = agreement_details.get('acceptanceTime', 'N/A')
    if acceptance_time != 'N/A':
        acceptance_time = acceptance_time.strftime('%Y-%m-%d %H:%M:%S')

    start_time = agreement_details.get('startTime', 'N/A')
    if start_time != 'N/A':
        start_time = start_time.strftime('%Y-%m-%d %H:%M:%S')

    end_time = agreement_details.get('endTime', 'N/A')
    if end_time != 'N/A':
        end_time = end_time.strftime('%Y-%m-%d %H:%M:%S')

    estimated_charges = agreement_details.get('estimatedCharges', {})
    agreement_amount = estimated_charges.get('agreementValue', 'N/A')
    currency_code = estimated_charges.get('currencyCode', 'N/A')

    return {
        'Agreement ID': agreement_id,
        'Agreement Type': agreement_type,
        'Status': status,
        'Proposer Account ID': proposer.get('accountId', 'N/A'),
        'Acceptor Account ID': acceptor.get('accountId', 'N/A'),
        'Acceptance Time': acceptance_time,
        'Start Time': start_time,
        'End Time': end_time,
        'Agreement Amount': agreement_amount,
        'Currency': currency_code
    }


def collect_agreements() -> list[dict[str, Any]]:
    """
    Collect AWS Marketplace agreement information (global service).

    Not wrapped in ``aws_error_handler`` and does not swallow errors to an
    empty list: a swallowed error here would be indistinguishable from a
    genuinely empty account (no Marketplace agreements), producing silent
    data loss (see the 07.15.2026 / 07.16.2026 silent-collection-failure
    audits). AWS Marketplace is a global, account-scope service (not
    multi-region — see scripts/shield_export.py for the sibling global/
    account-scope PARTIAL-tier fix this follows). Account-scope failures
    (client creation, pagination/search_agreements) are allowed to raise so
    the caller (main) can record this scope as *failed* rather than
    *empty*. Per-agreement errors are contained internally (logged and
    skipped).

    Returns:
        list: List of agreement information dictionaries.

    Raises:
        Exception: Any AWS/pagination error for the account scope (caller
            records it as a failed scope; it is never masked as empty).
    """
    print("\n=== COLLECTING MARKETPLACE AGREEMENTS ===")
    all_agreements = []

    # Marketplace Agreement API is a global service - use partition-aware home region
    home_region = utils.get_partition_default_region()
    mp_client = utils.get_boto3_client('marketplace-agreement', region_name=home_region)

    # Search for all agreements (active and expired), without filters, to
    # get all agreements. A real error here (client creation, pagination)
    # is allowed to propagate — it is not caught in this function.
    next_token = None
    while True:
        params = {}
        params['maxResults'] = 100
        if next_token:
            params['nextToken'] = next_token
        page = mp_client.search_agreements(**params)
        agreements = page.get('agreementViewSummaries', [])

        for agreement_summary in agreements:
            agreement_id = agreement_summary.get('agreementId', 'N/A')

            try:
                all_agreements.append(_build_agreement_row(mp_client, agreement_summary))
            except Exception as e:
                utils.log_warning(f"Could not get details for agreement {agreement_id}: {str(e)}")
                continue

        next_token = page.get('nextToken')
        if not next_token:
            break

    utils.log_success(f"Total agreements collected: {len(all_agreements)}")
    return all_agreements


@utils.aws_error_handler("Collecting agreement terms", default_return=[])
def collect_agreement_terms(agreements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect detailed terms for each agreement."""
    print("\n=== COLLECTING AGREEMENT TERMS ===")
    all_terms = []

    home_region = utils.get_partition_default_region()
    mp_client = utils.get_boto3_client('marketplace-agreement', region_name=home_region)

    for agreement in agreements:
        agreement_id = agreement.get('Agreement ID', 'N/A')
        if agreement_id == 'N/A':
            continue

        try:
            # Get agreement terms
            next_token = None
            while True:
                params = {'agreementId': agreement_id}
                params['maxResults'] = 100
                if next_token:
                    params['nextToken'] = next_token
                page = mp_client.get_agreement_terms(**params)
                accepted_terms = page.get('acceptedTerms', [])

                for term in accepted_terms:
                    term_type = term.get('type', 'N/A')

                    # Extract pricing information if available
                    pricing_info = 'N/A'
                    legal_info = 'N/A'
                    support_info = 'N/A'
                    renewal_info = 'N/A'

                    # ConfigurableUpfrontPricingTerm
                    if 'configurableUpfrontPricingTerm' in term:
                        pricing_term = term['configurableUpfrontPricingTerm']
                        pricing_info = f"Upfront: {pricing_term.get('currencyCode', 'USD')} {pricing_term.get('rateCards', [{}])[0].get('price', 'N/A')}"

                    # RecurringPaymentTerm
                    elif 'recurringPaymentTerm' in term:
                        payment_term = term['recurringPaymentTerm']
                        billing_period = payment_term.get('billingPeriod', 'N/A')
                        pricing_info = f"Recurring: {billing_period}"

                    # LegalTerm
                    elif 'legalTerm' in term:
                        legal_term = term['legalTerm']
                        legal_info = legal_term.get('type', 'N/A')

                    # SupportTerm
                    elif 'supportTerm' in term:
                        support_term = term['supportTerm']
                        support_info = support_term.get('type', 'N/A')

                    # RenewalTerm
                    elif 'renewalTerm' in term:
                        renewal_term = term['renewalTerm']
                        renewal_info = f"Type: {renewal_term.get('type', 'N/A')}"

                    all_terms.append({
                        'Agreement ID': agreement_id,
                        'Term Type': term_type,
                        'Pricing Details': pricing_info,
                        'Legal Details': legal_info,
                        'Support Details': support_info,
                        'Renewal Details': renewal_info
                    })

                next_token = page.get('nextToken')
                if not next_token:
                    break

        except Exception as e:
            utils.log_warning(f"Could not get terms for agreement {agreement_id}: {str(e)}")
            continue

    utils.log_success(f"Total agreement terms collected: {len(all_terms)}")
    return all_terms


def generate_summary(agreements: list[dict[str, Any]],
                     terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate summary statistics for Marketplace resources."""
    utils.log_info("Generating summary statistics...")

    summary = []

    # Agreements summary
    total_agreements = len(agreements)
    active_agreements = sum(1 for a in agreements if a.get('Status', '') == 'ACTIVE')
    expired_agreements = sum(1 for a in agreements if a.get('Status', '') == 'EXPIRED')

    summary.append({
        'Metric': 'Total Agreements',
        'Count': total_agreements,
        'Details': f'Active: {active_agreements}, Expired: {expired_agreements}'
    })

    # Calculate total spend (active agreements only)
    if agreements:
        total_spend = 0
        currency = 'USD'
        for agreement in agreements:
            if agreement.get('Status', '') == 'ACTIVE':
                amount = agreement.get('Agreement Amount', 'N/A')
                curr = agreement.get('Currency', 'USD')
                if amount != 'N/A' and isinstance(amount, (int, float, str)):
                    try:
                        total_spend += float(amount)
                        currency = curr
                    except (ValueError, TypeError):
                        pass

        if total_spend > 0:
            summary.append({
                'Metric': 'Active Agreement Value',
                'Count': f'{currency} {total_spend:,.2f}',
                'Details': 'Total estimated charges for active agreements'
            })

    # Agreement types
    if agreements:
        df = pd.DataFrame(agreements)
        agreement_types = df['Agreement Type'].value_counts().to_dict()
        for atype, count in agreement_types.items():
            summary.append({
                'Metric': f'{atype} Agreements',
                'Count': count,
                'Details': 'Agreement type distribution'
            })

    # Terms summary
    summary.append({
        'Metric': 'Total Agreement Terms',
        'Count': len(terms),
        'Details': 'Pricing, legal, support, and renewal terms'
    })

    return summary


def main():
    """
    Main execution function.

    AWS Marketplace is a global, account-scope service (not multi-region),
    so failures are tracked per account-scope collector rather than via
    ``utils.scan_regions_concurrent`` (see scripts/shield_export.py for the
    sibling global/account-scope PARTIAL-tier reference). This exporter
    already builds an always-non-empty ``Summary`` sheet (see
    ``generate_summary``), so a workbook always lands on disk regardless of
    whether the ``agreements`` scope succeeded — that "file always lands"
    behavior is preserved. What was missing is a way to tell "agreements
    scope failed" apart from "genuinely no agreements": a failure on that
    scope is now tracked in ``failed_scopes``, surfaced via a
    ``utils.report_collection_failures`` marker, and causes a non-zero
    exit -- it must never be silently collapsed into a zero-row Agreements
    sheet inside an otherwise complete-looking workbook (07.15.2026 /
    07.16.2026 audits).
    """
    if not utils.ensure_dependencies('pandas', 'openpyxl'):
        return
    global pd
    import pandas as pd
    script_name = Path(__file__).stem
    utils.setup_logging(script_name)
    utils.log_script_start(script_name)

    partition = utils.detect_partition()
    if not utils.is_service_available_in_partition("marketplace-agreement", partition):
        utils.log_warning("AWS Marketplace is not available in AWS GovCloud. Skipping.")
        sys.exit(0)

    account_id, account_name = utils.print_script_banner("AWS MARKETPLACE SUBSCRIPTIONS EXPORT")
    if not account_id:
        utils.log_error("Unable to determine AWS account ID. Please check your credentials.")
        return

    utils.log_info(f"AWS Account: {account_name} ({utils.mask_account_id(account_id)})")

    # Note: Marketplace APIs are global services
    print("\nNote: AWS Marketplace is a global service (not region-specific)")
    print("Data will be collected from all your Marketplace agreements and subscriptions.")

    # Collect data
    print("\nCollecting AWS Marketplace data...")

    # Account-scope failure tracking (see scripts/shield_export.py).
    failed_scopes = []

    # STEP 1: Collect agreements (PRIMARY scope — a real API error here
    # must propagate to failed_scopes, never collapse into an empty list
    # that reads as "no agreements").
    try:
        agreements = collect_agreements()
    except Exception as e:
        failed_scopes.append(('agreements', str(e)))
        utils.log_error(f"Marketplace agreements collection failed: {e}")
        agreements = []

    # STEP 2: Collect agreement terms (enrichment — degrades gracefully via
    # its own aws_error_handler decorator; a failure here does not fail the
    # whole export).
    terms = collect_agreement_terms(agreements)
    summary = generate_summary(agreements, terms)

    # Create DataFrames
    utils.log_info("Creating DataFrames...")

    dataframes = {}

    if summary:
        df_summary = pd.DataFrame(summary)
        df_summary = utils.prepare_dataframe_for_export(df_summary)
        dataframes['Summary'] = df_summary

    if agreements:
        df_agreements = pd.DataFrame(agreements)
        df_agreements = utils.prepare_dataframe_for_export(df_agreements)
        dataframes['Agreements'] = df_agreements

    if terms:
        df_terms = pd.DataFrame(terms)
        df_terms = utils.prepare_dataframe_for_export(df_terms)
        dataframes['Agreement Terms'] = df_terms

    # Export to Excel. The Summary sheet (see generate_summary) is always
    # populated, so dataframes is never empty and a workbook always lands
    # here -- this "file always lands" behavior is preserved even when the
    # agreements scope failed.
    filename = utils.create_export_filename(account_name, 'marketplace', 'global')

    utils.log_info(f"Exporting to {filename}...")
    utils.save_multiple_dataframes_to_excel(dataframes, filename)

    if not agreements and not terms and not failed_scopes:
        # Genuinely empty: the agreements scope succeeded and there is
        # simply nothing configured -- not a failure.
        utils.log_warning("No Marketplace data found to export")

    # If the agreements scope failed, make it loud: write a marker and exit
    # non-zero, even though the always-written Summary sheet means a
    # workbook still lands. A partial export that looks complete is exactly
    # the failure mode this guards against.
    if failed_scopes:
        utils.report_collection_failures(account_name, 'marketplace', failed_scopes)
        print(
            "\nERROR: Marketplace export completed with failures — data is "
            "incomplete. See the *-marketplace-FAILED-*.txt marker in the "
            "output directory."
        )
        sys.exit(1)

    utils.log_success("Marketplace export completed successfully")


if __name__ == "__main__":
    main()
