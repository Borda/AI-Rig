# Install AI-Rig plugins for Claude Code and/or Codex from the GitHub remote.
# Codex sync also mirrors this checkout's normal-session model defaults and personal policy into $CODEX_HOME.
# Remote installs use pushed state — commit and push before running.
#
# Run from the project root:
#   make sync-all            # Claude + Codex scopes (default target)
#   make sync-claude         # Claude scope only
#   make sync-codex          # Codex scope only
#   make clear-all           # teardown both scopes
#   make clear-claude        # teardown Claude scope only
#   make clear-codex         # teardown Codex scope only
#
# Requires GNU make (verified against GNU Make 3.81, the version Apple ships as /usr/bin/make;
# not BSD make). Every multi-statement recipe below is written the traditional portable way —
# explicit trailing `\` plus explicit `;` after every statement — rather than relying on the
# `.ONESHELL:` directive, which GNU Make 3.81 does not implement (confirmed empirically: state
# set on one un-continued recipe line does not survive to the next).
#
# EXTERNAL_PLUGIN_TIMEOUT_SECONDS (env var, default 120) bounds every external
# marketplace/plugin command. There is no CLI-flag equivalent — set it in the environment:
#   EXTERNAL_PLUGIN_TIMEOUT_SECONDS=300 make sync-claude

SHELL := /bin/bash
.SHELLFLAGS := -ec
.NOTPARALLEL:
.DEFAULT_GOAL := sync-all

PLUGINS := foundry oss develop research codemap-py bridge
EXTERNAL_PLUGINS := caveman@caveman
MARKETPLACE := $(shell jq -r '.name' .claude-plugin/marketplace.json)
SETTINGS := $(HOME)/.claude/settings.json
KNOWN_MARKETPLACES := $(HOME)/.claude/plugins/known_marketplaces.json
INSTALLED_PLUGINS := $(HOME)/.claude/plugins/installed_plugins.json
CACHE_DIR := $(HOME)/.claude/plugins/cache
PROJECT_DIR := $(shell pwd)
MARKETPLACE_REMOTE := $(shell git -C $(PROJECT_DIR) remote get-url origin 2>/dev/null | sed 's/\.git$$//')
CODEX_SYNC_SCRIPT := $(PROJECT_DIR)/plugins/codex-rig/scripts/sync_codex.py
CODEX_HOME_SYNC_SCRIPT := $(PROJECT_DIR)/scripts/sync_codex_session_policy.py
TIMEOUT_RUNNER := $(PROJECT_DIR)/scripts/run_with_timeout.py
EXTERNAL_PLUGIN_TIMEOUT_SECONDS ?= 120

.PHONY: sync-all sync-claude sync-codex clear-all clear-claude clear-codex \
        migrate-marketplace uninstall-claude-plugins refresh-ext-marketplace \
        update-ext-plugins register-marketplace install-claude-plugins \
        install-codex-plugins sync-codex-home-policy

## Meta targets ---------------------------------------------------------------

# sync-claude/sync-codex are hard sequential fail-fast chains, matching sync.sh's
# original `set -e` behavior exactly. sync-all deliberately deviates: it runs both
# phases regardless of either's outcome and aggregates the failure at the end,
# instead of a codex-phase failure silently discarding an earlier claude-phase
# failure count (which is what sync.sh's bare `set -e` does today).
sync-all:
	@status=0; \
	$(MAKE) sync-claude || status=1; \
	$(MAKE) sync-codex || status=1; \
	if [ $$status -ne 0 ]; then \
		echo "⚠ sync-all finished with failures — see output above"; \
	else \
		echo "✓ sync-all complete"; \
	fi; \
	exit $$status

sync-claude: migrate-marketplace uninstall-claude-plugins refresh-ext-marketplace update-ext-plugins register-marketplace install-claude-plugins
	@echo "✓ Claude sync complete"

sync-codex: install-codex-plugins sync-codex-home-policy
	@echo "✓ Codex sync complete"

clear-all:
	@status=0; \
	$(MAKE) clear-claude || status=1; \
	$(MAKE) clear-codex || status=1; \
	echo "✓ Cleared (managed plugins uninstalled; marketplace registrations + caveman left in place)"; \
	exit $$status

## Claude-side targets ---------------------------------------------------------

clear-claude:
	@echo "Clearing Claude marketplace plugins..."; \
	for p in $(PLUGINS); do \
		claude plugin uninstall "$$p@$(MARKETPLACE)" 2>/dev/null && echo "  ✓ uninstalled $$p" || echo "  – $$p not installed, skipping"; \
	done; \
	echo "✓ Claude plugins cleared"

migrate-marketplace:
	@echo "Migrating stale marketplace registrations..."; \
	while IFS= read -r stale; do \
		stale="$${stale%$$'\r'}"; \
		[[ -z "$$stale" ]] && continue; \
		echo "Migrating marketplace '$$stale' → '$(MARKETPLACE)'..."; \
		if [[ -d "$(CACHE_DIR)/$$stale" && ! -d "$(CACHE_DIR)/$(MARKETPLACE)" ]]; then \
			mv "$(CACHE_DIR)/$$stale" "$(CACHE_DIR)/$(MARKETPLACE)"; \
			echo "  ✓ cache dir renamed"; \
		elif [[ -d "$(CACHE_DIR)/$$stale" ]]; then \
			rm -rf "$(CACHE_DIR)/$$stale"; \
			echo "  ✓ stale cache dir removed"; \
		fi; \
		tmp=$$(mktemp); \
		jq --arg old "$$stale" --arg new "$(MARKETPLACE)" '.[$$new] = .[$$old] | del(.[$$old])' "$(KNOWN_MARKETPLACES)" > "$$tmp" && mv "$$tmp" "$(KNOWN_MARKETPLACES)"; \
		tmp=$$(mktemp); \
		jq --arg old "$$stale" --arg new "$(MARKETPLACE)" '.plugins = (.plugins | with_entries(.key |= gsub($$old; $$new)) | walk(if type == "string" then gsub($$old; $$new) else . end))' "$(INSTALLED_PLUGINS)" > "$$tmp" && mv "$$tmp" "$(INSTALLED_PLUGINS)"; \
		tmp=$$(mktemp); \
		jq --arg old "$$stale" --arg new "$(MARKETPLACE)" 'del(.extraKnownMarketplaces[$$old]) | walk(if type == "string" then gsub($$old; $$new) elif type == "object" then with_entries(.key |= gsub($$old; $$new)) else . end)' "$(SETTINGS)" > "$$tmp" && mv "$$tmp" "$(SETTINGS)"; \
		echo "  ✓ registries updated ($$stale → $(MARKETPLACE))"; \
	done < <(jq -r --arg path "$(PROJECT_DIR)" --arg new "$(MARKETPLACE)" 'to_entries | map(select(.value.source.path == $$path and .key != $$new)) | .[].key' "$(KNOWN_MARKETPLACES)")

uninstall-claude-plugins:
	@echo "Uninstalling existing plugins..."; \
	for p in $(PLUGINS); do \
		claude plugin uninstall "$$p@$(MARKETPLACE)" 2>/dev/null && echo "  ✓ uninstalled $$p" || echo "  – $$p not installed, skipping"; \
	done

refresh-ext-marketplace:
	@echo "Refreshing external plugin marketplaces..."; \
	if ! python3 "$(TIMEOUT_RUNNER)" --timeout-seconds "$(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)" --label "caveman marketplace registration" -- claude plugin marketplace add JuliusBrussee/caveman 2>/dev/null; then \
		echo "  ⚠ caveman marketplace registration failed or timed out; trying the existing registration"; \
	fi; \
	if python3 "$(TIMEOUT_RUNNER)" --timeout-seconds "$(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)" --label "caveman marketplace refresh" -- claude plugin marketplace update caveman 2>/dev/null; then \
		echo "  ✓ caveman refreshed"; \
	else \
		external_status=$$?; \
		if [[ $$external_status -eq 124 ]]; then \
			echo "  ⚠ caveman refresh timed out after $(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)s"; \
		else \
			echo "  ⚠ caveman refresh failed (offline?)"; \
		fi; \
	fi

# Gate on marketplace-refresh success dropped intentionally (blueprint constraint 7,
# no test coverage found) — always attempts reinstall regardless of refresh outcome.
update-ext-plugins:
	@echo "Updating external plugins..."; \
	for p in $(EXTERNAL_PLUGINS); do \
		if python3 "$(TIMEOUT_RUNNER)" --timeout-seconds "$(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)" --label "$$p uninstall" -- claude plugin uninstall "$$p" 2>/dev/null; then \
			echo "  ✓ uninstalled $$p"; \
		else \
			external_status=$$?; \
			if [[ $$external_status -eq 124 ]]; then \
				echo "  ⚠ $$p uninstall timed out; skipping reinstall"; \
				continue; \
			fi; \
			echo "  – $$p not installed, skipping uninstall"; \
		fi; \
		if python3 "$(TIMEOUT_RUNNER)" --timeout-seconds "$(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)" --label "$$p install" -- claude plugin install "$$p"; then \
			identity=$$(jq -r --arg plugin "$$p" '(.plugins[$$plugin] // []) | sort_by(.lastUpdated // "") | last // {} | [(.version // "unknown"), (.gitCommitSha // "unknown")] | @tsv' "$(INSTALLED_PLUGINS)" 2>/dev/null || printf 'unknown\tunknown'); \
			version="$${identity%%$$'\t'*}"; \
			revision="$${identity#*$$'\t'}"; \
			[[ "$$revision" != "unknown" ]] && revision="$${revision:0:12}"; \
			echo "  ✓ $$p: version $$version, revision $$revision"; \
		else \
			external_status=$$?; \
			if [[ $$external_status -eq 124 ]]; then \
				echo "  ✗ $$p install timed out after $(EXTERNAL_PLUGIN_TIMEOUT_SECONDS)s"; \
			else \
				echo "  ✗ $$p install failed"; \
			fi; \
		fi; \
	done

register-marketplace:
	@echo "Registering marketplace (GitHub source → versioned cache install)..."; \
	LOCAL_SHA=$$(git -C "$(PROJECT_DIR)" rev-parse HEAD 2>/dev/null); \
	REMOTE_SHA=$$(git ls-remote "$(MARKETPLACE_REMOTE)" HEAD 2>/dev/null | awk '{print $$1}'); \
	if [ -z "$$REMOTE_SHA" ]; then \
		echo "  ⚠ cannot reach $(MARKETPLACE_REMOTE) — skipping SHA check (offline or auth?)"; \
	elif [ "$$LOCAL_SHA" != "$$REMOTE_SHA" ]; then \
		echo "  ┌──────────────────────────────────────────────────────────────"; \
		echo "  │ ⚠ LOCAL ≠ REMOTE — cache will install the REMOTE commit, not your local work"; \
		echo "  │   local  HEAD: $$LOCAL_SHA"; \
		echo "  │   remote HEAD: $$REMOTE_SHA"; \
		echo "  │   → commit + push first, or these plugins install a stale/different version"; \
		echo "  └──────────────────────────────────────────────────────────────"; \
	fi; \
	claude plugin marketplace remove "$(MARKETPLACE)" 2>/dev/null || true; \
	claude plugin marketplace add "$(MARKETPLACE_REMOTE)"

# Folds install + purge + setup-skills into one target (blueprint constraint 4) — one
# shell invocation, so the bridge-purge guard (constraint 6) and the try-all-6-then-report
# contract (constraint 3) both stay local bash state, no relay file needed.
install-claude-plugins:
	@echo "Installing plugins..."; \
	BRIDGE_INSTALLED=false; \
	FAILED_INSTALLS=0; \
	for p in $(PLUGINS); do \
		if claude plugin install "$$p@$(MARKETPLACE)"; then \
			identity=$$(jq -r --arg plugin "$$p@$(MARKETPLACE)" '(.plugins[$$plugin] // []) | sort_by(.lastUpdated // "") | last // {} | [(.version // "unknown"), (.gitCommitSha // "unknown")] | @tsv' "$(INSTALLED_PLUGINS)" 2>/dev/null || printf 'unknown\tunknown'); \
			version="$${identity%%$$'\t'*}"; \
			revision="$${identity#*$$'\t'}"; \
			[[ "$$revision" != "unknown" ]] && revision="$${revision:0:12}"; \
			echo "  ✓ $$p@$(MARKETPLACE): version $$version, revision $$revision"; \
			if [[ "$$p" == "bridge" ]]; then \
				BRIDGE_INSTALLED=true; \
			fi; \
		else \
			echo "  ✗ $$p@$(MARKETPLACE) install failed"; \
			FAILED_INSTALLS=$$((FAILED_INSTALLS + 1)); \
		fi; \
	done; \
	PURGE_PLUGINS=(ponytail@ponytail); \
	if $$BRIDGE_INSTALLED; then \
		PURGE_PLUGINS+=(codex@openai-codex); \
	fi; \
	echo "Purging retired plugins..."; \
	for p in "$${PURGE_PLUGINS[@]}"; do \
		if claude plugin uninstall "$$p" 2>/dev/null; then \
			echo "  ✓ purged $$p"; \
		else \
			echo "  – $$p not installed, nothing to purge"; \
		fi; \
	done; \
	echo "Initializing installed plugin setup skills..."; \
	for p in $(PLUGINS); do \
		install_path=$$(jq -r --arg plugin "$$p@$(MARKETPLACE)" '(.plugins[$$plugin] // []) | map(select(.installPath?)) | sort_by(.installedAt // "") | last // {} | .installPath // ""' "$(INSTALLED_PLUGINS)"); \
		if [[ -z "$$install_path" ]]; then \
			echo "  – $$p not installed, skipping setup"; \
			continue; \
		fi; \
		if [[ "$$p" == "bridge" ]]; then \
			bridge_doctor="$$install_path/bin/bridge_diagnose.py"; \
			if [[ ! -f "$$bridge_doctor" || -L "$$bridge_doctor" ]]; then \
				echo "  ✗ bridge static diagnosis is incomplete or linked" >&2; \
				exit 1; \
			fi; \
			if ! python_version=$$(python3 --version 2>&1); then \
				echo "  ✗ bridge requires Python 3.10 or newer" >&2; \
				exit 1; \
			fi; \
			if [[ ! "$$python_version" =~ Python[[:space:]]+([0-9]+)\.([0-9]+) ]]; then \
				echo "  ✗ bridge requires Python 3.10 or newer" >&2; \
				exit 1; \
			fi; \
			if (( BASH_REMATCH[1] < 3 || (BASH_REMATCH[1] == 3 && BASH_REMATCH[2] < 10) )); then \
				echo "  ✗ bridge requires Python 3.10 or newer; found $$python_version" >&2; \
				exit 1; \
			fi; \
			if command -v codex >/dev/null 2>&1; then \
				bridge_direction="codex"; \
			else \
				echo "  – codex CLI not found; bridge diagnosis covers the claude direction only"; \
				bridge_direction="claude"; \
			fi; \
			if ! bridge_diagnosis=$$(python3 "$$bridge_doctor" --direction "$$bridge_direction"); then \
				echo "  ✗ bridge static diagnosis command failed" >&2; \
				exit 1; \
			fi; \
			if ! jq -e '(.ok == true and .live == false and .payload.complete == true)' <<<"$$bridge_diagnosis" >/dev/null; then \
				echo "  ✗ bridge static diagnosis failed" >&2; \
				exit 1; \
			fi; \
			echo "  ✓ bridge static diagnosis passed; no provider call made"; \
			continue; \
		fi; \
		setup_skill=""; \
		for candidate in "$$install_path/skills/setup/SKILL.md" "$$install_path/claude-skills/setup/SKILL.md"; do \
			if [[ -f "$$candidate" ]]; then \
				setup_skill="$$candidate"; \
				break; \
			fi; \
		done; \
		if [[ -z "$$setup_skill" ]]; then \
			echo "  – $$p has no setup skill, skipping"; \
			continue; \
		fi; \
		echo "  → $$p:setup"; \
		claude --print "/$$p:setup --approve"; \
	done; \
	if [ "$$FAILED_INSTALLS" -gt 0 ]; then \
		echo "⚠ Done with $$FAILED_INSTALLS failed install(s) — rerun after checking network and marketplace access"; \
		exit 1; \
	fi; \
	echo "✓ Done"

## Codex-side targets ----------------------------------------------------------

clear-codex:
	@echo "Clearing Codex plugins..."; \
	python3 "$(CODEX_SYNC_SCRIPT)" clear; \
	echo "✓ Codex plugins cleared"

# --codex-ref/--no-clean/--no-codex-global-agents dropped (blueprint constraint 2):
# always tracks the default branch, always cleans before reinstalling, always
# installs the global-instructions block.
install-codex-plugins:
	@python3 "$(CODEX_SYNC_SCRIPT)" install

sync-codex-home-policy:
	@python3 "$(CODEX_HOME_SYNC_SCRIPT)" \
		--source-config "$(PROJECT_DIR)/.codex/config.toml" \
		--source-policy "$(PROJECT_DIR)/.codex/global-session-policy.md" \
		--codex-home "$${CODEX_HOME:-$$HOME/.codex}"
