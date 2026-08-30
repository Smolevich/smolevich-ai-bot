from yoyo import step

__depends__ = {"0011_ui_events"}

# Cerebras retired: its free tier needs a card since 17 Aug 2026, so every model answers 402.
# The model_health rows would sit there forever and keep the provider in the health screen,
# and the provider_state row would keep it "parked" in the admin view. model_health_log stays —
# that history is the point of the probe.
steps = [
    step("DELETE FROM model_health WHERE provider = 'cerebras'", ignore_errors="apply"),
    step("DELETE FROM provider_state WHERE provider = 'cerebras'", ignore_errors="apply"),
]
