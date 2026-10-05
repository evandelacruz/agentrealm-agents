.PHONY: test conductor-test smoke-m6-olympuff smoke-m7-olympuff smoke-m11-olympuff
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test

# The character to play (A59): `make smoke-m7-olympuff CHARACTER_ID=123` or
# CHARACTER_NAME=Pat. With neither, the scripts read AGENTREALM_CHARACTER_ID.
CHARACTER_FLAGS = $(if $(CHARACTER_ID),--character-id $(CHARACTER_ID)) $(if $(CHARACTER_NAME),--character-name "$(CHARACTER_NAME)")

smoke-m6-olympuff: ## Live M6 acceptance on Olympuff (A4; needs AGENTREALM_API_KEY and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m6_olympuff.py $(CHARACTER_FLAGS)

smoke-m7-olympuff: ## Live M7 acceptance on Olympuff (A16; needs AGENTREALM_API_KEY and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID)
	python3 scripts/smoke_m7_olympuff.py $(CHARACTER_FLAGS)

smoke-m11-olympuff: ## Live M11 acceptance on Olympuff (A40; needs AGENTREALM_API_KEY, the strategist (AGENTREALM_STRATEGIST_MODEL and AGENTREALM_STRATEGIST_API_KEY or OPENAI_API_KEY), and CHARACTER_ID=, CHARACTER_NAME= or AGENTREALM_CHARACTER_ID; left the level means back on the overworld map)
	python3 scripts/smoke_m11_olympuff.py $(CHARACTER_FLAGS)
