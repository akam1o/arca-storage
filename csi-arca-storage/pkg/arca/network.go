package arca

import (
	"context"
	"fmt"
	"math/rand"
	"net"
	"sync"
	"sync/atomic"

	"k8s.io/klog/v2"
)

// IPPool represents a pool of IP addresses
type IPPool struct {
	Network   *net.IPNet
	VLANID    int
	Gateway   string
	GatewayIP net.IP
	FirstHost net.IP
	LastHost  net.IP
	NumHosts  int
}

// StandaloneAllocator implements network allocation using static IP pools
type StandaloneAllocator struct {
	pools       []IPPool
	poolCounter int32
	arcaClient  *Client
	mu          sync.Mutex
}

// PoolConfig represents configuration for a single IP pool
type PoolConfig struct {
	CIDR    string `json:"cidr"`
	Range   string `json:"range"` // e.g., "192.168.100.10-192.168.100.200"
	VLANID  int    `json:"vlan"`
	Gateway string `json:"gateway"`
}

// NewStandaloneAllocator creates a new standalone network allocator
func NewStandaloneAllocator(pools []PoolConfig, arcaClient *Client) (*StandaloneAllocator, error) {
	if len(pools) == 0 {
		return nil, fmt.Errorf("no IP pools configured")
	}

	ipPools := make([]IPPool, 0, len(pools))

	for i, poolCfg := range pools {
		pool, err := parsePoolConfig(&poolCfg)
		if err != nil {
			return nil, fmt.Errorf("failed to parse pool %d: %w", i, err)
		}
		ipPools = append(ipPools, *pool)
		klog.V(2).Infof("Loaded IP pool: VLAN %d, network %s, range %s-%s (%d hosts)",
			pool.VLANID, pool.Network.String(), pool.FirstHost, pool.LastHost, pool.NumHosts)
	}

	return &StandaloneAllocator{
		pools:      ipPools,
		arcaClient: arcaClient,
	}, nil
}

// parsePoolConfig parses pool configuration into IPPool
func parsePoolConfig(cfg *PoolConfig) (*IPPool, error) {
	// Parse CIDR
	_, network, err := net.ParseCIDR(cfg.CIDR)
	if err != nil {
		return nil, fmt.Errorf("invalid CIDR %s: %w", cfg.CIDR, err)
	}
	network.IP = network.IP.To4()
	if network.IP == nil {
		return nil, fmt.Errorf("invalid CIDR %s: only IPv4 pools are supported", cfg.CIDR)
	}
	if cfg.VLANID != 0 && (cfg.VLANID < 1 || cfg.VLANID > 4094) {
		return nil, fmt.Errorf("invalid VLAN ID %d: must be 0 or between 1 and 4094", cfg.VLANID)
	}

	broadcast := broadcastIPInNetwork(network)
	gatewayIP, err := parseOptionalGateway(cfg.Gateway, network, broadcast)
	if err != nil {
		return nil, err
	}

	pool := &IPPool{
		Network:   network,
		VLANID:    cfg.VLANID,
		Gateway:   cfg.Gateway,
		GatewayIP: gatewayIP,
	}

	// Parse range if provided
	if cfg.Range != "" {
		firstIP, lastIP, err := parseIPRange(cfg.Range)
		if err != nil {
			return nil, fmt.Errorf("invalid range %s: %w", cfg.Range, err)
		}
		if !network.Contains(firstIP) || !network.Contains(lastIP) {
			return nil, fmt.Errorf("invalid range %s: range must be inside CIDR %s", cfg.Range, cfg.CIDR)
		}
		if firstIP.Equal(network.IP) || firstIP.Equal(broadcast) || lastIP.Equal(network.IP) || lastIP.Equal(broadcast) {
			return nil, fmt.Errorf("invalid range %s: range cannot include network or broadcast address", cfg.Range)
		}
		if compareIP(firstIP, lastIP) > 0 {
			return nil, fmt.Errorf("invalid range: first IP must be <= last IP")
		}
		pool.FirstHost = firstIP
		pool.LastHost = lastIP
	} else {
		// Use entire network range (excluding network and broadcast)
		pool.FirstHost = incrementIP(network.IP, 1)
		pool.LastHost = incrementIP(broadcast, -1)
	}

	// Calculate number of hosts
	pool.NumHosts = ipDiff(pool.LastHost, pool.FirstHost) + 1
	if pool.NumHosts <= 0 || compareIP(pool.FirstHost, pool.LastHost) > 0 {
		return nil, fmt.Errorf("invalid range: first IP must be <= last IP")
	}

	return pool, nil
}

func parseOptionalGateway(gateway string, network *net.IPNet, broadcast net.IP) (net.IP, error) {
	if gateway == "" {
		return nil, nil
	}
	gatewayIP := net.ParseIP(gateway)
	if gatewayIP == nil || gatewayIP.To4() == nil {
		return nil, fmt.Errorf("invalid gateway %s: must be an IPv4 address", gateway)
	}
	gatewayIP = gatewayIP.To4()
	if !network.Contains(gatewayIP) {
		return nil, fmt.Errorf("invalid gateway %s: gateway must be inside CIDR %s", gateway, network.String())
	}
	if gatewayIP.Equal(network.IP) || gatewayIP.Equal(broadcast) {
		return nil, fmt.Errorf("invalid gateway %s: gateway cannot be network or broadcast address", gateway)
	}
	return gatewayIP, nil
}

// parseIPRange parses an IP range string like "192.168.100.10-192.168.100.200"
func parseIPRange(rangeStr string) (net.IP, net.IP, error) {
	var firstStr, lastStr string
	for i := 0; i < len(rangeStr); i++ {
		if rangeStr[i] == '-' {
			firstStr = rangeStr[:i]
			lastStr = rangeStr[i+1:]
			break
		}
	}

	if firstStr == "" || lastStr == "" {
		return nil, nil, fmt.Errorf("invalid range format, expected 'IP1-IP2'")
	}

	firstIP := net.ParseIP(firstStr)
	if firstIP == nil || firstIP.To4() == nil {
		return nil, nil, fmt.Errorf("invalid first IP: %s", firstStr)
	}
	firstIP = firstIP.To4()

	lastIP := net.ParseIP(lastStr)
	if lastIP == nil || lastIP.To4() == nil {
		return nil, nil, fmt.Errorf("invalid last IP: %s", lastStr)
	}
	lastIP = lastIP.To4()

	return firstIP, lastIP, nil
}

// Allocate allocates an IP address from pools (round-robin with collision detection)
func (a *StandaloneAllocator) Allocate(ctx context.Context, namespace string, attempt int) (*NetworkAllocation, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	// Round-robin pool selection
	startIdx := int(atomic.LoadInt32(&a.poolCounter)) % len(a.pools)
	atomic.AddInt32(&a.poolCounter, 1)

	for i := 0; i < len(a.pools); i++ {
		poolIdx := (startIdx + i) % len(a.pools)
		pool := a.pools[poolIdx]

		klog.V(4).Infof("Attempting allocation from pool %d (VLAN %d), attempt %d", poolIdx, pool.VLANID, attempt)

		// Get used IPs in this VLAN
		usedIPs, err := a.getUsedIPsInVLAN(ctx, pool.VLANID)
		if err != nil {
			klog.Warningf("Failed to get used IPs for VLAN %d: %v", pool.VLANID, err)
			continue
		}

		// Find first free IP (with random offset on retry for collision avoidance)
		offset := 0
		if attempt > 0 {
			offset = rand.Intn(pool.NumHosts)
		}

		for j := 0; j < pool.NumHosts; j++ {
			ip := incrementIP(pool.FirstHost, (offset+j)%pool.NumHosts)
			if pool.GatewayIP != nil && ip.Equal(pool.GatewayIP) {
				continue
			}
			if !usedIPs[ip.String()] {
				// Found free IP
				ones, _ := pool.Network.Mask.Size()
				allocation := &NetworkAllocation{
					VLANID:  pool.VLANID,
					IPCIDR:  fmt.Sprintf("%s/%d", ip.String(), ones),
					Gateway: pool.Gateway,
				}
				klog.V(2).Infof("Allocated IP %s from VLAN %d for namespace %s", allocation.IPCIDR, pool.VLANID, namespace)
				return allocation, nil
			}
		}

		klog.V(4).Infof("Pool %d (VLAN %d) exhausted", poolIdx, pool.VLANID)
	}

	return nil, ErrAllPoolsExhausted
}

// getUsedIPsInVLAN queries ARCA API to get used IPs in a VLAN
func (a *StandaloneAllocator) getUsedIPsInVLAN(ctx context.Context, vlanID int) (map[string]bool, error) {
	svms, err := a.arcaClient.ListSVMs(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to list SVMs: %w", err)
	}

	usedIPs := make(map[string]bool)
	for _, svm := range svms {
		if svm.VLANID == vlanID && svm.VIP != "" {
			usedIPs[svm.VIP] = true
		}
	}

	return usedIPs, nil
}

// incrementIP increments an IP address by n
func incrementIP(ip net.IP, n int) net.IP {
	result := make(net.IP, len(ip))
	copy(result, ip)

	// Convert to uint32 for easier manipulation
	ipUint := uint32(result[0])<<24 | uint32(result[1])<<16 | uint32(result[2])<<8 | uint32(result[3])
	ipUint += uint32(n)

	result[0] = byte(ipUint >> 24)
	result[1] = byte(ipUint >> 16)
	result[2] = byte(ipUint >> 8)
	result[3] = byte(ipUint)

	return result
}

func ipToUint32(ip net.IP) uint32 {
	return uint32(ip[0])<<24 | uint32(ip[1])<<16 | uint32(ip[2])<<8 | uint32(ip[3])
}

func compareIP(ip1, ip2 net.IP) int {
	ipUint1 := ipToUint32(ip1)
	ipUint2 := ipToUint32(ip2)
	switch {
	case ipUint1 < ipUint2:
		return -1
	case ipUint1 > ipUint2:
		return 1
	default:
		return 0
	}
}

// ipDiff calculates the difference between two IPs
func ipDiff(ip1, ip2 net.IP) int {
	ipUint1 := ipToUint32(ip1)
	ipUint2 := ipToUint32(ip2)

	if ipUint1 > ipUint2 {
		return int(ipUint1 - ipUint2)
	}
	return int(ipUint2 - ipUint1)
}

// broadcastIPInNetwork returns the broadcast IP in a network.
func broadcastIPInNetwork(network *net.IPNet) net.IP {
	// Get broadcast address
	broadcast := make(net.IP, len(network.IP))
	for i := range network.IP {
		broadcast[i] = network.IP[i] | ^network.Mask[i]
	}
	return broadcast
}
