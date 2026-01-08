"""REST API client for ARCA Storage."""

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

from .exceptions import (
    ArcaAPIConnectionError,
    ArcaAPIError,
    ArcaAPITimeout,
    ArcaExportError,
    ArcaSnapshotAlreadyExists,
    ArcaSnapshotNotFound,
    ArcaSVMNotFound,
    ArcaVolumeAlreadyExists,
    ArcaVolumeNotFound,
)


class ArcaStorageClient:
    """REST API client for ARCA Storage.

    This client provides methods to interact with the ARCA Storage REST API
    for volume, SVM, and export management operations.
    """

    def __init__(
        self,
        api_endpoint: str,
        timeout: int = 30,
        retry_count: int = 3,
        verify_ssl: bool = True,
    ):
        """Initialize ARCA Storage API client.

        Args:
            api_endpoint: ARCA Storage API URL (e.g., http://192.168.10.5:8080)
            timeout: HTTP request timeout in seconds
            retry_count: Number of retries for failed requests
            verify_ssl: Whether to verify SSL certificates

        Raises:
            ImportError: If requests library is not installed
        """
        if requests is None:
            raise ImportError(
                "requests library is required for ARCA Storage Cinder driver. "
                "Install it with: pip install requests"
            )

        self.base_url = api_endpoint.rstrip("/")
        self.timeout = timeout
        self.retry_count = retry_count
        self.verify_ssl = verify_ssl

        # Create session with connection pooling
        self.session = requests.Session()

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
            Response data dictionary

        Raises:
            ArcaAPIConnectionError: Connection failed
            ArcaAPITimeout: Request timed out
            ArcaAPIError: API returned error
        """
        url = urljoin(self.base_url, path)

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )

            # Check for HTTP errors
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                    # Try FastAPI HTTPException format first ({"detail": "..."})
                    error_msg = error_data.get("detail")
                    # Fall back to standard format ({"error": {"message": "..."}})
                    if not error_msg:
                        error_msg = error_data.get("error", {}).get("message", response.text)
                except Exception:
                    error_msg = response.text
                    error_data = None

                raise ArcaAPIError(
                    f"API request failed: {error_msg}",
                    status_code=response.status_code,
                    response_data=error_data,
                )

            # Parse response
            if response.status_code == 204:  # No Content
                return {}

            return response.json()

        except requests.exceptions.Timeout as e:
            raise ArcaAPITimeout(f"API request timed out after {self.timeout}s: {e}")
        except requests.exceptions.ConnectionError as e:
            raise ArcaAPIConnectionError(f"Failed to connect to ARCA Storage API: {e}")
        except requests.exceptions.RequestException as e:
            raise ArcaAPIError(f"API request failed: {e}")

    # Volume operations

    def create_volume(
        self,
        name: str,
        svm: str,
        size_gib: int,
        thin: bool = True,
        fs_type: str = "xfs",
    ) -> Dict[str, Any]:
        """Create a volume on ARCA Storage.

        Args:
            name: Volume name
            svm: SVM name
            size_gib: Size in GiB
            thin: Use thin provisioning (default: True)
            fs_type: Filesystem type (default: xfs)

        Returns:
            Volume information dictionary

        Raises:
            ArcaVolumeAlreadyExists: Volume already exists
            ArcaSVMNotFound: SVM not found
            ArcaAPIError: API error
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
            # API returns: {"data": {"volume": {...}}}
            return response.get("data", {}).get("volume", {})
        except ArcaAPIError as e:
            if e.status_code == 409 or "already exists" in e.message.lower():
                raise ArcaVolumeAlreadyExists(f"Volume {name} already exists in SVM {svm}")
            elif e.status_code == 404 or "not found" in e.message.lower():
                raise ArcaSVMNotFound(f"SVM {svm} not found")
            raise

    def delete_volume(self, name: str, svm: str, force: bool = False) -> None:
        """Delete a volume from ARCA Storage.

        Args:
            name: Volume name
            svm: SVM name
            force: Force deletion (default: False)

        Raises:
            ArcaVolumeNotFound: Volume not found
            ArcaAPIError: API error
        """
        params = {"svm": svm}
        if force:
            params["force"] = "true"

        try:
            self._make_request("DELETE", f"/v1/volumes/{name}", params=params)
        except ArcaAPIError as e:
            if e.status_code == 404:
                raise ArcaVolumeNotFound(f"Volume {name} not found in SVM {svm}")
            raise

    def resize_volume(self, name: str, svm: str, new_size_gib: int) -> Dict[str, Any]:
        """Resize a volume.

        Args:
            name: Volume name
            svm: SVM name
            new_size_gib: New size in GiB

        Returns:
            Updated volume information

        Raises:
            ArcaVolumeNotFound: Volume not found
            ArcaAPIError: API error
        """
        data = {"svm": svm, "new_size_gib": new_size_gib}

        try:
            response = self._make_request("PATCH", f"/v1/volumes/{name}", json_data=data)
            # API returns: {"data": {"volume": {...}}}
            return response.get("data", {}).get("volume", {})
        except ArcaAPIError as e:
            if e.status_code == 404:
                raise ArcaVolumeNotFound(f"Volume {name} not found in SVM {svm}")
            raise

    def list_volumes(
        self,
        svm: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List volumes.

        Args:
            svm: Filter by SVM name
            name: Filter by volume name
            limit: Maximum results (default: 100)
            cursor: Pagination cursor

        Returns:
            List of volume information dictionaries
        """
        params = {"limit": limit}
        if svm:
            params["svm"] = svm
        if name:
            params["name"] = name
        if cursor:
            params["cursor"] = cursor

        response = self._make_request("GET", "/v1/volumes", params=params)
        data = response.get("data", {})
        return data.get("items", [])

    def get_volume(self, name: str, svm: str) -> Dict[str, Any]:
        """Get volume information.

        Args:
            name: Volume name
            svm: SVM name

        Returns:
            Volume information dictionary

        Raises:
            ArcaVolumeNotFound: Volume not found
        """
        volumes = self.list_volumes(svm=svm, name=name)
        if not volumes:
            raise ArcaVolumeNotFound(f"Volume {name} not found in SVM {svm}")
        return volumes[0]

    # Export operations

    def create_export(
        self,
        svm: str,
        volume: str,
        client: str,
        access: str = "rw",
        root_squash: bool = True,
        sec: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create an NFS export.

        Args:
            svm: SVM name
            volume: Volume name
            client: Client CIDR (e.g., 10.0.0.0/16)
            access: Access type (rw or ro, default: rw)
            root_squash: Enable root squash (default: True, matches API default)
            sec: Security types (default: ["sys"])

        Returns:
            Export information dictionary

        Raises:
            ArcaExportError: Export creation failed
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

        try:
            response = self._make_request("POST", "/v1/exports", json_data=data)
            # API returns: {"data": {"export": {...}}}
            return response.get("data", {}).get("export", {})
        except ArcaAPIError as e:
            raise ArcaExportError(f"Failed to create export: {e.message}")

    def delete_export(self, svm: str, volume: str, client: str) -> None:
        """Delete an NFS export.

        Args:
            svm: SVM name
            volume: Volume name
            client: Client CIDR

        Raises:
            ArcaExportError: Export deletion failed
        """
        params = {"svm": svm, "volume": volume, "client": client}

        try:
            self._make_request("DELETE", "/v1/exports", params=params)
        except ArcaAPIError as e:
            if e.status_code != 404:  # Ignore if export doesn't exist
                raise ArcaExportError(f"Failed to delete export: {e.message}")

    def list_exports(
        self,
        svm: Optional[str] = None,
        volume: Optional[str] = None,
        client: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List NFS exports.

        Args:
            svm: Filter by SVM name
            volume: Filter by volume name
            client: Filter by client CIDR
            limit: Maximum results (default: 100)
            cursor: Pagination cursor

        Returns:
            List of export information dictionaries
        """
        params = {"limit": limit}
        if svm:
            params["svm"] = svm
        if volume:
            params["volume"] = volume
        if client:
            params["client"] = client
        if cursor:
            params["cursor"] = cursor

        response = self._make_request("GET", "/v1/exports", params=params)
        data = response.get("data", {})
        return data.get("items", [])

    # SVM operations (informational)

    # NOTE: Snapshot operations have been removed
    # The Cinder driver now uses file-based snapshots (cp --sparse=always)
    # instead of calling ARCA Storage API for snapshot management.
    # This simplifies the implementation and aligns with the NFS-based architecture
    # where volumes are sparse files on an XFS filesystem.

    # SVM operations (informational)

    def list_svms(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List SVMs.

        Args:
            name: Filter by SVM name

        Returns:
            List of SVM information dictionaries
        """
        params = {}
        if name:
            params["name"] = name

        response = self._make_request("GET", "/v1/svms", params=params)
        data = response.get("data", {})
        return data.get("items", [])

    def get_svm(self, name: str) -> Dict[str, Any]:
        """Get SVM information.

        Args:
            name: SVM name

        Returns:
            SVM information dictionary

        Raises:
            ArcaSVMNotFound: SVM not found
        """
        svms = self.list_svms(name=name)
        if not svms:
            raise ArcaSVMNotFound(f"SVM {name} not found")
        return svms[0]

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
        """Apply QoS limits to a volume.

        Args:
            volume: Volume name
            svm: SVM name
            read_iops: Read IOPS limit
            write_iops: Write IOPS limit
            read_bps: Read bandwidth limit in bytes/sec
            write_bps: Write bandwidth limit in bytes/sec

        Returns:
            QoS settings dictionary

        Raises:
            ArcaVolumeNotFound: Volume not found
            ArcaAPIError: API error
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

        try:
            response = self._make_request("PATCH", f"/v1/volumes/{volume}/qos", json_data=data)
            # API returns: {"data": {"qos": {...}}}
            return response.get("data", {}).get("qos", {})
        except ArcaAPIError as e:
            if e.status_code == 404:
                raise ArcaVolumeNotFound(f"Volume {volume} not found in SVM {svm}")
            raise

    def remove_qos(self, volume: str, svm: str) -> None:
        """Remove QoS limits from a volume.

        Args:
            volume: Volume name
            svm: SVM name

        Raises:
            ArcaVolumeNotFound: Volume not found
            ArcaAPIError: API error
        """
        params = {"svm": svm}

        try:
            self._make_request("DELETE", f"/v1/volumes/{volume}/qos", params=params)
        except ArcaAPIError as e:
            if e.status_code != 404:  # Ignore if volume doesn't exist
                raise

    def get_qos(self, volume: str, svm: str) -> Dict[str, Any]:
        """Get QoS settings for a volume.

        Args:
            volume: Volume name
            svm: SVM name

        Returns:
            QoS settings dictionary

        Raises:
            ArcaVolumeNotFound: Volume not found
            ArcaAPIError: API error
        """
        params = {"svm": svm}

        try:
            response = self._make_request("GET", f"/v1/volumes/{volume}/qos", params=params)
            # API returns: {"data": {"qos": {...}}}
            return response.get("data", {}).get("qos", {})
        except ArcaAPIError as e:
            if e.status_code == 404:
                raise ArcaVolumeNotFound(f"Volume {volume} not found in SVM {svm}")
            raise

    def close(self):
        """Close the HTTP session."""
        if self.session:
            self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
