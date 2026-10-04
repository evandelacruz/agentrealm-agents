.PHONY: test
test: ## Run unit tests (no server)
	cd python && python3 -m unittest discover -s tests
