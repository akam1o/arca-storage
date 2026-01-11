package driver

import (
	"context"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/wrapperspb"
	"k8s.io/klog/v2"
)

// GetPluginInfo returns metadata about the plugin
func (d *Driver) GetPluginInfo(ctx context.Context, req *csi.GetPluginInfoRequest) (*csi.GetPluginInfoResponse, error) {
	klog.V(4).Infof("GetPluginInfo called")

	if d.name == "" {
		return nil, status.Error(codes.Unavailable, "driver name not configured")
	}

	if d.version == "" {
		return nil, status.Error(codes.Unavailable, "driver version not configured")
	}

	return &csi.GetPluginInfoResponse{
		Name:          d.name,
		VendorVersion: d.version,
	}, nil
}

// GetPluginCapabilities returns the capabilities of the plugin
func (d *Driver) GetPluginCapabilities(ctx context.Context, req *csi.GetPluginCapabilitiesRequest) (*csi.GetPluginCapabilitiesResponse, error) {
	klog.V(4).Infof("GetPluginCapabilities called")

	capabilities := make([]*csi.PluginCapability, 0, 1)
	if d.mode == "controller" {
		capabilities = append(capabilities, &csi.PluginCapability{
			Type: &csi.PluginCapability_Service_{
				Service: &csi.PluginCapability_Service{
					Type: csi.PluginCapability_Service_CONTROLLER_SERVICE,
				},
			},
		})
	}

	return &csi.GetPluginCapabilitiesResponse{
		Capabilities: capabilities,
	}, nil
}

// Probe checks if the plugin is running
func (d *Driver) Probe(ctx context.Context, req *csi.ProbeRequest) (*csi.ProbeResponse, error) {
	klog.V(4).Infof("Probe called")

	// Check if driver is ready
	if !d.ready {
		return &csi.ProbeResponse{
			Ready: &wrapperspb.BoolValue{Value: false},
		}, nil
	}

	return &csi.ProbeResponse{
		Ready: &wrapperspb.BoolValue{Value: true},
	}, nil
}
