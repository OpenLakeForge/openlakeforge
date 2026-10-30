import pytest

pytest.importorskip("dlt")

from libs.bronze_dlt import bronze_pipeline  # noqa: E402

_STAGE = {"OPENLAKEFORGE_STAGE": "prod", "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "lakehouse-prod-bronze"}


def test_the_pipeline_is_named_for_stage_and_source_so_stages_never_share_state() -> None:
    dev = bronze_pipeline("orders", environ={**_STAGE, "OPENLAKEFORGE_STAGE": "dev"})
    prod = bronze_pipeline("orders", environ=_STAGE)

    assert (dev.pipeline_name, prod.pipeline_name) == ("dev__orders", "prod__orders")
    assert prod.dataset_name == "orders"
    assert dev.pipelines_dir != prod.pipelines_dir


def test_the_destination_is_the_stage_bronze_bucket() -> None:
    pipeline = bronze_pipeline("orders", environ=_STAGE)

    assert pipeline.destination.destination_name == "filesystem"
    assert pipeline.destination.config_params["bucket_url"] == "s3://lakehouse-prod-bronze"


def test_bucket_url_replaces_the_bucket_but_not_the_stage_in_the_pipeline_name(tmp_path) -> None:
    pipeline = bronze_pipeline("orders", environ=_STAGE, bucket_url=tmp_path.as_uri())

    assert pipeline.pipeline_name == "prod__orders"
    assert pipeline.destination.config_params["bucket_url"] == tmp_path.as_uri()


@pytest.mark.parametrize("missing", ["OPENLAKEFORGE_STAGE", "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET"])
def test_a_stage_that_does_not_set_its_bindings_fails_naming_them(missing: str) -> None:
    environ = {key: value for key, value in _STAGE.items() if key != missing}

    with pytest.raises(RuntimeError, match=missing):
        bronze_pipeline("orders", environ=environ)


def test_whitespace_only_bindings_count_as_unset() -> None:
    with pytest.raises(RuntimeError, match="OPENLAKEFORGE_STAGE"):
        bronze_pipeline("orders", environ={**_STAGE, "OPENLAKEFORGE_STAGE": "  "})
