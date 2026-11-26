# Ygor

<p align="center">
  <img src="assets/logo.png" alt="Ygor logo" width="280">
</p>

**YAML-Guided Orchestrator for Research**

Declarative experiment runner for reproducible computational research.

You describe an experiment in a YAML file — parameters, datasets, environment, execution policy — and `ygor` handles the combinatorial sweep, parallel workers, result storage, pause/resume, and post-processing.

## Installation

```bash
git clone https://github.com/TOCA-LAB/ygor.git
cd ygor
pip install .
```

Requires Python 3.11+.

### Migrating from xLab

The Python package and command are now named `ygor`: use `import ygor` and
`ygor run recipe.yaml`. Install this checkout again to obtain the new command.

New project configuration belongs in `.ygorrc.yaml`. Existing `.labrc.yaml`
files are also read; when both are present, matching settings in `.ygorrc.yaml`
take precedence. Both configuration filenames are ignored by Git.

Existing stores, run hashes, frozen recipes, and manifest formats remain
readable. Cached datasets, source checkouts, and images with the historical
markers are reused; new entries use `.ygor_*` markers. The Explorer also reads
the previous theme preference. Archived experiment records are not renamed.

---

## Quick start

Save the following as `recipe.yaml`. It uses only Python and runs the same
command once for each value of `n`.

```yaml
meta:
  name: square-demo

command: "python3 -c 'print({{ param.n }} ** 2)'"

parameters:
  values:
    n: [2, 3, 4]
```

```bash
ygor validate recipe.yaml
ygor run recipe.yaml --yes
```

Ygor reports three completed executions. Their stdout logs contain `4`, `9`, and
`16`, respectively. Open the Explorer to inspect the logs in your browser.

```bash
ygor web --open
```

For recipes that use real datasets or external tools, see the
[recipe reference](#recipe-reference).

---

## Recipe reference

### `meta`

```yaml
meta:
  name: experiment-name          # required if using ygor latest
  description: "Free text"
  author: user@example.org
  created: 2026-01-15
```

---

### `command`

One or more shell commands executed per execution. Supports Jinja2 templates.
Each command runs with its working directory set to that execution's
`{{ run.output_dir }}`, so relative output paths are isolated per execution.

```yaml
command: "solver --input {{ param.instance }} --seed {{ run.seed }}"

# or a sequence of commands (run in order; first failure aborts the execution)
command:
  - "solver --input {{ param.instance }} --seed {{ run.seed }} > {{ run.output_dir }}/out.txt"
  - "python parse.py {{ run.output_dir }}"
```

**Template context**

| Variable | Description |
|---|---|
| `{{ param.NAME }}` | Current value of parameter `NAME` |
| `{{ run.seed }}` | Current seed (0 if seeds not configured) |
| `{{ run.exec_id }}` | Execution index (1-based) |
| `{{ run.hash }}` | Run hash (first 8 chars) |
| `{{ run.run_dir }}` | Absolute path to the run directory |
| `{{ run.output_dir }}` | `<run_dir>/outputs/exec_NNNN` |
| `{{ recipe.dir }}` | Absolute path to the directory containing the recipe YAML |
| `{{ dataset.NAME }}` | Resolved path to dataset `NAME` |
| `{{ app.NAME }}` | Resolved path to app binary `NAME` |
| `{{ apps.NAME }}` | Alias for `{{ app.NAME }}` |

---

### `datasets`

```yaml
datasets:
  my-data:
    source: { type: http, url: "https://example.org/data.tar.gz" }
    sha256: b50349cde0b74844da1e95007ae38845e93e60acdd75f3d494fe10b1e8ceef22
    extract: tar.gz          # tar.gz | zip | tar.bz2 | tar.xz | bz2 | rar
    include: ["**/*.txt"]    # glob filter applied after extraction

  graphs:
    source:
      type: git
      url: "https://github.com/example/graphs.git"
      ref: v2.1.0            # tag, branch, or commit hash
    include: ["instances/**/*.mtx"]

  local-ref:
    source: { type: local, path: "../../data/custom/" }
```

---

### `parameters`

#### Values

```yaml
parameters:
  values:
    n:       [10, 50, 100, 500]          # explicit list
    alpha:   { from: 0.0, to: 1.0, step: 0.25 }  # step range
    k:       { from: 2, to: 32, count: 4 }         # count range (linear)
    timeout: { from: 1, to: 1000, count: 5, scale: log }  # log scale
```

Integer-valued ranges (`from`, `to`, `step` all integers) produce `int` values; fractional inputs produce `float`.

#### Strategies

```yaml
parameters:
  strategy: cartesian   # default — all combinations of all dimensions
  # strategy: zip       # pair dimensions element-by-element (same length required)
  # strategy: sample    # random sample (requires sample_size)
  # sample_size: 50
```

#### Instance glob

Expand a parameter from matching files inside a dataset:

```yaml
parameters:
  instance:
    - "dataset:instances/**/*.txt"
    - "dataset:graphs/hard/*.mtx"
```

`{{ param.instance }}` resolves to each matched file path.

#### Seeds (statistical replication)

```yaml
parameters:
  seeds:
    count: 30
    meta-seed: 42
```

Seeds are generated deterministically from `meta-seed` using a reproducible algorithm. Each (parameter tuple, seed) pair becomes one execution.

---

### `environment`

```yaml
environment:
  base-image: "ubuntu:24.04"    # run each execution inside Apptainer

  variables:                    # environment variables passed into the container
    OMP_NUM_THREADS: 1
    CC: gcc

  resources:                    # applied to Apptainer exec/app-build/post commands
    cpu: 8
    memory: 16GB

  image-build:                  # extra image customization commands
    run:
      - "mkdir -p /opt/my-tools"
      - "ln -s /opt/my-tools/bin/tool /usr/local/bin/tool"
      # Local secrets are supported when a command must read one:
      # - "some-command {{ secrets.local-token.value }}"

  apps:
    # Install apt packages (optionally pinned to a snapshot date)
    apt:
      snapshot: 2026-05-02      # pins apt repos to this date (reproducible builds)
      packages:
        - build-essential
        - cmake
        - libboost-all-dev

    # Install Python requirements
    python:
      requirements: requirements.lock

    # Clone and build a source dependency
    solver:
      source:
        type: git
        url: "https://github.com/example/solver.git"
        ref: v3.1.0
      build:
        - "mkdir -p build && cd build && cmake -DCMAKE_BUILD_TYPE=Release .."
        - "cmake --build build -j$(nproc)"
      binary: build/bin/solver   # path relative to repo root
```

When `base-image` is set, `ygor` builds a cached Apptainer SIF image from the `apt` and `python` specs and runs all executions inside containers. Use `--no-container` to bypass Apptainer even when `base-image` is set. The legacy `--no-docker` flag is still accepted as an alias.

The Apptainer image is content-addressed: it is only rebuilt when the base image, packages, requirements, snapshot date, or `image-build.run` commands change.

`environment.image-build.run` commands run during the Apptainer image build after apt and Python setup, so filesystem changes made by these commands persist in later execution containers. Apptainer uses the host network namespace by default, which makes it a better fit for local named licenses that depend on host identity.

These commands are stored in recipe locks and manifests, so do not put literal secrets, license keys, or tokens there. Use runtime bindings for local licenses and runtime secrets.

For build-time secrets, reference a local secret by name:

```yaml
environment:
  image-build:
    run:
      - "cd /opt/gurobi && grbgetkey {{ secrets.gurobi-key.value }}"
```

Define the value locally in `.ygorrc.yaml`:

```yaml
secrets:
  gurobi-key:
    from-env: GUROBI_KEY
    # or: value: "literal-key"
```

`ygor` copies these values into the temporary build environment under `/run/secrets/<name>` and rewrites the command to read from that file, so the secret value is not written to the generated definition file or run manifest. Any files created by the command may still become part of the image, so prefer runtime `bindings` for licenses when possible.

#### Runtime bindings

Use `environment.bindings` when a portable recipe needs a local runtime value,
file, or directory that should not be stored in experiment artifacts. Bindings
are declared by logical name in the recipe and resolved from local `.ygorrc.yaml`
at run time.

```yaml
environment:
  base-image: "gurobi/optimizer@sha256:a719c4b066cafba9849c26b1b94c45727dca5fcb40652f2ac9c2a8f38357103a"
  bindings:
    gurobi-license:
      type: file
      target: /run/ygor/bindings/gurobi.lic
      env:
        GRB_LICENSE_FILE: /run/ygor/bindings/gurobi.lic

    dataset-cache:
      type: directory
      required: false
      target: /run/ygor/bindings/dataset-cache
      env:
        DATASET_CACHE_DIR: /run/ygor/bindings/dataset-cache

    dataset-token:
      type: env
      env:
        DATASET_TOKEN: "{{ binding.value }}"
```

Local machine-specific values go in `.ygorrc.yaml`:

```yaml
bindings:
  gurobi-license:
    source: /home/user/gurobi.lic
  dataset-cache:
    source: /home/user/.cache/ygor/datasets
  dataset-token:
    from-env: UFABC_DATASET_TOKEN
    # or: value: "literal-token"
```

`file` and `directory` bindings are mounted read-only by default; set
`readonly: false` only when the container must write to the bound directory.
Required bindings default to `true`. Missing required bindings fail before
execution starts. Local source paths, host environment variable names, and
resolved binding values are not written to run manifests.

---

### `policy`

```yaml
policy:
  workers: 8               # parallel executions (default: 1)
  timeout-per-run: 30m     # per-execution wall-clock limit (s | m | h | d)
  on-failure: continue     # continue | abort
  retries: 2               # retry failed executions up to N times
```

---

### `post`

Commands run once after all executions finish. Same Jinja2 context as `command`, except `param`, `run.seed`, and `run.exec_id` are not available.

```yaml
post:
  - "python aggregate.py --run-dir {{ run.run_dir }}"
  - "python plot.py --results {{ run.run_dir }}/outputs"
```

---

## CLI commands

### `ygor validate`

Check a recipe for syntax and template errors without running anything.

```bash
ygor validate recipe.yaml
ygor validate recipe.yaml --json
```

### `ygor doctor`

Check the local CLI environment, dependencies, store access, and Apptainer availability.

```bash
ygor doctor
ygor doctor --store /data/store
ygor doctor --json
```

### `ygor run --dry-run`

Show the full execution plan without creating a run or executing recipe commands.
Use `--prepare` to resolve/download inputs and apps before planning.

```bash
ygor run --dry-run recipe.yaml
ygor run --dry-run --prepare recipe.yaml
ygor run --dry-run --commands recipe.yaml
ygor run --dry-run --prepare --commands recipe.yaml
ygor run --dry-run --json recipe.yaml
```

### `ygor run`

Execute a recipe and record the run in the store.

```bash
ygor run recipe.yaml
ygor run recipe.yaml --store /data/store   # custom store location
ygor run recipe.yaml --yes                 # skip confirmation prompt
ygor run recipe.yaml --no-container      # run on host even if base-image is set
```

### `ygor list`

List runs stored locally, most recent first.

```bash
ygor list
ygor list --status running
ygor list --recipe my-experiment
ygor list --since 2026-05-01
ygor list --limit 10
ygor list --json
```

### `ygor show`

Detailed view of a single run. Shows recipe input patterns by default; use
`--long` to also list every seed and concrete instance file frozen in the run.

```bash
ygor show                    # most recent run
ygor show a1b2c3d4
ygor show --recipe my-experiment
ygor show --status failed
ygor show --long a1b2c3d4
ygor show --recipe-lock a1b2c3d4
ygor show a1b2c3d4 --json
```

### `ygor status`

Show execution progress for a running or completed run.

```bash
ygor status             # most recent run
ygor status a1b2c3d4
ygor status --recipe my-experiment
ygor status --status running
ygor status a1b2c3d4 --json
```

### `ygor pause`

Request a graceful pause. Workers finish their current execution and then stop.

```bash
ygor pause              # most recent running run
ygor pause a1b2c3d4
ygor pause a1b2c3d4 --force  # stop running executions immediately; leave them pending
```

### `ygor resume`

Resume a paused, failed, or crashed run. Skips completed executions; retries failed ones (up to the policy retry limit).

```bash
ygor resume             # most recent eligible run
ygor resume a1b2c3d4
ygor resume a1b2c3d4 --retry-failed   # also re-run exhausted failures
ygor resume a1b2c3d4 --yes
```

### `ygor latest`

Print the hash of the most recent completed run for a recipe (useful in scripts).

```bash
ygor latest my-experiment
ygor latest my-experiment --any-status
```

### `ygor open`

Open a run directory in the default file manager.

```bash
ygor open                    # most recent run
ygor open a1b2c3d4
ygor open --recipe my-experiment
ygor open --status failed
```

### `ygor web`

Open the **Ygor Explorer**, a local, read-only web interface for your experiment
store. It is included in the Python package: no Node.js build, extra Python
dependencies, external fonts, CDN, or internet connection is required.

```bash
ygor web                               # http://127.0.0.1:8765
ygor web --store /data/experiments/store
ygor web --store /data/experiments/store --open
ygor web --port 9000                    # choose a different local port
```

Press **Ctrl+C** in the terminal to stop the server. `--open` opens your default
browser; it does not start or resume experiments. The store directory is the
directory **containing `runs/`**, not the `runs/` directory itself.

The explorer provides:

- A searchable run library with status and recipe filters, sorting and pagination.
- A run overview with progress, failure reasons, timing distributions and parameters.
- Searchable, paginated executions, with instance, seed, parameters and exit code.
- Execution inspection with output files, stdout, stderr and executor logs.
- The exact frozen `recipe.lock.yaml`, plus captured environment and source versions.
- File browsing, JSON/text/image previews, copy and file downloads.
- Light and dark themes, responsive layouts, keyboard focus and `/` to search.

Its pastel palette, compact controls and sidebar are inspired by the
[elementary OS design language](https://elementary.io/brand).
The interface is in Brazilian Portuguese. It uses local Inter/Open Sans fonts
when available, with native system-font fallbacks.

**Interpreting the data:** a completed Ygor process is not necessarily a completed
solver result. The explorer shows the original outputs without replacing bounds
or silently filtering invalid files. Empty files and JSON `completed: false`
are highlighted. Timing charts use **process wall time**, not an algorithm's
`execution_time` field, and describe which execution statuses are included.

**Large runs:** the library reads metadata without loading large execution arrays.
CSV/JSONL execution exports are indexed on first access in temporary SQLite files
outside the store. Subsequent filtering and paging use that index. The cache is
invalidated when an export changes and is removed when the server stops.
Small legacy runs without exports can be read from their manifests.
Text previews are limited to 512 KiB; downloads stream the complete file.

**Security:** this is a trusted, local-workstation tool, not a public web service.
It binds only to loopback and rejects cross-origin API requests and unexpected
hostnames. It exposes only the chosen run's recipe, manifest, execution exports,
`outputs/`, `streams/` and `logs/`; project configuration, hidden files and symlink
escapes are not served. HTML/SVG outputs are displayed as inert text rather than
executed. There are no experiment mutation endpoints.

### `ygor publish` / `ygor sync`

Mark completed runs as publishable, then synchronize publishable runs with a
shared store. The remote can be a local directory or an rsync destination.

```bash
ygor publish a1b2c3d4
ygor sync --remote /tmp/shared-store        # local directory, useful for tests
ygor sync --remote user@host:/srv/ygor/store
ygor sync --dry-run --remote /tmp/shared-store
```

You can also configure the remote in `.ygorrc.yaml`:

```yaml
sync:
  remote: "user@host:/srv/ygor/store"
```

---

## Store layout

```
store/
  publications.json       # runs marked as publishable for sync
  runs/
    <run-hash>/
      manifest.json        # run metadata, execution records, state
      recipe.lock.yaml     # snapshot of the recipe at launch time
      executions.csv       # tabular execution summary for analysis
      executions.jsonl     # structured execution summary for analysis
      logs/
        exec_0001.log
        ...
      streams/
        exec_0001/
          stdout.log       # captured stdout for the execution
          stderr.log       # captured stderr for the execution
        ...
      outputs/
        exec_0001/         # output files from each execution
        ...
  datasets/                # cached dataset downloads
  sources/                 # cached git clones
  images/                  # cached Apptainer SIF images
```

---

## Packaging

The `Makefile` builds system packages for distribution.

**Requirements:** `hatch`, `fpm` (`gem install fpm`), `rpm`, `libarchive-tools`, `zstd`.

```bash
make deb    # dist/ygor_<version>_amd64.deb
make rpm    # dist/ygor-<version>.x86_64.rpm
make arch   # dist/ygor-<version>-1-x86_64.pkg.tar.zst
make all    # all three
make clean
```

---

## Development

```bash
pip install -e ".[dev]"
pytest                        # all tests
pytest -m "not slow"          # skip slow container image build tests
node --test tests/web_ui.test.cjs # optional frontend helper checks (Node.js)
make test
```

The CI workflow (`.github/workflows/ci.yml`) runs tests against Python 3.11, 3.12, and 3.13 on every push, and builds + publishes release packages on `v*` tags.
