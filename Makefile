.PHONY: verify verify-floor test lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

# The test suite against the tmux 3.2 floor (docs/plan.md): TMUX_FLOOR, a tmux 3.2 binary (default:
# tmux on PATH; a path is resolved to an absolute one, and the binary may have any name), goes first
# on PATH as `tmux` in a private directory, and TERMCTL_REQUIRE_TMUX makes a missing tmux fail the
# real-tmux tests instead of skipping them. The version is checked through that same link. Example:
#   make verify-floor TMUX_FLOOR=/usr/bin/tmux
TMUX_FLOOR ?= tmux

verify-floor:
	@bin="$$(command -v "$(TMUX_FLOOR)")" && bin="$$(realpath "$$bin")" || { \
		echo "verify-floor: $(TMUX_FLOOR) not found; set TMUX_FLOOR to a tmux 3.2 binary" >&2; exit 1; }; \
	dir=""; \
	trap 'rm -rf "$$dir"' EXIT; \
	trap 'rm -rf "$$dir"; exit 129' HUP; \
	trap 'rm -rf "$$dir"; exit 130' INT; \
	trap 'rm -rf "$$dir"; exit 143' TERM; \
	dir="$$(mktemp -d)" && ln -s "$$bin" "$$dir/tmux" || exit 1; \
	version="$$("$$dir/tmux" -V)"; \
	case "$$version" in "tmux 3.2"*) ;; *) \
		echo "verify-floor: $$bin is $$version, not tmux 3.2; set TMUX_FLOOR" >&2; exit 1;; esac; \
	echo "verify-floor: $$version ($$bin)" && \
	PATH="$$dir:$$PATH" TERMCTL_REQUIRE_TMUX=1 uv run pytest

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
