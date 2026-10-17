"""`python -m bot` runs the forecasting bot (same as `python -m bot.run.forecasting_bot`)."""
import runpy

runpy.run_module("bot.run.forecasting_bot", run_name="__main__")
