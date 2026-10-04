package main

import (
	"context"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/netip"
	"strings"
	"sync"
	"time"
)

const version = "a14-socks5-v5"

var dnsServers = []string{"8.8.8.8:53", "1.1.1.1:53"}

var blockedTargetPrefixes = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("192.168.0.0/16"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"),
	netip.MustParsePrefix("224.0.0.0/4"),
	netip.MustParsePrefix("240.0.0.0/4"),
	netip.MustParsePrefix("::/128"),
	netip.MustParsePrefix("::1/128"),
	netip.MustParsePrefix("fc00::/7"),
	netip.MustParsePrefix("fe80::/10"),
	netip.MustParsePrefix("ff00::/8"),
	netip.MustParsePrefix("2001:db8::/32"),
}

// Names are independent of adapters. Every resolved address is checked before dialing.
func allowedTargetName(host string) bool {
	if net.ParseIP(host) != nil {
		return allowedTargetIP(host)
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if len(host) > 253 || !strings.Contains(host, ".") {
		return false
	}
	for _, suffix := range []string{".localhost", ".local", ".internal", ".lan", ".home.arpa"} {
		if strings.HasSuffix(host, suffix) {
			return false
		}
	}
	for _, label := range strings.Split(host, ".") {
		if len(label) < 1 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, char := range label {
			if !(char >= 'a' && char <= 'z' || char >= '0' && char <= '9' || char == '-') {
				return false
			}
		}
	}
	return true
}

func allowedTargetPort(port uint16) bool {
	return port == 80 || port == 443
}

func allowedTargetIP(raw string) bool {
	addr, err := netip.ParseAddr(raw)
	if err != nil {
		return false
	}
	addr = addr.Unmap()
	if !addr.IsGlobalUnicast() {
		return false
	}
	for _, prefix := range blockedTargetPrefixes {
		if prefix.Contains(addr) {
			return false
		}
	}
	return true
}

type allowList struct{ nets []*net.IPNet }

func parseAllow(raw string) (allowList, error) {
	var out allowList
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if !strings.Contains(item, "/") {
			ip := net.ParseIP(item)
			if ip == nil {
				return out, fmt.Errorf("invalid allow IP %q", item)
			}
			bits := 128
			if ip.To4() != nil {
				bits = 32
				ip = ip.To4()
			}
			out.nets = append(out.nets, &net.IPNet{IP: ip, Mask: net.CIDRMask(bits, bits)})
			continue
		}
		_, network, err := net.ParseCIDR(item)
		if err != nil {
			return out, fmt.Errorf("invalid allow CIDR %q: %w", item, err)
		}
		out.nets = append(out.nets, network)
	}
	if len(out.nets) == 0 {
		return out, fmt.Errorf("empty allow list")
	}
	return out, nil
}

func (a allowList) allowed(ip net.IP) bool {
	for _, network := range a.nets {
		if network.Contains(ip) {
			return true
		}
	}
	return false
}

type dnsResult struct {
	ips []net.IP
	err error
}

func resolveHosts(ctx context.Context, host string) ([]string, error) {
	if ip := net.ParseIP(host); ip != nil {
		return []string{ip.String()}, nil
	}

	results := make(chan dnsResult, len(dnsServers))
	for _, server := range dnsServers {
		server := server
		go func() {
			resolver := &net.Resolver{
				PreferGo: true,
				Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
					dialer := net.Dialer{Timeout: 4 * time.Second}
					if !strings.HasPrefix(network, "udp") && !strings.HasPrefix(network, "tcp") {
						network = "udp"
					}
					return dialer.DialContext(ctx, network, server)
				},
			}
			ips, err := resolver.LookupIP(ctx, "ip", host)
			results <- dnsResult{ips: ips, err: err}
		}()
	}

	seen := map[string]struct{}{}
	var v4, v6 []string
	var lastErr error
	for range dnsServers {
		select {
		case <-ctx.Done():
			if len(v4)+len(v6) > 0 {
				return append(v4, v6...), nil
			}
			return nil, fmt.Errorf("dns_timeout host=%s: %w", host, ctx.Err())
		case result := <-results:
			if result.err != nil {
				lastErr = result.err
				continue
			}
			for _, ip := range result.ips {
				s := ip.String()
				if _, ok := seen[s]; ok {
					continue
				}
				seen[s] = struct{}{}
				if ip.To4() != nil {
					v4 = append(v4, s)
				} else {
					v6 = append(v6, s)
				}
			}
		}
	}
	if len(v4)+len(v6) == 0 {
		if lastErr == nil {
			lastErr = errors.New("no addresses returned")
		}
		return nil, fmt.Errorf("dns_error host=%s: %w", host, lastErr)
	}
	return append(v4, v6...), nil
}

func dialTarget(host string, port uint16, totalTimeout, perIPTimeout time.Duration) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), totalTimeout)
	defer cancel()

	ips, err := resolveHosts(ctx, host)
	if err != nil {
		return nil, err
	}
	var lastErr error
	for idx, ip := range ips {
		if !allowedTargetIP(ip) {
			log.Printf("blocked non-public target host=%s ip=%s", host, ip)
			lastErr = errors.New("target address rejected by policy")
			continue
		}
		if err := ctx.Err(); err != nil {
			break
		}
		timeout := perIPTimeout
		if deadline, ok := ctx.Deadline(); ok {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				break
			}
			if remaining < timeout {
				timeout = remaining
			}
		}
		attemptCtx, attemptCancel := context.WithTimeout(ctx, timeout)
		started := time.Now()
		dialer := net.Dialer{KeepAlive: 30 * time.Second}
		conn, dialErr := dialer.DialContext(attemptCtx, "tcp", net.JoinHostPort(ip, fmt.Sprint(port)))
		attemptCancel()
		if dialErr == nil {
			if idx > 0 {
				log.Printf("target connect recovered host=%s ip=%s attempt=%d elapsed=%s", host, ip, idx+1, time.Since(started).Round(time.Millisecond))
			}
			return conn, nil
		}
		lastErr = dialErr
		log.Printf("target connect failed host=%s ip=%s attempt=%d elapsed=%s err=%v", host, ip, idx+1, time.Since(started).Round(time.Millisecond), dialErr)
	}
	if lastErr == nil {
		lastErr = ctx.Err()
	}
	return nil, fmt.Errorf("target_connect_failed host=%s attempts=%d: %w", host, len(ips), lastErr)
}

func writeReply(conn net.Conn, code byte) {
	_, _ = conn.Write([]byte{0x05, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0})
}

func copyWithIdleTimeout(dst, src net.Conn, idle time.Duration) {
	buf := make([]byte, 32*1024)
	for {
		if idle > 0 {
			_ = src.SetReadDeadline(time.Now().Add(idle))
		}
		n, err := src.Read(buf)
		if n > 0 {
			if idle > 0 {
				_ = dst.SetWriteDeadline(time.Now().Add(idle))
			}
			if _, writeErr := dst.Write(buf[:n]); writeErr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

type targetDialer func(string, uint16, time.Duration, time.Duration) (net.Conn, error)

func serveConn(client net.Conn, dialTimeout, perIPTimeout, idleTimeout time.Duration) {
	serveConnWithDialer(client, dialTimeout, perIPTimeout, idleTimeout, dialTarget)
}

func serveConnWithDialer(client net.Conn, dialTimeout, perIPTimeout, idleTimeout time.Duration, dial targetDialer) {
	defer client.Close()
	if tcp, ok := client.(*net.TCPConn); ok {
		_ = tcp.SetKeepAlive(true)
		_ = tcp.SetKeepAlivePeriod(30 * time.Second)
	}
	_ = client.SetReadDeadline(time.Now().Add(15 * time.Second))
	_ = client.SetWriteDeadline(time.Now().Add(15 * time.Second))
	buf := make([]byte, 262)

	if _, err := io.ReadFull(client, buf[:2]); err != nil || buf[0] != 0x05 {
		return
	}
	nmethods := int(buf[1])
	if nmethods < 1 || nmethods > 255 {
		return
	}
	if _, err := io.ReadFull(client, buf[:nmethods]); err != nil {
		return
	}
	noAuth := false
	for _, method := range buf[:nmethods] {
		if method == 0x00 {
			noAuth = true
			break
		}
	}
	if !noAuth {
		_, _ = client.Write([]byte{0x05, 0xff})
		return
	}
	if _, err := client.Write([]byte{0x05, 0x00}); err != nil {
		return
	}

	if _, err := io.ReadFull(client, buf[:4]); err != nil {
		return
	}
	if buf[0] != 0x05 || buf[1] != 0x01 || buf[2] != 0x00 {
		writeReply(client, 0x07)
		return
	}

	var host string
	switch buf[3] {
	case 0x01:
		if _, err := io.ReadFull(client, buf[:4]); err != nil {
			return
		}
		host = net.IP(buf[:4]).String()
	case 0x03:
		if _, err := io.ReadFull(client, buf[:1]); err != nil {
			return
		}
		length := int(buf[0])
		if length == 0 {
			writeReply(client, 0x08)
			return
		}
		if _, err := io.ReadFull(client, buf[:length]); err != nil {
			return
		}
		host = string(buf[:length])
	case 0x04:
		if _, err := io.ReadFull(client, buf[:16]); err != nil {
			return
		}
		host = net.IP(buf[:16]).String()
	default:
		writeReply(client, 0x08)
		return
	}

	if _, err := io.ReadFull(client, buf[:2]); err != nil {
		return
	}
	port := binary.BigEndian.Uint16(buf[:2])
	if !allowedTargetPort(port) || !allowedTargetName(host) {
		log.Printf("blocked target host=%s port=%d", host, port)
		writeReply(client, 0x02)
		return
	}

	target, err := dial(host, port, dialTimeout, perIPTimeout)
	if err != nil {
		log.Printf("proxy target failure host=%s port=%d err=%v", host, port, err)
		writeReply(client, 0x05)
		return
	}
	defer target.Close()
	if tcp, ok := target.(*net.TCPConn); ok {
		_ = tcp.SetKeepAlive(true)
		_ = tcp.SetKeepAlivePeriod(30 * time.Second)
	}
	_ = client.SetDeadline(time.Time{})
	_ = target.SetDeadline(time.Time{})
	writeReply(client, 0x00)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		copyWithIdleTimeout(target, client, idleTimeout)
		if tcp, ok := target.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
	}()
	go func() {
		defer wg.Done()
		copyWithIdleTimeout(client, target, idleTimeout)
		if tcp, ok := client.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
	}()
	wg.Wait()
}

func main() {
	addr := flag.String("addr", "127.0.0.1:18080", "listen address")
	allow := flag.String("allow", "127.0.0.1", "comma-separated client IPs/CIDRs")
	maxConns := flag.Int("max-conns", 16, "maximum concurrent client connections")
	dialTimeout := flag.Duration("dial-timeout", 10*time.Second, "total DNS/connect timeout")
	perIPTimeout := flag.Duration("per-ip-timeout", 3*time.Second, "per-address connect timeout")
	idleTimeout := flag.Duration("idle-timeout", 90*time.Second, "bidirectional tunnel idle timeout")
	showVersion := flag.Bool("version", false, "print version")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}
	if *maxConns < 1 || *maxConns > 256 {
		log.Fatal("max-conns must be 1..256")
	}
	if *perIPTimeout <= 0 || *dialTimeout <= 0 || *idleTimeout <= 0 {
		log.Fatal("timeouts must be positive")
	}
	allowed, err := parseAllow(*allow)
	if err != nil {
		log.Fatal(err)
	}
	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("listen %s: %v", *addr, err)
	}
	defer listener.Close()
	log.Printf("%s listening on %s allow=%s max_conns=%d dial_timeout=%s per_ip_timeout=%s idle_timeout=%s", version, *addr, *allow, *maxConns, *dialTimeout, *perIPTimeout, *idleTimeout)

	semaphore := make(chan struct{}, *maxConns)
	for {
		conn, err := listener.Accept()
		if err != nil {
			log.Printf("accept: %v", err)
			continue
		}
		host, _, err := net.SplitHostPort(conn.RemoteAddr().String())
		if err != nil || !allowed.allowed(net.ParseIP(host)) {
			log.Printf("rejected client %s", conn.RemoteAddr())
			_ = conn.Close()
			continue
		}
		select {
		case semaphore <- struct{}{}:
			go func(c net.Conn) {
				defer func() { <-semaphore }()
				serveConn(c, *dialTimeout, *perIPTimeout, *idleTimeout)
			}(conn)
		default:
			log.Printf("connection limit reached; rejecting %s", conn.RemoteAddr())
			_ = conn.Close()
		}
	}
}
