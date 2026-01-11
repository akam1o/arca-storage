#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${SCRIPT_ROOT}"

# Module info
MODULE_NAME="github.com/akam1o/csi-arca-storage"
API_PACKAGE="${MODULE_NAME}/pkg/apis"
API_VERSION="storage/v1alpha1"

echo "==> Generating deepcopy functions..."
go run k8s.io/code-generator/cmd/deepcopy-gen \
  "${API_PACKAGE}/${API_VERSION}" \
  --output-file zz_generated.deepcopy.go \
  --go-header-file "${SCRIPT_ROOT}/hack/boilerplate.go.txt"

echo "==> Generating typed clientset..."
go run k8s.io/code-generator/cmd/client-gen \
  --clientset-name versioned \
  --input "${API_VERSION}" \
  --input-base "${API_PACKAGE}" \
  --output-dir "${SCRIPT_ROOT}/pkg/client/clientset" \
  --output-pkg "${MODULE_NAME}/pkg/client/clientset" \
  --go-header-file "${SCRIPT_ROOT}/hack/boilerplate.go.txt"

echo ""
echo "Code generation complete!"
echo "Generated files:"
echo "  - pkg/apis/storage/v1alpha1/zz_generated.deepcopy.go"
echo "  - pkg/client/clientset/versioned/"
