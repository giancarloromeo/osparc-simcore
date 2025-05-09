# pylint: disable=protected-access
# pylint: disable=redefined-outer-name
# pylint: disable=too-many-arguments
# pylint: disable=unused-argument
# pylint: disable=unused-variable


import httpx
import pytest
import simcore_service_api_server.api.routes.solvers
from pydantic import TypeAdapter
from pytest_mock import MockFixture
from simcore_service_api_server._meta import API_VTAG
from simcore_service_api_server.models.pagination import OnePage
from simcore_service_api_server.models.schemas.solvers import Solver, SolverPort
from starlette import status


@pytest.mark.skip(reason="Still under development. Currently using fake implementation")
async def test_list_solvers(
    client: httpx.AsyncClient,
    mocker: MockFixture,
):
    warn = mocker.patch.object(
        simcore_service_api_server.api.routes.solvers._logger, "warning"
    )

    # list solvers latest releases
    resp = await client.get("/v0/solvers")
    assert resp.status_code == status.HTTP_200_OK

    # No warnings for ValidationError with the fixture
    assert (
        not warn.called
    ), f"No warnings expected in this fixture, got {warn.call_args!s}"

    data = resp.json()
    assert len(data) == 2

    for item in data:
        solver = Solver(**item)
        print(solver.model_dump_json(indent=1, exclude_unset=True))

        # use link to get the same solver
        assert solver.url
        assert solver.url.host == "api.testserver.io"  # cli.base_url
        assert solver.url.path

        # get_solver_latest_version_by_name
        resp0 = await client.get(solver.url.path)
        assert resp0.status_code == status.HTTP_501_NOT_IMPLEMENTED
        assert f"GET solver {solver.id}" in resp0.json()["errors"][0]
        # get_solver
        resp1 = await client.get(f"/v0/solvers/{solver.id}")
        assert resp1.status_code == status.HTTP_501_NOT_IMPLEMENTED
        assert f"GET solver {solver.id}" in resp1.json()["errors"][0]

        # get_solver_latest_version_by_name
        resp2 = await client.get(f"/v0/solvers/{solver.id}/latest")

        assert resp2.status_code == status.HTTP_501_NOT_IMPLEMENTED
        assert f"GET latest {solver.id}" in resp2.json()["errors"][0]


async def test_list_solver_ports(
    mocked_catalog_rpc_api: dict,
    client: httpx.AsyncClient,
    auth: httpx.BasicAuth,
):
    resp = await client.get(
        f"/{API_VTAG}/solvers/simcore/services/comp/itis/sleeper/releases/2.1.4/ports",
        auth=auth,
    )
    assert resp.status_code == status.HTTP_200_OK

    assert resp.json() == {
        "total": 2,
        "items": [
            {
                "key": "input_1",
                "kind": "input",
                "content_schema": {
                    "title": "Sleep interval",
                    "type": "integer",
                    "x_unit": "second",
                    "minimum": 0,
                    "maximum": 5,
                },
            },
            {
                "key": "output_1",
                "kind": "output",
                "content_schema": {
                    "description": "Integer is generated in range [1-9]",
                    "title": "File containing one random integer",
                    "type": "string",
                },
            },
        ],
    }


async def test_list_solvers_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    response = await client.get(f"/{API_VTAG}/solvers", auth=auth)
    assert response.status_code == status.HTTP_200_OK


async def test_list_releases_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    response = await client.get(f"/{API_VTAG}/solvers/releases", auth=auth)
    assert response.status_code == status.HTTP_200_OK


async def test_list_solver_page_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    response = await client.get(f"/{API_VTAG}/solvers/page", auth=auth)
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["items"]) == response.json()["total"]


async def test_list_solver_releases_page_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    solver_key = "simcore/services/comp/itis/sleeper"
    response = await client.get(
        f"/{API_VTAG}/solvers/{solver_key}/releases/page", auth=auth
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["items"]) == response.json()["total"]


async def test_list_solver_releases_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    solver_key = "simcore/services/comp/itis/sleeper"
    response = await client.get(f"/{API_VTAG}/solvers/{solver_key}/releases", auth=auth)
    assert response.status_code == status.HTTP_200_OK


async def test_list_solver_ports_with_mocked_catalog(
    client: httpx.AsyncClient,
    mocked_catalog_rpc_api: dict,
    auth: httpx.BasicAuth,
):
    solver_key = "simcore/services/comp/itis/sleeper"
    solver_version = "3.2.1"
    response = await client.get(
        f"/{API_VTAG}/solvers/{solver_key}/releases/{solver_version}/ports", auth=auth
    )
    assert response.status_code == status.HTTP_200_OK
    assert TypeAdapter(OnePage[SolverPort]).validate_python(response.json())
