.env:
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example"; fi
