# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""The magic GTK widget that represents a client window.

Most of the gunk required to be a valid window manager (reparenting, synthetic
events, mucking about with properties, etc. etc.) is wrapped up in here."""

# Maintain compatibility with old versions of Python, while avoiding a
# deprecation warning on new versions:
import os
from socket import gethostname

import gobject
import gtk.gdk
import cairo
import signal

from xpra.util import nonl, WORKSPACE_UNSET, WORKSPACE_ALL
from xpra.x11.bindings.window_bindings import constants, X11WindowBindings #@UnresolvedImport
X11Window = X11WindowBindings()

from xpra.x11.gtk_x11.gdk_bindings import (
                get_pyatom,                                 #@UnresolvedImport
                get_pywindow,                               #@UnresolvedImport
                add_event_receiver,                         #@UnresolvedImport
                remove_event_receiver,                      #@UnresolvedImport
                get_display_for,                            #@UnresolvedImport
                calc_constrained_size,                      #@UnresolvedImport
               )
from xpra.x11.gtk_x11.send_wm import (
                send_wm_take_focus,
                send_wm_delete_window)
from xpra.gtk_common.gobject_util import (AutoPropGObjectMixin,
                           one_arg_signal,
                           non_none_list_accumulator)
from xpra.gtk_common.error import XError, xsync, xswallow
from xpra.x11.gtk_x11.prop import prop_get, prop_set
from xpra.x11.gtk_x11.composite import CompositeHelper

from xpra.log import Logger
log = Logger("x11", "window")
focuslog = Logger("x11", "window", "focus")
grablog = Logger("x11", "window", "grab")
iconlog = Logger("x11", "window", "icon")
workspacelog = Logger("x11", "window", "workspace")


_NET_WM_STATE_REMOVE = 0
_NET_WM_STATE_ADD    = 1
_NET_WM_STATE_TOGGLE = 2

XNone = constants["XNone"]

CWX             = constants["CWX"]
CWY             = constants["CWY"]
CWWidth         = constants["CWWidth"]
CWHeight        = constants["CWHeight"]
CWBorderWidth   = constants["CWBorderWidth"]
CWSibling       = constants["CWSibling"]
CWStackMode     = constants["CWStackMode"]
CONFIGURE_GEOMETRY_MASK = CWX | CWY | CWWidth | CWHeight

IconicState = constants["IconicState"]
NormalState = constants["NormalState"]

# grab stuff:
NotifyNormal        = constants["NotifyNormal"]
NotifyGrab          = constants["NotifyGrab"]
NotifyUngrab        = constants["NotifyUngrab"]
NotifyWhileGrabbed  = constants["NotifyWhileGrabbed"]
NotifyNonlinearVirtual = constants["NotifyNonlinearVirtual"]
GRAB_CONSTANTS = {
                  NotifyNormal          : "NotifyNormal",
                  NotifyGrab            : "NotifyGrab",
                  NotifyUngrab          : "NotifyUngrab",
                  NotifyWhileGrabbed    : "NotifyWhileGrabbed",
                 }
DETAIL_CONSTANTS    = {}
for x in ("NotifyAncestor", "NotifyVirtual", "NotifyInferior",
          "NotifyNonlinear", "NotifyNonlinearVirtual", "NotifyPointer",
          "NotifyPointerRoot", "NotifyDetailNone"):
    DETAIL_CONSTANTS[constants[x]] = x
grablog("pointer grab constants: %s", GRAB_CONSTANTS)
grablog("detail constants: %s", DETAIL_CONSTANTS)


#if you want to use a virtual screen bigger than 32767x32767
#you will need to change those values, but some broken toolkits
#will then misbehave (they use signed shorts instead of signed ints..)
MAX_WINDOW_SIZE = 2**15-1
MAX_ASPECT = 2**15-1
USE_XSHM = os.environ.get("XPRA_XSHM", "1")=="1"
FORCE_QUIT = os.environ.get("XPRA_FORCE_QUIT", "1")=="1"

#these properties are not handled, and we don't want to spam the log file
#whenever an app decides to change them:
PROPERTIES_IGNORED = ("_NET_WM_OPAQUE_REGION", )
#make it easier to debug property changes, just add them here:
PROPERTIES_DEBUG = {}   #ie: {"WM_PROTOCOLS" : ["atom"]}


#add user friendly workspace logging:
WORKSPACE_STR = {WORKSPACE_UNSET    : "UNSET",
                 WORKSPACE_ALL      : "ALL"}
def workspacestr(w):
    return WORKSPACE_STR.get(w, w)


def sanestr(s):
    return (s or "").strip("\0").replace("\0", " ")


# Todo:
#   client focus hints
#   _NET_WM_SYNC_REQUEST
#   root window requests (pagers, etc. requesting to change client states)
#   _NET_WM_PING/detect window not responding (also a root window message)

# Okay, we need a block comment to explain the window arrangement that this
# file is working with.
#
#                +--------+
#                | widget |
#                +--------+
#                  /    \
#  <- top         /     -\-        bottom ->
#                /        \
#          +-------+       |
#          | image |  +---------+
#          +-------+  | corral  |
#                     +---------+
#                          |
#                     +---------+
#                     | client  |
#                     +---------+
#
# Each box in this diagram represents one X/GDK window.  In the common case,
# every window here takes up exactly the same space on the screen (!).  In
# fact, the two windows on the right *always* have exactly the same size and
# location, and the window on the left and the top window also always have
# exactly the same size and position.  However, each window in the diagram
# plays a subtly different role.
#
# The client window is obvious -- this is the window owned by the client,
# which they created and which we have various ICCCM/EWMH-mandated
# responsibilities towards.  It is also composited.
#
# The purpose of the 'corral' is to keep the client window managed -- we
# select for SubstructureRedirect on it, so that the client cannot resize
# etc. without going through the WM.
#
# These two windows are always managed together, as a unit; an invariant of
# the code is that they always take up exactly the same space on the screen.
# They get reparented back and forth between widgets, and when there are no
# widgets, they get reparented to a "parking area".  For now, we're just using
# the root window as a parking area, so we also map/unmap the corral window
# depending on whether we are parked or not; the corral and window is left
# mapped at all times.
#
# When a particular WindowView controls the underlying client window, then two
# things happen:
#   -- Its size determines the size of the client window.  Ideally they are
#      the same size -- but this is not always the case, because the client
#      may have specified sizing constraints, in which case the client window
#      is the "best fit" to the controlling widget window.
#   -- The client window and its corral are reparented under the widget
#      window, as in the diagram above.  This is necessary to allow mouse
#      events to work -- a WindowView widget can always *look* like the client
#      window is there, through the magic of Composite, but in order for it to
#      *act* like the client window is there in terms of receiving mouse
#      events, it has to actually be there.
#
# We should also have a block comment describing how to create a
# view/"controller" for a WindowModel.
#
# Viewing a (Base)WindowModel is easy.  Connect to the client-contents-changed
# signal.  Every time the window contents is updated, you'll get a message.
# This message is passed a single object e, which has useful members:
#   e.x, e.y, e.width, e.height:
#      The part of the client window that was modified, and needs to be
#      redrawn.
# To get the actual contents of the window to draw, there is a "handle"
# available as the "contents-handle" property on the Composite window.
#
# But what if you'd like to do more than just look at your pretty composited
# windows?  Maybe you'd like to, say, *interact* with them?  Then life is a
# little more complicated.  To make a view "live", we have to move the actual
# client window to be a child of your view window and position it correctly.
# Obviously, only one view can be live at any given time, so we have to figure
# out which one that is.  Supposing we have a WindowModel called "model" and
# a view called "view", then the following pieces come into play:
#   The "ownership-election" signal on window:
#     If a view wants the chance to become live, it must connect to this
#     signal.  When the signal is emitted, its handler should return a tuple
#     of the form:
#       (votes, my_view)
#     Just like a real election, everyone votes for themselves.  The view that
#     gives the highest value to 'votes' becomes the new owner.  However, a
#     view with a negative (< 0) votes value will never become the owner.
#   model.ownership_election():
#     This method (distinct from the ownership-election signal!) triggers an
#     election.  All views MUST call this method whenever they decide their
#     number of votes has changed.  All views MUST call this method when they
#     are destructing themselves (ideally after disconnecting from the
#     ownership-election signal).
#   The "owner" property on window:
#     This records the view that currently owns the window (i.e., the winner
#     of the last election), or None if no view is live.
#   view.take_window(model, window):
#     This method is called on 'view' when it becomes owner of 'model'.  It
#     should reparent 'window' into the appropriate place, and put it at the
#     appropriate place in its window stack.  (The x,y position, however, does
#     not matter.)
#   view.window_size(model):
#     This method is called when the model needs to know how much space it is
#     allocated.  It should return the maximum (width, height) allowed.
#     (However, the model may choose to use less than this.)
#   view.window_position(mode, width, height):
#     This method is called when the model needs to know where it should be
#     located (relative to the parent window the view placed it in).  'width'
#     and 'height' are the size the model window will actually be.  It should
#     return the (x, y) position desired.
#   model.maybe_recalculate_geometry_for(view):
#     This method (potentially) triggers a resize/move of the client window
#     within the view.  If 'view' is not the current owner, is a no-op, which
#     means that views can call it without worrying about whether they are in
#     fact the current owner.
#
# The actual method for choosing 'votes' is not really determined yet.
# Probably it should take into account at least the following factors:
#   -- has focus (or has mouse-over?)
#   -- is visible in a tray/other window, and the tray/other window is visible
#      -- and is focusable
#      -- and is not focusable
#   -- is visible in a tray, and the tray/other window is not visible
#      -- and is focusable
#      -- and is not focusable
#      (NB: Widget.get_ancestor(my.Tray) will give us the nearest ancestor
#      that isinstance(my.Tray), if any.)
#   -- is not visible
#   -- the size of the widget (as a final tie-breaker)

class Unmanageable(Exception):
    pass

class BaseWindowModel(AutoPropGObjectMixin, gobject.GObject):
    __gproperties__ = {
        "client-window": (gobject.TYPE_PYOBJECT,
                          "gtk.gdk.Window representing the client toplevel", "",
                          gobject.PARAM_READABLE),
        "geometry": (gobject.TYPE_PYOBJECT,
                     "current (border-corrected, relative to parent) coordinates (x, y, w, h) for the window", "",
                     gobject.PARAM_READABLE),
        "transient-for": (gobject.TYPE_PYOBJECT,
                          "Transient for (or None)", "",
                          gobject.PARAM_READABLE),
        "pid": (gobject.TYPE_INT,
                "PID of owning process", "",
                -1, 65535, -1,
                gobject.PARAM_READABLE),
        "opacity": (gobject.TYPE_INT64,
                "Opacity", "",
                -1, 0xffffffff, -1,
                gobject.PARAM_READABLE),
        "xid": (gobject.TYPE_INT,
                "X11 window id", "",
                -1, 65535, -1,
                gobject.PARAM_READABLE),
        "title": (gobject.TYPE_PYOBJECT,
                  "Window title (unicode or None)", "",
                  gobject.PARAM_READABLE),
        "group-leader": (gobject.TYPE_PYOBJECT,
                         "Window group leader as a pair: (xid, gdk window)", "",
                         gobject.PARAM_READABLE),
        "attention-requested": (gobject.TYPE_BOOLEAN,
                                "Urgency hint from client, or us", "",
                                False,
                                gobject.PARAM_READWRITE),
        "can-focus": (gobject.TYPE_BOOLEAN,
                      "Does this window ever accept keyboard input?", "",
                      True,
                      gobject.PARAM_READWRITE),
        "has-alpha": (gobject.TYPE_BOOLEAN,
                       "Does the window use transparency", "",
                       False,
                       gobject.PARAM_READABLE),
        "bypass-compositor": (gobject.TYPE_INT,
                       "hint that the window would benefit from running uncomposited ", "",
                       0, 2, 0,
                       gobject.PARAM_READABLE),
        "fullscreen": (gobject.TYPE_BOOLEAN,
                       "Fullscreen-ness of window", "",
                       False,
                       gobject.PARAM_READWRITE),
        "fullscreen-monitors": (gobject.TYPE_PYOBJECT,
                         "List of 4 monitor indices indicating the top, bottom, left, and right edges of the window when the fullscreen state is enabled", "",
                         gobject.PARAM_READABLE),
        "maximized": (gobject.TYPE_BOOLEAN,
                       "Is the window maximized", "",
                       False,
                       gobject.PARAM_READWRITE),
        "above": (gobject.TYPE_BOOLEAN,
                       "Is the window on top of most windows", "",
                       False,
                       gobject.PARAM_READWRITE),
        "below": (gobject.TYPE_BOOLEAN,
                       "Is the window below most windows", "",
                       False,
                       gobject.PARAM_READWRITE),
        "shaded": (gobject.TYPE_BOOLEAN,
                       "Is the window shaded", "",
                       False,
                       gobject.PARAM_READWRITE),
        "skip-taskbar": (gobject.TYPE_BOOLEAN,
                       "Should the window be included on a taskbar", "",
                       False,
                       gobject.PARAM_READWRITE),
        "skip-pager": (gobject.TYPE_BOOLEAN,
                       "Should the window be included on a pager", "",
                       False,
                       gobject.PARAM_READWRITE),
        "sticky": (gobject.TYPE_BOOLEAN,
                       "Is the window's position fixed on the screen", "",
                       False,
                       gobject.PARAM_READWRITE),
        "strut": (gobject.TYPE_PYOBJECT,
                  "Struts requested by window, or None", "",
                  gobject.PARAM_READABLE),
        "workspace": (gobject.TYPE_UINT,
                "The workspace this window is on", "",
                0, 2**32-1, WORKSPACE_UNSET,
                gobject.PARAM_READWRITE),
        "override-redirect": (gobject.TYPE_BOOLEAN,
                       "Is the window of type override-redirect", "",
                       False,
                       gobject.PARAM_READABLE),
        "tray": (gobject.TYPE_BOOLEAN,
                       "Is the window a system tray icon", "",
                       False,
                       gobject.PARAM_READABLE),
        "role" : (gobject.TYPE_PYOBJECT,
                          "The window's role (ICCCM session management)", "",
                          gobject.PARAM_READABLE),
        "modal": (gobject.TYPE_PYOBJECT,
                          "Modal (boolean)", "",
                          gobject.PARAM_READWRITE),
        "window-type": (gobject.TYPE_PYOBJECT,
                        "Window type",
                        "NB, most preferred comes first, then fallbacks",
                        gobject.PARAM_READABLE),
        }
    __gsignals__ = {
        "client-contents-changed": one_arg_signal,
        "raised"                : one_arg_signal,
        "unmanaged"             : one_arg_signal,
        "initiate-moveresize"   : one_arg_signal,

        "grab"                  : one_arg_signal,
        "ungrab"                : one_arg_signal,
# these signals must be defined in the subclasses to be seen by the event stuff:
#        "xpra-configure-event": one_arg_signal,
#        "xpra-focus-in-event"   : one_arg_signal,
#        "xpra-focus-out-event"  : one_arg_signal,
        }

    def __init__(self, client_window):
        log("new window %#x", client_window.xid)
        super(BaseWindowModel, self).__init__()
        self.client_window = client_window
        self.client_window_saved_events = self.client_window.get_events()
        self._managed = False
        self._managed_handlers = []
        self._setup_done = False
        self._input_field = True            # The WM_HINTS input field
        self._geometry = None
        self._damage_forward_handle = None
        self._internal_set_property("client-window", client_window)
        use_xshm = USE_XSHM and (not self.is_OR() and not self.is_tray())
        self._composite = CompositeHelper(self.client_window, False, use_xshm)
        self.property_names = ["pid", "transient-for", "fullscreen", "fullscreen-monitors", "bypass-compositor", "maximized", "window-type", "role", "group-leader",
                               "xid", "workspace", "has-alpha", "opacity", "strut"]

    def get_property_names(self):
        return self.property_names

    def get_dynamic_property_names(self):
        return ("title", "size-hints", "fullscreen", "fullscreen-monitors", "bypass-compositor", "maximized", "opacity", "workspace", "strut")


    def managed_connect(self, detailed_signal, handler, *args):
        """ connects a signal handler and makes sure we will clean it up on unmanage() """
        handler_id = self.connect(detailed_signal, handler, *args)
        self._managed_handlers.append(handler_id)
        return handler_id

    def managed_disconnect(self):
        for handler_id in self._managed_handlers:
            self.disconnect(handler_id)
        self._managed_handlers = []

    def call_setup(self):
        try:
            with xsync:
                self._geometry = X11Window.geometry_with_border(self.client_window.xid)
                self._read_initial_X11_properties()
        except XError as e:
            raise Unmanageable(e)
        add_event_receiver(self.client_window, self)
        # Keith Packard says that composite state is undefined following a
        # reparent, so I'm not sure doing this here in the superclass,
        # before we reparent, actually works... let's wait and see.
        try:
            with xsync:
                self._composite.setup()
        except XError as e:
            remove_event_receiver(self.client_window, self)
            log("window %#x does not support compositing: %s", self.client_window.xid, e)
            with xswallow:
                self._composite.destroy()
            self._composite = None
            raise Unmanageable(e)
        #compositing is now enabled, from now on we need to call setup_failed to clean things up
        self._managed = True
        try:
            with xsync:
                self.setup()
        except XError as e:
            try:
                with xsync:
                    self.setup_failed(e)
            except Exception as ex:
                log.error("error in cleanup handler: %s", ex)
            raise Unmanageable(e)
        self._setup_done = True

    def setup_failed(self, e):
        log("cannot manage %#x: %s", self.client_window.xid, e)
        self.do_unmanaged(False)

    def setup(self):
        # Start listening for important events.
        self.client_window.set_events(self.client_window_saved_events
                                      | gtk.gdk.STRUCTURE_MASK
                                      | gtk.gdk.PROPERTY_CHANGE_MASK
                                      | gtk.gdk.FOCUS_CHANGE_MASK)
        h = self._composite.connect("contents-changed", self._forward_contents_changed)
        self._damage_forward_handle = h

    def prop_get(self, key, ptype, ignore_errors=None, raise_xerrors=False):
        # Utility wrapper for prop_get on the client_window
        # also allows us to ignore property errors during setup_client
        if ignore_errors is None and (not self._setup_done or not self._managed):
            ignore_errors = True
        return prop_get(self.client_window, key, ptype, ignore_errors=bool(ignore_errors), raise_xerrors=raise_xerrors)

    def is_managed(self):
        return self._managed

    def is_shadow(self):
        return False

    def get_default_window_icon(self):
        return None


    def _forward_contents_changed(self, obj, event):
        if self._managed:
            self.emit("client-contents-changed", event)

    def acknowledge_changes(self):
        assert self._composite, "composite window destroyed outside the UI thread?"
        self._composite.acknowledge_changes()

    ################################
    # Property reading
    ################################

    def do_xpra_property_notify_event(self, event):
        assert event.window is self.client_window
        self._handle_property_change(str(event.atom))

    _property_handlers = {}

    def _handle_property_change(self, name):
        log("Property changed on %#x: %s", self.client_window.xid, name)
        if name in PROPERTIES_DEBUG:
            log.info("%s=%s", name, self.prop_get(name, PROPERTIES_DEBUG[name], True, False))
        if name in PROPERTIES_IGNORED:
            return
        self._call_property_handler(name)

    def _call_property_handler(self, name):
        if name in self._property_handlers:
            self._property_handlers[name](self)

    def do_xpra_configure_event(self, event):
        if self.client_window is None or not self._managed:
            return
        #shouldn't the border width always be 0?
        geom = (event.x, event.y, event.width, event.height, event.border_width)
        log("BaseWindowModel.do_xpra_configure_event(%s) client_window=%#x, old geometry=%s, new geometry=%s", event, self.client_window.xid, self._geometry, geom)
        if geom!=self._geometry:
            self._geometry = geom
            #X11Window.MoveResizeWindow(self.client_window.xid, )
            self.notify("geometry")

    def do_get_property_geometry(self, pspec):
        if self._geometry is None:
            with xsync:
                xwin = self.client_window.xid
                self._geometry = X11Window.geometry_with_border(xwin)
                log("BaseWindowModel.do_get_property_geometry() synced update: geometry(%#x)=%s", xwin, self._geometry)
        x, y, w, h, b = self._geometry
        return (x, y, w + 2*b, h + 2*b)

    def get_position(self):
        return self.do_get_property_geometry(None)[:2]

    def unmanage(self, exiting=False):
        if self._managed:
            self.emit("unmanaged", exiting)

    def do_unmanaged(self, wm_exiting):
        if not self._managed:
            return
        self._managed = False
        log("do_unmanaged(%s) damage_forward_handle=%s, composite=%s", wm_exiting, self._damage_forward_handle, self._composite)
        remove_event_receiver(self.client_window, self)
        gobject.idle_add(self.managed_disconnect)
        if self._composite:
            if self._damage_forward_handle:
                self._composite.disconnect(self._damage_forward_handle)
                self._damage_forward_handle = None
            self._composite.destroy()
            self._composite = None

    def _read_initial_properties(self):
        transient_for = self.prop_get("WM_TRANSIENT_FOR", "window")
        # May be None
        self._internal_set_property("transient-for", transient_for)

        window_types = self.prop_get("_NET_WM_WINDOW_TYPE", ["atom"])
        if not window_types:
            window_type = self._guess_window_type(transient_for)
            window_types = [gtk.gdk.atom_intern(window_type)]
        #normalize them (hide _NET_WM_WINDOW_TYPE prefix):
        window_types = [str(wt).replace("_NET_WM_WINDOW_TYPE_", "").replace("_NET_WM_TYPE_", "") for wt in window_types]
        self._internal_set_property("window-type", window_types)
        self._internal_set_property("xid", self.client_window.xid)
        self._internal_set_property("pid", self.prop_get("_NET_WM_PID", "u32") or -1)
        self._internal_set_property("role", self.prop_get("WM_WINDOW_ROLE", "latin1"))
        for mutable in ["WM_NAME", "_NET_WM_NAME", "_NET_WM_WINDOW_OPACITY", "_NET_WM_DESKTOP",
                        "_NET_WM_BYPASS_COMPOSITOR", "_NET_WM_FULLSCREEN_MONITORS",
                        "_NET_WM_STRUT", "_NET_WM_STRUT_PARTIAL"]:
            self._call_property_handler(mutable)

    def _read_initial_X11_properties(self):
        self._internal_set_property("has-alpha", X11Window.get_depth(self.client_window.xid)==32)


    def _handle_workspace_change(self):
        workspace = self.prop_get("_NET_WM_DESKTOP", "u32", True)
        if workspace is None:
            workspace = WORKSPACE_UNSET
        workspacelog("_NET_WM_DESKTOP=%s for window %#x", workspacestr(workspace), self.client_window.xid)
        self._internal_set_property("workspace", workspace)
    _property_handlers["_NET_WM_DESKTOP"] = _handle_workspace_change

    def move_to_workspace(self, workspace):
        #we send a message to ourselves, we could also just update the property
        current = self.get_property("workspace")
        if current==workspace:
            workspacelog("move_to_workspace(%s) unchanged", workspacestr(workspace))
            return
        workspacelog("move_to_workspace(%s) current=%s", workspacestr(workspace), workspacestr(current))
        with xswallow:
            if workspace==WORKSPACE_UNSET:
                workspacelog("removing _NET_WM_DESKTOP property from window %#x", self.client_window.xid)
                X11Window.XDeleteProperty(self.client_window.xid, "_NET_WM_DESKTOP")
            else:
                workspacelog("setting _NET_WM_DESKTOP=%s on window %#x", workspacestr(workspace), self.client_window.xid)
                prop_set(self.client_window, "_NET_WM_DESKTOP", "u32", workspace)


    def _handle_fullscreen_monitors_change(self):
        fsm = self.prop_get("_NET_WM_FULLSCREEN_MONITORS", ["u32"], True)
        self._internal_set_property("fullscreen-monitors", fsm)
        log("fullscreen-monitors=%s", fsm)
    _property_handlers["_NET_WM_FULLSCREEN_MONITORS"] = _handle_fullscreen_monitors_change


    def _handle_bypass_compositor_change(self):
        bypass = self.prop_get("_NET_WM_BYPASS_COMPOSITOR", "u32", True) or 0
        self._internal_set_property("bypass-compositor", bypass)
        log("bypass-compositor=%s", bypass)
    _property_handlers["_NET_WM_BYPASS_COMPOSITOR"] = _handle_bypass_compositor_change


    def _handle_wm_strut(self):
        partial = self.prop_get("_NET_WM_STRUT_PARTIAL", "strut-partial")
        if partial is not None:
            self._internal_set_property("strut", partial)
            return
        full = self.prop_get("_NET_WM_STRUT", "strut")
        # Might be None:
        self._internal_set_property("strut", full)

    _property_handlers["_NET_WM_STRUT"] = _handle_wm_strut
    _property_handlers["_NET_WM_STRUT_PARTIAL"] = _handle_wm_strut


    def _handle_opacity_change(self):
        opacity = self.prop_get("_NET_WM_WINDOW_OPACITY", "u32", True) or -1
        self._internal_set_property("opacity", opacity)
    _property_handlers["_NET_WM_WINDOW_OPACITY"] = _handle_opacity_change

    def _handle_title_change(self):
        name = self.prop_get("_NET_WM_NAME", "utf8", True)
        if name is None:
            name = self.prop_get("WM_NAME", "latin1", True)
        self._internal_set_property("title", sanestr(name))

    _property_handlers["WM_NAME"] = _handle_title_change
    _property_handlers["_NET_WM_NAME"] = _handle_title_change

    def _handle_wm_hints(self):
        with xswallow:
            wm_hints = X11Window.getWMHints(self.client_window.xid)
        if wm_hints is None:
            return
        # GdkWindow or None
        group_leader = None
        if "window_group" in wm_hints:
            xid = wm_hints.get("window_group")
            try:
                group_leader = xid, get_pywindow(xid)
            except:
                group_leader = xid, None
        self._internal_set_property("group-leader", group_leader)

        if "urgency" in wm_hints:
            self.set_property("attention-requested", True)

        _input = wm_hints.get("input")
        log("wm_hints.input = %s", _input)
        #we only set this value once:
        #(input_field always starts as True, and we then set it to an int)
        if self._input_field is True and _input is not None:
            #keep the value as an int to differentiate from the start value:
            self._input_field = int(_input)
            if bool(self._input_field):
                self.notify("can-focus")

    _property_handlers["WM_HINTS"] = _handle_wm_hints

    def _guess_window_type(self, transient_for):
        if transient_for is not None:
            # EWMH says that even if it's transient-for, we MUST check to
            # see if it's override-redirect (and if so treat as NORMAL).
            # But we wouldn't be here if this was override-redirect.
            # (OverrideRedirectWindowModel overrides this method)
            return "_NET_WM_WINDOW_TYPE_DIALOG"
        return "_NET_WM_WINDOW_TYPE_NORMAL"

    def is_tray(self):
        return False

    def uses_XShm(self):
        return self._composite and self._composite.get_property("shm-handle") is not None

    def has_alpha(self):
        return self.get_property("has-alpha")

    def get_image(self, x, y, width, height, logger=log.debug):
        handle = self._composite.get_property("contents-handle")
        if handle is None:
            logger("get_image(..) pixmap is None for window %#x", self.client_window.xid)
            return  None

        #try XShm:
        try:
            #logger("get_image(%s, %s, %s, %s) geometry=%s", x, y, width, height, self._geometry[:4])
            shm = self._composite.get_property("shm-handle")
            #logger("get_image(..) XShm handle: %s, handle=%s, pixmap=%s", shm, handle, handle.get_pixmap())
            if shm is not None:
                with xsync:
                    shm_image = shm.get_image(handle.get_pixmap(), x, y, width, height)
                #logger("get_image(..) XShm image: %s", shm_image)
                if shm_image:
                    return shm_image
        except Exception as e:
            if type(e)==XError and e.msg=="BadMatch":
                logger("get_image(%s, %s, %s, %s) get_image BadMatch ignored (window already gone?)", x, y, width, height)
            else:
                log.warn("get_image(%s, %s, %s, %s) get_image %s", x, y, width, height, e, exc_info=True)

        try:
            w = min(handle.get_width(), width)
            h = min(handle.get_height(), height)
            if w!=width or h!=height:
                logger("get_image(%s, %s, %s, %s) clamped to pixmap dimensions: %sx%s", x, y, width, height, w, h)
            with xsync:
                return handle.get_image(x, y, w, h)
        except Exception as e:
            if type(e)==XError and e.msg=="BadMatch":
                logger("get_image(%s, %s, %s, %s) get_image BadMatch ignored (window already gone?)", x, y, width, height)
            else:
                log.warn("get_image(%s, %s, %s, %s) get_image %s", x, y, width, height, e, exc_info=True)
            return None

    def do_xpra_client_message_event(self, event):
        log("do_xpra_client_message_event(%s)", event)
        # FIXME
        # Need to listen for:
        #   _NET_CURRENT_DESKTOP
        #   _NET_REQUEST_FRAME_EXTENTS
        #   _NET_WM_PING responses
        # and maybe:
        #   _NET_RESTACK_WINDOW
        #   _NET_WM_STATE (more fully)
        def update_wm_state(prop):
            current = self.get_property(prop)
            if event.data[0]==_NET_WM_STATE_ADD:
                v = True
            elif event.data[0]==_NET_WM_STATE_REMOVE:
                v = False
            elif event.data[0]==_NET_WM_STATE_TOGGLE:
                v = not bool(current)
            else:
                log.warn("invalid mode for _NET_WM_STATE: %s", event.data[0])
                return
            log("do_xpra_client_message_event(%s) window %s=%s (current state=%s)", event, prop, v, current)
            if v!=current:
                self.set_property(prop, v)

        if not event.data or len(event.data)!=5:
            log.warn("invalid event data: %s", event.data)
            return

        if event.message_type=="_NET_WM_STATE":
            atom1 = get_pyatom(event.window, event.data[1])
            log("_NET_WM_STATE: %s", atom1)
            if atom1=="_NET_WM_STATE_FULLSCREEN":
                update_wm_state("fullscreen")
            elif atom1=="_NET_WM_STATE_ABOVE":
                update_wm_state("above")
            elif atom1=="_NET_WM_STATE_BELOW":
                update_wm_state("below")
            elif atom1=="_NET_WM_STATE_SHADED":
                update_wm_state("shaded")
            elif atom1=="_NET_WM_STATE_STICKY":
                update_wm_state("sticky")
            elif atom1=="_NET_WM_STATE_SKIP_TASKBAR":
                update_wm_state("skip-taskbar")
            elif atom1=="_NET_WM_STATE_SKIP_PAGER":
                update_wm_state("skip-pager")
                get_pyatom(event.window, event.data[2])
            elif atom1 in ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"):
                atom2 = get_pyatom(event.window, event.data[2])
                #we only have one state for both, so we require both to be set:
                if atom1!=atom2 and atom2 in ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"):
                    update_wm_state("maximized")
            elif atom1=="_NET_WM_STATE_HIDDEN":
                log("ignoring 'HIDDEN' _NET_WM_STATE: %s", event)
                #we don't honour those because they make little sense, see:
                #https://mail.gnome.org/archives/wm-spec-list/2005-May/msg00004.html
                pass
            elif atom1=="_NET_WM_STATE_MODAL":
                update_wm_state("modal")
            else:
                log.info("do_xpra_client_message_event(%s) unhandled atom=%s", event, atom1)
        elif event.message_type=="WM_CHANGE_STATE":
            log("WM_CHANGE_STATE: %s", event.data[0])
            if event.data[0]==IconicState and event.serial>self.last_unmap_serial and not self.is_OR():
                self._internal_set_property("iconic", True)
        elif event.message_type=="_NET_WM_MOVERESIZE":
            log("_NET_WM_MOVERESIZE: %s", event)
            self.emit("initiate-moveresize", event)
        elif event.message_type=="_NET_MOVERESIZE_WINDOW":
            log("ignoring _NET_MOVERESIZE_WINDOW on %s (data=%s)", self, event.data)
        elif event.message_type=="_NET_ACTIVE_WINDOW" and event.data[0] in (0, 1):
            log("_NET_ACTIVE_WINDOW: %s", event)
            self.set_active()
            self.emit("raised", event)
        elif event.message_type=="_NET_CLOSE_WINDOW":
            log.info("_NET_CLOSE_WINDOW received by %s", self)
            self.request_close()
        elif event.message_type=="_NET_WM_DESKTOP":
            workspace = int(event.data[0])
            #query the workspace count on the root window
            #since we cannot access Wm from here..
            root = self.client_window.get_screen().get_root_window()
            ndesktops = prop_get(root, "_NET_NUMBER_OF_DESKTOPS", "u32", ignore_errors=True)
            workspacelog("received _NET_WM_DESKTOP: workspace=%s, number of desktops=%s", workspacestr(workspace), ndesktops)
            if ndesktops>0 and (workspace in (WORKSPACE_UNSET, WORKSPACE_ALL) or (workspace>=0 and workspace<ndesktops)):
                self.move_to_workspace(workspace)
            else:
                workspacelog.warn("invalid _NET_WM_DESKTOP request: workspace=%s, number of desktops=%s", workspacestr(workspace), ndesktops)
        elif event.message_type=="_NET_WM_FULLSCREEN_MONITORS":
            log("_NET_WM_FULLSCREEN_MONITORS: %s", event)
            #TODO: we should validate the indexes instead of copying them blindly!
            monitors = [event.data[0], event.data[1], event.data[2], event.data[3]]
            log("_NET_WM_FULLSCREEN_MONITORS: monitors=%s", monitors)
            prop_set(self.client_window, "_NET_WM_FULLSCREEN_MONITORS", ["u32"], monitors)
        else:
            log("do_xpra_client_message_event(%s)", event)

    def set_active(self):
        prop_set(self.client_window.get_screen().get_root_window(), "_NET_ACTIVE_WINDOW", "u32", self.client_window.xid)


    def do_xpra_focus_in_event(self, event):
        grablog("focus_in_event(%s) mode=%s, detail=%s",
            event, GRAB_CONSTANTS.get(event.mode), DETAIL_CONSTANTS.get(event.detail, event.detail))
        if event.mode==NotifyNormal and event.detail==NotifyNonlinearVirtual:
            self.emit("raised", event)
        else:
            self.may_emit_grab(event)

    def do_xpra_focus_out_event(self, event):
        grablog("focus_out_event(%s) mode=%s, detail=%s",
            event, GRAB_CONSTANTS.get(event.mode), DETAIL_CONSTANTS.get(event.detail, event.detail))
        self.may_emit_grab(event)

    def may_emit_grab(self, event):
        if event.mode==NotifyGrab:
            grablog("emitting grab on %s", self)
            self.emit("grab", event)
        if event.mode==NotifyUngrab:
            grablog("emitting ungrab on %s", self)
            self.emit("ungrab", event)


gobject.type_register(BaseWindowModel)


# FIXME: EWMH says that O-R windows should set various properties just like
# ordinary managed windows; so some of that code should get pushed up into the
# superclass sooner or later.  When someone cares, presumably.
class OverrideRedirectWindowModel(BaseWindowModel):
    __gsignals__ = {
        "xpra-configure-event": one_arg_signal,
        "xpra-unmap-event": one_arg_signal,
        "xpra-client-message-event" : one_arg_signal,
        "xpra-property-notify-event": one_arg_signal,
        "xpra-focus-in-event"   : one_arg_signal,
        "xpra-focus-out-event"  : one_arg_signal,
        }

    def __init__(self, client_window):
        super(OverrideRedirectWindowModel, self).__init__(client_window)
        self.property_names.append("override-redirect")

    def setup(self):
        self._read_initial_properties()
        BaseWindowModel.setup(self)
        # So now if the window becomes unmapped in the future then we will
        # notice... but it might be unmapped already, and any event
        # already generated, and our request for that event is too late!
        # So double check now, *after* putting in our request:
        if not X11Window.is_mapped(self.client_window.xid):
            raise Unmanageable("window already unmapped")
        ch = self._composite.get_property("contents-handle")
        if ch is None:
            raise Unmanageable("failed to get damage handle")

    def _read_initial_properties(self):
        BaseWindowModel._read_initial_properties(self)
        self._internal_set_property("override-redirect", True)

    def _guess_window_type(self, transient_for):
        return "_NET_WM_WINDOW_TYPE_NORMAL"

    def do_xpra_unmap_event(self, event):
        self.unmanage()

    def get_dimensions(self):
        ww, wh = self._geometry[2:4]
        return ww, wh

    def is_OR(self):
        return  True

    def raise_window(self):
        self.client_window.raise_()

    def __repr__(self):
        return "OverrideRedirectWindowModel(%#x)" % self.client_window.xid


gobject.type_register(OverrideRedirectWindowModel)


class SystemTrayWindowModel(OverrideRedirectWindowModel):

    def __init__(self, client_window):
        OverrideRedirectWindowModel.__init__(self, client_window)
        self.property_names = ["pid", "role", "xid", "has-alpha", "tray", "title"]

    def is_tray(self):
        return  True

    def has_alpha(self):
        return  True

    def _read_initial_properties(self):
        BaseWindowModel._read_initial_properties(self)
        self._internal_set_property("tray", True)
        self._internal_set_property("has-alpha", True)

    def move_resize(self, x, y, width, height):
        #Used by clients to tell us where the tray is located on screen
        log("SystemTrayWindowModel.move_resize(%s, %s, %s, %s)", x, y, width, height)
        self.client_window.move_resize(x, y, width, height)
        border = self._geometry[4]
        self._geometry = (x, y, width, height, border)

    def __repr__(self):
        return "SystemTrayWindowModel(%#x)" % self.client_window.xid


class WindowModel(BaseWindowModel):
    """This represents a managed client window.  It allows one to produce
    widgets that view that client window in various ways."""

    _NET_WM_ALLOWED_ACTIONS = [
        "_NET_WM_ACTION_CLOSE",
        "_NET_WM_ACTION_MOVE",
        "_NET_WM_ACTION_RESIZE",
        "_NET_WM_ACTION_FULLSCREEN",
        "_NET_WM_ACTION_MINIMIZE",
        "_NET_WM_ACTION_SHADE",
        "_NET_WM_ACTION_STICK",
        "_NET_WM_ACTION_MAXIMIZE_HORZ",
        "_NET_WM_ACTION_MAXIMIZE_VERT",
        "_NET_WM_ACTION_CHANGE_DESKTOP",
        "_NET_WM_ACTION_ABOVE",
        "_NET_WM_ACTION_BELOW",
        ]

    __gproperties__ = {
        # Interesting properties of the client window, that will be
        # automatically kept up to date:
        "actual-size": (gobject.TYPE_PYOBJECT,
                        "Size of client window (actual (width,height))", "",
                        gobject.PARAM_READABLE),
        "user-friendly-size": (gobject.TYPE_PYOBJECT,
                               "Description of client window size for user", "",
                               gobject.PARAM_READABLE),
        "requested-position": (gobject.TYPE_PYOBJECT,
                               "Client-requested position on screen", "",
                               gobject.PARAM_READABLE),
        "requested-size": (gobject.TYPE_PYOBJECT,
                           "Client-requested size on screen", "",
                           gobject.PARAM_READABLE),
        "size-hints": (gobject.TYPE_PYOBJECT,
                       "Client hints on constraining its size", "",
                       gobject.PARAM_READABLE),
        "class-instance": (gobject.TYPE_PYOBJECT,
                           "Classic X 'class' and 'instance'", "",
                           gobject.PARAM_READABLE),
        "protocols": (gobject.TYPE_PYOBJECT,
                      "Supported WM protocols", "",
                      gobject.PARAM_READABLE),
        "client-machine": (gobject.TYPE_PYOBJECT,
                           "Host where client process is running", "",
                           gobject.PARAM_READABLE),
        "command": (gobject.TYPE_PYOBJECT,
                           "Command used to start or restart the client", "",
                           gobject.PARAM_READABLE),
        # Toggling this property does not actually make the window iconified,
        # i.e. make it appear or disappear from the screen -- it merely
        # updates the various window manager properties that inform the world
        # whether or not the window is iconified.
        "iconic": (gobject.TYPE_BOOLEAN,
                   "ICCCM 'iconic' state -- any sort of 'not on desktop'.", "",
                   False,
                   gobject.PARAM_READWRITE),
        "state": (gobject.TYPE_PYOBJECT,
                  "State, as per _NET_WM_STATE", "",
                  gobject.PARAM_READABLE),
        "icon-title": (gobject.TYPE_PYOBJECT,
                       "Icon title (unicode or None)", "",
                       gobject.PARAM_READABLE),
        "icon": (gobject.TYPE_PYOBJECT,
                 "Icon (local Cairo surface)", "",
                 gobject.PARAM_READABLE),
        "icon-pixmap": (gobject.TYPE_PYOBJECT,
                        "Icon (server Pixmap)", "",
                        gobject.PARAM_READABLE),

        "owner": (gobject.TYPE_PYOBJECT,
                  "Owner", "",
                  gobject.PARAM_READABLE),
        "decorations": (gobject.TYPE_BOOLEAN,
                       "Should the window decorations be shown", "",
                       True,
                       gobject.PARAM_READABLE),
        }
    __gsignals__ = {
        # X11 bell event:
        "bell": one_arg_signal,

        "ownership-election": (gobject.SIGNAL_RUN_LAST,
                               gobject.TYPE_PYOBJECT, (),
                               non_none_list_accumulator),
        "xpra-property-notify-event": one_arg_signal,
        "xpra-configure-event": one_arg_signal,
        "xpra-focus-in-event"   : one_arg_signal,
        "xpra-focus-out-event"  : one_arg_signal,

        "child-map-request-event": one_arg_signal,
        "child-configure-request-event": one_arg_signal,
        "xpra-client-message-event" : one_arg_signal,
        "xpra-unmap-event": one_arg_signal,
        "xpra-destroy-event": one_arg_signal,
        "xpra-xkb-event": one_arg_signal,
        }

    def __init__(self, parking_window, client_window):
        """Register a new client window with the WM.

        Raises an Unmanageable exception if this window should not be
        managed, for whatever reason.  ATM, this mostly means that the window
        died somehow before we could do anything with it."""

        super(WindowModel, self).__init__(client_window)
        self.parking_window = parking_window
        self.corral_window = None
        self.in_save_set = False
        self.client_reparented = False
        self.last_unmap_serial = 0
        self.kill_count = 0

        self.connect("notify::iconic", self._handle_iconic_update)

        self.property_names += ["title", "icon-title", "size-hints", "class-instance", "icon", "client-machine", "command",
                                "modal", "decorations",
                                "above", "below", "shaded", "sticky", "skip-taskbar", "skip-pager"]
        self.call_setup()

    def setup(self):
        BaseWindowModel.setup(self)

        x, y, w, h, _ = self.client_window.get_geometry()
        # We enable PROPERTY_CHANGE_MASK so that we can call
        # x11_get_server_time on this window.
        self.corral_window = gtk.gdk.Window(self.parking_window,
                                            x = x, y = y, width =w, height= h,
                                            window_type=gtk.gdk.WINDOW_CHILD,
                                            wclass=gtk.gdk.INPUT_OUTPUT,
                                            event_mask=gtk.gdk.PROPERTY_CHANGE_MASK,
                                            title = "CorralWindow-%#x" % self.client_window.xid)
        log("setup() corral_window=%#x", self.corral_window.xid)
        prop_set(self.corral_window, "_NET_WM_NAME", "utf8", u"Xpra-CorralWindow-%#x" % self.client_window.xid)
        X11Window.substructureRedirect(self.corral_window.xid)
        add_event_receiver(self.corral_window, self)

        # The child might already be mapped, in case we inherited it from
        # a previous window manager.  If so, we unmap it now, and save the
        # serial number of the request -- this way, when we get an
        # UnmapNotify later, we'll know that it's just from us unmapping
        # the window, not from the client withdrawing the window.
        if X11Window.is_mapped(self.client_window.xid):
            log("hiding inherited window")
            self.last_unmap_serial = X11Window.Unmap(self.client_window.xid)

        # Process properties
        self._read_initial_properties()
        self._write_initial_properties_and_setup()

        # For now, we never use the Iconic state at all.
        self._internal_set_property("iconic", False)

        log("setup() adding to save set")
        X11Window.XAddToSaveSet(self.client_window.xid)
        self.in_save_set = True

        log("setup() reparenting")
        X11Window.Reparent(self.client_window.xid, self.corral_window.xid, 0, 0)
        self.client_reparented = True

        log("setup() geometry")
        w,h = X11Window.getGeometry(self.client_window.xid)[2:4]
        hints = self.get_property("size-hints")
        self._sanitize_size_hints(hints)
        nw, nh = calc_constrained_size(w, h, hints)[:2]
        if nw>=MAX_WINDOW_SIZE or nh>=MAX_WINDOW_SIZE:
            #we can't handle windows that big!
            raise Unmanageable("window constrained size is too large: %sx%s (from client geometry: %s,%s with size hints=%s)" % (nw, nh, w, h, hints))
        log("setup() resizing windows to %sx%s", nw, nh)
        self.corral_window.resize(nw, nh)
        self.client_window.resize(nw, nh)
        self.client_window.show_unraised()
        #this is here to trigger X11 errors if any are pending
        #or if the window is deleted already:
        self.client_window.get_geometry()
        self._internal_set_property("actual-size", (nw, nh))

    def get_dynamic_property_names(self):
        return list(BaseWindowModel.get_dynamic_property_names(self))+["icon", "icon-title", "size-hints", "iconic", "decorations", "modal",
                                                                       "above", "below", "shaded", "sticky", "skip-taskbar", "skip-pager"]


    def is_OR(self):
        return  False

    def raise_window(self):
        self.corral_window.raise_()

    def get_dimensions(self):
        return  self.get_property("actual-size")


    def do_xpra_client_message_event(self, event):
        if event.message_type=="_NET_MOVERESIZE_WINDOW" and event.data and len(event.data)==5:
            #TODO: honour gravity, show source indication
            geom = self.corral_window.get_geometry()
            x, y, w, h, _ = geom
            if event.data[0] & 0x100:
                x = event.data[1]
            if event.data[0] & 0x200:
                y = event.data[2]
            if event.data[0] & 0x400:
                w = event.data[3]
            if event.data[0] & 0x800:
                h = event.data[4]
            #honour hints:
            hints = self.get_property("size-hints")
            w, h, _, _ = calc_constrained_size(w, h, hints)
            log("_NET_MOVERESIZE_WINDOW on %s (data=%s, current geometry=%s, new geometry=%s)", self, event.data, geom, (x,y,w,h))
            with xswallow:
                X11Window.configureAndNotify(self.client_window.xid, x, y, w, h)
            return
        BaseWindowModel.do_xpra_client_message_event(self, event)

    def do_xpra_xkb_event(self, event):
        log("WindowModel.do_xpra_xkb_event(%r)" % event)
        if event.type!="bell":
            log.error("WindowModel.do_xpra_xkb_event(%r) unknown event type: %s" % (event, event.type))
            return
        event.window_model = self
        self.emit("bell", event)

    def do_xpra_property_notify_event(self, event):
        if event.delivered_to is self.corral_window:
            return
        BaseWindowModel.do_xpra_property_notify_event(self, event)

    def do_child_map_request_event(self, event):
        # If we get a MapRequest then it might mean that someone tried to map
        # this window multiple times in quick succession, before we actually
        # mapped it (so that several MapRequests ended up queued up; FSF Emacs
        # 22.1.50.1 does this, at least).  It alternatively might mean that
        # the client is naughty and tried to map their window which is
        # currently not displayed.  In either case, we should just ignore the
        # request.
        log("do_child_map_request_event(%s)", event)

    def do_xpra_unmap_event(self, event):
        if event.delivered_to is self.corral_window or self.corral_window is None:
            return
        assert event.window is self.client_window
        # The client window got unmapped.  The question is, though, was that
        # because it was withdrawn/destroyed, or was it because we unmapped it
        # going into IconicState?
        #
        # At the moment, we never actually put windows into IconicState
        # (i.e. unmap them), except in the special case when we start up and
        # find windows that are already mapped.  So we only need to check
        # against that one serial number.
        #
        # Also, if we receive a *synthetic* UnmapNotify event, that always
        # means that the client has withdrawn the window (even if it was not
        # mapped in the first place) -- ICCCM section 4.1.4.
        log("do_xpra_unmap_event(%s) client window unmapped", event)
        if event.send_event or event.serial>self.last_unmap_serial:
            self.unmanage()

    def do_xpra_destroy_event(self, event):
        if event.delivered_to is self.corral_window or self.corral_window is None:
            return
        assert event.window is self.client_window
        # This is somewhat redundant with the unmap signal, because if you
        # destroy a mapped window, then a UnmapNotify is always generated.
        # However, this allows us to catch the destruction of unmapped
        # ("iconified") windows, and also catch any mistakes we might have
        # made with unmap heuristics.  I love the smell of XDestroyWindow in
        # the morning.  It makes for simple code:
        self.unmanage()

    SCRUB_PROPERTIES = ["WM_STATE",
                        "_NET_WM_STATE",
                        "_NET_FRAME_EXTENTS",
                        "_NET_WM_ALLOWED_ACTIONS",
                        ]

    def do_unmanaged(self, wm_exiting):
        log("unmanaging window: %s (%s - %s)", self, self.corral_window, self.client_window)
        self._internal_set_property("owner", None)
        if self.corral_window:
            remove_event_receiver(self.corral_window, self)
            with xswallow:
                for prop in WindowModel.SCRUB_PROPERTIES:
                    X11Window.XDeleteProperty(self.client_window.xid, prop)
            if self.client_reparented:
                self.client_window.reparent(gtk.gdk.get_default_root_window(), 0, 0)
                self.client_reparented = False
            self.client_window.set_events(self.client_window_saved_events)
            #it is now safe to destroy the corral window:
            self.corral_window.destroy()
            self.corral_window = None
            # It is important to remove from our save set, even after
            # reparenting, because according to the X spec, windows that are
            # in our save set are always Mapped when we exit, *even if those
            # windows are no longer inferior to any of our windows!* (see
            # section 10. Connection Close).  This causes "ghost windows", see
            # bug #27:
            if self.in_save_set:
                with xswallow:
                    X11Window.XRemoveFromSaveSet(self.client_window.xid)
                self.in_save_set = False
            with xswallow:
                X11Window.sendConfigureNotify(self.client_window.xid)
            if wm_exiting:
                self.client_window.show_unraised()
        BaseWindowModel.do_unmanaged(self, wm_exiting)

    def ownership_election(self):
        #returns True if we have updated the geometry
        candidates = self.emit("ownership-election")
        if candidates:
            rating, winner = sorted(candidates)[-1]
            if rating < 0:
                winner = None
        else:
            winner = None
        old_owner = self.get_property("owner")
        if old_owner is winner:
            return False
        if old_owner is not None:
            self.corral_window.hide()
            self.corral_window.reparent(self.parking_window, 0, 0)
        self._internal_set_property("owner", winner)
        if winner is not None:
            winner.take_window(self, self.corral_window)
            self._update_client_geometry()
            self.corral_window.show_unraised()
            return True
        with xswallow:
            X11Window.sendConfigureNotify(self.client_window.xid)
        return False

    def maybe_recalculate_geometry_for(self, maybe_owner):
        if maybe_owner and self.get_property("owner") is maybe_owner:
            self._update_client_geometry()

    def _sanitize_size_hints(self, size_hints):
        if size_hints is None:
            return
        for attr in ["min_aspect", "max_aspect"]:
            v = size_hints.get(attr)
            if v is not None:
                try:
                    f = float(v)
                except:
                    f = None
                if f is None or f>=MAX_ASPECT:
                    log.warn("clearing invalid aspect hint value for %s: %s", attr, v)
                    size_hints[attr] = -1.0
        for attr in ["max_size", "min_size", "base_size", "resize_inc",
                     "min_aspect_ratio", "max_aspect_ratio"]:
            v = size_hints.get(attr)
            if v is not None:
                try:
                    w,h = v
                except:
                    w,h = None,None
                if (w is None or h is None) or w>=MAX_WINDOW_SIZE or h>=MAX_WINDOW_SIZE:
                    log("clearing invalid size hint value for %s: %s", attr, v)
                    del size_hints[attr]
        #if max-size is smaller than min-size (bogus), clamp it..
        mins = size_hints.get("min_size")
        maxs = size_hints.get("max_size")
        if mins is not None and maxs is not None:
            minw,minh = mins
            maxw,maxh = maxs
            if minw<=0 and minh<=0:
                #doesn't do anything
                size_hints["min_size"] = None
            if maxw<=0 or maxh<=0:
                #doesn't mak sense!
                size_hints["max_size"] = None
            if maxw<minw or maxh<minh:
                size_hints["min_size"] = max(minw, maxw), max(minh, maxh)
                size_hints["max_size"] = size_hints.min_size
                log.warn("invalid min_size=%s / max_size=%s changed to: %s / %s",
                         mins, maxs, size_hints["min_size"], size_hints["max_size"])

    def _update_client_geometry(self):
        owner = self.get_property("owner")
        if owner is not None:
            log("_update_client_geometry: owner()=%s", owner)
            def window_size():
                return  owner.window_size(self)
            def window_position(w, h):
                return  owner.window_position(self, w, h)
            self._do_update_client_geometry(window_size, window_position)
        elif not self._setup_done:
            #try to honour initial size and position requests during setup:
            def window_size():
                return self.get_property("requested-size")
            def window_position(w=0, h=0):
                return self.get_property("requested-position")
            log("_update_client_geometry: using initial size=%s and position=%s", window_size(), window_position())
            self._do_update_client_geometry(window_size, window_position)

    def _do_update_client_geometry(self, window_size_cb, window_position_cb):
        allocated_w, allocated_h = window_size_cb()
        log("_do_update_client_geometry: %sx%s", allocated_w, allocated_h)
        hints = self.get_property("size-hints")
        log("_do_update_client_geometry: hints=%s", hints)
        size = calc_constrained_size(allocated_w, allocated_h, hints)
        log("_do_update_client_geometry: size=%s", size)
        w, h, wvis, hvis = size
        x, y = window_position_cb(w, h)
        log("_do_update_client_geometry: position=%s", (x,y))
        self.corral_window.move_resize(x, y, w, h)
        self._internal_set_property("actual-size", (w, h))
        self._internal_set_property("user-friendly-size", (wvis, hvis))
        with xswallow:
            X11Window.configureAndNotify(self.client_window.xid, 0, 0, w, h)

    def do_xpra_configure_event(self, event):
        log("WindowModel.do_xpra_configure_event(%s) corral=%#x, client=%#x", event, self.corral_window.xid, self.client_window.xid)
        if not self._managed:
            return
        if event.window!=self.client_window:
            #we only care about events on the client window
            return
        if self.corral_window is None or not self.corral_window.is_visible():
            return
        if self.client_window is None or not self.client_window.is_visible():
            return
        try:
            #workaround applications whose windows disappear from underneath us:
            with xsync:
                if self.resize_corral_window(event.x, event.y, event.width, event.height, event.border_width):
                    self.notify("geometry")
        except XError as e:
            log.warn("failed to resize corral window: %s", e)

    def resize_corral_window(self, x, y, w, h, border):
        #the client window may have been resized or moved (generally programmatically)
        #so we may need to update the corral_window to match
        cox, coy, cow, coh = self.corral_window.get_geometry()[:4]
        modded = False
        if self._geometry[4]!=border:
            modded = True
        #size changes (and position if any):
        hints = self.get_property("size-hints")
        size = calc_constrained_size(w, h, hints)
        log("resize_corral_window() new constrained size=%s", size)
        w, h, wvis, hvis = size
        if cow!=w or coh!=h:
            if (x, y) != (0, 0):
                log("resize_corral_window() move and resize from %s to %s", (cox, coy, cow, coh), (x, y, w, h))
                self.corral_window.move_resize(x, y, w, h)
                self.client_window.move(0, 0)
                cox, coy, cow, coh = x, y, w, h
            else:
                #just resize:
                log("resize_corral_window() resize from %s to %s", (cow, coh), (w, h))
                self.corral_window.resize(w, h)
                cow, coh = w, h
            modded = True
        #just position change:
        elif (x, y) != (0, 0):
            log("resize_corral_window() moving corral window from %s to %s", (cox, coy), (x, y))
            self.corral_window.move(x, y)
            self.client_window.move(0, 0)
            cox, coy = x, y
            modded = True

        #these two should be using geometry rather than duplicating it?
        if self.get_property("actual-size")!=(w, h):
            self._internal_set_property("actual-size", (w, h))
            modded = True
        if self.get_property("user-friendly-size")!=(wvis, hvis):
            self._internal_set_property("user-friendly-size", (wvis, hvis))
            modded = True

        if modded:
            self._geometry = (cox, coy, cow, coh, border)
        log("resize_corral_window() modified=%s, geometry=%s", modded, self._geometry)
        return modded

    def do_child_configure_request_event(self, event):
        log("do_child_configure_request_event(%s) client=%#x, corral=%#x", event, self.client_window.xid, self.corral_window.xid)
        if event.value_mask & CWStackMode:
            log(" restack above=%s, detail=%s", event.above, event.detail)
        # Also potentially update our record of what the app has requested:
        (x, y) = self.get_property("requested-position")
        if event.value_mask & CWX:
            x = event.x
        if event.value_mask & CWY:
            y = event.y
        self._internal_set_property("requested-position", (x, y))

        (w, h) = self.get_property("requested-size")
        if event.value_mask & CWWidth:
            w = event.width
        if event.value_mask & CWHeight:
            h = event.height
        self._internal_set_property("requested-size", (w, h))
        # As per ICCCM 4.1.5, even if we ignore the request
        # send back a synthetic ConfigureNotify telling the client that nothing has happened.
        self._update_client_geometry()

        # FIXME: consider handling attempts to change stacking order here.
        # (In particular, I believe that a request to jump to the top is
        # meaningful and should perhaps even be respected.)

    _property_handlers = BaseWindowModel._property_handlers.copy()

    def _handle_wm_normal_hints(self):
        with xswallow:
            size_hints = X11Window.getSizeHints(self.client_window.xid)
        #getSizeHints exports fields using their X11 names as defined in the "XSizeHints" structure,
        #but we use a different naming (for historical reason and backwards compatibility)
        #so rename the fields:
        hints = {}
        if size_hints:
            for k,v in size_hints.items():
                hints[{"min_size"       : "minimum-size",
                       "max_size"       : "maximum-size",
                       "base_size"      : "base-size",
                       "resize_inc"     : "increment",
                       "win_gravity"    : "gravity",
                       }.get(k, k)] = v
        self._sanitize_size_hints(hints)
        # Don't send out notify and ConfigureNotify events when this property
        # gets no-op updated -- some apps like FSF Emacs 21 like to update
        # their properties every time they see a ConfigureNotify, and this
        # reduces the chance for us to get caught in loops:
        old_hints = self.get_property("size-hints")
        if hints and hints!=old_hints:
            self._internal_set_property("size-hints", hints)
            self._update_client_geometry()

    _property_handlers["WM_NORMAL_HINTS"] = _handle_wm_normal_hints

    def _handle_icon_title_change(self):
        icon_name = self.prop_get("_NET_WM_ICON_NAME", "utf8", True)
        iconlog("_NET_WM_ICON_NAME=%s", icon_name)
        if icon_name is None:
            icon_name = self.prop_get("WM_ICON_NAME", "latin1", True)
        self._internal_set_property("icon-title", sanestr(icon_name))

    _property_handlers["WM_ICON_NAME"] = _handle_icon_title_change
    _property_handlers["_NET_WM_ICON_NAME"] = _handle_icon_title_change

    def _handle_motif_wm_hints(self):
        #motif_hints = self.prop_get("_MOTIF_WM_HINTS", "motif-hints")
        motif_hints = prop_get(self.client_window, "_MOTIF_WM_HINTS", "motif-hints", ignore_errors=False, raise_xerrors=True)
        log("_handle_motif_wm_hints() motif_hints=%s", motif_hints)
        DECORATIONS_BIT = 1
        if motif_hints and motif_hints.flags&(2**DECORATIONS_BIT):
            self._internal_set_property("decorations", motif_hints.decorations)
    _property_handlers["_MOTIF_WM_HINTS"] = _handle_motif_wm_hints

    def _handle_net_wm_icon(self):
        iconlog("_NET_WM_ICON changed on %#x, re-reading", self.client_window.xid)
        surf = self.prop_get("_NET_WM_ICON", "icon")
        if surf is not None:
            # FIXME: There is no Pixmap.new_for_display(), so this isn't
            # actually display-clean.  Oh well.
            pixmap = gtk.gdk.Pixmap(None, surf.get_width(), surf.get_height(), 32)
            screen = get_display_for(pixmap).get_default_screen()
            pixmap.set_colormap(screen.get_rgba_colormap())
            cr = pixmap.cairo_create()
            cr.set_source_surface(surf)
            # Important to use SOURCE, because a newly created Pixmap can have
            # random trash as its contents, and otherwise that will show
            # through any alpha in the icon:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.paint()
        else:
            pixmap = None
        #FIXME: it would be more efficient to notify first,
        #then get the icon pixels on demand and cache them..
        self._internal_set_property("icon", surf)
        self._internal_set_property("icon-pixmap", pixmap)
        iconlog("icon is now %r", surf)
    _property_handlers["_NET_WM_ICON"] = _handle_net_wm_icon

    def _read_initial_properties(self):
        # Things that don't change:
        BaseWindowModel._read_initial_properties(self)
        def pget(key, ptype):
            return self.prop_get(key, ptype, raise_xerrors=True)

        geometry = self.client_window.get_geometry()
        self._internal_set_property("requested-position", (geometry[0], geometry[1]))
        self._internal_set_property("requested-size", (geometry[2], geometry[3]))

        #try using XGetClassHint:
        class_instance = X11Window.getClassHint(self.client_window.xid)
        if class_instance is None:
            #fallback to reading WM_CLASS:
            def get_wm_class_prop(ptype):
                class_instance = pget("WM_CLASS", ptype)
                if not class_instance:
                    return None
                try:
                    parts = class_instance.split("\0")
                    if len(parts)!=3:
                        return None
                    (c, i, _) = parts
                    return  (c, i)
                except ValueError:
                    log.warn("Malformed WM_CLASS: %s, ignoring", class_instance)
                    return None
            class_instance = get_wm_class_prop("latin1") or get_wm_class_prop("utf8")
        self._internal_set_property("class-instance", class_instance)

        protocols = pget("WM_PROTOCOLS", ["atom"])
        if protocols is None:
            protocols = []
        self._internal_set_property("protocols", protocols)
        self.notify("can-focus")

        client_machine = pget("WM_CLIENT_MACHINE", "latin1")
        # May be None
        self._internal_set_property("client-machine", client_machine)

        command = pget("WM_COMMAND", "latin1")
        if command:
            command = command.strip("\0")
        self._internal_set_property("command", command)

        # WARNING: have to handle _NET_WM_STATE before we look at WM_HINTS;
        # WM_HINTS assumes that our "state" property is already set.  This is
        # because there are four ways a window can get its urgency
        # ("attention-requested") bit set:
        #   1) _NET_WM_STATE_DEMANDS_ATTENTION in the _initial_ state hints
        #   2) setting the bit WM_HINTS, at _any_ time
        #   3) sending a request to the root window to add
        #      _NET_WM_STATE_DEMANDS_ATTENTION to their state hints
        #   4) if we (the wm) decide they should be and set it
        # To implement this, we generally track the urgency bit via
        # _NET_WM_STATE (since that is under our sole control during normal
        # operation).  Then (1) is accomplished through the normal rule that
        # initial states are read off from the client, and (2) is accomplished
        # by having WM_HINTS affect _NET_WM_STATE.  But this means that
        # WM_HINTS and _NET_WM_STATE handling become intertangled.
        net_wm_state = pget("_NET_WM_STATE", ["atom"])
        if net_wm_state:
            self._internal_set_property("state", frozenset(net_wm_state))
        else:
            self._internal_set_property("state", frozenset())
        modal = (net_wm_state is not None) and ("_NET_WM_STATE_MODAL" in net_wm_state)
        self._internal_set_property("modal", modal)

        #the default value is True, but is not being honoured! (so we force it here)
        self._internal_set_property("decorations", True)

        for mutable in ["WM_HINTS", "WM_NORMAL_HINTS",
                        "WM_ICON_NAME", "_NET_WM_ICON_NAME",
                        "_NET_WM_STRUT", "_NET_WM_STRUT_PARTIAL", "_MOTIF_WM_HINTS"]:
            self._call_property_handler(mutable)
        for mutable in ["_NET_WM_ICON"]:
            try:
                self._call_property_handler(mutable)
            except:
                log.error("error reading initial property %s", mutable, exc_info=True)

    def get_default_window_icon(self):
        #return the icon which would be used from the wmclass
        c_i = self.get_property("class-instance")
        if not c_i or len(c_i)!=2:
            return None
        wmclass_name, wmclass_class = [x.encode("utf-8") for x in c_i]
        iconlog("get_default_window_icon() using %s", (wmclass_name, wmclass_class))
        if not wmclass_name:
            return None
        it = gtk.icon_theme_get_default()
        p = None
        for fmt in ("%s-color", "%s", "%s_48x48", "application-x-%s", "%s-symbolic", "%s.symbolic"):
            icon_name = fmt % wmclass_name
            i = it.lookup_icon(icon_name, 48, 0)
            iconlog("%s.lookup_icon(%s)=%s", it, icon_name, i)
            if not i:
                continue
            p = i.load_icon()
            iconlog("%s.load_icon()=%s", i, p)
            if p:
                break
        if p is None:
            return None
        #to make it consistent with the "icon" property,
        #return a cairo surface..
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, p.get_width(), p.get_height())
        gc = gtk.gdk.CairoContext(cairo.Context(surf))
        gc.set_source_pixbuf(p, 0, 0)
        gc.paint()
        iconlog("get_default_window_icon()=%s", surf)
        return surf

    ################################
    # Property setting
    ################################

    # A few words about _NET_WM_STATE are in order.  Basically, it is a set of
    # flags.  Clients are allowed to set the initial value of this X property
    # to anything they like, when their window is first mapped; after that,
    # though, only the window manager is allowed to touch this property.  So
    # we store its value (or at least, our idea as to its value, the X server
    # in principle could disagree) as the "state" property.  There are
    # basically two things we need to accomplish:
    #   1) Whenever our property is modified, we mirror that modification into
    #      the X server.  This is done by connecting to our own notify::state
    #      signal.
    #   2) As a more user-friendly interface to these state flags, we provide
    #      several boolean properties like "attention-requested".
    #      These are virtual boolean variables; they are actually backed
    #      directly by the "state" property, and reading/writing them in fact
    #      accesses the "state" set directly.  This is done by overriding
    #      do_set_property and do_get_property.
    _state_properties = {
        "attention-requested"   : ("_NET_WM_STATE_DEMANDS_ATTENTION", ),
        "fullscreen"            : ("_NET_WM_STATE_FULLSCREEN", ),
        "maximized"             : ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"),
        "shaded"                : ("_NET_WM_STATE_SHADED", ),
        "above"                 : ("_NET_WM_STATE_ABOVE", ),
        "below"                 : ("_NET_WM_STATE_BELOW", ),
        "sticky"                : ("_NET_WM_STATE_STICKY", ),
        "skip-taskbar"          : ("_NET_WM_STATE_SKIP_TASKBAR", ),
        "skip-pager"            : ("_NET_WM_STATE_SKIP_PAGER", ),
        "modal"                 : ("_NET_WM_STATE_MODAL", ),
        }

    _state_properties_reversed = {}
    for k, states in _state_properties.iteritems():
        for x in states:
            _state_properties_reversed[x] = k

    def _state_add(self, *state_names):
        curr = set(self.get_property("state"))
        add = [s for s in state_names if s not in curr]
        if add:
            for x in add:
                curr.add(x)
            self._internal_set_property("state", frozenset(curr))
            self._state_notify(add)

    def _state_remove(self, *state_names):
        curr = set(self.get_property("state"))
        discard = [s for s in state_names if s in curr]
        if discard:
            for x in discard:
                curr.discard(x)
            self._internal_set_property("state", frozenset(curr))
            self._state_notify(discard)

    def _state_notify(self, state_names):
        notify_props = set()
        for x in state_names:
            if x in self._state_properties_reversed:
                notify_props.add(self._state_properties_reversed[x])
        for x in list(notify_props):
            self.notify(x)

    def _state_isset(self, state_name):
        return state_name in self.get_property("state")

    def _handle_state_changed(self, *args):
        # Sync changes to "state" property out to X property.
        with xswallow:
            prop_set(self.client_window, "_NET_WM_STATE",
                 ["atom"], self.get_property("state"))

    def do_set_property(self, pspec, value):
        if pspec.name in self._state_properties:
            state_names = self._state_properties[pspec.name]
            if value:
                self._state_add(*state_names)
            else:
                self._state_remove(*state_names)
        else:
            AutoPropGObjectMixin.do_set_property(self, pspec, value)

    def do_get_property_can_focus(self, name):
        assert name == "can-focus"
        return bool(self._input_field) or "WM_TAKE_FOCUS" in self.get_property("protocols")

    def do_get_property(self, pspec):
        if pspec.name in self._state_properties:
            #return True if any is set (only relevant for maximized)
            state_names = self._state_properties[pspec.name]
            for x in state_names:
                if self._state_isset(x):
                    return True
            return False
        else:
            return AutoPropGObjectMixin.do_get_property(self, pspec)


    def unmap(self):
        with xsync:
            if X11Window.is_mapped(self.client_window.xid):
                self.last_unmap_serial = X11Window.Unmap(self.client_window.xid)
                log("client window %#x unmapped, serial=%s", self.client_window.xid, self.last_unmap_serial)

    def map(self):
        with xsync:
            if not X11Window.is_mapped(self.client_window.xid):
                X11Window.MapWindow(self.client_window.xid)
                log("client window %#x mapped", self.client_window.xid)


    def _handle_iconic_update(self, *args):
        def set_state(state):
            log("_handle_iconic_update: set_state(%s)", state)
            with xswallow:
                prop_set(self.client_window, "WM_STATE", "state", state)

        if self.get_property("iconic"):
            set_state(IconicState)
            self._state_add("_NET_WM_STATE_HIDDEN")
        else:
            set_state(NormalState)
            self._state_remove("_NET_WM_STATE_HIDDEN")

    def _write_initial_properties_and_setup(self):
        # Things that don't change:
        prop_set(self.client_window, "_NET_WM_ALLOWED_ACTIONS",
                 ["atom"], self._NET_WM_ALLOWED_ACTIONS)
        prop_set(self.client_window, "_NET_FRAME_EXTENTS",
                 ["u32"], [0, 0, 0, 0])

        self.connect("notify::state", self._handle_state_changed)
        # Flush things:
        self._handle_state_changed()


    ################################
    # Focus handling:
    ################################

    def give_client_focus(self):
        """The focus manager has decided that our client should recieve X
        focus.  See world_window.py for details."""
        if self.corral_window:
            with xswallow:
                self.do_give_client_focus()

    def do_give_client_focus(self):
        focuslog("Giving focus to %#x", self.client_window.xid)
        # Have to fetch the time, not just use CurrentTime, both because ICCCM
        # says that WM_TAKE_FOCUS must use a real time and because there are
        # genuine race conditions here (e.g. suppose the client does not
        # actually get around to requesting the focus until after we have
        # already changed our mind and decided to give it to someone else).
        now = gtk.gdk.x11_get_server_time(self.corral_window)
        # ICCCM 4.1.7 *claims* to describe how we are supposed to give focus
        # to a window, but it is completely opaque.  From reading the
        # metacity, kwin, gtk+, and qt code, it appears that the actual rules
        # for giving focus are:
        #   -- the WM_HINTS input field determines whether the WM should call
        #      XSetInputFocus
        #   -- independently, the WM_TAKE_FOCUS protocol determines whether
        #      the WM should send a WM_TAKE_FOCUS ClientMessage.
        # If both are set, both methods MUST be used together. For example,
        # GTK+ apps respect WM_TAKE_FOCUS alone but I'm not sure they handle
        # XSetInputFocus well, while Qt apps ignore (!!!) WM_TAKE_FOCUS
        # (unless they have a modal window), and just expect to get focus from
        # the WM's XSetInputFocus.
        if bool(self._input_field):
            focuslog("... using XSetInputFocus")
            X11Window.XSetInputFocus(self.client_window.xid, now)
        if "WM_TAKE_FOCUS" in self.get_property("protocols"):
            focuslog("... using WM_TAKE_FOCUS")
            send_wm_take_focus(self.client_window, now)
        self.set_active()

    ################################
    # Killing clients:
    ################################

    def request_close(self):
        if "WM_DELETE_WINDOW" in self.get_property("protocols"):
            with xswallow:
                send_wm_delete_window(self.client_window)
        else:
            title = self.get_property("title")
            xid = self.get_property("xid")
            if FORCE_QUIT:
                log.warn("window %#x ('%s') does not support WM_DELETE_WINDOW... using force_quit()", xid, title)
                # You don't wanna play ball?  Then no more Mr. Nice Guy!
                self.force_quit()
            else:
                log.warn("window %#x ('%s') cannot be closed,", xid, title)
                log.warn(" it does not support WM_DELETE_WINDOW")
                log.warn(" and FORCE_QUIT is disabled")


    def force_quit(self):
        pid = self.get_property("pid")
        machine = self.get_property("client-machine")
        localhost = gethostname()
        log("force_quit() pid=%s, machine=%s, localhost=%s", pid, machine, localhost)
        xid = self.client_window.xid
        def XKill():
            with xswallow:
                X11Window.XKillClient(xid)
        if pid > 0 and machine is not None and machine == localhost:
            if pid==os.getpid():
                log.warn("force_quit() refusing to kill ourselves!")
                return
            if self.kill_count==0:
                #first time around: just send a SIGINT and hope for the best
                try:
                    os.kill(pid, signal.SIGINT)
                except OSError:
                    log.warn("failed to kill(SIGINT) client with pid %s", pid)
            else:
                #the more brutal way: SIGKILL + XKill
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    log.warn("failed to kill(SIGKILL) client with pid %s", pid)
                XKill()
            self.kill_count += 1
            return
        XKill()

    def __repr__(self):
        xid = 0
        if self.client_window:
            xid = self.client_window.xid
        title = self.get_property("title")
        if title:
            return "WindowModel(%#x - \"%s\")" % (xid, nonl(title))
        return "WindowModel(%#x)" % xid


gobject.type_register(WindowModel)
