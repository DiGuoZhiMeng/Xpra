# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2013, 2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.log import Logger
log = Logger("shadow", "osx")

from xpra.server.gtk_server_base import GTKServerBase
from xpra.server.shadow.root_window_model import RootWindowModel
from xpra.server.shadow.shadow_server_base import ShadowServerBase
from xpra.platform.darwin.gui import get_CG_imagewrapper, take_screenshot

import gtk.gdk
import Quartz.CoreGraphics as CG    #@UnresolvedImport

ALPHA = {
         CG.kCGImageAlphaNone                  : "AlphaNone",
         CG.kCGImageAlphaPremultipliedLast     : "PremultipliedLast",
         CG.kCGImageAlphaPremultipliedFirst    : "PremultipliedFirst",
         CG.kCGImageAlphaLast                  : "Last",
         CG.kCGImageAlphaFirst                 : "First",
         CG.kCGImageAlphaNoneSkipLast          : "SkipLast",
         CG.kCGImageAlphaNoneSkipFirst         : "SkipFirst",
   }


class OSXRootWindowModel(RootWindowModel):

    def get_image(self, x, y, width, height, logger=None):
        return get_CG_imagewrapper()

    def take_screenshot(self):
        log("grabbing screenshot")
        return take_screenshot()


class ShadowServer(ShadowServerBase, GTKServerBase):

    def __init__(self):
        #sanity check:
        image = CG.CGWindowListCreateImage(CG.CGRectInfinite,
                    CG.kCGWindowListOptionOnScreenOnly,
                    CG.kCGNullWindowID,
                    CG.kCGWindowImageDefault)
        if image is None:
            raise Exception("cannot grab test screenshot - maybe you need to run this command whilst logged in via the UI")
        ShadowServerBase.__init__(self, gtk.gdk.get_default_root_window())
        GTKServerBase.__init__(self)

    def init(self, opts):
        GTKServerBase.init(self, opts)
        self.keycodes = {}

    def makeRootWindowModel(self):
        return  OSXRootWindowModel(self.root)

    def _process_mouse_common(self, proto, wid, pointer, modifiers):
        CG.CGWarpMouseCursorPosition(pointer)

    def get_keycode(self, ss, client_keycode, keyname, modifiers):
        #no mapping yet...
        return client_keycode

    def fake_key(self, keycode, press):
        log.info("fake_key(%s, %s)", keycode, press)
        e = CG.CGEventCreateKeyboardEvent(None, keycode, press)
        #CGEventSetFlags(keyPress, modifierFlags)
        #modifierFlags: kCGEventFlagMaskShift, ...
        CG.CGEventPost(CG.kCGSessionEventTap, e)
        CG.CFRelease(e)

    def _process_button_action(self, proto, packet):
        wid, button, pressed, pointer, modifiers = packet[1:6]
        log("process_button_action(%s, %s)", proto, packet)
        self._process_mouse_common(proto, wid, pointer, modifiers)
        if button<=3:
            #we should be using CGEventCreateMouseEvent
            #instead we clear previous clicks when a "higher" button is pressed... oh well
            args = []
            for i in range(button):
                args.append(i==(button-1) and pressed)
            log("CG.CGPostMouseEvent(%s, %s, %s, %s)", pointer, 1, button, args)
            CG.CGPostMouseEvent(pointer, 1, button, *args)
        else:
            if not pressed:
                #we don't simulate press/unpress
                #so just ignore unpressed events
                return
            wheel = (button-2)//2
            direction = 1-(((button-2) % 2)*2)
            args = []
            for i in range(wheel):
                if i!=(wheel-1):
                    args.append(0)
                else:
                    args.append(direction)
            log("CG.CGPostScrollWheelEvent(%s, %s)", wheel, args)
            CG.CGPostScrollWheelEvent(wheel, *args)

    def make_hello(self, source):
        capabilities = GTKServerBase.make_hello(self, source)
        capabilities["shadow"] = True
        capabilities["server_type"] = "Python/gtk2/osx-shadow"
        return capabilities

    def get_info(self, proto):
        info = GTKServerBase.get_info(self, proto)
        info["features.shadow"] = True
        info["server.type"] = "Python/gtk2/osx-shadow"
        return info
