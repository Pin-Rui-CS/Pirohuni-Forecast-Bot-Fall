"""The forecasting bot. One run: run/ (entry, per-question lifecycle) -> research/
(gather evidence) -> brief/ (compile the evidence brief) -> forecasters/ (forecast)
-> run/ (submit). Shared services: llm/ (model routing, calls, costs), trace/
(run trace, call log, logging), scrapers/ (page fetching). See ARCHITECTURE.md."""
