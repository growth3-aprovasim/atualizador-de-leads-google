FROM python:3.11-slim

# Evitar criação de arquivos .pyc e garantir output imediato de logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependências do sistema necessárias
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Configurar timezone (padrão América/São_Paulo)
ENV TZ=America/Sao_Paulo

# Copiar dependências e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código da aplicação
COPY . .

# Expor porta do Painel Web (padrão 5000)
EXPOSE 5000

# Comando de inicialização com logs sem buffer
CMD ["python", "-u", "main.py"]
