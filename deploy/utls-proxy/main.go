// utls-proxy: TLS fingerprint proxy for Cloudflare-protected endpoints.
//
// Listens on HTTP, forwards to HTTPS upstream using Chrome TLS fingerprint
// via utls. Solves: Bifrost (Go fasthttp) gets blocked by Cloudflare because
// Go's crypto/tls has a recognizable JA3 fingerprint.
//
// Usage: UPSTREAM=https://chatgpt.com LISTEN=:8444 ./utls-proxy
package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	utls "github.com/refraction-networking/utls"
)

func main() {
	upstream := os.Getenv("UPSTREAM")
	if upstream == "" {
		upstream = "https://chatgpt.com"
	}
	upstream = strings.TrimRight(upstream, "/")

	listen := os.Getenv("LISTEN")
	if listen == "" {
		listen = ":8444"
	}

	upstreamURL, err := url.Parse(upstream)
	if err != nil {
		log.Fatalf("bad UPSTREAM %q: %v", upstream, err)
	}

	host := upstreamURL.Hostname()
	port := upstreamURL.Port()
	if port == "" {
		port = "443"
	}
	addr := net.JoinHostPort(host, port)

	log.Printf("utls-proxy: %s → %s (Chrome fingerprint)", listen, upstream)

	// Custom transport that uses utls for TLS handshake
	transport := &http.Transport{
		DialTLSContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			// Always connect to the upstream host, ignore the addr from the
			// http.Transport (which would be localhost since we're proxying)
			dialer := &net.Dialer{Timeout: 30 * time.Second}
			rawConn, err := dialer.DialContext(ctx, "tcp", addr)
			if err != nil {
				return nil, fmt.Errorf("dial %s: %w", addr, err)
			}

			tlsConn := utls.UClient(rawConn, &utls.Config{
				ServerName: host,
				MinVersion: tls.VersionTLS12,
			}, utls.HelloChrome_Auto)

			// Chrome hello includes h2 ALPN, but our transport is HTTP/1.1.
			// Patch the ALPN extension before handshake to force http/1.1.
			if err := tlsConn.BuildHandshakeState(); err != nil {
				rawConn.Close()
				return nil, fmt.Errorf("utls build state: %w", err)
			}
			for _, ext := range tlsConn.Extensions {
				if alpn, ok := ext.(*utls.ALPNExtension); ok {
					alpn.AlpnProtocols = []string{"http/1.1"}
					break
				}
			}
			if err := tlsConn.MarshalClientHello(); err != nil {
				rawConn.Close()
				return nil, fmt.Errorf("utls marshal: %w", err)
			}
			if err := tlsConn.HandshakeContext(ctx); err != nil {
				rawConn.Close()
				return nil, fmt.Errorf("utls handshake: %w", err)
			}

			return tlsConn, nil
		},
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
		// Disable auto-compression so we pass through exactly what the
		// upstream sends (important for SSE streaming)
		DisableCompression: true,
	}

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Build upstream URL: same path + query
		target := upstream + r.URL.RequestURI()

		proxyReq, err := http.NewRequestWithContext(r.Context(), r.Method, target, r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}

		// Copy all headers from the original request
		for k, vv := range r.Header {
			for _, v := range vv {
				proxyReq.Header.Add(k, v)
			}
		}
		// Ensure Host header matches upstream
		proxyReq.Host = upstreamURL.Host

		resp, err := transport.RoundTrip(proxyReq)
		if err != nil {
			log.Printf("upstream error: %v", err)
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		// Copy response headers
		for k, vv := range resp.Header {
			for _, v := range vv {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(resp.StatusCode)

		// Stream the response body — critical for SSE
		if f, ok := w.(http.Flusher); ok {
			buf := make([]byte, 4096)
			for {
				n, readErr := resp.Body.Read(buf)
				if n > 0 {
					w.Write(buf[:n])
					f.Flush()
				}
				if readErr != nil {
					break
				}
			}
		} else {
			io.Copy(w, resp.Body)
		}
	})

	// Health check endpoint
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ok","upstream":"` + upstream + `"}`))
	})
	mux.Handle("/", handler)

	server := &http.Server{
		Addr:         listen,
		Handler:      mux,
		ReadTimeout:  120 * time.Second,
		WriteTimeout: 120 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	log.Fatal(server.ListenAndServe())
}
