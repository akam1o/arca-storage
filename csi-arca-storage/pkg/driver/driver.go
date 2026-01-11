package driver

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc"
	"k8s.io/client-go/kubernetes"
	"k8s.io/klog/v2"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
	"github.com/akam1o/csi-arca-storage/pkg/idempotency"
	"github.com/akam1o/csi-arca-storage/pkg/lock"
	"github.com/akam1o/csi-arca-storage/pkg/mount"
	"github.com/akam1o/csi-arca-storage/pkg/store"
)

// Driver implements the CSI Driver interface
type Driver struct {
	name    string
	version string
	mode    string // "controller" or "node"
	nodeID  string
	ready   bool

	// gRPC server
	srv      *grpc.Server
	endpoint string

	// ARCA components
	arcaClient *arca.Client
	svmManager *arca.SVMManager
	allocator  *arca.StandaloneAllocator

	// Mount management (for node service)
	mountManager *mount.MountManager
	nodeState    *mount.NodeState

	// Idempotency helpers
	volumeIDGen   *idempotency.VolumeIDGenerator
	snapshotIDGen *idempotency.SnapshotIDGenerator

	// Kubernetes client
	k8sClient *kubernetes.Clientset

	// Lock manager
	lockManager *lock.Manager

	// Metadata store
	store store.Store

	// CSI capabilities
	csi.UnimplementedIdentityServer
	csi.UnimplementedControllerServer
	csi.UnimplementedNodeServer
}

// DriverConfig holds configuration for the driver
type DriverConfig struct {
	Name          string
	Version       string
	Mode          string // "controller" or "node"
	NodeID        string
	Endpoint      string
	ArcaClient    *arca.Client
	SVMManager    *arca.SVMManager
	Allocator     *arca.StandaloneAllocator
	K8sClient     *kubernetes.Clientset
	LockManager   *lock.Manager
	Store         store.Store
	StateFilePath string
	BaseMountPath string
}

// NewDriver creates a new CSI driver
func NewDriver(cfg *DriverConfig) (*Driver, error) {
	if cfg.Name == "" {
		cfg.Name = DriverName
	}
	if cfg.Version == "" {
		cfg.Version = DriverVersion
	}

	// Initialize store if not provided
	storeInstance := cfg.Store
	if storeInstance == nil {
		storeInstance = store.NewMemoryStore()
	}

	d := &Driver{
		name:          cfg.Name,
		version:       cfg.Version,
		mode:          cfg.Mode,
		nodeID:        cfg.NodeID,
		endpoint:      cfg.Endpoint,
		arcaClient:    cfg.ArcaClient,
		svmManager:    cfg.SVMManager,
		allocator:     cfg.Allocator,
		k8sClient:     cfg.K8sClient,
		lockManager:   cfg.LockManager,
		store:         storeInstance,
		volumeIDGen:   idempotency.NewVolumeIDGenerator(),
		snapshotIDGen: idempotency.NewSnapshotIDGenerator(),
	}

	// Initialize node-specific components if this is a node plugin.
	// We treat "NodeID is set" as the authoritative signal for node mode.
	if cfg.NodeID != "" {
		stateFilePath := cfg.StateFilePath
		if stateFilePath == "" {
			stateFilePath = DefaultStateFilePath
		}

		// Initialize NodeState
		nodeState, err := mount.NewNodeState(stateFilePath)
		if err != nil {
			return nil, fmt.Errorf("failed to initialize node state: %w", err)
		}
		d.nodeState = nodeState

		// Initialize MountManager with NodeState reference
		baseMountPath := cfg.BaseMountPath
		if baseMountPath == "" {
			baseMountPath = DefaultBaseMountPath
		}

		mountManager, err := mount.NewMountManager(nodeState, baseMountPath)
		if err != nil {
			return nil, fmt.Errorf("failed to initialize mount manager: %w", err)
		}
		d.mountManager = mountManager

		klog.Infof("Node plugin initialized with state file: %s", stateFilePath)
	}

	return d, nil
}

// Run starts the CSI driver gRPC server
func (d *Driver) Run(ctx context.Context) error {
	// Parse endpoint
	u, err := url.Parse(d.endpoint)
	if err != nil {
		return fmt.Errorf("failed to parse endpoint: %w", err)
	}

	var addr string
	switch u.Scheme {
	case "unix":
		addr = u.Path
		// Remove existing socket file
		if err := os.Remove(addr); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove existing socket: %w", err)
		}
		// Ensure directory exists
		if err := os.MkdirAll(filepath.Dir(addr), 0750); err != nil {
			return fmt.Errorf("failed to create socket directory: %w", err)
		}
	case "tcp":
		addr = u.Host
	default:
		return fmt.Errorf("unsupported endpoint scheme: %s", u.Scheme)
	}

	// Create gRPC server
	d.srv = grpc.NewServer(
		grpc.UnaryInterceptor(d.logGRPC),
	)

	// Register CSI services based on mode
	csi.RegisterIdentityServer(d.srv, d)

	if d.mode == "controller" {
		csi.RegisterControllerServer(d.srv, d)
		klog.Info("Registered Identity and Controller services")
	} else if d.mode == "node" {
		csi.RegisterNodeServer(d.srv, d)
		klog.Info("Registered Identity and Node services")
	}

	// Create listener
	listener, err := net.Listen(u.Scheme, addr)
	if err != nil {
		return fmt.Errorf("failed to listen: %w", err)
	}

	klog.Infof("CSI driver %s (version %s) listening on %s", d.name, d.version, d.endpoint)

	// Mark driver as ready
	d.ready = true

	// Start serving
	errCh := make(chan error, 1)
	go func() {
		errCh <- d.srv.Serve(listener)
	}()

	// Wait for context cancellation or server error
	select {
	case <-ctx.Done():
		klog.Info("Shutting down CSI driver...")
		d.srv.GracefulStop()
		return ctx.Err()
	case err := <-errCh:
		return err
	}
}

// logGRPC is a gRPC interceptor for logging
func (d *Driver) logGRPC(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
	klog.V(3).Infof("gRPC call: %s", info.FullMethod)
	resp, err := handler(ctx, req)
	if err != nil {
		klog.Warningf("gRPC call %s failed: %v", info.FullMethod, err)
	}
	return resp, err
}
