.PHONY: test conductor-test
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests

conductor-test: ## Build and test tools/conductor (Node 22+)
	cd tools/conductor && npm ci && npm test
