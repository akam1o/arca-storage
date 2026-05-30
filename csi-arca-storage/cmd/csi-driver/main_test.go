package main

import "testing"

func TestControllerLockNamespaceUsesPodNamespace(t *testing.T) {
	t.Setenv("POD_NAMESPACE", "arca-system")

	if got := controllerLockNamespace(); got != "arca-system" {
		t.Fatalf("controllerLockNamespace() = %q, want %q", got, "arca-system")
	}
}

func TestControllerLockNamespaceDefaultsToKubeSystem(t *testing.T) {
	t.Setenv("POD_NAMESPACE", "")

	if got := controllerLockNamespace(); got != defaultLockNamespace {
		t.Fatalf("controllerLockNamespace() = %q, want %q", got, defaultLockNamespace)
	}
}

func TestControllerLockNamespaceTrimsWhitespace(t *testing.T) {
	t.Setenv("POD_NAMESPACE", "  arca-system  ")

	if got := controllerLockNamespace(); got != "arca-system" {
		t.Fatalf("controllerLockNamespace() = %q, want %q", got, "arca-system")
	}
}
