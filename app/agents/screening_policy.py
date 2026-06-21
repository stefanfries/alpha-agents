"""Compatibility shim for renamed trend-detection policy module.

Prefer importing from app.policies.trend_detection.
"""

from app.policies.trend_detection import TrendDetectionPolicyConfig, passes_rule_group

# Backward-compatible aliases
ScreeningPolicyConfig = TrendDetectionPolicyConfig
passes_policy_group = passes_rule_group
