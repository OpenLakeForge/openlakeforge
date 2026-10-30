from __future__ import annotations

import boto3
from botocore.stub import Stubber
from openlakeforge_domain import CatalogNamespace

from olf import auth, glue
from olf.catalog import CATALOG_KEY, MANAGED_BY_KEY, MANAGED_BY_VALUE, NamespaceState


def make_client() -> tuple[glue.GlueClient, Stubber]:
    boto_client = boto3.client(
        "glue",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )
    stubber = Stubber(boto_client)
    client = glue.GlueClient(glue.GlueConfig(catalog_id="123456789012", region="us-east-1"), _client=boto_client)
    return client, stubber


def test_default_client_uses_the_selected_olf_aws_session(monkeypatch) -> None:  # noqa: ANN001
    calls: list[object] = []
    sdk_client = object()

    class Session:
        def client(self, service: str, **kwargs):  # noqa: ANN003, ANN201
            calls.append((service, kwargs))
            return sdk_client

    monkeypatch.setattr(auth, "aws_session", lambda environ, *, region: calls.append((environ, region)) or Session())

    client = glue.GlueClient(glue.GlueConfig(catalog_id="123456789012", region="eu-west-1"))

    assert client._client is sdk_client
    assert calls[0][1] == "eu-west-1"
    assert calls[1] == ("glue", {"region_name": "eu-west-1"})


def test_list_namespaces_paginates_and_maps_location_uri() -> None:
    client, stubber = make_client()
    stubber.add_response(
        "get_databases",
        {
            "DatabaseList": [{"Name": "sales_silver", "LocationUri": "s3://silver/sales_silver/"}],
            "NextToken": "page2",
        },
        {"CatalogId": "123456789012"},
    )
    stubber.add_response(
        "get_databases",
        {"DatabaseList": [{"Name": "sales_gold", "LocationUri": "s3://gold/sales_gold/"}]},
        {"CatalogId": "123456789012", "NextToken": "page2"},
    )

    with stubber:
        assert client.list_namespaces() == {
            "sales_silver": NamespaceState("s3://silver/sales_silver/"),
            "sales_gold": NamespaceState("s3://gold/sales_gold/"),
        }


def test_list_namespaces_defaults_missing_location_to_empty_string() -> None:
    client, stubber = make_client()
    stubber.add_response(
        "get_databases",
        {"DatabaseList": [{"Name": "no_location"}]},
        {"CatalogId": "123456789012"},
    )

    with stubber:
        assert client.list_namespaces() == {"no_location": NamespaceState("")}


def test_create_namespace_issues_the_expected_database_input() -> None:
    client, stubber = make_client()
    namespace = CatalogNamespace(name="sales_silver", location="s3://silver/sales_silver/")
    stubber.add_response(
        "create_database",
        {},
        {
            "CatalogId": "123456789012",
            "DatabaseInput": {
                "Name": "sales_silver",
                "LocationUri": "s3://silver/sales_silver/",
                "Description": "OpenLakeForge 123456789012 sales_silver Iceberg namespace",
                "Parameters": {MANAGED_BY_KEY: MANAGED_BY_VALUE, CATALOG_KEY: "lakehouse_dev"},
            },
        },
    )

    with stubber:
        client.create_namespace(namespace)

    stubber.assert_no_pending_responses()


def test_create_namespace_tolerates_already_existing() -> None:
    client, stubber = make_client()
    client._databases["sales_silver"] = {}
    namespace = CatalogNamespace(name="sales_silver", location="s3://silver/sales_silver/")
    stubber.add_client_error("create_database", service_error_code="AlreadyExistsException")

    with stubber:
        client.create_namespace(namespace)  # must not raise


def test_update_namespace_location_issues_update_database() -> None:
    client, stubber = make_client()
    client._databases["sales_silver"] = {}
    namespace = CatalogNamespace(name="sales_silver", location="s3://new-silver/sales_silver/")
    stubber.add_response(
        "update_database",
        {},
        {
            "CatalogId": "123456789012",
            "Name": "sales_silver",
            "DatabaseInput": {
                "Name": "sales_silver",
                "LocationUri": "s3://new-silver/sales_silver/",
                "Description": "OpenLakeForge 123456789012 sales_silver Iceberg namespace",
                "Parameters": {MANAGED_BY_KEY: MANAGED_BY_VALUE, CATALOG_KEY: "lakehouse_dev"},
            },
        },
    )

    with stubber:
        client.update_namespace_location(namespace)

    stubber.assert_no_pending_responses()


def test_drop_namespace_deletes_an_empty_database() -> None:
    client, stubber = make_client()
    stubber.add_response(
        "get_tables",
        {"TableList": []},
        {"CatalogId": "123456789012", "DatabaseName": "retired_silver", "MaxResults": 1},
    )
    stubber.add_response(
        "delete_database", {}, {"CatalogId": "123456789012", "Name": "retired_silver"}
    )

    with stubber:
        client.drop_namespace("retired_silver")

    stubber.assert_no_pending_responses()


def test_drop_namespace_removes_table_metadata_before_database() -> None:
    client, stubber = make_client()
    stubber.add_response(
        "get_tables",
        {"TableList": [{"Name": "orders"}]},
        {"CatalogId": "123456789012", "DatabaseName": "sales_silver", "MaxResults": 1},
    )

    stubber.add_response(
        "delete_table", {}, {"CatalogId": "123456789012", "DatabaseName": "sales_silver", "Name": "orders"}
    )
    stubber.add_response("delete_database", {}, {"CatalogId": "123456789012", "Name": "sales_silver"})

    with stubber:
        client.drop_namespace("sales_silver")

    stubber.assert_no_pending_responses()


def test_drop_namespace_tolerates_already_missing() -> None:
    client, stubber = make_client()
    stubber.add_response(
        "get_tables",
        {"TableList": []},
        {"CatalogId": "123456789012", "DatabaseName": "gone", "MaxResults": 1},
    )
    stubber.add_client_error("delete_database", service_error_code="EntityNotFoundException")

    with stubber:
        client.drop_namespace("gone")  # must not raise
