SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# Required:
#   PROJECT_DIR=/path/to/project
#   AK_API=https://artifacts.internal.amiq/go/go
#
# Authentication:
#   export ARTIFACT_KEEPER_TOKEN=...
#
# Optional:
#   MODULE_JSON=/tmp/go-modules.json
#   GOPROXY=https://proxy.golang.org,direct

PROJECT_DIR ?=
AK_API ?=

MODULE_JSON ?= /tmp/go-modules.json
GOPROXY ?= https://proxy.golang.org,direct

.PHONY: seed check clean-cache download upload

seed: download upload

check:
	@test -n "$(PROJECT_DIR)" || { \
		echo "ERROR: PROJECT_DIR is required"; \
		echo "Example: make seed PROJECT_DIR=/path/to/project AK_API=https://artifacts.example/go/go-local"; \
		exit 1; \
	}
	@test -f "$(PROJECT_DIR)/go.mod" || { \
		echo "ERROR: $(PROJECT_DIR)/go.mod does not exist"; \
		exit 1; \
	}
	@test -n "$(AK_API)" || { \
		echo "ERROR: AK_API is required"; \
		exit 1; \
	}
	@test -n "$${ARTIFACT_KEEPER_TOKEN:-}" || { \
		echo "ERROR: ARTIFACT_KEEPER_TOKEN is not set"; \
		exit 1; \
	}
	@command -v go >/dev/null || { echo "ERROR: go is not installed"; exit 1; }
	@command -v jq >/dev/null || { echo "ERROR: jq is not installed"; exit 1; }
	@command -v curl >/dev/null || { echo "ERROR: curl is not installed"; exit 1; }

clean-cache:
	@echo "==> Deleting Go module cache"
	go clean -modcache

download: check clean-cache
	@echo "==> Downloading Go modules"
	@echo "    Project: $(PROJECT_DIR)"
	@echo "    GOPROXY: $(GOPROXY)"
	@rm -f "$(MODULE_JSON)"
	cd "$(PROJECT_DIR)" && \
		GOPROXY="$(GOPROXY)" \
		go mod download -json all > "$(MODULE_JSON)"
	@echo "==> Module metadata written to $(MODULE_JSON)"
	@echo "==> $$(jq -s 'length' "$(MODULE_JSON)") module records downloaded"

upload: check
	@test -s "$(MODULE_JSON)" || { \
		echo "ERROR: $(MODULE_JSON) does not exist or is empty"; \
		echo "Run 'make download' first"; \
		exit 1; \
	}

	@echo "==> Uploading modules to Artifact Keeper"
	@echo "    API: $(AK_API)"

	@cache_root="$$(go env GOMODCACHE)/cache/download/"; \
	ak_api="$(AK_API)"; \
	ak_api="$${ak_api%/}"; \
	\
	upload_file() { \
		file="$$1"; \
		url="$$2"; \
		content_type="$$3"; \
		label="$$4"; \
		\
		response_file="$$(mktemp)"; \
		status="$$(curl \
			--silent \
			--show-error \
			--output "$$response_file" \
			--write-out '%{http_code}' \
			-X PUT \
			-H "Authorization: Bearer $$ARTIFACT_KEEPER_TOKEN" \
			-H "Content-Type: $$content_type" \
			--data-binary "@$$file" \
			"$$url" || true)"; \
		\
		case "$$status" in \
			200|201|202|204) \
				echo "    UPLOADED: $$label"; \
				;; \
			409) \
				echo "    SKIP: $$label already exists"; \
				;; \
			*) \
				echo "ERROR: Failed to upload $$label (HTTP $$status)"; \
				cat "$$response_file"; \
				echo; \
				rm -f "$$response_file"; \
				exit 1; \
				;; \
		esac; \
		\
		rm -f "$$response_file"; \
	}; \
	\
	jq -c '.' "$(MODULE_JSON)" | while IFS= read -r module; do \
		path="$$(jq -r '.Path // empty' <<< "$$module")"; \
		version="$$(jq -r '.Version // empty' <<< "$$module")"; \
		zip="$$(jq -r '.Zip // empty' <<< "$$module")"; \
		mod="$$(jq -r '.GoMod // empty' <<< "$$module")"; \
		error="$$(jq -r '.Error // empty' <<< "$$module")"; \
		\
		if [[ -n "$$error" ]]; then \
			echo "ERROR: $$path@$$version: $$error"; \
			exit 1; \
		fi; \
		\
		if [[ -z "$$path" || -z "$$version" ]]; then \
			echo "SKIP: module without Path/Version"; \
			continue; \
		fi; \
		\
		if [[ -z "$$zip" || -z "$$mod" ]]; then \
			echo "SKIP: $$path@$$version has no downloadable .zip/.mod"; \
			echo "      This may be a local replace directive."; \
			continue; \
		fi; \
		\
		if [[ "$$zip" != "$$cache_root"* ]]; then \
			echo "ERROR: Unexpected Go cache path:"; \
			echo "       $$zip"; \
			exit 1; \
		fi; \
		\
		rel="$${zip#$$cache_root}"; \
		escaped_module="$${rel%/@v/*}"; \
		escaped_version="$${rel##*/@v/}"; \
		escaped_version="$${escaped_version%.zip}"; \
		\
		base_url="$$ak_api/$$escaped_module/@v/$$escaped_version"; \
		\
		echo "==> $$path@$$version"; \
		\
		upload_file \
			"$$zip" \
			"$$base_url.zip" \
			"application/zip" \
			"$$path@$$version.zip"; \
		\
		upload_file \
			"$$mod" \
			"$$base_url.mod" \
			"text/plain" \
			"$$path@$$version.mod"; \
	done

	@echo "==> Upload complete"

