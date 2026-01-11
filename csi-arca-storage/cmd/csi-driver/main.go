package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/klog/v2"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
	"github.com/akam1o/csi-arca-storage/pkg/config"
	"github.com/akam1o/csi-arca-storage/pkg/driver"
	"github.com/akam1o/csi-arca-storage/pkg/lock"
	"github.com/akam1o/csi-arca-storage/pkg/store"
)

var (
	configPath = flag.String("config", "/etc/csi-arca-storage/config.yaml", "Path to configuration file")
	mode       = flag.String("mode", "", "Driver mode: 'controller' or 'node' (required)")
	nodeID     = flag.String("node-id", "", "Node ID (required for node plugin)")
	kubeconfig = flag.String("kubeconfig", "", "Path to kubeconfig file (optional, uses in-cluster config if not specified)")
	version    = flag.Bool("version", false, "Print version information and exit")
)

func main() {
	klog.InitFlags(nil)
	flag.Parse()

	if *version {
		fmt.Printf("CSI ARCA Storage Driver\n")
		fmt.Printf("Version: %s\n", driver.DriverVersion)
		fmt.Printf("Driver Name: %s\n", driver.DriverName)
		os.Exit(0)
	}

	klog.Infof("Starting CSI ARCA Storage Driver version %s", driver.DriverVersion)

	// Validate mode flag
	if *mode == "" {
		klog.Fatal("--mode flag is required (must be 'controller' or 'node')")
	}
	if *mode != "controller" && *mode != "node" {
		klog.Fatalf("Invalid mode '%s': must be 'controller' or 'node'", *mode)
	}
	klog.Infof("Running in %s mode", *mode)

	// Load configuration
	cfg, err := config.LoadConfig(*configPath)
	if err != nil {
		klog.Fatalf("Failed to load configuration: %v", err)
	}

	// Validate configuration
	if err := cfg.Validate(); err != nil {
		klog.Fatalf("Invalid configuration: %v", err)
	}

	// Override node ID from command line if specified
	if *nodeID != "" {
		cfg.Driver.NodeID = *nodeID
	}

	// Validate mode consistency with node-id flag
	isControllerMode := (*mode == "controller")
	hasNodeID := (*nodeID != "" || cfg.Driver.NodeID != "")

	if isControllerMode && hasNodeID {
		klog.Fatal("Inconsistent configuration: controller mode requires node-id to be empty")
	}
	if !isControllerMode && !hasNodeID {
		klog.Fatal("Inconsistent configuration: node mode requires --node-id flag")
	}

	// Override CSI endpoint from environment if set (useful for deployment manifests)
	if envEndpoint := os.Getenv("CSI_ENDPOINT"); envEndpoint != "" {
		cfg.Driver.Endpoint = envEndpoint
	}

	klog.Infof("Configuration loaded successfully")
	klog.V(2).Infof("ARCA API endpoint: %s", cfg.ARCA.BaseURL)
	klog.V(2).Infof("CSI endpoint: %s", cfg.Driver.Endpoint)
	if cfg.Driver.NodeID != "" {
		klog.V(2).Infof("Node ID: %s", cfg.Driver.NodeID)
	}

	// Create Kubernetes client and config
	k8sConfig, k8sClient, err := createKubernetesClient(*kubeconfig)
	if err != nil {
		klog.Fatalf("Failed to create Kubernetes client: %v", err)
	}

	// Create ARCA API client
	arcaClient, err := arca.NewClient(cfg.ToArcaClientConfig())
	if err != nil {
		klog.Fatalf("Failed to create ARCA client: %v", err)
	}

	// Create network allocator
	poolConfigs := cfg.ToArcaPoolConfigs()
	allocator, err := arca.NewStandaloneAllocator(poolConfigs, arcaClient)
	if err != nil {
		klog.Fatalf("Failed to create network allocator: %v", err)
	}

	// Create lock manager
	// Use pod name for controller, node ID for node plugin
	lockIdentity := cfg.Driver.NodeID
	if lockIdentity == "" {
		// Controller mode - use pod name for unique identity
		lockIdentity = os.Getenv("POD_NAME")
		if lockIdentity == "" {
			// Fallback to hostname if POD_NAME not set
			lockIdentity, err = os.Hostname()
			if err != nil {
				klog.Fatalf("Failed to determine lock identity: %v", err)
			}
		}
		klog.V(2).Infof("Using lock identity (controller mode): %s", lockIdentity)
	}
	lockManager := lock.NewManager(k8sClient, "kube-system", lockIdentity)

	// Create SVM manager
	svmManager := arca.NewSVMManager(arcaClient, allocator, lockManager, cfg.Network.MTU)

	// Create metadata store (CRD-based with caching)
	var metadataStore store.Store
	if isControllerMode {
		// Controller mode: use persistent CRD store
		crdStore, err := store.NewCRDStore(k8sConfig, k8sClient)
		if err != nil {
			klog.Fatalf("Failed to create CRD store: %v", err)
		}

		// Wrap with cache for performance (60s TTL, 1000 volumes, 10000 snapshots)
		cachedStore, err := store.NewCachedStore(crdStore, 60*time.Second, 1000, 10000)
		if err != nil {
			klog.Fatalf("Failed to create cached store: %v", err)
		}

		metadataStore = cachedStore
		klog.Info("Using CRD-based persistent store with caching")
	} else {
		// Node mode: use in-memory store (not needed for node operations)
		metadataStore = store.NewMemoryStore()
		klog.Info("Using in-memory store (node mode)")
	}

	// Create driver
	driverCfg := &driver.DriverConfig{
		Name:          driver.DriverName,
		Version:       driver.DriverVersion,
		Mode:          *mode,
		NodeID:        cfg.Driver.NodeID,
		Endpoint:      cfg.Driver.Endpoint,
		ArcaClient:    arcaClient,
		SVMManager:    svmManager,
		Allocator:     allocator,
		K8sClient:     k8sClient,
		LockManager:   lockManager,
		Store:         metadataStore,
		StateFilePath: cfg.Driver.StateFilePath,
		BaseMountPath: cfg.Driver.BaseMountPath,
	}

	d, err := driver.NewDriver(driverCfg)
	if err != nil {
		klog.Fatalf("Failed to create driver: %v", err)
	}

	// Setup signal handling for graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	go func() {
		sig := <-sigCh
		klog.Infof("Received signal %v, initiating shutdown...", sig)
		cancel()
	}()

	// Run driver
	if err := d.Run(ctx); err != nil && err != context.Canceled {
		klog.Fatalf("Driver exited with error: %v", err)
	}

	klog.Info("Driver stopped")
}

// createKubernetesClient creates a Kubernetes clientset
func createKubernetesClient(kubeconfigPath string) (*rest.Config, *kubernetes.Clientset, error) {
	var config *rest.Config
	var err error

	if kubeconfigPath != "" {
		// Use kubeconfig file
		config, err = clientcmd.BuildConfigFromFlags("", kubeconfigPath)
		if err != nil {
			return nil, nil, fmt.Errorf("failed to build config from kubeconfig: %w", err)
		}
		klog.V(2).Infof("Using kubeconfig: %s", kubeconfigPath)
	} else {
		// Use in-cluster config
		config, err = rest.InClusterConfig()
		if err != nil {
			return nil, nil, fmt.Errorf("failed to get in-cluster config: %w", err)
		}
		klog.V(2).Info("Using in-cluster Kubernetes configuration")
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create clientset: %w", err)
	}

	return config, clientset, nil
}
