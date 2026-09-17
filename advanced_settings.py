#!/usr/bin/env python3
"""
Title: StratusScan Advanced Settings Configuration
Version: v0.1.0
Date: NOV-15-2025

Description:
Configure advanced performance and behavior settings for StratusScan.
These settings are stored in config.json under 'advanced_settings'.

Features:
- Concurrent scanning configuration (max workers, fallback behavior)
- Progress display verbosity levels (quiet, standard, verbose)
- Session-level caching settings
- Performance tuning options

Usage:
    python advanced_settings.py
"""

import json
import sys
from pathlib import Path

# Import utils
try:
    import utils
except ImportError:
    script_dir = Path(__file__).parent.absolute()
    sys.path.append(str(script_dir))
    import utils


def get_default_settings():
    """Get default advanced settings."""
    return {
        'concurrent_scanning': {
            'enabled': True,
            'max_workers': 4,
            'fallback_on_error': True,
        },
        'progress_display': {
            'level': 'standard',  # 'quiet', 'standard', 'verbose'
            'show_region_progress': True,
            'show_pagination_progress': False,  # Only in verbose mode
        },
        'caching': {
            'enabled': True,
            'expire_after_minutes': 0,  # 0 = no expiration (session-only)
        },
        # Pricing rates come from AWS's public Price List Bulk feed, which
        # needs no credentials and no IAM grant. Any failure here falls back to
        # the bundled reference/ snapshot -- an export is never blocked on it.
        'pricing': {
            # Attempt the feed at all. Turn this off for an air-gapped or
            # egress-restricted environment to skip the network entirely
            # rather than waiting for it to time out.
            'live_feed_enabled': True,
            # How long a cached artifact for a still-current feed version is
            # reused. The cache is keyed by feed version, so a newly published
            # feed is never served from a stale entry regardless of this value.
            'cache_ttl_hours': 168,
            # The feed host serves no compression, so this is literal wire
            # bytes. 384 MiB clears the 302,856,576-byte us-east-1 EC2 CSV plus
            # the 205,737,309-byte us-gov-west-1 one when summed per offer.
            'max_feed_bytes': 402653184,
            'timeout_seconds': 30,
            # Hard wall-clock budget. Past this the fetch is abandoned and the
            # bundled snapshot is used.
            'max_seconds': 180,
        },
        'performance': {
            'batch_dataframe_size': 1000,
            'api_retry_attempts': 3,
            'api_retry_delay_seconds': 2,
            # Auto Scaling activity history is unbounded; cap what we pull per
            # group so a busy account cannot turn one export into thousands of
            # API calls (Issue #261).
            'scaling_activity_limit': 100,
        }
    }


def get_current_settings():
    """Get current advanced settings from config.json."""
    _, config = utils.get_config()
    current = config.get('advanced_settings', {})

    # Merge with defaults to ensure all keys exist
    defaults = get_default_settings()
    for key, value in defaults.items():
        if key not in current:
            current[key] = value
        elif isinstance(value, dict):
            for subkey, subvalue in value.items():
                if subkey not in current[key]:
                    current[key][subkey] = subvalue

    return current


def configure_concurrent_scanning():
    """Configure concurrent scanning settings."""
    print("\n" + "="*70)
    print("CONCURRENT SCANNING SETTINGS")
    print("="*70)
    print("\nConcurrent scanning allows StratusScan to scan multiple AWS regions")
    print("simultaneously, dramatically improving performance (4x-10x faster).")
    print("\nRecommendations:")
    print("  - Small AWS accounts (< 100 resources): 2-4 workers")
    print("  - Medium AWS accounts (100-1000 resources): 4-6 workers")
    print("  - Large AWS accounts (> 1000 resources): 6-10 workers")
    print("\nNote: Higher worker counts may trigger API rate limits.")
    print("      Automatic fallback to sequential scanning is enabled by default.")

    current = get_current_settings()

    # Enable/disable concurrent scanning
    enabled_str = 'Yes' if current['concurrent_scanning']['enabled'] else 'No'
    enabled = utils.prompt_for_confirmation(
        f"\nEnable concurrent scanning? [Current: {enabled_str}]",
        default=current['concurrent_scanning']['enabled'],
    )

    if enabled:
        # Configure max workers
        while True:
            max_workers_input = input(f"Max concurrent workers (2-10) [Current: {current['concurrent_scanning']['max_workers']}]: ").strip()
            if not max_workers_input:
                max_workers = current['concurrent_scanning']['max_workers']
                break
            try:
                max_workers = int(max_workers_input)
                if 2 <= max_workers <= 10:
                    break
                print("  ERROR: Please enter a number between 2 and 10.")
            except ValueError:
                print("  ERROR: Please enter a valid number.")

        # Fallback on error
        fallback_str = 'Yes' if current['concurrent_scanning']['fallback_on_error'] else 'No'
        fallback = utils.prompt_for_confirmation(
            f"\nFallback to sequential scanning on errors? [Current: {fallback_str}]",
            default=current['concurrent_scanning']['fallback_on_error'],
        )
    else:
        max_workers = current['concurrent_scanning']['max_workers']
        fallback = current['concurrent_scanning']['fallback_on_error']

    return {
        'enabled': enabled,
        'max_workers': max_workers,
        'fallback_on_error': fallback,
    }


def configure_progress_display():
    """Configure progress display verbosity."""
    print("\n" + "="*70)
    print("PROGRESS DISPLAY SETTINGS")
    print("="*70)
    print("\nProgress display levels:")
    print("\n  [1] Quiet - Minimal output, only errors and final results")
    print("      Example: 'Export complete: PROD-ACCOUNT-ec2-all-export-11.15.2025.xlsx'")
    print("\n  [2] Standard - Region completion progress (RECOMMENDED)")
    print("      Example: '[75.0%] Completed region 3/4: us-west-1 (12 instances found)'")
    print("\n  [3] Verbose - Detailed progress including pagination and API calls")
    print("      Example: '[33.3%] Processing page 1/3', 'DEBUG: Cached account info...'")

    current = get_current_settings()
    current_level = current['progress_display']['level']

    level_map = {'quiet': 1, 'standard': 2, 'verbose': 3}
    reverse_map = {1: 'quiet', 2: 'standard', 3: 'verbose'}
    current_num = level_map.get(current_level, 2)

    while True:
        choice = input(f"\nSelect progress level (1-3) [Current: {current_num} - {current_level.capitalize()}]: ").strip()
        if not choice:
            choice = current_num
            break
        try:
            choice = int(choice)
            if 1 <= choice <= 3:
                break
            print("  ERROR: Please enter 1, 2, or 3.")
        except ValueError:
            print("  ERROR: Please enter a valid number.")

    level = reverse_map[choice]

    # Set dependent flags based on level
    show_region = level in ['standard', 'verbose']
    show_pagination = level == 'verbose'

    print(f"\n  Selected: {level.capitalize()}")
    print(f"  - Region progress: {'Enabled' if show_region else 'Disabled'}")
    print(f"  - Pagination progress: {'Enabled' if show_pagination else 'Disabled'}")

    return {
        'level': level,
        'show_region_progress': show_region,
        'show_pagination_progress': show_pagination,
    }


def configure_caching():
    """Configure caching settings."""
    print("\n" + "="*70)
    print("CACHING SETTINGS")
    print("="*70)
    print("\nCaching stores frequently accessed data (account info, region lists,")
    print("pricing data) to avoid repeated API calls.")
    print("\nCache expiration options:")
    print("  - Session-only (0 minutes): Cache cleared when script exits (RECOMMENDED)")
    print("    • Simple and predictable")
    print("    • No stale data concerns")
    print("    • Perfect for single script runs and menu mode")
    print("\n  - Timed expiration (5-60 minutes): Cache expires after X minutes")
    print("    • Useful for heavy testing/troubleshooting")
    print("    • Risk of stale data if credentials change")
    print("    • Requires cache persistence to disk")

    current = get_current_settings()

    # Enable/disable caching
    enabled_str = 'Yes' if current['caching']['enabled'] else 'No'
    enabled = utils.prompt_for_confirmation(
        f"\nEnable caching? [Current: {enabled_str}]",
        default=current['caching']['enabled'],
    )

    if enabled:
        # Cache expiration
        current_expire = current['caching']['expire_after_minutes']
        expire_str = 'Session-only' if current_expire == 0 else f'{current_expire} minutes'

        while True:
            expire_input = input(f"Cache expiration in minutes (0 for session-only, 5-60 for timed) [Current: {current_expire} - {expire_str}]: ").strip()
            if not expire_input:
                expire = current_expire
                break
            try:
                expire = int(expire_input)
                if expire == 0 or 5 <= expire <= 60:
                    break
                print("  ERROR: Please enter 0 (session-only) or 5-60 minutes.")
            except ValueError:
                print("  ERROR: Please enter a valid number.")

        print(f"\n  Selected: {'Session-only (recommended)' if expire == 0 else f'{expire} minutes'}")
    else:
        expire = 0

    return {
        'enabled': enabled,
        'expire_after_minutes': expire,
    }


def configure_performance():
    """Configure performance tuning options."""
    print("\n" + "="*70)
    print("PERFORMANCE TUNING")
    print("="*70)
    print("\nAdvanced performance settings for AWS API interactions and data processing.")
    print("\nDefault settings are optimized for most use cases.")
    print("Only modify these if you're experiencing specific issues.")

    current = get_current_settings()['performance']

    # Batch DataFrame size
    print("\n1. Batch DataFrame Size")
    print("   Controls memory usage when processing large datasets (10,000+ resources).")
    print("   Higher values = faster but more memory usage")
    print("   Lower values = slower but less memory usage")

    while True:
        batch_input = input(f"   Batch size (500-5000) [Current: {current['batch_dataframe_size']}]: ").strip()
        if not batch_input:
            batch_size = current['batch_dataframe_size']
            break
        try:
            batch_size = int(batch_input)
            if 500 <= batch_size <= 5000:
                break
            print("   ERROR: Please enter a number between 500 and 5000.")
        except ValueError:
            print("   ERROR: Please enter a valid number.")

    # API retry attempts
    print("\n2. API Retry Attempts")
    print("   Number of times to retry failed AWS API calls.")
    print("   Higher values = more resilient but slower on persistent failures")

    while True:
        retry_input = input(f"   Retry attempts (1-10) [Current: {current['api_retry_attempts']}]: ").strip()
        if not retry_input:
            retry_attempts = current['api_retry_attempts']
            break
        try:
            retry_attempts = int(retry_input)
            if 1 <= retry_attempts <= 10:
                break
            print("   ERROR: Please enter a number between 1 and 10.")
        except ValueError:
            print("   ERROR: Please enter a valid number.")

    # API retry delay
    print("\n3. API Retry Delay (seconds)")
    print("   Initial delay before retrying failed API calls (exponential backoff).")
    print("   Higher values = less likely to hit rate limits but slower retries")

    while True:
        delay_input = input(f"   Retry delay (1-10 seconds) [Current: {current['api_retry_delay_seconds']}]: ").strip()
        if not delay_input:
            retry_delay = current['api_retry_delay_seconds']
            break
        try:
            retry_delay = int(delay_input)
            if 1 <= retry_delay <= 10:
                break
            print("   ERROR: Please enter a number between 1 and 10.")
        except ValueError:
            print("   ERROR: Please enter a valid number.")

    # Scaling activity history limit
    print("\n4. Scaling Activity History Limit")
    print("   Maximum scaling activities to export per Auto Scaling Group.")
    print("   AWS returns activity history newest-first and unbounded; this caps it.")
    print("   Higher values = deeper history but more API calls on busy groups")

    while True:
        activity_input = input(
            f"   Activities per group (10-1000) "
            f"[Current: {current['scaling_activity_limit']}]: "
        ).strip()
        if not activity_input:
            activity_limit = current['scaling_activity_limit']
            break
        try:
            activity_limit = int(activity_input)
            if 10 <= activity_limit <= 1000:
                break
            print("   ERROR: Please enter a number between 10 and 1000.")
        except ValueError:
            print("   ERROR: Please enter a valid number.")

    return {
        'batch_dataframe_size': batch_size,
        'api_retry_attempts': retry_attempts,
        'api_retry_delay_seconds': retry_delay,
        'scaling_activity_limit': activity_limit,
    }


def save_settings(settings):
    """Save advanced settings to config.json."""
    _, config = utils.get_config()
    config['advanced_settings'] = settings

    config_file = Path(__file__).parent / 'config.json'
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

    print("\n" + "="*70)
    print("SETTINGS SAVED SUCCESSFULLY")
    print("="*70)
    print(f"Settings saved to: {config_file}")
    print("\nThese settings will be used by all StratusScan export scripts.")


def display_current_settings():
    """Display current advanced settings."""
    settings = get_current_settings()

    print("\n" + "="*70)
    print("CURRENT ADVANCED SETTINGS")
    print("="*70)

    print("\nConcurrent Scanning:")
    print(f"  Enabled: {settings['concurrent_scanning']['enabled']}")
    print(f"  Max Workers: {settings['concurrent_scanning']['max_workers']}")
    print(f"  Fallback on Error: {settings['concurrent_scanning']['fallback_on_error']}")

    print("\nProgress Display:")
    print(f"  Level: {settings['progress_display']['level'].capitalize()}")
    print(f"  Show Region Progress: {settings['progress_display']['show_region_progress']}")
    print(f"  Show Pagination Progress: {settings['progress_display']['show_pagination_progress']}")

    print("\nCaching:")
    print(f"  Enabled: {settings['caching']['enabled']}")
    expire = settings['caching']['expire_after_minutes']
    print(f"  Expiration: {'Session-only' if expire == 0 else f'{expire} minutes'}")

    pricing = settings['pricing']
    print("\nPricing Feed:")
    print(f"  Live Feed Enabled: {pricing['live_feed_enabled']}")
    ttl = pricing['cache_ttl_hours']
    print(f"  Cache TTL: {'No expiry' if ttl == 0 else f'{ttl} hours'}")
    print(f"  Max Feed Size: {pricing['max_feed_bytes']:,} bytes")
    print(f"  Timeout: {pricing['timeout_seconds']}s per socket, "
          f"{pricing['max_seconds']}s total")

    print("\nPerformance:")
    print(f"  Batch DataFrame Size: {settings['performance']['batch_dataframe_size']}")
    print(f"  API Retry Attempts: {settings['performance']['api_retry_attempts']}")
    print(f"  API Retry Delay: {settings['performance']['api_retry_delay_seconds']}s")
    print(f"  Scaling Activity Limit: {settings['performance']['scaling_activity_limit']} per group")

    print("="*70)


def reset_to_defaults():
    """Reset all settings to defaults."""
    print("\n" + "="*70)
    print("RESET TO DEFAULTS")
    print("="*70)
    print("\nThis will reset ALL advanced settings to their default values:")
    print("  - Concurrent Scanning: Enabled (4 workers, fallback on)")
    print("  - Progress Display: Standard level")
    print("  - Caching: Enabled (session-only)")
    print("  - Performance: Default tuning values")

    confirm = utils.prompt_for_confirmation("\nAre you sure you want to reset?", default=False)

    if confirm:
        defaults = get_default_settings()
        save_settings(defaults)
        print(f"\n{utils.GLYPH_OK} All settings have been reset to defaults.")
    else:
        print(f"\n{utils.GLYPH_FAIL} Reset cancelled.")


def main():
    """Main configuration menu."""
    print("="*70)
    print("STRATUSSCAN ADVANCED SETTINGS")
    print("="*70)
    print("\nConfigure performance and behavior settings for StratusScan exports.")
    print("Settings are stored in config.json and apply to all export scripts.")

    while True:
        try:
            choice = utils.prompt_menu(
                "MAIN MENU",
                [
                    "View Current Settings",
                    "Configure Concurrent Scanning",
                    "Configure Progress Display",
                    "Configure Caching",
                    "Configure Performance Tuning",
                    "Reset to Defaults",
                    "Exit",
                ],
            )
        except (utils.BackSignal, utils.ExitToMainSignal, utils.QuitSignal):
            print("\nExiting advanced settings. Changes have been saved.")
            break

        if choice == 1:
            display_current_settings()
        elif choice == 2:
            current = get_current_settings()
            current['concurrent_scanning'] = configure_concurrent_scanning()
            save_settings(current)
        elif choice == 3:
            current = get_current_settings()
            current['progress_display'] = configure_progress_display()
            save_settings(current)
        elif choice == 4:
            current = get_current_settings()
            current['caching'] = configure_caching()
            save_settings(current)
        elif choice == 5:
            current = get_current_settings()
            current['performance'] = configure_performance()
            save_settings(current)
        elif choice == 6:
            reset_to_defaults()
        elif choice == 7:
            print("\nExiting advanced settings. Changes have been saved.")
            break


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nConfiguration cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nERROR: {e}")
        sys.exit(1)
