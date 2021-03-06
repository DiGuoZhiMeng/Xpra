# coding=utf8
# This file is part of Parti.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2013 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Parti is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import gtk.gdk
import gobject

from xpra.server_base import ServerBase
from wimpiggy.lowlevel import (xtest_fake_key,              #@UnresolvedImport
                               xtest_fake_button,           #@UnresolvedImport
                               set_key_repeat_rate,         #@UnresolvedImport
                               unpress_all_keys,            #@UnresolvedImport
                               has_randr, get_screen_sizes, #@UnresolvedImport
                               set_screen_size,             #@UnresolvedImport
                               get_screen_size,             #@UnresolvedImport
                               get_xatom,                   #@UnresolvedImport
                               get_children,                #@UnresolvedImport
                               )
from wimpiggy.prop import prop_set
from xpra.server_uuid import save_uuid, get_uuid
from wimpiggy.error import XError, trap

from wimpiggy.log import Logger
log = Logger()

from xpra.xkbhelper import clean_keyboard_state
from xpra.xposix.xsettings import XSettingsManager

MAX_CONCURRENT_CONNECTIONS = 20


def window_name(window):
    from wimpiggy.prop import prop_get
    return prop_get(window, "_NET_WM_NAME", "utf8", True) or "unknown"

def window_info(window):
    from wimpiggy.prop import prop_get
    net_wm_name = prop_get(window, "_NET_WM_NAME", "utf8", True)
    return "%s %s (%s / %s)" % (net_wm_name, window, window.get_geometry(), window.is_visible())

def dump_windows():
    root = gtk.gdk.get_default_root_window()
    log("root window: %s" % root)
    children = get_children(root)
    log("%s windows" % len(children))
    for window in get_children(root):
        log("found window: %s", window_info(window))


class X11ServerBase(ServerBase):
    """
        Base class for X11 servers,
        adds X11 specific methods to ServerBase.
        (see XpraServer or XpraX11ShadowServer for actual implementations)
    """

    def __init__(self, clobber, sockets, opts):
        self.x11_init(clobber)

        ServerBase.__init__(self, clobber, sockets, opts)


    def x11_init(self, clobber):
        self.init_x11_atoms()
        self.randr = has_randr()
        if self.randr and len(get_screen_sizes())<=1:
            #disable randr when we are dealing with a Xvfb
            #with only one resolution available
            #since we don't support adding them on the fly yet
            self.randr = False
        if self.randr:
            display = gtk.gdk.display_get_default()
            i=0
            while i<display.get_n_screens():
                screen = display.get_screen(i)
                screen.connect("size-changed", self._screen_size_changed)
                i += 1
        log("randr enabled: %s", self.randr)

    def init_x11_atoms(self):
        #some applications (like openoffice), do not work properly
        #if some x11 atoms aren't defined, so we define them in advance:
        for atom_name in ["_NET_WM_WINDOW_TYPE",
                          "_NET_WM_WINDOW_TYPE_NORMAL",
                          "_NET_WM_WINDOW_TYPE_DESKTOP",
                          "_NET_WM_WINDOW_TYPE_DOCK",
                          "_NET_WM_WINDOW_TYPE_TOOLBAR",
                          "_NET_WM_WINDOW_TYPE_MENU",
                          "_NET_WM_WINDOW_TYPE_UTILITY",
                          "_NET_WM_WINDOW_TYPE_SPLASH",
                          "_NET_WM_WINDOW_TYPE_DIALOG",
                          "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
                          "_NET_WM_WINDOW_TYPE_POPUP_MENU",
                          "_NET_WM_WINDOW_TYPE_TOOLTIP",
                          "_NET_WM_WINDOW_TYPE_NOTIFICATION",
                          "_NET_WM_WINDOW_TYPE_COMBO",
                          "_NET_WM_WINDOW_TYPE_DND",
                          "_NET_WM_WINDOW_TYPE_NORMAL"
                          ]:
            get_xatom(atom_name)

    def init_keyboard(self):
        ServerBase.init_keyboard(self)
        #clear all modifiers
        clean_keyboard_state()


    def get_uuid(self):
        return get_uuid()

    def save_uuid(self):
        save_uuid(self.uuid)

    def set_keyboard_repeat(self, key_repeat):
        if key_repeat:
            self.key_repeat_delay, self.key_repeat_interval = key_repeat
            if self.key_repeat_delay>0 and self.key_repeat_interval>0:
                set_key_repeat_rate(self.key_repeat_delay, self.key_repeat_interval)
                log.info("setting key repeat rate from client: %sms delay / %sms interval", self.key_repeat_delay, self.key_repeat_interval)
        else:
            #dont do any jitter compensation:
            self.key_repeat_delay = -1
            self.key_repeat_interval = -1
            #but do set a default repeat rate:
            set_key_repeat_rate(500, 30)


    def make_hello(self):
        capabilities = ServerBase.make_hello(self)
        capabilities["resize_screen"] = self.randr
        return capabilities

    def do_get_info(self, proto, server_sources, window_ids):
        info = ServerBase.do_get_info(self, proto, server_sources, window_ids)
        info["server_type"] = "gtk-x11"
        return info


    def set_keymap(self, server_source, force=False):
        try:
            #prevent _keys_changed() from firing:
            #(using a flag instead of keymap.disconnect(handler) as this did not seem to work!)
            self.keymap_changing = True

            self.keyboard_config = server_source.set_keymap(self.keyboard_config, self.keys_pressed, force)
        finally:
            # re-enable via idle_add to give all the pending
            # events a chance to run first (and get ignored)
            def reenable_keymap_changes(*args):
                self.keymap_changing = False
                self._keys_changed()
            gobject.idle_add(reenable_keymap_changes)


    def _clear_keys_pressed(self):
        #make sure the timers don't fire and interfere:
        if len(self.keys_repeat_timers)>0:
            for timer in self.keys_repeat_timers.values():
                gobject.source_remove(timer)
            self.keys_repeat_timers = {}
        #clear all the keys we know about:
        if len(self.keys_pressed)>0:
            log("clearing keys pressed: %s", self.keys_pressed)
            for keycode in self.keys_pressed.keys():
                xtest_fake_key(gtk.gdk.display_get_default(), keycode, False)
            self.keys_pressed = {}
        #this will take care of any remaining ones we are not aware of:
        #(there should not be any - but we want to be certain)
        unpress_all_keys(gtk.gdk.display_get_default())


    def get_max_screen_size(self):
        max_w, max_h = gtk.gdk.get_default_root_window().get_size()
        sizes = get_screen_sizes()
        if self.randr and len(sizes)>=1:
            for w,h in sizes:
                max_w = max(max_w, w)
                max_h = max(max_h, h)
        return max_w, max_h


    def set_best_screen_size(self):
        """ sets the screen size to use the largest width and height used by any of the clients """
        root_w, root_h = gtk.gdk.get_default_root_window().get_size()
        if not self.randr:
            return root_w, root_h
        max_w, max_h = 0, 0
        sizes = []
        for ss in self._server_sources.values():
            client_size = ss.desktop_size
            if not client_size:
                continue
            sizes.append(client_size)
            w, h = client_size
            max_w = max(max_w, w)
            max_h = max(max_h, h)
        log.info("max client resolution is %sx%s (from %s), current server resolution is %sx%s", max_w, max_h, sizes, root_w, root_h)
        if max_w>0 and max_h>0:
            return self.set_screen_size(max_w, max_h)
        return  root_w, root_h

    def set_screen_size(self, desired_w, desired_h):
        root_w, root_h = gtk.gdk.get_default_root_window().get_size()
        if desired_w==root_w and desired_h==root_h:
            return    root_w,root_h    #unlikely: perfect match already!
        #try to find the best screen size to resize to:
        new_size = None
        for w,h in get_screen_sizes():
            if w<desired_w or h<desired_h:
                continue            #size is too small for client
            if new_size:
                ew,eh = new_size
                if ew*eh<w*h:
                    continue        #we found a better (smaller) candidate already
            new_size = w,h
        log("best resolution for client(%sx%s) is: %s", desired_w, desired_h, new_size)
        if not new_size:
            return  root_w, root_h
        w, h = new_size
        if w==root_w and h==root_h:
            log.info("best resolution matching %sx%s is unchanged: %sx%s", desired_w, desired_h, w, h)
            return  root_w, root_h
        try:
            set_screen_size(w, h)
            root_w, root_h = get_screen_size()
            if root_w!=w or root_h!=h:
                log.error("odd, failed to set the new resolution, "
                          "tried to set it to %sx%s and ended up with %sx%s", w, h, root_w, root_h)
            else:
                log.info("new resolution matching %sx%s : screen now set to %sx%s", desired_w, desired_h, root_w, root_h)
        except Exception, e:
            log.error("ouch, failed to set new resolution: %s", e, exc_info=True)
        return  root_w, root_h


    def update_server_settings(self, settings):
        ServerBase.update_server_settings(self, settings)
        old_settings = dict(self._settings)
        log("server_settings: old=%s, updating with=%s", old_settings, settings)
        self._settings.update(settings)
        root = gtk.gdk.get_default_root_window()
        for k, v in settings.items():
            #cook the "resource-manager" value to add the DPI:
            if k == "resource-manager" and self.dpi>0:
                value = v.decode("utf-8")
                #parse the resources into a dict:
                values={}
                options = value.split("\n")
                for option in options:
                    if not option:
                        continue
                    parts = option.split(":\t")
                    if len(parts)!=2:
                        continue
                    values[parts[0]] = parts[1]
                values["Xft.dpi"] = self.dpi
                values["gnome.Xft/DPI"] = self.dpi*1024
                log("server_settings: resource-manager values=%s", values)
                #convert the dict back into a resource string:
                value = ''
                for vk, vv in values.items():
                    value += "%s:\t%s\n" % (vk, vv)
                value += '\n'
                #record the actual value used
                self._settings["resource-manager"] = value
                v = value.encode("utf-8")

            if k not in old_settings or v != old_settings[k]:
                def root_set(p):
                    log("server_settings: setting %s to %s", p, v)
                    prop_set(root, p, "latin1", v.decode("utf-8"))
                if k == "xsettings-blob":
                    self._xsettings_manager = XSettingsManager(v)
                elif k == "resource-manager":
                    root_set("RESOURCE_MANAGER")
                elif self.pulseaudio:
                    if k == "pulse-cookie":
                        root_set("PULSE_COOKIE")
                    elif k == "pulse-id":
                        root_set("PULSE_ID")
                    elif k == "pulse-server":
                        root_set("PULSE_SERVER")


    def fake_key(self, keycode, press):
        trap.call_synced(xtest_fake_key, gtk.gdk.display_get_default(), keycode, press)


    def _move_pointer(self, pos):
        x, y = pos
        display = gtk.gdk.display_get_default()
        display.warp_pointer(display.get_default_screen(), x, y)

    def _process_mouse_common(self, proto, wid, pointer, modifiers):
        ss = self._server_sources.get(proto)
        if ss is None:
            return
        ss.make_keymask_match(modifiers)
        window = self._id_to_window.get(wid)
        if not window:
            log("_process_mouse_common() invalid window id: %s", wid)
            return
        trap.swallow_synced(self._move_pointer, pointer)

    def _process_button_action(self, proto, packet):
        ss = self._server_sources.get(proto)
        if ss is None:
            return
        wid, button, pressed, pointer, modifiers = packet[1:6]
        self._process_mouse_common(proto, wid, pointer, modifiers)
        ss.user_event()
        display = gtk.gdk.display_get_default()
        try:
            trap.call_synced(xtest_fake_button, display, button, pressed)
        except XError:
            log.warn("Failed to pass on (un)press of mouse button %s"
                     + " (perhaps your Xvfb does not support mousewheels?)",
                     button)
