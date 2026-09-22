SHELL := /usr/bin/env sh

COMMIT := $(shell git rev-parse --short origin/main)

.PHONY: cp env

cp:
	@echo $(COMMIT)
	@printf '%s' $(COMMIT) | xclip -selection clipboard 2>/dev/null && echo "(copied to clipboard)" || true

env:
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example"; fi
