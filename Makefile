VERSION  := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
DIST     := dist
STAGING  := $(DIST)/.staging
ARCH     := $(shell uname -m)
DEB_ARCH := $(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
DEB_DEPENDS := python3 rsync apptainer unar

.PHONY: all wheel deb rpm arch test clean help

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "  wheel   Build Python wheel (requires hatch)"
	@echo "  deb     Build .deb package (requires fpm)"
	@echo "  rpm     Build .rpm package (requires fpm + rpm)"
	@echo "  arch    Build .pkg.tar.zst package (requires fpm + libarchive-tools + zstd)"
	@echo "  all     Build deb + rpm + arch"
	@echo "  test    Run fast tests"
	@echo "  clean   Remove $(DIST)/"

all: deb rpm arch

test:
	pytest -m "not slow" -q

# ── wheel ──────────────────────────────────────────────────────────────────────

wheel:
	hatch build -t wheel

# ── staging layout ─────────────────────────────────────────────────────────────
# Install the package + all Python deps to a non-version-specific path so the
# resulting system package works regardless of the Python minor version on the
# target host.

$(STAGING): wheel
	rm -rf $(STAGING) $(DIST)/.venv
	# Fresh venv so pip resolves only ygor's deps, not whatever is in the global env
	python -m venv $(DIST)/.venv
	$(DIST)/.venv/bin/pip install --quiet $(DIST)/ygor-$(VERSION)-*.whl
	mkdir -p $(STAGING)/usr/lib/ygor $(STAGING)/usr/bin
	sp=$$(find $(DIST)/.venv/lib -name site-packages -type d | head -1); \
	cp -r "$$sp/." $(STAGING)/usr/lib/ygor/
	# Strip pip and wheel — not needed at runtime
	rm -rf $(STAGING)/usr/lib/ygor/pip \
		$(STAGING)/usr/lib/ygor/pip-*.dist-info \
		$(STAGING)/usr/lib/ygor/wheel \
		$(STAGING)/usr/lib/ygor/wheel-*.dist-info
	find $(STAGING)/usr/lib/ygor -name "*.pyc" -delete
	find $(STAGING)/usr/lib/ygor -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
	printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "/usr/lib/ygor")\nfrom ygor.cli import app\napp()\n' \
		> $(STAGING)/usr/bin/ygor
	chmod +x $(STAGING)/usr/bin/ygor

# ── packages ───────────────────────────────────────────────────────────────────

deb: $(STAGING)
	fpm -s dir -t deb \
		-n ygor \
		-v $(VERSION) \
		--description "Declarative experiment runner" \
		$(foreach dep,$(DEB_DEPENDS),--depends $(dep)) \
		--deb-compression gz \
		--chdir $(STAGING) \
		--package $(DIST)/ygor_$(VERSION)_$(DEB_ARCH).deb \
		.

rpm: $(STAGING)
	fpm -s dir -t rpm \
		-n ygor \
		-v $(VERSION) \
		--description "Declarative experiment runner" \
		--depends python3 \
		--chdir $(STAGING) \
		--package $(DIST)/ygor-$(VERSION).$(ARCH).rpm \
		.

arch: $(STAGING)
	fpm -s dir -t pacman \
		-n ygor \
		-v $(VERSION) \
		--description "Declarative experiment runner" \
		--depends python \
		--chdir $(STAGING) \
		--package $(DIST)/ygor-$(VERSION)-1-$(ARCH).pkg.tar.zst \
		.

# ── housekeeping ───────────────────────────────────────────────────────────────

clean:
	rm -rf $(DIST)
