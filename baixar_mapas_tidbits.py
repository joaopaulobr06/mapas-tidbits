#!/usr/bin/env python3
"""
Baixa diariamente 4 mapas do Tropical Tidbits (pkg=apcpn, fh=360 fixo):
  - GFS   América (region=us)
  - GFS   Brasil   (region=samer)
  - ECMWF América (region=us)
  - ECMWF Brasil   (region=samer)

Pega sempre a rodada (runtime) mais recente disponível de cada modelo.

Uso:
    python baixar_mapas_tidbits.py

Agendamento: ver o workflow do GitHub Actions (baixar-mapas.yml).
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone

# ---------- CONFIGURAÇÃO ----------

PASTA_DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapas_tidbits")

FH_FIXO = 360  # forecast hour fixo, confirmado com o usuário
PKG = "apcpn"

# (nome_exibicao, código_modelo_no_site, horas_de_rodada_disponíveis em UTC)
MODELOS = [
    ("gfs", "gfs", [0, 6, 12, 18]),
    ("ecmwf", "ecmwf", [0, 12]),
]

# (nome_exibicao, código_região_no_site)
REGIOES = [
    ("america", "us"),
    ("brasil", "samer"),
]

BASE_URL = "https://www.tropicaltidbits.com/analysis/models"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MapDownloader/1.0)"
}

# ---------- LÓGICA ----------

def gerar_candidatos_rodada(horas_disponiveis, max_tentativas=8):
    """
    Gera candidatos de rodada (datetime UTC) da mais recente para trás,
    considerando o atraso de publicação do modelo (~4h após o horário nominal).
    """
    agora = datetime.now(timezone.utc)
    candidatos = []
    dia = agora
    for _ in range(3):  # olha hoje, ontem, anteontem se precisar
        for h in sorted(horas_disponiveis, reverse=True):
            candidato = dia.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidato <= agora - timedelta(hours=4):  # dá tempo do modelo processar
                candidatos.append(candidato)
        dia -= timedelta(days=1)
    return candidatos[:max_tentativas]


def montar_url(modelo_codigo, rodada_dt, regiao_codigo):
    yyyymmddhh = rodada_dt.strftime("%Y%m%d%H")
    nome_arquivo = f"{modelo_codigo}_{PKG}_{regiao_codigo}_{FH_FIXO}.png"
    return f"{BASE_URL}/{modelo_codigo}/{yyyymmddhh}/{nome_arquivo}", yyyymmddhh


def baixar_imagem(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        return None, f"erro de conexão: {e}"

    if r.status_code != 200:
        return None, f"status HTTP {r.status_code}"

    content_type = r.headers.get("Content-Type", "")
    if "image" not in content_type or len(r.content) < 5000:
        return None, f"resposta não parece ser imagem válida (content-type={content_type}, tamanho={len(r.content)})"

    return r.content, None


def baixar_modelo_regiao(modelo_nome, modelo_codigo, horas_disponiveis, regiao_nome, regiao_codigo, pasta_do_dia):
    for rodada in gerar_candidatos_rodada(horas_disponiveis):
        url, yyyymmddhh = montar_url(modelo_codigo, rodada, regiao_codigo)
        conteudo, erro = baixar_imagem(url)
        if conteudo:
            nome_arquivo = f"{modelo_nome}_{regiao_nome}_{yyyymmddhh}_fh{FH_FIXO}.png"
            caminho = os.path.join(pasta_do_dia, nome_arquivo)
            with open(caminho, "wb") as f:
                f.write(conteudo)
            print(f"[OK] {modelo_nome.upper()} {regiao_nome}: salvo em {caminho} (rodada {yyyymmddhh}Z, fh={FH_FIXO})")
            return True
        else:
            print(f"[tentativa falhou] {modelo_nome} {regiao_nome} rodada {yyyymmddhh}Z -> {erro}")
    print(f"[FALHOU] Não consegui baixar {modelo_nome.upper()} {regiao_nome} em nenhuma rodada recente.")
    return False


def main():
    os.makedirs(PASTA_DESTINO, exist_ok=True)

    nome_pasta_dia = datetime.now().strftime("%Y-%m-%d")
    pasta_do_dia = os.path.join(PASTA_DESTINO, nome_pasta_dia)
    os.makedirs(pasta_do_dia, exist_ok=True)

    print(f"Iniciando download em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Salvando mapas em: {pasta_do_dia}")
    resultados = []
    for modelo_nome, modelo_codigo, horas in MODELOS:
        for regiao_nome, regiao_codigo in REGIOES:
            ok = baixar_modelo_regiao(modelo_nome, modelo_codigo, horas, regiao_nome, regiao_codigo, pasta_do_dia)
            resultados.append(ok)

    if not all(resultados):
        print("\nAVISO: pelo menos um mapa não foi baixado. Isso pode indicar que o "
              "site mudou o padrão de URL. Veja o comentário no topo do script sobre "
              "como checar isso manualmente.")
        sys.exit(1)

    print("\nTodos os mapas baixados com sucesso.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# SE O SITE MUDAR O PADRÃO DE URL:
#   Abra o link no navegador (ex: tropicaltidbits.com/analysis/models/?model=gfs
#   &region=us&pkg=apcpn&runtime=AAAAMMDDHH&fh=360), clique com botão direito na
#   imagem -> "Copiar endereço da imagem" e compare com o padrão usado em
#   montar_url() acima. Ajuste nome_arquivo/BASE_URL conforme necessário.
# ---------------------------------------------------------------------------
