"""
SVM service layer.

Now delegates to the SVM reconciler for idempotent, step-tracked operations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from arca_storage.api.models import SVMCreate
from arca_storage.context import get_context
from arca_storage.errors import InvalidArgumentError, NotFoundError
from arca_storage.models.base import Phase
from arca_storage.models.svm import SVM, SVMSpec
from arca_storage.cli.lib.validators import validate_ip_cidr, validate_ipv4, validate_name, validate_vlan


def create_svm(svm_data: SVMCreate) -> Dict[str, Any]:
    """Create a new SVM via the reconciler."""
    validate_name(svm_data.name)
    validate_vlan(svm_data.vlan_id)
    validate_ip_cidr(svm_data.ip_cidr)
    if svm_data.gateway is not None:
        validate_ipv4(svm_data.gateway)

    ctx = get_context()

    # Check for duplicates
    existing = ctx.db.get_svm(svm_data.name)
    if existing:
        raise InvalidArgumentError(f"SVM '{svm_data.name}' already exists")

    svm = SVM(
        spec=SVMSpec(
            name=svm_data.name,
            vlan_id=svm_data.vlan_id,
            ip_cidr=svm_data.ip_cidr,
            gateway=svm_data.gateway,
            mtu=svm_data.mtu,
            root_volume_size_gib=svm_data.root_volume_size_gib,
        ),
    )

    svm = ctx.svm_reconciler.reconcile(svm)

    if svm.status.phase == Phase.FAILED:
        raise RuntimeError(svm.status.message)

    return _svm_to_dict(svm)


def list_svms(name: Optional[str] = None, limit: int = 100, cursor: Optional[str] = None) -> Dict[str, Any]:
    """List SVMs from the database."""
    ctx = get_context()
    items = ctx.db.list_svms(name=name, limit=limit)
    return {"items": items, "next_cursor": None}


def delete_svm(name: str, force: bool = False, delete_volumes: bool = False) -> None:
    """Delete an SVM via the reconciler."""
    ctx = get_context()
    record = ctx.db.get_svm(name)
    if not record:
        raise NotFoundError("SVM", name)

    svm = SVM(
        metadata=_meta_from_record(record),
        spec=SVMSpec.model_validate(record["spec"]),
        status=_parse_status(record, "svm"),
    )
    svm.status.phase = Phase.DELETING
    ctx.svm_reconciler.reconcile(svm)


def _svm_to_dict(svm: SVM) -> Dict[str, Any]:
    return {
        "name": svm.spec.name,
        "vlan_id": svm.spec.vlan_id,
        "ip_cidr": svm.spec.ip_cidr,
        "gateway": svm.spec.gateway,
        "mtu": svm.spec.mtu,
        "namespace": svm.spec.name,
        "status": svm.status.phase.value,
        "created_at": svm.metadata.created_at,
    }


def _meta_from_record(record: Dict[str, Any]) -> Any:
    from arca_storage.models.base import ResourceMeta
    return ResourceMeta(
        id=record["id"],
        generation=record.get("generation", 1),
    )


def _parse_status(record: Dict[str, Any], kind: str) -> Any:
    from arca_storage.models.svm import SVMStatus
    return SVMStatus.model_validate(record["status"])
