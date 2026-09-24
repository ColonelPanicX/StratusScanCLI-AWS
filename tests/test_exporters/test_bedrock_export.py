#!/usr/bin/env python3
"""
Moto-based tests for bedrock_export.py.

Focus: the silent-collection-failure contract (Tier-2). See
.collab/audit/07.16.2026-silent-collection-failure-blast-radius.md

Moto's Bedrock support is limited: ``list_foundation_models`` raises
``NotImplementedError`` and ``list_guardrails`` is absent from botocore below
1.34.90 (Issue #213), so guardrail tests use stub clients. Because of this,
cases (b)/(c) below (region-API-failure and failed-regions-surfaced) are
exercised via monkeypatch against ``_scan_foundation_models_region`` rather
than by driving a real moto failure through ``list_foundation_models``.
"""

import sys
from pathlib import Path

import botocore
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import bedrock_export  # noqa: E402
from bedrock_export import _scan_foundation_models_region, _scan_guardrails_region  # noqa: E402

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _fake_model_summary(model_id="anthropic.claude-v2", model_name="Claude V2"):
    return {
        "modelId": model_id,
        "modelArn": f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}",
        "modelName": model_name,
        "providerName": "Anthropic",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "responseStreamingSupported": True,
        "customizationsSupported": [],
        "inferenceTypesSupported": ["ON_DEMAND"],
        "modelLifecycle": {"status": "ACTIVE"},
    }


class TestSilentCollectionFailureRegression:
    """
    Regression tests for the 07.15 / 07.16.2026 audits: exporters silently
    lost data because a collection error was swallowed to an empty list,
    indistinguishable from a genuinely empty region.
    """

    @mock_aws
    def test_a_malformed_item_is_skipped_not_fatal(self, monkeypatch):
        """
        One foundation model that fails to process must not discard the
        whole region's results — the healthy model is still collected.

        moto does not implement list_foundation_models, so the client call
        is monkeypatched to yield two fake model summaries directly.
        """

        def fake_list_foundation_models(self):
            return {
                "modelSummaries": [
                    _fake_model_summary("good-model", "Good Model"),
                    _fake_model_summary("bad-model", "Bad Model"),
                ]
            }

        monkeypatch.setattr(
            botocore.client.BaseClient, "_make_api_call",
            lambda self, operation_name, kwargs: (
                fake_list_foundation_models(self)
                if operation_name == "ListFoundationModels"
                else (_ for _ in ()).throw(AssertionError(f"unexpected call: {operation_name}"))
            ),
        )

        original = bedrock_export._build_foundation_model_row

        def raise_for_bad(model, region):
            if model.get("modelId") == "bad-model":
                raise KeyError("SomeUnexpectedField")
            return original(model, region)

        monkeypatch.setattr(bedrock_export, "_build_foundation_model_row", raise_for_bad)

        rows = _scan_foundation_models_region(REGION)

        ids = {row["Model ID"] for row in rows}
        assert "good-model" in ids, "healthy model was lost when a sibling failed"
        assert "bad-model" not in ids, "malformed model should have been skipped"

    def test_b_region_api_failure_raises_not_empty(self, monkeypatch):
        """
        A region-level API failure must propagate (so the caller can record
        a FAILED region) rather than being swallowed into an empty list.

        moto has no working mock for list_foundation_models (it raises
        NotImplementedError, not a realistic ClientError), so the boto3
        client factory itself is monkeypatched to simulate a genuine AWS
        error at the region-collector boundary.
        """

        def boom(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "ListFoundationModels",
            )

        monkeypatch.setattr(bedrock_export.utils, "get_boto3_client", boom)

        with pytest.raises(botocore.exceptions.ClientError):
            _scan_foundation_models_region(REGION)

    def test_c_collect_foundation_models_surfaces_failed_regions(self, monkeypatch):
        """
        The scope wrapper must return failed regions via collect_failures,
        not drop them — this is what lets export write a FAILED marker and
        exit 1.

        moto's list_foundation_models is not implemented, so the
        per-region scan function is monkeypatched directly to simulate a
        genuine collection failure.
        """

        def boom(region):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "ListFoundationModels",
            )

        monkeypatch.setattr(bedrock_export, "_scan_foundation_models_region", boom)

        models, failed_regions = bedrock_export.collect_foundation_models([REGION])

        assert models == []
        assert [r for r, _ in failed_regions] == [REGION]

    def test_guardrails_op_availability_probe_skips_when_sdk_lacks_op(self, monkeypatch):
        """
        Below the SDK floor (Issue #213) the client has no ``list_guardrails``
        method: the region is skipped cleanly, NOT recorded as failed.
        Simulated with a stub client so the test does not depend on which
        botocore happens to be installed.
        """

        class _OldBedrockClient:
            pass

        monkeypatch.setattr(
            bedrock_export.utils, "get_boto3_client", lambda *a, **kw: _OldBedrockClient()
        )

        assert _scan_guardrails_region(REGION) == []
        all_guardrails, failed_regions = bedrock_export.collect_guardrails([REGION])
        assert all_guardrails == []
        assert failed_regions == []

    def test_guardrails_collected_when_sdk_has_op(self, monkeypatch):
        """
        Regression for the #213 probe bug: the old check tested snake_case
        ``'list_guardrails'`` against CamelCase ``operation_names`` and skipped
        guardrails on every SDK. With the op present, rows must come back and
        pagination must follow ``nextToken``.
        """
        pages = [
            {"guardrails": [{"id": "g1", "name": "one", "arn": "arn:1"}], "nextToken": "t1"},
            {"guardrails": [{"id": "g2", "name": "two", "arn": "arn:2"}]},
        ]
        calls = []

        class _BedrockClient:
            def list_guardrails(self, **params):
                calls.append(params)
                return pages[len(calls) - 1]

        monkeypatch.setattr(
            bedrock_export.utils, "get_boto3_client", lambda *a, **kw: _BedrockClient()
        )

        rows = _scan_guardrails_region(REGION)
        assert [r["Guardrail ID"] for r in rows] == ["g1", "g2"]
        assert calls == [{"maxResults": 100}, {"maxResults": 100, "nextToken": "t1"}]
