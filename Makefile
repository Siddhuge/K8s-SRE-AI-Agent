.PHONY: install lint type test test-unit test-integration kind-up kind-down demo image helm-lint

install:
	pip install -e ".[dev,rag,azure,aws]"

lint:
	ruff check src tests

type:
	mypy src

test-unit:
	PYTHONPATH=src pytest tests -q -k "not integration"

test-integration:
	PYTHONPATH=src CLUSTERS_CONFIG=config/clusters.yaml pytest tests/test_integration_kind.py -v

test: test-unit

kind-up:
	kind create cluster --name sre-demo --wait 90s
	kubectl --context kind-sre-demo create namespace payments
	kubectl --context kind-sre-demo -n payments create secret generic db-credentials \
		--from-literal=host=db.payments.svc --from-literal=password=s3cret
	kubectl --context kind-sre-demo apply -f tests/fixtures/crashloop-deploy.yaml

kind-down:
	kind delete cluster --name sre-demo

stack-up: ## full validated env: kind + scenarios + Prometheus/Loki/Grafana + Istio + ArgoCD
	./scripts/setup-stack.sh

stack-down: ## delete the kind cluster + port-forwards
	./scripts/teardown.sh

demo: ## run an RCA against the kind demo workload
	PYTHONPATH=src CLUSTERS_CONFIG=config/clusters.yaml python3 -c \
		"from k8s_sre_agent.rca.engine import diagnose; print(diagnose('kind-sre-demo','payments','api').to_markdown())"

image:
	docker build -t k8s-sre-agent:dev .

helm-lint:
	helm lint deploy/helm/k8s-sre-agent && helm template deploy/helm/k8s-sre-agent > /dev/null
