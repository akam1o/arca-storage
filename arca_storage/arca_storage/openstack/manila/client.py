"""REST API client for ARCA Storage Manila Driver."""

import ipaddress
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from arca_storage.openstack.http_errors import redact_sensitive, response_error_message, safe_error_detail

try:
    import requests  # type: ignore[import-untyped]
    from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
    from urllib3.util.retry import Retry
except ImportError:
    # requests is an optional dependency for OpenStack integration
    requests = None
    HTTPAdapter = None
    Retry = None  # type: ignore[assignment,misc]

try:
    from oslo_log import log as logging  # type: ignore[import-untyped]
    _HAS_OSLO_LOG = True
except ImportError:
    # oslo_log is optional for standalone usage
    import logging
    _HAS_OSLO_LOG = False

from .exceptions import (
    ArcaAPIConnectionError,
    ArcaAPITimeout,
    ArcaManilaAPIError,
    ArcaShareAlreadyExists,
    ArcaShareNotFound,
    ArcaSnapshotNotFound,
    ArcaSVMNotFound,
    ArcaSVMAlreadyExists,
    ArcaNetworkConflict,
)

LOG = logging.getLogger(__name__)

_RESOURCE_ID_PATH_SEGMENTS = frozenset({"volumes", "shares", "snapshots", "svms", "exports"})


def _quote_path_segment(value: str) -> str:
    return quote(str(value), safe="")


def _safe_log_path(path: str) -> str:
    """Return an API path shape suitable for logs without resource identifiers."""
    parsed_path = urlparse(path).path
    parts = [p for p in parsed_path.split("/") if p]
    if not parts:
        return "<request>"

    safe_parts: List[str] = []
    redact_next = False
    for part in parts:
        decoded_part = unquote(part)
        if redact_next:
            safe_parts.append("<id>")
            redact_next = False
            continue

        safe_parts.append(safe_error_detail(decoded_part))
        if decoded_part in _RESOURCE_ID_PATH_SEGMENTS:
            redact_next = True

    return "/" + "/".join(safe_parts)


def _is_loopback_hostname(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_token_transport(
    api_endpoint: str,
    auth_type: Optional[str],
    allow_insecure_token_transport: bool = False,
) -> None:
    """Reject bearer tokens over remote plain HTTP unless explicitly allowed."""
    if auth_type != "token" or allow_insecure_token_transport:
        return

    parsed = urlparse(api_endpoint)
    if parsed.scheme.lower() == "http" and not _is_loopback_hostname(parsed.hostname):
        raise ValueError(
            "token authentication over remote plain HTTP requires "
            "allow_insecure_token_transport=True"
        )


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
        ca_bundle: Optional[str] = None,
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
        allow_insecure_token_transport: bool = False,
    ):
        """Initialize ARCA Storage Manila API client.

        Args:
            api_endpoint: ARCA Storage API URL (e.g., http://192.168.10.5:8080)
            timeout: HTTP request timeout in seconds
            retry_count: Number of retries for failed requests
            verify_ssl: Whether to verify SSL certificates
            auth_type: Authentication type ('token', 'none', or None)
            api_token: Bearer token for token authentication
            ca_bundle: Path to CA bundle file for SSL verification
            client_cert: Path to client certificate file for mTLS
            client_key: Path to client private key file for mTLS
            allow_insecure_token_transport: Allow bearer tokens over non-loopback HTTP

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
            self.verify_ssl: Any = ca_bundle  # Use CA bundle path
        else:
            self.verify_ssl = verify_ssl  # Boolean or default system CAs

        # Create session with connection pooling
        self.session = requests.Session()

        # Configure authentication
        if auth_type == "token":
            normalized_token = api_token.strip() if isinstance(api_token, str) else ""
            if not normalized_token:
                raise ValueError("api_token is required when auth_type='token'")
            validate_token_transport(
                api_endpoint,
                auth_type,
                allow_insecure_token_transport,
            )
            self.session.headers.update({"Authorization": f"Bearer {normalized_token}"})
        elif auth_type and auth_type != "none":
            raise ValueError(f"Invalid auth_type: {auth_type}. Must be 'token' or 'none'")

        # Configure mTLS (client certificate)
        if client_key and not client_cert:
            raise ValueError("client_key requires client_cert")
        if client_cert:
            if client_key:
                self.session.cert = (client_cert, client_key)
            else:
                self.session.cert = client_cert

        # Configure retry strategy
        # Note: Only retry safe methods (GET) to avoid duplicate operations
        if HTTPAdapter is not None and Retry is not None:
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
        parts = [p for p in urlparse(path).path.split("/") if p]

        # Priority 1: Extract from path if resource ID exists
        # Pattern: /v1/volumes/{volume_name}[/action]
        if "volumes" in parts:
            idx = parts.index("volumes")
            if idx + 1 < len(parts):
                resource_part = unquote(parts[idx + 1])
                # If it's an action word, not a resource ID, fall through
                if resource_part not in ["volumes", "v1"]:
                    return resource_part

        # Pattern: /v1/snapshots/{snapshot_name}[/action]
        elif "snapshots" in parts:
            idx = parts.index("snapshots")
            if idx + 1 < len(parts):
                resource_part = unquote(parts[idx + 1])
                if resource_part not in ["snapshots", "v1"]:
                    return resource_part

        # Pattern: /v1/svms/{svm_name}[/action]
        elif "svms" in parts:
            idx = parts.index("svms")
            if idx + 1 < len(parts):
                resource_part = unquote(parts[idx + 1])
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

        safe_path = _safe_log_path(path)

        LOG.debug(
            "Making %s request to %s with params=%s, json_data=%s",
            method,
            safe_path,
            redact_sensitive(params),
            redact_sensitive(json_data),
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

            LOG.debug("Response status: %s", response.status_code)

            # Handle HTTP errors
            if response.status_code >= 400:
                error_msg, _error_data = response_error_message(response)

                # Map specific status codes to specific exceptions
                if response.status_code == 404:
                    LOG.warning("Resource not found: %s", safe_path)
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
                    LOG.warning("Conflict error: %s", safe_path)
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
                    LOG.error("API error: HTTP %s for %s", response.status_code, safe_path)
                    raise ArcaManilaAPIError(
                        details=f"HTTP {response.status_code}: {error_msg}"
                    )

            # Handle successful responses
            if response.status_code == 204:  # No Content
                return {}

            # Parse JSON response
            try:
                return response.json()
            except ValueError as e:
                details = safe_error_detail(e)
                raise ArcaManilaAPIError(
                    details=f"Invalid JSON response from ARCA API: {details}"
                ) from e

        except requests.exceptions.Timeout:
            LOG.error("Request timeout after %ss for %s", self.timeout, safe_path)
            raise ArcaAPITimeout(timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            details = safe_error_detail(e)
            LOG.error("Connection error for %s", safe_path)
            raise ArcaAPIConnectionError(details=details)
        except requests.exceptions.RequestException as e:
            details = safe_error_detail(e)
            LOG.error("Request exception for %s", safe_path)
            raise ArcaManilaAPIError(details=details)

    def _next_page_cursor(
        self,
        next_cursor: Optional[str],
        seen_cursors: set[str],
        resource: str,
    ) -> Optional[str]:
        """Return the next page cursor, rejecting cursor cycles."""
        if not next_cursor:
            return None
        if next_cursor in seen_cursors:
            raise ArcaManilaAPIError(
                details=f"Repeated {resource} pagination cursor from ARCA API: {next_cursor}"
            )
        seen_cursors.add(next_cursor)
        return next_cursor

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
            LOG.info("Created volume through ARCA API")
            return response.get("data", {}).get("volume", {})
        except ArcaAPITimeout as timeout_exc:
            # Timeout occurred - check if volume was actually created
            LOG.warning("Timeout creating volume; checking actual state")
            try:
                volume = self.get_volume(name, svm)
                LOG.info("Volume was created despite timeout")
                return volume
            except ArcaShareNotFound:
                # Volume doesn't exist, re-raise original timeout
                LOG.error("Volume was not created after timeout")
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
            self._make_request("DELETE", f"/v1/volumes/{_quote_path_segment(name)}", params=params)
            LOG.info("Deleted volume through ARCA API")
        except ArcaAPITimeout:
            # Timeout occurred - check if volume was actually deleted
            LOG.warning("Timeout deleting volume; checking actual state")
            try:
                self.get_volume(name, svm)
                # Volume still exists, re-raise timeout
                LOG.error("Volume still exists after timeout")
                raise
            except ArcaShareNotFound:
                # Volume successfully deleted, return normally
                LOG.info("Volume was deleted despite timeout")
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
        response = self._make_request("PATCH", f"/v1/volumes/{_quote_path_segment(name)}", json_data=data)
        return response.get("data", {}).get("volume", {})

    def list_volumes(
        self,
        svm: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List volumes.

        Args:
            svm: Optional SVM name filter
            name: Optional volume name filter

        Returns:
            List of volume info dictionaries
        """
        items: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params: Dict[str, Any] = {"limit": 200}
            if svm:
                params["svm"] = svm
            if name:
                params["name"] = name
            if cursor:
                params["cursor"] = cursor

            response = self._make_request("GET", "/v1/volumes", params=params)
            data = response.get("data", {})
            items.extend(data.get("items", []))
            cursor = self._next_page_cursor(
                data.get("next_cursor"), seen_cursors, "volume"
            )
            if not cursor:
                return items

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
        items = self.list_volumes(svm=svm, name=name)
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
        items: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params: Dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            response = self._make_request("GET", "/v1/svms", params=params)
            data = response.get("data", {})
            items.extend(data.get("items", []))
            cursor = self._next_page_cursor(
                data.get("next_cursor"), seen_cursors, "SVM"
            )
            if not cursor:
                return items

    def create_svm(
        self,
        name: str,
        vlan_id: Optional[int],
        ip_cidr: str,
        gateway: Optional[str] = None,
        mtu: int = 1500,
        root_volume_size_gib: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new SVM.

        Args:
            name: SVM name
            vlan_id: Optional VLAN ID (1-4094)
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
            "ip_cidr": ip_cidr,
            "mtu": mtu,
        }
        if vlan_id is not None:
            data["vlan_id"] = vlan_id
        if gateway:
            data["gateway"] = gateway
        if root_volume_size_gib:
            data["root_volume_size_gib"] = root_volume_size_gib

        response = self._make_request("POST", "/v1/svms", json_data=data)
        LOG.info("Created SVM through ARCA API")
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
            LOG.info("Created snapshot through ARCA API")
            return response.get("data", {}).get("snapshot", {})
        except ArcaAPITimeout as timeout_exc:
            # Timeout occurred - check if snapshot was actually created
            LOG.warning("Timeout creating snapshot; checking actual state")
            try:
                snapshots = self.list_snapshots(svm=svm, volume=volume)
                for snapshot in snapshots:
                    if snapshot.get("name") == name:
                        LOG.info("Snapshot was created despite timeout")
                        return snapshot
                # Snapshot doesn't exist, re-raise original timeout
                LOG.error("Snapshot was not created after timeout")
                raise timeout_exc
            except Exception:
                # If list fails, re-raise original timeout
                LOG.error("Failed to check snapshot state after timeout")
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
            self._make_request("DELETE", f"/v1/snapshots/{_quote_path_segment(name)}", params=params)
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
        items: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params: Dict[str, Any] = {"limit": 200}
            if svm:
                params["svm"] = svm
            if volume:
                params["volume"] = volume
            if cursor:
                params["cursor"] = cursor

            response = self._make_request("GET", "/v1/snapshots", params=params)
            data = response.get("data", {})
            items.extend(data.get("items", []))
            cursor = self._next_page_cursor(
                data.get("next_cursor"), seen_cursors, "snapshot"
            )
            if not cursor:
                return items

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
        data: Dict[str, Any] = {"name": name, "svm": svm, "snapshot": snapshot_name}
        if size_gib is not None:
            data["size_gib"] = size_gib

        response = self._make_request(
            "POST", f"/v1/volumes/{_quote_path_segment(source_volume)}/clone", json_data=data
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
        items: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params: Dict[str, Any] = {"limit": 200}
            if svm:
                params["svm"] = svm
            if volume:
                params["volume"] = volume
            if cursor:
                params["cursor"] = cursor

            response = self._make_request("GET", "/v1/exports", params=params)
            data = response.get("data", {})
            items.extend(data.get("items", []))
            cursor = self._next_page_cursor(
                data.get("next_cursor"), seen_cursors, "export"
            )
            if not cursor:
                return items

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
        data: Dict[str, Any] = {"svm": svm}
        if read_iops is not None:
            data["read_iops"] = read_iops
        if write_iops is not None:
            data["write_iops"] = write_iops
        if read_bps is not None:
            data["read_bps"] = read_bps
        if write_bps is not None:
            data["write_bps"] = write_bps

        response = self._make_request("PATCH", f"/v1/volumes/{_quote_path_segment(volume)}/qos", json_data=data)
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
        self._make_request("DELETE", f"/v1/volumes/{_quote_path_segment(volume)}/qos", params=params)

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
        response = self._make_request("GET", f"/v1/svms/{_quote_path_segment(svm)}/capacity")
        return response.get("data", {}).get("capacity", {})
