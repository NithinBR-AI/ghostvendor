DEMO_REPO   := NithinBR-AI/ghostvendor-demo-app
TRIGGER_BY  := NithinBR-AI
PYTHON      := .venv/Scripts/python.exe

.PHONY: run run-demo install

run:
	$(PYTHON) main.py $(DEMO_REPO) --triggered-by $(TRIGGER_BY)

run-demo: run

install:
	$(PYTHON) -m pip install -e ".[dev]"
