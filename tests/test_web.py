"""Read-only explorer, HTTP boundaries, incremental metadata and cache tests."""
import csv
import hashlib
import json
import io
from types import SimpleNamespace
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ygor.cli import app
from ygor.web import Explorer, ExplorerHandler, WebError, create_server, PREVIEW_BYTES


@pytest.fixture
def store(tmp_path):
    store = tmp_path / "store"
    run = store / "runs" / ("a" * 64)
    run.mkdir(parents=True)
    manifest = {
        "run_hash": run.name, "tool_version": "ygor test", "recipe_path": "example.yaml",
        "recipe": {"meta": {"name": "Example <script>alert(1)</script>"},
                   "parameters": {"values": {"alpha": [1, 2]}}},
        "state": {"status": "failed", "started_at": "2026-01-01T00:00:00+00:00",
                  "total_executions": 3, "counts": {"completed": 2, "failed": 1}},
        "host": {"hostname": "test"},
        "executions": [
            {"exec_id": 1, "status": "completed", "params": {"instance": "/dataset/graph.mtx", "alpha": 1},
             "duration_sec": 1.5, "seed": 42, "exit_code": 0, "output_dir": "outputs/exec_0001"},
            {"exec_id": 2, "status": "failed", "params": {"instance": "/dataset/other.mtx", "alpha": 2},
             "duration_sec": 300, "seed": 43, "exit_code": -1, "failure_reason": "timeout"},
            {"exec_id": 3, "status": "completed", "params": {"instance": "/dataset/100%.mtx", "alpha": 1},
             "duration_sec": 3.5, "seed": 44, "exit_code": 0},
        ],
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "recipe.lock.yaml").write_text('meta:\n  name: frozen\ncommand: "echo frozen"\n')
    for root in ("outputs/exec_0001", "streams/exec_0001", "logs"):
        (run / root).mkdir(parents=True, exist_ok=True)
    (run / "outputs/exec_0001/result.json").write_text(json.dumps({"value": 7, "completed": False}))
    (run / "outputs/exec_0001/empty.json").touch()
    (run / "streams/exec_0001/stdout.log").write_text("hello\n")
    (run / "streams/exec_0001/stderr.log").write_text("warning\n")
    (run / "logs/exec_0001.log").write_text("log\n")
    (store / ".ygorrc.yaml").write_text("secret: do not serve")
    return store


@pytest.fixture
def explorer(store):
    obj = Explorer(store)
    yield obj
    obj.close()


def run_dir(store):
    return store / "runs" / ("a" * 64)


def export_csv(run, entries):
    fields = ["exec_id", "status", "duration_sec", "seed", "exit_code", "failure_reason", "param.instance", "param.alpha"]
    with (run / "executions.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for e in entries:
            row = {k: e.get(k) for k in fields if not k.startswith("param.")}
            row.update({"param."+k: v for k,v in e["params"].items()})
            writer.writerow(row)


def test_empty_store(tmp_path):
    ex = Explorer(tmp_path / "absent")
    try:
        assert ex.list_runs()["runs"] == []
        assert not (tmp_path / "absent").exists()
    finally:
        ex.close()


def test_list_and_metadata(explorer, store):
    result = explorer.list_runs()
    assert len(result["runs"]) == 1
    assert result["read_only"] is True
    assert result["runs"][0]["state"]["total_executions"] == 3
    assert "executions" not in explorer.metadata(run_dir(store))


def test_compact_manifest_executions_before_state(explorer, store):
    p = run_dir(store) / "manifest.json"
    p.write_text(json.dumps({"executions": [], "state": {"status": "completed"}}))
    assert explorer.metadata(p.parent)["state"]["status"] == "completed"


def test_large_manifest_does_not_parse_execution_array(explorer, store):
    p = run_dir(store) / "manifest.json"
    header = {"recipe": {"command": 'echo "executions": [not a key]'}, "state": {"total_executions": 100000}}
    # Deliberately invalid trailing execution array: metadata reading must stop first.
    prefix = json.dumps(header)[:-1] + ', "executions": ['
    p.write_text(prefix + " " * (3 * 1024 * 1024))
    assert explorer.metadata(p.parent)["state"]["total_executions"] == 100000


def test_invalid_manifest_is_visible_as_warning(explorer, store):
    (run_dir(store) / "manifest.json").write_text("{broken")
    assert not explorer.list_runs()["runs"]
    assert len(explorer.list_runs()["errors"]) == 1


def test_resolve_run_prefix(explorer, store):
    assert explorer.resolve_run("aaaa") == run_dir(store)
    with pytest.raises(WebError, match="não encontrado"):
        explorer.resolve_run("bbbb")
    with pytest.raises(WebError):
        explorer.resolve_run("../")


def test_ambiguous_prefix(explorer, store):
    second = store / "runs" / ("a" * 63 + "b")
    second.mkdir()
    (second / "manifest.json").write_text("{}")
    with pytest.raises(WebError) as error:
        explorer.resolve_run("aaa")
    assert error.value.status == 409
    assert explorer.resolve_run("a" * 64) == run_dir(store)


def test_execution_filters_sort_and_pagination(explorer, store):
    run = run_dir(store)
    assert explorer.executions(run)["total"] == 3
    assert explorer.executions(run, query="graph.mtx")["total"] == 1
    assert explorer.executions(run, query="42")["items"][0]["seed"] == 42
    assert explorer.executions(run, status="failed")["items"][0]["exec_id"] == 2
    assert explorer.executions(run, sort="duration", descending=True)["items"][0]["exec_id"] == 2
    assert explorer.executions(run, limit=1, offset=1)["items"][0]["exec_id"] == 2
    assert explorer.executions(run, query="%")["total"] == 1
    assert explorer.executions(run, query="' OR 1=1 --")["total"] == 0


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"sort": "DROP TABLE executions"}])
def test_execution_query_validation(explorer, store, args):
    with pytest.raises(WebError):
        explorer.executions(run_dir(store), **args)


def test_detail_stats_distinguish_process_and_output(explorer, store):
    d = explorer.detail(run_dir(store))
    assert d["counts"] == {"completed": 2, "failed": 1}
    assert d["durations"]["mean"] == 2.5
    assert sum(d["durations"]["buckets"]) == 2
    assert d["failures"] == [{"reason": "timeout", "count": 1}]
    preview = explorer.preview(run_dir(store), "outputs/exec_0001/result.json")
    assert preview["json"]["completed"] is False


def test_snapshot_does_not_build_index(explorer, store, monkeypatch):
    def unexpected(*args):
        pytest.fail("Opening recipes, metadata or files must not index executions")
    monkeypatch.setattr(explorer, "_index", unexpected)
    snapshot = explorer.snapshot(run_dir(store))
    assert snapshot["has_statistics"] is False
    assert snapshot["counts"]["completed"] == 2


def test_csv_export_and_cache_refresh(explorer, store):
    run = run_dir(store)
    entries = json.loads((run / "manifest.json").read_text())["executions"]
    export_csv(run, entries)
    assert explorer.executions(run)["total"] == 3
    export_csv(run, entries[:1])
    assert explorer.executions(run)["total"] == 1
    assert explorer.execution(run, 1)["params"]["alpha"] == 1
    with pytest.raises(WebError):
        explorer.execution(run, 9)


def test_jsonl_export(explorer, store):
    run = run_dir(store)
    entries = json.loads((run / "manifest.json").read_text())["executions"]
    (run / "executions.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    assert explorer.executions(run)["total"] == 3


def test_frozen_recipe(explorer, store):
    assert "frozen" in explorer.preview(run_dir(store), "recipe.lock.yaml")["text"]


def test_files_root_is_allowlisted(explorer, store):
    run = run_dir(store)
    (run / "secret.txt").write_text("secret")
    (run / ".experiment").mkdir()
    assert "secret.txt" not in {i["name"] for i in explorer.files(run)["items"]}
    assert ".experiment" not in {i["name"] for i in explorer.files(run)["items"]}


@pytest.mark.parametrize("path", ["../.ygorrc.yaml", "/etc/passwd", "secret.txt", ".experiment/lock", "outputs/../../.ygorrc.yaml", "outputs/.env"])
def test_path_traversal_and_hidden_files_blocked(explorer, store, path):
    with pytest.raises(WebError):
        explorer.preview(run_dir(store), path)


def test_symlink_outside_run_blocked(explorer, store, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    p = run_dir(store) / "outputs/exec_0001/leak.txt"
    p.symlink_to(secret)
    with pytest.raises(WebError):
        explorer.preview(run_dir(store), "outputs/exec_0001/leak.txt")
    assert "leak.txt" not in {r["name"] for r in explorer.files(run_dir(store), "outputs/exec_0001")["items"]}


def test_symlink_to_unlisted_artifact_blocked(explorer, store):
    run = run_dir(store)
    (run / "secret.txt").write_text("private")
    (run / "outputs/leak.txt").symlink_to(run / "secret.txt")
    with pytest.raises(WebError):
        explorer.preview(run, "outputs/leak.txt")


def test_symlinked_export_blocked(explorer, store, tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("exec_id,status\n1,completed\n")
    (run_dir(store) / "executions.csv").symlink_to(outside)
    with pytest.raises(WebError):
        explorer.executions(run_dir(store))


def test_preview_empty_and_bounded(explorer, store):
    run = run_dir(store)
    assert explorer.preview(run, "outputs/exec_0001/empty.json")["empty"]
    p = run / "outputs/exec_0001/large.txt"
    p.write_text("x" * (PREVIEW_BYTES + 20) + "END")
    preview = explorer.preview(run, str(p.relative_to(run)))
    assert preview["truncated"]
    assert len(preview["text"]) == PREVIEW_BYTES
    assert explorer.preview(run, str(p.relative_to(run)), tail=True)["text"].endswith("END")


def test_preview_never_interprets_html(explorer, store):
    p = run_dir(store) / "outputs/exec_0001/output.html"
    p.write_text("<script>alert(1)</script>")
    result = explorer.preview(p.parents[2], "outputs/exec_0001/output.html")
    assert result["text"] == "<script>alert(1)</script>"
    assert not result["image"]


def test_does_not_modify_store(explorer, store):
    def snapshot():
        return {str(p.relative_to(store)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in store.rglob("*") if p.is_file()}
    before = snapshot()
    explorer.list_runs()
    explorer.detail(run_dir(store))
    explorer.executions(run_dir(store))
    explorer.files(run_dir(store))
    explorer.preview(run_dir(store), "recipe.lock.yaml")
    assert snapshot() == before


@pytest.fixture
def http(store):
    try:
        server = create_server(store, port=0)
    except PermissionError:
        pytest.skip("This sandbox does not allow listening sockets; handler tests run without sockets.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_http_assets_and_api(http):
    for path, content in [("/", b"Ygor"), ("/app.js", b"inspectExecution"), ("/style.css", b"--blue"), ("/favicon.svg", b"svg"), ("/api/runs", b"Example")]:
        with urllib.request.urlopen(http + path) as response:
            assert response.status == 200
            assert content in response.read()
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_http_execution_and_download(http):
    with urllib.request.urlopen(http + "/api/runs/aaaa/executions/1") as response:
        assert json.load(response)["exec_id"] == 1
    with urllib.request.urlopen(http + "/api/runs/aaaa/download?path=recipe.lock.yaml") as response:
        assert response.headers["Content-Disposition"].startswith("attachment")
        assert b"frozen" in response.read()


@pytest.mark.parametrize("path", ["/api/runs/aaaa/preview?path=..%2F.ygorrc.yaml", "/.ygorrc.yaml", "/api/runs/aaaa/image?path=recipe.lock.yaml"])
def test_http_paths_blocked(http, path):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(http + path)
    assert error.value.code in (403, 404)


def test_http_write_rejected(http):
    request = urllib.request.Request(http + "/api/runs", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 405


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}])
def test_dns_rebinding_and_cross_origin_blocked(http, headers):
    request = urllib.request.Request(http + "/api/runs", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 403


def test_remote_binding_rejected(store):
    with pytest.raises(ValueError):
        create_server(store, host="0.0.0.0", port=0)


def test_cli_help_and_invalid_binding(store):
    runner = CliRunner()
    result = runner.invoke(app, ["web", "--help"])
    assert result.exit_code == 0
    assert "--store" in result.output and "--port" in result.output
    assert runner.invoke(app, ["web", "--store", str(store), "--host", "0.0.0.0"]).exit_code == 1


@pytest.fixture
def request_handler(explorer):
    """Exercise the real HTTP handler without requiring a listening socket."""
    class Connection:
        def __init__(self, payload):
            self.input = io.BytesIO(payload)
            self.output = io.BytesIO()
        def makefile(self, *args, **kwargs):
            return self.input
        def sendall(self, value):
            self.output.write(value)
    def request(path, method="GET", host="127.0.0.1:8765", origin=None):
        headers = f"{method} {path} HTTP/1.0\r\nHost: {host}\r\n"
        if origin:
            headers += f"Origin: {origin}\r\n"
        connection = Connection((headers + "\r\n").encode())
        ExplorerHandler(connection, ("127.0.0.1", 10000), SimpleNamespace(explorer=explorer))
        header, _, body = connection.output.getvalue().partition(b"\r\n\r\n")
        return int(header.split()[1]), header.decode(), body
    return request


@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css", "/favicon.svg", "/api/runs", "/api/runs/aaaa", "/api/runs/aaaa/metadata", "/api/runs/aaaa/statistics", "/api/runs/aaaa/executions", "/api/runs/aaaa/executions/1", "/api/runs/aaaa/recipe", "/api/runs/aaaa/files", "/api/runs/aaaa/preview?path=outputs/exec_0001/result.json", "/api/runs/aaaa/download?path=recipe.lock.yaml"])
def test_http_handler_without_socket(request_handler, path):
    status, headers, body = request_handler(path)
    assert status == 200
    assert body
    assert "X-Content-Type-Options: nosniff" in headers
    assert "frame-ancestors 'none'" in headers


@pytest.mark.parametrize("path,kwargs,expected", [
    ("/api/runs", {"method": "POST"}, 405),
    ("/api/runs", {"method": "DELETE"}, 405),
    ("/api/runs", {"host": "evil.example"}, 403),
    ("/api/runs", {"origin": "https://evil.example"}, 403),
    ("/api/runs/aaaa/preview?path=..%2F.ygorrc.yaml", {}, 403),
    ("/.ygorrc.yaml", {}, 404),
    ("/api/runs/aaaa/executions?limit=-2", {}, 400),
    ("/api/runs/aaaa/image?path=recipe.lock.yaml", {}, 403),
    ("/api/runs/bbbb", {}, 404),
])
def test_http_handler_rejects_unsafe_requests(request_handler, path, kwargs, expected):
    status, _, body = request_handler(path, **kwargs)
    assert status == expected
    assert "error" in json.loads(body)


def test_metadata_scalar_reports_warning(explorer, store):
    (run_dir(store) / "manifest.json").write_text("42")
    assert len(explorer.list_runs()["errors"]) == 1
