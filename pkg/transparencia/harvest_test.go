package transparencia

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRunHarvestOfflineMockAndResume(t *testing.T) {
	eng, err := NewEngine()
	if err != nil {
		t.Fatalf("NewEngine failed: %v", err)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/dca"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"items":[{"exercicio":2024,"instituicao":"Prefeitura Mock","anexo":"DCA-Anexo I-D","coluna":"Despesas Pagas","cod_conta":"3.3.90","conta":"Servicos","valor":125000.0}]}`)
		default:
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			fmt.Fprint(w, `<html><body><table>
				<tr><th>Nome</th><th>Cargo</th><th>Salário Bruto</th></tr>
				<tr><td>SERVIDOR HARVEST</td><td>ANALISTA</td><td>R$ 6.500,00</td></tr>
			</table></body></html>`)
		}
	}))
	defer srv.Close()

	// Redireciona todo tráfego HTTP do hub para o servidor mock local sem rede externa
	mockClient := &http.Client{
		Timeout: 3 * time.Second,
		Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
			cloned := req.Clone(req.Context())
			cloned.URL.Scheme = "http"
			cloned.URL.Host = strings.TrimPrefix(srv.URL, "http://")
			return srv.Client().Transport.RoundTrip(cloned)
		}),
	}

	hub := &Hub{
		Engine:     eng,
		HTTPClient: mockClient,
		TCE:        NewTCEClient(mockClient),
		Siconfi:    NewSiconfiClientWithBaseURL(srv.URL, mockClient),
		PNCP:       NewPNCPClientWithBaseURL(srv.URL, mockClient),
	}

	tmpDir := t.TempDir()
	outPath := filepath.Join(tmpDir, "harvest_test.ndjson")

	// 1. Primeira execução: 2 municípios explícitos
	cfg := HarvestConfig{
		IBGEs:     []string{"3157807", "3106200"},
		Modules:   []string{"servidores", "despesas"},
		Ano:       2024,
		Limit:     5,
		Workers:   2,
		RateLimit: 5 * time.Millisecond,
		Timeout:   4 * time.Second,
		OutPath:   outPath,
	}

	summary, err := RunHarvest(context.Background(), hub, cfg, nil)
	if err != nil {
		t.Fatalf("RunHarvest failed: %v", err)
	}
	if summary.TotalTargets != 2 || summary.Processed != 2 || summary.SkippedResume != 0 {
		t.Fatalf("unexpected harvest summary: %+v", summary)
	}
	if summary.WithData == 0 {
		t.Fatalf("expected at least 1 harvested city with data, got %+v", summary)
	}

	// Verifica linhas NDJSON
	raw, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatalf("reading outPath: %v", err)
	}
	sc := bufio.NewScanner(bytes.NewReader(raw))
	var recs []HarvestRecord
	for sc.Scan() {
		var r HarvestRecord
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			t.Fatalf("invalid NDJSON line: %v", err)
		}
		recs = append(recs, r)
	}
	if len(recs) != 2 {
		t.Fatalf("expected 2 NDJSON records, got %d", len(recs))
	}

	// 2. Segunda execução com Resume=true: deve pular ambos os IBGEs já coletados
	cfg.Resume = true
	resumed, err := RunHarvest(context.Background(), hub, cfg, nil)
	if err != nil {
		t.Fatalf("RunHarvest resume failed: %v", err)
	}
	if resumed.SkippedResume != 2 || resumed.Processed != 0 {
		t.Fatalf("expected SkippedResume=2 Processed=0, got %+v", resumed)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
