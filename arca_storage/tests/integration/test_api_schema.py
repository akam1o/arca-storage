"""Tests for the generated API schema."""

from arca_storage.api.main import app


def test_openapi_schema_uses_phase_status_values_and_typed_data_envelopes():
    app.openapi_schema = None
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    phase_values = ["Pending", "Creating", "Ready", "Deleting", "Failed"]

    for status_schema in ("SVMStatus", "VolumeStatus", "SnapshotStatus", "ExportStatus"):
        assert schemas[status_schema]["enum"] == phase_values

    assert schemas["VolumeResponse"]["properties"]["data"] == {"$ref": "#/components/schemas/VolumeData"}
    assert schemas["VolumeData"]["properties"]["volume"] == {"$ref": "#/components/schemas/Volume"}
    assert schemas["ExportResponse"]["properties"]["data"] == {"$ref": "#/components/schemas/ExportData"}
    assert schemas["SnapshotResponse"]["properties"]["data"] == {"$ref": "#/components/schemas/SnapshotData"}
    assert schemas["SVMResponse"]["properties"]["data"]["anyOf"] == [
        {"$ref": "#/components/schemas/SVMData"},
        {"$ref": "#/components/schemas/SVM"},
    ]


def test_csi_directory_path_schema_matches_single_component_validation():
    app.openapi_schema = None
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    expected = "CSI volume name within the SVM; nested paths are not supported"

    for request_schema in ("DirectoryCreate", "QuotaSet", "QuotaExpand"):
        assert schemas[request_schema]["properties"]["path"]["description"] == expected

    delete_parameters = schema["paths"]["/v1/directories/{svm_name}"]["delete"]["parameters"]
    get_quota_parameters = schema["paths"]["/v1/quotas/{svm_name}"]["get"]["parameters"]

    assert _parameter_description(delete_parameters, "path") == expected
    assert _parameter_description(get_quota_parameters, "path") == expected


def _parameter_description(parameters, name):
    return next(parameter["description"] for parameter in parameters if parameter["name"] == name)
