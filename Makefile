.PHONY: test conductor-test seed-local
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

seed-local: ## Seed account + API key against localhost:8080 (needs AGENTREALM_STACK_DIR)
	python3 scripts/seed_local_stack.py

seed-local-probe: ## Same as seed-local, plus opt-in sandbox create probe (Manual §13)
	python3 scripts/seed_local_stack.py --probe

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test
