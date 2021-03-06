# This file is part of Xpra.
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2012-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.gtk_common.gobject_compat import import_gdk, import_gobject, import_pixbufloader, import_cairo, is_gtk3
gdk             = import_gdk()
gobject         = import_gobject()
cairo           = import_cairo()
PixbufLoader    = import_pixbufloader()

from xpra.os_util import BytesIOClass
from xpra.gtk_common.gtk_util import pixbuf_new_from_data, cairo_set_source_pixbuf, gdk_cairo_context, COLORSPACE_RGB
from xpra.client.gtk_base.gtk_window_backing_base import GTKWindowBacking
from xpra.client.window_backing_base import fire_paint_callbacks
from xpra.codecs.loader import get_codec
from xpra.os_util import builtins
_memoryview = builtins.__dict__.get("memoryview")


from xpra.log import Logger
log = Logger("paint", "cairo")


"""
An area we draw onto with cairo
This must be used with gtk3 since gtk3 no longer supports gdk pixmaps

/RANT: ideally we would want to use pycairo's create_for_data method:
#surf = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, width, height)
but this is disabled in most cases, or does not accept our rowstride, so we cannot use it.
Instead we have to use PIL to convert via a PNG or Pixbuf!
"""
class CairoBacking(GTKWindowBacking):

    if not is_gtk3():
        #with gtk2 we can convert these directly to a cairo image surface:
        RGB_MODES = ["ARGB", "XRGB"]
    else:
        RGB_MODES = []
    RGB_MODES += ["RGBA", "RGBX", "RGB"]


    def __init__(self, wid, w, h, has_alpha):
        GTKWindowBacking.__init__(self, wid, has_alpha)

    def __repr__(self):
        return "CairoBacking(%s)" % self._backing

    def init(self, w, h):
        old_backing = self._backing
        #should we honour self.depth here?
        self._backing = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(self._backing)
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if old_backing is not None:
            # Really we should respect bit-gravity here but... meh.
            old_w = old_backing.get_width()
            old_h = old_backing.get_height()
            cr.set_operator(cairo.OPERATOR_SOURCE)
            if w>old_w and h>old_h:
                #both width and height are bigger:
                cr.rectangle(old_w, 0, w-old_w, h)
                cr.fill()
                cr.new_path()
                cr.rectangle(0, old_h, old_w, h-old_h)
                cr.fill()
            elif w>old_w:
                #enlarged in width only
                cr.rectangle(old_w, 0, w-old_w, h)
                cr.fill()
            if h>old_h:
                #enlarged in height only
                cr.rectangle(0, old_h, w, h-old_h)
                cr.fill()
            #cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.set_source_surface(old_backing, 0, 0)
            cr.paint()
            #old_backing.finish()

    def close(self):
        if self._backing:
            self._backing.finish()
        GTKWindowBacking.close(self)


    def paint_image(self, coding, img_data, x, y, width, height, options, callbacks):
        log("cairo.paint_image(%s, %s bytes,%s,%s,%s,%s,%s,%s) alpha_enabled=%s", coding, len(img_data), x, y, width, height, options, callbacks, self._alpha_enabled)
        #catch PNG and jpeg we can handle via cairo or pixbufloader respectively
        #(both of which need to run from the UI thread)
        if coding.startswith("png") or coding=="jpeg":
            def ui_paint_image():
                if not self._backing:
                    fire_paint_callbacks(callbacks, False)
                    return
                try:
                    if coding.startswith("png"):
                        reader = BytesIOClass(img_data)
                        img = cairo.ImageSurface.create_from_png(reader)
                        success = self.cairo_paint_surface(img, x, y)
                    else:
                        assert coding=="jpeg"
                        pbl = PixbufLoader()
                        pbl.write(img_data)
                        pbl.close()
                        pixbuf = pbl.get_pixbuf()
                        del pbl
                        success = self.cairo_paint_pixbuf(pixbuf, x, y)
                except:
                    log.error("cairo error during paint", exc_info=True)
                    success = False
                fire_paint_callbacks(callbacks, success)
            gobject.idle_add(ui_paint_image)
            return
        #this will end up calling do_paint_rgb24 after converting the pixels to RGB
        GTKWindowBacking.paint_image(self, coding, img_data, x, y, width, height, options, callbacks)

    def cairo_paint_pixbuf(self, pixbuf, x, y):
        """ must be called from UI thread """
        log("cairo_paint_pixbuf(%s, %s, %s) backing=%s", pixbuf, x, y, self._backing)
        #now use it to paint:
        gc = gdk_cairo_context(cairo.Context(self._backing))
        cairo_set_source_pixbuf(gc, pixbuf, x, y)
        gc.paint()
        return True

    def cairo_paint_surface(self, img_surface, x, y):
        """ must be called from UI thread """
        log("cairo_paint_surface(%s, %s, %s)", img_surface, x, y)
        gc = gdk_cairo_context(cairo.Context(self._backing))
        gc.set_operator(cairo.OPERATOR_CLEAR)
        gc.rectangle(x, y, img_surface.get_width(), img_surface.get_height())
        gc.fill()
        gc.set_operator(cairo.OPERATOR_OVER)
        gc.set_source_surface(img_surface, x, y)
        gc.paint()
        return True


    def _do_paint_rgb24(self, img_data, x, y, width, height, rowstride, options):
        return self._do_paint_rgb(cairo.FORMAT_RGB24, False, img_data, x, y, width, height, rowstride, options)

    def _do_paint_rgb32(self, img_data, x, y, width, height, rowstride, options):
        return self._do_paint_rgb(cairo.FORMAT_ARGB32, True, img_data, x, y, width, height, rowstride, options)

    def _do_paint_rgb(self, cairo_format, has_alpha, img_data, x, y, width, height, rowstride, options):
        """ must be called from UI thread """
        log("cairo._do_paint_rgb(%s, %s, %s bytes,%s,%s,%s,%s,%s,%s)", cairo_format, has_alpha, len(img_data), x, y, width, height, rowstride, options)
        rgb_format = options.strget("rgb_format", "RGB")
        if _memoryview and isinstance(img_data, _memoryview):
            #Pixbuf cannot use the memoryview directly:
            img_data = img_data.tobytes()
        #"cairo.ImageSurface.create_for_data" is not implemented in GTK3! ARGH!
        # http://cairographics.org/documentation/pycairo/3/reference/surfaces.html#cairo.ImageSurface.create_for_data
        # "Not yet available in Python 3"
        #
        #It is available in the cffi cairo bindings, which can be used instead of pycairo
        # but then we can't use it from the draw callbacks:
        # https://mail.gnome.org/archives/python-hackers-list/2011-December/msg00004.html
        # "PyGObject just lacks the glue code that allows it to pass the statically-wrapped
        # cairo.Pattern to introspected methods"

        if not is_gtk3() and rgb_format in ("ARGB", "XRGB"):
            #the pixel format is also what cairo expects
            #maybe we should also check that the stride is acceptable for cairo?
            #cairo_stride = cairo.ImageSurface.format_stride_for_width(cairo_format, width)
            #log("cairo_stride=%s, stride=%s", cairo_stride, rowstride)
            img_surface = cairo.ImageSurface.create_for_data(img_data, cairo_format, width, height, rowstride)
            return self.cairo_paint_surface(img_surface, x, y)

        if not is_gtk3() and rgb_format in ("RGBA", "RGBX"):
            #with GTK2, we can use a pixbuf from RGB(A) pixels
            if rgb_format=="RGBA":
                #we have to unpremultiply for pixbuf!
                img_data = self.unpremultiply(img_data)
            pixbuf = pixbuf_new_from_data(img_data, COLORSPACE_RGB, has_alpha, 8, width, height, rowstride)
            return self.cairo_paint_pixbuf(pixbuf, x, y)

        #PIL fallback
        PIL = get_codec("PIL")
        if has_alpha:
            oformat = "RGBA"
        else:
            oformat = "RGB"
        img = PIL.Image.frombuffer(oformat, (width,height), img_data, "raw", rgb_format, rowstride, 1)
        #This is insane, the code below should work, but it doesn't:
        # img_data = bytearray(img.tostring('raw', oformat, 0, 1))
        # pixbuf = pixbuf_new_from_data(img_data, COLORSPACE_RGB, True, 8, width, height, rowstride)
        # success = self.cairo_paint_pixbuf(pixbuf, x, y)
        #So we still rountrip via PNG:
        png = BytesIOClass()
        img.save(png, format="PNG")
        reader = BytesIOClass(png.getvalue())
        png.close()
        img = cairo.ImageSurface.create_from_png(reader)
        return self.cairo_paint_surface(img, x, y)


    def cairo_draw(self, context):
        log("cairo_draw(%s) backing=%s", context, self._backing)
        if self._backing is None:
            return False
        try:
            context.set_source_surface(self._backing, 0, 0)
            context.set_operator(cairo.OPERATOR_SOURCE)
            context.paint()
            return True
        except KeyboardInterrupt:
            raise
        except:
            log.error("cairo_draw(%s)", context, exc_info=True)
            return False
