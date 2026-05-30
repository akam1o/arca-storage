package logredact

import (
	"errors"
	"strings"
	"testing"
)

func TestMessageRedactsSensitiveLogDetails(t *testing.T) {
	input := "mount failed for 10.0.0.1:/exports/team at /var/lib/kubelet/pods/x: Authorization: Bearer secret-token token=another-secret"

	got := Message(input)

	for _, forbidden := range []string{
		"10.0.0.1",
		"/exports/team",
		"/var/lib/kubelet/pods/x",
		"secret-token",
		"another-secret",
	} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("redacted message %q still contains %q", got, forbidden)
		}
	}
	for _, want := range []string{"mount failed", redactedSecret, redactedNFSSource, redactedPath} {
		if !strings.Contains(got, want) {
			t.Fatalf("redacted message %q does not contain %q", got, want)
		}
	}
}

func TestErrorHandlesNil(t *testing.T) {
	if got := Error(nil); got != "" {
		t.Fatalf("Error(nil) = %q, want empty string", got)
	}
	if got := Error(errors.New("permission denied")); got != "permission denied" {
		t.Fatalf("Error() = %q, want original message", got)
	}
}
