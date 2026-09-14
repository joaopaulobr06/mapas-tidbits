#!/usr/bin/env python3
"""
Baixa diariamente 4 mapas do Tropical Tidbits (pkg=apcpn, fh=360 fixo):
  - GFS   América (region=us)
  - GFS   Brasil   (region=samer)
  - ECMWF América (region=us)
  - ECMWF Brasil   (region=samer)

Resolve dinamicamente a rodada (runtime) mais recente e completa de cada
modelo consultando a pagina de analise do tropicaltidbits.com (o bloco
JSON-LD da pagina informa a URL real da imagem, incluindo o numero de
frame que o site usa internamente - que NAO e igual ao forecast hour;
ex.: ECMWF fh=360 -> frame 84, GFS fh=360 -> frame 60). Requer o header
Referer, sem o qual o site responde 403 Forbidden para as imagens.

Uso:
    python baixar_mapas_tidbits.py

Agendamento: ver o workflow do GitHub Actions (baixar-mapas.yml).
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------- CONFIGURAÇÃO ----------

PASTA_DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapas_tidbits")

FH_FIXO = 360  # forecast hour fixo, confirmado com o usuário
PKG = "apcpn"
MAX_RODADAS_ANTERIORES = 12  # fallback: quantas rodadas de 6h tentar para tras
SYNOPTIC_HOURS = (0, 6, 12, 18)

# (nome_exibicao, código_modelo_no_site)
MODELOS = [
    ("gfs", "gfs"),
    ("ecmwf", "ecmwf"),
]

# (nome_exibicao, código_região_no_site)
REGIOES = [
    ("america", "us"),
    ("brasil", "samer"),
]

BASE_URL = "https://www.tropicaltidbits.com/analysis/models/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MapDownloader/1.0)",
    "Referer": BASE_URL,
}

IMAGE_JSON_LD_RE = re.compile(r'"image"\s*:\s*"([^"]+)"')
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_TIMEOUT = 20

# ---------- LÓGICA ----------

def _rodadas_anteriores(ancora, quantidade):
    """Gera 'quantidade' rodadas sinoticas (00/06/12/18Z) anteriores a ancora
    (exclusive), da mais recente para a mais antiga."""
    current = ancora.replace(minute=0, second=0, microsecond=0)
    candidatos = []
    for _ in range(quantidade):
        idx = SYNOPTIC_HOURS.index(current.hour)
        if idx == 0:
            current = (current - timedelta(days=1)).replace(hour=SYNOPTIC_HOURS[-1])
        else:
            current = current.replace(hour=SYNOPTIC_HOURS[idx - 1])
        candidatos.append(current.strftime("%Y%m%d%H"))
    return candidatos


def _resolve_image_url(session, modelo_codigo, regiao_codigo, runtime):
    """Consulta a pagina de analise e extrai a URL real da imagem a partir do
    bloco JSON-LD embutido no HTML - evita qualquer suposicao sobre a
    numeracao de frame usada internamente pelo site.

    Se 'runtime' nao corresponder a uma rodada que o servidor reconhece, o
    proprio tropicaltidbits substitui silenciosamente pela rodada mais
    recente que ele considera valida - por isso o runtime efetivo e sempre
    extraido da URL retornada, nunca assumido a partir do parametro enviado.
    """
    params = {"model": modelo_codigo, "region": regiao_codigo, "pkg": PKG, "fh": FH_FIXO}
    if runtime is not None:
        params["runtime"] = runtime
    resp = session.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    match = IMAGE_JSON_LD_RE.search(resp.text)
    return match.group(1) if match else None


def _extrair_runtime_da_url(image_url):
    match = re.search(r"/(\d{10})/[^/]+\.png$", image_url)
    return match.group(1) if match else None


def _validar_png(session, url):
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None
    if "image/png" not in resp.headers.get("Content-Type", ""):
        return None
    if not resp.content.startswith(PNG_MAGIC):
        return None
    return resp.content


def _tentar_runtime(session, modelo_codigo, runtime, regioes_codigos):
    """Valida uma rodada candidata. IMPORTANTE: a renderizacao de cada regiao
    (us/samer) para o mesmo modelo+rodada pode terminar em momentos diferentes
    no servidor - por isso o frame so e aceito se o PNG de FH_FIXO existir de
    fato para TODAS as regioes pedidas, nao apenas para uma regiao de
    referencia. Sem essa checagem, uma regiao pode "vencer a corrida" e ficar
    com uma rodada mais nova cujo frame ainda nao foi renderizado para a
    outra regiao, causando 404."""
    try:
        image_url = _resolve_image_url(session, modelo_codigo, regioes_codigos[0], runtime)
    except requests.RequestException as e:
        print(f"[{modelo_codigo}] erro ao consultar rodada {runtime or '(automatica)'}: {e}")
        return None

    if not image_url:
        return None

    runtime_real = _extrair_runtime_da_url(image_url)
    if not runtime_real:
        return None

    if runtime is not None and runtime_real != runtime:
        # servidor ignorou o runtime pedido (nao existe) e substituiu por outro
        return None

    frame_match = re.search(r"_(\d+)\.png$", image_url)
    if not frame_match:
        return None
    frame_number = int(frame_match.group(1))

    for regiao_codigo in regioes_codigos:
        url_regiao = f"{BASE_URL}{modelo_codigo}/{runtime_real}/{modelo_codigo}_{PKG}_{regiao_codigo}_{frame_number}.png"
        try:
            conteudo = _validar_png(session, url_regiao)
        except requests.RequestException as e:
            print(f"[{modelo_codigo}] erro ao validar rodada {runtime_real} regiao {regiao_codigo}: {e}")
            return None
        if conteudo is None:
            return None  # frame de FH ainda nao renderizado para essa regiao nessa rodada

    return runtime_real, frame_number


def resolver_rodada(session, modelo_codigo, regioes_codigos):
    """Retorna (runtime, frame_number) da rodada mais recente cujo frame de
    FH_FIXO ja esteja renderizado para TODAS as regioes pedidas, com fallback
    para rodadas anteriores caso contrario."""
    try:
        auto_url = _resolve_image_url(session, modelo_codigo, regioes_codigos[0], None)
        ancora_str = _extrair_runtime_da_url(auto_url) if auto_url else None
    except requests.RequestException:
        ancora_str = None

    if ancora_str:
        ancora = datetime.strptime(ancora_str, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    else:
        ancora = datetime.now(timezone.utc)

    candidatos = ([ancora_str] if ancora_str else []) + _rodadas_anteriores(ancora, MAX_RODADAS_ANTERIORES)

    for runtime in candidatos:
        resultado = _tentar_runtime(session, modelo_codigo, runtime, regioes_codigos)
        if resultado is not None:
            return resultado

    return None, None


def baixar_com_retentativas(session, url, caminho, titulo):
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if "image/png" not in resp.headers.get("Content-Type", ""):
                raise ValueError(f"Content-Type inesperado: {resp.headers.get('Content-Type')}")
            if not resp.content.startswith(PNG_MAGIC):
                raise ValueError("conteudo recebido nao e um PNG valido")
            with open(caminho, "wb") as f:
                f.write(resp.content)
            print(f"[OK] {titulo}: salvo em {caminho} ({len(resp.content):,} bytes)")
            return True
        except (requests.RequestException, ValueError) as e:
            print(f"[tentativa {tentativa}/{MAX_RETRIES} falhou] {titulo}: {e}")
            if tentativa < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * tentativa)
    print(f"[FALHOU] {titulo}: nao foi possivel baixar apos {MAX_RETRIES} tentativas.")
    return False


def main():
    os.makedirs(PASTA_DESTINO, exist_ok=True)

    nome_pasta_dia = datetime.now().strftime("%Y-%m-%d")
    pasta_do_dia = os.path.join(PASTA_DESTINO, nome_pasta_dia)
    os.makedirs(pasta_do_dia, exist_ok=True)

    print(f"Iniciando download em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Salvando mapas em: {pasta_do_dia}")

    session = requests.Session()
    session.headers.update(HEADERS)

    regioes_codigos = [codigo for _, codigo in REGIOES]

    resultados = []
    for modelo_nome, modelo_codigo in MODELOS:
        runtime, frame_number = resolver_rodada(session, modelo_codigo, regioes_codigos)
        if runtime is None:
            print(f"[FALHOU] {modelo_nome.upper()}: nenhuma rodada completa encontrada "
                  f"nas ultimas {MAX_RODADAS_ANTERIORES} rodadas sinoticas.")
            for regiao_nome, _ in REGIOES:
                resultados.append(False)
            continue

        print(f"[{modelo_nome.upper()}] rodada completa: {runtime}Z (frame #{frame_number})")

        for regiao_nome, regiao_codigo in REGIOES:
            nome_arquivo_site = f"{modelo_codigo}_{PKG}_{regiao_codigo}_{frame_number}.png"
            url = f"{BASE_URL}{modelo_codigo}/{runtime}/{nome_arquivo_site}"
            nome_arquivo = f"{modelo_nome}_{regiao_nome}_{runtime}_fh{FH_FIXO}.png"
            caminho = os.path.join(pasta_do_dia, nome_arquivo)
            titulo = f"{modelo_nome.upper()} {regiao_nome}"
            resultados.append(baixar_com_retentativas(session, url, caminho, titulo))

    if not all(resultados):
        print("\nAVISO: pelo menos um mapa nao foi baixado.")
        sys.exit(1)

    print("\nTodos os mapas baixados com sucesso.")


if __name__ == "__main__":
    main()
