"""GTK4 + libadwaita front end.

The toolkit versions are pinned here so importing the GUI always selects GTK 4
rather than whatever else may be installed; the import below must therefore
follow the pins.
"""

# ruff: noqa: E402

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .window import VitrineWindow

__all__ = ["VitrineWindow"]
