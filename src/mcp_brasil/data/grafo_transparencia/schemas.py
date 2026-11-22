"""Pydantic v2 schemas for the Grafo de Transparência feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "municipio",
    "empresa",
    "pessoa",
    "politico",
    "contrato",
    "emenda",
    "sancao",
    "despesa",
]

SeverityLevel = Literal["ALTA", "MEDIA", "BAIXA", "INFO"]


class GraphNode(BaseModel):
    """Nó canônico no grafo de transparência."""

    node_id: str
    tipo: str
    chave_canonica: str
    nome: str
    uf: str = ""
    codigo_ibge: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Aresta direcionada ligando duas entidades com rastreabilidade de fonte."""

    edge_id: str
    src_id: str
    dst_id: str
    tipo_relacao: str
    valor: float = 0.0
    data_ref: str = ""
    confianca: float = 1.0
    fonte: str = ""
    url_evidencia: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RiskAlert(BaseModel):
    """Indício explicável gerado pelos detectores de cruzamento para triagem de auditoria."""

    alert_id: str
    regra: str
    severidade: SeverityLevel
    score: float = Field(ge=0.0, le=100.0)
    entidade_id: str
    entidade_nome: str = ""
    titulo: str
    explicacao: str
    evidencias: list[dict[str, Any]] = Field(default_factory=list)


class GraphPathStep(BaseModel):
    """Passo individual em um caminho entre duas entidades do grafo."""

    from_id: str
    from_nome: str
    to_id: str
    to_nome: str
    tipo_relacao: str
    valor: float = 0.0
    fonte: str = ""
    url_evidencia: str = ""


class GraphPath(BaseModel):
    """Caminho encontrado entre duas entidades no grafo (BFS)."""

    origem_id: str
    destino_id: str
    saltos: int
    passos: list[GraphPathStep] = Field(default_factory=list)


class DossierResult(BaseModel):
    """Resultado consolidado de uma investigação 360° ou rastreamento de rede."""

    alvo_id: str
    alvo_tipo: str
    alvo_nome: str
    resumo: str = ""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    alerts: list[RiskAlert] = Field(default_factory=list)
    fontes_consultadas: list[str] = Field(default_factory=list)
    fontes_opcionais_ausentes: list[str] = Field(default_factory=list)
    estatisticas: dict[str, Any] = Field(default_factory=dict)
    tempo_ms: int = 0


class HarvestImportSummary(BaseModel):
    """Resumo da ingestão de arquivo NDJSON gerado por `transparencia-go harvest`."""

    arquivo: str
    municipios_lidos: int = 0
    nodes_inseridos: int = 0
    edges_inseridas: int = 0
    alerts_gerados: int = 0
    erros_leitura: int = 0
