"""Self-validating metadata for daily co2gap parquet artefacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from release_manifest import sha256_file


CONTRACT_KEY = b"co2gap.artifact-contract"
CONTRACT_VERSION = 1


class ContractError(RuntimeError):
    pass


def file_fingerprint(path: Path) -> dict:
    path = Path(path)
    return {"sha256": sha256_file(path), "bytes": path.stat().st_size}


def keyset_checksum(keys: Iterable[tuple[str, int]]) -> tuple[str, int]:
    normal = sorted((str(day), int(fid)) for day, fid in keys)
    if len(normal) != len(set(normal)):
        raise ContractError("duplicate (day, flight_id) key")
    h = hashlib.sha256()
    for day, fid in normal:
        h.update(f"{day}\0{fid}\n".encode())
    return h.hexdigest(), len(normal)


def frame_keys(df: pd.DataFrame) -> list[tuple[str, int]]:
    if not {"day", "flight_id"}.issubset(df.columns):
        raise ContractError("artifact lacks day and flight_id")
    return list(zip(df["day"].astype(str), df["flight_id"].astype(int)))


def schema_checksum(schema: pa.Schema) -> str:
    fields = [(f.name, str(f.type), f.nullable) for f in schema]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def build_contract(*, stage: str, stage_version: int, day: str,
                   table: pa.Table, expected_keys: Iterable[tuple[str, int]],
                   inputs: dict[str, dict], configuration: dict) -> dict:
    df_keys = table.select(["day", "flight_id"]).to_pandas()
    actual_hash, actual_n = keyset_checksum(frame_keys(df_keys))
    expected_hash, expected_n = keyset_checksum(expected_keys)
    if actual_n != expected_n or actual_hash != expected_hash:
        raise ContractError(
            f"{stage} {day}: output keyset differs from expected "
            f"({actual_n} vs {expected_n} rows)")
    return {
        "contract_version": CONTRACT_VERSION,
        "stage": stage,
        "stage_version": stage_version,
        "day": day,
        "rows": table.num_rows,
        "schema_sha256": schema_checksum(table.schema),
        "key_columns": ["day", "flight_id"],
        "keyset_sha256": actual_hash,
        "expected_rows": expected_n,
        "expected_keyset_sha256": expected_hash,
        "inputs": inputs,
        "configuration": configuration,
    }


def write_parquet(*, table: pa.Table, path: Path, stage: str,
                  stage_version: int, day: str,
                  expected_keys: Iterable[tuple[str, int]], inputs: dict[str, dict],
                  configuration: dict) -> dict:
    contract = build_contract(
        stage=stage, stage_version=stage_version, day=day, table=table,
        expected_keys=expected_keys, inputs=inputs, configuration=configuration)
    metadata = dict(table.schema.metadata or {})
    metadata[CONTRACT_KEY] = json.dumps(
        contract, sort_keys=True, separators=(",", ":")).encode()
    pq.write_table(table.replace_schema_metadata(metadata), Path(path))
    return contract


def read_contract(path: Path) -> dict:
    metadata = pq.read_metadata(path)
    raw = (metadata.metadata or {}).get(CONTRACT_KEY)
    if raw is None:
        raise ContractError(f"{path} has no co2gap artifact contract")
    try:
        contract = json.loads(raw)
    except Exception as exc:
        raise ContractError(f"{path} has invalid contract JSON: {exc}") from exc
    if contract.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(f"{path} has unsupported contract version")
    if contract.get("rows") != metadata.num_rows:
        raise ContractError(f"{path} row count differs from its contract")
    return contract


def validate_parquet(path: Path, *, stage: str, stage_version: int, day: str,
                     inputs: dict[str, dict], configuration: dict) -> dict:
    path = Path(path)
    contract = read_contract(path)
    expected_header = {
        "stage": stage, "stage_version": stage_version, "day": day,
        "inputs": inputs, "configuration": configuration,
    }
    for key, expected in expected_header.items():
        if contract.get(key) != expected:
            raise ContractError(
                f"{path}: contract {key} differs ({contract.get(key)!r} != {expected!r})")
    table = pq.read_table(path, columns=["day", "flight_id"])
    actual_hash, actual_n = keyset_checksum(frame_keys(table.to_pandas()))
    if actual_n != contract["rows"] or actual_hash != contract["keyset_sha256"]:
        raise ContractError(f"{path}: keyset differs from its contract")
    arrow_schema = pq.read_schema(path).remove_metadata()
    if schema_checksum(arrow_schema) != contract["schema_sha256"]:
        raise ContractError(f"{path}: schema differs from its contract")
    if (contract["expected_rows"] != contract["rows"] or
            contract["expected_keyset_sha256"] != contract["keyset_sha256"]):
        raise ContractError(f"{path}: stored output did not satisfy expected keyset")
    return contract


def manifest_allowed_missing(manifest, stage: str) -> set[tuple[str, int]]:
    if manifest is None:
        return set()
    entries = manifest.data.get("exceptions", {}).get(f"{stage}_missing_keys", [])
    return {(str(e["day"]), int(e["flight_id"])) for e in entries}
