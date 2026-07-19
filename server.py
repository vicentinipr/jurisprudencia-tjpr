# -*- coding: utf-8 -*-
"""
Servidor MCP - Pesquisa de Jurisprudencia do TJPR
==================================================
Este programa funciona como um "estagiario virtual": ele recebe pedidos
do Claude, consulta o portal publico de jurisprudencia do TJPR
(https://portal.tjpr.jus.br/jurisprudencia/) e devolve os resultados
REAIS encontrados (ementas, numeros de processo, relator, data e link).

Voce NAO precisa editar este arquivo. Basta segui-lo ate a hospedagem.

Ferramentas oferecidas ao Claude:
  1. buscar_jurisprudencia  -> pesquisa por termos livres, com filtros
  2. diagnostico_tjpr       -> ferramenta de manutencao (se a busca
                               parar de funcionar, o Claude usa isto
                               para descobrir o que mudou no site)
"""

import os
import re
import html as html_lib
import unicodedata

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuracao basica do servidor
# ---------------------------------------------------------------------------
# O Render (servico de hospedagem) informa a "porta" pela variavel PORT.
PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP(
    "Jurisprudencia TJPR",
    host="0.0.0.0",
    port=PORT,
)

# Endereco do formulario publico de pesquisa de jurisprudencia do TJPR
TJPR_URL = "https://portal.tjpr.jus.br/jurisprudencia/publico/pesquisa.do"

# Pagina inicial publica de jurisprudencia (usada para criar a sessao antes da busca)
TJPR_HOME = "https://portal.tjpr.jus.br/jurisprudencia/"

# Cabecalhos que fazem a consulta parecer um navegador comum
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

TIMEOUT = httpx.Timeout(40.0, connect=15.0)


def _limpar(texto: str) -> str:
    """Remove espacos duplicados e caracteres invisiveis."""
    texto = html_lib.unescape(texto or "")
    texto = unicodedata.normalize("NFC", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _consultar_tjpr(params: dict) -> str:
    """Faz a consulta HTTP ao portal do TJPR e devolve o HTML da pagina.

    CORRECAO 2026: o portal do TJPR passou a exigir uma SESSAO valida antes
    da pesquisa. Se a busca for chamada "direto", o site apenas devolve o
    formulario vazio (0 resultados). Por isso, primeiro abrimos a pagina
    inicial de jurisprudencia (que cria o cookie de sessao jsessionid) e so
    entao enviamos a pesquisa, reaproveitando a MESMA sessao. O httpx.Client
    guarda e reenvia os cookies automaticamente dentro do mesmo "with".
    """
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as cli:
        # 1) Abre o formulario publico para obter o cookie de sessao (jsessionid)
        cli.get(TJPR_HOME)
        # 2) A pesquisa do TJPR e enviada por POST: os campos de busca
        #    (criterioPesquisa etc.) vao no CORPO da requisicao, e nao na URL.
        #    Enviar por GET apenas devolvia o formulario vazio. O actionType
        #    permanece na URL, exatamente como o proprio site faz ao pesquisar.
        resp = cli.post(
            TJPR_URL,
            params={"actionType": "pesquisar"},
            data=params,
            headers={"Referer": TJPR_HOME},
        )
        resp.raise_for_status()
        return resp.text


def _montar_params(
    termos: str,
    pagina: int = 1,
    data_julgamento_inicio: str = "",
    data_julgamento_fim: str = "",
) -> dict:
    """Monta os parametros aceitos pelo formulario de pesquisa do TJPR."""
    params = {
        "actionType": "pesquisar",
        "criterioPesquisa": termos,
        "mostrarCompleto": "true",
        "iniciar": "Pesquisar",
    }
    if data_julgamento_inicio:
        params["dataJulgamentoInicio"] = data_julgamento_inicio
    if data_julgamento_fim:
        params["dataJulgamentoFim"] = data_julgamento_fim
    if pagina and pagina > 1:
        params["pageNumber"] = str(pagina)
        params["pageNumberAtual"] = str(pagina)
    return params


def _extrair_resultados(html: str) -> list[dict]:
    """
    Le a pagina de resultados e extrai cada acordao encontrado.

    O TJPR pode mudar o layout do site a qualquer momento; por isso a
    extracao e feita em camadas: primeiro tenta os blocos "classicos"
    (tabelas de resultado) e, se nada for achado, tenta um metodo
    generico baseado no texto (procurando padroes como "Relator").
    """
    soup = BeautifulSoup(html, "html.parser")
    resultados: list[dict] = []

    # --- Camada 1: blocos/tabelas de resultado conhecidos -----------------
    blocos = soup.select(
        "table.resultTable, div.resultado, div.juris-resultado, "
        "div[id*=resultado] table, table[class*=result]"
    )
    for bloco in blocos:
        texto = _limpar(bloco.get_text(" ", strip=True))
        if len(texto) < 80:
            continue  # bloco pequeno demais para ser um acordao
        if "relator" not in texto.lower() and "processo" not in texto.lower():
            continue
        item = _interpretar_texto_do_acordao(texto)
        # Captura links do bloco (inteiro do acordao, detalhes etc.)
        links = []
        for a in bloco.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://portal.tjpr.jus.br" + href
            if href.startswith("http") and href not in links:
                links.append(href)
        if links:
            item["links"] = links[:3]
        resultados.append(item)

    if resultados:
        return resultados

    # --- Camada 2: metodo generico pelo texto da pagina -------------------
    texto_pagina = _limpar(soup.get_text(" ", strip=True))
    pedacos = re.split(r"(?=Processo[:\s])", texto_pagina)
    for pedaco in pedacos:
        if len(pedaco) < 150:
            continue
        if "relator" in pedaco.lower() or "ementa" in pedaco.lower():
            resultados.append(_interpretar_texto_do_acordao(pedaco[:4000]))

    return resultados


def _interpretar_texto_do_acordao(texto: str) -> dict:
    """Tenta separar, dentro do texto bruto, os campos mais importantes."""
    item: dict = {}

    m = re.search(r"Processo[:\s]+([\d.\-\/]+)", texto)
    if m:
        item["processo"] = m.group(1)

    m = re.search(r"Ac[oó]rd[aã]o[:\s]+([\d.\-\/]+)", texto, re.IGNORECASE)
    if m:
        item["acordao"] = m.group(1)

    m = re.search(
        r"Relator(?:a)?(?:\s*\(a\))?[:\s]+(.{3,80}?)(?=\s+(?:Org[aã]o|Comarca|Data|Julgamento|Publica|Ementa|Processo)|$)",
        texto,
        re.IGNORECASE,
    )
    if m:
        item["relator"] = _limpar(m.group(1))

    m = re.search(
        r"[OÓ]rg[aã]o\s+Julgador[:\s]+(.{3,80}?)(?=\s+(?:Comarca|Data|Julgamento|Publica|Relator|Ementa)|$)",
        texto,
        re.IGNORECASE,
    )
    if m:
        item["orgao_julgador"] = _limpar(m.group(1))

    m = re.search(
        r"(?:Data\s+(?:do\s+)?Julgamento|Julgamento)[:\s]+(\d{2}/\d{2}/\d{4})",
        texto,
        re.IGNORECASE,
    )
    if m:
        item["data_julgamento"] = m.group(1)

    m = re.search(r"Ementa[:\s]+(.+)", texto, re.IGNORECASE | re.DOTALL)
    if m:
        ementa = _limpar(m.group(1))
        item["ementa"] = ementa[:2500] + ("..." if len(ementa) > 2500 else "")
    else:
        item["texto"] = _limpar(texto)[:2500]

    return item


def _formatar_resposta(resultados: list[dict], termos: str, pagina: int) -> str:
    """Transforma a lista de resultados num texto claro para o Claude."""
    if not resultados:
        return (
            f"Nenhum resultado extraido para '{termos}' (pagina {pagina}).\n"
            "Possiveis causas: (a) realmente nao ha acordaos com esses termos; "
            "(b) o portal do TJPR mudou de layout; (c) o portal esta fora do ar.\n"
            "Sugestao: use a ferramenta 'diagnostico_tjpr' com os mesmos termos "
            "para inspecionar a resposta bruta do site e identificar a causa."
        )

    linhas = [
        f"RESULTADOS REAIS DO PORTAL DO TJPR para '{termos}' (pagina {pagina}).",
        f"Total de acordaos extraidos nesta pagina: {len(resultados)}.",
        "IMPORTANTE: cite apenas o que consta abaixo; nao complete de memoria.",
        "",
    ]
    for i, r in enumerate(resultados, 1):
        linhas.append(f"--- Resultado {i} ---")
        for chave, rotulo in [
            ("processo", "Processo"),
            ("acordao", "Acordao"),
            ("relator", "Relator(a)"),
            ("orgao_julgador", "Orgao Julgador"),
            ("data_julgamento", "Data do Julgamento"),
        ]:
            if r.get(chave):
                linhas.append(f"{rotulo}: {r[chave]}")
        if r.get("ementa"):
            linhas.append(f"Ementa: {r['ementa']}")
        elif r.get("texto"):
            linhas.append(f"Texto extraido: {r['texto']}")
        if r.get("links"):
            linhas.append("Links: " + " | ".join(r["links"]))
        linhas.append("")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Ferramenta 1: busca de jurisprudencia
# ---------------------------------------------------------------------------
@mcp.tool()
def buscar_jurisprudencia(
    termos: str,
    pagina: int = 1,
    data_julgamento_inicio: str = "",
    data_julgamento_fim: str = "",
) -> str:
    """Pesquisa acordaos no portal publico de jurisprudencia do TJPR.

    Args:
        termos: Palavras-chave da pesquisa (ex.: 'fraude licitacao peculato').
        pagina: Numero da pagina de resultados (1 = primeira).
        data_julgamento_inicio: Opcional, formato dd/mm/aaaa.
        data_julgamento_fim: Opcional, formato dd/mm/aaaa.

    Returns:
        Lista de acordaos reais com processo, relator, orgao julgador,
        data, ementa e links. Nunca invente resultados alem dos retornados.
    """
    if not termos or not termos.strip():
        return "Informe ao menos um termo de pesquisa."
    try:
        params = _montar_params(
            termos.strip(), pagina, data_julgamento_inicio, data_julgamento_fim
        )
        pagina_html = _consultar_tjpr(params)
    except httpx.TimeoutException:
        return (
            "O portal do TJPR demorou demais para responder (timeout). "
            "Tente novamente em instantes."
        )
    except httpx.HTTPStatusError as e:
        return (
            f"O portal do TJPR respondeu com erro HTTP {e.response.status_code}. "
            "O site pode estar temporariamente indisponivel."
        )
    except Exception as e:  # noqa: BLE001
        return f"Falha inesperada ao consultar o TJPR: {type(e).__name__}: {e}"

    resultados = _extrair_resultados(pagina_html)
    return _formatar_resposta(resultados, termos.strip(), pagina)


# ---------------------------------------------------------------------------
# Ferramenta 2: diagnostico (manutencao)
# ---------------------------------------------------------------------------
@mcp.tool()
def diagnostico_tjpr(termos: str = "peculato") -> str:
    """Ferramenta de manutencao: mostra um trecho da resposta BRUTA do portal
    do TJPR. Use somente quando 'buscar_jurisprudencia' nao retornar nada,
    para descobrir se o site mudou de layout ou esta fora do ar.

    Args:
        termos: Termos de teste para a consulta (padrao: 'peculato').

    Returns:
        Informacoes tecnicas da resposta do portal (status e amostra do HTML).
    """
    try:
        with httpx.Client(
            headers=HEADERS, timeout=TIMEOUT, follow_redirects=True
        ) as cli:
            cli.get(TJPR_HOME)  # abre a pagina primeiro (cria a sessao)
            resp = cli.post(
                TJPR_URL,
                params={"actionType": "pesquisar"},
                data=_montar_params(termos),
                headers={"Referer": TJPR_HOME},
            )
        soup = BeautifulSoup(resp.text, "html.parser")
        texto = _limpar(soup.get_text(" ", strip=True))
        return (
            f"URL consultada: {resp.url}\n"
            f"Status HTTP: {resp.status_code}\n"
            f"Tamanho do HTML: {len(resp.text)} caracteres\n"
            f"Amostra do texto da pagina (primeiros 3000 caracteres):\n\n"
            f"{texto[:3000]}"
        )
    except Exception as e:  # noqa: BLE001
        return f"Falha no diagnostico: {type(e).__name__}: {e}"




# ===========================================================================
# ===========================================================================
# BLOCO STJ - Pesquisa no SCON (scon.stj.jus.br)
# ===========================================================================
STJ_URL = "https://scon.stj.jus.br/SCON/pesquisar.jsp"

import cloudscraper
import requests

scraper_stj = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)


def _montar_params_stj(termos: str, pagina: int = 1) -> dict:
    """Monta os parametros da pesquisa livre do SCON/STJ (acordaos)."""
    params = {
        "livre": termos,
        "b": "ACOR",          # ACOR = acordaos
        "numDocsPagina": "10",
    }
    if pagina and pagina > 1:
        # No SCON, a paginacao usa o indice do primeiro documento da pagina
        params["i"] = str((pagina - 1) * 10 + 1)
    return params


def _extrair_resultados_stj(html: str) -> list[dict]:
    """
    Le a pagina de resultados do SCON. O layout classico usa pares de
    <div class="docTitulo"> (rotulo) e <div class="docTexto"> (conteudo)
    dentro de blocos <div class="documento">. Ha tambem um metodo
    generico de reserva, caso o site mude.
    """
    soup = BeautifulSoup(html, "html.parser")
    resultados: list[dict] = []

    mapa_rotulos = {
        "processo": "processo",
        "relator": "relator",
        "orgao julgador": "orgao_julgador",
        "data do julgamento": "data_julgamento",
        "data da publicacao": "data_publicacao",
        "ementa": "ementa",
    }

    # --- Camada 1: estrutura classica do SCON ------------------------------
    documentos = soup.select("div.documento")
    for doc in documentos:
        item: dict = {}
        titulos = doc.select("div.docTitulo, span.docTitulo")
        for t in titulos:
            rotulo = _limpar(t.get_text()).lower()
            rotulo = unicodedata.normalize("NFD", rotulo)
            rotulo = "".join(c for c in rotulo if not unicodedata.combining(c))
            corpo = t.find_next(class_="docTexto")
            if not corpo:
                continue
            valor = _limpar(corpo.get_text(" ", strip=True))
            for chave_rotulo, chave_item in mapa_rotulos.items():
                if rotulo.startswith(chave_rotulo):
                    if chave_item == "ementa":
                        valor = valor[:2500] + ("..." if len(valor) > 2500 else "")
                    item[chave_item] = valor
                    break
        if item:
            for a in doc.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = "https://scon.stj.jus.br" + href
                if "inteiro" in _limpar(a.get_text()).lower() or "Integra" in href:
                    item.setdefault("links", []).append(href)
            resultados.append(item)

    if resultados:
        return resultados

    # --- Camada 2: metodo generico pelo texto ------------------------------
    texto_pagina = _limpar(soup.get_text(" ", strip=True))
    pedacos = re.split(r"(?=Processo[:\s])", texto_pagina)
    for pedaco in pedacos:
        if len(pedaco) < 150:
            continue
        if "relator" in pedaco.lower() or "ementa" in pedaco.lower():
            resultados.append(_interpretar_texto_do_acordao(pedaco[:4000]))
    return resultados


@mcp.tool()
def buscar_jurisprudencia_stj(termos: str, pagina: int = 1) -> str:
    """Pesquisa acordaos na base publica de jurisprudencia do STJ (SCON).

    Args:
        termos: Palavras-chave da pesquisa livre (ex.: 'peculato dosimetria').
                Aceita operadores do SCON como E, OU, ADJ e aspas para
                expressao exata (ex.: "organizacao criminosa" E corrupcao).
        pagina: Numero da pagina de resultados (1 = primeira; 10 por pagina).

    Returns:
        Acordaos reais do STJ com processo, relator, orgao julgador, datas,
        ementa e links. Nunca invente resultados alem dos retornados.
    """
    if not termos or not termos.strip():
        return "Informe ao menos um termo de pesquisa."
    try:
        resp = scraper_stj.get(
            STJ_URL,
            params=_montar_params_stj(termos, pagina),
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        pagina_html = resp.text
    except requests.exceptions.Timeout:
        return "O portal do STJ demorou demais para responder. Tente novamente em instantes."
    except requests.exceptions.HTTPError as e:
        return (
            f"O portal do STJ respondeu com erro HTTP {e.response.status_code}. "
            "O site pode estar temporariamente indisponivel ou ter bloqueado a consulta."
        )
    except Exception as e:  # noqa: BLE001
        return f"Falha inesperada ao consultar o STJ: {type(e).__name__}: {e}"

    resultados = _extrair_resultados_stj(pagina_html)
    if not resultados:
        return (
            f"Nenhum resultado extraido no STJ para '{termos}' (pagina {pagina}).\n"
            "Pode nao haver acordaos com esses termos, ou o SCON mudou de layout / "
            "bloqueou a consulta automatizada. Use 'diagnostico_stj' para investigar."
        )
    texto = _formatar_resposta(resultados, termos.strip(), pagina)
    return texto.replace("PORTAL DO TJPR", "PORTAL DO STJ (SCON)")


@mcp.tool()
def diagnostico_stj(termos: str = "peculato") -> str:
    """Ferramenta de manutencao do STJ: mostra a resposta BRUTA do SCON.
    Use somente quando 'buscar_jurisprudencia_stj' nao retornar nada.

    Args:
        termos: Termos de teste (padrao: 'peculato').

    Returns:
        Status HTTP e amostra do texto da pagina retornada pelo SCON.
    """
    try:
        resp = scraper_stj.get(
            STJ_URL,
            params=_montar_params_stj(termos),
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        texto = _limpar(soup.get_text(" ", strip=True))
        return (
            f"URL consultada: {resp.url}\n"
            f"Status HTTP: {resp.status_code}\n"
            f"Tamanho do HTML: {len(resp.text)} caracteres\n"
            f"Amostra do texto (primeiros 3000 caracteres):\n\n{texto[:3000]}"
        )
    except Exception as e:  # noqa: BLE001
        return f"Falha no diagnostico do STJ: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Inicio do servidor
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # "streamable-http" e o formato de conexao que o Claude usa para
    # conectores personalizados. O endereco final tera o sufixo /mcp
    # (ex.: https://SEU-APP.onrender.com/mcp).
    mcp.run(transport="streamable-http")
