.PHONY: all build clean test docker-build docker-push fmt vet generate manifests install-tools

# Variables
BINARY_NAME=csi-driver
DOCKER_IMAGE?=csi-arca-storage
DOCKER_TAG?=latest
GOBASE=$(shell pwd)
GOBIN=$(GOBASE)/bin

# Go parameters
GOCMD=go
GOBUILD=$(GOCMD) build
GOCLEAN=$(GOCMD) clean
GOTEST=$(GOCMD) test
GOGET=$(GOCMD) get
GOMOD=$(GOCMD) mod
GOFMT=$(GOCMD) fmt
GOVET=$(GOCMD) vet

# Build flags
LDFLAGS=-ldflags "-s -w"

all: fmt vet build

build:
	@echo "Building $(BINARY_NAME)..."
	@mkdir -p $(GOBIN)
	$(GOBUILD) $(LDFLAGS) -o $(GOBIN)/$(BINARY_NAME) ./cmd/csi-driver
	@echo "Build complete: $(GOBIN)/$(BINARY_NAME)"

clean:
	@echo "Cleaning..."
	$(GOCLEAN)
	rm -rf $(GOBIN)
	@echo "Clean complete"

test:
	@echo "Running tests..."
	$(GOTEST) -v -race ./...
	@echo "Tests complete"

fmt:
	@echo "Formatting code..."
	$(GOFMT) ./...
	@echo "Format complete"

vet:
	@echo "Running go vet..."
	$(GOVET) ./...
	@echo "Vet complete"

tidy:
	@echo "Tidying modules..."
	$(GOMOD) tidy
	@echo "Tidy complete"

docker-build:
	@echo "Building Docker image $(DOCKER_IMAGE):$(DOCKER_TAG)..."
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) .
	@echo "Docker build complete"

docker-push:
	@echo "Pushing Docker image $(DOCKER_IMAGE):$(DOCKER_TAG)..."
	docker push $(DOCKER_IMAGE):$(DOCKER_TAG)
	@echo "Docker push complete"

install:
	@echo "Installing dependencies..."
	$(GOMOD) download
	@echo "Install complete"

run:
	@echo "Running $(BINARY_NAME)..."
	$(GOBIN)/$(BINARY_NAME) -config=config.example.yaml

install-tools:
	@echo "Installing code generation tools..."
	@go install sigs.k8s.io/controller-tools/cmd/controller-gen@v0.16.5
	@go install k8s.io/code-generator/cmd/client-gen@v0.35.0
	@go install k8s.io/code-generator/cmd/deepcopy-gen@v0.35.0
	@echo "Tools installed successfully"

generate:
	@echo "Generating deepcopy and clientset..."
	@bash hack/update-codegen.sh
	@echo "Code generation complete"

manifests:
	@echo "Generating CRD manifests..."
	@mkdir -p deploy/crds
	@PATH="$(HOME)/go/bin:$(PATH)" controller-gen crd:maxDescLen=0 paths="./pkg/apis/..." output:crd:artifacts:config=deploy/crds
	@echo "CRD manifests generated in deploy/crds/"

help:
	@echo "Available targets:"
	@echo "  all          - Format, vet, and build"
	@echo "  build        - Build the binary"
	@echo "  clean        - Remove build artifacts"
	@echo "  test         - Run tests"
	@echo "  fmt          - Format code"
	@echo "  vet          - Run go vet"
	@echo "  tidy         - Tidy go modules"
	@echo "  docker-build - Build Docker image"
	@echo "  docker-push  - Push Docker image"
	@echo "  install      - Download dependencies"
	@echo "  install-tools - Install code generation tools"
	@echo "  generate     - Generate deepcopy and clientset code"
	@echo "  manifests    - Generate CRD manifests"
	@echo "  run          - Run the binary"
	@echo "  help         - Show this help"
