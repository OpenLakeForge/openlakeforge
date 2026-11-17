"""The stage vocabulary shared by the CLI, the activation records and the evaluator."""

from __future__ import annotations

from enum import StrEnum


class StageName(StrEnum):
    DEV = "dev"
    UAT = "uat"
    PROD = "prod"
