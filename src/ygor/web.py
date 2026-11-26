"""Local, read-only experiment explorer. Uses only Python's standard library.

Large manifests are never loaded just to list runs. Execution exports are indexed
on demand in temporary SQLite databases, not in the experiment store.
"""
from __future__ import annotations

import csv
import ipaddress
import json
import math
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote, urlsplit

ASSETS = Path(__file__).with_name("web_assets")
PREVIEW_BYTES = 512 * 1024
ROOT_FILES = {"recipe.lock.yaml", "manifest.json", "executions.csv", "executions.jsonl"}
ROOT_DIRS = {"outputs", "streams", "logs"}
SAFE_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class WebError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _signature(path: Path) -> tuple[int, int, int]:
    s = path.stat()
    return s.st_mtime_ns, s.st_size, s.st_ino


def _metadata(path: Path) -> dict[str, Any]:
    if path.stat().st_size <= 2 * 1024 * 1024:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Manifest is not an object")
        data.pop("executions", None)
        return data
    # ygor's manifest writer places metadata and state before executions. Decode
    # top-level values, not a regex over YAML/JSON strings or the execution array.
    with path.open(encoding="utf-8") as fh:
        prefix = fh.read(2 * 1024 * 1024)
    decoder, pos, data = json.JSONDecoder(), 0, {}
    pos = len(prefix) - len(prefix.lstrip())
    if prefix[pos:pos + 1] != "{":
        raise ValueError("Invalid manifest")
    pos += 1
    while True:
        while pos < len(prefix) and prefix[pos] in " \r\n\t,":
            pos += 1
        key, pos = decoder.raw_decode(prefix, pos)
        pos = prefix.index(":", pos) + 1
        while prefix[pos].isspace():
            pos += 1
        if key == "executions":
            if "state" not in data:
                raise ValueError("Large manifest must have metadata before executions")
            return data
        data[key], pos = decoder.raw_decode(prefix, pos)


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    try:
        v = float(value)
        return int(v) if integer and math.isfinite(v) else v if math.isfinite(v) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _scalar(value: str) -> Any:
    try:
        decoded = json.loads(value)
        if isinstance(decoded, float) and not math.isfinite(decoded):
            return value
        if isinstance(decoded, (dict, list)):
            json.dumps(decoded, allow_nan=False)
        return decoded
    except (ValueError, TypeError):
        return value


class Explorer:
    def __init__(self, store: Path):
        self.store = store.resolve()
        self.runs = self.store / "runs"
        self._temp = tempfile.TemporaryDirectory(prefix="ygor-web-")
        self._cache: dict[str, tuple[tuple, dict]] = {}
        self._indexes: OrderedDict[str, tuple[tuple, Path]] = OrderedDict()
        self._lock = threading.RLock()

    def close(self) -> None:
        self._temp.cleanup()

    def resolve_run(self, identifier: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise WebError("Identificador de run inválido.")
        candidates = [p for p in self.runs.glob(identifier + "*")
                      if p.is_dir() and (p / "manifest.json").is_file()
                      and p.resolve().parent == self.runs.resolve()]
        if not candidates:
            raise WebError("Run não encontrado.", 404)
        exact = [p for p in candidates if p.name == identifier]
        if len(candidates) > 1 and not exact:
            raise WebError("Prefixo ambíguo. Use mais caracteres do hash.", 409)
        return (exact or candidates)[0]

    def metadata(self, run: Path) -> dict:
        path = self.safe_file(run, "manifest.json")
        sig = _signature(path)
        with self._lock:
            if run.name not in self._cache or self._cache[run.name][0] != sig:
                data = _metadata(path)
                if not isinstance(data, dict):
                    raise ValueError("Manifest is not an object")
                self._cache[run.name] = (sig, data)
            return self._cache[run.name][1]

    def list_runs(self) -> dict:
        items, errors = [], []
        if self.runs.is_dir():
            for run in self.runs.iterdir():
                if not run.is_dir() or not (run / "manifest.json").is_file():
                    continue
                try:
                    if run.resolve().parent != self.runs.resolve():
                        continue
                    m = self.metadata(run)
                    recipe = m.get("recipe") or {}
                    items.append({"id": run.name, "short_id": run.name[:8],
                                  "name": (recipe.get("meta") or {}).get("name") or Path(m.get("recipe_path") or run.name).stem,
                                  "description": (recipe.get("meta") or {}).get("description") or "",
                                  "recipe_path": m.get("recipe_path"), "state": m.get("state") or {},
                                  "host": (m.get("host") or {}).get("hostname"),
                                  "seeds": (m.get("seeds") or {}).get("count"),
                                  "parameters": (recipe.get("parameters") or {}).get("values") or {}})
                except (OSError, ValueError, TypeError, KeyError, WebError) as exc:
                    errors.append({"id": run.name, "message": f"Manifesto indisponível: {type(exc).__name__}"})
        items.sort(key=lambda r: r["state"].get("started_at") or "", reverse=True)
        return {"store": str(self.store), "runs": items, "errors": errors, "read_only": True}

    def _records(self, run: Path) -> Iterator[dict]:
        if (run / "executions.csv").is_file():
            with self.safe_file(run, "executions.csv").open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    row["params"] = {k[6:]: _scalar(v) for k, v in row.items()
                                     if isinstance(k, str) and k.startswith("param.")}
                    yield row
        elif (run / "executions.jsonl").is_file():
            with self.safe_file(run, "executions.jsonl").open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)
        else:
            if (run / "manifest.json").stat().st_size > 32 * 1024 * 1024:
                raise WebError("Este run não possui exportação CSV/JSONL. Exporte as execuções pelo Ygor antes de explorá-las.", 422)
            with self.safe_file(run, "manifest.json").open(encoding="utf-8") as fh:
                yield from json.load(fh).get("executions", [])

    def _index(self, run: Path) -> Path:
        source = next((run / n for n in ("executions.csv", "executions.jsonl", "manifest.json") if (run / n).is_file()))
        signature = (str(source), *_signature(source))
        with self._lock:
            old = self._indexes.get(run.name)
            if old and old[0] == signature:
                self._indexes.move_to_end(run.name)
                return old[1]
            path = Path(self._temp.name) / (run.name + ".sqlite3")
            db = sqlite3.connect(path)
            try:
                db.execute("DROP TABLE IF EXISTS executions")
                db.execute("CREATE TABLE executions(id INTEGER PRIMARY KEY, status TEXT, duration REAL, search TEXT, data TEXT)")
                def rows():
                    for raw in self._records(run):
                        # Flattened param.* fields duplicate params and can double
                        # the index size for large sweeps. Keep one canonical copy.
                        e = {k: v for k, v in raw.items() if not k.startswith("param.")}
                        for k in ("exec_id", "seed", "exit_code", "retry_count"):
                            e[k] = _number(e.get(k), integer=True)
                        if e["exec_id"] is None:
                            continue
                        e["duration_sec"] = _number(e.get("duration_sec"))
                        e["params"] = e.get("params") or {}
                        e["instance"] = Path(str(e["params"].get("instance", ""))).name
                        e.setdefault("log_path", f"logs/exec_{e['exec_id']:04d}.log")
                        search = " ".join(str(v) for v in (e["exec_id"], e["seed"], e.get("failure_reason"), *e["params"].values()))
                        yield e["exec_id"], e.get("status") or "pending", e["duration_sec"], search, json.dumps(e, ensure_ascii=False)
                db.executemany("INSERT INTO executions VALUES(?,?,?,?,?)", rows())
                db.execute("CREATE INDEX execution_status ON executions(status)")
                db.commit()
            finally:
                db.close()
            self._indexes[run.name] = (signature, path)
            self._indexes.move_to_end(run.name)
            # Limit disk and memory use. Never write a cache into the user's store.
            while len(self._indexes) > 4:
                _, (_, expired) = self._indexes.popitem(last=False)
                expired.unlink(missing_ok=True)
            return path

    def executions(self, run: Path, *, query: str = "", status: str = "", offset: int = 0,
                   limit: int = 50, sort: str = "id", descending: bool = False) -> dict:
        if not 1 <= limit <= 200 or offset < 0 or offset > 10_000_000:
            raise WebError("Paginação inválida.")
        if sort not in ("id", "duration"):
            raise WebError("Ordenação inválida.")
        where, args = [], []
        if query:
            where.append("search LIKE ? ESCAPE '\\'")
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if status:
            where.append("status=?")
            args.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock, closing(sqlite3.connect(self._index(run))) as db:
            count = db.execute("SELECT count(*) FROM executions" + clause, args).fetchone()[0]
            rows = db.execute(f"SELECT data FROM executions{clause} ORDER BY {sort} {'DESC' if descending else 'ASC'}, id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"items": [json.loads(r[0]) for r in rows], "total": count, "offset": offset, "limit": limit}

    def execution(self, run: Path, exec_id: int) -> dict:
        with self._lock, closing(sqlite3.connect(self._index(run))) as db:
            row = db.execute("SELECT data FROM executions WHERE id=?", (exec_id,)).fetchone()
        if not row:
            raise WebError("Execução não encontrada.", 404)
        return json.loads(row[0])

    def snapshot(self, run: Path) -> dict:
        """Cheap metadata for recipes/files; does not build an execution index."""
        m = self.metadata(run)
        return {"id": run.name, "metadata": m,
                "counts": (m.get("state") or {}).get("counts") or {}, "has_statistics": False}

    def detail(self, run: Path) -> dict:
        m = self.metadata(run)
        with self._lock, closing(sqlite3.connect(self._index(run))) as db:
            counts = dict(db.execute("SELECT status,count(*) FROM executions GROUP BY status"))
            durations = db.execute("SELECT min(duration),avg(duration),max(duration),sum(duration) FROM executions WHERE status='completed'").fetchone()
            failures = db.execute("SELECT json_extract(data,'$.failure_reason'),count(*) FROM executions WHERE status='failed' GROUP BY 1 ORDER BY 2 DESC").fetchall()
            maximum = durations[2] or 0
            buckets = dict(db.execute("SELECT min(23,cast(duration / ? * 24 AS INTEGER)),count(*) FROM executions WHERE status='completed' AND duration>=0 GROUP BY 1", (maximum or 1,)))
        return {"id": run.name, "metadata": m, "counts": counts, "has_statistics": True,
                "durations": {"min": durations[0], "mean": durations[1], "max": maximum, "total": durations[3],
                              "buckets": [buckets.get(i, 0) for i in range(24)]},
                "failures": [{"reason": r or "Não informado", "count": n} for r, n in failures]}

    def safe_file(self, run: Path, value: str, *, directory: bool = False) -> Path:
        rel = Path(value)
        if rel.is_absolute() or any(part in ("..", ".") or part.startswith(".") for part in rel.parts):
            raise WebError("Caminho não permitido.", 403)
        if value and (not rel.parts or rel.parts[0] not in ROOT_DIRS | ROOT_FILES):
            raise WebError("Arquivo fora das saídas do run.", 403)
        target = (run / rel).resolve()
        if not target.is_relative_to(run.resolve()) or not target.exists():
            raise WebError("Arquivo não encontrado.", 404)
        resolved_parts = target.relative_to(run.resolve()).parts
        if any(p.startswith(".") for p in resolved_parts) or (resolved_parts and resolved_parts[0] not in ROOT_DIRS | ROOT_FILES):
            raise WebError("Caminho não permitido.", 403)
        if not directory and not target.is_file():
            raise WebError("Arquivo não encontrado.", 404)
        return target

    def files(self, run: Path, value: str = "", *, offset: int = 0, limit: int = 100) -> dict:
        if offset < 0 or offset > 10_000_000 or not 1 <= limit <= 200:
            raise WebError("Paginação inválida.")
        directory = self.safe_file(run, value, directory=True)
        if not directory.is_dir():
            raise WebError("Pasta não encontrada.", 404)
        entries = sorted((p for p in directory.iterdir() if not p.name.startswith(".")
                          and (value or p.name in ROOT_FILES | ROOT_DIRS)
                          and (not p.is_symlink() or p.resolve().is_relative_to(run.resolve()))), key=lambda p: p.name)
        result = []
        for p in entries[offset:offset + limit]:
            try:
                result.append({"name": p.name, "path": str(p.relative_to(run)), "directory": p.is_dir(),
                               "size": p.stat().st_size if p.is_file() else None})
            except OSError:
                continue
        return {"path": value, "items": result, "total": len(entries), "offset": offset, "limit": limit}

    def preview(self, run: Path, value: str, *, tail: bool = False) -> dict:
        path = self.safe_file(run, value)
        size = path.stat().st_size
        with path.open("rb") as fh:
            if tail and size > PREVIEW_BYTES:
                fh.seek(size - PREVIEW_BYTES)
            data = fh.read(PREVIEW_BYTES)
        text = data.decode("utf-8", errors="replace")
        structured = None
        if size and size <= PREVIEW_BYTES:
            try:
                structured = json.loads(text)
                text = json.dumps(structured, ensure_ascii=False, indent=2)
            except ValueError:
                pass
        return {"path": value, "name": path.name, "size": size, "text": text,
                "truncated": size > PREVIEW_BYTES, "tail": tail, "empty": size == 0,
                "json": structured, "image": path.suffix.lower() in SAFE_IMAGES,
                "binary": b"\x00" in data}


class ExplorerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], explorer: Explorer):
        self.explorer = explorer
        super().__init__(address, ExplorerHandler)

    def server_close(self) -> None:
        super().server_close()
        self.explorer.close()


class ExplorerHandler(BaseHTTPRequestHandler):
    server: ExplorerServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _headers(self, status: int, content_type: str, size: int, **extra: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in extra.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _file(self, path: Path, *, download: bool = False) -> None:
        content_type = (mimetypes.guess_type(path.name)[0] or "application/octet-stream") if download or path.suffix.lower() in SAFE_IMAGES else "text/plain; charset=utf-8"
        extra = {"Content_Disposition": "attachment; filename*=UTF-8''" + quote(path.name)} if download else {}
        with path.open("rb") as fh:
            self._headers(200, content_type, path.stat().st_size, **extra)
            shutil.copyfileobj(fh, self.wfile, length=64 * 1024)

    def do_POST(self) -> None:
        self._json({"error": "O Explorer é somente leitura."}, 405)

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self) -> None:
        try:
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            try:
                local_host = host == "localhost" or ipaddress.ip_address(host or "").is_loopback
            except ValueError:
                local_host = False
            if not local_host:
                raise WebError("Este serviço aceita somente acesso local.", 403)
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise WebError("Origem não permitida.", 403)
            url = urlsplit(self.path)
            args = {k: v[-1] for k, v in parse_qs(url.query).items()}
            parts = unquote(url.path).strip("/").split("/")
            explorer = self.server.explorer
            if url.path == "/api/runs":
                self._json(explorer.list_runs())
            elif len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                run = explorer.resolve_run(parts[2])
                endpoint = parts[3] if len(parts) > 3 else "detail"
                if len(parts) > 5 or (len(parts) == 5 and endpoint != "executions"):
                    raise WebError("Página não encontrada.", 404)
                if endpoint == "detail":
                    self._json(explorer.detail(run))
                elif endpoint == "metadata":
                    self._json(explorer.snapshot(run))
                elif endpoint == "statistics":
                    self._json(explorer.detail(run))
                elif endpoint == "executions" and len(parts) == 5:
                    self._json(explorer.execution(run, int(parts[4])))
                elif endpoint == "executions":
                    self._json(explorer.executions(run, query=args.get("q", "")[:512], status=args.get("status", ""),
                               offset=int(args.get("offset", "0")), limit=int(args.get("limit", "50")),
                               sort=args.get("sort", "id"), descending=args.get("desc") == "1"))
                elif endpoint == "recipe":
                    self._json(explorer.preview(run, "recipe.lock.yaml"))
                elif endpoint == "files":
                    self._json(explorer.files(run, args.get("path", ""), offset=int(args.get("offset", "0")), limit=int(args.get("limit", "100"))))
                elif endpoint == "preview":
                    self._json(explorer.preview(run, args.get("path", ""), tail=args.get("tail") == "1"))
                elif endpoint in ("download", "image"):
                    path = explorer.safe_file(run, args.get("path", ""))
                    if endpoint == "image" and path.suffix.lower() not in SAFE_IMAGES:
                        raise WebError("Formato de imagem não permitido.", 403)
                    self._file(path, download=endpoint == "download")
                else:
                    raise WebError("Página não encontrada.", 404)
            elif url.path in ("/", "/app.js", "/style.css", "/favicon.svg"):
                path = ASSETS / ("index.html" if url.path == "/" else url.path[1:])
                data = path.read_bytes()
                mime = {".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[path.suffix]
                self._headers(200, mime + "; charset=utf-8", len(data))
                self.wfile.write(data)
            else:
                raise WebError("Página não encontrada.", 404)
        except WebError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (FileNotFoundError, NotADirectoryError):
            self._json({"error": "Arquivo indisponível. O run pode ter sido atualizado."}, 404)
        except (ValueError, TypeError, KeyError, StopIteration, sqlite3.Error):
            self._json({"error": "Dados indisponíveis ou parâmetros inválidos."}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self._json({"error": "Não foi possível ler os dados deste run."}, 500)


def create_server(store: Path, host: str = "127.0.0.1", port: int = 8765) -> ExplorerServer:
    if host != "localhost":
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local or ":" in host:
            raise ValueError("Use localhost ou um endereço IPv4 de loopback.")
    explorer = Explorer(store)
    try:
        return ExplorerServer((host, port), explorer)
    except Exception:
        explorer.close()
        raise
