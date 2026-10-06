.PHONY: setup test conductor-test smoke-m6-olympuff smoke-m7-olympuff smoke-m8-olympuff smoke-m9-olympuff smoke-m10-olympuff smoke-m11-olympuff probe-regen
setup: ## Install the AI planner's provider SDK (python/requirements.txt); live runs need them, `make test` does not
	python3 -m pip install -r python/requirements.txt

test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test

# The character to play (A59): `make smoke-m7-olympuff CHARACTER_ID=123` or
# CHARACTER_NAME=Pat. With neither, the scripts read AGENTREALM_CHARACTER_ID.
CHARACTER_FLAGS = $(if $(CHARACTER_ID),--character-id $(CHARACTER_ID)) $(if $(CHARACTER_NAME),--character-name "$(CHARACTER_NAME)")

smoke-m6-olympuff: ## Live M6 acceptance on Olympuff (A4; needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m6_olympuff.py $(CHARACTER_FLAGS)

smoke-m7-olympuff: ## Live M7 acceptance on Olympuff (A16; needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m7_olympuff.py $(CHARACTER_FLAGS)

smoke-m8-olympuff: ## Live M8 acceptance on Olympuff (A25; needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m8_olympuff.py $(CHARACTER_FLAGS)

smoke-m9-olympuff: ## Live M9 acceptance on Olympuff (A29; needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m9_olympuff.py $(CHARACTER_FLAGS)

smoke-m10-olympuff: ## Live M10 acceptance on Olympuff (A33; needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m10_olympuff.py $(CHARACTER_FLAGS)

smoke-m11-olympuff: ## Live M11 acceptance on Olympuff (A40; needs AGENTREALM_API_KEY, the AI planner key (AGENTREALM_PLANNER_ANTHROPIC_KEY or ANTHROPIC_API_KEY; or an OpenAI key with AGENTREALM_PLANNER_MODEL), and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID; left the level means back on the overworld map)
	python3 scripts/smoke_m11_olympuff.py $(CHARACTER_FLAGS)

probe-regen: ## Regen probe (A60): get hurt, then measure safe-zone regen for the A16 gate; run before the M7 hour if the knowledge base has no regen answer (needs AGENTREALM_API_KEY, a planner key (or --no-planner, a test mode) and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/probe_regen.py $(CHARACTER_FLAGS)
