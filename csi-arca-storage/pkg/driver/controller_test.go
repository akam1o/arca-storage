package driver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
	"github.com/akam1o/csi-arca-storage/pkg/idempotency"
	"github.com/akam1o/csi-arca-storage/pkg/store"
)

type failingCreateStore struct {
	*store.MemoryStore
	failCreate bool
}

func (s *failingCreateStore) CreateVolume(info *store.VolumeInfo) error {
	if s.failCreate {
		return errors.New("store create failed")
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

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			snapshotCreated = true
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{}}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/source-path/clone":
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

	_, err = driver.CreateVolume(context.Background(), &csi.CreateVolumeRequest{
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
