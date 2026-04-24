#!/usr/bin/env python3
"""Script para corrigir testes — adiciona resp.text a todos os MagicMocks."""
import re

# Ler o ficheiro
with open('/root/.openclaw/workspace/trading-bot-hyperliquid/tests/test_data_aggregator.py', 'r') as f:
    content = f.read()

# Padrão: resp = MagicMock() seguido de resp.status_code = 200
# Vamos adicionar resp.text = '{}' após resp.status_code

# Primeiro, adicionar import do mock_response no início
if 'from conftest import mock_response' not in content:
    content = content.replace(
        'from data_aggregator import DataAggregator, retry_on_failure',
        'from conftest import mock_response\nfrom data_aggregator import DataAggregator, retry_on_failure'
    )

# Substituir padrões de criação de mock sem text
# Padrão 1: resp = MagicMock()\n            resp.status_code = 200
content = re.sub(
    r'(resp = MagicMock\(\)\s+resp\.status_code = 200)(?!\s+resp\.text)',
    r'\1\n            resp.text = \'{}\'',
    content
)

# Padrão 2: resp = MagicMock() seguido de outras linhas
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    new_lines.append(lines[i])
    if 'resp = MagicMock()' in lines[i] and i + 1 < len(lines):
        # Verificar se a próxima linha já tem resp.text
        next_lines = '\n'.join(lines[i+1:i+4])
        if 'resp.text' not in next_lines and 'resp.status_code' in next_lines:
            # Inserir resp.text após status_code
            j = i + 1
            while j < len(lines) and 'resp.status_code' not in lines[j]:
                new_lines.append(lines[j])
                j += 1
            if j < len(lines):
                new_lines.append(lines[j])  # status_code line
                new_lines.append("            resp.text = '{}'")
                i = j
    i += 1

content = '\n'.join(new_lines)

# Guardar
with open('/root/.openclaw/workspace/trading-bot-hyperliquid/tests/test_data_aggregator.py', 'w') as f:
    f.write(content)

print("✅ Testes corrigidos!")
