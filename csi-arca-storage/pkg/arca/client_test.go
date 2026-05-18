package arca

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestClientDecodesFastAPIEnvelopes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/k8s-default":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","export_root":"/srv/arca/k8s-default","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/svms":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"svm":{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","export_root":"/srv/arca/k8s-default","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","export_root":"/srv/arca/k8s-default","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}],"next_cursor":null}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	ctx := context.Background()
	svm, err := client.GetSVM(ctx, "k8s-default")
	if err != nil {
		t.Fatalf("GetSVM() error = %v", err)
	}
	if svm.Name != "k8s-default" || svm.VIP != "192.168.10.5" || svm.ExportRoot != "/srv/arca/k8s-default" {
		t.Fatalf("GetSVM() = %#v", svm)
	}

	created, err := client.CreateSVM(ctx, &CreateSVMRequest{
		Name:    "k8s-default",
		VLANID:  100,
		IPCIDR:  "192.168.10.5/24",
		Gateway: "192.168.10.1",
		MTU:     1500,
	})
	if err != nil {
		t.Fatalf("CreateSVM() error = %v", err)
	}
	if created.Name != "k8s-default" || created.VIP != "192.168.10.5" {
		t.Fatalf("CreateSVM() = %#v", created)
	}

	svms, err := client.ListSVMs(ctx)
	if err != nil {
		t.Fatalf("ListSVMs() error = %v", err)
	}
	if len(svms) != 1 || svms[0].Name != "k8s-default" {
		t.Fatalf("ListSVMs() = %#v", svms)
	}
}

func TestClientNormalizesTrailingSlashBaseURL(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms/k8s-default" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-default","vip":"192.168.10.5"}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL + "/", Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	if _, err := client.GetSVM(context.Background(), "k8s-default"); err != nil {
		t.Fatalf("GetSVM() error = %v", err)
	}
}

func TestClientEscapesPathSegments(t *testing.T) {
	dangerousName := "tenant/../other%2Fencoded"
	escapedName := url.PathEscape(dangerousName)

	var requests []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		requests = append(requests, r.Method+" "+r.URL.EscapedPath())

		switch {
		case r.Method == http.MethodGet && r.URL.EscapedPath() == "/v1/svms/"+escapedName:
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"tenant","vip":"192.168.10.5"}}`))
		case r.Method == http.MethodDelete && r.URL.EscapedPath() == "/v1/svms/"+escapedName:
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && r.URL.EscapedPath() == "/v1/svms/"+escapedName+"/capacity":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"total_bytes":1073741824,"available_bytes":536870912,"used_bytes":536870912}}`))
		case r.Method == http.MethodDelete && r.URL.EscapedPath() == "/v1/directories/"+escapedName:
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && r.URL.EscapedPath() == "/v1/quotas/"+escapedName:
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"path":"pvc/data","quota_bytes":1073741824,"used_bytes":0,"project_id":10}}`))
		default:
			t.Fatalf("unexpected request: method=%s path=%s raw_path=%s query=%s", r.Method, r.URL.Path, r.URL.RawPath, r.URL.RawQuery)
		}
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	ctx := context.Background()
	if _, err := client.GetSVM(ctx, dangerousName); err != nil {
		t.Fatalf("GetSVM() error = %v", err)
	}
	if err := client.DeleteSVM(ctx, dangerousName); err != nil {
		t.Fatalf("DeleteSVM() error = %v", err)
	}
	if _, err := client.GetSVMCapacity(ctx, dangerousName); err != nil {
		t.Fatalf("GetSVMCapacity() error = %v", err)
	}
	if err := client.DeleteDirectory(ctx, dangerousName, "pvc/data"); err != nil {
		t.Fatalf("DeleteDirectory() error = %v", err)
	}
	if _, err := client.GetQuota(ctx, dangerousName, "pvc/data"); err != nil {
		t.Fatalf("GetQuota() error = %v", err)
	}

	if len(requests) != 5 {
		t.Fatalf("request count = %d, want 5: %v", len(requests), requests)
	}
}

func TestListSVMsFollowsPagination(t *testing.T) {
	var requests []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms" {
			http.NotFound(w, r)
			return
		}
		requests = append(requests, r.URL.RawQuery)

		switch len(requests) {
		case 1:
			if r.URL.Query().Get("limit") != "200" || r.URL.Query().Get("cursor") != "" {
				t.Fatalf("first request query = %s", r.URL.RawQuery)
			}
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[{"name":"svm-a","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5"}],"next_cursor":"cursor-1"}}`))
		case 2:
			if r.URL.Query().Get("limit") != "200" || r.URL.Query().Get("cursor") != "cursor-1" {
				t.Fatalf("second request query = %s", r.URL.RawQuery)
			}
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[{"name":"svm-b","vlan_id":100,"ip_cidr":"192.168.10.6/24","vip":"192.168.10.6"}],"next_cursor":null}}`))
		default:
			t.Fatalf("unexpected request %d query=%s", len(requests), r.URL.RawQuery)
		}
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	svms, err := client.ListSVMs(context.Background())
	if err != nil {
		t.Fatalf("ListSVMs() error = %v", err)
	}
	if len(svms) != 2 || svms[0].Name != "svm-a" || svms[1].Name != "svm-b" {
		t.Fatalf("ListSVMs() = %#v", svms)
	}
	if len(requests) != 2 {
		t.Fatalf("request count = %d", len(requests))
	}
}

func TestListSVMsRejectsRepeatedPaginationCursor(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms" {
			http.NotFound(w, r)
			return
		}
		calls++
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[],"next_cursor":"cursor-1"}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	_, err = client.ListSVMs(context.Background())
	if !errors.Is(err, ErrInvalidResponse) {
		t.Fatalf("ListSVMs() error = %v, want ErrInvalidResponse", err)
	}
	if calls != 2 {
		t.Fatalf("GET calls = %d, want 2", calls)
	}
}

func TestClientRetriesReadRequests(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms/k8s-default" {
			http.NotFound(w, r)
			return
		}
		calls++
		if calls == 1 {
			http.Error(w, "temporary failure", http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-default","vip":"192.168.10.5"}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	if _, err := client.GetSVM(context.Background(), "k8s-default"); err != nil {
		t.Fatalf("GetSVM() error = %v", err)
	}
	if calls != 2 {
		t.Fatalf("GET calls = %d, want 2", calls)
	}
}

func TestClientDoesNotRetryMutatingRequests(t *testing.T) {
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost || r.URL.Path != "/v1/snapshots" {
			http.NotFound(w, r)
			return
		}
		calls++
		http.Error(w, "temporary failure", http.StatusInternalServerError)
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	err = client.CreateSnapshot(context.Background(), &CreateSnapshotRequest{Name: "snap-a", SVM: "svm-a", Volume: "vol-a"})
	if err == nil {
		t.Fatalf("CreateSnapshot() error = nil, want failure")
	}
	if calls != 1 {
		t.Fatalf("POST calls = %d, want 1", calls)
	}
}

func TestCreateSVMRequestOmitsOptionalVLAN(t *testing.T) {
	payload, err := json.Marshal(&CreateSVMRequest{
		Name:   "k8s-default",
		IPCIDR: "192.168.10.5/32",
		MTU:    1500,
	})
	if err != nil {
		t.Fatalf("Marshal() error = %v", err)
	}
	if string(payload) != `{"name":"k8s-default","ip_cidr":"192.168.10.5/32","mtu":1500}` {
		t.Fatalf("payload = %s", payload)
	}
}

func TestClientMapsFastAPIResourceErrorDetails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"request_id":"req","status":"error","error":{"code":"NOT_FOUND","message":"Directory 'k8s-default/pvc-1234' not found","details":{"resource":"Directory","name":"k8s-default/pvc-1234"}}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	if err := client.DeleteDirectory(context.Background(), "k8s-default", "pvc-1234"); err != nil {
		t.Fatalf("DeleteDirectory() error = %v", err)
	}
}

func TestMapErrorCodeToErrorUsesStructuredResourceDetails(t *testing.T) {
	tests := []struct {
		name       string
		statusCode int
		code       string
		resource   string
		want       error
	}{
		{name: "svm not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "SVM", want: ErrSVMNotFound},
		{name: "directory not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "Directory", want: ErrDirectoryNotFound},
		{name: "volume not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "Volume", want: ErrVolumeNotFound},
		{name: "snapshot not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "Snapshot", want: ErrSnapshotNotFound},
		{name: "export not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "Export", want: ErrExportNotFound},
		{name: "quota not found", statusCode: http.StatusNotFound, code: "NOT_FOUND", resource: "Quota", want: ErrQuotaNotFound},
		{name: "svm already exists", statusCode: http.StatusConflict, code: "ALREADY_EXISTS", resource: "SVM", want: ErrSVMAlreadyExists},
		{name: "directory already exists", statusCode: http.StatusConflict, code: "ALREADY_EXISTS", resource: "Directory", want: ErrDirectoryAlreadyExists},
		{name: "volume already exists", statusCode: http.StatusConflict, code: "ALREADY_EXISTS", resource: "Volume", want: ErrVolumeAlreadyExists},
		{name: "snapshot already exists", statusCode: http.StatusConflict, code: "ALREADY_EXISTS", resource: "Snapshot", want: ErrSnapshotAlreadyExists},
		{name: "export already exists", statusCode: http.StatusConflict, code: "ALREADY_EXISTS", resource: "Export", want: ErrExportAlreadyExists},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := MapErrorCodeToError(tt.statusCode, &ArcaAPIError{
				Code:    tt.code,
				Message: tt.name,
				Details: map[string]interface{}{
					"resource": tt.resource,
				},
			})
			if !errors.Is(got, tt.want) {
				t.Fatalf("MapErrorCodeToError() = %v, want %v", got, tt.want)
			}
			if tt.code == "NOT_FOUND" && !IsNotFoundError(got) {
				t.Fatalf("IsNotFoundError(%v) = false", got)
			}
			if tt.code == "ALREADY_EXISTS" && !IsAlreadyExistsError(got) {
				t.Fatalf("IsAlreadyExistsError(%v) = false", got)
			}
			if !isNonRetryableError(got) {
				t.Fatalf("isNonRetryableError(%v) = false", got)
			}
		})
	}
}

func TestGetSVMCapacityDecodesFastAPIShape(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms/k8s-default/capacity" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"capacity":{"svm":"k8s-default","vg":"vg_pool_01","total_gb":100.0,"free_gb":25.5,"used_gb":74.5,"provisioned_gb":80.0}}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	capacity, err := client.GetSVMCapacity(context.Background(), "k8s-default")
	if err != nil {
		t.Fatalf("GetSVMCapacity() error = %v", err)
	}

	if capacity.TotalBytes != 100*1024*1024*1024 {
		t.Fatalf("TotalBytes = %d", capacity.TotalBytes)
	}
	if capacity.AvailableBytes != 255*1024*1024*1024/10 {
		t.Fatalf("AvailableBytes = %d", capacity.AvailableBytes)
	}
	if capacity.UsedBytes != 745*1024*1024*1024/10 {
		t.Fatalf("UsedBytes = %d", capacity.UsedBytes)
	}
}

func TestSnapshotRequestsUseFastAPIContract(t *testing.T) {
	var createBody map[string]interface{}
	var cloneBody map[string]interface{}
	var listQuery string
	var deleteQuery string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/snapshots":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &createBody); err != nil {
				t.Fatalf("create body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"snapshot":{"name":"abcd","svm":"k8s-default","volume":"pvc-1234"}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/snapshots":
			listQuery = r.URL.RawQuery
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[{"name":"abcd","svm":"k8s-default","volume":"pvc-1234","status":"Ready","created_at":"2026-01-01T00:00:00Z"}],"next_cursor":null}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/volumes/pvc-1234/clone":
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &cloneBody); err != nil {
				t.Fatalf("clone body unmarshal error = %v", err)
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"pvc-5678"}}}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/snapshots/abcd":
			deleteQuery = r.URL.RawQuery
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"deleted":true}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	ctx := context.Background()
	if err := client.CreateSnapshot(ctx, &CreateSnapshotRequest{Name: "abcd", SVM: "k8s-default", Volume: "pvc-1234"}); err != nil {
		t.Fatalf("CreateSnapshot() error = %v", err)
	}
	snapshots, err := client.ListSnapshots(ctx, "k8s-default", "pvc-1234", "abcd")
	if err != nil {
		t.Fatalf("ListSnapshots() error = %v", err)
	}
	if err := client.CloneVolumeFromSnapshot(ctx, &CloneVolumeFromSnapshotRequest{Name: "pvc-5678", SVM: "k8s-default", SourceVolume: "pvc-1234", Snapshot: "abcd", SizeGiB: 2}); err != nil {
		t.Fatalf("CloneVolumeFromSnapshot() error = %v", err)
	}
	if err := client.DeleteSnapshot(ctx, "abcd", "k8s-default", "pvc-1234"); err != nil {
		t.Fatalf("DeleteSnapshot() error = %v", err)
	}

	if createBody["name"] != "abcd" || createBody["svm"] != "k8s-default" || createBody["volume"] != "pvc-1234" {
		t.Fatalf("CreateSnapshot body = %#v", createBody)
	}
	if listQuery != "name=abcd&svm=k8s-default&volume=pvc-1234" {
		t.Fatalf("ListSnapshots query = %s", listQuery)
	}
	if len(snapshots) != 1 || snapshots[0].Name != "abcd" || snapshots[0].Status != "Ready" {
		t.Fatalf("ListSnapshots response = %#v", snapshots)
	}
	if cloneBody["name"] != "pvc-5678" || cloneBody["svm"] != "k8s-default" || cloneBody["snapshot"] != "abcd" || cloneBody["size_gib"].(float64) != 2 {
		t.Fatalf("CloneVolumeFromSnapshot body = %#v", cloneBody)
	}
	if _, ok := cloneBody["source_volume"]; ok {
		t.Fatalf("CloneVolumeFromSnapshot body leaked source_volume = %#v", cloneBody)
	}
	if deleteQuery != "svm=k8s-default&volume=pvc-1234" {
		t.Fatalf("DeleteSnapshot query = %s", deleteQuery)
	}
}

func TestCloneVolumeFromSnapshotReturnsAlreadyExistsConflict(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost || r.URL.Path != "/v1/volumes/pvc-source/clone" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"request_id":"req","status":"error","error":{"code":"ALREADY_EXISTS","message":"volume already exists","details":{"resource":"Volume"}}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	err = client.CloneVolumeFromSnapshot(context.Background(), &CloneVolumeFromSnapshotRequest{
		Name:         "pvc-target",
		SVM:          "k8s-default",
		SourceVolume: "pvc-source",
		Snapshot:     "snap-a",
	})

	if !errors.Is(err, ErrVolumeAlreadyExists) {
		t.Fatalf("CloneVolumeFromSnapshot() error = %v, want ErrVolumeAlreadyExists", err)
	}
}

func TestRestoreSnapshotUsesSourceVolumeInClonePath(t *testing.T) {
	var cloneBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost || r.URL.Path != "/v1/volumes/pvc-source/clone" {
			http.NotFound(w, r)
			return
		}

		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &cloneBody); err != nil {
			t.Fatalf("clone body unmarshal error = %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"volume":{"name":"pvc-target"}}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 0})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}

	if err := client.RestoreSnapshot(context.Background(), &RestoreSnapshotRequest{
		SVMName:      "k8s-default",
		SourceVolume: "pvc-source",
		SnapshotPath: "snap-a",
		TargetPath:   "pvc-target",
	}); err != nil {
		t.Fatalf("RestoreSnapshot() error = %v", err)
	}

	if cloneBody["name"] != "pvc-target" || cloneBody["svm"] != "k8s-default" || cloneBody["snapshot"] != "snap-a" {
		t.Fatalf("RestoreSnapshot clone body = %#v", cloneBody)
	}
	if _, ok := cloneBody["source_volume"]; ok {
		t.Fatalf("RestoreSnapshot clone body leaked source_volume = %#v", cloneBody)
	}
}

func TestRestoreSnapshotRequiresSourceVolume(t *testing.T) {
	var client Client

	err := client.RestoreSnapshot(context.Background(), &RestoreSnapshotRequest{
		SVMName:      "k8s-default",
		SnapshotPath: "snap-a",
		TargetPath:   "pvc-target",
	})
	if err == nil || err.Error() != "source volume is required" {
		t.Fatalf("RestoreSnapshot() error = %v", err)
	}
}
