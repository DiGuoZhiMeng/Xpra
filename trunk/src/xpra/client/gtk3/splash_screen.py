# This file is part of Xpra.
# Copyright (C) 2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
from gi.repository import Gtk, GLib, Pango

from xpra import __version__
from xpra.gtk_common.gtk_util import add_close_accel, get_icon_pixbuf
from xpra.gtk_common.gobject_compat import install_signal_handlers
from xpra.client.gtk_base.css_overrides import inject_css_overrides
from xpra.platform.gui import force_focus
from xpra.log import Logger

log = Logger("client", "util")

inject_css_overrides()


class SplashScreen(Gtk.Window):

    def __init__(self):
        self.exit_code = None
        super().__init__(type=Gtk.WindowType.POPUP)
        self.connect("delete_event", self.exit)
        title = "Xpra %s" % __version__
        self.set_title("Xpra")
        self.set_size_request(320, 160)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_decorated(False)
        vbox = Gtk.VBox()
        hbox = Gtk.HBox(homogeneous=False)
        icon = get_icon_pixbuf("xpra")
        if icon:
            self.set_icon(icon)
            hbox.pack_start(Gtk.Image.new_from_pixbuf(icon), False, False, 20)
        label = Gtk.Label(label=title)
        label.modify_font(Pango.FontDescription("sans 18"))
        hbox.pack_start(label, True, True, 20)
        vbox.add(hbox)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_size_request(320, 30)
        self.progress_bar.set_text(" ")
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_fraction(0)
        self.progress_timer = 0
        vbox.add(self.progress_bar)
        self.add(vbox)
        install_signal_handlers(None, self.handle_signal)
        self.opacity = 100


    def run(self):
        from xpra.make_thread import start_thread
        start_thread(self.read_stdin, "read-stdin", True)
        self.show_all()
        force_focus()
        self.present()
        Gtk.main()
        return self.exit_code or 0

    def read_stdin(self):
        log("read_stdin()")
        while self.exit_code is None:
            line = sys.stdin.readline()
            self.handle_stdin_line(line)

    def handle_stdin_line(self, line):
        parts = line.rstrip("\n\r").split(":")
        if parts[0]:
            try:
                pct = int(parts[0])
            except ValueError:
                pass
            else:
                self.cancel_progress_timer()
                GLib.idle_add(self.progress_bar.set_fraction, pct/100.0)
                if pct==100:
                    GLib.timeout_add(20, self.fade_out)
                    GLib.timeout_add(1500, self.exit)
                else:
                    self.progress_timer = GLib.timeout_add(40, self.increase_fraction, pct)
        if len(parts)>=2:
            self.progress_bar.set_text(parts[1])
            self.progress_bar.set_show_text(True)

    def cancel_progress_timer(self):
        pt = self.progress_timer
        if pt:
            self.progress_timer = 0
            GLib.source_remove(pt)

    def increase_fraction(self, pct, inc=1, max_increase=10):
        log("increase_fraction%s", (pct, inc, max_increase))
        self.cancel_progress_timer()
        GLib.idle_add(self.progress_bar.set_fraction, (pct+inc)/100.0)
        if inc<max_increase:
            self.progress_timer = GLib.timeout_add(40+inc*10, self.increase_fraction, pct, inc+1, max_increase)
        return False


    def fade_out(self):
        self.opacity = max(0, self.opacity-2)
        self.set_opacity(self.opacity/100.0)
        return self.opacity>0

    def exit(self, *args):
        log("exit%s calling %s", args, Gtk.main_quit)
        if self.exit_code is None:
            self.exit_code = 0
        self.cancel_progress_timer()
        Gtk.main_quit()


    def handle_signal(self, signum, _frame=None):
        self.exit_code = 128-(signum or 0)
        GLib.idle_add(self.exit)


def main(args):
    import os
    if os.environ.get("XPRA_HIDE_DOCK") is None:
        os.environ["XPRA_HIDE_DOCK"] = "1"
    from xpra.platform import program_context
    with program_context("splash", "Splash"):
        Gtk.Window.set_auto_startup_notification(False)
        w = SplashScreen()
        add_close_accel(w, Gtk.main_quit)
        return w.run()


if __name__ == "__main__":
    main(sys.argv)
    sys.exit(0)
