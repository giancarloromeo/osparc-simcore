# pylint: disable=redefined-outer-name
# pylint: disable=unused-argument

import random

import pytest
from _pytest.monkeypatch import MonkeyPatch
from dask.distributed import LocalCluster, Scheduler, Worker
from distributed.deploy.spec import SpecCluster
from models_library.service_settings_labels import SimcoreServiceLabels
from pydantic.types import NonNegativeInt
from simcore_service_director_v2.models.domains.dynamic_services import (
    DynamicServiceCreate,
)
from simcore_service_director_v2.models.schemas.dynamic_services import (
    SchedulerData,
    ServiceDetails,
    ServiceLabelsStoredData,
)
from yarl import URL


@pytest.fixture
def simcore_services_network_name() -> str:
    return "test_network_name"


@pytest.fixture(autouse=True)
def disable_dynamic_sidecar_scheduler_in_unit_tests(
    monkeypatch, simcore_services_network_name: str
) -> None:
    # FIXME: PC-> ANE: please avoid autouse!!!
    monkeypatch.setenv("REGISTRY_AUTH", "false")
    monkeypatch.setenv("REGISTRY_USER", "test")
    monkeypatch.setenv("REGISTRY_PW", "test")
    monkeypatch.setenv("REGISTRY_SSL", "false")
    monkeypatch.setenv("SIMCORE_SERVICES_NETWORK_NAME", simcore_services_network_name)
    monkeypatch.setenv("TRAEFIK_SIMCORE_ZONE", "test_traefik_zone")
    monkeypatch.setenv("SWARM_STACK_NAME", "test_swarm_name")


@pytest.fixture
def simcore_service_labels() -> SimcoreServiceLabels:
    return SimcoreServiceLabels(
        **SimcoreServiceLabels.Config.schema_extra["examples"][1]
    )


@pytest.fixture
def dynamic_service_create() -> DynamicServiceCreate:
    return DynamicServiceCreate.parse_obj(ServiceDetails.Config.schema_extra["example"])


@pytest.fixture
def service_labels_stored_data() -> ServiceLabelsStoredData:
    return ServiceLabelsStoredData.parse_obj(
        ServiceLabelsStoredData.Config.schema_extra["example"]
    )


@pytest.fixture(scope="session")
def dynamic_sidecar_port() -> int:
    return 1222


@pytest.fixture
def scheduler_data_from_http_request(
    dynamic_service_create: DynamicServiceCreate,
    simcore_service_labels: SimcoreServiceLabels,
    dynamic_sidecar_port: int,
) -> SchedulerData:
    return SchedulerData.from_http_request(
        service=dynamic_service_create,
        simcore_service_labels=simcore_service_labels,
        port=dynamic_sidecar_port,
    )


@pytest.fixture
def scheduler_data_from_service_labels_stored_data(
    service_labels_stored_data: ServiceLabelsStoredData, dynamic_sidecar_port: int
) -> SchedulerData:
    return SchedulerData.from_service_labels_stored_data(
        service_labels_stored_data=service_labels_stored_data, port=dynamic_sidecar_port
    )


@pytest.fixture
async def dask_local_cluster(monkeypatch: MonkeyPatch) -> LocalCluster:
    async with LocalCluster(
        n_workers=2, threads_per_worker=1, asynchronous=True
    ) as cluster:
        scheduler_address = URL(cluster.scheduler_address)
        monkeypatch.setenv("DASK_SCHEDULER_HOST", scheduler_address.host or "invalid")
        monkeypatch.setenv("DASK_SCHEDULER_PORT", f"{scheduler_address.port}")
        yield cluster


@pytest.fixture
def cluster_id() -> NonNegativeInt:
    return random.randint(0, 10)


@pytest.fixture
def cluster_id_resource(cluster_id: NonNegativeInt) -> str:
    return f"CLUSTER_{cluster_id}"


@pytest.fixture
async def dask_spec_local_cluster(
    monkeypatch: MonkeyPatch, cluster_id_resource: str
) -> SpecCluster:
    # in this mode we can precisely create a specific cluster
    workers = {
        "cpu-worker": {
            "cls": Worker,
            "options": {
                "nthreads": 2,
                "resources": {"CPU": 2, "RAM": 48e9, cluster_id_resource: 1},
            },
        },
        "gpu-worker": {
            "cls": Worker,
            "options": {
                "nthreads": 1,
                "resources": {"CPU": 1, "GPU": 1, "RAM": 48e9, cluster_id_resource: 1},
            },
        },
        "mpi-worker": {
            "cls": Worker,
            "options": {
                "nthreads": 1,
                "resources": {"CPU": 8, "MPI": 1, "RAM": 768e9, cluster_id_resource: 1},
            },
        },
        "gpu-mpi-worker": {
            "cls": Worker,
            "options": {
                "nthreads": 1,
                "resources": {"GPU": 1, "MPI": 1, "RAM": 768e9, cluster_id_resource: 1},
            },
        },
    }
    scheduler = {"cls": Scheduler, "options": {"dashboard_address": ":8787"}}

    async with SpecCluster(
        workers=workers, scheduler=scheduler, asynchronous=True
    ) as cluster:
        scheduler_address = URL(cluster.scheduler_address)
        monkeypatch.setenv("DASK_SCHEDULER_HOST", scheduler_address.host or "invalid")
        monkeypatch.setenv("DASK_SCHEDULER_PORT", f"{scheduler_address.port}")
        yield cluster