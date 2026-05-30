package arca

import (
	"errors"
	"fmt"
	"regexp"
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

	// ErrVolumeNotFound indicates the volume does not exist
	ErrVolumeNotFound = errors.New("volume not found")

	// ErrVolumeAlreadyExists indicates the volume already exists
	ErrVolumeAlreadyExists = errors.New("volume already exists")

	// ErrSnapshotNotFound indicates the snapshot does not exist
	ErrSnapshotNotFound = errors.New("snapshot not found")

	// ErrSnapshotAlreadyExists indicates the snapshot already exists
	ErrSnapshotAlreadyExists = errors.New("snapshot already exists")

	// ErrExportNotFound indicates the export does not exist
	ErrExportNotFound = errors.New("export not found")

	// ErrExportAlreadyExists indicates the export already exists
	ErrExportAlreadyExists = errors.New("export already exists")

	// ErrQuotaNotFound indicates the quota does not exist
	ErrQuotaNotFound = errors.New("quota not found")

	// ErrUnavailable indicates the ARCA service is unavailable
	ErrUnavailable = errors.New("arca service unavailable")

	// ErrInvalidResponse indicates an invalid API response
	ErrInvalidResponse = errors.New("invalid api response")

	// ErrTimeout indicates the request timed out
	ErrTimeout = errors.New("request timeout")
)

const redactedSecret = "<redacted>"

var (
	authorizationSecretPattern = regexp.MustCompile(`(?i)\b(authorization)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\r\n,;&}\]]+)`)
	bearerSecretPattern        = regexp.MustCompile(`(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+`)
	keyValueSecretPattern      = regexp.MustCompile(`(?i)\b(api[_-]?token|auth[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|client[_-]?key|private[_-]?key|password|passwd|secret|credential|credentials|token)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}\]]+)`)
)

// APIError represents an error from the ARCA API
type APIError struct {
	StatusCode int
	Code       string // Structured error code (e.g., "NOT_FOUND", "ALREADY_EXISTS")
	Message    string
	Err        error
}

func (e *APIError) Error() string {
	message := redactSensitive(e.Message)
	if e.Err != nil {
		return fmt.Sprintf("arca api error (status %d, code=%s): %s: %s", e.StatusCode, e.Code, message, redactSensitive(e.Err.Error()))
	}
	return fmt.Sprintf("arca api error (status %d, code=%s): %s", e.StatusCode, e.Code, message)
}

func (e *APIError) Unwrap() error {
	return e.Err
}

// NewAPIError creates a new API error
func NewAPIError(statusCode int, message string, err error) *APIError {
	return &APIError{
		StatusCode: statusCode,
		Message:    redactSensitive(message),
		Err:        err,
	}
}

// ArcaAPIError represents the structured error response from the ARCA API
type ArcaAPIError struct {
	Code    string                 `json:"code"`
	Message string                 `json:"message"`
	Details map[string]interface{} `json:"details,omitempty"`
}

// ArcaErrorResponse represents the full error response envelope
type ArcaErrorResponse struct {
	RequestID string       `json:"request_id"`
	Status    string       `json:"status"`
	Error     ArcaAPIError `json:"error"`
}

// MapErrorCodeToError maps a structured error code to a sentinel error
func MapErrorCodeToError(statusCode int, errResp *ArcaAPIError) error {
	if errResp == nil {
		return NewAPIError(statusCode, "unknown error", nil)
	}

	redactedMessage := redactSensitive(errResp.Message)
	resourceType := resourceTypeFromDetails(errResp.Details)

	switch errResp.Code {
	case "NOT_FOUND":
		switch resourceType {
		case "SVM":
			return ErrSVMNotFound
		case "Directory":
			return ErrDirectoryNotFound
		case "Volume":
			return ErrVolumeNotFound
		case "Snapshot":
			return ErrSnapshotNotFound
		case "Export":
			return ErrExportNotFound
		case "Quota":
			return ErrQuotaNotFound
		default:
			return ErrSVMNotFound
		}
	case "ALREADY_EXISTS":
		switch resourceType {
		case "SVM":
			return ErrSVMAlreadyExists
		case "Directory":
			return ErrDirectoryAlreadyExists
		case "Volume":
			return ErrVolumeAlreadyExists
		case "Snapshot":
			return ErrSnapshotAlreadyExists
		case "Export":
			return ErrExportAlreadyExists
		default:
			return ErrSVMAlreadyExists
		}
	case "CONFLICT":
		if containsAny(errResp.Message, "ip", "vlan", "network") {
			return ErrNetworkConflict
		}
		return &APIError{StatusCode: statusCode, Code: errResp.Code, Message: redactedMessage}
	case "RESOURCE_EXHAUSTED":
		return ErrAllPoolsExhausted
	case "UNAVAILABLE":
		return ErrUnavailable
	case "TIMEOUT":
		return ErrTimeout
	default:
		return &APIError{StatusCode: statusCode, Code: errResp.Code, Message: redactedMessage}
	}
}

func redactSensitive(message string) string {
	if message == "" {
		return message
	}

	redacted := authorizationSecretPattern.ReplaceAllString(message, "${1}${2}"+redactedSecret)
	redacted = bearerSecretPattern.ReplaceAllString(redacted, "${1} "+redactedSecret)
	redacted = keyValueSecretPattern.ReplaceAllString(redacted, "${1}${2}"+redactedSecret)
	return redacted
}

func resourceTypeFromDetails(details map[string]interface{}) string {
	for _, key := range []string{"resource_type", "resource"} {
		if resourceType, ok := details[key].(string); ok {
			return resourceType
		}
	}
	return ""
}

// MapHTTPStatusToError maps HTTP status codes to specific errors.
// This is used as a fallback when the response body doesn't contain
// a structured error (e.g., non-JSON responses from proxies).
func MapHTTPStatusToError(statusCode int, message string) error {
	switch statusCode {
	case 404:
		if containsAny(message, "svm", "storage virtual machine") {
			return ErrSVMNotFound
		} else if containsAny(message, "directory", "path") {
			return ErrDirectoryNotFound
		} else if containsAny(message, "volume") {
			return ErrVolumeNotFound
		} else if containsAny(message, "snapshot") {
			return ErrSnapshotNotFound
		} else if containsAny(message, "export") {
			return ErrExportNotFound
		} else if containsAny(message, "quota") {
			return ErrQuotaNotFound
		}
		return ErrSVMNotFound
	case 409:
		if containsAny(message, "ip", "vlan", "network") {
			return ErrNetworkConflict
		} else if containsAny(message, "directory") {
			return ErrDirectoryAlreadyExists
		} else if containsAny(message, "volume") {
			return ErrVolumeAlreadyExists
		} else if containsAny(message, "snapshot") {
			return ErrSnapshotAlreadyExists
		} else if containsAny(message, "export") {
			return ErrExportAlreadyExists
		}
		return ErrSVMAlreadyExists
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
		errors.Is(err, ErrVolumeNotFound) ||
		errors.Is(err, ErrSnapshotNotFound) ||
		errors.Is(err, ErrExportNotFound) ||
		errors.Is(err, ErrQuotaNotFound)
}

// IsAlreadyExistsError checks if an error is an "already exists" error
func IsAlreadyExistsError(err error) bool {
	return errors.Is(err, ErrSVMAlreadyExists) ||
		errors.Is(err, ErrDirectoryAlreadyExists) ||
		errors.Is(err, ErrVolumeAlreadyExists) ||
		errors.Is(err, ErrSnapshotAlreadyExists) ||
		errors.Is(err, ErrExportAlreadyExists)
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
