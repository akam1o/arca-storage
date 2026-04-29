package arca

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClientDecodesFastAPIEnvelopes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms/k8s-default":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/svms":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"svm":{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}}}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/svms":
			_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[{"name":"k8s-default","vlan_id":100,"ip_cidr":"192.168.10.5/24","vip":"192.168.10.5","gateway":"192.168.10.1","mtu":1500,"state":"ready","created_at":"2026-01-01T00:00:00Z"}],"next_cursor":null}}`))
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
	if svm.Name != "k8s-default" || svm.VIP != "192.168.10.5" {
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
