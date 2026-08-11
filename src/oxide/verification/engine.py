"""Fail-closed policy and Verus runner for a target-owned Rust verification contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class VerificationError(Exception):
    """A deterministic subject or policy failure."""


class InfrastructureError(Exception):
    """The requested authoritative execution could not be completed."""


_HOST_ASSETS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
}
_DEFAULT_CONTRACT_PATH = "verification/contract.toml"
_EVIDENCE_SCHEMA = Path(__file__).with_name("evidence.schema.json")
_MANDATORY_FORBIDDEN_PATTERNS = (
    r"\bassume\s*\(",
    r"\badmit\s*\(",
    r"verifier\s*::\s*external_body",
    r"external_fn_specification",
    r"\baxiom\b",
    r"\bensures\s*(?:\(\s*)?(?:true|!\s*false)\b",
    r"\brequires\s*(?:\(\s*)?(?:false|!\s*true)\b",
    r"\bunsafe\b",
    r"#\s*\[\s*cfg\b",
    r"\bcfg\s*!\s*\(",
    r"\binclude\s*!\s*\(",
    r'\bextern\s+"',
)


def engine_digest() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), _EVIDENCE_SCHEMA):
        data = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise VerificationError(f"cannot read TOML {path}: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"expected TOML table in {path}")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"expected JSON object in {path}")
    return value


def inside(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VerificationError(f"path escapes repository: {value}") from error
    return candidate


def string_list(
    value: Any,
    field: str,
    *,
    nonempty: bool = False,
    unique: bool = True,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise VerificationError(f"{field} must be a list of nonempty strings")
    if nonempty and not value:
        raise VerificationError(f"{field} must not be empty")
    if unique and len(set(value)) != len(value):
        raise VerificationError(f"{field} contains duplicates")
    return value


def tables(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise VerificationError(f"{field} must be an array of tables")
    return value


def existing_paths(root: Path, values: Iterable[str], field: str) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = inside(root, value)
        if not path.exists():
            raise VerificationError(f"{field} path does not exist: {value}")
        paths.append(path)
    return paths


def files_under(paths: Iterable[Path], suffix: str | None = None) -> list[Path]:
    found: set[Path] = set()
    for path in paths:
        if path.is_file():
            candidates = [path]
        else:
            candidates = [item for item in path.rglob("*") if item.is_file()]
        for item in candidates:
            if suffix is None or item.suffix == suffix:
                found.add(item.resolve())
    return sorted(found)


def rust_symbol(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise VerificationError(f"{field} must be one Rust identifier")
    return value


def rust_code(path: Path) -> str:
    """Return Rust source with comments and literals blanked for policy matching."""
    source = path.read_text(encoding="utf-8", errors="strict")
    output = list(source)
    index = 0
    length = len(source)

    def blank(start: int, stop: int) -> None:
        for cursor in range(start, stop):
            if output[cursor] != "\n":
                output[cursor] = " "

    while index < length:
        if source.startswith("//", index):
            stop = source.find("\n", index + 2)
            stop = length if stop < 0 else stop
            blank(index, stop)
            index = stop
            continue
        if source.startswith("/*", index):
            depth = 1
            stop = index + 2
            while stop < length and depth:
                if source.startswith("/*", stop):
                    depth += 1
                    stop += 2
                elif source.startswith("*/", stop):
                    depth -= 1
                    stop += 2
                else:
                    stop += 1
            blank(index, stop)
            index = stop
            continue
        raw = re.match(r"(?:br|rb|r)(?P<hashes>#{0,255})\"", source[index:])
        if raw is not None:
            delimiter = '"' + raw.group("hashes")
            stop = source.find(delimiter, index + raw.end())
            stop = length if stop < 0 else stop + len(delimiter)
            blank(index, stop)
            index = stop
            continue
        quote_index = index + 1 if source.startswith('b"', index) else index
        if quote_index < length and source[quote_index] == '"':
            stop = quote_index + 1
            escaped = False
            while stop < length:
                character = source[stop]
                stop += 1
                if character == '"' and not escaped:
                    break
                escaped = character == "\\" and not escaped
                if character != "\\":
                    escaped = False
            blank(index, stop)
            index = stop
            continue
        char_match = re.match(r"(?:b)?'(?:\\.|[^'\\\n])'", source[index:])
        if char_match is not None:
            stop = index + char_match.end()
            blank(index, stop)
            index = stop
            continue
        index += 1
    return "".join(output)


def function_definition_paths(
    paths: Iterable[Path], symbol: str, *, proof: bool, public: bool = False
) -> list[Path]:
    visibility = r"pub(?:\s*\([^)]*\))?\s+" if public else r"(?:pub(?:\s*\([^)]*\))?\s+)?"
    mode = r"proof\s+" if proof else r"(?:exec\s+)?"
    pattern = re.compile(rf"\b{visibility}{mode}fn\s+{re.escape(symbol)}\b")
    return [
        path
        for path in paths
        if path.is_file() and path.suffix == ".rs" and pattern.search(rust_code(path))
    ]


def require_function(
    paths: Iterable[Path], symbol: str, field: str, *, proof: bool, public: bool = False
) -> Path:
    matches = function_definition_paths(paths, symbol, proof=proof, public=public)
    if not matches:
        kind = "proof function" if proof else "public executable function"
        raise VerificationError(
            f"{field} {kind} is absent from its declared source closure: {symbol}"
        )
    if len(matches) != 1:
        raise VerificationError(
            f"{field} has multiple definitions in its declared closure: {symbol}"
        )
    return matches[0]


def function_body(paths: Iterable[Path], symbol: str, field: str) -> str:
    declaration = re.compile(
        rf"\b(?:pub(?:\s*\([^)]*\))?\s+)?(?:proof\s+|exec\s+|spec\s+)?"
        rf"fn\s+{re.escape(symbol)}\b"
    )
    for path in paths:
        if not path.is_file() or path.suffix != ".rs":
            continue
        code = rust_code(path)
        match = declaration.search(code)
        if match is None:
            continue
        opening = code.find("{", match.end())
        if opening < 0:
            break
        depth = 1
        cursor = opening + 1
        while cursor < len(code) and depth:
            if code[cursor] == "{":
                depth += 1
            elif code[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            return code[opening + 1 : cursor - 1]
        break
    raise VerificationError(f"{field} has no executable proof body: {symbol}")


def require_function_call(body: str, symbol: str, field: str) -> None:
    if re.search(rf"\b{re.escape(symbol)}\s*\(", body) is None:
        raise VerificationError(f"{field} does not invoke required theorem: {symbol}")


_PATH_MODULE = re.compile(
    r'#\s*\[\s*path\s*=\s*"([^"]+)"\s*\]\s*'
    r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;"
)
_FILE_MODULE = re.compile(r"\b(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;")


def path_module_bindings(repository: Path, source_path: Path) -> list[tuple[Path, str]]:
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise VerificationError(f"cannot read Rust module {source_path}: {error}") from error
    code = rust_code(source_path)
    bindings: list[tuple[Path, str]] = []
    for match in _PATH_MODULE.finditer(source):
        if code[match.start()] != "#":
            continue
        target = (source_path.parent / match.group(1)).resolve()
        if not target.is_relative_to(repository) or not target.is_file():
            raise VerificationError(
                f"Rust path module escapes or is absent: {match.group(1)} in "
                f"{source_path.relative_to(repository)}"
            )
        bindings.append((target, match.group(2)))
    return bindings


def rust_module_closure(repository: Path, entry: Path) -> set[Path]:
    """Resolve the checked crate's file-module graph without executing build code."""
    pending = [entry.resolve()]
    reachable: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        if not current.is_relative_to(repository) or not current.is_file():
            raise VerificationError(f"Rust module is outside the repository or absent: {current}")
        reachable.add(current)
        try:
            source = current.read_text(encoding="utf-8")
        except OSError as error:
            raise VerificationError(f"cannot read Rust module {current}: {error}") from error
        code = rust_code(current)
        explicit = list(_PATH_MODULE.finditer(source))
        explicit_spans: list[tuple[int, int]] = []
        for match in explicit:
            if code[match.start()] != "#":
                continue
            target = (current.parent / match.group(1)).resolve()
            if not target.is_relative_to(repository) or not target.is_file():
                raise VerificationError(
                    f"Rust path module escapes or is absent: {match.group(1)} in "
                    f"{current.relative_to(repository)}"
                )
            explicit_spans.append(match.span())
            pending.append(target)

        module_directory = (
            current.parent
            if current.name in {"lib.rs", "main.rs", "mod.rs"}
            else current.parent / current.stem
        )
        for match in _FILE_MODULE.finditer(code):
            if any(start <= match.start() < stop for start, stop in explicit_spans):
                continue
            module = match.group(1)
            candidates = [
                (module_directory / f"{module}.rs").resolve(),
                (module_directory / module / "mod.rs").resolve(),
            ]
            present = [path for path in candidates if path.is_file()]
            if len(present) != 1:
                raise VerificationError(
                    f"Rust module {module} from {current.relative_to(repository)} "
                    f"must resolve to exactly one source file"
                )
            if not present[0].is_relative_to(repository):
                raise VerificationError(f"Rust module escapes the repository: {present[0]}")
            pending.append(present[0])
    return reachable


def require_composition_module_binding(
    repository: Path,
    production_entry: Path,
    composition_root: Path,
    module: str,
) -> None:
    matches = [
        target
        for target, bound_module in path_module_bindings(repository, production_entry)
        if bound_module == module
    ]
    if len(matches) != 1:
        raise VerificationError(
            "production entry must bind the declared composition module through one exact path"
        )
    if matches[0] != composition_root:
        raise VerificationError(
            "production composition module does not resolve to the declared composition root: "
            f"{matches[0].relative_to(repository)}"
        )


def require_meaningful_contract(paths: Iterable[Path], component_id: str) -> None:
    text = "\n".join(rust_code(path) for path in paths if path.is_file() and path.suffix == ".rs")
    if re.search(r"\b(?:requires|ensures|invariant)\b", text) is None:
        raise VerificationError(
            f"component {component_id} contracts state no requires, ensures, or invariant obligation"
        )


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _rustup_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if shutil.which("rustup", path=environment.get("PATH")) is None:
        homebrew = Path("/opt/homebrew/opt/rustup/bin/rustup")
        if homebrew.is_file():
            environment["PATH"] = f"{homebrew.parent}:{environment.get('PATH', '')}"
    if shutil.which("rustup", path=environment.get("PATH")) is None:
        raise InfrastructureError("rustup is unavailable for the pinned Verus toolchain")
    return environment


def _toolchain_archive(lock: dict[str, Any]) -> tuple[Path, str, dict[str, Any]]:
    key = _HOST_ASSETS.get((platform.system(), platform.machine()))
    assets = lock.get("assets")
    if key is None or not isinstance(assets, dict) or not isinstance(assets.get(key), dict):
        raise InfrastructureError(
            f"no pinned Verus asset for host {platform.system()}/{platform.machine()}"
        )
    asset = assets[key]
    name = asset.get("name")
    expected = asset.get("sha256")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected) is None
    ):
        raise VerificationError(f"toolchain asset {key} is malformed")
    configured = os.environ.get("OXIDE_VERUS_ARCHIVE")
    if configured:
        archive = Path(configured).expanduser().resolve()
    elif platform.system() == "Darwin":
        archive = Path.home() / "Library" / "Caches" / "oxide-verus" / name
    else:
        archive = (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "oxide-verus" / name
        )
    if not archive.is_file():
        raise InfrastructureError(
            f"pinned Verus archive is unavailable: {archive}; set OXIDE_VERUS_ARCHIVE"
        )
    if sha256_file(archive) != f"sha256:{expected}":
        raise InfrastructureError(f"pinned Verus archive digest mismatch: {archive}")
    return archive, key, asset


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    path = Path(member.filename)
    mode = member.external_attr >> 16
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or stat.S_IFMT(mode) == stat.S_IFLNK
    ):
        raise InfrastructureError(f"unsafe entry in pinned Verus archive: {member.filename}")


@contextmanager
def pinned_verus(lock: dict[str, Any]) -> Iterable[tuple[Path, dict[str, str], dict[str, Any]]]:
    archive, host_key, asset = _toolchain_archive(lock)
    verus_lock = lock.get("verus")
    if not isinstance(verus_lock, dict):
        raise VerificationError("toolchain lock lacks Verus metadata")
    with tempfile.TemporaryDirectory(prefix="oxide-verus-") as raw:
        root = Path(raw)
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                for member in members:
                    _validate_zip_member(member)
                version_members = [
                    item for item in members if Path(item.filename).name == "version.json"
                ]
                if len(version_members) != 1:
                    raise InfrastructureError("pinned Verus archive has no unique version.json")
                version = json.loads(bundle.read(version_members[0]).decode("utf-8"))
                bundle.extractall(root)
                for member in members:
                    mode = member.external_attr >> 16
                    destination = root / member.filename
                    if destination.exists() and stat.S_ISREG(mode):
                        destination.chmod(mode & 0o777)
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InfrastructureError(
                f"cannot materialize pinned Verus archive: {error}"
            ) from error
        metadata = version.get("verus") if isinstance(version, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("version") != verus_lock.get("release")
            or metadata.get("commit") != verus_lock.get("revision")
            or not str(metadata.get("toolchain", "")).startswith(
                str(verus_lock.get("rust_toolchain", ""))
            )
        ):
            raise InfrastructureError("pinned Verus archive metadata differs from the lock")
        version_path = root / version_members[0].filename
        executable = version_path.parent / (
            "verus.exe" if platform.system() == "Windows" else "verus"
        )
        if not executable.is_file():
            raise InfrastructureError("pinned Verus archive lacks its verifier executable")
        environment = _rustup_environment()
        details = {
            "host": host_key,
            "asset": asset["name"],
            "asset_sha256": f"sha256:{asset['sha256']}",
            "verus_version": metadata["version"],
            "verus_revision": metadata["commit"],
            "rust_toolchain": metadata["toolchain"],
        }
        yield executable, environment, details


def closure_digest(root: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted({item.resolve() for item in paths}):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.as_posix()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def git_tree(repository: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) not in {40, 64}:
        raise InfrastructureError(f"cannot resolve Git tree: {completed.stderr.strip()}")
    return value


def exact_tree(value: str | None, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise VerificationError(f"{field} is not a Git tree identity")
    return value


def require_exact_checkout(repository: Path, prospective_tree: str) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise InfrastructureError(f"cannot inspect prospective checkout: {status.stderr.strip()}")
    if status.stdout:
        raise VerificationError("prospective verification checkout is not clean")
    actual = git_tree(repository)
    if actual != prospective_tree:
        raise VerificationError(
            f"prospective tree does not identify the exact checkout: {prospective_tree} != {actual}"
        )


def validate_component(
    component: dict[str, Any], repository: Path, policy: dict[str, Any], composition: str
) -> tuple[str, set[Path], list[Path], Path]:
    required = {
        "id",
        "classification",
        "sources",
        "public_entries",
        "product_requirements",
        "abstract_spec",
        "contracts",
        "proof_roots",
        "refinement_theorem",
        "composition_theorem",
        "trusted_assumptions",
        "features",
        "target",
    }
    missing = required - component.keys()
    if missing:
        raise VerificationError(f"component missing fields: {', '.join(sorted(missing))}")
    component_id = component["id"]
    if not isinstance(component_id, str) or not component_id:
        raise VerificationError("component id must be a nonempty string")
    if component["classification"] != "verified-production":
        raise VerificationError(f"component {component_id} is not verified-production")
    refinement = rust_symbol(
        component["refinement_theorem"], f"component {component_id}.refinement_theorem"
    )
    composition_member = rust_symbol(
        component["composition_theorem"], f"component {component_id}.composition_theorem"
    )
    if not isinstance(component["abstract_spec"], str) or not component["abstract_spec"]:
        raise VerificationError(f"component {component_id}.abstract_spec must be nonempty")
    if composition_member != composition:
        raise VerificationError(f"component {component_id} is disconnected from composition")
    if component["target"] != policy["target"]:
        raise VerificationError(f"component {component_id} target differs from policy")
    if (
        string_list(component["features"], f"component {component_id}.features", nonempty=True)
        != policy["production_features"]
    ):
        raise VerificationError(f"component {component_id} features differ from policy")
    source_values = string_list(
        component["sources"], f"component {component_id}.sources", nonempty=True
    )
    public_entries = string_list(
        component["public_entries"], f"component {component_id}.public_entries"
    )
    string_list(
        component["product_requirements"],
        f"component {component_id}.product_requirements",
        nonempty=True,
    )
    contract_values = string_list(
        component["contracts"], f"component {component_id}.contracts", nonempty=True
    )
    proof_values = string_list(
        component["proof_roots"], f"component {component_id}.proof_roots", nonempty=True
    )
    string_list(component["trusted_assumptions"], f"component {component_id}.trusted_assumptions")
    paths = existing_paths(repository, source_values, f"component {component_id}.sources")
    supporting = existing_paths(
        repository,
        [component["abstract_spec"], *contract_values, *proof_values],
        f"component {component_id} proof closure",
    )
    source_files = files_under(paths, ".rs")
    contract_files = existing_paths(
        repository, contract_values, f"component {component_id}.contracts"
    )
    proof_files = existing_paths(repository, proof_values, f"component {component_id}.proof_roots")
    for entry in public_entries:
        require_function(
            source_files,
            rust_symbol(entry, f"component {component_id}.public_entries"),
            f"component {component_id}.public_entries",
            proof=False,
            public=True,
        )
    refinement_path = require_function(
        proof_files,
        refinement,
        f"component {component_id}.refinement_theorem",
        proof=True,
    )
    require_meaningful_contract(contract_files, component_id)
    return component_id, set(files_under(paths, ".rs")), supporting, refinement_path


def validate_adapter(
    adapter: dict[str, Any], repository: Path
) -> tuple[str, set[Path], list[Path]]:
    required = {
        "id",
        "classification",
        "sources",
        "contract",
        "observations",
        "assumptions",
        "independent_review",
    }
    missing = required - adapter.keys()
    if missing:
        raise VerificationError(f"trusted adapter missing fields: {', '.join(sorted(missing))}")
    adapter_id = adapter["id"]
    if not isinstance(adapter_id, str) or not adapter_id:
        raise VerificationError("trusted adapter id must be nonempty")
    if adapter["classification"] != "trusted-effect-adapter":
        raise VerificationError(f"adapter {adapter_id} has invalid classification")
    source_values = string_list(adapter["sources"], f"adapter {adapter_id}.sources", nonempty=True)
    string_list(adapter["observations"], f"adapter {adapter_id}.observations", nonempty=True)
    string_list(adapter["assumptions"], f"adapter {adapter_id}.assumptions", nonempty=True)
    if not isinstance(adapter["contract"], str) or not adapter["contract"]:
        raise VerificationError(f"adapter {adapter_id}.contract must be nonempty")
    if not isinstance(adapter["independent_review"], str) or not adapter["independent_review"]:
        raise VerificationError(f"adapter {adapter_id} lacks independent review identity")
    paths = existing_paths(repository, source_values, f"adapter {adapter_id}.sources")
    support = existing_paths(repository, [adapter["contract"]], f"adapter {adapter_id}.contract")
    require_meaningful_contract(support, adapter_id)
    return adapter_id, set(files_under(paths, ".rs")), support


def validate_assumption(assumption: dict[str, Any]) -> str:
    required = {"id", "statement", "boundary", "evidence", "independent_review"}
    missing = required - assumption.keys()
    if missing:
        raise VerificationError(f"trusted assumption missing fields: {', '.join(sorted(missing))}")
    assumption_id = assumption.get("id")
    if not isinstance(assumption_id, str) or not assumption_id:
        raise VerificationError("trusted assumption id must be nonempty")
    for field in ("statement", "boundary", "independent_review"):
        if not isinstance(assumption.get(field), str) or not assumption[field]:
            raise VerificationError(f"trusted assumption {assumption_id}.{field} must be nonempty")
    string_list(
        assumption.get("evidence"), f"trusted assumption {assumption_id}.evidence", nonempty=True
    )
    return assumption_id


def validate_policy(
    repository: Path,
    contract_root: Path,
    contract_path: str = _DEFAULT_CONTRACT_PATH,
) -> tuple[dict[str, Any], dict[str, Any], list[Path]]:
    policy = read_toml(inside(contract_root, contract_path))
    if policy.get("schema") != 3:
        raise VerificationError("unsupported verification policy schema")
    if policy.get("hash_algorithm") != "sha256":
        raise VerificationError("verification policy must use sha256 identities")
    for field in (
        "manifest",
        "toolchain_lock",
        "verification_spec",
        "product_spec",
        "target",
        "production_entry",
        "composition_root",
        "composition_module",
        "composition_theorem",
    ):
        if not isinstance(policy.get(field), str) or not policy[field]:
            raise VerificationError(f"policy.{field} must be nonempty")
    policy["production_features"] = string_list(
        policy.get("production_features"), "policy.production_features", nonempty=True
    )
    solver_rlimit = policy.get("solver_rlimit", 10)
    if (
        isinstance(solver_rlimit, bool)
        or not isinstance(solver_rlimit, int)
        or not 1 <= solver_rlimit <= 1000
    ):
        raise VerificationError("contract.solver_rlimit must be between 1 and 1000")
    policy["solver_rlimit"] = solver_rlimit
    composition_module = rust_symbol(policy["composition_module"], "policy.composition_module")
    composition_theorem = rust_symbol(policy["composition_theorem"], "policy.composition_theorem")
    additional_forbidden = string_list(
        policy.get("additional_forbidden_patterns", []),
        "contract.additional_forbidden_patterns",
    )
    for pattern in additional_forbidden:
        try:
            re.compile(pattern)
        except re.error as error:
            raise VerificationError(
                f"contract.additional_forbidden_patterns contains invalid regex: {pattern!r}"
            ) from error
    forbidden = [*_MANDATORY_FORBIDDEN_PATTERNS, *additional_forbidden]
    immutable_paths = string_list(
        policy.get("immutable_paths"), "contract.immutable_paths", nonempty=True
    )
    required_immutable = (
        contract_path,
        str(policy["toolchain_lock"]),
        str(policy["verification_spec"]),
        str(policy["product_spec"]),
    )
    for required in required_immutable:
        if not any(
            required == prefix.rstrip("/") or required.startswith(prefix.rstrip("/") + "/")
            for prefix in immutable_paths
        ):
            raise VerificationError(
                f"contract.immutable_paths does not freeze judge input: {required}"
            )
    frozen_paths = existing_paths(contract_root, immutable_paths, "contract.immutable_paths")
    for value, frozen in zip(immutable_paths, frozen_paths, strict=True):
        candidate = inside(repository, value)
        equal = False
        if frozen.is_file() and candidate.is_file():
            equal = sha256_file(candidate) == sha256_file(frozen)
        elif frozen.is_dir() and candidate.is_dir():
            frozen_files = files_under([frozen])
            candidate_files = files_under([candidate])
            frozen_relative = [path.relative_to(frozen).as_posix() for path in frozen_files]
            candidate_relative = [
                path.relative_to(candidate).as_posix() for path in candidate_files
            ]
            equal = frozen_relative == candidate_relative and all(
                sha256_file(left) == sha256_file(right)
                for left, right in zip(frozen_files, candidate_files, strict=True)
            )
        if not equal:
            raise VerificationError(
                f"candidate modifies an immutable verification-contract input: {value}"
            )
    toolchain = read_toml(inside(contract_root, policy["toolchain_lock"]))
    if toolchain.get("schema") != 1:
        raise VerificationError("unsupported toolchain lock schema")
    verus = toolchain.get("verus")
    if (
        not isinstance(verus, dict)
        or not isinstance(verus.get("revision"), str)
        or len(verus["revision"]) != 40
    ):
        raise VerificationError("toolchain lock lacks full Verus revision")
    execution = toolchain.get("verification")
    if (
        not isinstance(execution, dict)
        or isinstance(execution.get("timeout_seconds"), bool)
        or not isinstance(execution.get("timeout_seconds"), int)
        or not 1 <= execution["timeout_seconds"] <= 86400
        or execution.get("random_seed") != 0
        or execution.get("resource_policy") != "fail-closed"
    ):
        raise VerificationError("toolchain verification policy is malformed or nondeterministic")
    evidence = policy.get("evidence")
    if not isinstance(evidence, dict):
        raise VerificationError("contract.evidence must be a table")
    for field, limit in (
        ("max_log_bytes", 16777216),
        ("max_artifacts", 1024),
        ("max_artifact_bytes", 67108864),
    ):
        value = evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise VerificationError(f"contract.evidence.{field} is outside its safe bound")
    schema = read_json(_EVIDENCE_SCHEMA)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise InfrastructureError("harness proof evidence schema is not draft 2020-12")

    manifest = read_toml(inside(repository, policy["manifest"]))
    if manifest.get("schema") != 1:
        raise VerificationError("unsupported coverage manifest schema")
    status = manifest.get("status")
    if status not in {"unimplemented", "implemented"}:
        raise VerificationError("manifest.status must be unimplemented or implemented")
    assurance_claim = manifest.get("assurance_claim")
    if assurance_claim not in {"none", "exact-context-refinement"}:
        raise VerificationError("manifest.assurance_claim is unsupported")
    trusted_computing_base = string_list(
        manifest.get("trusted_computing_base"),
        "manifest.trusted_computing_base",
        nonempty=True,
    )
    assumptions = tables(manifest.get("assumptions"), "manifest.assumptions")
    assumption_ids = [validate_assumption(item) for item in assumptions]
    if len(set(assumption_ids)) != len(assumption_ids):
        raise VerificationError("trusted assumption ids must be unique")
    components = tables(manifest.get("components"), "manifest.components")
    adapters = tables(manifest.get("trusted_adapters"), "manifest.trusted_adapters")
    tooling = tables(manifest.get("tooling"), "manifest.tooling")
    composition = manifest.get("composition_theorem")
    if not isinstance(composition, str):
        raise VerificationError("manifest.composition_theorem must be a string")
    members = string_list(manifest.get("composition_members"), "manifest.composition_members")
    shared_proof_values = string_list(
        manifest.get("shared_proof_closure"), "manifest.shared_proof_closure"
    )
    if status == "unimplemented":
        if (
            assurance_claim != "none"
            or components
            or adapters
            or assumptions
            or composition
            or members
            or shared_proof_values
        ):
            raise VerificationError(
                "unimplemented manifest cannot claim assurance, components, adapters, assumptions, or proof composition"
            )
    elif assurance_claim != "exact-context-refinement" or not components or not composition:
        raise VerificationError(
            "implemented manifest needs exact-context-refinement assurance, components, and composition"
        )
    if composition:
        rust_symbol(composition, "manifest.composition_theorem")
        if composition != composition_theorem:
            raise VerificationError(
                "manifest composition theorem differs from the frozen contract theorem"
            )

    classified: dict[Path, str] = {}
    closure: list[Path] = []
    component_ids: list[str] = []
    refinement_paths: dict[str, Path] = {}
    for component in components:
        component_id, sources, support, refinement_path = validate_component(
            component, repository, policy, composition
        )
        component_ids.append(component_id)
        refinement_paths[component_id] = refinement_path
        closure.extend([*sources, *support])
        for source in sources:
            if source in classified:
                raise VerificationError(f"source has multiple classifications: {source}")
            classified[source] = component_id
    refinement_symbols = [str(component["refinement_theorem"]) for component in components]
    if len(set(refinement_symbols)) != len(refinement_symbols):
        raise VerificationError("component refinement theorem names must be unique")
    adapter_ids: list[str] = []
    for adapter in adapters:
        adapter_id, sources, support = validate_adapter(adapter, repository)
        adapter_ids.append(adapter_id)
        closure.extend([*sources, *support])
        for source in sources:
            if source in classified:
                raise VerificationError(f"source has multiple classifications: {source}")
            classified[source] = adapter_id
    if len(set(component_ids + adapter_ids)) != len(component_ids + adapter_ids):
        raise VerificationError("component and adapter ids must be globally unique")
    if set(adapter_ids) - set(trusted_computing_base):
        raise VerificationError(
            "every trusted effect adapter id must appear in trusted_computing_base"
        )
    if sorted(members) != sorted(component_ids):
        raise VerificationError("composition_members must equal all verified component ids")
    referenced_assumptions = {
        value
        for owner in [*components, *adapters]
        for value in owner.get("trusted_assumptions", owner.get("assumptions", []))
    }
    if referenced_assumptions - set(assumption_ids):
        raise VerificationError("component or adapter references an undeclared trusted assumption")
    if set(assumption_ids) - referenced_assumptions:
        raise VerificationError("trusted assumption is not connected to a component or adapter")

    tooling_files: dict[Path, str] = {}
    tooling_ids: set[str] = set()
    for item in tooling:
        tooling_id = item.get("id")
        if not isinstance(tooling_id, str) or not tooling_id or tooling_id in tooling_ids:
            raise VerificationError("tooling ids must be unique nonempty strings")
        tooling_ids.add(tooling_id)
        if item.get("authority") != "none":
            raise VerificationError("non-authoritative tooling cannot hold authority")
        tooling_paths = existing_paths(
            repository,
            string_list(item.get("paths"), f"tooling {tooling_id}.paths", nonempty=True),
            f"tooling {tooling_id}.paths",
        )
        for path in files_under(tooling_paths):
            if path in tooling_files:
                raise VerificationError(f"tooling path has multiple classifications: {path}")
            tooling_files[path] = tooling_id

    production_roots = [
        inside(repository, item)
        for item in string_list(
            policy.get("production_roots"), "policy.production_roots", nonempty=True
        )
    ]
    trusted_adapter_roots = [
        inside(repository, item)
        for item in string_list(
            policy.get("trusted_adapter_roots"),
            "policy.trusted_adapter_roots",
            nonempty=True,
        )
    ]
    for source, owner in classified.items():
        if not any(source.is_relative_to(root) for root in production_roots):
            raise VerificationError(
                f"classified production source lies outside production roots: "
                f"{source.relative_to(repository)}"
            )
        in_adapter_root = any(source.is_relative_to(root) for root in trusted_adapter_roots)
        if owner in adapter_ids and not in_adapter_root:
            raise VerificationError(
                f"trusted adapter source lies outside the trusted adapter roots: "
                f"{source.relative_to(repository)}"
            )
        if owner in component_ids and in_adapter_root:
            raise VerificationError(
                f"verified production source occupies the trusted adapter boundary: "
                f"{source.relative_to(repository)}"
            )
    existing_roots = [path for path in production_roots if path.exists()]
    if status == "implemented" and len(existing_roots) != len(production_roots):
        raise VerificationError("implemented program is missing a production root")
    for source in files_under(existing_roots, ".rs"):
        if source not in classified:
            raise VerificationError(
                f"unclassified production source: {source.relative_to(repository)}"
            )

    non_authoritative_roots = [
        inside(repository, item)
        for item in string_list(
            policy.get("non_authoritative_roots"),
            "policy.non_authoritative_roots",
            nonempty=True,
        )
    ]
    for source in tooling_files:
        if not any(source.is_relative_to(root) for root in non_authoritative_roots):
            raise VerificationError(
                f"tooling classification lies outside non-authoritative roots: "
                f"{source.relative_to(repository)}"
            )
    for source in files_under(path for path in non_authoritative_roots if path.exists()):
        if source not in tooling_files:
            raise VerificationError(
                f"unclassified non-authoritative tooling: {source.relative_to(repository)}"
            )

    scan_roots: list[Path] = []
    for field in ("contract_roots", "abstract_model_roots", "proof_roots"):
        for value in string_list(policy.get(field), f"policy.{field}", nonempty=True):
            path = inside(repository, value)
            if path.exists():
                scan_roots.append(path)
    verified_sources = [path for path, owner in classified.items() if owner in component_ids]
    for path in files_under([*scan_roots, *verified_sources]):
        if path.suffix != ".rs":
            continue
        text = rust_code(path)
        for pattern in forbidden:
            if re.search(pattern, text):
                raise VerificationError(
                    f"forbidden proof escape {pattern!r} in {path.relative_to(repository)}"
                )

    if status == "implemented":
        composition_root = inside(repository, policy["composition_root"])
        if not composition_root.is_file():
            raise VerificationError("implemented manifest lacks its program composition proof root")
        production_entry = inside(repository, policy["production_entry"])
        if not production_entry.is_file():
            raise VerificationError("implemented manifest lacks its production crate entry")
        require_composition_module_binding(
            repository,
            production_entry,
            composition_root,
            composition_module,
        )
        reachable_modules = rust_module_closure(repository, production_entry)
        if composition_root not in reachable_modules:
            raise VerificationError("program composition proof root is unreachable from production")
        require_function(
            [composition_root],
            composition,
            "manifest.composition_theorem",
            proof=True,
        )
        body = function_body(
            [composition_root],
            composition,
            "manifest.composition_theorem",
        )
        composition_bindings = path_module_bindings(repository, composition_root)
        for component in components:
            component_id = str(component["id"])
            refinement = str(component["refinement_theorem"])
            bound_modules = [
                module
                for target, module in composition_bindings
                if target == refinement_paths[component_id]
            ]
            if len(bound_modules) != 1:
                raise VerificationError(
                    f"component {component_id} refinement proof must be bound once by exact "
                    "path from the composition root"
                )
            require_function_call(
                body,
                f"{bound_modules[0]}::{refinement}",
                "manifest.composition_theorem",
            )
        shared_proof_paths = existing_paths(
            repository,
            shared_proof_values,
            "manifest.shared_proof_closure",
        )
        actual_proof_files = set(files_under(scan_roots, ".rs"))
        declared_paths = [*closure, *shared_proof_paths, composition_root]
        declared_files = set(files_under(declared_paths, ".rs"))
        outside_roots = declared_files - actual_proof_files - set(classified)
        if outside_roots:
            rendered = sorted(path.relative_to(repository).as_posix() for path in outside_roots)
            raise VerificationError(
                f"declared verification support lies outside policy proof roots: {rendered}"
            )
        declared_proof_files = declared_files & actual_proof_files
        if declared_proof_files != actual_proof_files:
            missing = sorted(
                path.relative_to(repository).as_posix()
                for path in actual_proof_files - declared_proof_files
            )
            raise VerificationError(
                "verification source closure differs from manifest declarations: "
                f"unclassified={missing}"
            )
        required_reachable = (
            set(classified)
            | declared_proof_files
            | {
                production_entry,
                composition_root,
            }
        )
        unreachable = required_reachable - reachable_modules
        if unreachable:
            rendered = sorted(path.relative_to(repository).as_posix() for path in unreachable)
            raise VerificationError(
                "declared production/proof source is unreachable from the verified crate: "
                f"{rendered}"
            )
        theorem_search = actual_proof_files | set(classified)
        for component in components:
            component_id = str(component["id"])
            refinement = str(component["refinement_theorem"])
            definitions = set(function_definition_paths(theorem_search, refinement, proof=True))
            if definitions != {refinement_paths[component_id]}:
                raise VerificationError(
                    f"component {component_id} refinement theorem is duplicated or displaced"
                )
        closure.extend(shared_proof_paths)

    subject_files = [
        inside(repository, policy["manifest"]),
        inside(repository, policy["product_spec"]),
        *closure,
    ]
    verification_subject = inside(contract_root, policy["verification_spec"])
    if verification_subject.exists():
        subject_files.append(verification_subject)
    for field in ("contract_roots", "abstract_model_roots", "proof_roots"):
        roots = [
            inside(repository, value)
            for value in policy[field]
            if inside(repository, value).exists()
        ]
        subject_files.extend(files_under(roots))
    manifest["_component_ids"] = component_ids
    manifest["_adapter_ids"] = adapter_ids
    manifest["_trusted_computing_base"] = trusted_computing_base
    manifest["_assumption_ids"] = assumption_ids
    manifest["_closure_digest"] = closure_digest(repository, subject_files)
    return policy, {"manifest": manifest, "toolchain": toolchain}, subject_files


def write_artifact(data: bytes, directory: Path, suffix: str) -> dict[str, Any]:
    digest = sha256_bytes(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest.removeprefix('sha256:')}{suffix}"
    if path.exists() and path.read_bytes() != data:
        raise InfrastructureError(f"artifact digest collision at {path}")
    if not path.exists():
        path.write_bytes(data)
    return {"sha256": digest, "bytes": len(data), "media_type": "text/plain; charset=utf-8"}


def proof_receipt(
    repository: Path,
    contract_root: Path,
    policy: dict[str, Any],
    state: dict[str, Any],
    candidate_tree: str,
    prospective_tree: str,
    command: list[str],
    exit_code: int,
    result: str,
    log: dict[str, Any],
    toolchain_execution: dict[str, Any],
    contract_path: str = _DEFAULT_CONTRACT_PATH,
) -> dict[str, Any]:
    manifest = state["manifest"]
    toolchain = state["toolchain"]
    specifications = {
        policy["product_spec"]: sha256_file(inside(contract_root, policy["product_spec"])),
        policy["verification_spec"]: sha256_file(
            inside(contract_root, policy["verification_spec"])
        ),
    }
    for component in manifest["components"]:
        abstract_spec = str(component["abstract_spec"])
        specifications[abstract_spec] = sha256_file(inside(repository, abstract_spec))
    assumptions: list[str] = []
    for component in manifest["components"]:
        assumptions.extend(component["trusted_assumptions"])
    for adapter in manifest["trusted_adapters"]:
        assumptions.extend(adapter["assumptions"])
    theorem_roots = [component["refinement_theorem"] for component in manifest["components"]]
    theorem_roots.append(manifest["composition_theorem"])
    return {
        "schema": "OxideVerusEvidenceV1",
        "result": result,
        "candidate_tree": candidate_tree,
        "prospective_tree": prospective_tree,
        "specifications": specifications,
        "proof_closure_sha256": manifest["_closure_digest"],
        "manifest_sha256": sha256_file(inside(repository, policy["manifest"])),
        "contract_sha256": sha256_file(inside(contract_root, contract_path)),
        "engine_sha256": engine_digest(),
        "toolchain_lock_sha256": sha256_file(inside(contract_root, policy["toolchain_lock"])),
        "toolchain_revision": toolchain["verus"]["revision"],
        "toolchain_execution": toolchain_execution,
        "production_features": policy["production_features"],
        "target": policy["target"],
        "trusted_computing_base": manifest["_trusted_computing_base"],
        "trusted_assumptions": sorted(set(assumptions)),
        "components": manifest["_component_ids"],
        "theorem_roots": theorem_roots,
        "command": command,
        "exit_code": exit_code,
        "log": log,
        "artifacts": [],
    }


def validate_artifact_receipt(value: Any, maximum_bytes: int, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes", "media_type"}:
        raise InfrastructureError(f"{field} is not a canonical artifact receipt")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("sha256", ""))) is None:
        raise InfrastructureError(f"{field} has an invalid digest")
    size = value.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= maximum_bytes:
        raise InfrastructureError(f"{field} exceeds the bounded artifact policy")
    if not isinstance(value.get("media_type"), str) or not value["media_type"]:
        raise InfrastructureError(f"{field} has no media type")


def validate_generated_receipt(
    receipt: dict[str, Any], policy: dict[str, Any], state: dict[str, Any]
) -> None:
    expected = {
        "schema",
        "result",
        "candidate_tree",
        "prospective_tree",
        "specifications",
        "proof_closure_sha256",
        "manifest_sha256",
        "contract_sha256",
        "engine_sha256",
        "toolchain_lock_sha256",
        "toolchain_revision",
        "toolchain_execution",
        "production_features",
        "target",
        "trusted_computing_base",
        "trusted_assumptions",
        "components",
        "theorem_roots",
        "command",
        "exit_code",
        "log",
        "artifacts",
    }
    if set(receipt) != expected or receipt.get("schema") != "OxideVerusEvidenceV1":
        raise InfrastructureError("generated proof receipt has the wrong schema closure")
    if receipt.get("result") not in {"passed", "product_failure", "infrastructure_failure"}:
        raise InfrastructureError("generated proof receipt has an invalid result")
    for field in ("candidate_tree", "prospective_tree"):
        exact_tree(receipt.get(field), field)
    sha_fields = (
        "proof_closure_sha256",
        "manifest_sha256",
        "contract_sha256",
        "engine_sha256",
        "toolchain_lock_sha256",
    )
    if any(
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get(field, ""))) is None
        for field in sha_fields
    ):
        raise InfrastructureError("generated proof receipt has an invalid context digest")
    specifications = receipt.get("specifications")
    if (
        not isinstance(specifications, dict)
        or not {policy["product_spec"], policy["verification_spec"]}.issubset(specifications)
        or any(
            re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)) is None
            for value in specifications.values()
        )
    ):
        raise InfrastructureError("generated proof receipt has an invalid specification closure")
    execution = receipt.get("toolchain_execution")
    execution_fields = {
        "host",
        "asset",
        "asset_sha256",
        "verus_version",
        "verus_revision",
        "rust_toolchain",
    }
    if not isinstance(execution, dict) or set(execution) != execution_fields:
        raise InfrastructureError("generated proof receipt lacks exact toolchain execution")
    if (
        execution.get("host") != policy["target"]
        or execution.get("verus_revision") != state["toolchain"]["verus"]["revision"]
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(execution.get("asset_sha256", ""))) is None
    ):
        raise InfrastructureError(
            "generated proof receipt toolchain differs from the frozen contract"
        )
    list_fields = ("trusted_computing_base", "trusted_assumptions", "components", "theorem_roots")
    for field in list_fields:
        value = receipt.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise InfrastructureError(f"generated proof receipt {field} is malformed")
        if len(value) != len(set(value)):
            raise InfrastructureError(f"generated proof receipt {field} contains duplicates")
    if (
        receipt["trusted_computing_base"] != state["manifest"]["_trusted_computing_base"]
        or receipt["trusted_assumptions"] != sorted(state["manifest"]["_assumption_ids"])
        or receipt["components"] != state["manifest"]["_component_ids"]
        or not receipt["components"]
        or not receipt["theorem_roots"]
    ):
        raise InfrastructureError("generated proof receipt differs from manifest coverage")
    if (
        receipt.get("production_features") != policy["production_features"]
        or receipt.get("target") != policy["target"]
    ):
        raise InfrastructureError("generated proof receipt differs from production configuration")
    command = receipt.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise InfrastructureError("generated proof receipt command is malformed")
    if not isinstance(receipt.get("exit_code"), int) or isinstance(receipt["exit_code"], bool):
        raise InfrastructureError("generated proof receipt exit code is malformed")
    maximum = int(policy["evidence"]["max_artifact_bytes"])
    validate_artifact_receipt(receipt.get("log"), maximum, "proof log")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > int(policy["evidence"]["max_artifacts"]):
        raise InfrastructureError("generated proof receipt artifacts exceed policy")
    for index, artifact in enumerate(artifacts):
        validate_artifact_receipt(artifact, maximum, f"proof artifact {index}")


def _verus_command(policy: dict[str, Any], verus: Path, *, root: Path | None = None) -> list[str]:
    command = [str(verus), str(root or policy["production_entry"]), "--crate-type=lib"]
    if root is None:
        command.extend(
            [
                "--verify-module",
                str(policy["composition_module"]),
                "--verify-function",
                str(policy["composition_theorem"]),
            ]
        )
    command.extend(
        [
            "--no-cheating",
            "--rlimit",
            str(policy["solver_rlimit"]),
            "--smt-option",
            "smt.random_seed=0",
        ]
    )
    return command


def run_composition(args: argparse.Namespace, repository: Path, contract_root: Path) -> int:
    actual = git_tree(repository)
    candidate = exact_tree(args.candidate_tree or actual, "candidate_tree")
    prospective = exact_tree(args.prospective_tree or actual, "prospective_tree")
    policy: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    command: list[str] = []
    exit_code = 2
    result = "infrastructure_failure"
    log_bytes = b""
    toolchain_execution: dict[str, Any] = {}
    try:
        require_exact_checkout(repository, prospective)
        policy, state, _ = validate_policy(repository, contract_root, args.contract)
        if state["manifest"]["status"] != "implemented":
            raise VerificationError(
                "composition is unavailable: the target program is unimplemented"
            )
        composition_root = inside(repository, policy["composition_root"])
        if not composition_root.is_file():
            raise VerificationError("program composition proof root is missing")
        maximum = policy["evidence"]["max_log_bytes"]
        timeout = state["toolchain"]["verification"]["timeout_seconds"]
        with pinned_verus(state["toolchain"]) as (verus, environment, details):
            toolchain_execution = details
            if details["host"] != policy["target"]:
                raise InfrastructureError(
                    f"verification host {details['host']} differs from the frozen target {policy['target']}"
                )
            command = _verus_command(policy, verus)
            with tempfile.TemporaryFile() as log_file:
                try:
                    completed = subprocess.run(
                        command,
                        cwd=repository,
                        env=environment,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    raise InfrastructureError(f"Verus timed out after {timeout}s") from error
                size = log_file.tell()
                log_file.seek(0)
                if size > maximum:
                    raise InfrastructureError(
                        f"proof log exceeds bounded receipt limit: {size} > {maximum}"
                    )
                log_bytes = log_file.read()
        exit_code = completed.returncode
        result = (
            "passed"
            if exit_code == 0
            else "product_failure"
            if exit_code == 1
            else "infrastructure_failure"
        )
    except VerificationError as error:
        log_bytes = f"verification policy failure: {error}\n".encode()
        exit_code = 1
        result = "product_failure"
    except InfrastructureError as error:
        log_bytes = f"verification infrastructure failure: {error}\n".encode()
        exit_code = 2
        result = "infrastructure_failure"

    if args.receipt:
        if policy is None or state is None or not toolchain_execution:
            bounded = log_bytes[:65536]
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(
                json.dumps(
                    {
                        "schema": "OxideVerusEvidenceFailureV1",
                        "result": result,
                        "candidate_tree": candidate,
                        "prospective_tree": prospective,
                        "exit_code": exit_code,
                        "detail": bounded.decode("utf-8", errors="replace"),
                        "detail_sha256": sha256_bytes(log_bytes),
                        "detail_bytes": len(log_bytes),
                        "detail_truncated": len(log_bytes) > len(bounded),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            artifact_dir = args.artifact_dir or args.receipt.parent / "artifacts"
            log = write_artifact(log_bytes, artifact_dir, ".log")
            receipt = proof_receipt(
                repository,
                contract_root,
                policy,
                state,
                candidate,
                prospective,
                command,
                exit_code,
                result,
                log,
                toolchain_execution,
                args.contract,
            )
            validate_generated_receipt(receipt, policy, state)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    sys.stdout.buffer.write(log_bytes)
    return 0 if result == "passed" else exit_code


def run_gate(args: argparse.Namespace, repository: Path, contract_root: Path) -> int:
    actual = git_tree(repository)
    prospective = exact_tree(args.prospective_tree or actual, "prospective_tree")
    candidate = exact_tree(args.candidate_tree or actual, "candidate_tree")
    require_exact_checkout(repository, prospective)
    policy, state, _ = validate_policy(repository, contract_root, args.contract)
    if state["manifest"]["status"] == "implemented":
        return run_composition(args, repository, contract_root)
    receipt = {
        "schema": "OxideVerificationFoundationEvidenceV1",
        "result": "passed",
        "scope": "verification-foundation",
        "program_status": "unimplemented",
        "candidate_tree": candidate,
        "prospective_tree": prospective,
        "proof_closure_sha256": state["manifest"]["_closure_digest"],
        "manifest_sha256": sha256_file(inside(repository, policy["manifest"])),
        "contract_sha256": sha256_file(inside(contract_root, args.contract)),
        "engine_sha256": engine_digest(),
        "toolchain_lock_sha256": sha256_file(inside(contract_root, policy["toolchain_lock"])),
        "target": policy["target"],
        "features": policy["production_features"],
        "formal_verification_claim": False,
    }
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(receipt, sort_keys=True))
    return 0


def run_proof(args: argparse.Namespace, repository: Path, contract_root: Path) -> int:
    actual = git_tree(repository)
    prospective = exact_tree(args.prospective_tree or actual, "prospective_tree")
    require_exact_checkout(repository, prospective)
    policy, state, _ = validate_policy(repository, contract_root, args.contract)
    root = inside(repository, args.root)
    allowed_roots = [
        inside(repository, value)
        for field in ("contract_roots", "abstract_model_roots", "proof_roots")
        for value in policy[field]
    ]
    if not root.is_file() or not any(root.is_relative_to(value) for value in allowed_roots):
        raise VerificationError("proof root is absent or outside the declared verification roots")
    timeout = int(state["toolchain"]["verification"]["timeout_seconds"])
    with pinned_verus(state["toolchain"]) as (verus, environment, details):
        if details["host"] != policy["target"]:
            raise InfrastructureError(
                f"verification host {details['host']} differs from the frozen target {policy['target']}"
            )
        command = _verus_command(policy, verus, root=root)
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                env=environment,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise InfrastructureError(f"Verus timed out after {timeout}s") from error
    if completed.returncode == 0:
        return 0
    if completed.returncode == 1:
        raise VerificationError(f"proof root failed: {args.root}")
    raise InfrastructureError(
        f"Verus infrastructure exited {completed.returncode} for proof root {args.root}"
    )


def run_toolchain(contract_root: Path, contract_path: str = _DEFAULT_CONTRACT_PATH) -> int:
    policy = read_toml(inside(contract_root, contract_path))
    lock_path = policy.get("toolchain_lock")
    if not isinstance(lock_path, str) or not lock_path:
        raise VerificationError("policy.toolchain_lock must be nonempty")
    lock = read_toml(inside(contract_root, lock_path))
    with pinned_verus(lock) as (verus, environment, details):
        target = policy.get("target")
        if target != details["host"]:
            raise InfrastructureError(
                f"verification host {details['host']} differs from the frozen target {target}"
            )
        completed = subprocess.run(
            [str(verus), "--version", "--output-json"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    if completed.returncode != 0:
        raise InfrastructureError(
            completed.stderr.strip() or "pinned Verus failed its version probe"
        )
    try:
        reported = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise InfrastructureError("pinned Verus emitted malformed version metadata") from error
    if reported.get("verus", {}).get("commit") != lock.get("verus", {}).get("revision"):
        raise InfrastructureError("executed Verus revision differs from the lock")
    print(json.dumps({"status": "qualified", **details}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument(
        "--contract-root",
        type=Path,
        default=Path(os.environ.get("OXIDE_FROZEN_CONTRACT_ROOT", Path.cwd())),
        help="independently frozen target contract closure",
    )
    parser.add_argument(
        "--contract",
        default=_DEFAULT_CONTRACT_PATH,
        help="target-relative verification contract path",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("toolchain", help="qualify the exact pinned Verus release")
    subparsers.add_parser("policy", help="validate classifications and deterministic policy")
    proof = subparsers.add_parser("proof", help="verify one declared foundation proof root")
    proof.add_argument("--root", required=True)
    proof.add_argument("--prospective-tree")
    for operation, help_text in (
        ("composition", "run exact-tree Verus composition"),
        ("gate", "run policy and, when production exists, exact-tree composition"),
    ):
        gate = subparsers.add_parser(operation, help=help_text)
        gate.add_argument("--candidate-tree")
        gate.add_argument("--prospective-tree")
        gate.add_argument("--receipt", type=Path)
        gate.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    repository = args.repository.resolve()
    contract_root = args.contract_root.resolve()
    try:
        if args.operation == "toolchain":
            return run_toolchain(contract_root, args.contract)
        if args.operation == "policy":
            policy, state, _ = validate_policy(repository, contract_root, args.contract)
            print(
                json.dumps(
                    {
                        "status": state["manifest"]["status"],
                        "components": len(state["manifest"]["_component_ids"]),
                        "trusted_adapters": len(state["manifest"]["_adapter_ids"]),
                        "proof_closure_sha256": state["manifest"]["_closure_digest"],
                        "target": policy["target"],
                        "features": policy["production_features"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.operation == "proof":
            return run_proof(args, repository, contract_root)
        if args.operation == "gate":
            return run_gate(args, repository, contract_root)
        return run_composition(args, repository, contract_root)
    except VerificationError as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    except InfrastructureError as error:
        print(f"verification infrastructure failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
