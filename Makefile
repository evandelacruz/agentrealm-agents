.PHONY: test conductor-test smoke-m6-olympuff smoke-m7-olympuff
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test

smoke-m6-olympuff: ## Live M6 acceptance on Olympuff (A4; needs AGENTREALM_API_KEY)
	python3 scripts/smoke_m6_olympuff.py

smoke-m7-olympuff: ## Live M7 acceptance on Olympuff (A16; needs AGENTREALM_API_KEY)
	python3 scripts/smoke_m7_olympuff.py
