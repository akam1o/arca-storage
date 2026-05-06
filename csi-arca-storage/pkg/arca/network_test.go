package arca

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestParsePoolConfigRejectsUnsafeRanges(t *testing.T) {
	tests := []struct {
		name    string
		cfg     PoolConfig
		wantErr string
	}{
		{
			name:    "outside cidr",
			cfg:     PoolConfig{CIDR: "10.0.0.0/24", Range: "10.0.1.10-10.0.1.20", VLANID: 100},
			wantErr: "inside CIDR",
		},
		{
			name:    "network address",
			cfg:     PoolConfig{CIDR: "10.0.0.0/24", Range: "10.0.0.0-10.0.0.20", VLANID: 100},
			wantErr: "network or broadcast",
		},
		{
			name:    "broadcast address",
			cfg:     PoolConfig{CIDR: "10.0.0.0/24", Range: "10.0.0.20-10.0.0.255", VLANID: 100},
			wantErr: "network or broadcast",
		},
		{
			name:    "gateway outside cidr",
			cfg:     PoolConfig{CIDR: "10.0.0.0/24", Range: "10.0.0.20-10.0.0.30", VLANID: 100, Gateway: "10.0.1.1"},
			wantErr: "gateway must be inside CIDR",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := parsePoolConfig(&tt.cfg)
			if err == nil {
				t.Fatalf("parsePoolConfig() expected error")
			}
			if !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("parsePoolConfig() error = %q, want substring %q", err, tt.wantErr)
			}
		})
	}
}

func TestStandaloneAllocatorSkipsGateway(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/svms" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"request_id":"req","status":"ok","data":{"items":[],"next_cursor":null}}`))
	}))
	defer server.Close()

	client, err := NewClient(&ClientConfig{BaseURL: server.URL, Timeout: time.Second, RetryCount: 1})
	if err != nil {
		t.Fatalf("NewClient() error = %v", err)
	}
	allocator, err := NewStandaloneAllocator(
		[]PoolConfig{{CIDR: "10.0.0.0/30", VLANID: 100, Gateway: "10.0.0.1"}},
		client,
	)
	if err != nil {
		t.Fatalf("NewStandaloneAllocator() error = %v", err)
	}

	allocation, err := allocator.Allocate(context.Background(), "default", 0)
	if err != nil {
		t.Fatalf("Allocate() error = %v", err)
	}
	if allocation.IPCIDR != "10.0.0.2/30" {
		t.Fatalf("Allocate() IPCIDR = %q, want 10.0.0.2/30", allocation.IPCIDR)
	}
}
