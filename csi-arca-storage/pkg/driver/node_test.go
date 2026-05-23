package driver

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/klog/v2"
	mountutils "k8s.io/mount-utils"

	arcamount "github.com/akam1o/csi-arca-storage/pkg/mount"
)

type fakeMountSourceValidator struct {
	err error
}

func (v fakeMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	return v.err
}

type mountSourceValidationCall struct {
	targetPath     string
	expectedSource string
}

type recordingMountSourceValidator struct {
	err   error
	errs  []error
	calls []mountSourceValidationCall
}

func (v *recordingMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	v.calls = append(v.calls, mountSourceValidationCall{
		targetPath:     targetPath,
		expectedSource: expectedSource,
	})
	if len(v.errs) >= len(v.calls) {
		return v.errs[len(v.calls)-1]
	}
	return v.err
}

type fakeNodeMountManager struct {
	mu            sync.Mutex
	mountPath     string
	mountPathErr  error
	ensureErr     error
	ensureStarted chan struct{}
	ensureRelease chan struct{}
	shouldUnmount bool
	ensureCalls   int
	getCalls      []string
	shouldCalls   []string
	unmountCalls  []string
}

func (m *fakeNodeMountManager) EnsureSVMMount(ctx context.Context, svmName, vip, exportRoot string, nfsMountOptions []string) (string, error) {
	m.mu.Lock()
	m.ensureCalls++
	ensureStarted := m.ensureStarted
	if ensureStarted != nil {
		m.ensureStarted = nil
	}
	ensureRelease := m.ensureRelease
	mountPath := m.mountPath
	ensureErr := m.ensureErr
	m.mu.Unlock()

	if ensureStarted != nil {
		close(ensureStarted)
	}
	if ensureRelease != nil {
		<-ensureRelease
	}

	if ensureErr != nil {
		return "", ensureErr
	}
	if mountPath == "" {
		return filepath.Join(os.TempDir(), svmName), nil
	}
	return mountPath, nil
}

func (m *fakeNodeMountManager) GetMountPath(svmName string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.getCalls = append(m.getCalls, svmName)
	if m.mountPathErr != nil {
		return "", m.mountPathErr
	}
	if m.mountPath == "" {
		return filepath.Join(os.TempDir(), svmName), nil
	}
	return m.mountPath, nil
}

func (m *fakeNodeMountManager) ShouldUnmountSVM(ctx context.Context, svmName string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.shouldCalls = append(m.shouldCalls, svmName)
	return m.shouldUnmount, nil
}

func (m *fakeNodeMountManager) UnmountSVM(ctx context.Context, svmName string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.unmountCalls = append(m.unmountCalls, svmName)
	return nil
}

type errorMounter struct {
	mountutils.Interface
	isLikelyNotMountPointErr error
	mountErr                 error
	mountErrOnCall           int
	mountCalls               int
	unmountErr               error
}

func (m *errorMounter) IsLikelyNotMountPoint(file string) (bool, error) {
	if m.isLikelyNotMountPointErr != nil {
		return false, m.isLikelyNotMountPointErr
	}
	return m.Interface.IsLikelyNotMountPoint(file)
}

func (m *errorMounter) Mount(source, target, fstype string, options []string) error {
	m.mountCalls++
	if m.mountErr != nil && (m.mountErrOnCall == 0 || m.mountCalls == m.mountErrOnCall) {
		return m.mountErr
	}
	return m.Interface.Mount(source, target, fstype, options)
}

func (m *errorMounter) Unmount(target string) error {
	if m.unmountErr != nil {
		return m.unmountErr
	}
	return m.Interface.Unmount(target)
}

func testMountCapability() *csi.VolumeCapability {
	return &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
		AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
	}
}

func testBlockCapability() *csi.VolumeCapability {
	return &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Block{Block: &csi.VolumeCapability_BlockVolume{}},
		AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
	}
}

func assertErrorOmits(t *testing.T, err error, values ...string) {
	t.Helper()

	rendered := err.Error()
	for _, value := range values {
		if value == "" {
			continue
		}
		if strings.Contains(rendered, value) {
			t.Fatalf("error %q contains %q", rendered, value)
		}
	}
}

func assertInternalErrorOmits(t *testing.T, err error, wantMessage string, values ...string) {
	t.Helper()

	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal, got %v", err)
	}
	if !strings.Contains(err.Error(), wantMessage) {
		t.Fatalf("error = %v, want %q", err, wantMessage)
	}
	assertErrorOmits(t, err, values...)
}

func captureKlogOutput(t *testing.T, fn func()) string {
	t.Helper()

	state := klog.CaptureState()
	defer state.Restore()

	var fs flag.FlagSet
	klog.InitFlags(&fs)
	for name, value := range map[string]string{
		"alsologtostderr": "false",
		"logtostderr":     "false",
		"one_output":      "true",
		"skip_headers":    "true",
		"v":               "4",
	} {
		if err := fs.Set(name, value); err != nil {
			t.Fatalf("failed to set klog flag %s: %v", name, err)
		}
	}

	var out bytes.Buffer
	klog.SetOutput(&out)
	fn()
	klog.Flush()
	return out.String()
}

func TestNodeLogHelpersIncludeRedactedErrorDetails(t *testing.T) {
	err := fmt.Errorf(
		"mount failed for 10.0.0.1:/exports/team at /var/lib/kubelet/pods/x: Authorization: Bearer secret-token token=another-secret",
	)

	logOutput := captureKlogOutput(t, func() {
		_ = nodeInternalError("failed to bind mount", err)
		nodeLogWarning("Failed to rollback node mount", err)
		nodeLogError("Failed to clean node mount", err)
	})

	for _, want := range []string{"mount failed", "failed to bind mount", "<redacted>", "<nfs-source>", "<path>"} {
		if !strings.Contains(logOutput, want) {
			t.Fatalf("node logs %q do not contain %q", logOutput, want)
		}
	}
	for _, forbidden := range []string{
		"10.0.0.1",
		"/exports/team",
		"/var/lib/kubelet/pods/x",
		"secret-token",
		"another-secret",
	} {
		if strings.Contains(logOutput, forbidden) {
			t.Fatalf("node logs %q contain %q", logOutput, forbidden)
		}
	}
}

func TestNFSMountOptionsFromCapabilityUsesMountFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nfsvers=4.1", "rsize=262144", "ro", "bind", "remount", "rw", "soft", "resvport"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.1", "rsize=262144", "wsize=1048576", "soft", "timeo=600", "retrans=2", "resvport"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityMergesCustomFlagsWithDefaults(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nconnect=8"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.2", "rsize=1048576", "wsize=1048576", "hard", "timeo=600", "retrans=2", "noresvport", "nconnect=8"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityDefaultsWhenNoFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	if len(got) == 0 {
		t.Fatal("expected default NFS mount options")
	}
}

func TestBindMountOptionsStayLocalOnly(t *testing.T) {
	if got, want := bindMountOptions(), []string{"bind"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected bind mount options: got %v want %v", got, want)
	}

	if got, want := readonlyBindRemountOptions(), []string{"bind", "remount", "ro"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected read-only remount options: got %v want %v", got, want)
	}
}

func TestValidateSVMNameRejectsPathTraversal(t *testing.T) {
	for _, name := range []string{"../tenant", "tenant/a", "tenant a", "-tenant"} {
		if err := validateSVMName(name); err == nil {
			t.Fatalf("expected invalid SVM name %q to be rejected", name)
		}
	}

	if err := validateSVMName("tenant-a_1.example"); err != nil {
		t.Fatalf("expected valid SVM name to be accepted: %v", err)
	}
}

func TestValidateVolumePathRejectsRootAliasesAndTraversal(t *testing.T) {
	for _, path := range []string{"", ".", "./", "dir/..", "dir/../vol", "dir/.", "dir//vol", "/vol"} {
		if err := validateVolumePath(path); err == nil {
			t.Fatalf("expected invalid volume path %q to be rejected", path)
		}
	}

	for _, path := range []string{"pvc-1234", "volumes/pvc-1234"} {
		if err := validateVolumePath(path); err != nil {
			t.Fatalf("expected valid volume path %q to be accepted: %v", path, err)
		}
	}
}

func TestNodeGetVolumeStatsReturnsFilesystemUsage(t *testing.T) {
	tmp := t.TempDir()
	volumePath := filepath.Join(tmp, "volume")
	if err := os.MkdirAll(volumePath, 0750); err != nil {
		t.Fatalf("failed to create volume path: %v", err)
	}
	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: new(arcamount.MountManager),
	}

	resp, err := driver.NodeGetVolumeStats(context.Background(), &csi.NodeGetVolumeStatsRequest{
		VolumeId:   "vol-a",
		VolumePath: volumePath,
	})
	if err != nil {
		t.Fatalf("NodeGetVolumeStats() error = %v", err)
	}

	var bytesUsage, inodesUsage *csi.VolumeUsage
	for _, usage := range resp.GetUsage() {
		switch usage.GetUnit() {
		case csi.VolumeUsage_BYTES:
			bytesUsage = usage
		case csi.VolumeUsage_INODES:
			inodesUsage = usage
		}
	}
	if bytesUsage == nil {
		t.Fatal("bytes usage entry not returned")
	}
	if bytesUsage.GetTotal() <= 0 {
		t.Fatalf("bytes total = %d, want > 0", bytesUsage.GetTotal())
	}
	if bytesUsage.GetAvailable() < 0 || bytesUsage.GetAvailable() > bytesUsage.GetTotal() {
		t.Fatalf("bytes available = %d, total = %d", bytesUsage.GetAvailable(), bytesUsage.GetTotal())
	}
	if inodesUsage == nil {
		t.Fatal("inodes usage entry not returned")
	}
	if inodesUsage.GetTotal() <= 0 {
		t.Fatalf("inodes total = %d, want > 0", inodesUsage.GetTotal())
	}
	if inodesUsage.GetAvailable() < 0 || inodesUsage.GetAvailable() > inodesUsage.GetTotal() {
		t.Fatalf("inodes available = %d, total = %d", inodesUsage.GetAvailable(), inodesUsage.GetTotal())
	}
}

func TestStatfsBlocksToBytesRejectsOverflow(t *testing.T) {
	_, err := statfsBlocksToBytes(uint64(1<<63), 2, "total bytes")
	if err == nil {
		t.Fatal("statfsBlocksToBytes() error = nil, want overflow error")
	}
	if !strings.Contains(err.Error(), "total bytes exceeds int64 range") {
		t.Fatalf("statfsBlocksToBytes() error = %v, want overflow message", err)
	}
}

func TestStatfsValueToInt64RejectsOverflow(t *testing.T) {
	_, err := statfsValueToInt64(uint64(1<<63), "total inodes")
	if err == nil {
		t.Fatal("statfsValueToInt64() error = nil, want overflow error")
	}
	if !strings.Contains(err.Error(), "total inodes exceeds int64 range") {
		t.Fatalf("statfsValueToInt64() error = %v, want overflow message", err)
	}
}

func TestNodeStageSerializesSVMMountLifecycle(t *testing.T) {
	tmp := t.TempDir()
	ensureStarted := make(chan struct{})
	ensureRelease := make(chan struct{})
	released := false
	defer func() {
		if !released {
			close(ensureRelease)
		}
	}()

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	mountManager := &fakeNodeMountManager{
		mountPath:     filepath.Join(tmp, "svm-mount"),
		ensureStarted: ensureStarted,
		ensureRelease: ensureRelease,
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: mountManager,
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	stage := func(volumeID, stagingTargetPath string) error {
		_, err := driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
			VolumeId:          volumeID,
			StagingTargetPath: stagingTargetPath,
			VolumeCapability:  testMountCapability(),
			VolumeContext: map[string]string{
				volumeContextSVM:        "svm-a",
				volumeContextVIP:        "10.0.0.1",
				volumeContextVolumePath: filepath.Join("volumes", volumeID),
			},
		})
		return err
	}

	errCh := make(chan error, 2)
	go func() {
		errCh <- stage("vol-a", filepath.Join(tmp, "stage-a"))
	}()

	select {
	case <-ensureStarted:
	case <-time.After(2 * time.Second):
		t.Fatal("first stage did not reach EnsureSVMMount")
	}

	secondStarted := make(chan struct{})
	go func() {
		close(secondStarted)
		errCh <- stage("vol-b", filepath.Join(tmp, "stage-b"))
	}()
	<-secondStarted

	time.Sleep(50 * time.Millisecond)
	mountManager.mu.Lock()
	ensureCalls := mountManager.ensureCalls
	mountManager.mu.Unlock()
	if ensureCalls != 1 {
		t.Fatalf("second stage reached EnsureSVMMount before first stage finished: calls=%d", ensureCalls)
	}

	close(ensureRelease)
	released = true

	for i := 0; i < 2; i++ {
		select {
		case err := <-errCh:
			if err != nil {
				t.Fatalf("NodeStageVolume failed: %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("NodeStageVolume did not finish")
		}
	}
	mountManager.mu.Lock()
	ensureCalls = mountManager.ensureCalls
	mountManager.mu.Unlock()
	if ensureCalls != 2 {
		t.Fatalf("EnsureSVMMount calls = %d, want 2", ensureCalls)
	}
	if got := nodeState.CountStagedVolumesForSVM("svm-a"); got != 2 {
		t.Fatalf("staged volumes for svm-a = %d, want 2", got)
	}
}

func TestNodeOperationLogsDoNotEchoHostPaths(t *testing.T) {
	tmp := t.TempDir()
	stagingPath := filepath.Join(tmp, "secret-staging-path")
	targetPath := filepath.Join(tmp, "secret-target-path")
	svmMountPath := filepath.Join(tmp, "secret-svm-mount")
	volumePath := "volumes/secret-volume-path"
	vip := "10.0.0.1"

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         &fakeNodeMountManager{mountPath: svmMountPath, shouldUnmount: true},
		nodeMounter:          mountutils.NewFakeMounter(nil),
		mountSourceValidator: fakeMountSourceValidator{},
	}

	logOutput := captureKlogOutput(t, func() {
		_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
			VolumeId:          "vol-a",
			StagingTargetPath: stagingPath,
			VolumeCapability:  testMountCapability(),
			VolumeContext: map[string]string{
				volumeContextSVM:        "svm-a",
				volumeContextVIP:        vip,
				volumeContextVolumePath: volumePath,
			},
		})
		if err != nil {
			t.Fatalf("NodeStageVolume() error = %v", err)
		}

		_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
			VolumeId:          "vol-a",
			StagingTargetPath: stagingPath,
			TargetPath:        targetPath,
			VolumeCapability:  testMountCapability(),
		})
		if err != nil {
			t.Fatalf("NodePublishVolume() error = %v", err)
		}

		_, err = driver.NodeUnpublishVolume(context.Background(), &csi.NodeUnpublishVolumeRequest{
			VolumeId:   "vol-a",
			TargetPath: targetPath,
		})
		if err != nil {
			t.Fatalf("NodeUnpublishVolume() error = %v", err)
		}

		_, err = driver.NodeUnstageVolume(context.Background(), &csi.NodeUnstageVolumeRequest{
			VolumeId:          "vol-a",
			StagingTargetPath: stagingPath,
		})
		if err != nil {
			t.Fatalf("NodeUnstageVolume() error = %v", err)
		}
	})

	for _, value := range []string{
		stagingPath,
		targetPath,
		svmMountPath,
		filepath.Join(svmMountPath, volumePath),
		volumePath,
		vip,
	} {
		if strings.Contains(logOutput, value) {
			t.Fatalf("node operation logs %q contain %q", logOutput, value)
		}
	}
}

func TestNodeStageCleansUpSVMMountWhenStagingDirectoryCreationFails(t *testing.T) {
	tmp := t.TempDir()
	blockingFile := filepath.Join(tmp, "not-a-directory")
	if err := os.WriteFile(blockingFile, []byte("blocked"), 0600); err != nil {
		t.Fatalf("failed to create blocking file: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	mountManager := &fakeNodeMountManager{
		mountPath:     filepath.Join(tmp, "svm-mount"),
		shouldUnmount: true,
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: mountManager,
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: filepath.Join(blockingFile, "stage"),
		VolumeCapability:  testMountCapability(),
		VolumeContext: map[string]string{
			volumeContextSVM:        "svm-a",
			volumeContextVIP:        "10.0.0.1",
			volumeContextVolumePath: "volumes/vol-a",
		},
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal, got %v", err)
	}
	assertErrorOmits(t, err, blockingFile)
	if mountManager.ensureCalls != 1 {
		t.Fatalf("EnsureSVMMount calls = %d, want 1", mountManager.ensureCalls)
	}
	if !reflect.DeepEqual(mountManager.shouldCalls, []string{"svm-a"}) {
		t.Fatalf("ShouldUnmountSVM calls = %#v", mountManager.shouldCalls)
	}
	if !reflect.DeepEqual(mountManager.unmountCalls, []string{"svm-a"}) {
		t.Fatalf("UnmountSVM calls = %#v", mountManager.unmountCalls)
	}
}

func TestNodeStageInternalErrorsDoNotEchoDetails(t *testing.T) {
	const secret = "secret-node-stage-detail"

	tests := []struct {
		name        string
		mountMgr    *fakeNodeMountManager
		nodeMounter mountutils.Interface
		wantMessage string
	}{
		{
			name:        "ensure svm mount",
			mountMgr:    &fakeNodeMountManager{ensureErr: fmt.Errorf("ensure failed: %s", secret)},
			nodeMounter: mountutils.NewFakeMounter(nil),
			wantMessage: "failed to ensure SVM mount",
		},
		{
			name:        "check mount point",
			mountMgr:    &fakeNodeMountManager{},
			nodeMounter: &errorMounter{Interface: mountutils.NewFakeMounter(nil), isLikelyNotMountPointErr: fmt.Errorf("check failed: %s", secret)},
			wantMessage: "failed to check mount point",
		},
		{
			name:        "bind mount",
			mountMgr:    &fakeNodeMountManager{},
			nodeMounter: &errorMounter{Interface: mountutils.NewFakeMounter(nil), mountErr: fmt.Errorf("mount failed: %s", secret)},
			wantMessage: "failed to bind mount",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tmp := t.TempDir()
			nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
			if err != nil {
				t.Fatalf("failed to create node state: %v", err)
			}
			driver := &Driver{
				mode:         "node",
				nodeID:       "node-a",
				nodeState:    nodeState,
				mountManager: tt.mountMgr,
				nodeMounter:  tt.nodeMounter,
			}

			_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
				VolumeId:          "vol-a",
				StagingTargetPath: filepath.Join(tmp, "stage"),
				VolumeCapability:  testMountCapability(),
				VolumeContext: map[string]string{
					volumeContextSVM:        "svm-a",
					volumeContextVIP:        "10.0.0.1",
					volumeContextVolumePath: "volumes/vol-a",
				},
			})
			assertInternalErrorOmits(t, err, tt.wantMessage, secret)
		})
	}
}

func TestNodeStageExistingMountSourceMismatchDoesNotEchoPaths(t *testing.T) {
	tmp := t.TempDir()
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	volumePath := "volumes/vol-a"
	sourcePath := filepath.Join(svmMountPath, volumePath)
	staleSource := filepath.Join(tmp, "stale-volume")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	validator := fakeMountSourceValidator{
		err: fmt.Errorf("mount source mismatch: active=%s requested=%s", staleSource, sourcePath),
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: staleSource, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
		mountSourceValidator: validator,
	}

	_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		VolumeCapability:  testMountCapability(),
		VolumeContext: map[string]string{
			volumeContextSVM:        "svm-a",
			volumeContextVIP:        "10.0.0.1",
			volumeContextVolumePath: volumePath,
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, stagingPath, staleSource, sourcePath)
	if nodeState.CountStagedVolumesForSVM("svm-a") != 0 {
		t.Fatal("staging should not be recorded after source mismatch")
	}
}

func TestNodeStageInvalidVolumeContextDoesNotEchoInput(t *testing.T) {
	tests := []struct {
		name      string
		key       string
		value     string
		wantCause string
	}{
		{
			name:      "svm",
			key:       volumeContextSVM,
			value:     "secret-svm/../tenant",
			wantCause: "invalid SVM name",
		},
		{
			name:      "vip",
			key:       volumeContextVIP,
			value:     "secret-vip-value",
			wantCause: "invalid VIP",
		},
		{
			name:      "export root",
			key:       volumeContextExportRoot,
			value:     "secret-export-root",
			wantCause: "invalid export root",
		},
		{
			name:      "volume path",
			key:       volumeContextVolumePath,
			value:     "secret-volume-path/..",
			wantCause: "invalid volume path",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tmp := t.TempDir()
			nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
			if err != nil {
				t.Fatalf("failed to create node state: %v", err)
			}
			mountManager := &fakeNodeMountManager{mountPath: filepath.Join(tmp, "svm-mount")}
			driver := &Driver{
				mode:         "node",
				nodeID:       "node-a",
				nodeState:    nodeState,
				mountManager: mountManager,
				nodeMounter:  mountutils.NewFakeMounter(nil),
			}
			volumeContext := map[string]string{
				volumeContextSVM:        "svm-a",
				volumeContextVIP:        "10.0.0.1",
				volumeContextVolumePath: "volumes/vol-a",
			}
			volumeContext[tt.key] = tt.value

			_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
				VolumeId:          "vol-a",
				StagingTargetPath: filepath.Join(tmp, "stage"),
				VolumeCapability:  testMountCapability(),
				VolumeContext:     volumeContext,
			})
			if status.Code(err) != codes.InvalidArgument {
				t.Fatalf("expected InvalidArgument, got %v", err)
			}
			if !strings.Contains(err.Error(), tt.wantCause) {
				t.Fatalf("NodeStageVolume() error = %v, want %q", err, tt.wantCause)
			}
			if strings.Contains(err.Error(), tt.value) {
				t.Fatalf("NodeStageVolume() error %q contains invalid input", err)
			}
			if mountManager.ensureCalls != 0 {
				t.Fatalf("EnsureSVMMount calls = %d, want 0", mountManager.ensureCalls)
			}
		})
	}
}

func TestNodeStageRejectsBlockCapability(t *testing.T) {
	tmp := t.TempDir()
	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	mountManager := &fakeNodeMountManager{mountPath: filepath.Join(tmp, "svm-mount")}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: mountManager,
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: filepath.Join(tmp, "stage"),
		VolumeCapability:  testBlockCapability(),
		VolumeContext: map[string]string{
			volumeContextSVM:        "svm-a",
			volumeContextVIP:        "10.0.0.1",
			volumeContextVolumePath: "volumes/vol-a",
		},
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected InvalidArgument, got %v", err)
	}
	if !strings.Contains(err.Error(), "block access type is not supported") {
		t.Fatalf("unexpected error: %v", err)
	}
	if mountManager.ensureCalls != 0 {
		t.Fatalf("EnsureSVMMount calls = %d, want 0", mountManager.ensureCalls)
	}
}

func TestNodeUnstageUsesFreshNodeStateForSVMCleanup(t *testing.T) {
	tmp := t.TempDir()
	stateFile := filepath.Join(tmp, "state.json")
	writer, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create writer node state: %v", err)
	}
	reader, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create reader node state: %v", err)
	}

	stagingPath := filepath.Join(tmp, "stage")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}
	if err := writer.RecordVolumeStaging(
		"vol-a",
		"svm-a",
		"10.0.0.1",
		"",
		"volumes/vol-a",
		stagingPath,
		nil,
	); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	mountManager := &fakeNodeMountManager{shouldUnmount: true}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    reader,
		mountManager: mountManager,
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: "/svm/volumes/vol-a", Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
	}

	_, err = driver.NodeUnstageVolume(context.Background(), &csi.NodeUnstageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
	})
	if err != nil {
		t.Fatalf("NodeUnstageVolume failed: %v", err)
	}
	if !reflect.DeepEqual(mountManager.shouldCalls, []string{"svm-a"}) {
		t.Fatalf("ShouldUnmountSVM calls = %#v", mountManager.shouldCalls)
	}
	if !reflect.DeepEqual(mountManager.unmountCalls, []string{"svm-a"}) {
		t.Fatalf("UnmountSVM calls = %#v", mountManager.unmountCalls)
	}

	reloaded, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to reload node state: %v", err)
	}
	if got := reloaded.CountStagedVolumesForSVM("svm-a"); got != 0 {
		t.Fatalf("staged volumes for svm-a = %d, want 0", got)
	}
}

func TestNodeUnstageInternalErrorsDoNotEchoDetails(t *testing.T) {
	const secret = "secret-node-unstage-detail"

	tests := []struct {
		name        string
		nodeMounter mountutils.Interface
		wantMessage string
	}{
		{
			name: "check mount point",
			nodeMounter: &errorMounter{
				Interface:                mountutils.NewFakeMounter(nil),
				isLikelyNotMountPointErr: fmt.Errorf("check failed: %s", secret),
			},
			wantMessage: "failed to check mount point",
		},
		{
			name: "unmount",
			nodeMounter: &errorMounter{
				Interface: mountutils.NewFakeMounter([]mountutils.MountPoint{
					{Device: "/svm/volumes/vol-a", Path: "/stage", Type: "", Opts: []string{"bind"}},
				}),
				unmountErr: fmt.Errorf("unmount failed: %s", secret),
			},
			wantMessage: "failed to unmount",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tmp := t.TempDir()
			stagingPath := filepath.Join(tmp, "stage")
			if err := os.MkdirAll(stagingPath, 0750); err != nil {
				t.Fatalf("failed to create staging path: %v", err)
			}
			if tt.name == "unmount" {
				mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
				if err != nil {
					t.Fatalf("failed to resolve staging path: %v", err)
				}
				tt.nodeMounter = &errorMounter{
					Interface: mountutils.NewFakeMounter([]mountutils.MountPoint{
						{Device: "/svm/volumes/vol-a", Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
					}),
					unmountErr: fmt.Errorf("unmount failed: %s", secret),
				}
			}
			nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
			if err != nil {
				t.Fatalf("failed to create node state: %v", err)
			}
			driver := &Driver{
				mode:         "node",
				nodeID:       "node-a",
				nodeState:    nodeState,
				mountManager: new(arcamount.MountManager),
				nodeMounter:  tt.nodeMounter,
			}

			_, err = driver.NodeUnstageVolume(context.Background(), &csi.NodeUnstageVolumeRequest{
				VolumeId:          "vol-a",
				StagingTargetPath: stagingPath,
			})
			assertInternalErrorOmits(t, err, tt.wantMessage, secret, stagingPath)
		})
	}
}

func TestNodePublishRejectsUnrecordedExistingMount(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "not recorded for volume") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, targetPath, stagingPath, mountedTargetPath, mountedStagingPath)
}

func TestNodePublishRejectsBlockCapability(t *testing.T) {
	tmp := t.TempDir()
	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: new(arcamount.MountManager),
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: filepath.Join(tmp, "stage"),
		TargetPath:        filepath.Join(tmp, "target"),
		VolumeCapability:  testBlockCapability(),
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("expected InvalidArgument, got %v", err)
	}
	if !strings.Contains(err.Error(), "block access type is not supported") {
		t.Fatalf("unexpected error: %v", err)
	}
	if nodeState.HasVolumePublish("vol-a", filepath.Join(tmp, "target")) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishInternalErrorsDoNotEchoDetails(t *testing.T) {
	const secret = "secret-node-publish-detail"

	tests := []struct {
		name        string
		readOnly    bool
		nodeMounter func(mountedStagingPath string) mountutils.Interface
		wantMessage string
	}{
		{
			name: "check target mount point",
			nodeMounter: func(mountedStagingPath string) mountutils.Interface {
				return &errorMounter{
					Interface:                mountutils.NewFakeMounter(nil),
					isLikelyNotMountPointErr: fmt.Errorf("check failed: %s", secret),
				}
			},
			wantMessage: "failed to check mount point",
		},
		{
			name: "bind mount",
			nodeMounter: func(mountedStagingPath string) mountutils.Interface {
				return &errorMounter{
					Interface: mountutils.NewFakeMounter([]mountutils.MountPoint{
						{Device: "/svm/volumes/vol-a", Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
					}),
					mountErr: fmt.Errorf("mount failed: %s", secret),
				}
			},
			wantMessage: "failed to bind mount",
		},
		{
			name:     "readonly remount",
			readOnly: true,
			nodeMounter: func(mountedStagingPath string) mountutils.Interface {
				return &errorMounter{
					Interface: mountutils.NewFakeMounter([]mountutils.MountPoint{
						{Device: "/svm/volumes/vol-a", Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
					}),
					mountErr:       fmt.Errorf("remount failed: %s", secret),
					mountErrOnCall: 2,
				}
			},
			wantMessage: "failed to remount as read-only",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tmp := t.TempDir()
			stagingPath := filepath.Join(tmp, "staging")
			targetPath := filepath.Join(tmp, "target")
			if err := os.MkdirAll(stagingPath, 0750); err != nil {
				t.Fatalf("failed to create staging path: %v", err)
			}
			mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
			if err != nil {
				t.Fatalf("failed to resolve staging path: %v", err)
			}

			nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
			if err != nil {
				t.Fatalf("failed to create node state: %v", err)
			}
			if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
				t.Fatalf("failed to record staging: %v", err)
			}
			driver := &Driver{
				mode:                 "node",
				nodeID:               "node-a",
				nodeState:            nodeState,
				mountManager:         &fakeNodeMountManager{mountPath: filepath.Join(tmp, "svm")},
				nodeMounter:          tt.nodeMounter(mountedStagingPath),
				mountSourceValidator: fakeMountSourceValidator{},
			}

			_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
				VolumeId:          "vol-a",
				StagingTargetPath: stagingPath,
				TargetPath:        targetPath,
				Readonly:          tt.readOnly,
				VolumeCapability:  testMountCapability(),
			})
			assertInternalErrorOmits(t, err, tt.wantMessage, secret, stagingPath, targetPath)
		})
	}
}

func TestNodePublishRejectsFirstPublishWithMismatchedStagingPath(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	recordedStagingPath := filepath.Join(tmp, "recorded-staging")
	requestedStagingPath := filepath.Join(tmp, "requested-staging")
	if err := os.MkdirAll(recordedStagingPath, 0750); err != nil {
		t.Fatalf("failed to create recorded staging path: %v", err)
	}
	if err := os.MkdirAll(requestedStagingPath, 0750); err != nil {
		t.Fatalf("failed to create requested staging path: %v", err)
	}
	mountedRequestedStagingPath, err := filepath.EvalSymlinks(requestedStagingPath)
	if err != nil {
		t.Fatalf("failed to resolve requested staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", recordedStagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: "/svm/volumes/vol-a", Path: mountedRequestedStagingPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: requestedStagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "staging path mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, recordedStagingPath, requestedStagingPath)
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsFirstPublishWhenStagingPathIsNotMounted(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          mountutils.NewFakeMounter(nil),
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "is not mounted") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, stagingPath)
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsFirstPublishWithWrongStagingSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	volumePath := "volumes/vol-a"
	staleSource := filepath.Join(tmp, "stale-volume")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", volumePath, stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	expectedSource := filepath.Join(svmMountPath, volumePath)
	validator := &recordingMountSourceValidator{
		err: fmt.Errorf("mount source mismatch: active=%s requested=%s", staleSource, expectedSource),
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: staleSource, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
		mountSourceValidator: validator,
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "does not match recorded source") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, stagingPath, staleSource, expectedSource)
	if !reflect.DeepEqual(validator.calls, []mountSourceValidationCall{{targetPath: stagingPath, expectedSource: expectedSource}}) {
		t.Fatalf("unexpected source validation calls: %#v", validator.calls)
	}
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsExistingMountWithDifferentSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}
	if err := nodeState.RecordVolumePublish("vol-a", targetPath, false); err != nil {
		t.Fatalf("failed to record publish: %v", err)
	}

	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: "/var/lib/kubelet/plugins/stale-volume", Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{err: fmt.Errorf("mount source mismatch: active=stale requested=%s", stagingPath)},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, targetPath, stagingPath)
}

func TestNodePublishRejectsExistingMountWithWrongStagingSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	volumePath := "volumes/vol-a"
	staleSource := filepath.Join(tmp, "stale-volume")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", volumePath, stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}
	if err := nodeState.RecordVolumePublish("vol-a", targetPath, false); err != nil {
		t.Fatalf("failed to record publish: %v", err)
	}

	expectedSource := filepath.Join(svmMountPath, volumePath)
	validator := &recordingMountSourceValidator{
		errs: []error{
			nil,
			fmt.Errorf("mount source mismatch: active=%s requested=%s", staleSource, expectedSource),
		},
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
			{Device: staleSource, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
		mountSourceValidator: validator,
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "does not match recorded source") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, targetPath, stagingPath, staleSource, expectedSource)

	wantCalls := []mountSourceValidationCall{
		{targetPath: targetPath, expectedSource: stagingPath},
		{targetPath: stagingPath, expectedSource: expectedSource},
	}
	if !reflect.DeepEqual(validator.calls, wantCalls) {
		t.Fatalf("unexpected source validation calls: got %#v want %#v", validator.calls, wantCalls)
	}
}

func TestNodePublishRejectsExistingMountWithReadonlyMismatch(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	sourcePath := filepath.Join(svmMountPath, "volumes/vol-a")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	stateFile := filepath.Join(tmp, "state.json")
	stateData := map[string]any{
		"volumes": map[string]any{
			"vol-a": map[string]any{
				"volume_id":       "vol-a",
				"svm_name":        "svm-a",
				"vip":             "10.0.0.1",
				"volume_path":     "volumes/vol-a",
				"staging_path":    stagingPath,
				"published_paths": []string{targetPath},
			},
		},
	}
	rawState, err := json.Marshal(stateData)
	if err != nil {
		t.Fatalf("failed to marshal legacy node state: %v", err)
	}
	if err := os.WriteFile(stateFile, rawState, 0600); err != nil {
		t.Fatalf("failed to write legacy node state: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
		{Device: sourcePath, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		Readonly:          true,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "readonly mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
	assertErrorOmits(t, err, targetPath, stagingPath, sourcePath)
}

func TestNodeUnpublishInternalErrorsDoNotEchoDetails(t *testing.T) {
	const secret = "secret-node-unpublish-detail"

	tests := []struct {
		name        string
		nodeMounter func(mountedTargetPath string) mountutils.Interface
		wantMessage string
	}{
		{
			name: "check mount point",
			nodeMounter: func(mountedTargetPath string) mountutils.Interface {
				return &errorMounter{
					Interface:                mountutils.NewFakeMounter(nil),
					isLikelyNotMountPointErr: fmt.Errorf("check failed: %s", secret),
				}
			},
			wantMessage: "failed to check mount point",
		},
		{
			name: "unmount",
			nodeMounter: func(mountedTargetPath string) mountutils.Interface {
				return &errorMounter{
					Interface: mountutils.NewFakeMounter([]mountutils.MountPoint{
						{Device: "/svm/volumes/vol-a", Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
					}),
					unmountErr: fmt.Errorf("unmount failed: %s", secret),
				}
			},
			wantMessage: "failed to unmount",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			tmp := t.TempDir()
			targetPath := filepath.Join(tmp, "target")
			if err := os.MkdirAll(targetPath, 0750); err != nil {
				t.Fatalf("failed to create target path: %v", err)
			}
			mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
			if err != nil {
				t.Fatalf("failed to resolve target path: %v", err)
			}
			nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
			if err != nil {
				t.Fatalf("failed to create node state: %v", err)
			}
			driver := &Driver{
				mode:         "node",
				nodeID:       "node-a",
				nodeState:    nodeState,
				mountManager: new(arcamount.MountManager),
				nodeMounter:  tt.nodeMounter(mountedTargetPath),
			}

			_, err = driver.NodeUnpublishVolume(context.Background(), &csi.NodeUnpublishVolumeRequest{
				VolumeId:   "vol-a",
				TargetPath: targetPath,
			})
			assertInternalErrorOmits(t, err, tt.wantMessage, secret, targetPath)
		})
	}
}
