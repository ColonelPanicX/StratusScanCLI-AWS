#!/usr/bin/env python3
"""
Compute Resources All-in-One Export Script

Thin wrapper over _resource_bundle.run_bundle().  Defines the compute exporter
registry and delegates all orchestration (region/script selection, subprocess
execution, zip archiving, summary) to the shared engine.

Covered services (multi-select at runtime):
  EC2, EKS, ECS, Auto Scaling Groups, Lambda Functions, ECR, AMI,
  EC2 Image Builder, EC2 Capacity Reservations, EC2 Dedicated Hosts, WorkSpaces
"""

import sys
from pathlib import Path

try:
    from _resource_bundle import run_bundle
except ImportError:
    sys.path.append(str(Path(__file__).parent))
    from _resource_bundle import run_bundle

CATEGORY = "Compute Resources"
SLUG = "compute-resources"
SCRIPTS = [
    ("EC2",                       "ec2_export.py"),
    ("EKS",                       "eks_export.py"),
    ("ECS",                       "ecs_export.py"),
    ("Auto Scaling Groups",       "autoscaling_export.py"),
    ("Lambda Functions",          "lambda_export.py"),
    ("ECR",                       "ecr_export.py"),
    ("AMI",                       "ami_export.py"),
    ("EC2 Image Builder",         "image_builder_export.py"),
    ("EC2 Capacity Reservations", "ec2_capacity_reservations_export.py"),
    ("EC2 Dedicated Hosts",       "ec2_dedicated_hosts_export.py"),
    ("WorkSpaces",                "workspaces_export.py"),
]


def main() -> None:
    run_bundle(
        CATEGORY,
        SLUG,
        SCRIPTS,
        description="Export all compute resources (EC2, EKS, ECS, Lambda, etc.) to Excel",
    )


if __name__ == "__main__":
    main()
