# This file is part of Xpra.
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2012-2017 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import cairo
from PIL import Image
from gtk import gdk     #@UnresolvedImport

from xpra.client.window_backing_base import SCROLL_ENCODING
from xpra.client.gtk2.window_backing import GTK2WindowBacking
from xpra.client.window_backing_base import fire_paint_callbacks
from xpra.client.paint_colors import get_paint_box_color
from xpra.client.gtk_base.cairo_paint_common import setup_cairo_context, cairo_paint_pointer_overlay
from xpra.os_util import memoryview_to_bytes, _buffer
from xpra.util import envbool
from xpra.log import Logger

log = Logger("paint")

PIXMAP_RGB_MODES = ["RGB", "RGBX", "RGBA"]
INDIRECT_BGR = envbool("XPRA_PIXMAP_INDIRECT_BGR", False)
if INDIRECT_BGR:
    PIXMAP_RGB_MODES += ["BGRX", "BGRA", "BGR"]


"""
Backing using a gdk.Pixmap
"""
class PixmapBacking(GTK2WindowBacking):

    RGB_MODES = PIXMAP_RGB_MODES

    def __repr__(self):
        return "PixmapBacking(%s)" % self._backing

    def init(self, ww, wh, bw, bh):
        #use window size as backing size:
        self.render_size = ww, wh
        self.size = bw, bh
        old_backing = self._backing
        self.do_init_new_backing_instance()
        self.copy_backing(old_backing)


    def get_encoding_properties(self):
        props = GTK2WindowBacking.get_encoding_properties(self)
        if SCROLL_ENCODING:
            props["encoding.scrolling"] = True
        return props


    def do_init_new_backing_instance(self):
        w, h = self.size
        self._backing = None
        assert w<32768 and h<32768, "dimensions too big: %sx%s" % (w, h)
        if w==0 or h==0:
            #this can happen during cleanup
            return
        if w<0 or h<0:
            log.warn("Warning: invalid backing dimensions %ix%i", w, h)
            w = max(1, w)
            h = max(1, h)
        try:
            if self._alpha_enabled:
                self._backing = gdk.Pixmap(None, w, h, 32)
                screen = self._backing.get_screen()
                rgba = screen.get_rgba_colormap()
                if rgba is not None:
                    self._backing.set_colormap(rgba)
                else:
                    #cannot use transparency
                    log.warn("Warning: cannot display transparency, no RGBA colormap")
                    self._alpha_enabled = False
                    self._backing = gdk.Pixmap(gdk.get_default_root_window(), w, h)
            else:
                self._backing = gdk.Pixmap(gdk.get_default_root_window(), w, h)
        except RuntimeError as e:
            log("do_init_new_backing_instance()", exc_info=True)
            log.error("Error creating pixmap backing of size %ix%i", w, h)
            log.error(" %s", e)
            self._backing = None

    def copy_backing(self, old_backing):
        bw, bh = self.size
        cr = self._backing.cairo_create()
        cr.set_source_rgb(1, 1, 1)
        if old_backing is not None:
            oldw, oldh = old_backing.get_size()
            sx, sy, dx, dy, w, h = self.gravity_copy_coords(oldw, oldh, bw, bh)
            cr.translate(dx-sx, dy-sy)
            cr.rectangle(sx, sy, w, h)
            cr.fill()
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_pixmap(old_backing, 0, 0)
            cr.paint()
        else:
            cr.rectangle(0, 0, bw, bh)
            cr.fill()

    def paint_scroll(self, img_data, _options, callbacks):
        #Warning: unused as this causes strange visual corruption
        self.idle_add(self.do_paint_scroll, img_data, callbacks)

    def do_paint_scroll(self, scrolls, callbacks):
        old_backing = self._backing
        self.do_init_new_backing_instance()
        self.copy_backing(old_backing)
        gc = self._backing.new_gc()
        for sx,sy,sw,sh,xdelta,ydelta in scrolls:
            self._backing.draw_drawable(gc, old_backing, sx, sy, sx+xdelta, sy+ydelta, sw, sh)
        fire_paint_callbacks(callbacks)

    def bgr_to_rgb(self, img_data, width, height, rowstride, rgb_format, target_format):
        if not rgb_format.startswith("BGR"):
            return img_data, rowstride
        #use an rgb format name that PIL will recognize:
        in_format = rgb_format.replace("X", "A")
        img = Image.frombuffer(target_format, (width, height), img_data, "raw", in_format, rowstride)
        img_data = img.tobytes("raw", target_format)
        log.warn("%s converted to %s", rgb_format, target_format)
        return img_data, width*len(target_format)

    def _do_paint_rgb24(self, img_data, x, y, width, height, rowstride, options):
        if isinstance(img_data, (memoryview, _buffer, bytearray)):
            img_data = memoryview_to_bytes(img_data)
        if INDIRECT_BGR:
            rgb_format = options.strget("rgb_format", "")
            img_data, rowstride = self.bgr_to_rgb(img_data, width, height, rowstride, rgb_format, "RGB")
        gc = self._backing.new_gc()
        self._backing.draw_rgb_image(gc, x, y, width, height, gdk.RGB_DITHER_NONE, img_data, rowstride)
        if self.paint_box_line_width>0:
            self.paint_box(x, y, width, height, options)
        return True

    def _do_paint_rgb32(self, img_data, x, y, width, height, rowstride, options):
        has_alpha = options.boolget("has_alpha", False) or options.get("rgb_format", "").find("A")>=0
        if has_alpha:
            img_data = self.unpremultiply(img_data)
        if isinstance(img_data, (memoryview, _buffer, bytearray)):
            img_data = memoryview_to_bytes(img_data)
        if INDIRECT_BGR:
            rgb_format = options.strget("rgb_format", "")
            img_data, rowstride = self.bgr_to_rgb(img_data, width, height, rowstride, rgb_format, "RGBA")
        if has_alpha:
            #draw_rgb_32_image does not honour alpha, we have to use pixbuf:
            pixbuf = gdk.pixbuf_new_from_data(img_data, gdk.COLORSPACE_RGB, True, 8, width, height, rowstride)
            cr = self._backing.cairo_create()
            cr.rectangle(x, y, width, height)
            cr.set_source_pixbuf(pixbuf, x, y)
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.paint()
        else:
            #no alpha or scaling is easier:
            gc = self._backing.new_gc()
            self._backing.draw_rgb_32_image(gc, x, y, width, height, gdk.RGB_DITHER_NONE, img_data, rowstride)
        if self.paint_box_line_width>0:
            self.paint_box(x, y, width, height, options)
        return True

    def paint_box(self, x, y, w, h, options):
        encoding = options.get("encoding")
        b = self._backing
        if not encoding or not b:
            return
        gc = b.cairo_create()
        color = get_paint_box_color(encoding)
        gc.set_line_width(self.paint_box_line_width)
        gc.set_source_rgba(*color)
        gc.rectangle(x, y, w, h)
        gc.stroke()


    def cairo_draw(self, context):
        self.cairo_draw_from_drawable(context, self._backing)


    def cairo_draw_from_drawable(self, context, drawable):
        if drawable is None:
            return
        ww, wh = self.render_size
        w, h = self.size
        if ww==0 or w==0 or wh==0 or h==0:
            return
        x, y = self.offsets[:2]
        log("cairo_draw_from_drawable%s render_size=%s, size=%s, offsets=%s, pointer_overlay=%s",
            (context, drawable), self.render_size, self.size, self.offsets, self.pointer_overlay)
        setup_cairo_context(context, ww, wh, w, h, x, y)
        context.set_source_pixmap(drawable, 0, 0)
        context.paint()
        if self.pointer_overlay and self.cursor_data:
            px, py, _size, start_time = self.pointer_overlay[2:]    #pylint: disable=unsubscriptable-object
            spx = round(w*px/ww)
            spy = round(h*py/wh)
            cairo_paint_pointer_overlay(context, self.cursor_data, spx, spy, start_time)
