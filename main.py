from __future__ import annotations

import argparse
import configparser
import hashlib
import logging
import re
import shutil
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

SQLITE_EXTENSIONS = {".sqlite", ".sqlite3", ".sqllite3", ".db"}

@dataclass
class ColumnInfo:
    name: str
    type: str
    notnull: bool
    pk_index: int


@dataclass
class ForeignKeyConfig:
    child_fields: tuple[str, ...]
    parent_table: str
    parent_fields: tuple[str, ...]


@dataclass
class RowKeyConfig:
    mode: str = "column"
    target: str = "rowkey"
    fields: tuple[str, ...] = field(default_factory=tuple)
    separator: str = ";"
    trailing_separator: bool = True
    table: str | None = None
    entity_field: str | None = None
    entity_value: str | None = None
    key_field: str | None = None


@dataclass
class KeyPolicy:
    mode: str = "preserve"
    regenerate_fields: tuple[str, ...] = field(default_factory=tuple)
    start_at: int = 1


@dataclass
class EntityConfig:
    source_file: Path
    entity_name: str
    table: str
    primary_key: tuple[str, ...] = field(default_factory=tuple)
    key_policy: KeyPolicy = field(default_factory=KeyPolicy)
    rowkey: RowKeyConfig | None = None
    foreign_keys: list[ForeignKeyConfig] = field(default_factory=list)
    sensitive_fields: list[str] = field(default_factory=list)


class CriticalError(RuntimeError):
    """Erro que deve impedir o uso da base sanitizada."""


class SafeLogger:
    """Wrapper para logs técnicos sem registrar valores sensíveis originais."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def info(self, msg: str, *args: Any) -> None:
        self.logger.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        rendered = msg % args if args else msg
        self.warnings.append(rendered)
        self.logger.warning(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        rendered = msg % args if args else msg
        self.errors.append(rendered)
        self.logger.error(msg, *args)


def setup_logging(verbose: bool) -> SafeLogger:
    Path("logs").mkdir(exist_ok=True)
    logger = logging.getLogger("anonimizacao")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler("logs/anonimizacao.log", encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return SafeLogger(logger)


def quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise CriticalError(f"Identificador SQL inválido: {identifier!r}")
    return f'"{identifier}"'


def split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "s"}


def is_valid_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
        if header != b"SQLite format 3\x00":
            return False
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA schema_version").fetchone()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def locate_database(db_arg: str | None) -> Path:
    if db_arg:
        path = Path(db_arg)
        if not is_valid_sqlite(path):
            raise CriticalError(f"Arquivo informado não é um SQLite válido: {path}")
        return path
    db_dir = Path("db")
    candidates = [p for p in db_dir.iterdir() if p.suffix.lower() in SQLITE_EXTENSIONS and is_valid_sqlite(p)] if db_dir.exists() else []
    if not candidates:
        raise CriticalError("Nenhuma base SQLite válida encontrada em db/. Use --db.")
    if len(candidates) > 1:
        raise CriticalError("Mais de uma base encontrada em db/. Informe o caminho com --db.")
    return candidates[0]


def default_output_path(input_db: Path) -> Path:
    return Path("output") / f"{input_db.stem}_demo{input_db.suffix or '.sqlite3'}"


def prepare_working_copy(input_db: Path, out_arg: str | None, dry_run: bool, log: SafeLogger) -> Path:
    out_path = Path(out_arg) if out_arg else default_output_path(input_db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if input_db.resolve() == out_path.resolve():
        raise CriticalError("A saída não pode ser o mesmo arquivo da base original.")
    # Cópia segura da base original: a ferramenta nunca abre a base real em modo escrita.
    shutil.copy2(input_db, out_path)
    log.info("Base original copiada para %s%s", out_path, " (dry-run)" if dry_run else "")
    return out_path


def read_config_file(path: Path) -> EntityConfig:
    entity = path.stem.strip().lower()
    text = path.read_text(encoding="utf-8")
    parser = configparser.ConfigParser(allow_no_value=True, delimiters=("="))
    parser.optionxform = str.lower
    has_sections = bool(re.search(r"^\s*\[[^\]]+\]", text, re.M))

    # Leitura dos arquivos de configuração: aceita formato estruturado e formato antigo simples.
    if has_sections:
        parser.read_string(text)
        table = parser.get("table", "name", fallback=f"tblvp{entity}").strip()
        pk = split_csv(parser.get("primary_key", "fields", fallback=""))
        mode = parser.get("key_policy", "mode", fallback="preserve").strip().lower()
        regen = split_csv(parser.get("key_policy", "regenerate_fields", fallback=""))
        start_at = parser.getint("key_policy", "start_at", fallback=1)
        key_policy = KeyPolicy(mode=mode, regenerate_fields=regen, start_at=start_at)
        rowkey = None
        if parser.has_section("rowkey"):
            rowkey = RowKeyConfig(
                mode=parser.get("rowkey", "mode", fallback="column").strip().lower(),
                target=parser.get("rowkey", "target", fallback="rowkey").strip(),
                fields=split_csv(parser.get("rowkey", "fields", fallback="")),
                separator=parser.get("rowkey", "separator", fallback=";"),
                trailing_separator=parse_bool(parser.get("rowkey", "trailing_separator", fallback="true"), True),
                table=parser.get("rowkey", "table", fallback=None),
                entity_field=parser.get("rowkey", "entity_field", fallback=None),
                entity_value=parser.get("rowkey", "entity_value", fallback=table),
                key_field=parser.get("rowkey", "key_field", fallback=None),
            )
        fks = []
        if parser.has_section("foreign_keys"):
            # Interpretação de FKs: campos locais à esquerda e tabela.campos do pai à direita.
            for left, right in parser.items("foreign_keys"):
                child_fields = split_csv(left)
                parent_table, parent_fields_raw = right.split(".", 1)
                fks.append(ForeignKeyConfig(child_fields, parent_table.strip(), split_csv(parent_fields_raw)))
        sensitive = []
        if parser.has_section("sensitive_fields"):
            sensitive = [k.strip() for k, _ in parser.items("sensitive_fields") if k.strip()]
        return EntityConfig(path, entity, table, pk, key_policy, rowkey, fks, sensitive)

    fields = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return EntityConfig(path, entity, f"tblvp{entity}", sensitive_fields=fields)


def load_entity_configs(sensitive_dir: Path, log: SafeLogger) -> list[EntityConfig]:
    if not sensitive_dir.exists():
        log.warning("Pasta de configurações não encontrada: %s", sensitive_dir)
        return []
    configs = [read_config_file(p) for p in sorted(sensitive_dir.glob("*.txt")) if p.stem.lower() != "cleartables"]
    log.info("Arquivos de configuração lidos: %d", len(configs))
    return configs


def load_clear_tables(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def get_columns(conn: sqlite3.Connection, table: str) -> dict[str, ColumnInfo]:
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return {r[1].lower(): ColumnInfo(r[1], r[2] or "", bool(r[3]), int(r[5])) for r in rows}


def detect_primary_key(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    # Interpretação de chaves primárias: usa a configuração; se ausente, detecta via PRAGMA.
    cols = get_columns(conn, table)
    pk = sorted((c for c in cols.values() if c.pk_index), key=lambda c: c.pk_index)
    return tuple(c.name for c in pk) or ("rowid",)


def validate_configs(conn: sqlite3.Connection, configs: list[EntityConfig], strict: bool, log: SafeLogger) -> None:
    for cfg in configs:
        if not table_exists(conn, cfg.table):
            msg = f"Tabela configurada não encontrada: {cfg.table}"
            log.error(msg) if strict else log.warning(msg)
            if strict:
                raise CriticalError(msg)
            continue
        cols = get_columns(conn, cfg.table)
        if not cfg.primary_key:
            cfg.primary_key = detect_primary_key(conn, cfg.table)
        for field_name in set(cfg.primary_key) | set(cfg.sensitive_fields):
            if field_name != "rowid" and field_name.lower() not in cols:
                msg = f"Coluna não encontrada: {cfg.table}.{field_name}"
                log.error(msg) if strict else log.warning(msg)
                if strict:
                    raise CriticalError(msg)
        if cfg.rowkey:
            for field_name in cfg.rowkey.fields:
                if field_name.lower() not in cols:
                    msg = f"Campo de rowkey inexistente: {cfg.table}.{field_name}"
                    log.error(msg) if strict else log.warning(msg)
                    if strict:
                        raise CriticalError(msg)
            if cfg.rowkey.mode != "external_table" and cfg.rowkey.target.lower() not in cols:
                msg = f"Coluna rowkey inexistente: {cfg.table}.{cfg.rowkey.target}"
                log.error(msg) if strict else log.warning(msg)
                if strict:
                    raise CriticalError(msg)
        for fk in cfg.foreign_keys:
            for child in fk.child_fields:
                if child.lower() not in cols:
                    msg = f"Campo de FK inexistente: {cfg.table}.{child}"
                    log.error(msg) if strict else log.warning(msg)
                    if strict:
                        raise CriticalError(msg)
            if not table_exists(conn, fk.parent_table):
                msg = f"Tabela pai de FK inexistente: {fk.parent_table}"
                log.error(msg) if strict else log.warning(msg)
                if strict:
                    raise CriticalError(msg)
                continue
            parent_cols = get_columns(conn, fk.parent_table)
            for parent in fk.parent_fields:
                if parent.lower() not in parent_cols:
                    msg = f"Campo pai de FK inexistente: {fk.parent_table}.{parent}"
                    log.error(msg) if strict else log.warning(msg)
                    if strict:
                        raise CriticalError(msg)


def topo_sort(configs: list[EntityConfig], log: SafeLogger) -> list[EntityConfig]:
    by_table = {c.table: c for c in configs}
    deps = {c.table: {fk.parent_table for fk in c.foreign_keys if fk.parent_table in by_table} for c in configs}
    reverse: dict[str, set[str]] = defaultdict(set)
    for table, parents in deps.items():
        for parent in parents:
            reverse[parent].add(table)
    queue = deque([t for t, parents in deps.items() if not parents])
    ordered: list[str] = []
    while queue:
        table = queue.popleft()
        ordered.append(table)
        for child in reverse[table]:
            deps[child].discard(table)
            if not deps[child]:
                queue.append(child)
    if len(ordered) != len(configs):
        cycle_tables = sorted(set(by_table) - set(ordered))
        log.warning("Dependência circular ou incompleta detectada em: %s", ", ".join(cycle_tables))
        ordered.extend(cycle_tables)
    return [by_table[t] for t in ordered]


def stable_int(seed: str, parts: Iterable[Any], minimum: int, maximum: int) -> int:
    payload = "|".join([seed, *map(str, parts)]).encode("utf-8")
    value = int(hashlib.sha256(payload).hexdigest()[:16], 16)
    return minimum + (value % (maximum - minimum + 1))


def only_digits(value: Any) -> str:
    return re.sub(r"\D", "", "" if value is None else str(value))


def format_like(original: Any, digits: str, kind: str) -> str:
    original_s = "" if original is None else str(original)
    if kind == "cpf" and re.search(r"\D", original_s):
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if kind == "cnpj" and re.search(r"\D", original_s):
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if kind == "cep" and re.search(r"\D", original_s):
        return f"{digits[:5]}-{digits[5:]}"
    return digits


def cpf_digits(base_num: int) -> str:
    nums = [int(d) for d in f"{base_num % 10**9:09d}"]
    for weights in (range(10, 1, -1), range(11, 1, -1)):
        s = sum(n * w for n, w in zip(nums, weights))
        d = 0 if s % 11 < 2 else 11 - (s % 11)
        nums.append(d)
    return "".join(map(str, nums))


def cnpj_digits(base_num: int) -> str:
    nums = [int(d) for d in f"{base_num % 10**8:08d}0001"]
    for weights in ([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]):
        s = sum(n * w for n, w in zip(nums, weights))
        d = 0 if s % 11 < 2 else 11 - (s % 11)
        nums.append(d)
    return "".join(map(str, nums))


def friendly_entity(entity: str) -> str:
    known = {
        "cliente": "Cliente", "empresa": "Empresa", "representante": "Representante", "produto": "Produto",
        "tabelapreco": "Tabela de Preço", "pedido": "Pedido", "notafiscal": "Nota Fiscal",
        "condicaopagamento": "Condição de Pagamento", "fornecedor": "Fornecedor",
    }
    if entity in known:
        return known[entity]
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", entity).replace("_", " ")
    return words.title() if words else "Entidade"


def infer_kind(field: str, entity: str, original: Any) -> str:
    f = field.lower()
    digits = only_digits(original)
    if "email" in f or "mail" in f:
        return "email"
    if "cpfcnpj" in f or f in {"documento", "nudocumento"}:
        if len(digits) == 11:
            return "cpf"
        if len(digits) == 14 or entity in {"cliente", "empresa", "fornecedor"}:
            return "cnpj"
        return "cpf"
    if "cnpj" in f:
        return "cnpj"
    if "cpf" in f:
        return "cpf"
    if "cep" in f:
        return "cep"
    if any(x in f for x in ("celular", "whatsapp")):
        return "celular"
    if any(x in f for x in ("fone", "telefone")):
        return "telefone"
    if any(x in f for x in ("logradouro", "endereco", "endereço")) and not f.startswith("nu"):
        return "logradouro"
    if any(x in f for x in ("bairro",)):
        return "bairro"
    if any(x in f for x in ("cidade",)):
        return "cidade"
    if f in {"uf", "cdestado", "cdestadocomercial"}:
        return "uf"
    if "estado" in f:
        return "estado"
    if "complemento" in f:
        return "complemento"
    if any(x in f for x in ("numero", "nulogradouro", "nuendereco")):
        return "numero"
    if "inscricaoestadual" in f or f == "ie" or "insc" in f:
        return "ie"
    if any(x in f for x in ("observacao", "observação", "obs", "comentario", "historico")):
        return "observacao"
    if any(x in f for x in ("razaosocial", "fantasia", "nome", "nm")):
        return "nome"
    if any(x in f for x in ("descricao", "descrição", "dsdescricao", "titulo", "produto", "marca")):
        return "descricao"
    return "generico"


def truncate_for_column(value: Any, col_type: str) -> Any:
    if value is None:
        return value
    m = re.search(r"\((\d+)\)", col_type or "")
    if m and isinstance(value, str):
        return value[: int(m.group(1))]
    return value


def synthetic_value(seed: str, cfg: EntityConfig, field: str, key: tuple[Any, ...], original: Any, col: ColumnInfo) -> Any:
    # Geração de dados sintéticos: determinística por seed, tabela, campo e chave do registro.
    col_type = (col.type or "").upper()
    if any(t in col_type for t in ("INT", "REAL", "NUM", "DEC", "FLOAT", "DOUBLE")) and not any(x in field.lower() for x in ("cpf", "cnpj", "cep", "fone", "telefone", "celular", "documento")):
        return stable_int(seed, (cfg.table, field, *key), 1, 999999)
    seq = stable_int(seed, (cfg.table, *key), 1, 999999)
    kind = infer_kind(field, cfg.entity_name, original)
    base = friendly_entity(cfg.entity_name)
    slug = re.sub(r"[^a-z0-9]", "", cfg.entity_name.lower()) or "demo"
    if kind == "email":
        value = f"{slug}{seq:06d}@demo.local"
    elif kind == "cpf":
        value = format_like(original, cpf_digits(stable_int(seed, (cfg.table, field, *key), 1, 999999999)), "cpf")
    elif kind == "cnpj":
        value = format_like(original, cnpj_digits(stable_int(seed, (cfg.table, field, *key), 1, 99999999)), "cnpj")
    elif kind == "cep":
        value = format_like(original, f"88000{seq % 1000:03d}", "cep")
    elif kind == "celular":
        value = f"(48) 9{9000 + seq % 1000:04d}-{seq % 10000:04d}" if re.search(r"\D", str(original or "")) else f"489{9000 + seq % 1000:04d}{seq % 10000:04d}"
    elif kind == "telefone":
        value = f"(48) {3300 + seq % 100:04d}-{seq % 10000:04d}" if re.search(r"\D", str(original or "")) else f"48{3300 + seq % 100:04d}{seq % 10000:04d}"
    elif kind == "logradouro":
        value = f"Rua Demonstração {seq:06d}"
    elif kind == "bairro":
        value = "Bairro Demo"
    elif kind == "cidade":
        value = "Cidade Demo"
    elif kind == "uf":
        value = "SC"
    elif kind == "estado":
        value = "Santa Catarina"
    elif kind == "complemento":
        value = f"Sala {seq:06d}"
    elif kind == "numero":
        value = str(100 + seq % 900)
    elif kind == "ie":
        value = "ISENTO"
    elif kind == "observacao":
        value = f"Observação fictícia para demonstração {seq:06d}."
    elif kind in {"nome", "descricao"}:
        value = f"{base} Demo {seq:06d}"
    else:
        value = f"Dado Demo {seq:06d}"
    return truncate_for_column(value, col.type)


def where_clause(fields: tuple[str, ...]) -> str:
    return " AND ".join([f"rowid = ?" if f == "rowid" else f"{quote_ident(f)} IS ?" for f in fields])


def select_key_expr(fields: tuple[str, ...]) -> str:
    return ", ".join(["rowid" if f == "rowid" else quote_ident(f) for f in fields])


def build_rowkey(values: Iterable[Any], sep: str, trailing: bool) -> str:
    rendered = sep.join("" if v is None else str(v) for v in values)
    return rendered + sep if trailing else rendered


def regenerate_keys(conn: sqlite3.Connection, cfg: EntityConfig, seed: str, dry_run: bool, log: SafeLogger) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    # Política de preservação/regeneração de chaves: preservar é o padrão seguro; regenerar é exceção explícita.
    if cfg.key_policy.mode != "regenerate":
        log.info("Chaves preservadas para %s", cfg.table)
        return {}
    if not cfg.key_policy.regenerate_fields:
        raise CriticalError(f"Regeneração de chaves sem regenerate_fields em {cfg.table}")
    key_map: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    rows = conn.execute(f"SELECT {select_key_expr(cfg.primary_key)} FROM {quote_ident(cfg.table)} ORDER BY {select_key_expr(cfg.primary_key)}").fetchall()
    for idx, old_key in enumerate(rows, start=cfg.key_policy.start_at):
        new_key = list(old_key)
        for field_name in cfg.key_policy.regenerate_fields:
            if field_name not in cfg.primary_key:
                raise CriticalError(f"Campo regenerado fora da PK em {cfg.table}: {field_name}")
            new_key[cfg.primary_key.index(field_name)] = idx
        new_tuple = tuple(new_key)
        key_map[tuple(old_key)] = new_tuple
        if not dry_run:
            sets = ", ".join(f"{quote_ident(f)} = ?" for f in cfg.key_policy.regenerate_fields)
            params = [new_tuple[cfg.primary_key.index(f)] for f in cfg.key_policy.regenerate_fields] + list(old_key)
            conn.execute(f"UPDATE {quote_ident(cfg.table)} SET {sets} WHERE {where_clause(cfg.primary_key)}", params)
    log.info("Chaves regeneradas em %s: %d registros", cfg.table, len(key_map))
    return key_map


def propagate_keys(conn: sqlite3.Connection, configs: list[EntityConfig], key_maps: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]], dry_run: bool, log: SafeLogger) -> None:
    for cfg in configs:
        for fk in cfg.foreign_keys:
            mapping = key_maps.get(fk.parent_table)
            if not mapping:
                continue
            parent_cfg_pk = next((c.primary_key for c in configs if c.table == fk.parent_table), fk.parent_fields)
            for old_parent_key, new_parent_key in mapping.items():
                old_parent_lookup = dict(zip(parent_cfg_pk, old_parent_key))
                new_parent_lookup = dict(zip(parent_cfg_pk, new_parent_key))
                old_child_vals = [old_parent_lookup[p] for p in fk.parent_fields]
                new_child_vals = [new_parent_lookup[p] for p in fk.parent_fields]
                sets = ", ".join(f"{quote_ident(f)} = ?" for f in fk.child_fields)
                wh = " AND ".join(f"{quote_ident(f)} IS ?" for f in fk.child_fields)
                if not dry_run:
                    conn.execute(f"UPDATE {quote_ident(cfg.table)} SET {sets} WHERE {wh}", [*new_child_vals, *old_child_vals])
            log.info("FK atualizada: %s -> %s (%d mapas)", cfg.table, fk.parent_table, len(mapping))


def anonymize_table(conn: sqlite3.Connection, cfg: EntityConfig, seed: str, dry_run: bool, log: SafeLogger) -> None:
    if not cfg.sensitive_fields or not table_exists(conn, cfg.table):
        return
    cols = get_columns(conn, cfg.table)
    valid_fields = [f for f in cfg.sensitive_fields if f.lower() in cols]
    select_fields = tuple(dict.fromkeys([*cfg.primary_key, *valid_fields]))
    rows = conn.execute(f"SELECT {select_key_expr(select_fields)} FROM {quote_ident(cfg.table)}").fetchall()
    for row in rows:
        data = dict(zip(select_fields, row))
        key = tuple(data[f] for f in cfg.primary_key)
        updates = []
        params = []
        for field_name in valid_fields:
            col = cols[field_name.lower()]
            new_value = synthetic_value(seed, cfg, field_name, key, data[field_name], col)
            updates.append(f"{quote_ident(field_name)} = ?")
            params.append(new_value)
        if updates and not dry_run:
            conn.execute(f"UPDATE {quote_ident(cfg.table)} SET {', '.join(updates)} WHERE {where_clause(cfg.primary_key)}", [*params, *key])
    log.info("Tabela %s anonimizada: %d registros, %d campos", cfg.table, len(rows), len(valid_fields))


def recalc_rowkeys(conn: sqlite3.Connection, cfg: EntityConfig, dry_run: bool, log: SafeLogger) -> int:
    # Recálculo da rowkey: sempre usa os campos atuais, nunca o valor antigo da rowkey.
    if not cfg.rowkey or not table_exists(conn, cfg.table):
        return 0
    rk = cfg.rowkey
    count = 0
    if rk.mode == "external_table":
        if not (rk.table and rk.entity_field and rk.key_field and table_exists(conn, rk.table)):
            log.warning("Rowkey externa sem configuração/tabela válida para %s", cfg.table)
            return 0
        rows = conn.execute(f"SELECT {select_key_expr(cfg.primary_key)}, {select_key_expr(rk.fields)} FROM {quote_ident(cfg.table)}").fetchall()
        for row in rows:
            key = tuple(row[: len(cfg.primary_key)])
            values = row[len(cfg.primary_key):]
            new_rk = build_rowkey(values, rk.separator, rk.trailing_separator)
            if not dry_run:
                conn.execute(
                    f"UPDATE {quote_ident(rk.table)} SET {quote_ident(rk.key_field)} = ? WHERE {quote_ident(rk.entity_field)} IS ? AND {quote_ident(rk.key_field)} IS ?",
                    (new_rk, rk.entity_value, build_rowkey(key, rk.separator, rk.trailing_separator)),
                )
            count += 1
    else:
        rows = conn.execute(f"SELECT {select_key_expr(cfg.primary_key)}, {select_key_expr(rk.fields)} FROM {quote_ident(cfg.table)}").fetchall()
        for row in rows:
            key = tuple(row[: len(cfg.primary_key)])
            values = row[len(cfg.primary_key):]
            new_rk = build_rowkey(values, rk.separator, rk.trailing_separator)
            if not dry_run:
                conn.execute(f"UPDATE {quote_ident(cfg.table)} SET {quote_ident(rk.target)} = ? WHERE {where_clause(cfg.primary_key)}", [new_rk, *key])
            count += 1
    log.info("Rowkeys recalculadas em %s: %d", cfg.table, count)
    return count


def clear_tables(conn: sqlite3.Connection, tables: list[str], dry_run: bool, strict: bool, log: SafeLogger) -> None:
    for table in tables:
        if not table_exists(conn, table):
            msg = f"Tabela para limpeza não encontrada: {table}"
            log.error(msg) if strict else log.warning(msg)
            if strict:
                raise CriticalError(msg)
            continue
        total = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
        if not dry_run:
            conn.execute(f"DELETE FROM {quote_ident(table)}")
        log.info("Tabela limpa: %s (%d registros)", table, total)


def validate_foreign_keys(conn: sqlite3.Connection, configs: list[EntityConfig], strict: bool, log: SafeLogger) -> int:
    # Validação de integridade: confere as FKs configuradas após propagação de chaves.
    issues = 0
    for cfg in configs:
        if not table_exists(conn, cfg.table):
            continue
        for fk in cfg.foreign_keys:
            if not table_exists(conn, fk.parent_table):
                continue
            child_cols = " AND ".join([f"c.{quote_ident(c)} IS p.{quote_ident(p)}" for c, p in zip(fk.child_fields, fk.parent_fields)])
            non_null = " AND ".join([f"c.{quote_ident(c)} IS NOT NULL" for c in fk.child_fields])
            sql = f"SELECT COUNT(*) FROM {quote_ident(cfg.table)} c WHERE {non_null} AND NOT EXISTS (SELECT 1 FROM {quote_ident(fk.parent_table)} p WHERE {child_cols})"
            count = conn.execute(sql).fetchone()[0]
            if count:
                issues += count
                msg = f"FK inconsistente: {cfg.table} -> {fk.parent_table}: {count} registros órfãos"
                log.error(msg) if strict else log.warning(msg)
    if strict and issues:
        raise CriticalError(f"Integridade referencial inválida: {issues} inconsistências")
    return issues


def validate_rowkeys(conn: sqlite3.Connection, configs: list[EntityConfig], strict: bool, log: SafeLogger) -> int:
    issues = 0
    for cfg in configs:
        if not cfg.rowkey or cfg.rowkey.mode == "external_table" or not table_exists(conn, cfg.table):
            continue
        rk = cfg.rowkey
        rows = conn.execute(f"SELECT {quote_ident(rk.target)}, {select_key_expr(rk.fields)} FROM {quote_ident(cfg.table)}").fetchall()
        bad = sum(1 for row in rows if row[0] != build_rowkey(row[1:], rk.separator, rk.trailing_separator))
        if bad:
            issues += bad
            msg = f"Rowkey divergente em {cfg.table}: {bad} registros"
            log.error(msg) if strict else log.warning(msg)
    if strict and issues:
        raise CriticalError(f"Rowkey inválida: {issues} divergências")
    return issues


def process_database(args: argparse.Namespace, log: SafeLogger) -> int:
    input_db = locate_database(args.db)
    out_db = prepare_working_copy(input_db, args.out, args.dry_run, log)
    configs = load_entity_configs(Path(args.sensitive_dir), log)
    if args.key_mode:
        for cfg in configs:
            cfg.key_policy.mode = args.key_mode
    clear_list = load_clear_tables(Path(args.clear_file))
    seed = str(args.seed if args.seed is not None else "lgpd-formalizer")
    conn = sqlite3.connect(out_db)
    conn.row_factory = sqlite3.Row
    key_maps: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]] = {}
    try:
        validate_configs(conn, configs, args.strict, log)
        ordered = topo_sort([c for c in configs if table_exists(conn, c.table)], log)
        # Transação e rollback: todas as mutações ocorrem após BEGIN e são revertidas em erro crítico ou dry-run.
        conn.execute("BEGIN")
        log.info("Transação iniciada%s", " em modo dry-run" if args.dry_run else "")
        clear_tables(conn, clear_list, args.dry_run, args.strict, log)
        for cfg in ordered:
            key_maps[cfg.table] = regenerate_keys(conn, cfg, seed, args.dry_run, log)
        propagate_keys(conn, ordered, key_maps, args.dry_run, log)
        for cfg in ordered:
            anonymize_table(conn, cfg, seed, args.dry_run, log)
        for cfg in ordered:
            recalc_rowkeys(conn, cfg, args.dry_run, log)
        fk_issues = validate_foreign_keys(conn, ordered, args.strict, log)
        if args.dry_run:
            rowkey_issues = 0
            log.info("Validação de rowkey simulada em dry-run; alterações não foram persistidas para comparação física.")
        else:
            rowkey_issues = validate_rowkeys(conn, ordered, args.strict, log)
        if args.dry_run:
            # Modo dry-run: simula o fluxo completo e descarta qualquer alteração na cópia.
            conn.rollback()
            log.info("Dry-run concluído; alterações descartadas na cópia de saída.")
        else:
            conn.commit()
            log.info("Transação confirmada.")
        log.info("Relatório final: entidades=%d tabelas_limpas=%d avisos=%d erros=%d fk_issues=%d rowkey_issues=%d", len(configs), len(clear_list), len(log.warnings), len(log.errors), fk_issues, rowkey_issues)
        print(f"Base de saída: {out_db}")
        print(f"Log: logs/anonimizacao.log")
        return 0 if not log.errors else 2
    except Exception as exc:
        conn.rollback()
        log.error("Erro crítico; rollback executado. A base final não deve ser usada. Motivo: %s", exc)
        return 1
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sanitiza uma base SQLite para demonstrações comerciais.")
    parser.add_argument("--db", help="Caminho da base SQLite original")
    parser.add_argument("--out", help="Caminho da base sanitizada de saída")
    parser.add_argument("--sensitive-dir", default="arquivos_sensiveis", help="Pasta com configurações por entidade")
    parser.add_argument("--clear-file", default="clearTables.txt", help="Arquivo com tabelas a limpar")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem persistir alterações na cópia")
    parser.add_argument("--seed", help="Seed para geração determinística")
    parser.add_argument("--verbose", action="store_true", help="Exibe logs detalhados")
    parser.add_argument("--key-mode", choices=["preserve", "regenerate"], help="Sobrescreve a política de chave das entidades")
    parser.add_argument("--strict", action="store_true", help="Falha em configurações inválidas e inconsistências")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    log = setup_logging(args.verbose)
    log.info("Início do processamento em %s", datetime.now(UTC).isoformat(timespec="seconds"))
    if args.key_mode:
        log.warning("--key-mode informado; use com cautela, pois alteração de chaves é crítica.")
    return process_database(args, log)


if __name__ == "__main__":
    sys.exit(main())
