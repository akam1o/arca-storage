package arca

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/akam1o/csi-arca-storage/pkg/lock"
)

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

func TestSVMNameForNamespaceMatchesCRDLimit(t *testing.T) {
	namespace := strings.Repeat("a", 60)
	got := svmNameForNamespace(namespace)

	if len(got) > maxArcaSVMNameBytes {
		t.Fatalf("name length = %d, want <= %d", len(got), maxArcaSVMNameBytes)
	}
	if got == "k8s-"+namespace {
		t.Fatalf("namespace at CRD boundary was not shortened: %q", got)
	}
}

func TestEnsureSVMWaitsForReadyExistingSVM(t *testing.T) {
	oldPollInterval := svmReadyPollInterval
	svmReadyPollInterval = time.Millisecond
	defer func() {
		svmReadyPollInterval = oldPollInterval
	}()

	var getCount int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms/k8s-default" {
			http.NotFound(w, r)
			return
		}

		state := "Creating"
		if atomic.AddInt32(&getCount, 1) >= 2 {
			state = "Ready"
		}
		_, _ = fmt.Fprintf(
			w,
			`{"request_id":"req","status":"ok","data":{"name":"k8s-default","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","gateway":"","mtu":1500,"state":%q,"created_at":"2026-01-01T00:00:00Z"}}`,
			state,
		)
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	svm, err := NewSVMManager(client, nil, nil, 1500).EnsureSVM(ctx, "default")
	if err != nil {
		t.Fatalf("EnsureSVM() error = %v", err)
	}
	if svm.State != "Ready" {
		t.Fatalf("EnsureSVM() state = %q, want Ready", svm.State)
	}
	if atomic.LoadInt32(&getCount) < 2 {
		t.Fatalf("GetSVM count = %d, want at least 2", getCount)
	}
}

func TestEnsureSVMReturnsErrorWithoutClient(t *testing.T) {
	_, err := NewSVMManager(nil, nil, nil, 1500).EnsureSVM(context.Background(), "default")
	if err == nil || !strings.Contains(err.Error(), "ARCA client") {
		t.Fatalf("EnsureSVM() error = %v, want ARCA client configuration error", err)
	}
}

func TestEnsureSVMReturnsErrorWithoutLockManager(t *testing.T) {
	client := newMissingSVMTestClient(t)

	_, err := NewSVMManager(client, nil, nil, 1500).EnsureSVM(context.Background(), "default")
	if err == nil || !strings.Contains(err.Error(), "lock manager") {
		t.Fatalf("EnsureSVM() error = %v, want lock manager configuration error", err)
	}
}

func TestEnsureSVMReturnsErrorWithoutNetworkAllocator(t *testing.T) {
	client := newMissingSVMTestClient(t)
	lockMgr := lock.NewManager(k8sfake.NewClientset(), "kube-system", "test-controller")

	_, err := NewSVMManager(client, nil, lockMgr, 1500).EnsureSVM(context.Background(), "default")
	if err == nil || !strings.Contains(err.Error(), "network allocator") {
		t.Fatalf("EnsureSVM() error = %v, want network allocator configuration error", err)
	}
}

func newMissingSVMTestClient(t *testing.T) *Client {
	t.Helper()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"request_id":"req","status":"error","error":{"code":"NOT_FOUND","message":"SVM not found","details":{"resource":"SVM"}}}`))
	}))
	t.Cleanup(server.Close)

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}
	return client
}
