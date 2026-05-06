package driver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
	"github.com/akam1o/csi-arca-storage/pkg/idempotency"
	"github.com/akam1o/csi-arca-storage/pkg/lock"
	"github.com/akam1o/csi-arca-storage/pkg/store"
)

type failingCreateStore struct {
	*store.MemoryStore
	failCreate               bool
	failUpdate               bool
	failCreateSnapshot       bool
	failUpdateSnapshotStatus bool
}

func (s *failingCreateStore) CreateVolume(info *store.VolumeInfo) error {
	if s.failCreate {
		return errors.New("store create failed")
	}
	return s.MemoryStore.CreateVolume(info)
}

func (s *failingCreateStore) UpdateVolume(info *store.VolumeInfo) error {
	if s.failUpdate {
		return errors.New("store update failed")
	}
	return s.MemoryStore.UpdateVolume(info)
}

func (s *failingCreateStore) CreateSnapshot(info *store.SnapshotInfo) error {
	if s.failCreateSnapshot {
		return errors.New("store snapshot create failed")
	}
	return s.MemoryStore.CreateSnapshot(info)
}

func (s *failingCreateStore) UpdateSnapshotStatus(snapshotID string, readyToUse bool) error {
	if s.failUpdateSnapshotStatus {
		return errors.New("store snapshot status update failed")
	}
	return s.MemoryStore.UpdateSnapshotStatus(snapshotID, readyToUse)
}

type racingCreateStore struct {
	*store.MemoryStore
	existing *store.VolumeInfo
}

func (s *racingCreateStore) CreateVolume(info *store.VolumeInfo) error {
	if s.existing != nil && s.existing.VolumeID == info.VolumeID {
		if err := s.MemoryStore.CreateVolume(s.existing); err != nil && !store.IsAlreadyExists(err) {
			return err
		}
		return fmt.Errorf("%w: volume %s", store.ErrAlreadyExists, info.VolumeID)
	}
	return s.MemoryStore.CreateVolume(info)
}

func TestCreateVolumeCleansUpBackendWhenMetadataStoreFails(t *testing.T) {
	memoryStore := store.NewMemoryStore()
	st := &failingCreateStore{MemoryStore: memoryStore}
	if err := st.MemoryStore.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "source-path",
		CapacityBytes: 1 << 30,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}
	if err := st.MemoryStore.CreateSnapshot(&store.SnapshotInfo{
		SnapshotID:     "snap-a",
		SourceVolumeID: "source-vol",
		SVMName:        "svm-a",
		Path:           "snap-a",
		SizeBytes:      1 << 30,
		ReadyToUse:     true,
	}); err != nil {
		t.Fatalf("seed snapshot: %v", err)
	}
	st.failCreate = true

	var cloneBody map[string]interface{}
	var cleanupPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/svm-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"svm-a","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","gateway":"","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &cloneBody); err != nil {
				t.Fatalf("clone body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"restored"}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/directories/svm-a":
			cleanupPath = r.URL.Query().Get("path")
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	volumeIDGen := idempotency.NewVolumeIDGenerator()
	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}
	targetPath := volumeIDGen.GenerateVolumeID("restore-pvc")

	_, err = driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "restore-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "restore-pvc",
		},
		CapacityRange: &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: []*csi.VolumeCapability{
			{
				AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
				AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
			},
		},
		VolumeContentSource: &csi.VolumeContentSource{
			Type: &csi.VolumeContentSource_Snapshot{
				Snapshot: &csi.VolumeContentSource_SnapshotSource{SnapshotId: "snap-a"},
			},
		},
	})

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal error, got %v", err)
	}
	if cloneBody["name"] != targetPath || cloneBody["svm"] != "svm-a" || cloneBody["snapshot"] != "snap-a" {
		t.Fatalf("CloneVolumeFromSnapshot body = %#v", cloneBody)
	}
	if _, ok := cloneBody["source_volume"]; ok {
		t.Fatalf("CloneVolumeFromSnapshot body leaked source_volume = %#v", cloneBody)
	}
	if cleanupPath != targetPath {
		t.Fatalf("cleanup path = %q, want %q", cleanupPath, targetPath)
	}
}

func TestCreateVolumeDoesNotMutateQuotaWhenCreateRaceIsIncompatible(t *testing.T) {
	volumeIDGen := idempotency.NewVolumeIDGenerator()
	volumeID := volumeIDGen.GenerateVolumeID("race-pvc")
	st := &racingCreateStore{
		MemoryStore: store.NewMemoryStore(),
		existing: &store.VolumeInfo{
			VolumeID:      volumeID,
			Name:          "race-pvc",
			SVMName:       "k8s-ns-a",
			VIP:           "10.0.0.10",
			ExportRoot:    "/exports/k8s-ns-a",
			Path:          volumeID,
			CapacityBytes: 1 << 30,
			CreatedAt:     time.Now(),
		},
	}

	var directoryCreated bool
	var quotaCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/k8s-ns-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-ns-a","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","export_root":"/exports/k8s-ns-a","gateway":"","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/directories":
			directoryCreated = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"directory":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			quotaCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		svmManager:    arca.NewSVMManager(client, nil, nil, 1500),
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	_, err = driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "race-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "race-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
	})

	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("expected AlreadyExists error, got %v", err)
	}
	if !directoryCreated {
		t.Fatalf("directory endpoint was not called")
	}
	if quotaCalled {
		t.Fatalf("quota endpoint was called")
	}
	stored, err := st.GetVolume(volumeID)
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if stored.CapacityBytes != 1<<30 {
		t.Fatalf("stored capacity = %d, want %d", stored.CapacityBytes, int64(1<<30))
	}
}

func TestCreateVolumeWaitsForQuotaBeforeIdempotentSuccess(t *testing.T) {
	st := store.NewMemoryStore()
	volumeIDGen := idempotency.NewVolumeIDGenerator()
	volumeID := volumeIDGen.GenerateVolumeID("race-pvc")
	quotaEntered := make(chan struct{})
	releaseQuota := make(chan struct{})
	var quotaCalls int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/k8s-ns-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-ns-a","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","export_root":"/exports/k8s-ns-a","gateway":"","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/directories":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"directory":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			if atomic.AddInt32(&quotaCalls, 1) == 1 {
				close(quotaEntered)
				<-releaseQuota
			}
			http.Error(w, "quota failed", http.StatusBadRequest)
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/directories/k8s-ns-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		svmManager:    arca.NewSVMManager(client, nil, nil, 1500),
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}
	req := &csi.CreateVolumeRequest{
		Name: "race-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "race-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 1 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
	}

	firstDone := make(chan error, 1)
	go func() {
		_, err := driver.CreateVolume(context.Background(), req)
		firstDone <- err
	}()

	select {
	case <-quotaEntered:
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for first quota call")
	}

	secondStarted := make(chan struct{})
	secondDone := make(chan error, 1)
	go func() {
		close(secondStarted)
		_, err := driver.CreateVolume(context.Background(), req)
		secondDone <- err
	}()
	<-secondStarted

	select {
	case err := <-secondDone:
		t.Fatalf("second CreateVolume returned before quota completed: %v", err)
	case <-time.After(100 * time.Millisecond):
	}

	close(releaseQuota)

	if err := <-firstDone; status.Code(err) != codes.Internal {
		t.Fatalf("first CreateVolume code = %v, want Internal", status.Code(err))
	}
	if err := <-secondDone; status.Code(err) != codes.Internal {
		t.Fatalf("second CreateVolume code = %v, want Internal", status.Code(err))
	}
	if _, err := st.GetVolume(volumeID); !store.IsNotFound(err) {
		t.Fatalf("stored volume error = %v, want not found", err)
	}
	if calls := atomic.LoadInt32(&quotaCalls); calls != 2 {
		t.Fatalf("quota calls = %d, want 2", calls)
	}
}

func TestCreateVolumeUsesDistributedLockBeforeIdempotentSuccess(t *testing.T) {
	st := store.NewMemoryStore()
	volumeIDGen := idempotency.NewVolumeIDGenerator()
	quotaEntered := make(chan struct{})
	releaseQuota := make(chan struct{})
	var quotaCalls int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/k8s-ns-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-ns-a","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","export_root":"/exports/k8s-ns-a","gateway":"","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/directories":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"directory":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			if atomic.AddInt32(&quotaCalls, 1) == 1 {
				close(quotaEntered)
				<-releaseQuota
			}
			http.Error(w, "quota failed", http.StatusBadRequest)
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/directories/k8s-ns-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	k8sClient := k8sfake.NewSimpleClientset()
	newDriver := func(identity string) *Driver {
		return &Driver{
			mode:          "controller",
			arcaClient:    client,
			svmManager:    arca.NewSVMManager(client, nil, nil, 1500),
			store:         st,
			volumeIDGen:   volumeIDGen,
			snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
			lockManager:   lock.NewManager(k8sClient, "kube-system", identity),
		}
	}
	req := &csi.CreateVolumeRequest{
		Name: "race-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "race-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 1 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
	}

	firstDone := make(chan error, 1)
	go func() {
		_, err := newDriver("pod-a").CreateVolume(context.Background(), req)
		firstDone <- err
	}()

	select {
	case <-quotaEntered:
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for first quota call")
	}

	secondDone := make(chan error, 1)
	go func() {
		_, err := newDriver("pod-b").CreateVolume(context.Background(), req)
		secondDone <- err
	}()

	select {
	case err := <-secondDone:
		t.Fatalf("second CreateVolume returned before distributed lock was released: %v", err)
	case <-time.After(100 * time.Millisecond):
	}

	close(releaseQuota)

	if err := <-firstDone; status.Code(err) != codes.Internal {
		t.Fatalf("first CreateVolume code = %v, want Internal", status.Code(err))
	}
	select {
	case err := <-secondDone:
		if status.Code(err) != codes.Internal {
			t.Fatalf("second CreateVolume code = %v, want Internal", status.Code(err))
		}
	case <-time.After(3 * time.Second):
		t.Fatalf("timed out waiting for second CreateVolume")
	}
	if calls := atomic.LoadInt32(&quotaCalls); calls != 2 {
		t.Fatalf("quota calls = %d, want 2", calls)
	}
}

func TestCreateVolumeResumesPendingMetadata(t *testing.T) {
	st := store.NewMemoryStore()
	volumeIDGen := idempotency.NewVolumeIDGenerator()
	volumeID := volumeIDGen.GenerateVolumeID("pending-pvc")
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      volumeID,
		Name:          "pending-pvc",
		SVMName:       "k8s-ns-a",
		VIP:           "10.0.0.10",
		ExportRoot:    "/exports/k8s-ns-a",
		Path:          volumeID,
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
		ReadyToUse:    store.VolumeReadyState(false),
	}); err != nil {
		t.Fatalf("seed pending volume: %v", err)
	}

	var quotaCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			quotaCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	resp, err := driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "pending-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "pending-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 1 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
	})

	if err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}
	if !quotaCalled {
		t.Fatalf("quota endpoint was not called")
	}
	if resp.GetVolume().GetVolumeId() != volumeID {
		t.Fatalf("volume id = %q, want %q", resp.GetVolume().GetVolumeId(), volumeID)
	}
	stored, err := st.GetVolume(volumeID)
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if !store.IsVolumeReady(stored) {
		t.Fatalf("stored volume was not marked ready")
	}
}

func TestListVolumesSkipsPendingVolumes(t *testing.T) {
	st := store.NewMemoryStore()
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-ready",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "ready-path",
		CapacityBytes: 1 << 30,
		ReadyToUse:    store.VolumeReadyState(true),
	}); err != nil {
		t.Fatalf("seed ready volume: %v", err)
	}
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-pending",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "pending-path",
		CapacityBytes: 1 << 30,
		ReadyToUse:    store.VolumeReadyState(false),
	}); err != nil {
		t.Fatalf("seed pending volume: %v", err)
	}

	driver := &Driver{
		mode:  "controller",
		store: st,
	}

	resp, err := driver.ListVolumes(context.Background(), &csi.ListVolumesRequest{})
	if err != nil {
		t.Fatalf("ListVolumes() error = %v", err)
	}
	if len(resp.GetEntries()) != 1 {
		t.Fatalf("entries = %d, want 1: %#v", len(resp.GetEntries()), resp.GetEntries())
	}
	if got := resp.GetEntries()[0].GetVolume().GetVolumeId(); got != "vol-ready" {
		t.Fatalf("listed volume = %q, want vol-ready", got)
	}
}

func TestValidateVolumeCapabilitiesRejectsPendingVolume(t *testing.T) {
	st := store.NewMemoryStore()
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-pending",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "pending-path",
		CapacityBytes: 1 << 30,
		ReadyToUse:    store.VolumeReadyState(false),
	}); err != nil {
		t.Fatalf("seed pending volume: %v", err)
	}

	driver := &Driver{
		mode:  "controller",
		store: st,
	}

	_, err := driver.ValidateVolumeCapabilities(context.Background(), &csi.ValidateVolumeCapabilitiesRequest{
		VolumeId:           "vol-pending",
		VolumeCapabilities: testVolumeCapabilities(),
	})

	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected Unavailable error, got %v", err)
	}
}

func TestCreateSnapshotRejectsPendingSourceVolume(t *testing.T) {
	st := store.NewMemoryStore()
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-pending",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "pending-path",
		CapacityBytes: 1 << 30,
		ReadyToUse:    store.VolumeReadyState(false),
	}); err != nil {
		t.Fatalf("seed pending volume: %v", err)
	}

	var snapshotCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			snapshotCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	_, err = driver.CreateSnapshot(context.Background(), &csi.CreateSnapshotRequest{
		Name:           "snap-a",
		SourceVolumeId: "vol-pending",
	})

	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected Unavailable error, got %v", err)
	}
	if snapshotCalled {
		t.Fatalf("snapshot endpoint was called")
	}
}

func TestCreateSnapshotCleansUpBackendWhenMetadataStoreFails(t *testing.T) {
	st := &failingCreateStore{
		MemoryStore:        store.NewMemoryStore(),
		failCreateSnapshot: true,
	}
	if err := st.MemoryStore.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "source-path",
		CapacityBytes: 1 << 30,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}

	snapshotIDGen := idempotency.NewSnapshotIDGenerator()
	snapshotID := snapshotIDGen.GenerateSnapshotID("source-vol/snap-a")
	var snapshotCreated bool
	var deletedQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			snapshotCreated = true
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/"+snapshotID:
			deletedQuery = r.URL.RawQuery
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		snapshotIDGen: snapshotIDGen,
	}

	_, err = driver.CreateSnapshot(context.Background(), &csi.CreateSnapshotRequest{
		Name:           "snap-a",
		SourceVolumeId: "source-vol",
	})

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal error, got %v", err)
	}
	if !snapshotCreated {
		t.Fatalf("snapshot endpoint was not called")
	}
	if deletedQuery != "svm=svm-a&volume=source-path" {
		t.Fatalf("delete query = %q, want svm=svm-a&volume=source-path", deletedQuery)
	}
}

func TestCreateSnapshotCleansUpBackendWhenStatusStoreFails(t *testing.T) {
	st := &failingCreateStore{
		MemoryStore:              store.NewMemoryStore(),
		failUpdateSnapshotStatus: true,
	}
	if err := st.MemoryStore.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "source-path",
		CapacityBytes: 1 << 30,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}

	snapshotIDGen := idempotency.NewSnapshotIDGenerator()
	snapshotID := snapshotIDGen.GenerateSnapshotID("source-vol/snap-a")
	var deletedQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/"+snapshotID:
			deletedQuery = r.URL.RawQuery
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		snapshotIDGen: snapshotIDGen,
	}

	_, err = driver.CreateSnapshot(context.Background(), &csi.CreateSnapshotRequest{
		Name:           "snap-a",
		SourceVolumeId: "source-vol",
	})

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal error, got %v", err)
	}
	if deletedQuery != "svm=svm-a&volume=source-path" {
		t.Fatalf("delete query = %q, want svm=svm-a&volume=source-path", deletedQuery)
	}
	if _, err := st.GetSnapshot(snapshotID); !store.IsNotFound(err) {
		t.Fatalf("snapshot metadata was not removed, err=%v", err)
	}
}

func TestCreateVolumeCleansUpTemporaryCloneSnapshotOnCloneFailure(t *testing.T) {
	st := store.NewMemoryStore()
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "source-path",
		CapacityBytes: 1 << 30,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}

	volumeIDGen := idempotency.NewVolumeIDGenerator()
	targetPath := volumeIDGen.GenerateVolumeID("clone-pvc")
	temporarySnapshotName := "clone-" + targetPath
	var snapshotCreated bool
	var snapshotDeleted bool
	var deletedVolume string
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			snapshotCreated = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
			cancel()
			http.Error(w, "clone failed", http.StatusInternalServerError)
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/"+temporarySnapshotName:
			snapshotDeleted = true
			deletedVolume = r.URL.Query().Get("volume")
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	_, err = driver.CreateVolume(ctx, &csi.CreateVolumeRequest{
		Name: "clone-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "clone-pvc",
		},
		CapacityRange: &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: []*csi.VolumeCapability{
			{
				AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
				AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
			},
		},
		VolumeContentSource: &csi.VolumeContentSource{
			Type: &csi.VolumeContentSource_Volume{
				Volume: &csi.VolumeContentSource_VolumeSource{VolumeId: "source-vol"},
			},
		},
	})

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal error, got %v", err)
	}
	if !snapshotCreated {
		t.Fatalf("temporary snapshot was not created")
	}
	if !snapshotDeleted {
		t.Fatalf("temporary snapshot was not deleted after clone failure")
	}
	if deletedVolume != "source-path" {
		t.Fatalf("deleted snapshot volume = %q, want source-path", deletedVolume)
	}
}

func TestCreateVolumeCleansUpExistingTemporaryCloneSnapshotOnRetry(t *testing.T) {
	st := store.NewMemoryStore()
	const sourceCapacity = int64(4 << 30)
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		ExportRoot:    "/exports/svm-a",
		Path:          "source-path",
		CapacityBytes: sourceCapacity,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}

	volumeIDGen := idempotency.NewVolumeIDGenerator()
	targetPath := volumeIDGen.GenerateVolumeID("clone-pvc")
	temporarySnapshotName := "clone-" + targetPath
	var snapshotConflict bool
	var snapshotDeleted bool
	var deletedVolume string
	var cloneBody struct {
		Name     string `json:"name"`
		SVM      string `json:"svm"`
		Snapshot string `json:"snapshot"`
		SizeGiB  int    `json:"size_gib"`
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			snapshotConflict = true
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"error","error":{"code":"ALREADY_EXISTS","message":"snapshot already exists","details":{"resource":"Snapshot"}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &cloneBody); err != nil {
				t.Fatalf("clone body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"cloned"}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/"+temporarySnapshotName:
			snapshotDeleted = true
			deletedVolume = r.URL.Query().Get("volume")
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	_, err = driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "clone-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "clone-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
		VolumeContentSource: &csi.VolumeContentSource{
			Type: &csi.VolumeContentSource_Volume{
				Volume: &csi.VolumeContentSource_VolumeSource{VolumeId: "source-vol"},
			},
		},
	})

	if err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}
	if !snapshotConflict {
		t.Fatalf("existing temporary snapshot path was not exercised")
	}
	if cloneBody.Name != targetPath || cloneBody.SVM != "svm-a" || cloneBody.Snapshot != temporarySnapshotName {
		t.Fatalf("CloneVolumeFromSnapshot body = %#v", cloneBody)
	}
	if !snapshotDeleted {
		t.Fatalf("existing temporary snapshot was not deleted after clone retry")
	}
	if deletedVolume != "source-path" {
		t.Fatalf("deleted snapshot volume = %q, want source-path", deletedVolume)
	}
}

func TestCreateVolumeFromVolumeRecordsEffectiveSourceCapacity(t *testing.T) {
	st := store.NewMemoryStore()
	const sourceCapacity = int64(4 << 30)
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		ExportRoot:    "/exports/svm-a",
		Path:          "source-path",
		CapacityBytes: sourceCapacity,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}

	volumeIDGen := idempotency.NewVolumeIDGenerator()
	targetPath := volumeIDGen.GenerateVolumeID("clone-pvc")
	temporarySnapshotName := "clone-" + targetPath
	var cloneBody struct {
		Name     string `json:"name"`
		SVM      string `json:"svm"`
		Snapshot string `json:"snapshot"`
		SizeGiB  int    `json:"size_gib"`
	}
	var quotaBody struct {
		SVMName    string `json:"svm_name"`
		Path       string `json:"path"`
		QuotaBytes int64  `json:"quota_bytes"`
	}
	var snapshotDeleted bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &cloneBody); err != nil {
				t.Fatalf("clone body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"cloned"}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/"+temporarySnapshotName:
			snapshotDeleted = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &quotaBody); err != nil {
				t.Fatalf("quota body unmarshal error = %v", err)
			}
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	resp, err := driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "clone-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "clone-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
		VolumeContentSource: &csi.VolumeContentSource{
			Type: &csi.VolumeContentSource_Volume{
				Volume: &csi.VolumeContentSource_VolumeSource{VolumeId: "source-vol"},
			},
		},
	})

	if err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}
	if cloneBody.Name != targetPath || cloneBody.SVM != "svm-a" || cloneBody.Snapshot != temporarySnapshotName {
		t.Fatalf("CloneVolumeFromSnapshot body = %#v", cloneBody)
	}
	if cloneBody.SizeGiB != 4 {
		t.Fatalf("clone size_gib = %d, want 4", cloneBody.SizeGiB)
	}
	if quotaBody.SVMName != "svm-a" || quotaBody.Path != targetPath || quotaBody.QuotaBytes != sourceCapacity {
		t.Fatalf("SetQuota body = %#v", quotaBody)
	}
	if resp.GetVolume().GetCapacityBytes() != sourceCapacity {
		t.Fatalf("response capacity = %d, want %d", resp.GetVolume().GetCapacityBytes(), sourceCapacity)
	}
	stored, err := st.GetVolume(resp.GetVolume().GetVolumeId())
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if stored.CapacityBytes != sourceCapacity {
		t.Fatalf("stored capacity = %d, want %d", stored.CapacityBytes, sourceCapacity)
	}
	if !snapshotDeleted {
		t.Fatalf("temporary snapshot was not deleted after clone")
	}
}

func TestCreateVolumeFromSnapshotRecordsEffectiveSourceCapacity(t *testing.T) {
	st := store.NewMemoryStore()
	const sourceCapacity = int64(4 << 30)
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "source-vol",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		ExportRoot:    "/exports/svm-a",
		Path:          "source-path",
		CapacityBytes: sourceCapacity,
	}); err != nil {
		t.Fatalf("seed source volume: %v", err)
	}
	if err := st.CreateSnapshot(&store.SnapshotInfo{
		SnapshotID:     "snap-a",
		SourceVolumeID: "source-vol",
		SVMName:        "svm-a",
		Path:           "snap-a",
		SizeBytes:      sourceCapacity,
		ReadyToUse:     true,
	}); err != nil {
		t.Fatalf("seed snapshot: %v", err)
	}

	volumeIDGen := idempotency.NewVolumeIDGenerator()
	targetPath := volumeIDGen.GenerateVolumeID("restore-pvc")
	var cloneBody struct {
		Name     string `json:"name"`
		SVM      string `json:"svm"`
		Snapshot string `json:"snapshot"`
		SizeGiB  int    `json:"size_gib"`
	}
	var quotaBody struct {
		SVMName    string `json:"svm_name"`
		Path       string `json:"path"`
		QuotaBytes int64  `json:"quota_bytes"`
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/svm-a":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"svm-a","ip_cidr":"10.0.0.10/24","vip":"10.0.0.10","gateway":"","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &cloneBody); err != nil {
				t.Fatalf("clone body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"restored"}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &quotaBody); err != nil {
				t.Fatalf("quota body unmarshal error = %v", err)
			}
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:          "controller",
		arcaClient:    client,
		store:         st,
		volumeIDGen:   volumeIDGen,
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	resp, err := driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
		Name: "restore-pvc",
		Parameters: map[string]string{
			paramNamespace: "ns-a",
			paramPVCName:   "restore-pvc",
		},
		CapacityRange:      &csi.CapacityRange{RequiredBytes: 2 << 30},
		VolumeCapabilities: testVolumeCapabilities(),
		VolumeContentSource: &csi.VolumeContentSource{
			Type: &csi.VolumeContentSource_Snapshot{
				Snapshot: &csi.VolumeContentSource_SnapshotSource{SnapshotId: "snap-a"},
			},
		},
	})

	if err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}
	if cloneBody.Name != targetPath || cloneBody.SVM != "svm-a" || cloneBody.Snapshot != "snap-a" {
		t.Fatalf("CloneVolumeFromSnapshot body = %#v", cloneBody)
	}
	if cloneBody.SizeGiB != 4 {
		t.Fatalf("clone size_gib = %d, want 4", cloneBody.SizeGiB)
	}
	if quotaBody.SVMName != "svm-a" || quotaBody.Path != targetPath || quotaBody.QuotaBytes != sourceCapacity {
		t.Fatalf("SetQuota body = %#v", quotaBody)
	}
	if resp.GetVolume().GetCapacityBytes() != sourceCapacity {
		t.Fatalf("response capacity = %d, want %d", resp.GetVolume().GetCapacityBytes(), sourceCapacity)
	}
	stored, err := st.GetVolume(resp.GetVolume().GetVolumeId())
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if stored.CapacityBytes != sourceCapacity {
		t.Fatalf("stored capacity = %d, want %d", stored.CapacityBytes, sourceCapacity)
	}
}

func TestControllerExpandVolumeRecordsProvisionedCapacity(t *testing.T) {
	st := store.NewMemoryStore()
	const (
		initialCapacity  = int64(4 << 30)
		expectedCapacity = int64(5 << 30)
	)
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: initialCapacity,
	}); err != nil {
		t.Fatalf("seed volume: %v", err)
	}

	var quotaBody struct {
		SVMName    string `json:"svm_name"`
		Path       string `json:"path"`
		QuotaBytes int64  `json:"quota_bytes"`
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &quotaBody); err != nil {
				t.Fatalf("quota body unmarshal error = %v", err)
			}
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:       "controller",
		arcaClient: client,
		store:      st,
	}

	resp, err := driver.ControllerExpandVolume(context.Background(), &csi.ControllerExpandVolumeRequest{
		VolumeId:      "vol-a",
		CapacityRange: &csi.CapacityRange{RequiredBytes: initialCapacity + 1},
	})

	if err != nil {
		t.Fatalf("ControllerExpandVolume() error = %v", err)
	}
	if quotaBody.SVMName != "svm-a" || quotaBody.Path != "path-a" || quotaBody.QuotaBytes != expectedCapacity {
		t.Fatalf("SetQuota body = %#v", quotaBody)
	}
	if resp.GetCapacityBytes() != expectedCapacity {
		t.Fatalf("response capacity = %d, want %d", resp.GetCapacityBytes(), expectedCapacity)
	}
	stored, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if stored.CapacityBytes != expectedCapacity {
		t.Fatalf("stored capacity = %d, want %d", stored.CapacityBytes, expectedCapacity)
	}
}

func TestControllerExpandVolumeRejectsPendingVolume(t *testing.T) {
	st := store.NewMemoryStore()
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-pending",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "pending-path",
		CapacityBytes: 1 << 30,
		ReadyToUse:    store.VolumeReadyState(false),
	}); err != nil {
		t.Fatalf("seed pending volume: %v", err)
	}

	var quotaCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			quotaCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:       "controller",
		arcaClient: client,
		store:      st,
	}

	_, err = driver.ControllerExpandVolume(context.Background(), &csi.ControllerExpandVolumeRequest{
		VolumeId:      "vol-pending",
		CapacityRange: &csi.CapacityRange{RequiredBytes: 2 << 30},
	})

	if status.Code(err) != codes.Unavailable {
		t.Fatalf("expected Unavailable error, got %v", err)
	}
	if quotaCalled {
		t.Fatalf("quota endpoint was called")
	}
}

func TestControllerExpandVolumeRejectsProvisionedCapacityAboveLimit(t *testing.T) {
	st := store.NewMemoryStore()
	const initialCapacity = int64(4 << 30)
	if err := st.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: initialCapacity,
	}); err != nil {
		t.Fatalf("seed volume: %v", err)
	}

	var quotaCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			quotaCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:       "controller",
		arcaClient: client,
		store:      st,
	}

	_, err = driver.ControllerExpandVolume(context.Background(), &csi.ControllerExpandVolumeRequest{
		VolumeId: "vol-a",
		CapacityRange: &csi.CapacityRange{
			RequiredBytes: initialCapacity + 1,
			LimitBytes:    initialCapacity + 1,
		},
	})

	if status.Code(err) != codes.OutOfRange {
		t.Fatalf("expected OutOfRange error, got %v", err)
	}
	if quotaCalled {
		t.Fatalf("quota endpoint was called")
	}
	stored, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("stored volume not found: %v", err)
	}
	if stored.CapacityBytes != initialCapacity {
		t.Fatalf("stored capacity = %d, want %d", stored.CapacityBytes, initialCapacity)
	}
}

func TestControllerExpandVolumeFailsWhenMetadataUpdateFails(t *testing.T) {
	st := &failingCreateStore{MemoryStore: store.NewMemoryStore(), failUpdate: true}
	if err := st.MemoryStore.CreateVolume(&store.VolumeInfo{
		VolumeID:      "vol-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 1 << 30,
	}); err != nil {
		t.Fatalf("seed volume: %v", err)
	}

	var quotaCalled bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/quotas":
			quotaCalled = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"quota":{}}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := arca.NewClient(&arca.ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	driver := &Driver{
		mode:       "controller",
		arcaClient: client,
		store:      st,
	}

	_, err = driver.ControllerExpandVolume(context.Background(), &csi.ControllerExpandVolumeRequest{
		VolumeId:      "vol-a",
		CapacityRange: &csi.CapacityRange{RequiredBytes: 2 << 30},
	})

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal error, got %v", err)
	}
	if !quotaCalled {
		t.Fatalf("quota endpoint was not called")
	}
}

func testVolumeCapabilities() []*csi.VolumeCapability {
	return []*csi.VolumeCapability{
		{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	}
}
