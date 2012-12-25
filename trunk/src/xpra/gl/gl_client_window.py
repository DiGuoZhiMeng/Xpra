# This file is part of Parti.
# Copyright (C) 2012 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2012 Antoine Martin <antoine@devloop.org.uk>
# Parti is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from wimpiggy.log import Logger
log = Logger()

from xpra.gl.gl_check import check_support
check_support()

from xpra.client_window import ClientWindow
from xpra.gl.gl_window_backing import GLPixmapBacking


class GLClientWindow(ClientWindow):

    def __init__(self, client, group_leader, wid, x, y, w, h, metadata, override_redirect, client_properties, auto_refresh_delay):
        log("GLClientWindow(..)")
        ClientWindow.__init__(self, client, group_leader, wid, x, y, w, h, metadata, override_redirect, client_properties, auto_refresh_delay)
        self.add(self._backing.glarea)

    def do_configure_event(self, event):
        log("GL do_configure_event(%s)", event)
        ClientWindow.do_configure_event(self, event)
        self._backing.paint_screen = True

    def destroy(self):
        self._backing.paint_screen = False
        ClientWindow.destroy(self)

    def new_backing(self, w, h):
        log("GL new_backing(%s, %s)", w, h)
        w = max(2, w)
        h = max(2, h)
        lock = None
        if self._backing:
            lock = self._backing._video_decoder_lock
        try:
            if lock:
                lock.acquire()
            if self._backing is None:
                self._backing = GLPixmapBacking(self._id, w, h, self._client.supports_mmap, self._client.mmap)
            self._backing.init(w, h)
        finally:
            if lock:
                lock.release()
