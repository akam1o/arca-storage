# Build stage
FROM golang:1.25-alpine AS builder

# Install build dependencies
RUN apk add --no-cache git make

WORKDIR /workspace

# Copy go mod files
COPY go.mod go.sum ./
RUN go mod download

# Copy source code
COPY . .

# Build the binary
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w" \
    -o /csi-driver \
    ./cmd/csi-driver

# Runtime stage
FROM alpine:3.19

# Install runtime dependencies
RUN apk add --no-cache \
    ca-certificates \
    nfs-utils \
    e2fsprogs \
    xfsprogs \
    util-linux

# Copy binary from builder
COPY --from=builder /csi-driver /csi-driver

# Create directories
RUN mkdir -p /var/lib/csi-arca-storage \
    /etc/csi-arca-storage \
    /csi

# Set entrypoint
ENTRYPOINT ["/csi-driver"]
