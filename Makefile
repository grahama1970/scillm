notebooks-smoke:
	@echo "Rebuilding viewer notebooks"
	@uv run -- python scripts/notebooks_build.py
	@echo "Running self-contained notebooks with 150s timeout each"
	@set -e; \
	for nb in \
	  notebooks/01_chutes_openai_compatible.ipynb \
	  notebooks/02_router_parallel_batch.ipynb \
	  notebooks/03_model_list_first_success.ipynb \
	  notebooks/04a_tools_only.ipynb \
	  notebooks/09_fallback_infer_with_meta.ipynb \
	  notebooks/10_auto_router_one_liner.ipynb \
	  notebooks/11_provider_perplexity.ipynb \
	  notebooks/14_provider_matrix.ipynb; do \
	  echo "Executing $$nb"; \
	  uv run -- python -m nbconvert --ExecutePreprocessor.timeout=150 --to notebook --execute $$nb --output $$(basename $$nb .ipynb)_executed.ipynb --output-dir notebooks; \
	done
	@echo "OK"

notebooks-smoke-ci:
	@echo "CI smoke (subset that skips providers without keys)"
	@uv run -- python scripts/notebooks_build.py
	@OPENAI_API_KEY="" ANTHROPIC_API_KEY="" PERPLEXITY_API_KEY="" uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/14_provider_matrix.ipynb --output notebooks/14_provider_matrix_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/11_provider_perplexity.ipynb --output notebooks/11_provider_perplexity_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/09_fallback_infer_with_meta.ipynb --output notebooks/09_fallback_infer_with_meta_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/10_auto_router_one_liner.ipynb --output notebooks/10_auto_router_one_liner_executed.ipynb
	@echo "CI OK"

agents-smoke:
	@echo "Running agents/bridges notebooks with 180s timeout each"
	@set -e; \
	for nb in \
	  notebooks/05_codex_agent_doctor.ipynb \
	  notebooks/06_mini_agent_doctor.ipynb \
	  notebooks/07_codeworld_mcts.ipynb \
	  notebooks/08_certainly_bridge.ipynb; do \
	  echo "Executing $$nb"; \
	  uv run -- python -m nbconvert --ExecutePreprocessor.timeout=180 --to notebook --execute $$nb --output $$(basename $$nb .ipynb)_executed.ipynb --output-dir notebooks; \
	done
	@echo "Agents OK"
