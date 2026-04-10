FROM golang:1.22-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY main.go .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o utls-proxy .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=builder /build/utls-proxy /usr/local/bin/utls-proxy
ENTRYPOINT ["utls-proxy"]
