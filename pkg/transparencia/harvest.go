package transparencia

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// HarvestConfig configura a coleta em lote (batch) de dados municipais em formato NDJSON.
type HarvestConfig struct {
	UF        string
	IBGEs     []string
	Modules   []string
	Ano       int
	Mes       int
	Limit     int
	Workers   int
	RateLimit time.Duration
	Timeout   time.Duration
	OutPath   string
	Resume    bool
	APIOnly   bool
	MaxCities int
}

// HarvestRecord representa uma linha NDJSON contendo os dados coletados de um município.
type HarvestRecord struct {
	CodigoIBGE      string            `json:"codigo_ibge"`
	Nome            string            `json:"nome"`
	UF              string            `json:"uf"`
	CNPJ            string            `json:"cnpj,omitempty"`
	Latitude        float64           `json:"latitude,omitempty"`
	Longitude       float64           `json:"longitude,omitempty"`
	Populacao       int               `json:"populacao,omitempty"`
	Plataforma      string            `json:"plataforma"`
	PortalURL       string            `json:"portal_url,omitempty"`
	FonteDescoberta string            `json:"fonte_descoberta,omitempty"`
	CollectedAt     string            `json:"collected_at"`
	DurationMs      int64             `json:"duration_ms"`
	Servidores      []ServidorItem    `json:"servidores,omitempty"`
	Despesas        []DespesaItem     `json:"despesas,omitempty"`
	Receitas        []ReceitaItem     `json:"receitas,omitempty"`
	Licitacoes      []LicitacaoItem   `json:"licitacoes,omitempty"`
	Contratos       []ContratoItem    `json:"contratos,omitempty"`
	Diarias         []DiariaItem      `json:"diarias,omitempty"`
	Obras           []ObraItem        `json:"obras,omitempty"`
	Convenios       []ConvenioItem    `json:"convenios,omitempty"`
	Errors          map[string]string `json:"errors,omitempty"`
}

// HarvestSummary consolida estatísticas finais da execução do harvest.
type HarvestSummary struct {
	TotalTargets  int            `json:"total_targets"`
	SkippedResume int            `json:"skipped_resume"`
	Processed     int            `json:"processed"`
	WithData      int            `json:"with_data"`
	WithErrors    int            `json:"with_errors"`
	ModuleCounts  map[string]int `json:"module_counts"`
	DurationMs    int64          `json:"duration_ms"`
	OutPath       string         `json:"out_path,omitempty"`
}

type hostRateLimiter struct {
	mu       sync.Mutex
	interval time.Duration
	nextAt   map[string]time.Time
}

func newHostRateLimiter(interval time.Duration) *hostRateLimiter {
	return &hostRateLimiter{
		interval: interval,
		nextAt:   make(map[string]time.Time),
	}
}

func (l *hostRateLimiter) Wait(ctx context.Context, rawURL string) error {
	if l == nil || l.interval <= 0 {
		return nil
	}
	host := "_default"
	if u, err := url.Parse(strings.TrimSpace(rawURL)); err == nil && u.Hostname() != "" {
		host = strings.ToLower(u.Hostname())
	}

	l.mu.Lock()
	now := time.Now()
	target := l.nextAt[host]
	if target.Before(now) {
		target = now
	}
	l.nextAt[host] = target.Add(l.interval)
	wait := time.Until(target)
	l.mu.Unlock()

	if wait <= 0 {
		return nil
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// LoadHarvestedIBGEs lê códigos IBGE já presentes em um arquivo NDJSON para suportar --resume.
func LoadHarvestedIBGEs(path string) (map[string]bool, error) {
	seen := make(map[string]bool)
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return seen, nil
		}
		return nil, err
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var stub struct {
			CodigoIBGE string `json:"codigo_ibge"`
		}
		if err := json.Unmarshal([]byte(line), &stub); err == nil && stub.CodigoIBGE != "" {
			seen[stub.CodigoIBGE] = true
		}
	}
	if err := sc.Err(); err != nil {
		return seen, err
	}
	return seen, nil
}

func normalizeHarvestModules(modules []string) []string {
	allowed := map[string]bool{
		"servidores": true,
		"despesas":   true,
		"receitas":   true,
		"licitacoes": true,
		"contratos":  true,
		"diarias":    true,
		"obras":      true,
		"convenios":  true,
	}
	var out []string
	seen := make(map[string]bool)
	for _, raw := range modules {
		for _, part := range strings.Split(raw, ",") {
			m := strings.ToLower(strings.TrimSpace(part))
			if allowed[m] && !seen[m] {
				seen[m] = true
				out = append(out, m)
			}
		}
	}
	if len(out) == 0 {
		return []string{"servidores", "contratos", "despesas"}
	}
	return out
}

func selectHarvestTargets(eng *Engine, cfg HarvestConfig) []*Municipio {
	ufFilter := strings.ToUpper(strings.TrimSpace(cfg.UF))
	ibgeSet := make(map[string]bool, len(cfg.IBGEs))
	for _, raw := range cfg.IBGEs {
		for _, part := range strings.Split(raw, ",") {
			c := strings.TrimSpace(part)
			if c != "" {
				ibgeSet[c] = true
			}
		}
	}

	var apiSet map[string]bool
	if cfg.APIOnly {
		apiList := eng.ListPortaisCatalogMunicipios(ufFilter)
		apiSet = make(map[string]bool, len(apiList))
		for _, rec := range apiList {
			apiSet[rec.CodigoIBGE] = true
		}
	}

	var targets []*Municipio
	for _, m := range eng.municipios {
		if ufFilter != "" && !strings.EqualFold(m.UF, ufFilter) {
			continue
		}
		if len(ibgeSet) > 0 && !ibgeSet[m.CodigoIBGE] {
			continue
		}
		if apiSet != nil && !apiSet[m.CodigoIBGE] {
			continue
		}
		targets = append(targets, m)
	}
	sort.Slice(targets, func(i, j int) bool {
		if targets[i].Populacao != targets[j].Populacao {
			return targets[i].Populacao > targets[j].Populacao
		}
		return targets[i].CodigoIBGE < targets[j].CodigoIBGE
	})
	if cfg.MaxCities > 0 && len(targets) > cfg.MaxCities {
		targets = targets[:cfg.MaxCities]
	}
	return targets
}

// RunHarvest executa a coleta em lote concorrente e grava cada município em NDJSON.
func RunHarvest(ctx context.Context, hub *Hub, cfg HarvestConfig, w io.Writer) (*HarvestSummary, error) {
	start := time.Now()
	if hub == nil || hub.Engine == nil {
		return nil, fmt.Errorf("hub e engine são obrigatórios para harvest")
	}

	modules := normalizeHarvestModules(cfg.Modules)
	workers := cfg.Workers
	if workers <= 0 {
		workers = 8
	}
	if workers > 32 {
		workers = 32
	}
	limit := cfg.Limit
	if limit <= 0 {
		limit = 100
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	rateLimit := cfg.RateLimit
	if rateLimit < 0 {
		rateLimit = 0
	}

	var outFile *os.File
	if w == nil {
		if strings.TrimSpace(cfg.OutPath) != "" {
			if dir := filepath.Dir(cfg.OutPath); dir != "" && dir != "." {
				if err := os.MkdirAll(dir, 0o755); err != nil {
					return nil, fmt.Errorf("erro criando diretório de saída %s: %w", dir, err)
				}
			}
			flags := os.O_CREATE | os.O_WRONLY
			if cfg.Resume {
				flags |= os.O_APPEND
			} else {
				flags |= os.O_TRUNC
			}
			f, err := os.OpenFile(cfg.OutPath, flags, 0o644)
			if err != nil {
				return nil, fmt.Errorf("erro abrindo arquivo de saída %s: %w", cfg.OutPath, err)
			}
			outFile = f
			defer outFile.Close()
			w = outFile
		} else {
			w = os.Stdout
		}
	}

	alreadyDone := make(map[string]bool)
	if cfg.Resume && strings.TrimSpace(cfg.OutPath) != "" {
		loaded, err := LoadHarvestedIBGEs(cfg.OutPath)
		if err != nil {
			return nil, fmt.Errorf("erro lendo checkpoint %s: %w", cfg.OutPath, err)
		}
		alreadyDone = loaded
	}

	allTargets := selectHarvestTargets(hub.Engine, cfg)
	summary := &HarvestSummary{
		TotalTargets: len(allTargets),
		ModuleCounts: make(map[string]int, len(modules)),
		OutPath:      cfg.OutPath,
	}

	var queue []*Municipio
	for _, m := range allTargets {
		if alreadyDone[m.CodigoIBGE] {
			summary.SkippedResume++
			continue
		}
		queue = append(queue, m)
	}

	limiter := newHostRateLimiter(rateLimit)
	var writeMu sync.Mutex
	jobs := make(chan *Municipio, len(queue))
	for _, m := range queue {
		jobs <- m
	}
	close(jobs)

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for m := range jobs {
				if ctx.Err() != nil {
					return
				}
				rec := harvestOneMunicipio(ctx, hub, m, modules, cfg.Ano, cfg.Mes, limit, timeout, limiter)

				lineBytes, err := json.Marshal(rec)
				if err != nil {
					continue
				}
				writeMu.Lock()
				_, _ = w.Write(append(lineBytes, '\n'))
				summary.Processed++
				hasData := false
				if n := len(rec.Servidores); n > 0 {
					summary.ModuleCounts["servidores"] += n
					hasData = true
				}
				if n := len(rec.Despesas); n > 0 {
					summary.ModuleCounts["despesas"] += n
					hasData = true
				}
				if n := len(rec.Receitas); n > 0 {
					summary.ModuleCounts["receitas"] += n
					hasData = true
				}
				if n := len(rec.Licitacoes); n > 0 {
					summary.ModuleCounts["licitacoes"] += n
					hasData = true
				}
				if n := len(rec.Contratos); n > 0 {
					summary.ModuleCounts["contratos"] += n
					hasData = true
				}
				if n := len(rec.Diarias); n > 0 {
					summary.ModuleCounts["diarias"] += n
					hasData = true
				}
				if n := len(rec.Obras); n > 0 {
					summary.ModuleCounts["obras"] += n
					hasData = true
				}
				if n := len(rec.Convenios); n > 0 {
					summary.ModuleCounts["convenios"] += n
					hasData = true
				}
				if hasData {
					summary.WithData++
				}
				if len(rec.Errors) > 0 {
					summary.WithErrors++
				}
				writeMu.Unlock()
			}
		}()
	}
	wg.Wait()

	summary.DurationMs = time.Since(start).Milliseconds()
	return summary, nil
}

func harvestOneMunicipio(
	ctx context.Context,
	hub *Hub,
	m *Municipio,
	modules []string,
	ano, mes, limit int,
	timeout time.Duration,
	limiter *hostRateLimiter,
) HarvestRecord {
	start := time.Now()
	cityCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	disc := DiscoverPortal(cityCtx, m, DiscoverOptions{
		HTTPClient:   hub.HTTPClient,
		TCEPIBaseURL: hub.TCE.tcePIBaseURL,
		Timeout:      timeout,
	})

	rec := HarvestRecord{
		CodigoIBGE:      m.CodigoIBGE,
		Nome:            m.Nome,
		UF:              m.UF,
		CNPJ:            m.CNPJ,
		Latitude:        m.Latitude,
		Longitude:       m.Longitude,
		Populacao:       m.Populacao,
		Plataforma:      disc.Plataforma,
		PortalURL:       disc.URLResolvida,
		FonteDescoberta: disc.FonteDescoberta,
		CollectedAt:     time.Now().UTC().Format(time.RFC3339),
	}

	opts := QueryOptions{
		UF:      m.UF,
		Ano:     ano,
		Mes:     mes,
		Limit:   limit,
		Timeout: timeout,
	}

	addErr := func(mod, msg string) {
		if rec.Errors == nil {
			rec.Errors = make(map[string]string)
		}
		rec.Errors[mod] = msg
	}

	for _, mod := range modules {
		if err := limiter.Wait(cityCtx, disc.URLResolvida); err != nil {
			addErr(mod, err.Error())
			continue
		}
		switch mod {
		case "servidores":
			res, err := hub.queryServidoresWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Servidores = res.Servidores
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "despesas":
			res, err := hub.queryDespesasWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Despesas = res.Despesas
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "receitas":
			res, err := hub.queryReceitasWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Receitas = res.Receitas
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "licitacoes":
			res, err := hub.queryLicitacoesWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Licitacoes = res.Licitacoes
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "contratos":
			res, err := hub.queryContratosWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Contratos = res.Contratos
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "diarias":
			res, err := hub.queryDiariasWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Diarias = res.Diarias
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "obras":
			res, err := hub.queryObrasWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Obras = res.Obras
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		case "convenios":
			res, err := hub.queryConveniosWithDiscovery(cityCtx, m, disc, opts)
			if err != nil {
				addErr(mod, err.Error())
			} else if res != nil && res.Disponivel {
				rec.Convenios = res.Convenios
			} else if res != nil && res.Mensagem != "" {
				addErr(mod, res.Mensagem)
			}
		}
	}

	rec.DurationMs = time.Since(start).Milliseconds()
	return rec
}
