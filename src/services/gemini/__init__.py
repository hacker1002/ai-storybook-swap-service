"""Gemini call helpers — invoke helper, model resolution, response extraction,
safety settings, payload/token budget.

`gemini_ainvoke` (invoke.py) is the ONE place a `ChatGoogleGenerativeAI` client
is built + invoked; `resolve_gemini_model` (model_resolution.py) is the ONE
model-precedence resolver (ADR-049). Import from the submodules directly — this
package `__init__` stays import-light (no eager re-export) so importing
`src.services.gemini` never drags in langchain.
"""
