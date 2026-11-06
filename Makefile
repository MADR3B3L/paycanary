.PHONY: demo dev test
demo:  ## offline demo, no keys, http://127.0.0.1:8000/
	./run.sh demo
dev:   ## demo with test dependencies installed
	./run.sh dev
test:  ## full test suite in the project venv
	./run.sh test
