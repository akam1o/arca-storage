// SPDX-License-Identifier: Apache-2.0

package store

import (
	"errors"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

// Common store errors
var (
	ErrNotFound      = errors.New("resource not found")
	ErrAlreadyExists = errors.New("resource already exists")
	ErrConflict      = errors.New("resource conflict")
)

// IsNotFound returns true if the error is a "not found" error
func IsNotFound(err error) bool {
	return errors.Is(err, ErrNotFound)
}

// IsAlreadyExists returns true if the error is an "already exists" error
func IsAlreadyExists(err error) bool {
	return errors.Is(err, ErrAlreadyExists)
}

// IsConflict returns true if the error is a "conflict" error
func IsConflict(err error) bool {
	return errors.Is(err, ErrConflict)
}

// MapKubernetesError maps Kubernetes API errors to store errors
func MapKubernetesError(err error, resourceType, resourceID string) error {
	if err == nil {
		return nil
	}

	if apierrors.IsNotFound(err) {
		return fmt.Errorf("%w: %s %s", ErrNotFound, resourceType, resourceID)
	}

	if apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("%w: %s %s", ErrAlreadyExists, resourceType, resourceID)
	}

	if apierrors.IsConflict(err) {
		return fmt.Errorf("%w: %s %s", ErrConflict, resourceType, resourceID)
	}

	// Return the original error for other types (e.g., unavailable, timeout)
	return fmt.Errorf("k8s API error for %s %s: %w", resourceType, resourceID, err)
}
