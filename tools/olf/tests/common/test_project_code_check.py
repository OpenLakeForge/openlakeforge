from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from olf.common import project_code_check


def _source_asset(*, can_subset: bool, output_required: bool) -> SimpleNamespace:
    return SimpleNamespace(
        keys=(SimpleNamespace(path=("crm", "orders")),),
        can_subset=can_subset,
        op=SimpleNamespace(output_defs=(SimpleNamespace(name="orders", is_required=output_required),)),
    )


def test_bronze_subsetability_requires_one_optional_subsettable_multi_asset_per_source() -> None:
    inventory = SimpleNamespace(sources=(SimpleNamespace(name="crm"),))
    definitions = SimpleNamespace(assets=(_source_asset(can_subset=True, output_required=False),))

    project_code_check._verify_bronze_subsetability(inventory, definitions)


@pytest.mark.parametrize(
    ("can_subset", "output_required", "message"),
    [
        (False, False, "must support product-specific subsets"),
        (True, True, "must have optional outputs"),
    ],
)
def test_bronze_subsetability_rejects_non_subsettable_or_required_outputs(
    can_subset: bool, output_required: bool, message: str
) -> None:
    inventory = SimpleNamespace(sources=(SimpleNamespace(name="crm"),))
    definitions = SimpleNamespace(assets=(_source_asset(can_subset=can_subset, output_required=output_required),))

    with pytest.raises(RuntimeError, match=message):
        project_code_check._verify_bronze_subsetability(inventory, definitions)


def _job(executor: str, run_config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(executor_def=SimpleNamespace(name=executor), run_config=run_config)


def test_a_product_job_runs_its_steps_in_process() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")

    project_code_check._verify_in_process_execution(product, _job("in_process"))
    project_code_check._verify_in_process_execution(product, _job("in_process", {"ops": {}}))


def test_a_product_job_on_the_multiprocess_executor_is_refused() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")

    with pytest.raises(RuntimeError, match="order_revenue_job must use the in-process executor; it uses 'multi"):
        project_code_check._verify_in_process_execution(product, _job("multi_or_in_process_executor"))


def test_a_product_job_with_execution_run_config_is_refused() -> None:
    product = SimpleNamespace(id="order_revenue", job_name="order_revenue_job")
    job = _job("in_process", {"execution": {"config": {"multiprocess": {"max_concurrent": 4}}}})

    with pytest.raises(RuntimeError, match="run config must not set execution"):
        project_code_check._verify_in_process_execution(product, job)


@pytest.mark.parametrize(
    "orchestration",
    [
        {"strategy": "sequential"},
        {"strategy": "sequential", "max_concurrent_entities": 4},
        {"strategy": "parallel", "max_concurrent_entities": 4},
        None,
    ],
)
def test_a_bounded_or_sequential_floe_orchestration_is_accepted(orchestration: dict | None) -> None:
    project_code_check._verify_floe_orchestration(orchestration)


def test_parallel_floe_orchestration_without_a_limit_is_refused() -> None:
    with pytest.raises(RuntimeError, match="must set max_concurrent_entities"):
        project_code_check._verify_floe_orchestration({"strategy": "parallel"})


@dataclass(frozen=True)
class _Key:
    """Hashable like Dagster's AssetKey, which keys `asset_deps`."""

    path: tuple[str, ...]


def _key(path: tuple[str, ...]) -> _Key:
    return _Key(path)


def _domain_job(
    entities: dict[str, tuple[str, str]], *, domain: str = "sales", can_subset: bool = True
) -> SimpleNamespace:
    """dagster-floe's asset for one domain's Job: an accepted and a rejected output per entity."""
    specs: list[SimpleNamespace] = []
    deps: dict[_Key, list[_Key]] = {}
    for table, bronze in entities.items():
        accepted, rejected = _key((domain, table)), _key((domain, "rejected", table))
        specs.append(SimpleNamespace(key=accepted, metadata={"floe/entity": table, "floe/output": "accepted"}))
        specs.append(SimpleNamespace(key=rejected, metadata={"floe/entity": table, "floe/output": "rejected"}))
        deps[accepted] = deps[rejected] = [_key(bronze)]
    return SimpleNamespace(
        op=SimpleNamespace(tags={"dagster/compute_kind": "floe"}),
        specs=specs,
        asset_deps=deps,
        can_subset=can_subset,
    )


def _silver_inventory(*tables: str) -> SimpleNamespace:
    entities = [SimpleNamespace(name=table, source="crm", resource=table) for table in tables or ("orders",)]
    return SimpleNamespace(domains=[SimpleNamespace(name="sales")], floe_entities=lambda _domain: entities)


def test_silver_assets_must_each_depend_on_their_own_bronze_resource() -> None:
    wired = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")})])
    project_code_check._verify_silver_assets(_silver_inventory(), wired)

    shared = SimpleNamespace(assets=[_domain_job({"orders": ("sources", "openlakeforge", "crm")})])
    with pytest.raises(RuntimeError, match="must depend on its Bronze resource crm/orders"):
        project_code_check._verify_silver_assets(_silver_inventory(), shared)


def test_silver_assets_must_match_the_floe_entities() -> None:
    extra = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders"), "returns": ("crm", "returns")})])

    with pytest.raises(RuntimeError, match="do not match the Floe entities"):
        project_code_check._verify_silver_assets(_silver_inventory(), extra)


def test_a_domain_runs_as_one_subsettable_floe_job() -> None:
    split = SimpleNamespace(
        assets=[_domain_job({"orders": ("crm", "orders")}), _domain_job({"accounts": ("crm", "accounts")})]
    )
    with pytest.raises(RuntimeError, match="each domain must run as one Floe Job"):
        project_code_check._verify_silver_assets(_silver_inventory("orders", "accounts"), split)

    whole = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")}, can_subset=False)])
    with pytest.raises(RuntimeError, match="must support subsets"):
        project_code_check._verify_silver_assets(_silver_inventory(), whole)


def test_a_declared_table_ending_in_rejected_is_not_mistaken_for_a_rejection_output() -> None:
    inventory = _silver_inventory("orders_rejected")
    definitions = SimpleNamespace(assets=[_domain_job({"orders_rejected": ("crm", "orders_rejected")})])

    project_code_check._verify_silver_assets(inventory, definitions)


def test_gold_assets_of_a_product_named_like_its_domain_are_not_silver() -> None:
    gold = SimpleNamespace(
        op=SimpleNamespace(tags={"dagster/compute_kind": "dbt"}),
        specs=[SimpleNamespace(key=_key(("sales", "revenue")), metadata={})],
        asset_deps={},
    )
    definitions = SimpleNamespace(assets=[_domain_job({"orders": ("crm", "orders")}), gold])

    project_code_check._verify_silver_assets(_silver_inventory(), definitions)


def _schedule(name: str, *, job: str = "order_revenue_pipeline", cron: str = "0 3 * * *", status: str = "STOPPED"):
    return SimpleNamespace(name=name, job_name=job, cron_schedule=cron, default_status=SimpleNamespace(value=status))


def _schedule_library(*, status: str = "STOPPED", dev: bool = False, count: int = 1) -> SimpleNamespace:
    declared = _schedule("order_revenue_daily", status=status)
    return SimpleNamespace(
        ProductDefinitionSpec=lambda **fields: SimpleNamespace(**fields),
        build_stage_schedules=lambda spec, *, stage: (
            [declared] * count if stage == "prod" or (dev and stage == "dev") else []
        ),
    )


def _inventory() -> SimpleNamespace:
    product = SimpleNamespace(id="order_revenue", domain_name="sales", job_name="order_revenue_pipeline")
    return SimpleNamespace(products=(product,))


_MAINTENANCE = frozenset({"silver_maintenance_weekly", "gold_maintenance_weekly"})


def _loaded(prod: list | None = None, **other_stages: list) -> dict[str, list]:
    stages: dict[str, list] = {"dev": [], "uat": [], "": [], "prod": prod or []}
    stages.update(other_stages)
    return stages


def test_stage_schedules_accept_the_declared_schedule_in_prod_and_none_elsewhere() -> None:
    maintenance = _schedule("silver_maintenance_weekly", job="silver_maintenance")
    loaded = _loaded([_schedule("order_revenue_daily"), maintenance])

    project_code_check._verify_stage_schedules(_inventory(), _schedule_library(), loaded, {}, _MAINTENANCE)
    project_code_check._verify_stage_schedules(_inventory(), _schedule_library(), _loaded(), {}, _MAINTENANCE)


@pytest.mark.parametrize(
    ("library", "loaded", "message"),
    [
        (_schedule_library(dev=True), _loaded(), "stage 'dev' must define no schedule"),
        (_schedule_library(count=2), _loaded(), "at most one schedule"),
        (_schedule_library(), _loaded(uat=[_schedule("order_revenue_daily")]), "stage 'uat' must load no schedule"),
        (_schedule_library(), _loaded(**{"": [_schedule("order_revenue_daily")]}), "stage '' must load no schedule"),
        (_schedule_library(), _loaded([_schedule("hand_written")]), "no product cadence declares"),
        (
            _schedule_library(),
            _loaded([_schedule("order_revenue_daily", job="hand_written_job")]),
            "loads as job hand_written_job",
        ),
        (
            _schedule_library(),
            _loaded([_schedule("order_revenue_daily", cron="* * * * *")]),
            "loads as job order_revenue_pipeline, cron '\\* \\* \\* \\* \\*'",
        ),
        (
            _schedule_library(),
            _loaded([_schedule("order_revenue_daily", status="RUNNING")]),
            "must not start a schedule unless the cadence sets enabled_in_prod=True",
        ),
        (
            _schedule_library(),
            _loaded([_schedule("order_revenue_daily")] * 2),
            "more than once",
        ),
        (
            _schedule_library(),
            _loaded([_schedule("gold_maintenance_weekly", status="RUNNING")]),
            "maintenance deletes data",
        ),
        (
            _schedule_library(status="RUNNING"),
            _loaded([_schedule("order_revenue_daily")]),
            "contradicts|cadence declares .*RUNNING",
        ),
        (
            _schedule_library(status="RUNNING"),
            _loaded([_schedule("silver_maintenance_weekly", job="silver_maintenance")]),
            "declares order_revenue_daily RUNNING in prod, but the code location does not load it",
        ),
    ],
)
def test_stage_schedules_reject_what_the_code_location_loads_against_the_cadence(
    library: SimpleNamespace, loaded: dict[str, list], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        project_code_check._verify_stage_schedules(_inventory(), library, loaded, {}, _MAINTENANCE)


def test_stage_schedules_accept_a_running_prod_schedule_the_cadence_declares() -> None:
    project_code_check._verify_stage_schedules(
        _inventory(),
        _schedule_library(status="RUNNING"),
        _loaded([_schedule("order_revenue_daily", status="RUNNING")]),
        {},
        _MAINTENANCE,
    )


_FAKE_RUNTIME = """
import os
import sys
from dataclasses import dataclass
from dagster import DefaultScheduleStatus, Definitions, ScheduleDefinition, job, op


@dataclass(frozen=True)
class ProductCadence:
    cron: str = "0 3 * * *"
    enabled_in_prod: bool = False


@dataclass(frozen=True)
class ProductDefinitionSpec:
    domain: str
    product: str
    cadence: ProductCadence | None = None

    @property
    def job_name(self):
        return f"{self.product}_pipeline"


def build_stage_schedules(spec, *, stage=None):
    stage = os.environ.get("OPENLAKEFORGE_STAGE", "") if stage is None else stage
    if stage != "prod":
        return []
    cadence = spec.cadence or ProductCadence()
    status = DefaultScheduleStatus.RUNNING if cadence.enabled_in_prod else DefaultScheduleStatus.STOPPED
    return [ScheduleDefinition(name=f"{spec.product}_schedule", job_name=spec.job_name, cron_schedule=cadence.cron,
                               default_status=status)]


def build_product_definitions(spec):
    @op
    def noop():
        pass

    @job(name=spec.job_name)
    def pipeline():
        noop()

    return Definitions(jobs=[pipeline], schedules=build_stage_schedules(spec))
"""

_PRODUCT_NO_SPEC = """
from fake_runtime import ProductCadence, ProductDefinitionSpec, build_product_definitions

defs = build_product_definitions(
    ProductDefinitionSpec("sales", "order_revenue", cadence=ProductCadence("0 * * * *", enabled_in_prod=True))
)
"""

_PRODUCT_OWN_SCHEDULE = """
from dagster import DefaultScheduleStatus, Definitions, ScheduleDefinition
from fake_runtime import ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec("sales", "order_revenue")
own = ScheduleDefinition(
    name="order_revenue_manual_on", job_name=spec.job_name, cron_schedule="0 3 * * *",
    default_status=DefaultScheduleStatus.RUNNING,
)
defs = Definitions.merge(build_product_definitions(spec), Definitions(schedules=[own]))
"""


_PRODUCT_RUNNING_SENSOR = """
from dagster import DefaultSensorStatus, Definitions, RunRequest, sensor
from fake_runtime import ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec("sales", "order_revenue")


@sensor(job_name=spec.job_name, default_status=DefaultSensorStatus.RUNNING)
def every_minute():
    yield RunRequest()


defs = Definitions.merge(build_product_definitions(spec), Definitions(sensors=[every_minute]))
"""

_PRODUCT_DECLARED_SCHEDULE_NOT_SHIPPED = """
from dagster import Definitions
from fake_runtime import ProductCadence, ProductDefinitionSpec, build_product_definitions

spec = ProductDefinitionSpec("sales", "order_revenue", cadence=ProductCadence("0 * * * *", enabled_in_prod=True))
defs = Definitions(jobs=build_product_definitions(spec).jobs)
"""


def _fixture_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, product_module: str) -> SimpleNamespace:
    pytest.importorskip("dagster")
    code = tmp_path / "lakehouse_code"
    (code / "pipelines" / "dagster").mkdir(parents=True)
    for package in (code, code / "pipelines", code / "pipelines" / "dagster"):
        (package / "__init__.py").write_text("")
    (tmp_path / "fake_runtime.py").write_text(_FAKE_RUNTIME)
    (code / "pipelines" / "dagster" / "order_revenue.py").write_text(product_module)
    (code / "definitions.py").write_text(
        "from dagster import Definitions\n"
        "from lakehouse_code.pipelines.dagster.order_revenue import defs as product\n"
        "defs = Definitions.merge(product)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [name for name in sys.modules if name.split(".")[0] in {"lakehouse_code", "fake_runtime"}]:
        monkeypatch.delitem(sys.modules, name)
    import fake_runtime  # type: ignore[import-not-found]

    products = (SimpleNamespace(id="order_revenue", domain_name="sales", job_name="order_revenue_pipeline"),)
    return SimpleNamespace(library=fake_runtime, products=products)


def _check_fixture_project(project: SimpleNamespace) -> None:
    specs = project_code_check._product_specs(project.products)
    stages = {stage: project_code_check._load_stage_definitions(stage) for stage in project_code_check._SCHEDULE_STAGES}
    project_code_check._verify_stage_sensors(stages)
    project_code_check._verify_stage_schedules(
        SimpleNamespace(products=project.products),
        project.library,
        {stage: loaded.schedules for stage, loaded in stages.items()},
        specs,
        _MAINTENANCE,
    )


def test_a_product_module_that_builds_definitions_without_a_module_level_spec_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fixture_project(tmp_path, monkeypatch, _PRODUCT_NO_SPEC)

    with pytest.raises(RuntimeError, match="does not expose a module-level `spec`"):
        _check_fixture_project(project)


def test_a_schedule_the_module_merges_in_is_refused_in_dev_and_in_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fixture_project(tmp_path, monkeypatch, _PRODUCT_OWN_SCHEDULE)
    specs = project_code_check._product_specs(project.products)
    loaded = {
        stage: project_code_check._load_stage_definitions(stage).schedules
        for stage in project_code_check._SCHEDULE_STAGES
    }
    inventory = SimpleNamespace(products=project.products)

    with pytest.raises(RuntimeError, match="stage 'dev' must load no schedule"):
        project_code_check._verify_stage_schedules(inventory, project.library, loaded, specs, _MAINTENANCE)
    prod_only = {**loaded, "dev": [], "uat": [], "": []}
    with pytest.raises(RuntimeError, match="order_revenue_manual_on, which no product cadence declares"):
        project_code_check._verify_stage_schedules(inventory, project.library, prod_only, specs, _MAINTENANCE)


def test_a_running_sensor_the_module_merges_in_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _fixture_project(tmp_path, monkeypatch, _PRODUCT_RUNNING_SENSOR)

    with pytest.raises(RuntimeError, match="stage 'dev' loads the sensor every_minute \\(default status RUNNING\\)"):
        _check_fixture_project(project)


def test_a_running_schedule_the_cadence_declares_but_the_module_does_not_ship_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _fixture_project(tmp_path, monkeypatch, _PRODUCT_DECLARED_SCHEDULE_NOT_SHIPPED)

    with pytest.raises(RuntimeError, match="declares order_revenue_schedule RUNNING in prod, but the code location"):
        _check_fixture_project(project)


def test_the_declared_schedule_the_module_ships_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _PRODUCT_NO_SPEC.replace(
        "defs = build_product_definitions(\n    ProductDefinitionSpec", "spec = (\n    ProductDefinitionSpec"
    )
    module += "\ndefs = build_product_definitions(spec)\n"
    project = _fixture_project(tmp_path, monkeypatch, module)

    _check_fixture_project(project)


def test_environment_exports_values_to_the_reader_and_restores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES__X__KEPT", "before")
    monkeypatch.delenv("SOURCES__X__NEW", raising=False)
    snapshot = dict(os.environ)

    with project_code_check._environment({"SOURCES__X__KEPT": "inside", "SOURCES__X__NEW": "inside"}):
        assert os.environ["SOURCES__X__KEPT"] == os.environ["SOURCES__X__NEW"] == "inside"

    assert dict(os.environ) == snapshot


_PUBLIC = {
    "openlakeforge_runtime.product_dagster": {"ProductDefinitionSpec", "build_product_definitions"},
    "openlakeforge_runtime.bronze_csv": {"BronzeLoadResult"},
}


def _project(tmp_path: Path, source: str) -> Path:
    module = tmp_path / "lakehouse_code" / "pipelines" / "dagster" / "orders.py"
    module.parent.mkdir(parents=True)
    module.write_text(source)
    return tmp_path


def test_imports_of_the_public_runtime_api_are_not_reported(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "from openlakeforge_runtime.product_dagster import ProductDefinitionSpec, build_product_definitions\n"
        "from openlakeforge_runtime.bronze_csv import BronzeLoadResult\n"
        "import dagster\n",
    )

    assert project_code_check.private_runtime_imports(root, _PUBLIC) == []


def test_private_runtime_modules_and_names_are_reported_with_their_location(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "from openlakeforge_runtime.product_dagster import build_product_definitions, _floe_profile\n"
        "from openlakeforge_runtime.s3_artifacts import upload_file_to_base_uri\n"
        "from openlakeforge_runtime import floe_profile\n"
        "import openlakeforge_runtime.gold_maintenance\n",
    )

    assert project_code_check.private_runtime_imports(root, _PUBLIC) == [
        "lakehouse_code/pipelines/dagster/orders.py:1: openlakeforge_runtime.product_dagster._floe_profile "
        "is not in the module's public API",
        "lakehouse_code/pipelines/dagster/orders.py:2: openlakeforge_runtime.s3_artifacts "
        "is not a public runtime module",
        "lakehouse_code/pipelines/dagster/orders.py:3: openlakeforge_runtime.floe_profile "
        "is not a public runtime module",
        "lakehouse_code/pipelines/dagster/orders.py:4: openlakeforge_runtime.gold_maintenance "
        "is not a public runtime module",
    ]
