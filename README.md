# Sincronizador de Leads: Supabase -> Google Sheets + Mini Painel Web

Sistema automatizado em Python/Docker para extrair métricas de movimentação e leads ativos do Supabase e sincronizar em tempo real com planilhas do Google Sheets a cada **10 minutos**, incluindo um **Mini Site Web** para gerenciar campanhas e planilhas facilmente pelo navegador (ideal para EasyPanel).

---

## 🌟 Funcionalidades

- **Painel Web Visual (Mini Site):**
  - Adicione, edite ou remova campanhas e planilhas diretamente pelo navegador.
  - Botão **"Sincronizar Agora"** para disparar atualização imediata sob demanda.
  - Visualização de logs em tempo real, status do serviço e contagem regressiva para a próxima execução.
  - Ajuste de intervalo de sincronização (padrão: **10 minutos**).
- **Sincronização Automática com Supabase:**
  - Extrai os dados diários da view `vw_movimentacao_diaria_grupos`.
  - Atualiza as colunas: **A** (Data `DD/MM/YYYY`), **B** (Entradas) e **C** (Saídas).
  - Garante e preserva a fórmula da coluna **D** (`=B{linha}-C{linha}`).
  - Toda vez que o dia vira, cria automaticamente a linha de baixo preservando o histórico das linhas superiores.
  - Preenche a célula **G2** com a quantidade de **Leads Ativos** da view `vw_leads_ativos_grupos`.
- **Pronto para EasyPanel:**
  - `Dockerfile` configurado para expor a porta **5000**.
  - O painel web permite configurar tudo sem precisar abrir terminais ou editar arquivos manualmente no servidor.

---

## 📁 Estrutura do Projeto

```text
├── templates/
│   └── index.html              # Interface web do painel (Tailwind CSS)
├── main.py                     # Servidor Web Flask + Worker de sincronização
├── config.json                 # Configuração persistente das campanhas
├── google_credentials.json     # Chave da Conta de Serviço do Google Cloud
├── requirements.txt            # Dependências Python (Flask, Supabase, gspread...)
├── Dockerfile                  # Imagem Docker com porta 5000 exposta
├── docker-compose.yml          # Configuração Docker Compose local/produção
├── .env                        # Variáveis de ambiente
└── README.md                   # Esta documentação
```

---

## 🐳 Como Subir no EasyPanel

1. **Suba este projeto para um repositório Git** (GitHub, GitLab, etc.).
2. **No EasyPanel:**
   - Crie um novo projeto ou abra um existente.
   - Clique em **+ Service > App**.
   - Conecte ao seu repositório Git. O EasyPanel reconhecerá o `Dockerfile`.
3. **Configuração de Portas / Rede no EasyPanel:**
   - Em **Domains**, aponte o seu domínio ou use o domínio temporário do EasyPanel apontando para a porta **`5000`**.
4. **Variáveis de Ambiente (Environment):**
   ```env
   SUPABASE_URL=https://bjfapsvlhojiouarbzap.supabase.co
   SUPABASE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
   PORT=5000
   ```
   *Credencial do Google:*
   - Copie o conteúdo do arquivo `google_credentials.json` e adicione a variável:
     ```env
     GOOGLE_SERVICE_ACCOUNT_JSON={"type": "service_account", ...}
     ```
     *(Dessa forma não é necessário montar volumes no EasyPanel!)*
5. Clique em **Deploy**.
6. Acesse o link gerado pelo EasyPanel e gerencie todas as suas campanhas e planilhas pelo painel visual!

---

## 💻 Como Rodar Localmente

1. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```
2. Inicie a aplicação:
   ```bash
   python main.py
   ```
3. Abra no navegador:
   ```text
   http://localhost:5000
   ```
