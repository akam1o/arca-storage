"""
FastAPI main application.
"""

import logging
import re
import secrets
import traceback
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from arca_storage.api.models import (
    CSI_VOLUME_PATH_DESCRIPTION,
    DirectoryCreate,
    ExportCreate,
    ExportListResponse,
    ExportResponse,
    QuotaExpand,
    QuotaSet,
    SnapshotCreate,
    SnapshotListResponse,
    SnapshotResponse,
    SuccessResponse,
    SVMCreate,
    SVMListResponse,
    SVMResponse,
    VolumeCloneCreate,
    VolumeCreate,
    VolumeListResponse,
    VolumeQoSApply,
    VolumeQoSResponse,
    VolumeResize,
    VolumeResponse,
)
from arca_storage.api.services import directory_service, export_service, qos_service, snapshot_service, svm_service, volume_service
from arca_storage.api.auth import (
    API_TOKEN_REQUIRED_MESSAGE,
    configured_api_token,
    non_loopback_request_server_host,
    unauthenticated_loopback_allowed,
)
from arca_storage.context import get_context
from arca_storage.errors import ArcaError, InvalidArgumentError

app = FastAPI(title="Arca Storage API", description="REST API for Arca Storage SVM management", version="0.1.0")
logger = logging.getLogger(__name__)
_VALIDATION_INPUT_PART_RE = re.compile(r"[^\s/,:;=]+")
_VALUE_ERROR_QUOTED_TEXT_RE = re.compile(r"(['\"])[^'\"]+\1")
_SENSITIVE_KEY_PARTS = ("authorization", "token", "password", "secret", "client_key")
_SENSITIVE_BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+([^\s,;]+)")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?token|auth[_-]?token|token|password|secret|client[_-]?key)=([^\s,;]+)"
)


def _request_log_path(request: Request) -> str:
    """Return a route template for logs without resource identifiers."""
    route_path = getattr(request.scope.get("route"), "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return "/<unmatched>"


@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    """Require a bearer token when ARCA_API_TOKEN/ARCA_AUTH_TOKEN is configured."""
    token = configured_api_token()
    if not token:
        host = non_loopback_request_server_host(request.scope)
        if host is None and unauthenticated_loopback_allowed():
            return await call_next(request)
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=503,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": "AUTH_TOKEN_REQUIRED",
                    "message": API_TOKEN_REQUIRED_MESSAGE,
                    "details": {"host": host or "loopback"},
                },
            },
        )

    auth_header = request.headers.get("authorization", "")
    scheme, _, supplied = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, token):
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=401,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {"code": "UNAUTHORIZED", "message": "Unauthorized", "details": {}},
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)


@app.exception_handler(ArcaError)
async def arca_error_handler(request: Request, exc: ArcaError) -> JSONResponse:
    """Return structured error responses for all ArcaError subtypes."""
    request_id = str(uuid.uuid4())
    error = _redact_arca_error(exc)
    logger.warning(
        "ArcaError (request_id=%s, path=%s, code=%s): %s",
        request_id,
        _request_log_path(request),
        exc.code.value,
        error["message"],
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "request_id": request_id,
            "status": "error",
            "error": error,
        },
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Return client errors for validation failures raised below FastAPI."""
    return await arca_error_handler(request, InvalidArgumentError(_redact_value_error_message(str(exc))))


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return structured client errors for request parsing and model validation failures."""
    return await arca_error_handler(
        request,
        InvalidArgumentError(
            "Request validation failed",
            {"errors": _request_validation_errors_without_inputs(exc)},
        ),
    )


def _request_validation_errors_without_inputs(exc: RequestValidationError) -> list[Dict[str, Any]]:
    errors: list[Dict[str, Any]] = []
    for error in exc.errors():
        input_values = _validation_input_strings(error.get("input"))
        sanitized = {
            key: _redact_validation_error_field(key, value, input_values)
            for key, value in error.items()
            if key != "input"
        }
        errors.append(jsonable_encoder(sanitized))
    return errors


def _redact_arca_error(exc: ArcaError) -> dict[str, Any]:
    return {
        "code": exc.code.value,
        "message": _redact_sensitive_text(exc.message),
        "details": _redact_sensitive_value(exc.details),
    }


def _redact_sensitive_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_sensitive_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(message: str) -> str:
    message = _SENSITIVE_BEARER_RE.sub(r"\1 <redacted>", message)
    return _SENSITIVE_ASSIGNMENT_RE.sub(r"\1=<redacted>", message)


def _redact_value_error_message(message: str) -> str:
    message = _redact_sensitive_text(message)
    return _VALUE_ERROR_QUOTED_TEXT_RE.sub(lambda match: f"{match.group(1)}<redacted>{match.group(1)}", message)


def _validation_input_strings(value: Any) -> set[str]:
    if isinstance(value, str):
        stripped = value.strip()
        candidates = {candidate for candidate in (value, stripped) if candidate}
        candidates.update(part for part in _VALIDATION_INPUT_PART_RE.findall(stripped) if len(part) >= 4)
        return candidates
    if isinstance(value, list):
        values: set[str] = set()
        for item in value:
            values.update(_validation_input_strings(item))
        return values
    if isinstance(value, dict):
        values = set()
        for item in value.values():
            values.update(_validation_input_strings(item))
        return values
    return set()


def _redact_validation_error_field(key: str, value: Any, input_values: set[str]) -> Any:
    if key not in {"msg", "ctx"}:
        return value
    return _redact_validation_input_strings(value, input_values)


def _redact_validation_input_strings(value: Any, input_values: set[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for input_value in sorted(input_values, key=len, reverse=True):
            redacted = redacted.replace(input_value, "<redacted>")
        return redacted
    if isinstance(value, list):
        return [_redact_validation_input_strings(item, input_values) for item in value]
    if isinstance(value, dict):
        return {key: _redact_validation_input_strings(item, input_values) for key, item in value.items()}
    return value


def _redacted_exception_traceback(exc: Exception) -> str:
    return _redact_sensitive_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global fallback exception handler."""
    request_id = str(uuid.uuid4())
    logger.error(
        "Unhandled error (request_id=%s, path=%s, type=%s): %s\n%s",
        request_id,
        _request_log_path(request),
        type(exc).__name__,
        _redact_sensitive_text(str(exc)),
        _redacted_exception_traceback(exc),
    )
    return JSONResponse(
        status_code=500,
        content={
            "request_id": request_id,
            "status": "error",
            "error": {"code": "INTERNAL", "message": "Internal server error", "details": {}},
        },
    )


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    return {"request_id": request_id, "status": "ok", "data": {"state": "live"}}


@app.get("/readyz")
def readyz() -> JSONResponse:
    request_id = str(uuid.uuid4())
    checks = {"db": "ok"}
    try:
        ctx = get_context()
        ctx.db.list_svms(limit=1)
    except Exception as e:
        checks["db"] = "error"
        logger.warning("Readiness check failed (request_id=%s, check=db): %s", request_id, e)
        return JSONResponse(
            status_code=503,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": "UNAVAILABLE",
                    "message": "Readiness check failed",
                    "details": {"checks": checks},
                },
            },
        )
    return JSONResponse(
        status_code=200,
        content={"request_id": request_id, "status": "ok", "data": {"checks": checks}},
    )


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        "# HELP arca_storage_api_up Arca Storage API process liveness\n"
        "# TYPE arca_storage_api_up gauge\n"
        "arca_storage_api_up 1\n"
    )


# SVM endpoints


@app.post("/v1/svms", response_model=SVMResponse, status_code=201)
def create_svm(svm: SVMCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = svm_service.create_svm(svm)
    return {"request_id": request_id, "status": "ok", "data": {"svm": result}}


@app.get("/v1/svms", response_model=SVMListResponse)
def list_svms(
    name: Optional[str] = Query(None, description="Filter by SVM name"),
    limit: int = Query(100, ge=1, le=200, description="Maximum number of results"),
    cursor: Optional[str] = Query(None, description="Pagination cursor"),
) -> Dict[str, Any]:
    """
    List all SVMs.
    """
    request_id = str(uuid.uuid4())
    result = svm_service.list_svms(name, limit, cursor)
    return {
        "request_id": request_id,
        "status": "ok",
        "data": {"items": result["items"], "next_cursor": result.get("next_cursor")},
    }


@app.get("/v1/svms/{name}", response_model=SVMResponse)
def get_svm(name: str) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = svm_service.get_svm(name)
    return {"request_id": request_id, "status": "ok", "data": result}


@app.get("/v1/svms/{name}/capacity", response_model=SuccessResponse)
def get_svm_capacity(name: str) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = svm_service.get_svm_capacity(name)
    return {"request_id": request_id, "status": "ok", "data": {"capacity": result}}


@app.delete("/v1/svms/{name}", response_model=SuccessResponse)
def delete_svm(
    name: str,
    force: bool = Query(False, description="Force deletion"),
    delete_volumes: bool = Query(False, description="Delete volumes as well"),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    svm_service.delete_svm(name, force, delete_volumes)
    return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}


# CSI directory/quota compatibility endpoints


@app.post("/v1/directories", response_model=SuccessResponse, status_code=201)
def create_directory(directory: DirectoryCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = directory_service.create_directory(directory)
    return {"request_id": request_id, "status": "ok", "data": {"directory": result}}


@app.delete("/v1/directories/{svm_name}", response_model=SuccessResponse)
def delete_directory(
    svm_name: str,
    path: str = Query(..., description=CSI_VOLUME_PATH_DESCRIPTION),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    directory_service.delete_directory(svm_name, path)
    return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}


@app.post("/v1/quotas", response_model=SuccessResponse)
def set_quota(quota: QuotaSet) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = directory_service.set_quota(quota)
    return {"request_id": request_id, "status": "ok", "data": result}


@app.patch("/v1/quotas", response_model=SuccessResponse)
def expand_quota(quota: QuotaExpand) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = directory_service.expand_quota(quota)
    return {"request_id": request_id, "status": "ok", "data": result}


@app.get("/v1/quotas/{svm_name}", response_model=SuccessResponse)
def get_quota(
    svm_name: str,
    path: str = Query(..., description=CSI_VOLUME_PATH_DESCRIPTION),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = directory_service.get_quota(svm_name, path)
    return {"request_id": request_id, "status": "ok", "data": result}


# Volume endpoints


@app.post("/v1/volumes", response_model=VolumeResponse, status_code=201)
def create_volume(volume: VolumeCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = volume_service.create_volume(volume)
    return {"request_id": request_id, "status": "ok", "data": {"volume": result}}


@app.patch("/v1/volumes/{name}", response_model=VolumeResponse)
def resize_volume(name: str, resize: VolumeResize) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = volume_service.resize_volume(name, resize.svm, resize.new_size_gib)
    return {"request_id": request_id, "status": "ok", "data": {"volume": result}}


@app.delete("/v1/volumes/{name}", response_model=SuccessResponse)
def delete_volume(
    name: str, svm: str = Query(..., description="SVM name"), force: bool = Query(False, description="Force deletion")
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    volume_service.delete_volume(name, svm, force)
    return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}


@app.get("/v1/volumes", response_model=VolumeListResponse)
def list_volumes(
    svm: Optional[str] = Query(None, description="Filter by SVM name"),
    name: Optional[str] = Query(None, description="Filter by volume name"),
    limit: int = Query(100, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    List all volumes.
    """
    request_id = str(uuid.uuid4())
    result = volume_service.list_volumes(svm, name, limit, cursor)
    return {
        "request_id": request_id,
        "status": "ok",
        "data": {"items": result["items"], "next_cursor": result.get("next_cursor")},
    }


# Export endpoints


@app.post("/v1/exports", response_model=ExportResponse, status_code=201)
def add_export(export: ExportCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = export_service.add_export(export)
    return {"request_id": request_id, "status": "ok", "data": {"export": result}}


@app.delete("/v1/exports", response_model=SuccessResponse)
def remove_export(
    svm: str = Query(..., description="SVM name"),
    volume: str = Query(..., description="Volume name"),
    client: str = Query(..., description="Client CIDR"),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    export_service.remove_export(svm, volume, client)
    return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}


@app.get("/v1/exports", response_model=ExportListResponse)
def list_exports(
    svm: Optional[str] = Query(None, description="Filter by SVM name"),
    volume: Optional[str] = Query(None, description="Filter by volume name"),
    client: Optional[str] = Query(None, description="Filter by client CIDR"),
    limit: int = Query(100, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    List all exports.
    """
    request_id = str(uuid.uuid4())
    result = export_service.list_exports(svm, volume, client, limit, cursor)
    return {
        "request_id": request_id,
        "status": "ok",
        "data": {"items": result["items"], "next_cursor": result.get("next_cursor")},
    }


# Snapshot endpoints


@app.post("/v1/snapshots", response_model=SnapshotResponse, status_code=201)
def create_snapshot(snapshot: SnapshotCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = snapshot_service.create_snapshot(snapshot)
    return {"request_id": request_id, "status": "ok", "data": {"snapshot": result}}


@app.delete("/v1/snapshots/{name}", response_model=SuccessResponse)
def delete_snapshot(
    name: str,
    svm: str = Query(..., description="SVM name"),
    volume: str = Query(..., description="Volume name"),
    force: bool = Query(False, description="Force deletion"),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    snapshot_service.delete_snapshot(name, svm, volume, force)
    return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}


@app.get("/v1/snapshots", response_model=SnapshotListResponse)
def list_snapshots(
    svm: Optional[str] = Query(None, description="Filter by SVM name"),
    volume: Optional[str] = Query(None, description="Filter by volume name"),
    name: Optional[str] = Query(None, description="Filter by snapshot name"),
    limit: int = Query(100, ge=1, le=200),
    cursor: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    List all snapshots.
    """
    request_id = str(uuid.uuid4())
    result = snapshot_service.list_snapshots(svm, volume, name, limit, cursor)
    return {
        "request_id": request_id,
        "status": "ok",
        "data": {"items": result["items"], "next_cursor": result.get("next_cursor")},
    }


@app.post("/v1/volumes/{name}/clone", response_model=VolumeResponse, status_code=201)
def clone_volume_from_snapshot(name: str, clone: VolumeCloneCreate) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    result = snapshot_service.clone_volume_from_snapshot(name, clone)
    return {"request_id": request_id, "status": "ok", "data": {"volume": result}}


# QoS endpoints


@app.patch("/v1/volumes/{name}/qos", response_model=VolumeQoSResponse)
def apply_qos_to_volume(name: str, qos: VolumeQoSApply) -> Dict[str, Any]:
    """
    Apply QoS limits to a volume.

    This endpoint allows setting IOPS and bandwidth limits on a volume using cgroups v2 I/O Controller.

    - **read_iops**: Read IOPS limit
    - **write_iops**: Write IOPS limit
    - **read_bps**: Read bandwidth limit in bytes/sec
    - **write_bps**: Write bandwidth limit in bytes/sec

    Example:
    ```json
    {
        "svm": "production_svm",
        "read_iops": 5000,
        "write_iops": 5000,
        "read_bps": 524288000,
        "write_bps": 524288000
    }
    ```
    """
    request_id = str(uuid.uuid4())
    result = qos_service.apply_qos_to_volume(
        svm=qos.svm,
        volume=name,
        read_iops=qos.read_iops,
        write_iops=qos.write_iops,
        read_bps=qos.read_bps,
        write_bps=qos.write_bps,
    )
    return {"request_id": request_id, "status": "ok", "data": {"qos": result}}


@app.delete("/v1/volumes/{name}/qos", response_model=SuccessResponse)
def remove_qos_from_volume(
    name: str,
    svm: str = Query(..., description="SVM name"),
) -> Dict[str, Any]:
    """
    Remove QoS limits from a volume.

    This resets all I/O limits to unlimited (max).
    """
    request_id = str(uuid.uuid4())
    qos_service.remove_qos_from_volume(svm=svm, volume=name)
    return {"request_id": request_id, "status": "ok", "data": {"message": "QoS limits removed"}}


@app.get("/v1/volumes/{name}/qos", response_model=VolumeQoSResponse)
def get_qos_settings(
    name: str,
    svm: str = Query(..., description="SVM name"),
) -> Dict[str, Any]:
    """
    Get current QoS settings for a volume.

    Returns the current I/O limits (IOPS and bandwidth) applied to the volume.
    """
    request_id = str(uuid.uuid4())
    result = qos_service.get_qos_settings(svm=svm, volume=name)
    return {"request_id": request_id, "status": "ok", "data": {"qos": result}}
