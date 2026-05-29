"""Tests for the generated API schema."""

from arca_storage.api.main import app


def test_openapi_schema_uses_phase_status_values_and_typed_data_envelopes():
    app.openapi_schema = None
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    phase_values = ["Pending", "Creating", "Ready", "Deleting", "Failed"]

    for status_schema in (
        "SVMStatus",
        "VolumeStatus",
        "SnapshotStatus",
        "ExportStatus",
    ):
        assert schemas[status_schema]["enum"] == phase_values

    assert schemas["VolumeResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/VolumeData"
    }
    assert schemas["VolumeData"]["properties"]["volume"] == {
        "$ref": "#/components/schemas/Volume"
    }
    assert schemas["ExportResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/ExportData"
    }
    assert schemas["SnapshotResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/SnapshotData"
    }
    assert schemas["VolumeQoSResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/VolumeQoSData"
    }
    assert schemas["VolumeQoSData"]["properties"]["qos"] == {
        "$ref": "#/components/schemas/VolumeQoS"
    }
    assert schemas["DeletedResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/DeletedData"
    }
    assert schemas["DirectoryResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/DirectoryData"
    }
    assert schemas["DirectoryData"]["properties"]["directory"] == {
        "$ref": "#/components/schemas/Directory"
    }
    assert schemas["QuotaResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/QuotaData"
    }
    assert schemas["QoSRemovalResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/QoSRemovalData"
    }
    assert schemas["SVMCapacityResponse"]["properties"]["data"] == {
        "$ref": "#/components/schemas/SVMCapacityData"
    }
    assert schemas["SVMCapacityData"]["properties"]["capacity"] == {
        "$ref": "#/components/schemas/SVMCapacity"
    }
    assert schemas["SVMResponse"]["properties"]["data"]["anyOf"] == [
        {"$ref": "#/components/schemas/SVMData"},
        {"$ref": "#/components/schemas/SVM"},
    ]


def test_positive_resource_limit_schemas_share_exclusive_minimum_constraints():
    app.openapi_schema = None
    schemas = app.openapi()["components"]["schemas"]

    required_positive_fields = (
        ("QuotaSet", "quota_bytes"),
        ("QuotaExpand", "new_quota_bytes"),
        ("VolumeCreate", "size_gib"),
        ("VolumeResize", "new_size_gib"),
    )
    optional_positive_fields = (
        ("SVMCreate", "root_volume_size_gib"),
        ("DirectoryCreate", "quota_bytes"),
        ("VolumeCloneCreate", "size_gib"),
        ("VolumeQoSApply", "read_iops"),
        ("VolumeQoSApply", "write_iops"),
        ("VolumeQoSApply", "read_bps"),
        ("VolumeQoSApply", "write_bps"),
    )

    for schema_name, field_name in required_positive_fields:
        field = schemas[schema_name]["properties"][field_name]
        assert field["type"] == "integer"
        assert field["exclusiveMinimum"] == 0.0

    for schema_name, field_name in optional_positive_fields:
        field = schemas[schema_name]["properties"][field_name]
        assert field["anyOf"] == [
            {"type": "integer", "exclusiveMinimum": 0.0},
            {"type": "null"},
        ]


def test_csi_directory_path_schema_matches_single_component_validation():
    app.openapi_schema = None
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    expected = "CSI volume name within the SVM; nested paths are not supported"

    for request_schema in ("DirectoryCreate", "QuotaSet", "QuotaExpand"):
        assert schemas[request_schema]["properties"]["path"]["description"] == expected

    delete_parameters = schema["paths"]["/v1/directories/{svm_name}"]["delete"][
        "parameters"
    ]
    get_quota_parameters = schema["paths"]["/v1/quotas/{svm_name}"]["get"]["parameters"]

    assert _parameter_description(delete_parameters, "path") == expected
    assert _parameter_description(get_quota_parameters, "path") == expected


def _parameter_description(parameters, name):
    return next(
        parameter["description"]
        for parameter in parameters
        if parameter["name"] == name
    )


def test_openapi_schema_documents_bearer_auth_for_protected_routes():
    app.openapi_schema = None
    schema = app.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert schema["paths"]["/v1/svms"]["get"]["security"] == [{"BearerAuth": []}]
    assert schema["paths"]["/metrics"]["get"]["security"] == [{"BearerAuth": []}]
