"""Key bindings for the interactive prompt-toolkit REPL."""

from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.key_binding import KeyBindings


def bindings(status, cycle_mode=None):
    keys = KeyBindings()

    @keys.add("c-k")
    def clear(event):
        event.app.renderer.clear()

    @keys.add("c-s")
    def show(event):
        def _show():
            output = status()
            if output:
                print(output)

        run_in_terminal(_show)

    if cycle_mode is not None:

        @keys.add("s-tab")
        def switch_mode(event):
            cycle_mode()
            event.app.invalidate()

    @keys.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @keys.add("enter")
    def submit(event):
        event.current_buffer.validate_and_handle()

    # PromptSession enables the standard Ctrl+R reverse history search.
    return keys
