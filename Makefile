.PHONY: build up down test test-unit test-integration test-e2e compile-domain verify-airgap detect clean mutation-gate

build:
	docker compose build

up:
	docker compose up -d --wait

down:
	docker compose down

detect:
	python3 scripts/stub_detector.py
	python3 scripts/secret_scan.py

test-unit:
	python3 -m pytest tests/unit/ -v

test-integration:
	python3 -m pytest tests/integration/ -v -m "not airgap"

test-e2e:
	python3 -m pytest tests/e2e/ -v

test: detect test-unit test-integration test-e2e

compile-domain:
	python3 -m domain_kits.compiler.engine --sheet domain-kits/sheets/port_ops.yaml

# Phase B — REGISTERED in KNOWN_INCOMPLETE.md. Do not run on Windows host CI.
verify-airgap:
	docker build -f agent/Dockerfile.airgap -t self_agent_airgap .
	docker run --rm --network none -e AIRGAP=true \
		-v $(PWD)/tests/fixtures/preload:/data/preload \
		self_agent_airgap python3 -m agent.security.airgap

mutation-gate:
	python3 scripts/mutation_gate.py --targets \
		agent/orchestrator/engine.py agent/swarm/spawner.py hive/sync.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
