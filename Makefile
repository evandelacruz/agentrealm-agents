.PHONY: test conductor-test seed-local smoke-m6-olympuff
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

seed-local: ## Seed account + API key against localhost:8080 (needs AGENTREALM_STACK_DIR)
	python3 scripts/seed_local_stack.py

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test

smoke-m6-olympuff: ## Live M6 acceptance on Olympuff (A4; needs AGENTREALM_API_KEY)
	python3 scripts/smoke_m6_olympuff.py
