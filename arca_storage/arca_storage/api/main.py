"""
FastAPI main application.
"""

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from arca_storage.api.models import (
    ExportCreate,
    ExportListResponse,
    ExportResponse,
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
from arca_storage.api.services import export_service, qos_service, snapshot_service, svm_service, volume_service

app = FastAPI(title="Arca Storage API", description="REST API for Arca Storage SVM management", version="0.1.0")
logger = logging.getLogger(__name__)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler."""
    request_id = str(uuid.uuid4())
    logger.exception("Unhandled error (request_id=%s, path=%s)", request_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "request_id": request_id,
            "status": "error",
            "error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "details": {}},
        },
    )


# SVM endpoints


@app.post("/v1/svms", response_model=SVMResponse, status_code=201)
def create_svm(svm: SVMCreate) -> Dict[str, Any]:
    """
    Create a new SVM.
    """
    request_id = str(uuid.uuid4())
    try:
        result = svm_service.create_svm(svm)
        return {"request_id": request_id, "status": "ok", "data": {"svm": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


@app.delete("/v1/svms/{name}", response_model=SuccessResponse)
def delete_svm(
    name: str,
    force: bool = Query(False, description="Force deletion"),
    delete_volumes: bool = Query(False, description="Delete volumes as well"),
) -> Dict[str, Any]:
    """
    Delete an SVM.
    """
    try:
        request_id = str(uuid.uuid4())
        svm_service.delete_svm(name, force, delete_volumes)
        return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


# Volume endpoints


@app.post("/v1/volumes", response_model=VolumeResponse, status_code=201)
def create_volume(volume: VolumeCreate) -> Dict[str, Any]:
    """
    Create a new volume.
    """
    request_id = str(uuid.uuid4())
    try:
        result = volume_service.create_volume(volume)
        return {"request_id": request_id, "status": "ok", "data": {"volume": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/v1/volumes/{name}", response_model=VolumeResponse)
def resize_volume(name: str, resize: VolumeResize) -> Dict[str, Any]:
    """
    Resize a volume.
    """
    request_id = str(uuid.uuid4())
    try:
        result = volume_service.resize_volume(name, resize.svm, resize.new_size_gib)
        return {"request_id": request_id, "status": "ok", "data": {"volume": result}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/v1/volumes/{name}", response_model=SuccessResponse)
def delete_volume(
    name: str, svm: str = Query(..., description="SVM name"), force: bool = Query(False, description="Force deletion")
) -> Dict[str, Any]:
    """
    Delete a volume.
    """
    request_id = str(uuid.uuid4())
    try:
        volume_service.delete_volume(name, svm, force)
        return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    """
    Add an NFS export.
    """
    request_id = str(uuid.uuid4())
    try:
        result = export_service.add_export(export)
        return {"request_id": request_id, "status": "ok", "data": {"export": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/exports", response_model=SuccessResponse)
def remove_export(
    svm: str = Query(..., description="SVM name"),
    volume: str = Query(..., description="Volume name"),
    client: str = Query(..., description="Client CIDR"),
) -> Dict[str, Any]:
    """
    Remove an NFS export.
    """
    request_id = str(uuid.uuid4())
    try:
        export_service.remove_export(svm, volume, client)
        return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    """
    Create a snapshot of a volume.
    """
    request_id = str(uuid.uuid4())
    try:
        result = snapshot_service.create_snapshot(snapshot)
        return {"request_id": request_id, "status": "ok", "data": {"snapshot": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/v1/snapshots/{name}", response_model=SuccessResponse)
def delete_snapshot(
    name: str,
    svm: str = Query(..., description="SVM name"),
    volume: str = Query(..., description="Volume name"),
    force: bool = Query(False, description="Force deletion"),
) -> Dict[str, Any]:
    """
    Delete a snapshot.
    """
    request_id = str(uuid.uuid4())
    try:
        snapshot_service.delete_snapshot(name, svm, volume, force)
        return {"request_id": request_id, "status": "ok", "data": {"deleted": True}}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    """
    Create a new volume from a snapshot (clone).
    """
    request_id = str(uuid.uuid4())
    try:
        result = snapshot_service.clone_volume_from_snapshot(clone)
        return {"request_id": request_id, "status": "ok", "data": {"volume": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    try:
        result = qos_service.apply_qos_to_volume(
            svm=qos.svm,
            volume=name,
            read_iops=qos.read_iops,
            write_iops=qos.write_iops,
            read_bps=qos.read_bps,
            write_bps=qos.write_bps,
        )
        return {"request_id": request_id, "status": "ok", "data": {"qos": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    try:
        qos_service.remove_qos_from_volume(svm=svm, volume=name)
        return {"request_id": request_id, "status": "ok", "data": {"message": "QoS limits removed"}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    try:
        result = qos_service.get_qos_settings(svm=svm, volume=name)
        return {"request_id": request_id, "status": "ok", "data": {"qos": result}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
