#!/usr/bin/env python

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk   #pylint: disable=wrong-import-position

def main():
	window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
	window.set_size_request(300, 200)
	window.max_width = 600
	window.max_height = 400
	window.connect("delete_event", Gtk.main_quit)
	vbox = Gtk.VBox(False, 0)
	btn = Gtk.Button("change geometry hints")
	vbox.add(btn)
	label = Gtk.Label()
	vbox.add(label)
	def change_hints(*_args):
		window.max_width = max(400, (window.max_width + 100) % 800)
		window.max_height = max(200, (window.max_width + 100) % 600)
		window.set_geometry_hints(max_width=window.max_width, max_height=window.max_height)
		label.set_text("max size set to: %sx%s" % (window.max_width, window.max_height))
	change_hints()
	btn.connect('clicked', change_hints)
	window.add(vbox)
	window.show_all()

	window.show_all()
	Gtk.main()
	return 0


if __name__ == "__main__":
	main()
