package arca

import (
	"errors"
	"fmt"
)

var (
	// ErrSVMNotFound indicates the SVM does not exist
	ErrSVMNotFound = errors.New("svm not found")

	// ErrSVMAlreadyExists indicates the SVM already exists
	ErrSVMAlreadyExists = errors.New("svm already exists")

	// ErrNetworkConflict indicates network resource conflict (IP/VLAN collision)
	ErrNetworkConflict = errors.New("network resource conflict")

	// ErrAllPoolsExhausted indicates all IP pools are exhausted
	ErrAllPoolsExhausted = errors.New("all IP pools exhausted")

	// ErrDirectoryNotFound indicates the directory does not exist
	ErrDirectoryNotFound = errors.New("directory not found")

	// ErrDirectoryAlreadyExists indicates the directory already exists
	ErrDirectoryAlreadyExists = errors.New("directory already exists")

	// ErrSnapshotNotFound indicates the snapshot does not exist
	ErrSnapshotNotFound = errors.New("snapshot not found")

	// ErrSnapshotAlreadyExists indicates the snapshot already exists
	ErrSnapshotAlreadyExists = errors.New("snapshot already exists")

	// ErrQuotaNotFound indicates the quota does not exist
	ErrQuotaNotFound = errors.New("quota not found")

	// ErrUnavailable indicates the ARCA service is unavailable
	ErrUnavailable = errors.New("arca service unavailable")

	// ErrInvalidResponse indicates an invalid API response
	ErrInvalidResponse = errors.New("invalid api response")

	// ErrTimeout indicates the request timed out
	ErrTimeout = errors.New("request timeout")
)

// APIError represents an error from the ARCA API
type APIError struct {
	StatusCode int
	Message    string
	Err        error
}

func (e *APIError) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("arca api error (status %d): %s: %v", e.StatusCode, e.Message, e.Err)
	}
	return fmt.Sprintf("arca api error (status %d): %s", e.StatusCode, e.Message)
}

func (e *APIError) Unwrap() error {
	return e.Err
}

// NewAPIError creates a new API error
func NewAPIError(statusCode int, message string, err error) *APIError {
	return &APIError{
		StatusCode: statusCode,
		Message:    message,
		Err:        err,
	}
}

// MapHTTPStatusToError maps HTTP status codes to specific errors
func MapHTTPStatusToError(statusCode int, message string) error {
	switch statusCode {
	case 404:
		// Distinguish between different resource types based on message
		if containsAny(message, "svm", "storage virtual machine") {
			return ErrSVMNotFound
		} else if containsAny(message, "directory", "path") {
			return ErrDirectoryNotFound
		} else if containsAny(message, "snapshot") {
			return ErrSnapshotNotFound
		} else if containsAny(message, "quota") {
			return ErrQuotaNotFound
		}
		return ErrSVMNotFound // Default to SVM not found
	case 409:
		// Distinguish between existence conflicts and network conflicts
		if containsAny(message, "ip", "vlan", "network") {
			return ErrNetworkConflict
		} else if containsAny(message, "directory") {
			return ErrDirectoryAlreadyExists
		} else if containsAny(message, "snapshot") {
			return ErrSnapshotAlreadyExists
		}
		return ErrSVMAlreadyExists // Default to SVM already exists
	case 503:
		return ErrUnavailable
	default:
		return NewAPIError(statusCode, message, nil)
	}
}

// IsNotFoundError checks if an error is a "not found" error
func IsNotFoundError(err error) bool {
	return errors.Is(err, ErrSVMNotFound) ||
		errors.Is(err, ErrDirectoryNotFound) ||
		errors.Is(err, ErrSnapshotNotFound) ||
		errors.Is(err, ErrQuotaNotFound)
}

// IsAlreadyExistsError checks if an error is an "already exists" error
func IsAlreadyExistsError(err error) bool {
	return errors.Is(err, ErrSVMAlreadyExists) ||
		errors.Is(err, ErrDirectoryAlreadyExists) ||
		errors.Is(err, ErrSnapshotAlreadyExists)
}

// containsAny checks if s contains any of the substrings
func containsAny(s string, substrs ...string) bool {
	for _, substr := range substrs {
		if len(s) >= len(substr) {
			for i := 0; i <= len(s)-len(substr); i++ {
				match := true
				for j := 0; j < len(substr); j++ {
					// Case-insensitive comparison
					c1, c2 := s[i+j], substr[j]
					if c1 >= 'A' && c1 <= 'Z' {
						c1 += 'a' - 'A'
					}
					if c2 >= 'A' && c2 <= 'Z' {
						c2 += 'a' - 'A'
					}
					if c1 != c2 {
						match = false
						break
					}
				}
				if match {
					return true
				}
			}
		}
	}
	return false
}
