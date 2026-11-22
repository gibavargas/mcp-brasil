"""Formatters and graph exporters (Markdown dossier, JSON, Mermaid, self-contained HTML)."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .constants import LGPD_AUDIT_DISCLAIMER
from .schemas import DossierResult, GraphEdge, GraphNode, GraphPath, RiskAlert

_MERMAID_ID_RE = re.compile(r"[^A-Za-z0-9_]")


def _fmt_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _mermaid_node_id(node_id: str) -> str:
    cleaned = _MERMAID_ID_RE.sub("_", node_id)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "n_" + cleaned
    return cleaned[:48]


def _mermaid_label(text: str, max_len: int = 48) -> str:
    safe = (text or "").replace('"', "'").replace("\n", " ").replace("[", "(").replace("]", ")")
    if len(safe) > max_len:
        safe = safe[: max_len - 3] + "..."
    return safe


def format_dossier_markdown(dossier: DossierResult) -> str:
    """Renderiza um DossierResult completo em Markdown investigativo estruturado."""
    lines: list[str] = [
        f"# Dossiê 360° — {dossier.alvo_nome}",
        "",
        f"- **Entidade**: `{dossier.alvo_id}` (`{dossier.alvo_tipo}`)",
        (
            f"- **Subgrafo mapeado**: **{len(dossier.nodes)}** nós | "
            f"**{len(dossier.edges)}** relações | **{len(dossier.alerts)}** indícios"
        ),
        f"- **Tempo de execução**: {dossier.tempo_ms} ms",
    ]
    if dossier.fontes_consultadas:
        lines.append(f"- **Fontes consultadas**: {', '.join(dossier.fontes_consultadas)}")
    if dossier.fontes_opcionais_ausentes:
        opt_str = ", ".join(dossier.fontes_opcionais_ausentes)
        lines.append(f"- **Fontes opcionais não configuradas**: {opt_str}")

    if dossier.resumo:
        lines.extend(["", dossier.resumo])

    # Seção 1 — Indícios / Alertas ordenados por severidade e score
    lines.extend(["", "## Indícios para Triagem e Auditoria"])
    if not dossier.alerts:
        lines.append(
            "_Nenhum indício de risco disparado nas regras executadas para o subgrafo atual._"
        )
    else:
        lines.extend(
            [
                "",
                "| Severidade | Score | Regra | Título |",
                "|---|---:|---|---|",
            ]
        )
        for a in dossier.alerts[:25]:
            lines.append(f"| **{a.severidade}** | {a.score:.1f} | `{a.regra}` | {a.titulo} |")
        lines.append("")
        for a in dossier.alerts[:10]:
            lines.append(f"### [{a.severidade} • {a.score:.1f}] {a.titulo}")
            lines.append(a.explicacao)
            urls = [str(ev.get("url")) for ev in a.evidencias if ev.get("url")]
            if urls:
                lines.append(f"- Evidências: {', '.join(dict.fromkeys(urls[:3]))}")
            lines.append("")

    # Seção 2 — Principais Relações Mapeadas
    lines.extend(["## Principais Relações no Grafo"])
    if not dossier.edges:
        lines.append("_Nenhuma relação registrada._")
    else:
        node_names = {n.node_id: n.nome for n in dossier.nodes}
        lines.extend(
            [
                "",
                "| Origem | Relação | Destino | Valor | Fonte |",
                "|---|---|---|---:|---|",
            ]
        )
        sorted_edges = sorted(dossier.edges, key=lambda e: (-e.valor, e.tipo_relacao, e.edge_id))
        for e in sorted_edges[:30]:
            src_n = node_names.get(e.src_id, e.src_id)
            dst_n = node_names.get(e.dst_id, e.dst_id)
            val_s = _fmt_brl(e.valor) if e.valor > 0 else "—"
            lines.append(f"| {src_n} | `{e.tipo_relacao}` | {dst_n} | {val_s} | {e.fonte} |")

    lines.extend(["", f"> {LGPD_AUDIT_DISCLAIMER}"])
    return "\n".join(lines)


def format_paths_markdown(
    origem_label: str,
    destino_label: str,
    paths: list[GraphPath],
) -> str:
    """Renderiza os menores caminhos encontrados entre duas entidades."""
    lines = [
        f"# Conexões no Grafo: {origem_label} ↔ {destino_label}",
        "",
    ]
    if not paths:
        lines.append(
            "_Nenhum caminho encontrado entre as duas entidades no grafo DuckDB atual. "
            "Execute `investigar_municipio_360`, `investigar_fornecedor_360` ou "
            "`importar_harvest_grafo` para enriquecer o grafo._"
        )
        return "\n".join(lines)

    for idx, path in enumerate(paths, start=1):
        lines.append(f"## Caminho #{idx} ({path.saltos} salto(s))")
        for step in path.passos:
            val_txt = f" ({_fmt_brl(step.valor)})" if step.valor > 0 else ""
            src_txt = f" — fonte: {step.fonte}" if step.fonte else ""
            lines.append(
                f"- **{step.from_nome}** —`{step.tipo_relacao}`→ "
                f"**{step.to_nome}**{val_txt}{src_txt}"
            )
        lines.append("")

    lines.append(f"> {LGPD_AUDIT_DISCLAIMER}")
    return "\n".join(lines)


def export_graph_json(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    alerts: list[RiskAlert] | None = None,
) -> str:
    """Serializa subgrafo completo em JSON estruturado."""
    payload: dict[str, Any] = {
        "nodes": [n.model_dump() for n in nodes],
        "edges": [e.model_dump() for e in edges],
        "alerts": [a.model_dump() for a in (alerts or [])],
        "disclaimer": LGPD_AUDIT_DISCLAIMER,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def export_graph_mermaid(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    max_edges: int = 60,
) -> str:
    """Exporta o subgrafo em sintaxe Mermaid (`flowchart LR`) com classes por tipo de entidade."""
    lines: list[str] = [
        "flowchart LR",
        "    classDef municipio fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a;",
        "    classDef empresa fill:#fef3c7,stroke:#d97706,color:#78350f;",
        "    classDef pessoa fill:#dcfce7,stroke:#15803d,color:#14532d;",
        "    classDef politico fill:#f3e8ff,stroke:#7e22ce,color:#581c87;",
        "    classDef sancao fill:#fee2e2,stroke:#dc2626,color:#7f1d1d;",
    ]
    used_nodes: dict[str, GraphNode] = {}
    selected_edges = sorted(edges, key=lambda e: (-e.valor, e.tipo_relacao))[:max_edges]
    node_map = {n.node_id: n for n in nodes}

    for e in selected_edges:
        if e.src_id in node_map:
            used_nodes[e.src_id] = node_map[e.src_id]
        if e.dst_id in node_map:
            used_nodes[e.dst_id] = node_map[e.dst_id]
    if not used_nodes:
        for n in nodes[:30]:
            used_nodes[n.node_id] = n

    for nid, node in used_nodes.items():
        mid = _mermaid_node_id(nid)
        lbl = _mermaid_label(f"{node.nome} ({node.tipo})")
        lines.append(f'    {mid}["{lbl}"]:::{node.tipo}')

    for e in selected_edges:
        if e.src_id not in used_nodes or e.dst_id not in used_nodes:
            continue
        s_id = _mermaid_node_id(e.src_id)
        d_id = _mermaid_node_id(e.dst_id)
        elbl = e.tipo_relacao
        if e.valor > 0:
            elbl = f"{e.tipo_relacao} ({_fmt_brl(e.valor)})"
        lines.append(f'    {s_id} -->|"{_mermaid_label(elbl, 42)}"| {d_id}')

    return "\n".join(lines)


def export_graph_html(
    title: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    alerts: list[RiskAlert] | None = None,
) -> str:
    """Gera visualizador HTML interativo autocontido (SVG + force-directed em JS puro, sem CDN)."""
    safe_title = html.escape(title or "Grafo de Transparência Pública")
    graph_data = {
        "nodes": [
            {"id": n.node_id, "label": n.nome, "tipo": n.tipo, "uf": n.uf} for n in nodes[:120]
        ],
        "edges": [
            {
                "source": e.src_id,
                "target": e.dst_id,
                "label": e.tipo_relacao,
                "valor": e.valor,
                "fonte": e.fonte,
            }
            for e in edges[:200]
        ],
        "alerts": [
            {
                "severidade": a.severidade,
                "score": a.score,
                "titulo": a.titulo,
                "explicacao": a.explicacao,
            }
            for a in (alerts or [])[:30]
        ],
    }
    data_json = json.dumps(graph_data, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{safe_title}</title>
<style>
  body {{
    margin: 0; font-family: system-ui, -apple-system, sans-serif;
    background: #0f172a; color: #e2e8f0; display: flex; height: 100vh; overflow: hidden;
  }}
  #canvas-wrap {{ flex: 1; position: relative; }}
  svg {{ width: 100%; height: 100%; cursor: grab; }}
  #sidebar {{
    width: 340px; background: #1e293b; border-left: 1px solid #334155;
    padding: 16px; overflow-y: auto;
  }}
  h1 {{ font-size: 16px; margin: 0 0 8px; color: #f8fafc; }}
  .badge {{
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 11px; font-weight: 700; margin-right: 6px;
  }}
  .ALTA {{ background: #991b1b; color: #fecaca; }}
  .MEDIA {{ background: #92400e; color: #fde68a; }}
  .BAIXA, .INFO {{ background: #1e3a8a; color: #bfdbfe; }}
  .card {{
    background: #0f172a; border: 1px solid #334155; border-radius: 6px;
    padding: 10px; margin-bottom: 10px; font-size: 12px;
  }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; font-size: 11px; }}
  .dot {{
    width: 10px; height: 10px; border-radius: 50%;
    display: inline-block; margin-right: 4px;
  }}
  .disclaimer {{
    font-size: 11px; color: #94a3b8; margin-top: 12px;
    border-top: 1px solid #334155; padding-top: 8px;
  }}
</style>
</head>
<body>
<div id="canvas-wrap"><svg id="graph"></svg></div>
<aside id="sidebar">
  <h1>{safe_title}</h1>
  <div class="legend">
    <span><i class="dot" style="background:#3b82f6"></i>Município</span>
    <span><i class="dot" style="background:#f59e0b"></i>Empresa</span>
    <span><i class="dot" style="background:#22c55e"></i>Pessoa</span>
    <span><i class="dot" style="background:#a855f7"></i>Político</span>
    <span><i class="dot" style="background:#ef4444"></i>Sanção</span>
  </div>
  <div id="info" class="card">Clique ou arraste um nó para inspecionar conexões.</div>
  <div id="alerts"></div>
  <div class="disclaimer">{html.escape(LGPD_AUDIT_DISCLAIMER)}</div>
</aside>
<script>
const DATA = {data_json};
const COLORS = {{
  municipio: '#3b82f6', empresa: '#f59e0b', pessoa: '#22c55e',
  politico: '#a855f7', sancao: '#ef4444'
}};
const svg = document.getElementById('graph');
const W = svg.clientWidth || 900, H = svg.clientHeight || 650;
const nodes = DATA.nodes.map((n, i) => ({{
  ...n,
  x: W/2 + Math.cos(i * 0.7) * (120 + (i % 5) * 35),
  y: H/2 + Math.sin(i * 0.7) * (120 + (i % 5) * 35),
  vx: 0, vy: 0
}}));
const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
const edges = DATA.edges.filter(e => byId[e.source] && byId[e.target]);
const ns = 'http://www.w3.org/2000/svg';
const gEdges = document.createElementNS(ns, 'g');
const gNodes = document.createElementNS(ns, 'g');
svg.appendChild(gEdges); svg.appendChild(gNodes);

const edgeEls = edges.map(e => {{
  const line = document.createElementNS(ns, 'line');
  line.setAttribute('stroke', '#475569'); line.setAttribute('stroke-width', '1.5');
  gEdges.appendChild(line);
  return {{ e, line }};
}});

const nodeEls = nodes.map(n => {{
  const g = document.createElementNS(ns, 'g');
  const c = document.createElementNS(ns, 'circle');
  c.setAttribute('r', n.tipo === 'municipio' ? 14 : 10);
  c.setAttribute('fill', COLORS[n.tipo] || '#64748b');
  c.setAttribute('stroke', '#f8fafc'); c.setAttribute('stroke-width', '1.5');
  const t = document.createElementNS(ns, 'text');
  t.textContent = n.label.slice(0, 26);
  t.setAttribute('fill', '#e2e8f0'); t.setAttribute('font-size', '11');
  t.setAttribute('dx', '14'); t.setAttribute('dy', '4');
  g.appendChild(c); g.appendChild(t);
  g.addEventListener('click', () => {{
    const rels = edges.filter(e => e.source === n.id || e.target === n.id)
      .map(e => {{
        const peer = e.source === n.id ? byId[e.target].label : byId[e.source].label;
        return `<li>${{e.label}}: ${{peer}}</li>`;
      }}).join('');
    const ul = rels || '<li>Sem arestas</li>';
    document.getElementById('info').innerHTML =
      `<strong>${{n.label}}</strong> (${{n.tipo}})<br/><ul>${{ul}}</ul>`;
  }});
  gNodes.appendChild(g);
  return {{ n, g }};
}});

const alertsDiv = document.getElementById('alerts');
DATA.alerts.forEach(a => {{
  const d = document.createElement('div');
  d.className = 'card';
  d.innerHTML =
    `<span class="badge ${{a.severidade}}">${{a.severidade}} ${{a.score}}</span>`
    + `<strong>${{a.titulo}}</strong><p>${{a.explicacao}}</p>`;
  alertsDiv.appendChild(d);
}});

for (let step = 0; step < 120; step++) {{
  for (let i = 0; i < nodes.length; i++) {{
    for (let j = i + 1; j < nodes.length; j++) {{
      const dx = nodes[j].x - nodes[i].x, dy = nodes[j].y - nodes[i].y;
      const dist = Math.hypot(dx, dy) || 1;
      const rep = 2200 / (dist * dist);
      nodes[i].vx -= (dx / dist) * rep; nodes[i].vy -= (dy / dist) * rep;
      nodes[j].vx += (dx / dist) * rep; nodes[j].vy += (dy / dist) * rep;
    }}
  }}
  edges.forEach(e => {{
    const a = byId[e.source], b = byId[e.target];
    const dx = b.x - a.x, dy = b.y - a.y, dist = Math.hypot(dx, dy) || 1;
    const f = (dist - 120) * 0.03;
    a.vx += (dx / dist) * f; a.vy += (dy / dist) * f;
    b.vx -= (dx / dist) * f; b.vy -= (dy / dist) * f;
  }});
  nodes.forEach(n => {{
    n.vx = (n.vx + (W/2 - n.x) * 0.004) * 0.85;
    n.vy = (n.vy + (H/2 - n.y) * 0.004) * 0.85;
    n.x = Math.max(30, Math.min(W - 30, n.x + n.vx));
    n.y = Math.max(30, Math.min(H - 30, n.y + n.vy));
  }});
}}
edgeEls.forEach(({{ e, line }}) => {{
  line.setAttribute('x1', byId[e.source].x); line.setAttribute('y1', byId[e.source].y);
  line.setAttribute('x2', byId[e.target].x); line.setAttribute('y2', byId[e.target].y);
}});
nodeEls.forEach(({{ n, g }}) => g.setAttribute('transform', `translate(${{n.x}},${{n.y}})`));
</script>
</body>
</html>"""
