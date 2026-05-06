package arca

import "testing"

func TestSVMNameForNamespaceKeepsShortNamesReadable(t *testing.T) {
	got := svmNameForNamespace("default")
	if got != "k8s-default" {
		t.Fatalf("svmNameForNamespace() = %q, want %q", got, "k8s-default")
	}
}

func TestSVMNameForNamespaceBoundsLongNames(t *testing.T) {
	namespace := "tenant-with-a-very-long-but-valid-kubernetes-namespace-name-123"
	got := svmNameForNamespace(namespace)

	if len(got) > maxArcaSVMNameBytes {
		t.Fatalf("name length = %d, want <= %d", len(got), maxArcaSVMNameBytes)
	}
	if got == "k8s-"+namespace {
		t.Fatalf("long namespace was not shortened: %q", got)
	}
	if got != svmNameForNamespace(namespace) {
		t.Fatalf("name generation is not stable")
	}

	other := svmNameForNamespace(namespace + "4")
	if got == other {
		t.Fatalf("different long namespaces produced the same bounded name: %q", got)
	}
}
