# PayPal Agent Toolkit (Python core), vendored unchanged

- Source: https://github.com/paypal/agent-toolkit, folder `python/paypal_agent_toolkit` (commit `a3aa963960caf32912691a759879a8ce7a6018b2`), the same code as `paypal-agent-toolkit==1.11.0` on PyPI.
- License: Apache-2.0, see `LICENSE` in this folder. Copyright PayPal.
- What's here: `__init__.py` and the `shared/` package (the PayPal client, the tool list, and the tool handlers for orders, invoices, disputes and more). Not one line is changed.
- Left out: the `openai/`, `langchain/`, `crewai/` and `bedrock/` adapters. Procuro calls the toolkit's tools directly through `PayPalAPI.run(method, params)`, the entry point those adapters use.
- Why vendored instead of `pip install`: `paypal-agent-toolkit` 1.11.0 pins `crewai-tools==0.13.2`, which is not on PyPI, so the package cannot be installed with its dependencies (on Heroku or anywhere). Its core needs only `requests`, `httpx` and `pydantic`, which are in `requirements.txt`.
