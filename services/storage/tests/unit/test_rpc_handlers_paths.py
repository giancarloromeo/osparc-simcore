# pylint:disable=no-name-in-module
# pylint:disable=protected-access
# pylint:disable=redefined-outer-name
# pylint:disable=too-many-arguments
# pylint:disable=too-many-positional-arguments
# pylint:disable=unused-argument
# pylint:disable=unused-variable


import asyncio
import datetime
import random
from pathlib import Path
from typing import Any, TypeAlias

import pytest
from faker import Faker
from fastapi import FastAPI
from models_library.api_schemas_rpc_async_jobs.async_jobs import (
    AsyncJobNameData,
    AsyncJobResult,
)
from models_library.api_schemas_storage import STORAGE_RPC_NAMESPACE
from models_library.products import ProductName
from models_library.projects_nodes_io import LocationID, NodeID, SimcoreS3FileID
from models_library.rabbitmq_basic_types import RPCMethodName
from models_library.users import UserID
from pydantic import ByteSize, TypeAdapter
from pytest_simcore.helpers.storage_utils import FileIDDict, ProjectWithFilesParams
from servicelib.rabbitmq._client_rpc import RabbitMQRPCClient
from servicelib.rabbitmq.rpc_interfaces.async_jobs.async_jobs import (
    wait_and_get_result,
)
from servicelib.rabbitmq.rpc_interfaces.storage.paths import compute_path_size
from simcore_service_storage.modules.celery.worker import CeleryTaskQueueWorker
from simcore_service_storage.simcore_s3_dsm import SimcoreS3DataManager

pytest_simcore_core_services_selection = ["postgres", "rabbit"]
pytest_simcore_ops_services_selection = ["adminer"]

_IsFile: TypeAlias = bool


def _filter_and_group_paths_one_level_deeper(
    paths: list[Path], prefix: Path
) -> list[tuple[Path, _IsFile]]:
    relative_paths = (path for path in paths if path.is_relative_to(prefix))
    return sorted(
        {
            (
                (path, len(path.relative_to(prefix).parts) == 1)
                if len(path.relative_to(prefix).parts) == 1
                else (prefix / path.relative_to(prefix).parts[0], False)
            )
            for path in relative_paths
        },
        key=lambda x: x[0],
    )


async def _assert_compute_path_size(
    storage_rpc_client: RabbitMQRPCClient,
    location_id: LocationID,
    user_id: UserID,
    product_name: ProductName,
    *,
    path: Path,
    expected_total_size: int,
) -> ByteSize:
    async_job, async_job_name = await compute_path_size(
        storage_rpc_client,
        product_name=product_name,
        user_id=user_id,
        location_id=location_id,
        path=path,
    )
    await asyncio.sleep(1)
    async for job_composed_result in wait_and_get_result(
        storage_rpc_client,
        rpc_namespace=STORAGE_RPC_NAMESPACE,
        method_name=RPCMethodName(compute_path_size.__name__),
        job_id=async_job.job_id,
        job_id_data=AsyncJobNameData(user_id=user_id, product_name=product_name),
        client_timeout=datetime.timedelta(seconds=120),
    ):
        if job_composed_result.done:
            response = await job_composed_result.result()
            assert isinstance(response, AsyncJobResult)
            received_size = TypeAdapter(ByteSize).validate_python(response.result)
            assert received_size == expected_total_size
            return received_size

    pytest.fail("Job did not finish")


@pytest.mark.parametrize(
    "location_id",
    [SimcoreS3DataManager.get_location_id()],
    ids=[SimcoreS3DataManager.get_location_name()],
    indirect=True,
)
@pytest.mark.parametrize(
    "project_params",
    [
        ProjectWithFilesParams(
            num_nodes=5,
            allowed_file_sizes=(TypeAdapter(ByteSize).validate_python("1b"),),
            workspace_files_count=10,
        )
    ],
    ids=str,
)
async def test_path_compute_size(
    initialized_app: FastAPI,
    storage_rabbitmq_rpc_client: RabbitMQRPCClient,
    user_id: UserID,
    location_id: LocationID,
    with_random_project_with_files: tuple[
        dict[str, Any],
        dict[NodeID, dict[SimcoreS3FileID, FileIDDict]],
    ],
    project_params: ProjectWithFilesParams,
    product_name: ProductName,
):
    assert (
        len(project_params.allowed_file_sizes) == 1
    ), "test preconditions are not filled! allowed file sizes should have only 1 option for this test"
    project, list_of_files = with_random_project_with_files

    total_num_files = sum(
        len(files_in_node) for files_in_node in list_of_files.values()
    )

    # get size of a full project
    expected_total_size = project_params.allowed_file_sizes[0] * total_num_files
    path = Path(project["uuid"])
    await _assert_compute_path_size(
        storage_rabbitmq_rpc_client,
        location_id,
        user_id,
        path=path,
        expected_total_size=expected_total_size,
        product_name=product_name,
    )

    # get size of one of the nodes
    selected_node_id = NodeID(random.choice(list(project["workbench"])))  # noqa: S311
    path = Path(project["uuid"]) / f"{selected_node_id}"
    selected_node_s3_keys = [
        Path(s3_object_id) for s3_object_id in list_of_files[selected_node_id]
    ]
    expected_total_size = project_params.allowed_file_sizes[0] * len(
        selected_node_s3_keys
    )
    await _assert_compute_path_size(
        storage_rabbitmq_rpc_client,
        location_id,
        user_id,
        path=path,
        expected_total_size=expected_total_size,
        product_name=product_name,
    )

    # get size of the outputs of one of the nodes
    path = Path(project["uuid"]) / f"{selected_node_id}" / "outputs"
    selected_node_s3_keys = [
        Path(s3_object_id)
        for s3_object_id in list_of_files[selected_node_id]
        if s3_object_id.startswith(f"{path}")
    ]
    expected_total_size = project_params.allowed_file_sizes[0] * len(
        selected_node_s3_keys
    )
    await _assert_compute_path_size(
        storage_rabbitmq_rpc_client,
        location_id,
        user_id,
        path=path,
        expected_total_size=expected_total_size,
        product_name=product_name,
    )

    # get size of workspace in one of the nodes (this is semi-cached in the DB)
    path = Path(project["uuid"]) / f"{selected_node_id}" / "workspace"
    selected_node_s3_keys = [
        Path(s3_object_id)
        for s3_object_id in list_of_files[selected_node_id]
        if s3_object_id.startswith(f"{path}")
    ]
    expected_total_size = project_params.allowed_file_sizes[0] * len(
        selected_node_s3_keys
    )
    workspace_total_size = await _assert_compute_path_size(
        storage_rabbitmq_rpc_client,
        location_id,
        user_id,
        path=path,
        expected_total_size=expected_total_size,
        product_name=product_name,
    )

    # get size of folders inside the workspace
    folders_inside_workspace = [
        p[0]
        for p in _filter_and_group_paths_one_level_deeper(selected_node_s3_keys, path)
        if p[1] is False
    ]
    accumulated_subfolder_size = 0
    for workspace_subfolder in folders_inside_workspace:
        selected_node_s3_keys = [
            Path(s3_object_id)
            for s3_object_id in list_of_files[selected_node_id]
            if s3_object_id.startswith(f"{workspace_subfolder}")
        ]
        expected_total_size = project_params.allowed_file_sizes[0] * len(
            selected_node_s3_keys
        )
        accumulated_subfolder_size += await _assert_compute_path_size(
            storage_rabbitmq_rpc_client,
            location_id,
            user_id,
            path=workspace_subfolder,
            expected_total_size=expected_total_size,
            product_name=product_name,
        )

    assert workspace_total_size == accumulated_subfolder_size


async def test_path_compute_size_inexistent_path(
    mock_celery_app: None,
    initialized_app: FastAPI,
    storage_rabbitmq_rpc_client: RabbitMQRPCClient,
    with_storage_celery_worker: CeleryTaskQueueWorker,
    location_id: LocationID,
    user_id: UserID,
    faker: Faker,
    fake_datcore_tokens: tuple[str, str],
    product_name: ProductName,
):
    await _assert_compute_path_size(
        storage_rabbitmq_rpc_client,
        location_id,
        user_id,
        path=Path(faker.file_path(absolute=False)),
        expected_total_size=0,
        product_name=product_name,
    )
