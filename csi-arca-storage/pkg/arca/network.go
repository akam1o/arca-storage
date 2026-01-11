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

	pool := &IPPool{
		Network: network,
		VLANID:  cfg.VLANID,
		Gateway: cfg.Gateway,
	}

	// Parse range if provided
	if cfg.Range != "" {
		firstIP, lastIP, err := parseIPRange(cfg.Range)
		if err != nil {
			return nil, fmt.Errorf("invalid range %s: %w", cfg.Range, err)
		}
		pool.FirstHost = firstIP
		pool.LastHost = lastIP
	} else {
		// Use entire network range (excluding network and broadcast)
		pool.FirstHost = incrementIP(network.IP, 1)
		pool.LastHost = lastIPInNetwork(network)
	}

	// Calculate number of hosts
	pool.NumHosts = ipDiff(pool.LastHost, pool.FirstHost) + 1
	if pool.NumHosts <= 0 {
		return nil, fmt.Errorf("invalid range: first IP must be <= last IP")
	}

	return pool, nil
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
	if firstIP == nil {
		return nil, nil, fmt.Errorf("invalid first IP: %s", firstStr)
	}

	lastIP := net.ParseIP(lastStr)
	if lastIP == nil {
		return nil, nil, fmt.Errorf("invalid last IP: %s", lastStr)
	}

	return firstIP.To4(), lastIP.To4(), nil
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

// ipDiff calculates the difference between two IPs
func ipDiff(ip1, ip2 net.IP) int {
	ipUint1 := uint32(ip1[0])<<24 | uint32(ip1[1])<<16 | uint32(ip1[2])<<8 | uint32(ip1[3])
	ipUint2 := uint32(ip2[0])<<24 | uint32(ip2[1])<<16 | uint32(ip2[2])<<8 | uint32(ip2[3])

	if ipUint1 > ipUint2 {
		return int(ipUint1 - ipUint2)
	}
	return int(ipUint2 - ipUint1)
}

// lastIPInNetwork returns the last usable IP in a network (excluding broadcast)
func lastIPInNetwork(network *net.IPNet) net.IP {
	// Get broadcast address
	broadcast := make(net.IP, len(network.IP))
	for i := range network.IP {
		broadcast[i] = network.IP[i] | ^network.Mask[i]
	}
	// Return broadcast - 1 (last usable host)
	return incrementIP(broadcast, -1)
}
