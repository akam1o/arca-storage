"""Utility functions for ARCA Storage Cinder Driver."""

import hashlib
import os
import subprocess
from typing import Optional

from .exceptions import ArcaStorageException


def get_mount_point_for_volume(base_path: str, volume_id: str) -> str:
    """Generate mount point path for a volume.

    Args:
        base_path: Base directory for mounts (e.g., /var/lib/cinder/mnt)
        volume_id: Volume ID

    Returns:
        Full path to mount point
    """
    # Use hash to create shorter directory names
    volume_hash = hashlib.sha256(volume_id.encode()).hexdigest()[:16]
    return os.path.join(base_path, volume_hash)


def get_mount_point_for_svm(base_path: str, svm_name: str) -> str:
    """Generate mount point path for an SVM.

    Args:
        base_path: Base directory for mounts (e.g., /var/lib/cinder/mnt)
        svm_name: SVM name

    Returns:
        Full path to mount point using literal SVM name

    Raises:
        ArcaStorageException: If svm_name contains path traversal characters
    """
    # Sanitize SVM name to prevent path traversal attacks
    # SVM names should only contain alphanumeric, dots, underscores, and hyphens
    import re
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", svm_name):
        raise ArcaStorageException(
            f"Invalid SVM name '{svm_name}': must start with alphanumeric "
            "and contain only alphanumeric, dots, underscores, or hyphens"
        )

    # Use literal SVM name for easy identification
    return os.path.join(base_path, f"svm_{svm_name}")


def ensure_mount_point_exists(mount_point: str) -> None:
    """Ensure mount point directory exists.

    Args:
        mount_point: Path to mount point

    Raises:
        ArcaStorageException: If directory creation fails
    """
    try:
        os.makedirs(mount_point, mode=0o750, exist_ok=True)
    except OSError as e:
        raise ArcaStorageException(f"Failed to create mount point {mount_point}: {e}")


def mount_nfs(export_path: str, mount_point: str, mount_options: str) -> None:
    """Mount NFS export (concurrency-safe, idempotent).

    Args:
        export_path: NFS export path (e.g., 192.168.100.5:/exports/svm1)
        mount_point: Local mount point
        mount_options: Mount options (e.g., rw,noatime,vers=4.1)

    Raises:
        ArcaStorageException: If mount fails
    """
    # Ensure mount point exists
    ensure_mount_point_exists(mount_point)

    # Check if already mounted with the same export
    current_mount = get_nfs_share_info(mount_point)
    if current_mount:
        if current_mount["device"] == export_path:
            # Already mounted correctly - this is the normal case for per-SVM exports
            return
        else:
            raise ArcaStorageException(
                f"Mount point {mount_point} already has different export: {current_mount['device']}"
            )

    # Determine NFS type from mount options
    # Default to nfs4, but allow override via mount options
    nfs_type = "nfs4"
    if "vers=3" in mount_options or "nfsvers=3" in mount_options:
        nfs_type = "nfs"

    # Mount NFS export
    cmd = ["mount", "-t", nfs_type, "-o", mount_options, export_path, mount_point]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise ArcaStorageException(f"Mount operation timed out for {export_path}")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        # Check if "already mounted" error (race condition with another worker)
        if "already mounted" in error_msg.lower() or "busy" in error_msg.lower():
            # Re-check if it's mounted with the correct export
            current_mount = get_nfs_share_info(mount_point)
            if current_mount and current_mount["device"] == export_path:
                # Another worker mounted it - this is OK
                return
        raise ArcaStorageException(f"Failed to mount {export_path}: {error_msg}")


def unmount_nfs(mount_point: str, force: bool = False) -> None:
    """Unmount NFS export.

    Args:
        mount_point: Local mount point
        force: Force unmount (lazy unmount)

    Raises:
        ArcaStorageException: If unmount fails
    """
    if not is_mounted(mount_point):
        return

    # Try normal unmount first
    cmd = ["umount", mount_point]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        if force:
            # Try lazy unmount
            lazy_unmount(mount_point)
        else:
            raise ArcaStorageException(f"Unmount operation timed out for {mount_point}")
    except subprocess.CalledProcessError as e:
        error_msg = (e.stderr or e.stdout or str(e)).lower()
        # Ignore "not mounted" errors
        if "not mounted" in error_msg:
            return

        if force:
            # Try lazy unmount on failure
            try:
                lazy_unmount(mount_point)
            except ArcaStorageException:
                # If lazy unmount also fails, raise the original error
                raise ArcaStorageException(f"Failed to unmount {mount_point}: {error_msg}")
        else:
            raise ArcaStorageException(f"Failed to unmount {mount_point}: {error_msg}")


def lazy_unmount(mount_point: str) -> None:
    """Perform lazy unmount (umount -l).

    Args:
        mount_point: Local mount point

    Raises:
        ArcaStorageException: If lazy unmount fails
    """
    cmd = ["umount", "-l", mount_point]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise ArcaStorageException(f"Lazy unmount operation timed out for {mount_point}")
    except subprocess.CalledProcessError as e:
        error_msg = (e.stderr or e.stdout or str(e)).lower()
        # Ignore "not mounted" errors
        if "not mounted" not in error_msg:
            raise ArcaStorageException(f"Failed to lazy unmount {mount_point}: {error_msg}")


def is_mounted(mount_point: str) -> bool:
    """Check if path is mounted.

    Args:
        mount_point: Path to check

    Returns:
        True if mounted, False otherwise
    """
    try:
        # Read /proc/mounts to check if path is mounted
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mount_point:
                    return True
        return False
    except Exception:
        # Fallback to mountpoint command
        try:
            result = subprocess.run(
                ["mountpoint", "-q", mount_point],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


def cleanup_mount_point(mount_point: str) -> None:
    """Remove mount point directory.

    Args:
        mount_point: Path to mount point
    """
    try:
        if os.path.exists(mount_point) and os.path.isdir(mount_point):
            # Check if empty
            if not os.listdir(mount_point):
                os.rmdir(mount_point)
    except OSError:
        # Ignore errors during cleanup
        pass


def get_nfs_share_info(mount_point: str) -> Optional[dict]:
    """Get information about mounted NFS share.

    Args:
        mount_point: Local mount point

    Returns:
        Dictionary with NFS share info, or None if not mounted
    """
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == mount_point:
                    return {
                        "device": parts[0],  # NFS export path
                        "mount_point": parts[1],
                        "fs_type": parts[2],
                        "options": parts[3],
                    }
        return None
    except Exception:
        return None


def validate_nfs_export_accessible(export_path: str, timeout: int = 10) -> bool:
    """Validate NFS export is accessible (best effort).

    This is an optional check. For NFSv4-only environments where showmount
    may not be available, this check may return False even when the export
    is accessible via mount.

    Args:
        export_path: NFS export path (e.g., 192.168.100.5:/exports/svm1/vol1)
        timeout: Timeout in seconds

    Returns:
        True if accessible via showmount, False otherwise or if showmount unavailable
    """
    try:
        # Split server and path
        if ":" not in export_path:
            return False

        server, _ = export_path.split(":", 1)

        # Try showmount (may not be available in NFSv4-only environments)
        result = subprocess.run(
            ["showmount", "-e", server],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # showmount not available or timed out - not necessarily an error
        return False
    except Exception:
        return False


def get_volume_usage(mount_point: str) -> Optional[dict]:
    """Get disk usage for mounted volume.

    Args:
        mount_point: Local mount point

    Returns:
        Dictionary with usage info, or None if not available
    """
    try:
        import shutil

        usage = shutil.disk_usage(mount_point)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": (usage.used / usage.total * 100) if usage.total > 0 else 0,
        }
    except Exception:
        return None


def create_volume_file(mount_point: str, volume_name: str, size_gb: int) -> str:
    """Create a raw volume file for Cinder (atomic, concurrency-safe).

    This creates a sparse file that will be used as the actual volume backing store.
    Uses atomic file creation to prevent race conditions across multiple workers.

    Args:
        mount_point: NFS mount point
        volume_name: Volume name (used as filename)
        size_gb: Volume size in GB

    Returns:
        Path to created volume file

    Raises:
        ArcaStorageException: If file creation fails
    """
    # Volume file path (use volume_name for compatibility with RemoteFSDriver)
    volume_file = os.path.join(mount_point, volume_name)

    try:
        # Atomic file creation using O_CREAT | O_EXCL to prevent race conditions
        # This will fail if the file already exists (another worker created it)
        fd = os.open(volume_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

        try:
            # Extend file to desired size using ftruncate (sparse file)
            size_bytes = size_gb * 1024 * 1024 * 1024
            os.ftruncate(fd, size_bytes)
        finally:
            os.close(fd)

        return volume_file

    except FileExistsError:
        raise ArcaStorageException(f"Volume file already exists: {volume_file}")
    except OSError as e:
        raise ArcaStorageException(f"Failed to create volume file {volume_file}: {e}")


def delete_volume_file(mount_point: str, volume_name: str) -> None:
    """Delete volume file.

    Args:
        mount_point: NFS mount point
        volume_name: Volume name (filename)
    """
    volume_file = os.path.join(mount_point, volume_name)

    try:
        if os.path.exists(volume_file):
            os.remove(volume_file)
    except OSError as e:
        # Log warning but don't fail
        import logging
        LOG = logging.getLogger(__name__)
        LOG.warning("Failed to delete volume file %s: %s", volume_file, e)


def get_volume_file_path(mount_point: str, volume_name: str) -> str:
    """Get volume file path.

    Args:
        mount_point: NFS mount point
        volume_name: Volume name (filename)

    Returns:
        Path to volume file
    """
    return os.path.join(mount_point, volume_name)


def extend_volume_file(mount_point: str, volume_name: str, new_size_gb: int) -> None:
    """Extend volume file to new size.

    Args:
        mount_point: NFS mount point
        volume_name: Volume name (filename)
        new_size_gb: New size in GB

    Raises:
        ArcaStorageException: If extension fails
    """
    volume_file = os.path.join(mount_point, volume_name)

    if not os.path.exists(volume_file):
        raise ArcaStorageException(f"Volume file does not exist: {volume_file}")

    try:
        # Extend sparse file
        size_bytes = new_size_gb * 1024 * 1024 * 1024
        cmd = ["truncate", "-s", str(size_bytes), volume_file]

        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

    except subprocess.TimeoutExpired:
        raise ArcaStorageException(f"Volume file extension timed out: {volume_file}")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or e.stdout or str(e)
        raise ArcaStorageException(f"Failed to extend volume file: {error_msg}")


def copy_sparse_file(source_path: str, dest_path: str, timeout: int = 600) -> None:
    """Copy a file preserving sparseness using atomic operations.

    Uses cp --sparse=always to copy files while preserving sparse regions.
    The copy is performed atomically by copying to a temporary file first,
    then renaming to the final destination. Uses secure random temp names
    to prevent symlink attacks. Includes fsync for durability.

    Args:
        source_path: Path to source file
        dest_path: Path to destination file
        timeout: Timeout in seconds for copy operation (default: 600)

    Raises:
        ArcaStorageException: If copy fails
    """
    import secrets

    if not os.path.exists(source_path):
        raise ArcaStorageException(f"Source file does not exist: {source_path}")

    # Security: Ensure source is a regular file, not a symlink
    if os.path.islink(source_path) or not os.path.isfile(source_path):
        raise ArcaStorageException(f"Source must be a regular file, not a symlink: {source_path}")

    if os.path.exists(dest_path):
        raise ArcaStorageException(f"Destination file already exists: {dest_path}")

    # Create temporary file with random suffix to prevent prediction
    dest_dir = os.path.dirname(dest_path)
    dest_name = os.path.basename(dest_path)
    random_suffix = secrets.token_hex(8)  # 16 character random hex
    temp_path = os.path.join(dest_dir, f".{dest_name}.tmp.{random_suffix}")

    try:
        # Copy to temporary file with -- to prevent filename attacks
        cmd = ["cp", "--sparse=always", "--", source_path, temp_path]

        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )

        # Set permissions to 0600 (owner read/write only)
        os.chmod(temp_path, 0o600)

        # Sync file data to disk before rename (durability)
        with open(temp_path, "rb") as f:
            os.fsync(f.fileno())

        # Atomic rename to final destination with race condition check
        # Use link() + unlink() pattern to prevent overwriting existing destination
        try:
            # Try to create hard link at destination (fails if dest exists)
            os.link(temp_path, dest_path)
            # If successful, remove the temp file
            os.unlink(temp_path)
        except FileExistsError:
            # Another worker created the destination file concurrently
            raise ArcaStorageException(
                f"Destination file was created by another worker: {dest_path}"
            )
        except OSError as e:
            # If link() failed for reasons other than file exists (e.g., cross-device)
            # Fall back to rename() but re-check destination doesn't exist
            if os.path.exists(dest_path):
                raise ArcaStorageException(
                    f"Destination file already exists (race detected): {dest_path}"
                )
            # Rename is safe here since we just checked
            os.rename(temp_path, dest_path)

        # Sync parent directory to ensure rename/link is durable
        dir_fd = os.open(dest_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except subprocess.TimeoutExpired:
        # Clean up temp file on timeout
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise ArcaStorageException(
            f"File copy timed out after {timeout}s: {source_path} -> {dest_path}"
        )
    except subprocess.CalledProcessError as e:
        # Clean up temp file on copy failure
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        error_msg = e.stderr or e.stdout or str(e)
        raise ArcaStorageException(f"Failed to copy file: {error_msg}")
    except OSError as e:
        # Clean up temp file on any OS error
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise ArcaStorageException(f"Failed during file copy operation: {e}")

