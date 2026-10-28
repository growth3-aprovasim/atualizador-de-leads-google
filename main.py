import os
import sys
import time
import json
import logging
import datetime
import threading
from typing import List, Dict, Any, Optional
from collections import deque

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from supabase import create_client, Client
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Carrega variáveis de ambiente
load_dotenv()

# Fila circular para armazenar os últimos 60 logs em memória para exibição no mini site
LOG_BUFFER = deque(maxlen=60)

class BufferedLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass

# Configuração de Logs
logger = logging.getLogger("leads-sync")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

buffer_handler = BufferedLogHandler()
buffer_handler.setFormatter(formatter)
logger.addHandler(buffer_handler)

# Estado global da sincronização
SYNC_LOCK = threading.Lock()
LAST_SYNC_TIME: Optional[datetime.datetime] = None
NEXT_SYNC_TIME: Optional[datetime.datetime] = None
IS_SYNCING = False

CONFIG_PATH = os.getenv("CONFIG_FILE", "config.json")


def load_config() -> Dict[str, Any]:
    """Carrega as configurações do config.json."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Erro ao ler {CONFIG_PATH}: {e}")

    # Padrão com a campanha inicial caso o arquivo não exista
    default_config = {
        "sync_interval_minutes": 10,
        "campaigns": [
            {
                "name": "Projeto TJ-SP Pós-Edital [PES-PPE-26]",
                "campaign_filter": "[PES-PPE-26]",
                "exclude_vip": True,
                "is_vip": False,
                "spreadsheet_id": "1XDa4buD5pLFbidn9vkBILUTjR8p-aRkBd50mzHXsg3M",
                "sheet_name": "LEAD TOTAL"
            }
        ]
    }
    save_config(default_config)
    return default_config


def save_config(cfg: Dict[str, Any]) -> None:
    """Salva as configurações atualizadas no config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info("Configuração atualizada com sucesso no disco.")
    except Exception as e:
        logger.error(f"Erro ao salvar {CONFIG_PATH}: {e}")


def get_google_client() -> gspread.Client:
    """Obtém cliente autenticado do Google Sheets com suporte flexível a JSON direto ou arquivo."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    # 1. Tentar pegar JSON direto de qualquer variável possível
    env_candidates = [
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
        os.getenv("GOOGLE_CREDENTIALS_PATH"),
        os.getenv("GOOGLE_CREDENTIALS_JSON"),
        os.getenv("GOOGLE_CREDS"),
        os.getenv("GOOGLE_CREDENTIALS")
    ]

    for candidate in env_candidates:
        if candidate and isinstance(candidate, str):
            candidate_clean = candidate.strip()
            # Remover possíveis aspas externas que o EasyPanel ou shell possam ter adicionado
            if (candidate_clean.startswith('"') and candidate_clean.endswith('"')) or \
               (candidate_clean.startswith("'") and candidate_clean.endswith("'")):
                candidate_clean = candidate_clean[1:-1].strip()

            if "{" in candidate_clean and ("private_key" in candidate_clean or "service_account" in candidate_clean):
                try:
                    creds_dict = json.loads(candidate_clean)
                    # Corrigir quebra de linha escapada na private_key se necessário
                    if "private_key" in creds_dict and "\\n" in creds_dict["private_key"]:
                        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
                    
                    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                    return gspread.authorize(credentials)
                except Exception as e:
                    logger.error(f"Tentativa de ler credenciais do Google via JSON falhou: {e}")

    # 2. Tentar arquivo físico no disco
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
    if os.path.exists(creds_path) and os.path.isfile(creds_path):
        try:
            return gspread.service_account(filename=creds_path)
        except Exception as e:
            logger.error(f"Erro ao carregar arquivo '{creds_path}': {e}")

    if os.path.exists("google_credentials.json"):
        try:
            return gspread.service_account(filename="google_credentials.json")
        except Exception as e:
            logger.error(f"Erro ao carregar 'google_credentials.json': {e}")

    raise FileNotFoundError(
        "Credenciais do Google não encontradas! No EasyPanel, adicione a variável GOOGLE_SERVICE_ACCOUNT_JSON "
        "com o JSON da conta de serviço."
    )


def get_supabase_client() -> Client:
    """Obtém cliente do Supabase."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL e SUPABASE_KEY são obrigatórios no ambiente.")
    return create_client(url, key)


def sync_campaign_to_sheet(
    supabase_client: Client,
    google_client: gspread.Client,
    campaign_cfg: Dict[str, Any]
) -> None:
    """Sincroniza uma campanha configurada para o Google Sheets."""
    name = campaign_cfg.get("name", "Campanha")
    filter_tag = campaign_cfg.get("campaign_filter", "")
    exclude_vip = campaign_cfg.get("exclude_vip", False)
    is_vip = campaign_cfg.get("is_vip", False)
    spreadsheet_id = campaign_cfg.get("spreadsheet_id")
    sheet_name = campaign_cfg.get("sheet_name", "LEAD TOTAL")

    if not spreadsheet_id:
        logger.warning(f"[{name}] Spreadsheet ID não informado. Ignorando.")
        return

    logger.info(f"Sincronizando: '{name}' | Filtro: '{filter_tag}' | Aba: '{sheet_name}'")

    # 1. Movimentação no Supabase
    query_mov = supabase_client.table("vw_movimentacao_diaria_grupos").select("*")
    if filter_tag:
        query_mov = query_mov.ilike("nome_campanha", f"%{filter_tag}%")
    res_mov = query_mov.order("dia").execute()
    mov_rows = res_mov.data or []

    # Filtrar VIP de forma inteligente
    # Se for aba VIP, pega VIP. Se for aba LEAD TOTAL (geral), exclui VIP para não contaminar
    is_vip_sheet = "vip" in sheet_name.lower()
    
    if is_vip or is_vip_sheet:
        mov_rows = [r for r in mov_rows if "vip" in str(r.get("nome_campanha", "")).lower()]
        leads_rows = [r for r in leads_rows if "vip" in str(r.get("nome_campanha", "")).lower()]
    elif exclude_vip or "total" in sheet_name.lower():
        mov_rows = [r for r in mov_rows if "vip" not in str(r.get("nome_campanha", "")).lower()]
        leads_rows = [r for r in leads_rows if "vip" not in str(r.get("nome_campanha", "")).lower()]

    total_leads = sum(int(r.get("quantidade_de_leads", 0)) for r in leads_rows)

    # 3. Google Sheets
    spreadsheet = google_client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)

    # 4. Ler estado atual da Coluna A (Datas), Coluna I (Datas) e Coluna J (Leads no Dia)
    col_a = worksheet.col_values(1)
    col_j = worksheet.col_values(10)

    # Mapear datas que já estão na Coluna A e encontrar a última linha ocupada por datas
    date_to_row: Dict[str, int] = {}
    last_date_row = 1  # Linha 1 é o cabeçalho
    for idx, cell_val in enumerate(col_a, start=1):
        if idx == 1:
            continue
        clean_date = str(cell_val).strip()
        if clean_date and ("/" in clean_date or "-" in clean_date):
            date_to_row[clean_date] = idx
            if idx > last_date_row:
                last_date_row = idx

    # Mapear valores já salvos na Coluna J para cada linha
    existing_leads_by_row: Dict[int, Any] = {}
    for idx, cell_val in enumerate(col_j, start=1):
        if idx == 1:
            continue
        clean_val = str(cell_val).strip()
        if clean_val and clean_val != "":
            existing_leads_by_row[idx] = clean_val

    updates = []
    inserted_count = 0
    updated_count = 0

    hoje_str = datetime.date.today().strftime("%d/%m/%Y")

    # Mapear os registros de movimentação acumulando (SOMANDO) por data para evitar sobrescritas!
    mov_by_date: Dict[str, Dict[str, int]] = {}
    for item in mov_rows:
        raw_date = item.get("dia")
        if not raw_date:
            continue
        try:
            d_obj = datetime.datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
            dia_f = d_obj.strftime("%d/%m/%Y")
        except ValueError:
            dia_f = str(raw_date)

        entradas = int(item.get("entradas", 0))
        saidas = int(item.get("saidas", 0))
        saldo = int(item.get("saldo_dia", entradas - saidas))

        if dia_f in mov_by_date:
            mov_by_date[dia_f]["entradas"] += entradas
            mov_by_date[dia_f]["saidas"] += saidas
            mov_by_date[dia_f]["saldo"] += saldo
        else:
            mov_by_date[dia_f] = {
                "entradas": entradas,
                "saidas": saidas,
                "saldo": saldo
            }

    # Ordenar datas cronologicamente
    sorted_dates = sorted(
        mov_by_date.keys(),
        key=lambda d: datetime.datetime.strptime(d, "%d/%m/%Y") if "/" in d else d
    )

    # Calcular histórico retroativo de leads por dia (de trás para frente) caso ainda não esteja salvo
    calculated_leads_by_date: Dict[str, int] = {}
    if sorted_dates:
        curr_leads = total_leads
        for d in reversed(sorted_dates):
            calculated_leads_by_date[d] = curr_leads
            saldo_dia = mov_by_date[d]["saldo"]
            curr_leads = max(0, curr_leads - saldo_dia)

    # Próxima linha livre para novas datas (sempre contínua após a última data existente)
    current_free_row = max(last_date_row + 1, 2)

    # Processar cada dia ordenado
    for dia_formatado in sorted_dates:
        item_data = mov_by_date[dia_formatado]
        entradas = item_data["entradas"]
        saidas = item_data["saidas"]

        if dia_formatado in date_to_row:
            row_idx = date_to_row[dia_formatado]
            updated_count += 1
        else:
            row_idx = current_free_row
            current_free_row += 1
            date_to_row[dia_formatado] = row_idx
            inserted_count += 1
            # Coluna A: Data
            updates.append({
                "range": f"A{row_idx}",
                "values": [[dia_formatado]]
            })

        # Atualiza Colunas B e C (Entradas e Saídas)
        updates.append({
            "range": f"B{row_idx}:C{row_idx}",
            "values": [[entradas, saidas]]
        })

        # Coluna D: Fórmula (=B{row_idx}-C{row_idx})
        updates.append({
            "range": f"D{row_idx}",
            "values": [[f"=B{row_idx}-C{row_idx}"]]
        })

        # Coluna I: DATA
        updates.append({
            "range": f"I{row_idx}",
            "values": [[dia_formatado]]
        })

        # Coluna J: LEADS NO DIA
        if dia_formatado == hoje_str or dia_formatado == sorted_dates[-1]:
            leads_dia_val = total_leads
            updates.append({
                "range": f"J{row_idx}",
                "values": [[leads_dia_val]]
            })
        elif row_idx not in existing_leads_by_row:
            leads_dia_val = calculated_leads_by_date.get(dia_formatado, total_leads)
            updates.append({
                "range": f"J{row_idx}",
                "values": [[leads_dia_val]]
            })

    # Atualiza célula G2 com Total de Leads Ativos da Campanha
    updates.append({
        "range": "G2",
        "values": [[total_leads]]
    })

    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        logger.info(
            f"[{name}] Concluído com sucesso! "
            f"Atualizadas: {updated_count}, Novas linhas: {inserted_count}, Total leads (G2): {total_leads}"
        )


def execute_full_sync() -> bool:
    """Executa o ciclo de sincronização de todas as campanhas protegendo com Lock."""
    global LAST_SYNC_TIME, IS_SYNCING
    if not SYNC_LOCK.acquire(blocking=False):
        logger.warning("Uma sincronização já está em andamento. Aguarde a finalização.")
        return False

    IS_SYNCING = True
    try:
        cfg = load_config()
        campaigns = cfg.get("campaigns", [])
        if not campaigns:
            logger.info("Nenhuma campanha para sincronizar.")
            return True

        supabase_cli = get_supabase_client()
        google_cli = get_google_client()

        for camp in campaigns:
            try:
                sync_campaign_to_sheet(supabase_cli, google_cli, camp)
            except Exception as e:
                logger.error(f"Erro ao processar '{camp.get('name')}': {e}", exc_info=True)

        LAST_SYNC_TIME = datetime.datetime.now()
        logger.info("Ciclo de sincronização finalizado com sucesso.")
        return True
    except Exception as e:
        logger.error(f"Erro geral no ciclo de sincronização: {e}", exc_info=True)
        return False
    finally:
        IS_SYNCING = False
        SYNC_LOCK.release()


def background_scheduler_worker():
    """Worker de background que roda continuamente e dispara a cada X minutos."""
    global NEXT_SYNC_TIME
    logger.info("Iniciando agendador em background...")
    
    # Primeira execução imediata
    execute_full_sync()

    while True:
        cfg = load_config()
        interval = max(int(cfg.get("sync_interval_minutes", 10)), 1)
        NEXT_SYNC_TIME = datetime.datetime.now() + datetime.timedelta(minutes=interval)
        logger.info(f"Próxima sincronização automática programada para: {NEXT_SYNC_TIME.strftime('%H:%M:%S')}")

        while datetime.datetime.now() < NEXT_SYNC_TIME:
            time.sleep(2)

        logger.info("Disparando sincronização programada de 10 minutos...")
        execute_full_sync()


# --- SERVIDOR WEB FLASK ---
app = Flask(__name__, template_folder="templates")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({
        "status": "online",
        "is_syncing": IS_SYNCING,
        "last_sync": LAST_SYNC_TIME.isoformat() if LAST_SYNC_TIME else None,
        "next_sync": NEXT_SYNC_TIME.isoformat() if NEXT_SYNC_TIME else None,
        "config": cfg,
        "logs": list(LOG_BUFFER)
    })

@app.route("/api/sync-now", methods=["POST"])
def api_sync_now():
    # Roda em thread separada para não bloquear a resposta HTTP
    threading.Thread(target=execute_full_sync, daemon=True).start()
    return jsonify({"message": "Sincronização iniciada em segundo plano!"})

@app.route("/api/campaigns", methods=["POST"])
def api_add_campaign():
    data = request.json or {}
    name = data.get("name", "").strip()
    filter_val = data.get("campaign_filter", "").strip()
    sheet_name = data.get("sheet_name", "LEAD TOTAL").strip()
    spreadsheet_id = data.get("spreadsheet_id", "").strip()
    exclude_vip = bool(data.get("exclude_vip", False))
    is_vip = bool(data.get("is_vip", False))

    if not name or not filter_val or not spreadsheet_id:
        return jsonify({"error": "Preencha os campos obrigatórios (Nome, Filtro e ID da Planilha)."}), 400

    cfg = load_config()
    campaigns = cfg.get("campaigns", [])
    campaigns.append({
        "name": name,
        "campaign_filter": filter_val,
        "exclude_vip": exclude_vip,
        "is_vip": is_vip,
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": sheet_name
    })
    cfg["campaigns"] = campaigns
    save_config(cfg)
    logger.info(f"Nova campanha adicionada via painel web: '{name}' ({filter_val})")
    return jsonify({"message": "Campanha cadastrada com sucesso!"})

@app.route("/api/campaigns/<int:index>", methods=["PUT"])
def api_edit_campaign(index):
    data = request.json or {}
    cfg = load_config()
    campaigns = cfg.get("campaigns", [])

    if index < 0 or index >= len(campaigns):
        return jsonify({"error": "Campanha não encontrada."}), 404

    campaigns[index] = {
        "name": data.get("name", "").strip(),
        "campaign_filter": data.get("campaign_filter", "").strip(),
        "exclude_vip": bool(data.get("exclude_vip", False)),
        "is_vip": bool(data.get("is_vip", False)),
        "spreadsheet_id": data.get("spreadsheet_id", "").strip(),
        "sheet_name": data.get("sheet_name", "LEAD TOTAL").strip()
    }
    cfg["campaigns"] = campaigns
    save_config(cfg)
    logger.info(f"Campanha [{index}] atualizada via painel web.")
    return jsonify({"message": "Campanha atualizada com sucesso!"})

@app.route("/api/campaigns/<int:index>", methods=["DELETE"])
def api_delete_campaign(index):
    cfg = load_config()
    campaigns = cfg.get("campaigns", [])

    if index < 0 or index >= len(campaigns):
        return jsonify({"error": "Campanha não encontrada."}), 404

    removed = campaigns.pop(index)
    cfg["campaigns"] = campaigns
    save_config(cfg)
    logger.info(f"Campanha '{removed.get('name')}' removida via painel web.")
    return jsonify({"message": "Campanha removida com sucesso!"})

@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.json or {}
    interval = data.get("sync_interval_minutes")
    if not interval or int(interval) < 1:
        return jsonify({"error": "Intervalo inválido."}), 400

    cfg = load_config()
    cfg["sync_interval_minutes"] = int(interval)
    save_config(cfg)
    logger.info(f"Intervalo de sincronização atualizado para {interval} minutos via painel web.")
    return jsonify({"message": "Intervalo atualizado com sucesso!"})


if __name__ == "__main__":
    # Inicia a thread de sincronização automática em background
    scheduler_thread = threading.Thread(target=background_scheduler_worker, daemon=True)
    scheduler_thread.start()

    # Porta configurável para o EasyPanel / Servidores (padrão 5000)
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Iniciando Mini Site / Painel Web em http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
