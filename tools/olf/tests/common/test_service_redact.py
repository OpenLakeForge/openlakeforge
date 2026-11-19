import pytest
from openlakeforge_services.redact import REDACTED, redact


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        ("connect to postgresql://dagster:pa55word@db:5432/dagster failed", "pa55word", "postgresql://"),
        (
            "GET https://store.example/b/k?X-Amz-Credential=x&X-Amz-Signature=abc123 403",
            "X-Amz-Signature",
            "https://store.example/b/k?",
        ),
        ("login failed: password=hunter2, user=ada", "hunter2", "user=ada"),
        ('{"api_key": "k-123", "region": "eu"}', "k-123", '"region": "eu"'),
        ("aws_secret_access_key: wJalrXUtnFEMI", "wJalrXUtnFEMI", "aws_secret_access_key"),
        ("header Authorization: Bearer abc.def-ghi", "abc.def-ghi", "Authorization: Bearer"),
        ("Basic YWRhOmh1bnRlcjI= refused", "YWRhOmh1bnRlcjI=", "refused"),
        ("key AKIAIOSFODNN7EXAMPLE has no access", "AKIAIOSFODNN7EXAMPLE", "has no access"),
        ("secret wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY leaked", "wJalrXUtnFEMIK7MDENG", "leaked"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln was refused", "eyJzdWIiOiJ4In0", "was refused"),
    ],
)
def test_a_secret_is_replaced_and_the_text_around_it_kept(text: str, secret: str, kept: str) -> None:
    redacted = redact(text)

    assert secret not in redacted
    assert REDACTED in redacted
    assert kept in redacted


@pytest.mark.parametrize(
    "text",
    [
        "orders.amount expected decimal, found string",
        'Error occurred while executing op "silver_sales_order_revenue_pipeline":',
        "run 6c1f3a5e-0000-4000-8000-000000000001 wrote s3://ops/activations/dev/quality/manifest.json",
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    ],
)
def test_text_without_a_secret_is_unchanged(text: str) -> None:
    assert redact(text) == text
