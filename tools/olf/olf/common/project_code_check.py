"""Runtime invariants for descriptor-driven Dagster project definitions.

This module deliberately runs in the project-code dependency environment created
by ``olf check project-code``.  It is kept as Python so it remains testable and
does not depend on a checkout script or shell interpolation.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping, Sequence

    from openlakeforge_core import FloeEntity, LakehouseInventory, Product


# Dagster, Floe and the project's `openlakeforge_runtime` package are untyped or not importable
# from the CLI package, so these protocols name the surface this check relies on.
class _Executor(Protocol):
    name: str


class _Job(Protocol):
    run_config: Mapping[str, Any] | None
    executor_def: _Executor


class _Schedule(Protocol):
    name: str
    job_name: str
    cron_schedule: str
    default_status: object


class _Sensor(Protocol):
    name: str
    default_status: object


class _StageDefinitions(NamedTuple):
    schedules: list[_Schedule]
    sensors: list[_Sensor]


class _ProductDagsterLibrary(Protocol):
    """The `openlakeforge_runtime.product_dagster` members this check calls or patches."""

    ProductDefinitionSpec: Callable[..., object]

    def build_stage_schedules(self, spec: object, *, stage: str | None = None) -> Sequence[_Schedule]: ...

    def build_maintenance_definitions(self, *, stage: str | None = None) -> Any: ...

    def build_gold_maintenance_definitions(self, *, stage: str | None = None) -> Any: ...


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _asset_keys(definitions: object) -> list[tuple[str, ...]]:
    return [
        tuple(key.path)
        for asset_def in getattr(definitions, "assets", ())
        if hasattr(asset_def, "keys")
        for key in asset_def.keys
    ]


def _product_inputs(product: Product) -> tuple[str, ...]:
    """The Silver tables the product reads: the module's own list when it keeps one, else what the project declares."""
    module = import_module(f"lakehouse_code.pipelines.dagster.{product.id}")
    prefix = product.id.upper()
    values = getattr(module, f"{prefix}_SILVER_INPUTS", getattr(module, f"{prefix}_ENTITIES", None))
    if values is not None:
        return tuple(values)
    spec_class = import_module("openlakeforge_runtime.product_dagster").ProductDefinitionSpec
    return tuple(spec_class(domain=product.domain_name, product=product.id).resolve().silver_inputs)


def private_runtime_imports(root: Path, public: Mapping[str, Collection[str]]) -> list[str]:
    """`file:line: name` for every import of a runtime module or name outside the public API.

    `public` maps each public module to its `__all__`; project code is held to it so a runtime refactor
    cannot break a customer project (docs/reference/runtime-api.md).
    """
    package = "openlakeforge_runtime"
    found: list[str] = []
    for path in sorted((root / "lakehouse_code").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            targets: list[tuple[str, str | None]]
            if isinstance(node, ast.Import):
                targets = [(alias.name, None) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [(node.module, alias.name) for alias in node.names]
            else:
                continue
            for module, name in targets:
                if module != package and not module.startswith(f"{package}."):
                    continue
                where = f"{path.relative_to(root)}:{node.lineno}"
                if module == package and name is not None:
                    module, name = f"{package}.{name}", None
                if module not in public:
                    found.append(f"{where}: {module} is not a public runtime module")
                elif name is not None and name not in public[module]:
                    found.append(f"{where}: {module}.{name} is not in the module's public API")
    return found


def validate(root: Path) -> None:
    """Validate product selection, Floe-derived Silver assets, and their Bronze edges."""
    from dagster import Definitions
    from dagster._core.workspace.autodiscovery import loadable_targets_from_python_module
    from openlakeforge_core import load_lakehouse_inventory

    # Imported by name so mypy does not follow into the user project's tree.
    merged_defs = import_module("lakehouse_code.definitions").defs
    product_dagster_lib = cast(_ProductDagsterLibrary, import_module("openlakeforge_runtime.product_dagster"))

    floe_profile = import_module("openlakeforge_runtime.floe_profile")
    execution = floe_profile.render_profile(os.environ)["execution"]
    runner = execution["runner"]
    _verify_floe_orchestration(execution.get("orchestration"))
    if runner.get("type") != "kubernetes_job" or runner.get("job_scope") != "domain":
        _fail(f"Floe must run one Kubernetes Job per domain; the stage profile renders {runner.get('type')!r}")
    runtime = import_module("openlakeforge_runtime")
    public = {name: import_module(name).__all__ for name in runtime.PUBLIC_MODULES}
    for finding in private_runtime_imports(root, public):
        print(f"warning: {finding}", file=sys.stderr)
    inventory = load_lakehouse_inventory(root / "lakehouse_code")
    products = tuple(inventory.products)
    if not products:
        _fail("lakehouse inventory has no products")
    product_ids = {product.id for product in products}
    module_names = ["lakehouse_code.definitions", *(f"lakehouse_code.pipelines.dagster.{p}" for p in product_ids)]
    for module_name in module_names:
        targets = loadable_targets_from_python_module(module_name, str(root))
        if len(targets) != 1 or targets[0].attribute != "defs":
            _fail(f"{module_name} must expose exactly one defs target")

    product_defs = {
        product.id: import_module(f"lakehouse_code.pipelines.dagster.{product.id}").defs for product in products
    }
    merged_keys_list = _asset_keys(merged_defs)
    merged_keys = set(merged_keys_list)
    if len(merged_keys_list) != len(merged_keys):
        _fail("duplicate Dagster asset keys found")
    _verify_bronze_subsetability(inventory, merged_defs)
    _verify_silver_assets(inventory, merged_defs)

    canonical_bronze = {(source.name, item.name) for source in inventory.sources for item in source.resources}
    entities = {
        (domain.name, entity.name): entity for domain in inventory.domains for entity in inventory.floe_entities(domain)
    }
    canonical_silver = set(entities)
    canonical_gold = {key for key in merged_keys if key[0] in product_ids}
    all_product_keys: set[tuple[str, ...]] = set()

    for product in products:
        inputs = _product_inputs(product)
        domain = product.domain_name
        if not inputs or any((domain, name) not in entities for name in inputs):
            _fail(f"{product.id} Silver inputs are not all Floe entities of domain {domain}")

        definition_keys = set(_asset_keys(product_defs[product.id]))
        all_product_keys.update(definition_keys)
        other_products = product_ids - {product.id}
        if not any(key[0] == product.id for key in definition_keys):
            _fail(f"{product.id} definitions did not load product assets")
        if any(key[0] in other_products for key in definition_keys):
            _fail(f"{product.id} definitions load another product's assets")

        job = merged_defs.resolve_job_def(product.job_name)
        _verify_in_process_execution(product, job)
        selected = {tuple(key.path) for key in job.asset_layer.selected_asset_keys}
        expected_silver = {(domain, item) for item in inputs}
        expected_bronze = {(entities[key].source, entities[key].resource) for key in expected_silver}
        expected_gold = {key for key in definition_keys if key[0] == product.id}
        if selected & canonical_bronze != expected_bronze:
            _fail(f"{product.job_name} does not select exactly its Bronze inputs")
        if selected & canonical_silver != expected_silver:
            _fail(f"{product.job_name} does not select exactly its Silver inputs")
        if selected & canonical_gold != expected_gold:
            _fail(f"{product.job_name} does not select exactly its Gold assets")
        if not expected_bronze | expected_silver | expected_gold <= merged_keys:
            _fail(f"{product.id} selected assets are absent from merged definitions")

    if not all_product_keys <= merged_keys:
        _fail("merged definitions do not contain every product asset")
    _verify_shared_assets(inventory, entities, merged_keys_list)
    stages = {stage: _load_stage_definitions(stage) for stage in _SCHEDULE_STAGES}
    _verify_stage_sensors(stages)
    _verify_stage_schedules(
        inventory,
        product_dagster_lib,
        {stage: loaded.schedules for stage, loaded in stages.items()},
        _product_specs(products),
        _maintenance_schedule_names(product_dagster_lib),
    )
    _verify_bronze_dlt_stage_state()
    _verify_bronze_dlt_load_modes()
    Definitions.validate_loadable(merged_defs)
    merged_defs.get_repository_def().load_all_definitions()


def _verify_bronze_subsetability(inventory: LakehouseInventory, definitions: object) -> None:
    """Require each shared Bronze source multi-asset to support product subsets."""
    source_names = {source.name for source in inventory.sources}
    source_assets = [
        asset_def
        for asset_def in getattr(definitions, "assets", ())
        if any(tuple(key.path)[0] in source_names for key in getattr(asset_def, "keys", ()))
    ]
    if len(source_assets) != len(source_names):
        _fail("each Source must have exactly one executable Bronze multi-asset")
    for source_asset in source_assets:
        if not getattr(source_asset, "can_subset", False):
            _fail("Source Bronze multi-assets must support product-specific subsets")
        output_defs = getattr(getattr(source_asset, "op", None), "output_defs", ())
        required_outputs = [output.name for output in output_defs if output.is_required]
        if required_outputs:
            _fail(
                "subsettable Source Bronze multi-assets must have optional outputs; "
                f"required outputs: {required_outputs}"
            )


def _verify_silver_assets(inventory: LakehouseInventory, definitions: object) -> None:
    """Require one subsettable Floe asset per domain, defining exactly its Silver tables, each fed by its own resource.

    Each domain's asset is one Kubernetes Job; subsettable, so a run that
    needs some of a domain's tables runs only those entities. The Bronze edge
    comes from the entity's `source.ref`/`resource` in Floe, and the Silver
    tables are the domain's Floe entities.
    """
    tables = {
        (domain.name, entity.name): (entity.source, entity.resource)
        for domain in inventory.domains
        for entity in inventory.floe_entities(domain)
    }
    found: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    job_domains: list[str] = []
    for asset_def in getattr(definitions, "assets", ()):
        tags = getattr(getattr(asset_def, "op", None), "tags", None) or {}
        if tags.get("dagster/compute_kind") != "floe":
            continue
        # dagster-floe marks each entity's table `floe/output: accepted` and
        # its quarantine table `floe/output: rejected`.
        accepted = [
            spec.key for spec in getattr(asset_def, "specs", ()) if spec.metadata.get("floe/output") == "accepted"
        ]
        domains = {tuple(key.path)[0] for key in accepted}
        if len(domains) != 1:
            _fail(f"each Floe Job asset must span exactly one domain; got {sorted(domains)}")
        if not getattr(asset_def, "can_subset", False):
            _fail(f"Floe Job asset for domain {next(iter(domains))} must support subsets of its tables")
        job_domains.extend(domains)
        deps = getattr(asset_def, "asset_deps", {})
        for key in accepted:
            found[tuple(key.path)] = {tuple(dep.path) for dep in deps.get(key, ())}
    if len(job_domains) != len(set(job_domains)):
        _fail(f"each domain must run as one Floe Job; got {sorted(job_domains)}")
    if set(found) != set(tables):
        _fail(f"Floe Silver assets {sorted(found)} do not match the Floe entities {sorted(tables)}")
    for key, bronze in tables.items():
        if bronze not in found[key]:
            _fail(f"Silver asset {'/'.join(key)} must depend on its Bronze resource {'/'.join(bronze)}")


def _verify_floe_orchestration(orchestration: Any) -> None:
    """Require a parallel Floe orchestration to bound how many of a domain Job's entities run at once."""
    if not isinstance(orchestration, dict) or orchestration.get("strategy") == "sequential":
        return
    limit = orchestration.get("max_concurrent_entities")
    if not isinstance(limit, int) or limit < 1:
        _fail("the stage profile's parallel Floe orchestration must set max_concurrent_entities")


def _verify_in_process_execution(product: Product, job: _Job) -> None:
    """Require the product job to run its steps in the run's own process.

    A process per step re-imports the code location before each one, about a
    third of a product run. Any `execution` run config is a multiprocess
    setting, which the in-process executor refuses when the run starts.
    """
    if job.executor_def.name != "in_process":
        _fail(f"{product.job_name} must use the in-process executor; it uses {job.executor_def.name!r}")
    if "execution" in (job.run_config or {}):
        _fail(f"{product.job_name} run config must not set execution; the in-process executor takes none")


_STAGE_ENV = "OPENLAKEFORGE_STAGE"
_SCHEDULE_STAGES = ("dev", "uat", "", "prod")


def _maintenance_schedule_names(library: _ProductDagsterLibrary) -> frozenset[str]:
    """The maintenance schedules `lakehouse_code/definitions.py` merges in: prod only, never started by a deploy."""
    return frozenset(
        schedule.name
        for build in (library.build_maintenance_definitions, library.build_gold_maintenance_definitions)
        for schedule in build(stage="prod").schedules or ()
    )


def _product_specs(products: Sequence[Product]) -> dict[str, object]:
    """The `spec` each product's pipeline module declares, which the schedule check compares the loaded code with.

    A module that calls `build_product_definitions` without keeping its spec in a module-level `spec` is refused:
    the check could not tell what cadence it declared, so it would pass whatever the module ships.
    """
    specs: dict[str, object] = {}
    for product in products:
        module = import_module(f"lakehouse_code.pipelines.dagster.{product.id}")
        spec = getattr(module, "spec", None)
        if spec is not None:
            specs[product.id] = spec
        elif _calls_build_product_definitions(module):
            _fail(
                f"{module.__name__} calls build_product_definitions but does not expose a module-level `spec`; "
                "assign the ProductDefinitionSpec to `spec` so the cadence it declares can be checked"
            )
    return specs


def _calls_build_product_definitions(module: object) -> bool:
    path = getattr(module, "__file__", None)
    if not path:
        return False
    for node in ast.walk(ast.parse(Path(path).read_text(), filename=path)):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "build_product_definitions") or (
                isinstance(func, ast.Attribute) and func.attr == "build_product_definitions"
            ):
                return True
    return False


@contextmanager
def _fresh_project_modules() -> Iterator[None]:
    """Let `lakehouse_code` be imported again, and put the modules the caller already holds back afterwards."""
    held = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "lakehouse_code"}
    for name in held:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == "lakehouse_code"]:
            del sys.modules[name]
        sys.modules.update(held)


def _load_stage_definitions(stage: str) -> _StageDefinitions:
    """The schedules and sensors `lakehouse_code.definitions` loads when the pod reports `stage`, as shipped."""
    with _environment({_STAGE_ENV: stage}), _fresh_project_modules():
        defs = import_module("lakehouse_code.definitions").defs
        return _StageDefinitions(list(defs.schedules or ()), list(defs.sensors or ()))


def _verify_stage_sensors(stages: Mapping[str, _StageDefinitions]) -> None:
    """Refuse every sensor the code location defines, in every stage.

    A sensor with `default_status=RUNNING` starts runs on deploy in DEV, UAT and
    PROD alike, outside the cadence. No sensor ships today and the cadence is the
    only declared way to start runs, so any sensor is refused rather than only
    RUNNING ones. `Definitions.sensors` holds just the sensors the code defines:
    Dagster adds its implicit `default_automation_condition_sensor` (STOPPED by
    default) when the repository is built, so assets that carry automation
    conditions do not trip this. Defining an `AutomationConditionSensorDefinition`
    yourself does.
    """
    for stage, loaded in stages.items():
        for sensor in loaded.sensors:
            status = getattr(sensor.default_status, "value", sensor.default_status)
            _fail(
                f"stage {stage!r} loads the sensor {sensor.name} (default status {status}); a sensor can start runs "
                "when a revision deploys, so none may be defined: schedule the product through its cadence instead"
            )


def _verify_stage_schedules(
    inventory: LakehouseInventory,
    library: _ProductDagsterLibrary,
    loaded: Mapping[str, Sequence[_Schedule]],
    specs: Mapping[str, object],
    maintenance: Collection[str],
) -> None:
    """Require the schedules the code location loads to be the ones each stage and each product's cadence allow.

    Every stage runs the same immutable revision, so this is the only thing
    standing between a promotion and a recurring production-shaped workload
    appearing in DEV. `loaded` is what `lakehouse_code.definitions` actually
    loads for each stage in `_SCHEDULE_STAGES`, hand-written schedules
    included; it is compared with what `build_stage_schedules` returns for the
    `spec` each product module declares (a product whose module declares none
    is held to the default cadence). DEV, UAT and an unset stage must load no
    schedule. PROD must load nothing but those product schedules, with their
    job, cron and status, and the maintenance schedules, which stay STOPPED. A
    schedule defaults to RUNNING only through `enabled_in_prod=True` in the
    cadence. A schedule the cadence declares RUNNING must also be among those
    PROD loads: a module that declares `enabled_in_prod=True` but ships
    definitions built from another spec would otherwise pass. How often PROD
    runs, or whether it has a schedule at all, is the product's cadence and not
    this check's business. `maintenance` names the maintenance schedules.
    """
    expected: dict[str, tuple[Any, str]] = {}
    for product in inventory.products:
        spec = specs.get(product.id) or library.ProductDefinitionSpec(domain=product.domain_name, product=product.id)
        for stage in ("dev", "uat", ""):
            if library.build_stage_schedules(spec, stage=stage):
                _fail(f"stage {stage!r} must define no schedule for {product.job_name}")
        schedules = library.build_stage_schedules(spec, stage="prod")
        if len(schedules) > 1:
            _fail(f"prod must define at most one schedule for {product.job_name}")
        for schedule in schedules:
            if schedule.job_name != product.job_name:
                _fail(f"{schedule.name} targets {schedule.job_name}, not the descriptor job {product.job_name}")
            expected[schedule.name] = (schedule, product.job_name)
    for stage in ("dev", "uat", ""):
        if names := [schedule.name for schedule in loaded.get(stage, ())]:
            _fail(f"stage {stage!r} must load no schedule; the code location loads {sorted(names)}")
    seen: set[str] = set()
    for schedule in loaded.get("prod", ()):
        status = getattr(schedule.default_status, "value", schedule.default_status)
        if schedule.name in seen:
            _fail(f"prod loads the schedule {schedule.name} more than once")
        seen.add(schedule.name)
        if schedule.name in maintenance:
            if status != "STOPPED":
                _fail(f"{schedule.name} defaults to {status}; maintenance deletes data, so deploying must not start it")
            continue
        if schedule.name not in expected:
            _fail(
                f"prod loads the schedule {schedule.name}, which no product cadence declares "
                f"(declared: {sorted(expected)}); build it from the product's cadence"
            )
        declared, _ = expected[schedule.name]
        want = getattr(declared.default_status, "value", declared.default_status)
        if status == "RUNNING" and want != "RUNNING":
            _fail(
                f"{schedule.name} defaults to RUNNING; deploying a revision must not start a schedule "
                "unless the cadence sets enabled_in_prod=True"
            )
        if schedule.job_name != declared.job_name or schedule.cron_schedule != declared.cron_schedule or status != want:
            _fail(
                f"{schedule.name} loads as job {schedule.job_name}, cron {schedule.cron_schedule!r}, {status}; "
                f"the product's cadence declares {declared.job_name}, {declared.cron_schedule!r}, {want}"
            )
    for name, (declared, _) in expected.items():
        want = getattr(declared.default_status, "value", declared.default_status)
        if want == "RUNNING" and name not in seen:
            _fail(
                f"the cadence declares {name} RUNNING in prod, but the code location does not load it; "
                "ship the definitions built from the same spec"
            )


def _verify_shared_assets(
    inventory: LakehouseInventory, entities: Mapping[tuple[str, str], FloeEntity], asset_keys: list[tuple[str, ...]]
) -> None:
    inputs = [(product.domain_name, name) for product in inventory.products for name in _product_inputs(product)]
    counts = Counter((entities[key].source, entities[key].resource) for key in inputs)
    counts.update(inputs)
    for key, count in counts.items():
        if count > 1 and asset_keys.count(key) != 1:
            _fail(f"shared asset {key[0]}/{key[1]} must have exactly one definition")


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _verify_bronze_dlt_stage_state() -> None:
    """One dlt source, promoted unchanged, keeps each stage's bindings and state its own (#546).

    A mocked external API stands in for a real one, so the check needs no
    network: it serves different records per base URL. DEV and PROD run the
    same source with their own `SOURCES__*` bindings, as the run pod receives
    them, into their own destination. A second run on a fresh working
    directory must resume from the state in its own destination only.
    """
    import dlt

    bronze_dlt = import_module("openlakeforge_runtime.bronze_dlt")
    api = {
        "https://api.dev.example": [{"id": 1, "updated_at": 1}, {"id": 2, "updated_at": 2}],
        "https://api.prod.example": [{"id": 10, "updated_at": 1}],
    }
    calls: list[tuple[str, str]] = []

    @dlt.source(name="mock_api")
    def mock_api(base_url: str = dlt.config.value, api_token: str = dlt.secrets.value) -> object:
        @dlt.resource(write_disposition="append")
        # dlt reads the cursor from this default argument; it is dlt's own idiom.
        def records(
            updated_at: Any = dlt.sources.incremental("updated_at", initial_value=0),  # noqa: B008
        ) -> Iterator[object]:
            calls.append((base_url, api_token))
            yield [row for row in api[base_url] if row["updated_at"] > updated_at.last_value]

        return records

    def load(stage: str, bucket: Path) -> int:
        # dlt resolves `dlt.config.value` / `dlt.secrets.value` from os.environ itself, so the
        # source bindings are the one thing that must be exported; the stage goes by argument.
        bindings = {
            "SOURCES__MOCK_API__BASE_URL": f"https://api.{stage}.example",
            "SOURCES__MOCK_API__API_TOKEN": f"{stage}-token",
        }
        with _environment(bindings):
            pipeline = bronze_dlt.bronze_pipeline(
                "mock_api", environ={"OPENLAKEFORGE_STAGE": stage}, bucket_url=bucket.as_uri()
            )
            pipeline.run(mock_api(), loader_file_format="jsonl")
        return int(pipeline.last_trace.last_normalize_info.row_counts.get("records", 0))

    with tempfile.TemporaryDirectory(prefix="olf-dlt-check-") as work:
        dev, prod = Path(work, "dev-bronze"), Path(work, "prod-bronze")
        first = (load("dev", dev), load("prod", prod))
        api["https://api.dev.example"].append({"id": 3, "updated_at": 3})
        second = (load("dev", dev), load("prod", prod))
        if first != (2, 1) or second != (1, 0):
            _fail(f"dlt incremental state is not per stage: loaded {first} then {second} (dev, prod) rows")
        if sorted(set(calls)) != [("https://api.dev.example", "dev-token"), ("https://api.prod.example", "prod-token")]:
            _fail(f"a stage read another stage's source bindings: {sorted(set(calls))}")
        for stage, bucket in (("dev", dev), ("prod", prod)):
            states = {path.name.split("__")[0] for path in (bucket / "mock_api" / "_dlt_pipeline_state").iterdir()}
            if states != {stage}:
                _fail(f"{stage}'s Bronze destination holds dlt state for {sorted(states)}")
            if not any((bucket / "mock_api" / "records").iterdir()):
                _fail(f"{stage}'s records did not land under <bucket>/mock_api/records/, where Floe reads Bronze")

    # The loads above use local destinations; a stage loads to its S3 bucket,
    # which dlt reaches through s3fs. Opening that filesystem needs no network.
    # Explicit keys keep the check off the host's AWS credential chain, which a
    # stage pod's workload identity fills instead.
    from dlt.common.storages import fsspec_from_config

    stage_bucket = {
        "OPENLAKEFORGE_STAGE": "dev",
        "OPENLAKEFORGE_STORAGE_BRONZE_BUCKET": "dev-bronze",
        "AWS_ENDPOINT_URL_S3": "http://s3.invalid",
        "AWS_ACCESS_KEY_ID": "check-key",
        "AWS_SECRET_ACCESS_KEY": "check-secret",
    }
    pipeline = bronze_dlt.bronze_pipeline("mock_api", environ=stage_bucket)
    try:
        fsspec_from_config(pipeline.destination_client().config)
    except Exception as exc:  # dlt raises its own MissingDependencyException
        _fail(f"dlt cannot open a stage's S3 Bronze bucket ({type(exc).__name__}): {exc}")


_LOAD_MODES_SOURCE = """\
apiVersion: openlakeforge.io/v1beta1
kind: Source
name: mock_api
displayName: Mock API
description: A mocked API for the load-mode check.
status: planned
resources:
  - name: records
    load: { mode: incremental, cursor: updated_at, primary_key: [id] }
  - name: lookups
"""


def _verify_bronze_dlt_load_modes() -> None:
    """`source.yaml` load modes drive what a dlt load writes to Bronze (#343).

    An incremental resource appends only the rows past its cursor, and a run
    with nothing new writes nothing; a full resource replaces its files. Newer
    batches must sort last, since Floe merges a repeated key from the latest
    file in URI order.
    """
    import dlt

    bronze_dlt = import_module("openlakeforge_runtime.bronze_dlt")
    records = [{"id": 1, "updated_at": 1, "v": "a"}, {"id": 2, "updated_at": 2, "v": "b"}]

    def source(cursor: str = "updated_at") -> object:
        @dlt.source(name="mock_api")
        def mock_api() -> object:
            @dlt.resource(name="records")
            # dlt reads the cursor from this default argument; it is dlt's own idiom.
            def records_resource(
                updated_at: Any = dlt.sources.incremental(cursor, initial_value=0),  # noqa: B008
            ) -> Iterator[object]:
                yield [row for row in records if row["updated_at"] > updated_at.last_value]

            @dlt.resource
            def lookups() -> Iterator[object]:
                yield [{"code": "x"}, {"code": "y"}]

            return records_resource, lookups

        return mock_api()

    with tempfile.TemporaryDirectory(prefix="olf-dlt-modes-") as work:
        root, bucket = Path(work, "lakehouse_code"), Path(work, "bronze")
        (root / "bronze" / "mock_api").mkdir(parents=True)
        (root / "bronze" / "mock_api" / "source.yaml").write_text(_LOAD_MODES_SOURCE, encoding="utf-8")

        def load(*, cursor: str = "updated_at") -> dict[str, int]:
            results = bronze_dlt.load_dlt_source(
                "mock_api",
                source(cursor),
                ("records", "lookups"),
                environ={"OPENLAKEFORGE_STAGE": "dev"},
                bucket_url=bucket.as_uri(),
                lakehouse_root=root,
            )
            return {name: result.rows for name, result in results.items()}

        def files(resource: str) -> list[str]:
            return sorted(path.name for path in (bucket / "mock_api" / resource).glob("*.parquet"))

        first, unchanged = load(), load()
        if first != {"records": 2, "lookups": 2} or unchanged != {"records": 0, "lookups": 2}:
            _fail(f"dlt load modes: first run loaded {first}, a run with no change loaded {unchanged}")
        if len(files("records")) != 1 or len(files("lookups")) != 1:
            _fail(f"dlt load modes: Bronze holds {files('records')} and {files('lookups')} after an unchanged run")

        records[0] = {"id": 1, "updated_at": 3, "v": "a2"}
        records.append({"id": 3, "updated_at": 4, "v": "c"})
        delta = load()
        batches = files("records")
        if delta["records"] != 2 or len(batches) != 2:
            _fail(f"dlt load modes: a changed and a new row loaded {delta['records']} row(s) into {batches}")
        newest = import_module("pyarrow.parquet").read_table(bucket / "mock_api" / "records" / batches[-1])
        if sorted(newest.column("id").to_pylist()) != [1, 3]:
            _fail(f"dlt load modes: the newest batch does not sort last in Bronze: {batches}")

        try:
            load(cursor="v")
        except RuntimeError as exc:
            if "updated_at" not in str(exc):
                _fail(f"dlt load modes: a cursor mismatch failed without naming the declared cursor: {exc}")
        else:
            _fail("dlt load modes: a connector whose cursor is not the declared one loaded anyway")


def main() -> None:
    try:
        validate(Path(sys.argv[1]).resolve())
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print("Merged and product Dagster definitions loaded.")


if __name__ == "__main__":
    main()
