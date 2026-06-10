# Sanitizador de Base SQLite para Demonstração

## 1. Objetivo

Este projeto cria uma **cópia sanitizada** de uma base SQLite real para uso em demonstrações comerciais internas. A base original é preservada, copiada para `output/` e somente a cópia é alterada.

A ferramenta foi pensada para bases relacionais SQLite usadas por aplicações comerciais que precisam exibir telas reais de negócio sem expor nomes, documentos, contatos, endereços, observações livres, históricos, anexos ou dados comerciais identificáveis do cliente original.

## 2. Aviso sobre LGPD

A ferramenta aplica medidas técnicas compatíveis com boas práticas de anonimização/pseudonimização: substitui campos configurados por dados sintéticos irreversíveis, recalcula `rowkey`, limpa tabelas configuradas e, somente quando solicitado, valida relacionamentos lógicos declarados.

A validação jurídica final, porém, depende do **controlador**, **DPO** ou **jurídico** da empresa. Dados anonimizados não devem permitir identificação direta ou indireta do titular. Se códigos técnicos preservados ainda permitirem associação interna com a base original ou com tabelas de correspondência, a base pode ser considerada **pseudonimizada** no ambiente interno, e não necessariamente anonimizada de forma absoluta.

Prioridade prática da ferramenta:

1. preservar o funcionamento técnico do aplicativo;
2. remover dados reais visíveis;
3. substituir dados pessoais e identificáveis por dados sintéticos coerentes;
4. limpar logs, históricos, anexos e observações sensíveis;
5. validar `rowkey` e, quando solicitado, relacionamentos lógicos;
6. documentar claramente limites técnicos e LGPD.

## 3. Cenário de uso

Fluxo esperado:

```text
Cliente real -> base SQLite copiada -> base sanitizada -> demonstração em tela para prospect
```

O prospect não deve receber banco, aplicação instalada, dumps, exportações, relatórios reais, logs ou acesso direto/indireto à base. Mesmo assim, qualquer campo visível em tela deve ser tratado como potencialmente sensível.

## 4. Estrutura de pastas

```text
LGPD-Formalizer/
├── main.py
├── README.md
├── db/
│   └── .gitkeep
├── arquivos_sensiveis/
│   ├── cliente.txt
│   ├── empresa.txt
│   ├── representante.txt
│   ├── produto.txt
│   ├── tabelapreco.txt
│   ├── condicaopagamento
│   └── tipopagamento
├── clearTables.txt
├── output/
│   └── .gitkeep
├── logs/
│   └── .gitkeep
└── examples/
    ├── cliente.txt
    ├── empresa.txt
    ├── representante.txt
    ├── produto.txt
    ├── tabelapreco.txt
    └── clearTables.txt
```

## 5. Como preparar a base

Coloque a base original em `db/`. Extensões aceitas:

```text
.sqlite
.sqlite3
.sqllite3
.db
```

Mesmo com extensão incorreta, como `.sqllite3`, o script valida o cabeçalho SQLite. Se houver mais de uma base válida em `db/`, informe o caminho explicitamente com `--db`.

A base original nunca é aberta para escrita. O script cria uma cópia em `output/` e modifica somente a cópia.

## 6. Como configurar uma entidade

Cada arquivo regular dentro de `arquivos_sensiveis/` representa uma entidade. A extensão `.txt` é opcional; arquivos sem extensão, como `condicaopagamento` e `tipopagamento`, também são lidos. Por padrão, o nome do arquivo vira nome da tabela com prefixo `tblvp`:

```text
cliente.txt          -> tblvpcliente
empresa.txt          -> tblvpempresa
representante.txt    -> tblvprepresentante
condicaopagamento    -> tblvpcondicaopagamento
tipopagamento        -> tblvptipopagamento
```

Se necessário, informe explicitamente o nome da tabela na seção `[table]`.

## 7. Formato do arquivo de configuração

Formato estruturado recomendado:

```ini
[table]
name=tblvpcliente

[primary_key]
fields=cdempresa,cdcliente

[key_policy]
mode=preserve

[rowkey]
target=rowkey
fields=cdempresa,cdcliente
separator=;
trailing_separator=true

[logical_relations]
cdempresa=tblvpempresa.cdempresa
cdempresa,cdrepresentante=tblvprepresentante.cdempresa,cdrepresentante

[sensitive_fields]
nmrazaosocial
nmfantasia
dsemail
nufone
nucnpj
dsobservacao
```

Formato antigo simples também é aceito: uma coluna sensível por linha, sem seções. Nesse caso, a tabela é inferida pelo nome do arquivo.

## 8. Chaves primárias

Chave simples:

```ini
[primary_key]
fields=cdcliente
```

Chave composta:

```ini
[primary_key]
fields=cdempresa,cdcliente
```

A chave primária é usada para identificar registros, gerar dados determinísticos, recalcular `rowkey` e montar mapas de chave quando houver regeneração. Se a seção não for informada, o script tenta detectar a PK com `PRAGMA table_info`; se não houver PK, usa `rowid`.

## 9. Relacionamentos lógicos

O sistema não depende de constraints de FK no SQLite. Se existirem ligações entre tabelas, declare-as como relacionamentos lógicos na seção `[logical_relations]`:

```ini
[logical_relations]
cdempresa=tblvpempresa.cdempresa
cdempresa,cdrepresentante=tblvprepresentante.cdempresa,cdrepresentante
```

A tabela do arquivo é a origem da ligação lógica; a tabela à direita é o destino. O script valida se os campos configurados existem nos dois lados e usa as dependências apenas para ordenar o processamento. A validação de registros sem destino não roda por padrão; use `--validate-relations` quando quiser conferir órfãos.

A seção antiga `[foreign_keys]` continua aceita por compatibilidade, mas a nomenclatura recomendada é `[logical_relations]`, porque não é necessário existir FK real no banco. Se uma tabela de destino tiver chaves regeneradas, o mapa de chave antiga -> nova é usado para atualizar as ligações configuradas. Alterar chaves é uma operação crítica; por padrão, use `mode=preserve`.

## 10. Rowkey

A `rowkey` deve sempre ser reconstruída a partir dos campos atuais da chave, nunca reaproveitada a partir do valor antigo.

Rowkey como coluna da própria tabela:

```ini
[rowkey]
target=rowkey
fields=cdempresa,cdcliente
separator=;
trailing_separator=true
```

Exemplo de resultado: `1;1234;`.

Sem separador final:

```ini
trailing_separator=false
```

Rowkey em tabela externa:

```ini
[rowkey]
mode=external_table
table=rowkey
entity_field=nmtabela
entity_value=tblvpcliente
key_field=dsrowkey
fields=cdempresa,cdcliente
separator=;
trailing_separator=true
```

A ferramenta não apaga nem recria a tabela externa de `rowkey`; apenas tenta atualizar conforme configuração explícita.

## 11. Tabelas limpas

Use `clearTables.txt` para tabelas que devem ser esvaziadas:

```text
# Tabelas de logs e dados que não devem ir para demonstração
tblvplogintegracao
tblvphistoricoalteracao
tblvpanexo
tblvpobservacao
tblvplogerro
```

Linhas vazias e comentários com `#` são ignorados. A limpeza ocorre dentro da transação da cópia de saída.

## 12. Como executar

```bash
python main.py
python main.py --db db/base.sqlite3 --out output/base_demo.sqlite3
python main.py --dry-run
python main.py --seed 123
python main.py --strict
python main.py --verbose
```

Argumentos úteis:

| Argumento | Descrição |
|---|---|
| `--db` | Caminho da base original. |
| `--out` | Caminho da base final sanitizada. |
| `--sensitive-dir` | Pasta com arquivos por entidade. Padrão: `arquivos_sensiveis`. |
| `--clear-file` | Arquivo com tabelas a limpar. Padrão: `clearTables.txt`. |
| `--dry-run` | Simula o processo e descarta alterações. |
| `--seed` | Garante geração determinística para mesma base/configuração. |
| `--strict` | Interrompe em tabela/campo inválido, rowkey divergente e, junto com `--validate-relations`, relacionamento sem destino. |
| `--validate-relations` | Valida órfãos nos relacionamentos lógicos configurados. Não é habilitado por padrão. |
| `--key-mode` | Sobrescreve a política de chave; use com extrema cautela. |

## 13. Logs

Os logs ficam em:

```text
logs/anonimizacao.log
```

O log registra início, base usada, cópia criada, configurações lidas, entidades processadas, campos anonimizados, tabelas limpas, chaves preservadas/regeneradas, relacionamentos lógicos atualizados, rowkeys recalculadas, avisos e erros.

O log não registra valores reais de campos sensíveis.

## 14. Como adicionar nova tabela

1. Crie `arquivos_sensiveis/novaentidade` ou `arquivos_sensiveis/novaentidade.txt`.
2. Configure `[table]` ou use o padrão `tblvpnovaentidade`.
3. Configure `[primary_key]`.
4. Configure `[rowkey]`, se existir.
5. Configure `[logical_relations]`, se houver ligações lógicas entre tabelas.
6. Liste todos os campos sensíveis/visíveis em `[sensitive_fields]`.
7. Rode `python main.py --dry-run --strict`.
8. Valide `logs/anonimizacao.log`.
9. Gere a base final sem `--dry-run`.
10. Abra o aplicativo com a base demo e revise telas, relatórios e exportações.

## 15. Como validar a base final

Contar registros:

```sql
SELECT COUNT(*) FROM tblvpcliente;
SELECT COUNT(*) FROM tblvpempresa;
```

Validar campos sensíveis substituídos:

```sql
SELECT nmrazaosocial, dsemail, nucnpj FROM tblvpcliente LIMIT 20;
```

Validar `rowkey` com separador final:

```sql
SELECT COUNT(*) AS divergentes
FROM tblvpcliente
WHERE rowkey <> CAST(cdempresa AS TEXT) || ';' || CAST(cdcliente AS TEXT) || ';';
```

Validar relacionamento lógico configurado:

```sql
SELECT COUNT(*) AS orfaos
FROM tblvpcliente c
WHERE c.cdrepresentante IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM tblvprepresentante r
    WHERE r.cdempresa = c.cdempresa
      AND r.cdrepresentante = c.cdrepresentante
  );
```

Confirmar tabelas limpas:

```sql
SELECT COUNT(*) FROM tblvplogintegracao;
SELECT COUNT(*) FROM tblvpanexo;
```

## 16. Checklist antes da demonstração

- A base original foi preservada?
- A base demo foi criada em `output/`?
- Os nomes reais foram removidos?
- CNPJs reais foram removidos?
- CPFs reais foram removidos?
- E-mails reais foram removidos?
- Telefones reais foram removidos?
- Endereços reais foram removidos?
- Observações livres foram sanitizadas?
- Tabelas de log foram limpas?
- Tabelas de anexo foram limpas, se aplicável?
- `rowkey` foi recalculada?
- Relacionamentos lógicos foram validados, se você executou com `--validate-relations`?
- O aplicativo abriu corretamente com a base demo?
- Nenhum relatório/exportação mostra dados reais?
- O prospect não terá acesso ao banco, aplicação instalada, logs, relatórios, dumps ou exportações?

## Principais funções do código

- `locate_database`: localiza e valida a base SQLite original.
- `prepare_working_copy`: copia a base para `output/` antes de qualquer alteração.
- `load_entity_configs`: lê dinamicamente todos os arquivos regulares em `arquivos_sensiveis/`, inclusive sem extensão.
- `validate_configs`: valida tabelas, colunas, PKs, relacionamentos lógicos e rowkeys configuradas.
- `topo_sort`: ordena entidades por dependência declarada.
- `synthetic_value`: gera valores coerentes por tipo de campo e entidade.
- `regenerate_keys` e `propagate_keys`: tratam regeneração excepcional de chaves e atualização das ligações lógicas configuradas.
- `anonymize_table`: anonimiza somente campos configurados.
- `recalc_rowkeys`: recalcula rowkeys com os valores atuais.
- `validate_logical_relations` e `validate_rowkeys`: validam a base final quando essas conferências são solicitadas/aplicáveis.

## Dados sintéticos gerados

A ferramenta gera, sem dependências externas:

- CPF válido com dígitos verificadores;
- CNPJ válido com dígitos verificadores;
- e-mails `@demo.local`;
- telefones/celulares brasileiros fictícios;
- CEPs fictícios válidos;
- textos sintéticos derivados dinamicamente do nome da entidade configurada, sem depender de uma lista fixa de entidades;
- endereços e observações fictícias.

A geração é determinística quando `--seed` é informado.

## Limitações técnicas

- A ferramenta anonimiza apenas campos configurados; campos visíveis esquecidos na configuração podem permanecer reais.
- A validação LGPD final não é automática e deve envolver controlador, DPO ou jurídico.
- Regenerar chaves pode conflitar com constraints, triggers e dependências não declaradas; prefira `mode=preserve`.
- Relacionamentos lógicos só são propagados quando declarados nos arquivos de configuração e só são validados contra órfãos com `--validate-relations`.
- Rowkey externa depende de configuração compatível com a estrutura real.
- A ferramenta não altera schema, índices, triggers ou tipos de coluna.

## Arquivos de exemplo

A pasta `examples/` contém modelos para `cliente`, `empresa`, `representante`, `produto`, `tabelapreco` e `clearTables.txt`. Copie e adapte esses arquivos para `arquivos_sensiveis/` conforme a base real.
