# Vendored: Google's AP2 Python SDK

Source: https://github.com/google-agentic-commerce/AP2 (`code/sdk/python/ap2/sdk`), AP2 v0.2,
commit `e1ea56db72a6385bce3e5c1112b3a56ce60acb43`. License: Apache-2.0 (see LICENSE in this folder). Copied unchanged.

Why vendored: the repository isn't published on PyPI, and its pyproject pins exact versions of
`cryptography` and `pydantic` that clash with the rest of this backend. The SDK runs fine with the
versions in requirements.txt (its own dependencies `jwcrypto` and `sd-jwt` are listed there).

Used by `app/mandates.py` (issuing and verifying mandates and receipts) and
`scripts/verify_receipt.py` (checking a downloaded receipt offline).
