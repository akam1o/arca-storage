package mount

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"k8s.io/klog/v2"
	mountutils "k8s.io/mount-utils"
)

func newTestMountManager(t *testing.T) *MountManager {
	t.Helper()
	return &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}},
		baseMountPath: t.TempDir(),
		mounter:       mountutils.NewFakeMounter(nil),
		validator:     &fakeMountSourceValidator{},
	}
}

type fakeMountSourceValidator struct {
	err   error
	calls []mountSourceValidationCall
}

type mountSourceValidationCall struct {
	targetPath     string
	expectedSource string
}

func (v *fakeMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	v.calls = append(v.calls, mountSourceValidationCall{
		targetPath:     targetPath,
		expectedSource: expectedSource,
	})
	return v.err
}

func captureKlogOutput(t *testing.T, fn func()) string {
	t.Helper()

	state := klog.CaptureState()
	defer state.Restore()

	var out bytes.Buffer
	fs := flag.NewFlagSet(t.Name(), flag.ContinueOnError)
	klog.InitFlags(fs)
	for name, value := range map[string]string{
		"logtostderr":     "false",
		"alsologtostderr": "false",
		"one_output":      "true",
		"skip_headers":    "true",
		"v":               "4",
	} {
		if err := fs.Set(name, value); err != nil {
			t.Fatalf("failed to set klog flag %s: %v", name, err)
		}
	}
	klog.SetOutput(&out)

	fn()
	klog.Flush()

	return out.String()
}

func TestMountLogHelpersIncludeRedactedErrorDetails(t *testing.T) {
	err := fmt.Errorf(
		"mount failed for 10.0.0.1:/exports/team at /var/lib/kubelet/plugins/csi.arca-storage.io/mounts/team: token=secret-token",
	)

	logOutput := captureKlogOutput(t, func() {
		mountLogWarning("Failed to ensure SVM mount", err)
		mountLogError("Failed to unmount SVM", err)
	})

	for _, want := range []string{"mount failed", "<redacted>", "<nfs-source>", "<path>"} {
		if !strings.Contains(logOutput, want) {
			t.Fatalf("mount logs %q do not contain %q", logOutput, want)
		}
	}
	for _, forbidden := range []string{
		"10.0.0.1",
		"/exports/team",
		"/var/lib/kubelet/plugins/csi.arca-storage.io/mounts/team",
		"secret-token",
	} {
		if strings.Contains(logOutput, forbidden) {
			t.Fatalf("mount logs %q contain %q", logOutput, forbidden)
		}
	}
}

func TestNewMountManagerRejectsUnsafeBaseMountPath(t *testing.T) {
	uncleanPath := t.TempDir() + "/../mounts"
	tests := []struct {
		name          string
		baseMountPath string
		wantErr       string
	}{
		{
			name:          "relative",
			baseMountPath: "relative/mounts",
			wantErr:       "base mount path must be absolute",
		},
		{
			name:          "unclean",
			baseMountPath: uncleanPath,
			wantErr:       "base mount path must be canonical",
		},
		{
			name:          "root",
			baseMountPath: string(filepath.Separator),
			wantErr:       "base mount path must not be the filesystem root",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			nodeState := &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}}

			_, err := NewMountManager(nodeState, tt.baseMountPath)
			if err == nil {
				t.Fatal("NewMountManager error = nil, want base mount path validation error")
			}
			if !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("NewMountManager error = %v, want %q", err, tt.wantErr)
			}
		})
	}
}

func TestNewMountManagerRejectsInvalidSVMNameFromNodeState(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:     "../tenant-a",
			VIP:         "192.0.2.10",
			StagingPath: "/stage/volume-a",
		},
	}}}

	_, err := NewMountManager(nodeState, filepath.Join(t.TempDir(), "mounts"))
	if err == nil {
		t.Fatal("NewMountManager error = nil, want invalid SVM name error")
	}
	if !strings.Contains(err.Error(), "invalid SVM name in node state") {
		t.Fatalf("NewMountManager error = %v, want invalid SVM name in node state", err)
	}
}

func TestMountManagerRejectsInvalidSVMNames(t *testing.T) {
	manager := newTestMountManager(t)
	ctx := context.Background()

	if _, err := manager.EnsureSVMMount(ctx, "../tenant-a", "192.0.2.10", "", nil); err == nil {
		t.Fatal("EnsureSVMMount should reject invalid SVM name")
	}
	if _, err := manager.ShouldUnmountSVM(ctx, "../tenant-a"); err == nil {
		t.Fatal("ShouldUnmountSVM should reject invalid SVM name")
	}
	if err := manager.UnmountSVM(ctx, "../tenant-a"); err == nil {
		t.Fatal("UnmountSVM should reject invalid SVM name")
	}
	if _, err := manager.GetMountPath("../tenant-a"); err == nil {
		t.Fatal("GetMountPath should reject invalid SVM name")
	}
}

func TestEnsureSVMMountRejectsConflictingOptions(t *testing.T) {
	manager := newTestMountManager(t)
	ctx := context.Background()
	initialOptions := MergeNFSMountOptions([]string{"nconnect=8"})

	if _, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", "", initialOptions); err != nil {
		t.Fatalf("initial mount failed: %v", err)
	}
	if _, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", "", []string{"nconnect=8"}); err != nil {
		t.Fatalf("same options should be accepted: %v", err)
	}

	_, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", "", []string{"nconnect=16"})
	if err == nil {
		t.Fatal("expected conflicting options to be rejected")
	}
	if !strings.Contains(err.Error(), "different NFS options") {
		t.Fatalf("unexpected error: %v", err)
	}
	if strings.Contains(err.Error(), "nconnect=16") {
		t.Fatalf("conflicting options error leaked requested option: %v", err)
	}
}

func TestEnsureSVMMountUsesConfiguredExportRoot(t *testing.T) {
	manager := newTestMountManager(t)

	if _, err := manager.EnsureSVMMount(context.Background(), "tenant-a", "192.0.2.10", "/srv/arca/tenant-a", nil); err != nil {
		t.Fatalf("mount failed: %v", err)
	}

	fakeMounter := manager.mounter.(*mountutils.FakeMounter)
	if len(fakeMounter.MountPoints) != 1 {
		t.Fatalf("mount points = %#v", fakeMounter.MountPoints)
	}
	if fakeMounter.MountPoints[0].Device != "192.0.2.10:/srv/arca/tenant-a" {
		t.Fatalf("mounted source = %q", fakeMounter.MountPoints[0].Device)
	}
}

func TestEnsureSVMMountRejectsWrongExistingSource(t *testing.T) {
	validator := &fakeMountSourceValidator{
		err: fmt.Errorf("mount source mismatch: active=192.0.2.99:/exports/tenant-a requested=192.0.2.10:/exports/tenant-a"),
	}
	manager := &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}},
		baseMountPath: t.TempDir(),
		mounter:       mountutils.NewFakeMounter(nil),
		validator:     validator,
	}
	mountPath := manager.getMountPath("tenant-a")
	if err := os.MkdirAll(mountPath, 0750); err != nil {
		t.Fatalf("failed to create mount path: %v", err)
	}
	if err := manager.mounter.Mount("192.0.2.99:/exports/tenant-a", mountPath, "nfs4", nil); err != nil {
		t.Fatalf("failed to seed fake mount: %v", err)
	}
	manager.mounts["tenant-a"] = &SVMMount{
		SVMName:   "tenant-a",
		VIP:       "192.0.2.99",
		MountPath: mountPath,
	}

	_, err := manager.EnsureSVMMount(context.Background(), "tenant-a", "192.0.2.10", "", nil)
	if err == nil {
		t.Fatal("expected wrong existing source to be rejected")
	}
	if !strings.Contains(err.Error(), "not safe to reuse") {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, forbidden := range []string{mountPath, "192.0.2.99", "192.0.2.10", "/exports/tenant-a"} {
		if strings.Contains(err.Error(), forbidden) {
			t.Fatalf("wrong source error leaked %q: %v", forbidden, err)
		}
	}
	if len(validator.calls) != 1 || validator.calls[0].expectedSource != "192.0.2.10:/exports/tenant-a" {
		t.Fatalf("unexpected validator calls: %#v", validator.calls)
	}
}

func TestMountManagerLogsDoNotEchoMountDetails(t *testing.T) {
	baseMountPath := filepath.Join(t.TempDir(), "secret-base-mount")
	manager := &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}},
		baseMountPath: baseMountPath,
		mounter:       mountutils.NewFakeMounter(nil),
		validator:     &fakeMountSourceValidator{},
	}
	ctx := context.Background()
	vip := "192.0.2.10"
	exportRoot := "/secret-export-root"
	mountPath := filepath.Join(baseMountPath, "tenant-a")
	nfsSource := vip + ":" + exportRoot

	logOutput := captureKlogOutput(t, func() {
		if _, err := manager.EnsureSVMMount(ctx, "tenant-a", vip, exportRoot, []string{"nconnect=8"}); err != nil {
			t.Fatalf("EnsureSVMMount failed: %v", err)
		}
		if err := manager.UnmountSVM(ctx, "tenant-a"); err != nil {
			t.Fatalf("UnmountSVM failed: %v", err)
		}
	})

	for _, forbidden := range []string{baseMountPath, mountPath, vip, exportRoot, nfsSource} {
		if strings.Contains(logOutput, forbidden) {
			t.Fatalf("mount manager logs leaked %q in:\n%s", forbidden, logOutput)
		}
	}
}

func TestGetUniqueSVMMountsNormalizesLegacyOptions(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=8"},
		},
	}}}

	got, err := nodeState.GetUniqueSVMMounts()
	if err != nil {
		t.Fatalf("GetUniqueSVMMounts failed: %v", err)
	}
	want := MergeNFSMountOptions([]string{"nconnect=8"})
	if !reflect.DeepEqual(got["tenant-a"].NFSMountOptions, want) {
		t.Fatalf("unexpected normalized options: got %v want %v", got["tenant-a"].NFSMountOptions, want)
	}
	if got["tenant-a"].ExportRoot != "/exports/tenant-a" {
		t.Fatalf("unexpected export root: %s", got["tenant-a"].ExportRoot)
	}
}

func TestGetUniqueSVMMountsRejectsConflictingOptions(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=8"},
		},
		"volume-b": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=16"},
		},
	}}}

	_, err := nodeState.GetUniqueSVMMounts()
	if err == nil {
		t.Fatal("expected conflicting options to be rejected")
	}
}

func TestGetUniqueSVMMountsRejectsConflictingExportRoots(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:    "tenant-a",
			VIP:        "192.0.2.10",
			ExportRoot: "/exports/tenant-a",
		},
		"volume-b": {
			SVMName:    "tenant-a",
			VIP:        "192.0.2.10",
			ExportRoot: "/srv/arca/tenant-a",
		},
	}}}

	_, err := nodeState.GetUniqueSVMMounts()
	if err == nil {
		t.Fatal("expected conflicting export roots to be rejected")
	}
}

func TestValidateVolumeStagingDetectsMismatchedVolumePath(t *testing.T) {
	state, err := NewNodeState(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewNodeState failed: %v", err)
	}
	if err := state.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"",
		"pvc-a",
		"/stage/volume-a",
		[]string{"nconnect=8"},
	); err != nil {
		t.Fatalf("RecordVolumeStaging failed: %v", err)
	}

	err = state.ValidateVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"",
		"pvc-b",
		"/stage/volume-a",
		MergeNFSMountOptions([]string{"nconnect=8"}),
	)
	if err == nil {
		t.Fatal("expected volume path mismatch to be rejected")
	}
	if !strings.Contains(err.Error(), "path mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestHasVolumePublishRequiresRecordedTarget(t *testing.T) {
	state, err := NewNodeState(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewNodeState failed: %v", err)
	}
	if err := state.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"",
		"pvc-a",
		"/stage/volume-a",
		nil,
	); err != nil {
		t.Fatalf("RecordVolumeStaging failed: %v", err)
	}

	if state.HasVolumePublish("volume-a", "/pods/volume-a") {
		t.Fatal("target should not be recorded before RecordVolumePublish")
	}
	if err := state.RecordVolumePublish("volume-a", "/pods/volume-a", false); err != nil {
		t.Fatalf("RecordVolumePublish failed: %v", err)
	}
	if !state.HasVolumePublish("volume-a", "/pods/volume-a") {
		t.Fatal("target should be recorded after RecordVolumePublish")
	}
	if err := state.ValidateVolumePublish("volume-a", "/pods/volume-a", false); err != nil {
		t.Fatalf("recorded readonly state should be accepted: %v", err)
	}
	if err := state.ValidateVolumePublish("volume-a", "/pods/volume-a", true); err == nil {
		t.Fatal("readonly mismatch should be rejected")
	}
}

func TestShouldUnmountSVMRefreshesNodeState(t *testing.T) {
	stateFile := filepath.Join(t.TempDir(), "state.json")
	writer, err := NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("NewNodeState(writer) failed: %v", err)
	}
	reader, err := NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("NewNodeState(reader) failed: %v", err)
	}
	manager := newTestMountManager(t)
	manager.nodeState = reader

	if err := writer.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"",
		"pvc-a",
		"/stage/volume-a",
		nil,
	); err != nil {
		t.Fatalf("RecordVolumeStaging(writer) failed: %v", err)
	}

	shouldUnmount, err := manager.ShouldUnmountSVM(context.Background(), "tenant-a")
	if err != nil {
		t.Fatalf("ShouldUnmountSVM failed: %v", err)
	}
	if shouldUnmount {
		t.Fatal("SVM should not be unmounted when another process has staged a volume")
	}
}
