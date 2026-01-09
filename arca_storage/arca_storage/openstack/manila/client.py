"""REST API client for ARCA Storage Manila Driver."""

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    # requests is an optional dependency for OpenStack integration
    requests = None
    HTTPAdapter = None
    Retry = None

try:
    from oslo_log import log as logging
    _HAS_OSLO_LOG = True
except ImportError:
    # oslo_log is optional for standalone usage
    import logging
    _HAS_OSLO_LOG = False

from .exceptions import (
    ArcaAPIConnectionError,
    ArcaAPITimeout,
    ArcaAccessRuleError,
    ArcaManilaAPIError,
    ArcaShareAlreadyExists,
    ArcaShareNotFound,
    ArcaSnapshotNotFound,
    ArcaSVMNotFound,
    ArcaSVMAlreadyExists,
    ArcaNetworkConflict,
)

LOG = logging.getLogger(__name__)


class ArcaManilaClient:
    """REST API client for ARCA Storage Manila operations.

    This client provides methods to interact with the ARCA Storage REST API
    for share, snapshot, export, and SVM management operations.
    """

    def __init__(
        self,
        api_endpoint: str,
        timeout: int = 30,
        retry_count: int = 3,
        verify_ssl: bool = True,
        auth_type: Optional[str] = None,
        api_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
    ):
        """Initialize ARCA Storage Manila API client.

        Args:
            api_endpoint: ARCA Storage API URL (e.g., http://192.168.10.5:8080)
            timeout: HTTP request timeout in seconds
            retry_count: Number of retries for failed requests
            verify_ssl: Whether to verify SSL certificates
            auth_type: Authentication type ('token', 'basic', or None)
            api_token: Bearer token for token authentication
            username: Username for basic authentication
            password: Password for basic authentication
            ca_bundle: Path to CA bundle file for SSL verification
            client_cert: Path to client certificate file for mTLS
            client_key: Path to client private key file for mTLS

        Raises:
            ImportError: If requests library is not installed
            ValueError: If authentication configuration is invalid
        """
        if requests is None:
            raise ImportError(
                "requests library is required for ARCA Storage Manila driver. "
                "Install it with: pip install requests"
            )

        self.base_url = api_endpoint.rstrip("/")
        self.timeout = timeout
        self.retry_count = retry_count

        # SSL verification setup
        if ca_bundle:
            self.verify_ssl = ca_bundle  # Use CA bundle path
        else:
            self.verify_ssl = verify_ssl  # Boolean or default system CAs

        # Create session with connection pooling
        self.session = requests.Session()

        # Configure authentication
        if auth_type == "token":
            if not api_token:
                raise ValueError("api_token is required when auth_type='token'")
            self.session.headers.update({"Authorization": f"Bearer {api_token}"})
        elif auth_type == "basic":
            if not username or not password:
                raise ValueError("username and password are required when auth_type='basic'")
            self.session.auth = (username, password)
        elif auth_type and auth_type != "none":
            raise ValueError(f"Invalid auth_type: {auth_type}. Must be 'token', 'basic', or 'none'")

        # Configure mTLS (client certificate)
        if client_cert:
            if client_key:
                self.session.cert = (client_cert, client_key)
            else:
                self.session.cert = client_cert

        # Configure retry strategy
        # Note: Only retry safe methods (GET) to avoid duplicate operations
        if HTTPAdapter and Retry:
            retry_strategy = Retry(
                total=retry_count,
                backoff_factor=1,  # 1s, 2s, 4s...
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET"],  # Only retry idempotent operations
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

    def _extract_resource_id(
        self, path: str, method: str, json_data: Optional[Dict[str, Any]] = None
    ) -> str:
        """Extract resource ID from path intelligently.

        Args:
            path: API path (e.g., /v1/volumes/share-123/clone)
            method: HTTP method
            json_data: Request body (for POST operations)

        Returns:
            Resource ID string

        Examples:
            /v1/volumes/share-123 → share-123
            /v1/volumes/share-123/clone → share-123 (source volume in path)
            /v1/volumes/share-123/qos → share-123
            /v1/snapshots/snap-456 → snap-456
            /v1/svms/svm-name/capacity → svm-name
            POST /v1/volumes with {"name": "share-123"} → share-123 (new resource)

        Note:
            For POST operations, prefers resource ID from path if present (e.g., for
            clone operations). Only uses json_data["name"] for collection-create
            endpoints like POST /v1/volumes (no resource ID in path).
        """
        # Split path and filter out empty segments
        parts = [p for p in path.split("/") if p]

        # Priority 1: Extract from path if resource ID exists
        # Pattern: /v1/volumes/{volume_name}[/action]
        if "volumes" in parts:
            idx = parts.index("volumes")
            if idx + 1 < len(parts):
                resource_part = parts[idx + 1]
                # If it's an action word, not a resource ID, fall through
                if resource_part not in ["volumes", "v1"]:
                    return resource_part

        # Pattern: /v1/snapshots/{snapshot_name}[/action]
        elif "snapshots" in parts:
            idx = parts.index("snapshots")
            if idx + 1 < len(parts):
                resource_part = parts[idx + 1]
                if resource_part not in ["snapshots", "v1"]:
                    return resource_part

        # Pattern: /v1/svms/{svm_name}[/action]
        elif "svms" in parts:
            idx = parts.index("svms")
            if idx + 1 < len(parts):
                resource_part = parts[idx + 1]
                if resource_part not in ["svms", "v1"]:
                    return resource_part

        # Pattern: /v1/exports (no specific ID in path)
        elif "exports" in parts:
            # For collection-create POST operations, use name from body
            if method == "POST" and json_data and "name" in json_data:
                return json_data["name"]
            return "exports"

        # Priority 2: For collection-create POST operations (no resource ID in path),
        # use name from request body
        if method == "POST" and json_data and "name" in json_data:
            return json_data["name"]

        # Fallback: use last non-empty segment
        return parts[-1] if parts else "unknown"

    def _make_request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make HTTP request to ARCA Storage API.

        Args:
            method: HTTP method (GET, POST, DELETE, PATCH)
            path: API path (e.g., /v1/volumes)
            json_data: Request body as JSON
            params: Query parameters

        Returns:
            Response data dictionary (empty dict for 204 No Content)

        Raises:
            ArcaAPIConnectionError: Connection failed
            ArcaAPITimeout: Request timed out
            ArcaManilaAPIError: API returned error
            ArcaShareNotFound: Share not found (404)
            ArcaShareAlreadyExists: Share already exists (409)
        """
        # Build URL preserving any path prefix in base_url
        # urljoin() discards base_url path if path starts with '/', so use string concat
        if path.startswith("/"):
            url = self.base_url + path
        else:
            url = f"{self.base_url}/{path}"

        LOG.debug(
            f"Making {method} request to {path} with params={params}, json_data={json_data}"
        )

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )

            LOG.debug(f"Response status: {response.status_code}")

            # Handle HTTP errors
            if response.status_code >= 400:
                # Try to parse JSON error response
                try:
                    error_data = response.json()
                    # Try FastAPI HTTPException format first ({"detail": "..."})
                    error_msg = error_data.get("detail")
                    # Fall back to standard format ({"error": {"message": "..."}})
                    if not error_msg:
                        error_msg = error_data.get("error", {}).get("message", response.text)
                except Exception:
                    # Non-JSON error response
                    error_msg = response.text
                    error_data = None

                # Map specific status codes to specific exceptions
                if response.status_code == 404:
                    LOG.warning(f"Resource not found: {path}, error: {error_msg}")
                    # Extract resource ID intelligently based on path pattern
                    resource_id = self._extract_resource_id(path, method, json_data)

                    if "/volumes/" in path:
                        raise ArcaShareNotFound(share_id=resource_id)
                    elif "/snapshots/" in path:
                        raise ArcaSnapshotNotFound(snapshot_id=resource_id)
                    elif "/svms/" in path:
                        raise ArcaSVMNotFound(svm_name=resource_id)
                    elif "/exports" in path:
                        # Export not found - also map to ArcaShareNotFound for idempotency
                        raise ArcaShareNotFound(share_id=resource_id)
                    else:
                        # Generic 404 - use error message as details
                        raise ArcaManilaAPIError(details=f"Resource not found: {error_msg}")

                elif response.status_code == 409:
                    LOG.warning(f"Conflict error: {path}, error: {error_msg}")
                    # For conflicts on create, use the name from request body
                    if method == "POST" and json_data and "name" in json_data:
                        resource_id = json_data["name"]
                    else:
                        resource_id = self._extract_resource_id(path, method, json_data)

                    # Differentiate between network conflicts and SVM name conflicts
                    # IMPORTANT: Check IP conflicts FIRST before "already exists" checks
                    # to avoid misclassifying "IP address already exists" as SVM name conflict
                    # Type safety: ensure error_msg is string (FastAPI may return list/dict)
                    if not isinstance(error_msg, str):
                        error_msg = str(error_msg)
                    error_lower = error_msg.lower()
                    # Use more specific patterns to avoid false positives (e.g., "email address")
                    ip_conflict_patterns = [
                        # Exact-ish matches (keep to reduce false positives)
                        "ip address already",
                        "ip already",
                        "address conflict",
                        "ip conflict",
                    ]

                    # Some backends include the IP between "ip address" and "already in use":
                    # e.g. "IP address 192.168.0.10 is already in use"
                    import re

                    ip_in_use_patterns = [
                        r"\bip\b.*\balready\b.*\bin use\b",
                        r"\bip address\b.*\balready\b.*\bin use\b",
                    ]

                    if any(pattern in error_lower for pattern in ip_conflict_patterns) or any(
                        re.search(rx, error_lower) for rx in ip_in_use_patterns
                    ):
                        # IP address conflict (VLAN reuse is allowed, so only check for IP conflicts)
                        raise ArcaNetworkConflict(details=error_msg)
                    elif "already exists" in error_lower and "/svms" in path:
                        # SVM name already exists - use specific SVM exception
                        raise ArcaSVMAlreadyExists(svm_name=resource_id)
                    elif "already exists" in error_lower and ("/volumes" in path or "/shares" in path):
                        # Share/volume already exists
                        raise ArcaShareAlreadyExists(share_id=resource_id)
                    elif "already exists" in error_lower:
                        # Generic resource conflict - fallback to share exception for backward compatibility
                        raise ArcaShareAlreadyExists(share_id=resource_id)
                    else:
                        raise ArcaManilaAPIError(details=f"Conflict: {error_msg}")

                else:
                    LOG.error(f"API error: HTTP {response.status_code}, {error_msg}")
                    raise ArcaManilaAPIError(
                        details=f"HTTP {response.status_code}: {error_msg}"
                    )

            # Handle successful responses
            if response.status_code == 204:  # No Content
                return {}

            # Parse JSON response
            return response.json()

        except requests.exceptions.Timeout as e:
            LOG.error(f"Request timeout after {self.timeout}s: {path}")
            raise ArcaAPITimeout(timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            LOG.error(f"Connection error: {path}, {str(e)}")
            raise ArcaAPIConnectionError(details=str(e))
        except requests.exceptions.RequestException as e:
            LOG.error(f"Request exception: {path}, {str(e)}")
            raise ArcaManilaAPIError(details=str(e))

    # Volume operations (shares stored as volumes)

    def create_volume(
        self, name: str, svm: str, size_gib: int, thin: bool = True, fs_type: str = "xfs"
    ) -> Dict[str, Any]:
        """Create a volume (share).

        Args:
            name: Volume name (e.g., share-{share_id})
            svm: SVM name
            size_gib: Volume size in GiB
            thin: Use thin provisioning
            fs_type: Filesystem type (default: xfs)

        Returns:
            Volume info including export_path

        Raises:
            ArcaShareAlreadyExists: Volume already exists
            ArcaManilaAPIError: API error
        """
        data = {
            "name": name,
            "svm": svm,
            "size_gib": size_gib,
            "thin": thin,
            "fs_type": fs_type,
        }
        try:
            response = self._make_request("POST", "/v1/volumes", json_data=data)
            LOG.info(f"Created volume {name} on SVM {svm}")
            return response.get("data", {}).get("volume", {})
        except ArcaAPITimeout as timeout_exc:
            # Timeout occurred - check if volume was actually created
            LOG.warning(f"Timeout creating volume {name}, checking actual state...")
            try:
                volume = self.get_volume(name, svm)
                LOG.info(f"Volume {name} was created despite timeout")
                return volume
            except ArcaShareNotFound:
                # Volume doesn't exist, re-raise original timeout
                LOG.error(f"Volume {name} was not created after timeout")
                raise timeout_exc

    def delete_volume(self, name: str, svm: str, force: bool = False) -> None:
        """Delete a volume (share).

        Args:
            name: Volume name
            svm: SVM name
            force: Force deletion

        Raises:
            ArcaShareNotFound: Volume not found
            ArcaManilaAPIError: API error
        """
        params = {"svm": svm, "force": str(force).lower()}
        try:
            self._make_request("DELETE", f"/v1/volumes/{name}", params=params)
            LOG.info(f"Deleted volume {name} from SVM {svm}")
        except ArcaAPITimeout:
            # Timeout occurred - check if volume was actually deleted
            LOG.warning(f"Timeout deleting volume {name}, checking actual state...")
            try:
                self.get_volume(name, svm)
                # Volume still exists, re-raise timeout
                LOG.error(f"Volume {name} still exists after timeout")
                raise
            except ArcaShareNotFound:
                # Volume successfully deleted, return normally
                LOG.info(f"Volume {name} was deleted despite timeout")
                pass

    def resize_volume(self, name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
        """Resize a volume (extend share).

        Args:
            name: Volume name
            svm: SVM name
            new_size_gib: New size in GiB

        Returns:
            Updated volume info

        Raises:
            ArcaShareNotFound: Volume not found
            ArcaManilaAPIError: API error
        """
        data = {"svm": svm, "new_size_gib": new_size_gib}
        response = self._make_request("PATCH", f"/v1/volumes/{name}", json_data=data)
        return response.get("data", {}).get("volume", {})

    def get_volume(self, name: str, svm: str) -> Dict[str, Any]:
        """Get volume info.

        Args:
            name: Volume name
            svm: SVM name

        Returns:
            Volume info including export_path

        Raises:
            ArcaShareNotFound: Volume not found
            ArcaManilaAPIError: API error
        """
        params = {"svm": svm, "name": name}
        response = self._make_request("GET", "/v1/volumes", params=params)
        items = response.get("data", {}).get("items", [])
        if not items:
            raise ArcaShareNotFound(share_id=name)
        return items[0]

    # SVM operations

    def get_svm(self, name: str) -> Dict[str, Any]:
        """Get SVM information.

        Args:
            name: SVM name

        Returns:
            SVM info including vip, vlan_id, status

        Raises:
            ArcaSVMNotFound: SVM not found
            ArcaManilaAPIError: API error
        """
        params = {"name": name}
        response = self._make_request("GET", "/v1/svms", params=params)
        items = response.get("data", {}).get("items", [])
        if not items:
            raise ArcaSVMNotFound(svm_name=name)
        return items[0]

    def list_svms(self) -> List[Dict[str, Any]]:
        """List all SVMs.

        Returns:
            List of SVM info dictionaries

        Raises:
            ArcaManilaAPIError: API error
        """
        response = self._make_request("GET", "/v1/svms")
        return response.get("data", {}).get("items", [])

    def create_svm(
        self,
        name: str,
        vlan_id: int,
        ip_cidr: str,
        gateway: Optional[str] = None,
        mtu: int = 1500,
        root_volume_size_gib: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new SVM.

        Args:
            name: SVM name
            vlan_id: VLAN ID (1-4094)
            ip_cidr: IP address with CIDR (e.g., 192.168.10.5/24)
            gateway: Gateway IP (optional, will be inferred if not provided)
            mtu: MTU size (default: 1500)
            root_volume_size_gib: Optional root volume size in GiB

        Returns:
            SVM info including vip, status, created_at

        Raises:
            ArcaShareAlreadyExists: SVM already exists
            ArcaManilaAPIError: API error
        """
        data = {
            "name": name,
            "vlan_id": vlan_id,
            "ip_cidr": ip_cidr,
            "mtu": mtu,
        }
        if gateway:
            data["gateway"] = gateway
        if root_volume_size_gib:
            data["root_volume_size_gib"] = root_volume_size_gib

        response = self._make_request("POST", "/v1/svms", json_data=data)
        LOG.info("Created SVM %s with VLAN %d and IP %s", name, vlan_id, ip_cidr)
        return response.get("data", {}).get("svm", {})

    # Snapshot operations (LVM thin snapshots)

    def create_snapshot(self, name: str, svm: str, volume: str) -> Dict[str, Any]:
        """Create LVM thin snapshot.

        Args:
            name: Snapshot name (e.g., snapshot-{snapshot_id})
            svm: SVM name
            volume: Source volume name (e.g., share-{share_id})

        Returns:
            Snapshot info

        Raises:
            ArcaManilaAPIError: API error
        """
        data = {"name": name, "svm": svm, "volume": volume}
        try:
            response = self._make_request("POST", "/v1/snapshots", json_data=data)
            LOG.info(f"Created snapshot {name} for volume {volume} on SVM {svm}")
            return response.get("data", {}).get("snapshot", {})
        except ArcaAPITimeout as timeout_exc:
            # Timeout occurred - check if snapshot was actually created
            LOG.warning(f"Timeout creating snapshot {name}, checking actual state...")
            try:
                snapshots = self.list_snapshots(svm=svm, volume=volume)
                for snapshot in snapshots:
                    if snapshot.get("name") == name:
                        LOG.info(f"Snapshot {name} was created despite timeout")
                        return snapshot
                # Snapshot doesn't exist, re-raise original timeout
                LOG.error(f"Snapshot {name} was not created after timeout")
                raise timeout_exc
            except Exception as e:
                # If list fails, re-raise original timeout
                LOG.error(f"Failed to check snapshot state after timeout: {e}")
                raise timeout_exc

    def delete_snapshot(self, name: str, svm: str, volume: str) -> None:
        """Delete LVM thin snapshot.

        Args:
            name: Snapshot name
            svm: SVM name
            volume: Volume name

        Raises:
            ArcaSnapshotNotFound: Snapshot not found
            ArcaManilaAPIError: API error
        """
        params = {"svm": svm, "volume": volume}
        try:
            self._make_request("DELETE", f"/v1/snapshots/{name}", params=params)
        except ArcaAPITimeout:
            # Timeout occurred - check if snapshot was actually deleted
            snapshots = self.list_snapshots(svm=svm, volume=volume)
            for snapshot in snapshots:
                if snapshot.get("name") == name:
                    # Snapshot still exists, re-raise timeout
                    raise
            # Snapshot successfully deleted, return normally
            pass

    def list_snapshots(
        self, svm: Optional[str] = None, volume: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List snapshots.

        Args:
            svm: Optional SVM name filter
            volume: Optional volume name filter

        Returns:
            List of snapshot info dictionaries

        Raises:
            ArcaManilaAPIError: API error
        """
        params = {}
        if svm:
            params["svm"] = svm
        if volume:
            params["volume"] = volume

        response = self._make_request("GET", "/v1/snapshots", params=params)
        return response.get("data", {}).get("items", [])

    def clone_volume_from_snapshot(
        self,
        name: str,
        svm: str,
        source_volume: str,
        snapshot_name: str,
        size_gib: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Clone volume from snapshot (writable clone).

        Args:
            name: New volume name (e.g., share-{new_share_id})
            svm: SVM name
            source_volume: Source volume name
            snapshot_name: Snapshot name to clone from
            size_gib: Optional size override (for larger clones)

        Returns:
            New volume info including export_path

        Raises:
            ArcaManilaAPIError: API error
        """
        data = {"name": name, "svm": svm, "snapshot_name": snapshot_name}
        if size_gib is not None:
            data["size_gib"] = size_gib

        response = self._make_request(
            "POST", f"/v1/volumes/{source_volume}/clone", json_data=data
        )
        return response.get("data", {}).get("volume", {})

    # Export/Access rule operations

    def create_export(
        self,
        svm: str,
        volume: str,
        client: str,
        access: str = "rw",
        root_squash: bool = False,
        sec: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create NFS export ACL entry.

        Args:
            svm: SVM name
            volume: Volume name (e.g., share-{share_id})
            client: Client CIDR (e.g., '10.0.0.0/24')
            access: Access mode ('rw' or 'ro')
            root_squash: Enable root squashing
            sec: Security types (default: ['sys'])

        Returns:
            Export info

        Raises:
            ArcaAccessRuleError: Export creation failed
            ArcaManilaAPIError: API error
        """
        if sec is None:
            sec = ["sys"]

        data = {
            "svm": svm,
            "volume": volume,
            "client": client,
            "access": access,
            "root_squash": root_squash,
            "sec": sec,
        }
        response = self._make_request("POST", "/v1/exports", json_data=data)
        return response.get("data", {}).get("export", {})

    def delete_export(self, svm: str, volume: str, client: str) -> None:
        """Delete NFS export ACL entry.

        Args:
            svm: SVM name
            volume: Volume name
            client: Client CIDR

        Raises:
            ArcaManilaAPIError: API error
        """
        params = {"svm": svm, "volume": volume, "client": client}
        self._make_request("DELETE", "/v1/exports", params=params)

    def list_exports(
        self, svm: Optional[str] = None, volume: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List NFS export ACL entries.

        Args:
            svm: Optional SVM name filter
            volume: Optional volume name filter

        Returns:
            List of export info dictionaries

        Raises:
            ArcaManilaAPIError: API error
        """
        params = {}
        if svm:
            params["svm"] = svm
        if volume:
            params["volume"] = volume

        response = self._make_request("GET", "/v1/exports", params=params)
        return response.get("data", {}).get("items", [])

    # QoS operations

    def apply_qos(
        self,
        volume: str,
        svm: str,
        read_iops: Optional[int] = None,
        write_iops: Optional[int] = None,
        read_bps: Optional[int] = None,
        write_bps: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply QoS limits to volume.

        Args:
            volume: Volume name
            svm: SVM name
            read_iops: Read IOPS limit
            write_iops: Write IOPS limit
            read_bps: Read bandwidth limit (bytes/sec)
            write_bps: Write bandwidth limit (bytes/sec)

        Returns:
            QoS info

        Raises:
            ArcaManilaAPIError: API error
        """
        data = {"svm": svm}
        if read_iops is not None:
            data["read_iops"] = read_iops
        if write_iops is not None:
            data["write_iops"] = write_iops
        if read_bps is not None:
            data["read_bps"] = read_bps
        if write_bps is not None:
            data["write_bps"] = write_bps

        response = self._make_request("PATCH", f"/v1/volumes/{volume}/qos", json_data=data)
        return response.get("data", {}).get("qos", {})

    def remove_qos(self, volume: str, svm: str) -> None:
        """Remove QoS limits from volume.

        Args:
            volume: Volume name
            svm: SVM name

        Raises:
            ArcaManilaAPIError: API error
        """
        params = {"svm": svm}
        self._make_request("DELETE", f"/v1/volumes/{volume}/qos", params=params)

    # Capacity operations

    def get_svm_capacity(self, svm: str) -> Dict[str, Any]:
        """Get SVM capacity statistics.

        Args:
            svm: SVM name

        Returns:
            Capacity info: {total_gb, used_gb, free_gb, provisioned_gb}

        Raises:
            ArcaManilaAPIError: API error

        Note:
            This endpoint may need to be added to ARCA API if not present.
        """
        response = self._make_request("GET", f"/v1/svms/{svm}/capacity")
        return response.get("data", {}).get("capacity", {})
