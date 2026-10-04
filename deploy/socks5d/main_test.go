package main

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestTargetPolicy(t *testing.T) {
	for _, host := range []string{"delucru.md", "www.rabota.md", "arbitrary-future-board.example", "x.awswaf.com", "EXAMPLE.COM.", "xn--bcher-kva.example", "8.8.8.8", "2606:4700:4700::1111"} {
		if !allowedTargetName(host) {
			t.Errorf("public host rejected: %s", host)
		}
	}
	for _, host := range []string{"localhost", "host.local", "host.internal", "host.home.arpa", "host.lan", "x..com", "-x.com", "x-.com", "x.com\n", "user@x.com", "127.0.0.1", "10.1.2.3", "::1", "::ffff:192.168.1.1", "100.106.163.104"} {
		if allowedTargetName(host) {
			t.Errorf("unsafe host accepted: %q", host)
		}
	}
	for _, ip := range []string{"0.0.0.0", "10.0.0.1", "127.0.0.1", "100.64.1.1", "169.254.169.254", "172.19.0.1", "192.168.1.1", "192.0.2.1", "198.18.1.1", "224.0.0.1", "::", "::1", "fe80::1", "fc00::1", "2001:db8::1", "::ffff:10.0.0.1"} {
		if allowedTargetIP(ip) {
			t.Errorf("unsafe resolved IP accepted: %s", ip)
		}
	}
	for _, port := range []uint16{0, 22, 53, 5432, 6379, 18080} {
		if allowedTargetPort(port) {
			t.Errorf("non-web port accepted: %d", port)
		}
	}
	if !allowedTargetPort(80) || !allowedTargetPort(443) {
		t.Fatal("HTTP/HTTPS rejected")
	}
}

func TestClientIsolation(t *testing.T) {
	list, err := parseAllow("127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if !list.allowed(net.ParseIP("127.0.0.1")) || list.allowed(net.ParseIP("100.106.163.104")) {
		t.Fatal("client isolation broken")
	}
	if _, err = parseAllow(""); err == nil {
		t.Fatal("empty client allowlist accepted")
	}
}

func socksConnect(conn net.Conn, host string, port uint16) (byte, error) {
	if _, err := conn.Write([]byte{5, 1, 0}); err != nil {
		return 0, err
	}
	greeting := make([]byte, 2)
	if _, err := io.ReadFull(conn, greeting); err != nil {
		return 0, err
	}
	if greeting[1] != 0 {
		return 0, fmt.Errorf("authentication rejected")
	}
	request := append([]byte{5, 1, 0, 3, byte(len(host))}, []byte(host)...)
	request = binary.BigEndian.AppendUint16(request, port)
	if _, err := conn.Write(request); err != nil {
		return 0, err
	}
	reply := make([]byte, 10)
	_, err := io.ReadFull(conn, reply)
	return reply[1], err
}

// The complete SOCKS handshake and HTTP stream run offline through a local origin.
// Inject only the final dial: production still resolves/checks/pins public IPs.
func TestOfflineWebTunnelThreeContexts(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "fixture:"+r.Host+r.URL.Path)
	}))
	defer origin.Close()
	for index, host := range []string{"delucru.md", "rabota.md", "future-board.example"} {
		t.Run(host, func(t *testing.T) {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan struct{})
			go func() {
				defer close(done)
				client, err := listener.Accept()
				if err != nil {
					return
				}
				serveConnWithDialer(client, time.Second, time.Second, time.Second, func(name string, port uint16, _, _ time.Duration) (net.Conn, error) {
					if name != host || (port != 80 && port != 443) {
						return nil, fmt.Errorf("wrong destination")
					}
					return net.DialTimeout("tcp", origin.Listener.Addr().String(), time.Second)
				})
			}()
			client, err := net.DialTimeout("tcp", listener.Addr().String(), time.Second)
			if err != nil {
				t.Fatal(err)
			}
			defer client.Close()
			client.SetDeadline(time.Now().Add(3 * time.Second))
			port := uint16(443)
			if index == 2 {
				port = 80
			}
			code, err := socksConnect(client, host, port)
			if err != nil || code != 0 {
				t.Fatalf("CONNECT: code=%d err=%v", code, err)
			}
			fmt.Fprintf(client, "GET /jobs HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", host)
			body, err := io.ReadAll(client)
			if err != nil || !strings.Contains(string(body), "fixture:"+host+"/jobs") {
				t.Fatalf("HTTP stream failed: %v", err)
			}
			client.Close()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("tunnel leaked")
			}
		})
	}
}

func TestSOCKSRejectsBeforeDial(t *testing.T) {
	for _, tc := range []struct {
		host string
		port uint16
	}{{"10.0.0.1", 443}, {"host.local", 443}, {"delucru.md", 22}} {
		client, server := net.Pipe()
		done := make(chan struct{})
		go func() {
			defer close(done)
			serveConnWithDialer(server, time.Second, time.Second, time.Second, func(string, uint16, time.Duration, time.Duration) (net.Conn, error) {
				t.Error("unsafe target reached dialer")
				return nil, fmt.Errorf("unexpected dial")
			})
		}()
		client.SetDeadline(time.Now().Add(time.Second))
		code, err := socksConnect(client, tc.host, tc.port)
		if err != nil || code != 2 {
			t.Errorf("unsafe target: code=%d err=%v", code, err)
		}
		client.Close()
		<-done
	}
}
