# StratusScan IAM Policies

This directory contains IAM policies for StratusScan across AWS Commercial and GovCloud partitions.

## ⭐ Recommended: Use AWS Managed Policies

**The simplest and most maintainable approach is to use AWS Managed Policies instead of custom policies.**

See **[AWS-MANAGED-POLICIES.md](AWS-MANAGED-POLICIES.md)** for detailed instructions.

**Quick Setup** - Attach these 4 AWS Managed Policies:
- ✓ `ReadOnlyAccess`
- ✓ `IAMReadOnlyAccess`
- ✓ `SecurityAudit`
- ✓ `AWSSupportAccess`

**Benefits**: AWS-maintained, automatically updated, no size limits, works in both Commercial and GovCloud.

---

## Alternative: Custom Inline Policies

If you need custom policies (e.g., for additional restrictions), use the JSON files below.

## Policy Files

### AWS Commercial (Partition: `aws`)

- **`commercial-required-permissions.json`** - Core permissions required for StratusScan functionality
  - 15 statement blocks
  - 230 total actions
  - Services: Compute, Storage, Database, Network, IAM, Security, Monitoring, Application, DevTools, Data Analytics, Management, Messaging, Licensing, Organizations, Access Management
  
- **`commercial-optional-permissions.json`** - Optional permissions for advanced features
  - 4 statement blocks
  - 38 total actions
  - Services: ML/AI (SageMaker, Bedrock, Comprehend, Rekognition), Global Edge (CloudFront, Global Accelerator), Mobile/Marketing (Pinpoint), Marketplace, Cost Explorer utilization (Savings Plans / RI utilization and coverage -- paid, opt-in)

### AWS GovCloud (Partition: `aws-us-gov`)

- **`govcloud-required-permissions.json`** - Core permissions required for StratusScan functionality in GovCloud
  - 15 statement blocks
  - 230 total actions
  - **Differences from Commercial Required:**
    - REMOVED: `globalaccelerator:*` permissions (service not available in GovCloud)
    - REMOVED: `cloudfront:*` permissions (operates outside GovCloud boundary, ITAR concerns)
  - Compliance: FedRAMP High, DoD SRG IL-2/4/5, FIPS 140-3, ITAR compliant

- **`govcloud-optional-permissions.json`** - Optional permissions for advanced features in GovCloud
  - 3 statement blocks
  - 34 total actions
  - **Differences from Commercial Optional:**
    - REMOVED: `globalaccelerator:*` permissions (not available)
    - REMOVED: `cloudfront:*` permissions (outside GovCloud boundary)
    - INCLUDES: Bedrock permissions (available as of November 2024)
  - Services: ML/AI (SageMaker, Bedrock, Comprehend, Rekognition), Mobile/Marketing (Pinpoint), Marketplace (limited)

### Legacy Unified Policies (Deprecated)

- **`stratusscan-required-permissions.json`** - Original combined policy (Commercial-focused)
- **`stratusscan-optional-permissions.json`** - Original optional policy (Commercial-focused)

**Note**: The legacy policies combine both required and optional permissions without partition-specific adjustments. Use the partition-specific policies for production deployments.

## IAM vs IAM Identity Center Compatibility

All policies in this directory are compatible with both:
- **IAM**: Attach to IAM users or roles
- **IAM Identity Center**: Use in permission sets

No separate versions are needed - the permission formats are identical.

## Usage Recommendations

### For AWS Commercial

**Minimum (Core Functionality)**:
```bash
# Attach only required permissions
aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws:iam::123456789012:policy/StratusScanCommercialRequired
```

**Full Functionality**:
```bash
# Attach both required and optional permissions
aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws:iam::123456789012:policy/StratusScanCommercialRequired

aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws:iam::123456789012:policy/StratusScanCommercialOptional
```

### For AWS GovCloud

**Minimum (Core Functionality)**:
```bash
# Attach only required permissions (GovCloud-specific)
aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws-us-gov:iam::123456789012:policy/StratusScanGovCloudRequired
```

**Full Functionality**:
```bash
# Attach both required and optional permissions (GovCloud-specific)
aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws-us-gov:iam::123456789012:policy/StratusScanGovCloudRequired

aws iam attach-role-policy \
  --role-name StratusScanRole \
  --policy-arn arn:aws-us-gov:iam::123456789012:policy/StratusScanGovCloudOptional
```

## Key Differences: Commercial vs GovCloud

### Services NOT Available in GovCloud

1. **AWS Global Accelerator** - Not available in GovCloud, not expanding to GovCloud
   - Alternative: Use Route 53 with health checks and latency-based routing

2. **Amazon CloudFront** - Operates outside GovCloud boundary
   - Status: Being Planned for GovCloud regions
   - Current: Can be used with GovCloud origins but operates in commercial regions
   - ITAR Concern: CloudFront processes data outside GovCloud boundary
   - Recommendation: Exclude for ITAR-controlled data

### GovCloud Service Limitations

**Amazon S3**:
- No S3 Transfer Acceleration
- No S3 Storage Lens
- No S3 Express One Zone
- No S3 Tables, S3 Metadata
- No MFA Delete
- Limited Multi-Region Access Points functionality

**Amazon CloudWatch**:
- No Transaction Search
- No GetMetricWidgetImage API
- No dashboard sharing
- No Trusted Advisor metrics alarms
- No cross-account observability

**AWS Marketplace**:
- Limited catalog (fewer products than commercial)
- No container products
- No ML products
- Cannot launch from AWS Marketplace website with GovCloud account
- No Service Catalog integration

**AWS Trusted Advisor**:
- Limited checks compared to commercial regions
- Cannot change severity of existing support cases

### Services Available in GovCloud

**Amazon Bedrock** (as of November 2024):
- Available in us-gov-west-1 and us-gov-east-1
- FedRAMP High authorized
- DoD IL-4/5 authorized
- Models: Claude 3.5 Sonnet, Claude 3 (Sonnet/Haiku/Opus), Llama 3/3.1/3.2, Amazon Titan

All other services in the required permissions are fully available in GovCloud.

## Compliance Notes

### AWS GovCloud Compliance

- **FedRAMP High**: All services support FedRAMP High baseline
- **DoD SRG**: Many services have DoD Cloud Computing SRG Impact Level 2, 4, and 5 authorization
- **FIPS 140-3**: All service endpoints use FIPS 140-3 approved cryptographic modules
- **ITAR**: All services operate within GovCloud boundary (except CloudFront/Global Accelerator which are excluded)

### Best Practices

1. **Least Privilege**: Start with required permissions only, add optional as needed
2. **Partition Awareness**: Always use partition-specific policies (commercial vs govcloud)
3. **Regular Audits**: Review permissions quarterly for unused services
4. **Compliance**: For ITAR workloads in GovCloud, do NOT add CloudFront permissions
5. **Testing**: Validate permissions in non-production environments first

## Policy Validation

All policies have been validated for:
- Valid JSON syntax
- Correct IAM action names
- Service prefix accuracy
- Compatibility with IAM and IAM Identity Center

## Support

For issues or questions:
1. Review GovCloud service availability: https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/using-services.html
2. Check GovCloud service analysis: `.collab/reference/govcloud-service-analysis.json`
3. Open an issue in the StratusScan repository

## Version History

- **2025-11-19**: Created partition-specific policies (commercial vs govcloud)
  - Split required vs optional permissions
  - Added GovCloud compliance metadata
  - Documented service availability differences
  - Added Bedrock support for GovCloud (Nov 2024 release)

