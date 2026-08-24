PYTHON := python3

.DEFAULT_GOAL := help

help:  ## List the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  %-15s %s\n", $$1, $$2}'

.PHONY: help test mypy ruff lint model-check verification coverage-gate check drawio build-data diagrams trace dead-code dashboard derive merge clean

ARCH_DIR := docs/architecture
ARCH_DIAGRAMS := c4_architecture code_architecture decisions

# Minimum per-Component line coverage enforced by `make coverage-gate` (CI).
# Raise this as coverage improves — it is a ratchet, never lower it.
COVERAGE_MIN := 60

# draw.io source (copyright — never vendored; cloned locally for build-data).
# Pinned: the notation registries' style fingerprints are built against this
# tag, so `make build-data` is reproducible only at this ref. Bump deliberately,
# then rebuild + recommit the registries.
DRAWIO_REPO := https://github.com/jgraph/drawio.git
DRAWIO_REF := v30.2.5

test:  ## Run the test suite (+ style round-trip if fixtures present)
	$(PYTHON) -m pytest tests -v
	@if [ -d tools/palette/output ]; then \
		$(PYTHON) -m pytest tools/styles/test_roundtrip.py -v; \
	else \
		echo "No fixtures found - skipping roundtrip tests (run 'make build-data' first)"; \
	fi

mypy:  ## Type-check (mypy)
	mypy

ruff:  ## Lint (ruff)
	ruff check .

lint: mypy ruff  ## mypy + ruff, zero issues
model-check:  ## Verify the architecture model is consistent (MBSE)
	$(PYTHON) scripts/check_model.py

# Per-Component test coverage (verification traceability). Dynamic — runs the
# suite under coverage — so it is a separate target, not part of `check`.
verification:  ## Per-Component test-coverage report (advisory)
	$(PYTHON) scripts/verification_report.py

# Gating variant: fails if any code-backed Component is below COVERAGE_MIN.
# Kept out of `check` (which stays static/fast); CI runs this as its own step.
coverage-gate:  ## Fail if any Component is below COVERAGE_MIN (CI)
	$(PYTHON) scripts/verification_report.py --min $(COVERAGE_MIN)

check: lint test model-check  ## Full gate: lint + test + model-check (run before done)

# Fetch the pinned draw.io source into ./drawio (copyright — gitignored, never
# committed). Idempotent: skips if already present. A wrong local version will
# surface as a fingerprint mismatch in build-data.
drawio:  ## Clone the pinned draw.io source into ./drawio (build-data needs it)
	@if [ -d drawio/.git ]; then \
		echo "drawio: present ($$(git -C drawio describe --tags --always 2>/dev/null))"; \
	else \
		echo "drawio: cloning $(DRAWIO_REPO) @ $(DRAWIO_REF)"; \
		git clone --depth 1 --branch $(DRAWIO_REF) $(DRAWIO_REPO) drawio; \
	fi

build-data: drawio  ## Run the generated-data pipeline (auto-fetches draw.io)
	$(PYTHON) scripts/build_data.py

# Trace which classes/functions each CLI action permutation touches and write
# the analysable artifact (action_trace.json). Use --full for the exhaustive
# cartesian product. Dynamic — drives the pipeline — so not part of `check`.
trace:  ## Trace classes/functions each CLI action permutation touches
	$(PYTHON) scripts/trace_actions.py --quiet

# Advisory dead-code report: mdg_drawio definitions that no CLI action
# permutation touches, minus the allowlist. Always exits 0; the allowlist
# stays honest via tests/test_dead_code.py (part of `make test`).
dead-code:  ## Advisory dead-code report (reachability vs definitions)
	$(PYTHON) scripts/analyze_dead_code.py

# Aggregate the quality signals (tests, coverage, model-check, dead-code, lint)
# into a self-contained static D3 dashboard.html. Runs the suite under coverage,
# so it is a standalone target, not part of `check`.
dashboard:  ## Build the static D3 quality dashboard (dashboard.html)
	$(PYTHON) scripts/build_dashboard.py

# Reverse derivation (POC): given a hand-drawn .drawio, derive which registry
# shape each cell came from via weighted style matching + library ranking.
# Needs generated data (reads the palette styles). Usage:
#   make derive FILE=path/to/diagram.drawio
derive:  ## Reverse-derive registry shapes from a .drawio (FILE=...)
	$(PYTHON) -m mdg_drawio.reverse $(FILE)

# Merge newly hand-drawn cells from a .drawio into an existing .mdg, correctly
# indented/nested. Dry run by default (prints a diff, touches nothing); pass
# WRITE=1 to actually apply -- validated against the real .mdg parser first,
# so an invalid merge is refused and the file left untouched. Thin wrapper
# around the `mdg merge` subcommand -- use that directly if `mdg` is on PATH.
# Usage:
#   make merge MDG=path/to/existing.mdg FILE=path/to/diagram.drawio [WRITE=1]
merge:  ## Merge new .drawio cells into an existing .mdg (MDG=... FILE=... [WRITE=1])
	$(PYTHON) -m mdg_drawio merge $(MDG) $(FILE) $(if $(WRITE),--write,)

# Refresh the committed architecture diagrams from the model. Intentionally NOT
# --force: the overlay round-trip preserves manually arranged node positions
# while still applying model/style changes. The .drawio files are not part of
# the gate, so hand-tuning their layout is safe.
diagrams:  ## Refresh committed architecture .drawio (overlay-preserving)
	@for f in $(ARCH_DIAGRAMS); do \
		$(PYTHON) -m mdg_drawio -i $(ARCH_DIR)/$$f.mdg -o $(ARCH_DIR)/$$f.drawio; \
	done

clean:  ## Remove generated files
	rm -rf tools/palette/output tools/styles/output mdg_drawio/generated_data
	rm -f action_trace.json dashboard.html
