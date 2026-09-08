from yoyo import step

__depends__ = {"0013_model_pin_and_voice_usage"}

# An engine mode was sticky in a way nobody meant it to be: chosen once from the admin
# menu, it survived every reset and every model change, so the admin's row sat in
# `claude` with `minimax/minimax-m3:free` in it. The model was later delisted, claude-code
# asked OpenRouter for `minimax/minimax-m3:free[1m]`, and its own error text was what the
# chat showed. History stays exactly as it was; only the engine goes back to the default
# everybody else already has, and the sandbox is re-armed from the menu when it is wanted.
steps = [
    step("UPDATE sessions SET engine_mode = 'native' WHERE engine_mode <> 'native'"),
]
