package main

import (
	"bytes"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type HubTarget struct {
	mu        sync.RWMutex
	httpsPort int
	httpPort  int
	csrfToken string
	extPort   int
	extToken  string
}

var hub = &HubTarget{}

func discoverHub() (int, int, string) {
	pyDiscover := `
import glob, json, os, re, subprocess
try:
    ss_out = subprocess.check_output(["ss", "-tlnp"], text=True, stderr=subprocess.DEVNULL)
except Exception:
    ss_out = ""
pid_ports = {}
for line in ss_out.splitlines():
    if "127.0.0.1:" not in line:
        continue
    m_port = re.search(r"127\.0\.0\.1:(\d+)", line)
    m_pids = re.findall(r"pid=(\d+)", line)
    if m_port and m_pids:
        port = int(m_port.group(1))
        if port in (8080, 5387, 37998, 37999):
            continue
        for pid in m_pids:
            pid_ports.setdefault(pid, []).append(port)

csrf = ""
if os.path.exists("/tmp/jetski_hub_server.ERR"):
    try:
        with open("/tmp/jetski_hub_server.ERR", "r", errors="ignore") as f:
            m = re.findall(r"CSRFToken:\s*([0-9a-fA-F-]+)", f.read())
            if m:
                csrf = m[-1]
    except Exception:
        pass

env_ls_csrf = {}
for env_path in glob.glob("/proc/[0-9]*/environ"):
    try:
        if os.stat(env_path).st_uid != os.getuid():
            continue
        with open(env_path, "rb") as f:
            raw = f.read().split(b"\x00")
        ls_addr, tok = None, None
        for item in raw:
            if item.startswith(b"ANTIGRAVITY_LS_ADDRESS="):
                ls_addr = item.split(b"=", 1)[1].decode("utf-8", "ignore")
            elif item.startswith(b"ANTIGRAVITY_CSRF_TOKEN="):
                tok = item.split(b"=", 1)[1].decode("utf-8", "ignore")
        if tok and ls_addr and ":" in ls_addr:
            p = int(ls_addr.rsplit(":", 1)[1])
            env_ls_csrf[p] = tok
    except Exception:
        continue

res = {}
for pid, ports in pid_ports.items():
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            continue
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        if "jetski-hub-server" in cmd:
            for p in ports:
                if p in env_ls_csrf:
                    csrf = env_ls_csrf[p]
                    break
            res = {"ports": sorted(set(ports)), "csrf": csrf}
            break
    except Exception:
        continue
print(json.dumps(res))
`
	out, err := exec.Command("python3", "-c", pyDiscover).Output()
	if err != nil {
		return 0, 0, ""
	}
	var parsed struct {
		Ports []int  `json:"ports"`
		Csrf  string `json:"csrf"`
	}
	if json.Unmarshal(out, &parsed) != nil || len(parsed.Ports) == 0 || parsed.Csrf == "" {
		return 0, 0, ""
	}
	var httpsPort, httpPort int
	for _, p := range parsed.Ports {
		conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 500 * time.Millisecond}, "tcp", fmt.Sprintf("127.0.0.1:%d", p), &tls.Config{InsecureSkipVerify: true})
		if err == nil {
			conn.Close()
			httpsPort = p
		} else {
			httpPort = p
		}
	}
	return httpsPort, httpPort, parsed.Csrf
}

func refreshHubLoop() {
	for {
		hp, htp, tok := discoverHub()
		if hp > 0 && tok != "" {
			hub.mu.Lock()
			hub.httpsPort = hp
			hub.httpPort = htp
			hub.csrfToken = tok
			hub.mu.Unlock()
		}
		time.Sleep(3 * time.Second)
	}
}

func parseVarint(b []byte) (uint64, int) {
	var x uint64
	var s uint
	for i, c := range b {
		if c < 0x80 {
			return x | uint64(c)<<s, i + 1
		}
		x |= uint64(c&0x7f) << s
		s += 7
	}
	return 0, 0
}

func parseReconnectProto(body []byte) (int, string) {
	if len(body) > 0 && body[0] == '{' {
		var m struct {
			Port  int    `json:"extensionServerPort"`
			Token string `json:"extensionServerCsrfToken"`
		}
		if json.Unmarshal(body, &m) == nil && m.Port > 0 {
			return m.Port, m.Token
		}
	}
	var port int
	var token string
	i := 0
	for i < len(body) {
		tag, n := parseVarint(body[i:])
		if n == 0 {
			break
		}
		i += n
		fieldNum := tag >> 3
		wireType := tag & 7
		if wireType == 0 {
			val, vn := parseVarint(body[i:])
			if vn == 0 {
				break
			}
			i += vn
			if fieldNum == 1 {
				port = int(val)
			}
		} else if wireType == 2 {
			length, ln := parseVarint(body[i:])
			if ln == 0 || i+ln+int(length) > len(body) {
				break
			}
			i += ln
			strVal := string(body[i : i+int(length)])
			i += int(length)
			if fieldNum == 2 {
				token = strVal
			}
		} else {
			break
		}
	}
	return port, token
}

func encodeVarint(x uint64) []byte {
	var buf []byte
	for x >= 0x80 {
		buf = append(buf, byte(x)|0x80)
		x >>= 7
	}
	buf = append(buf, byte(x))
	return buf
}

func encodeLenDelim(fieldNum int, data []byte) []byte {
	tag := uint64((fieldNum << 3) | 2)
	out := encodeVarint(tag)
	out = append(out, encodeVarint(uint64(len(data)))...)
	out = append(out, data...)
	return out
}

func pushSummariesLoop(sumDBPath string) {
	pyScript := fmt.Sprintf(`
import sqlite3, json, base64, os
db_path = %q
if not os.path.exists(db_path):
    print("{}")
else:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    rows = conn.execute("SELECT conversation_id, raw_summary FROM conversation_summaries WHERE raw_summary IS NOT NULL").fetchall()
    out = {}
    for k, v in rows:
        if isinstance(v, bytes):
            out[k] = base64.b64encode(v).decode()
    print(json.dumps(out))
`, sumDBPath)

	client := &http.Client{Timeout: 2 * time.Second}
	for {
		time.Sleep(2 * time.Second)
		hub.mu.RLock()
		extPort := hub.extPort
		extToken := hub.extToken
		hub.mu.RUnlock()
		if extPort == 0 || extToken == "" {
			continue
		}
		out, err := exec.Command("python3", "-c", pyScript).Output()
		if err != nil {
			continue
		}
		var m map[string]string
		if json.Unmarshal(out, &m) != nil || len(m) == 0 {
			continue
		}
		var topicBytes []byte
		for cid, b64 := range m {
			if _, err := base64.StdEncoding.DecodeString(b64); err != nil {
				continue
			}
			rowVal := encodeLenDelim(1, []byte(b64))
			rowVal = append(rowVal, encodeVarint((2<<3)|0)...)
			rowVal = append(rowVal, encodeVarint(1)...)
			entry := encodeLenDelim(1, []byte(cid))
			entry = append(entry, encodeLenDelim(2, rowVal)...)
			topicBytes = append(topicBytes, encodeLenDelim(1, entry)...)
		}
		reqProto := encodeLenDelim(1, []byte("trajectorySummaries"))
		reqProto = append(reqProto, encodeLenDelim(2, topicBytes)...)
		req, err := http.NewRequest("POST", fmt.Sprintf("http://127.0.0.1:%d/exa.extension_server_pb.ExtensionServerService/PushUnifiedStateSyncUpdate", extPort), bytes.NewReader(reqProto))
		if err == nil {
			req.Header.Set("Content-Type", "application/proto")
			req.Header.Set("x-codeium-csrf-token", extToken)
			if resp, err := client.Do(req); err == nil {
				resp.Body.Close()
			}
		}
	}
}

func main() {
	homeDir, _ := os.UserHomeDir()
	defaultBinDir := filepath.Join(homeDir, ".gemini", "jetski", "bin")
	defaultSumDB := filepath.Join(homeDir, ".gemini", "jetski", "conversation_summaries.db")

	httpsPortFlag := flag.Int("https_port", 37999, "Bridge HTTPS port")
	httpPortFlag := flag.Int("http_port", 37998, "Bridge HTTP port")
	certFile := flag.String("cert", filepath.Join(defaultBinDir, "ls_bridge_cert.pem"), "TLS cert path")
	keyFile := flag.String("key", filepath.Join(defaultBinDir, "ls_bridge_key.pem"), "TLS key path")
	extPortFlag := flag.Int("extension_server_port", 0, "Initial ExtensionServer port")
	extTokenFlag := flag.String("extension_server_csrf_token", "", "Initial ExtensionServer token")
	flag.Parse()

	hp, htp, tok := discoverHub()
	hub.httpsPort = hp
	hub.httpPort = htp
	hub.csrfToken = tok
	hub.extPort = *extPortFlag
	hub.extToken = *extTokenFlag

	go refreshHubLoop()
	go pushSummariesLoop(defaultSumDB)

	transport := &http.Transport{
		TLSClientConfig:   &tls.Config{InsecureSkipVerify: true, NextProtos: []string{"h2", "http/1.1"}},
		ForceAttemptHTTP2: true,
	}

	proxyHandler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/Exit") {
			w.Header().Set("Content-Type", r.Header.Get("Content-Type"))
			w.WriteHeader(http.StatusOK)
			return
		}
		if strings.HasSuffix(r.URL.Path, "/ReconnectExtensionServer") {
			body, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(body))
			ep, et := parseReconnectProto(body)
			if ep > 0 {
				hub.mu.Lock()
				hub.extPort = ep
				hub.extToken = et
				hub.mu.Unlock()
				log.Printf("Updated ExtensionServer to port=%d", ep)
			}
		}
		hub.mu.RLock()
		targetPort := hub.httpsPort
		targetToken := hub.csrfToken
		hub.mu.RUnlock()

		if targetPort == 0 {
			http.Error(w, "jetski-hub-server not found", http.StatusBadGateway)
			return
		}
		targetURL, _ := url.Parse(fmt.Sprintf("https://127.0.0.1:%d", targetPort))
		rp := httputil.NewSingleHostReverseProxy(targetURL)
		rp.Transport = transport
		rp.FlushInterval = -1
		origDirector := rp.Director
		rp.Director = func(req *http.Request) {
			origDirector(req)
			req.Header.Set("x-codeium-csrf-token", targetToken)
		}
		rp.ServeHTTP(w, r)
	})

	go func() {
		httpSrv := &http.Server{
			Addr:    fmt.Sprintf("127.0.0.1:%d", *httpPortFlag),
			Handler: proxyHandler,
		}
		log.Printf("Bridge HTTP listening on 127.0.0.1:%d", *httpPortFlag)
		_ = httpSrv.ListenAndServe()
	}()

	cert, err := tls.LoadX509KeyPair(*certFile, *keyFile)
	if err != nil {
		log.Fatalf("Failed to load cert/key: %v", err)
	}
	tlsSrv := &http.Server{
		Addr:    fmt.Sprintf("127.0.0.1:%d", *httpsPortFlag),
		Handler: proxyHandler,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{cert},
			NextProtos:   []string{"h2", "http/1.1"},
		},
	}
	log.Printf("Bridge HTTPS (h2) listening on 127.0.0.1:%d -> Hub httpsPort=%d", *httpsPortFlag, hub.httpsPort)
	log.Fatal(tlsSrv.ListenAndServeTLS("", ""))
}
