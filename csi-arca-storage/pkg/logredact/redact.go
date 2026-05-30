package logredact

import "regexp"

const (
	redactedSecret    = "<redacted>"
	redactedNFSSource = "<nfs-source>"
	redactedPath      = "<path>"
	redactedIP        = "<ip>"
)

var (
	authorizationSecretPattern = regexp.MustCompile(`(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+`)
	bearerSecretPattern        = regexp.MustCompile(`(?i)\bbearer\s+[A-Za-z0-9._~+/\-=]+`)
	keyValueSecretPattern      = regexp.MustCompile(`(?i)\b([a-z0-9_-]*(?:token|password|secret|key)[a-z0-9_-]*\s*[=:]\s*)[^\s,;]+`)
	nfsSourcePattern           = regexp.MustCompile(`\b[\w.-]+(?::\d+)?:/[^\s,;'")\]]+`)
	absolutePathPattern        = regexp.MustCompile(`(^|[\s=:'"(\[])(/[^\s,;'")\]]+)`)
	ipv4Pattern                = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
)

// Message removes common secrets and host-specific mount details from log text.
func Message(message string) string {
	redacted := authorizationSecretPattern.ReplaceAllString(message, "${1}${2}"+redactedSecret)
	redacted = bearerSecretPattern.ReplaceAllString(redacted, "Bearer "+redactedSecret)
	redacted = keyValueSecretPattern.ReplaceAllString(redacted, "${1}"+redactedSecret)
	redacted = nfsSourcePattern.ReplaceAllString(redacted, redactedNFSSource)
	redacted = absolutePathPattern.ReplaceAllString(redacted, "${1}"+redactedPath)
	redacted = ipv4Pattern.ReplaceAllString(redacted, redactedIP)
	return redacted
}

// Error formats an error message for logs while preserving nil-safe callers.
func Error(err error) string {
	if err == nil {
		return ""
	}
	return Message(err.Error())
}
