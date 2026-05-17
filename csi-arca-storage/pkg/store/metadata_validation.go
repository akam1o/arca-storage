// SPDX-License-Identifier: Apache-2.0

package store

import (
	"fmt"
	"path"
	"regexp"
	"strings"
)

var backendRelativePathPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$`)

func validateBackendRelativePath(value, field string, required bool) error {
	if value == "" {
		if required {
			return fmt.Errorf("%s is required", field)
		}
		return nil
	}
	if strings.HasPrefix(value, "/") {
		return fmt.Errorf("%s must be a relative path", field)
	}
	cleaned := path.Clean(value)
	if cleaned == "." {
		return fmt.Errorf("%s must identify a directory below the SVM root", field)
	}
	if cleaned != value {
		return fmt.Errorf("%s must be canonical", field)
	}
	for _, part := range strings.Split(cleaned, "/") {
		if part == "" || part == "." || part == ".." {
			return fmt.Errorf("%s contains invalid path segment %q", field, part)
		}
	}
	if !backendRelativePathPattern.MatchString(cleaned) {
		return fmt.Errorf("%s must contain only alphanumeric, dots, underscores, hyphens, and single slashes", field)
	}
	return nil
}

func validateVolumeInfo(info *VolumeInfo) error {
	if info == nil {
		return fmt.Errorf("volume info is required")
	}
	if err := validateBackendRelativePath(info.Path, "volume path", true); err != nil {
		return err
	}
	if err := validateBackendRelativePath(info.TemporaryCloneSourceVolumePath, "temporary clone source volume path", false); err != nil {
		return err
	}
	return nil
}

func validateSnapshotInfo(info *SnapshotInfo) error {
	if info == nil {
		return fmt.Errorf("snapshot info is required")
	}
	if err := validateBackendRelativePath(info.Path, "snapshot path", true); err != nil {
		return err
	}
	if err := validateBackendRelativePath(info.SourceVolumePath, "snapshot source volume path", false); err != nil {
		return err
	}
	return nil
}
