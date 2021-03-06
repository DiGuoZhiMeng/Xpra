# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2013-2016 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import time
import operator
import threading
from math import sqrt

from xpra.net.compression import Compressed, LargeStructure
from xpra.codecs.codec_constants import TransientCodecException, RGB_FORMATS, PIXEL_SUBSAMPLING
from xpra.server.window.window_source import WindowSource, STRICT_MODE, AUTO_REFRESH_SPEED, AUTO_REFRESH_QUALITY
from xpra.server.window.region import rectangle, merge_all          #@UnresolvedImport
from xpra.server.window.motion import match_distance, consecutive_lines, calculate_distances, CRC_Image #@UnresolvedImport
from xpra.server.window.video_subregion import VideoSubregion, VIDEO_SUBREGION
from xpra.server.window.video_scoring import get_pipeline_score
from xpra.codecs.loader import PREFERED_ENCODING_ORDER, EDGE_ENCODING_ORDER
from xpra.util import parse_scaling_value, engs, envint, envbool, csv
from xpra.log import Logger

log = Logger("encoding")
csclog = Logger("csc")
scorelog = Logger("score")
scalinglog = Logger("scaling")
sublog = Logger("subregion")
videolog = Logger("video")
avsynclog = Logger("av-sync")
scrolllog = Logger("scroll")
compresslog = Logger("compress")


MAX_NONVIDEO_PIXELS = envint("XPRA_MAX_NONVIDEO_PIXELS", 1024*4)
MIN_VIDEO_FPS = envint("XPRA_MIN_VIDEO_FPS", 10)
MIN_VIDEO_EVENTS = envint("XPRA_MIN_VIDEO_EVENTS", 20)

FORCE_CSC_MODE = os.environ.get("XPRA_FORCE_CSC_MODE", "")   #ie: "YUV444P"
if FORCE_CSC_MODE and FORCE_CSC_MODE not in RGB_FORMATS and FORCE_CSC_MODE not in PIXEL_SUBSAMPLING:
    log.warn("ignoring invalid CSC mode specified: %s", FORCE_CSC_MODE)
    FORCE_CSC_MODE = ""
FORCE_CSC = bool(FORCE_CSC_MODE) or envbool("XPRA_FORCE_CSC", False)
SCALING = envbool("XPRA_SCALING", True)
SCALING_HARDCODED = parse_scaling_value(os.environ.get("XPRA_SCALING_HARDCODED", ""))

FORCE_AV_DELAY = envint("XPRA_FORCE_AV_DELAY", 0)
B_FRAMES = envbool("XPRA_B_FRAMES", True)
VIDEO_SKIP_EDGE = envbool("XPRA_VIDEO_SKIP_EDGE", False)
SCROLL_ENCODING = envbool("XPRA_SCROLL_ENCODING", True)
SCROLL_MIN_PERCENT = max(1, min(100, envint("XPRA_SCROLL_MIN_PERCENT", 30)))

SAVE_VIDEO_FRAMES = os.environ.get("XPRA_SAVE_VIDEO_FRAMES")
if SAVE_VIDEO_FRAMES not in ("png", "jpeg", None):
    log.warn("invalid value for XPRA_SAVE_VIDEO_FRAMES: must be 'png' or 'jpeg'")
    SAVE_VIDEO_FRAMES = None

FAST_ORDER = ["jpeg", "rgb32", "rgb24", "png"] + PREFERED_ENCODING_ORDER


class WindowVideoSource(WindowSource):
    """
        A WindowSource that handles video codecs.
    """

    def __init__(self, *args):
        #this will call init_vars():
        WindowSource.__init__(self, *args)
        self.scroll_encoding = SCROLL_ENCODING
        self.supports_scrolling = self.scroll_encoding and self.encoding_options.boolget("scrolling")
        self.supports_video_scaling = self.encoding_options.boolget("video_scaling", False)
        self.supports_video_b_frames = self.encoding_options.strlistget("video_b_frames", [])

    def init_encoders(self):
        WindowSource.init_encoders(self)
        #for 0.12 onwards: per encoding lists:
        self.full_csc_modes = {}
        self.parse_csc_modes(self.encoding_options.dictget("full_csc_modes", default_value=None))

        self.video_encodings = self.video_helper.get_encodings()
        for x in self.video_encodings:
            if x in self.server_core_encodings:
                self._encoders[x] = self.video_encode
        self._encoders["auto"] = self.video_encode
        #these are used for non-video areas, ensure "jpeg" is used if available
        #as we may be dealing with large areas still, and we want speed:
        nv_common = (set(self.server_core_encodings) & set(self.core_encodings)) - set(self.video_encodings)
        self.non_video_encodings = [x for x in PREFERED_ENCODING_ORDER if x in nv_common]
        self.common_video_encodings = [x for x in PREFERED_ENCODING_ORDER if x in self.video_encodings and x in self.core_encodings]

        #those two instances should only ever be modified or accessed from the encode thread:
        self._csc_encoder = None
        self._video_encoder = None
        self._last_pipeline_check = 0

    def __repr__(self):
        return "WindowVideoSource(%s : %s)" % (self.wid, self.window_dimensions)

    def init_vars(self):
        WindowSource.init_vars(self)
        self.video_subregion = VideoSubregion(self.timeout_add, self.source_remove, self.refresh_subregion, self.auto_refresh_delay)

        #these constraints get updated with real values
        #when we construct the video pipeline:
        self.min_w = 1
        self.min_h = 1
        self.max_w = 16384
        self.max_h = 16384
        self.width_mask = 0xFFFF
        self.height_mask = 0xFFFF
        self.actual_scaling = (1, 1)

        self.last_pipeline_params = None
        self.last_pipeline_scores = []
        self.last_pipeline_time = 0

        self.supports_video_scaling = False

        self.full_csc_modes = {}                            #for 0.12 onwards: per encoding lists
        self.video_encodings = []
        self.common_video_encodings = []
        self.non_video_encodings = []
        self.edge_encoding = None
        self.start_video_frame = 0
        self.b_frame_flush_timer = None
        self.b_frame_flush_data = None
        self.encode_from_queue_timer = None
        self.encode_from_queue_due = 0
        self.scroll_data = None
        self.last_scroll_time = 0

    def set_auto_refresh_delay(self, d):
        WindowSource.set_auto_refresh_delay(self, d)
        r = self.video_subregion
        if r:
            r.set_auto_refresh_delay(d)

    def calculate_batch_delay(self, has_focus, other_is_fullscreen, other_is_maximized):
        WindowSource.calculate_batch_delay(self, has_focus, other_is_fullscreen, other_is_maximized)
        vsr = self.video_subregion
        bc = self.batch_config
        if not bc.locked and vsr:
            #we have a video subregion, update its refresh delay
            r = vsr.rectangle
            if r:
                ww, wh = self.window_dimensions
                pct = (100*r.width*r.height)/(ww*wh)
                d = 2 * int(max(100, self.auto_refresh_delay * max(50, pct) / 50, bc.delay*4))
                vsr.set_auto_refresh_delay(d)

    def update_av_sync_frame_delay(self):
        self.av_sync_frame_delay = 0
        ve = self._video_encoder
        if ve:
            d = ve.get_info().get("delayed", 0)
            self.av_sync_frame_delay += 40 * d
            avsynclog("update_av_sync_frame_delay() video encoder=%s, delayed frames=%i, frame delay=%i", ve, d, self.av_sync_frame_delay)
        self.may_update_av_sync_delay()


    def get_client_info(self):
        info = {
                "supports_video_scaling"    : self.supports_video_scaling,
                }
        for enc, csc_modes in (self.full_csc_modes or {}).items():
            info["csc_modes.%s" % enc] = csc_modes
        return info

    def get_property_info(self):
        i = WindowSource.get_property_info(self)
        i.update({
                "scaling.control"       : self.scaling_control,
                "scaling"               : self.scaling or (1, 1),
                })
        return i

    def get_info(self):
        info = WindowSource.get_info(self)
        sr = self.video_subregion
        if sr:
            sri = sr.get_info()
            sri["video-mode"] = self.subregion_is_video()
            info["video_subregion"] = sri
        info["scaling"] = self.actual_scaling
        def addcinfo(prefix, x):
            if not x:
                return
            try:
                i = x.get_info()
                i[""] = x.get_type()
                info[prefix] = i
            except:
                log.error("Error collecting codec information from %s", x, exc_info=True)
        addcinfo("csc", self._csc_encoder)
        addcinfo("encoder", self._video_encoder)
        info.setdefault("encodings", {}).update({
                                                 "non-video"    : self.non_video_encodings,
                                                 "video"        : self.common_video_encodings,
                                                 "edge"         : self.edge_encoding or "",
                                                 })
        einfo = {
                 "pipeline_param" : self.get_pipeline_info(),
                 "scrolling"      : self.supports_scrolling,
                 }
        if self._last_pipeline_check>0:
            einfo["pipeline_last_check"] = int(1000*(time.time()-self._last_pipeline_check))
        lps = self.last_pipeline_scores
        if lps:
            popts = einfo.setdefault("pipeline_option", {})
            for i, lp in enumerate(lps):
                popts[i] = self.get_pipeline_score_info(*lp)
        info.setdefault("encoding", {}).update(einfo)
        return info

    def get_pipeline_info(self):
        lp = self.last_pipeline_params
        if not lp:
            return {}
        encoding, width, height, src_format = lp
        return {
                "encoding"      : encoding,
                "dimensions"    : (width, height),
                "src_format"    : src_format
                }

    def get_pipeline_score_info(self, score, scaling, csc_scaling, csc_width, csc_height, csc_spec, enc_in_format, encoder_scaling, enc_width, enc_height, encoder_spec):
        def specinfo(x):
            try:
                return x.codec_type
            except:
                return repr(x)
        pi  = {
               "score"             : score,
               "scaling"           : scaling,
               "format"            : str(enc_in_format),
               "encoder"           : {
                                      ""        : specinfo(encoder_spec),
                                      "scaling" : encoder_scaling,
                                      "width"   : enc_width,
                                      "height"  : enc_height,
                                      },
               }
        if csc_spec:
            pi["csc"] = {
                         ""         : specinfo(csc_spec),
                         "scaling"  : csc_scaling,
                         "width"    : csc_width,
                         "height"   : csc_height,
                         }
        else:
            pi["csc"] = "None"
        return pi


    def cleanup(self):
        WindowSource.cleanup(self)
        self.cleanup_codecs()

    def cleanup_codecs(self):
        """ Video encoders (x264, nvenc and vpx) and their csc helpers
            require us to run cleanup code to free the memory they use.
            We have to do this from the encode thread to be safe.
            (the encoder and csc module may be in use by that thread)
        """
        self.cancel_video_encoder_flush()
        if self._csc_encoder:
            self.csc_encoder_clean()
        if self._video_encoder:
            self.video_encoder_clean()

    def csc_encoder_clean(self):
        """ Calls self._csc_encoder.clean() from the encode thread """
        csce = self._csc_encoder
        if csce:
            self._csc_encoder = None
            self.call_in_encode_thread(False, csce.clean)

    def video_encoder_clean(self):
        """ Calls self._video_encoder.clean() from the encode thread """
        ve = self._video_encoder
        if ve:
            self._video_encoder = None
            self.call_in_encode_thread(False, ve.clean)


    def parse_csc_modes(self, full_csc_modes):
        #only override if values are specified:
        csclog("parse_csc_modes(%s) current value=%s", full_csc_modes, self.full_csc_modes)
        if full_csc_modes is not None and type(full_csc_modes)==dict:
            self.full_csc_modes = full_csc_modes


    def set_new_encoding(self, encoding, strict=None):
        if self.encoding!=encoding:
            #ensure we re-init the codecs asap:
            self.cleanup_codecs()
        WindowSource.set_new_encoding(self, encoding, strict)

    def update_encoding_selection(self, encoding=None, exclude=[], init=False):
        #override so we don't use encodings that don't have valid csc modes:
        log("wvs.update_encoding_selection(%s, %s, %s)", encoding, exclude, init)
        for x in self.video_encodings:
            if x not in self.core_encodings:
                exclude.append(x)
                continue
            csc_modes = self.full_csc_modes.get(x)
            if not csc_modes or x not in self.core_encodings:
                exclude.append(x)
                if not init:
                    l = log.warn
                else:
                    l = log
                l("client does not support any csc modes with %s", x)
        WindowSource.update_encoding_selection(self, encoding, exclude, init)

    def do_set_client_properties(self, properties):
        #client may restrict csc modes for specific windows
        self.parse_csc_modes(properties.dictget("encoding.full_csc_modes", default_value=None))
        self.supports_scrolling = self.scroll_encoding and properties.boolget("encoding.scrolling", self.supports_scrolling)
        self.supports_video_scaling = properties.boolget("encoding.video_scaling", self.supports_video_scaling)
        self.video_subregion.supported = properties.boolget("encoding.video_subregion", VIDEO_SUBREGION) and VIDEO_SUBREGION
        self.scaling_control = max(0, min(100, properties.intget("scaling.control", self.scaling_control)))
        WindowSource.do_set_client_properties(self, properties)
        #encodings may have changed, so redo this:
        nv_common = (set(self.server_core_encodings) & set(self.core_encodings)) - set(self.video_encodings)
        self.non_video_encodings = [x for x in PREFERED_ENCODING_ORDER if x in nv_common]
        try:
            self.edge_encoding = [x for x in EDGE_ENCODING_ORDER if x in self.non_video_encodings][0]
        except:
            self.edge_encoding = None
        log("do_set_client_properties(%s) full_csc_modes=%s, video_scaling=%s, video_subregion=%s, non_video_encodings=%s, edge_encoding=%s, scaling_control=%s",
            properties, self.full_csc_modes, self.supports_video_scaling, self.video_subregion.supported, self.non_video_encodings, self.edge_encoding, self.scaling_control)

    def get_best_encoding_impl_default(self):
        if self.common_video_encodings:
            return self.get_best_encoding_video
        return WindowSource.get_best_encoding_impl_default(self)


    def get_best_encoding_video(self, pixel_count, ww, wh, speed, quality, current_encoding):
        """
            decide whether we send a full window update using the video encoder,
            or if a separate small region(s) is a better choice
        """
        def nonvideo(q=quality):
            s = max(0, min(100, speed))
            q = max(0, min(100, q))
            return self.get_best_nonvideo_encoding(pixel_count, ww, wh, s, q, self.non_video_encodings[0], self.non_video_encodings)

        def lossless(reason):
            log("get_best_encoding_video(..) temporarily switching to lossless mode for %8i pixels: %s", pixel_count, reason)
            s = max(0, min(100, speed))
            q = 100
            return self.get_best_nonvideo_encoding(pixel_count, ww, wh, s, q, self.non_video_encodings[0], self.non_video_encodings)

        if not self.non_video_encodings:
            return current_encoding
        if not self.common_video_encodings:
            return nonvideo()

        #ensure the dimensions we use for decision making are the ones actually used:
        cww = ww & self.width_mask
        cwh = wh & self.height_mask

        rgbmax = self._rgb_auto_threshold
        videomin = min(640*480, cww*cwh)
        sr = self.video_subregion.rectangle
        if sr:
            videomin = min(videomin, sr.width * sr.height)
            rgbmax = min(rgbmax, sr.width*sr.height//2)
        if pixel_count<=rgbmax or cww<8 or cwh<8:
            return lossless("low pixel count")

        if current_encoding!="auto" and current_encoding not in self.common_video_encodings:
            #not doing video, bail out:
            return nonvideo()

        if cww*cwh<=MAX_NONVIDEO_PIXELS or cww<16 or cwh<16:
            #window is too small!
            return nonvideo(quality+30)

        if cww<self.min_w or cww>self.max_w or cwh<self.min_h or cwh>self.max_h:
            #video encoder cannot handle this size!
            #(maybe this should be an 'assert' statement here?)
            return nonvideo()

        now = time.time()
        if now-self.statistics.last_resized<0.350:
            #window has just been resized, may still resize
            return nonvideo(quality-30)

        if self._current_quality!=quality or self._current_speed!=speed:
            #quality or speed override, best not to force video encoder re-init
            return nonvideo()

        if sr and ((sr.width&self.width_mask)!=cww or (sr.height&self.height_mask)!=cwh):
            #we have a video region, and this is not it, so don't use video
            #raise the quality as the areas around video tend to not be graphics
            return nonvideo(quality+30)

        lde = list(self.statistics.last_damage_events)
        lim = now-2
        pixels_last_2secs = sum(w*h for when,_,_,w,h in lde if when>lim)
        if pixels_last_2secs<5*videomin:
            #less than 5 full frames in last 2 seconds
            return nonvideo(quality+30)
        lim = now-0.5
        pixels_last_05secs = sum(w*h for when,_,_,w,h in lde if when>lim)
        if pixels_last_05secs<pixels_last_2secs//8:
            #framerate is dropping?
            return nonvideo(quality+30)

        if not self.is_shadow:
            #calculate the threshold for using video vs small regions:
            factors = (max(1, (speed-75)/5.0),                      #speed multiplier
                       1 + int(self.is_OR or self.is_tray)*2,       #OR windows tend to be static
                       max(1, 10-self._sequence),                   #gradual discount the first 9 frames, as the window may be temporary
                       1.0 / (int(bool(self._video_encoder)) + 1)   #if we have a video encoder already, make it more likely we'll use it:
                       )
            max_nvp = int(reduce(operator.mul, factors, MAX_NONVIDEO_PIXELS))
            if pixel_count<=max_nvp:
                #below threshold
                return nonvideo(quality+30)

        if cww<self.min_w or cww>self.max_w or cwh<self.min_h or cwh>self.max_h:
            #failsafe:
            return nonvideo()
        return current_encoding

    def get_best_nonvideo_encoding(self, pixel_count, ww, wh, speed, quality, current_encoding, options=[]):
        #if we're here, then the window has no alpha (or the client cannot handle alpha)
        #and we can ignore the current encoding
        options = options or self.non_video_encodings
        if pixel_count<self._rgb_auto_threshold:
            #high speed and high quality, rgb is still good
            if "rgb24" in options:
                return "rgb24"
            if "rgb32" in options:
                return "rgb32"
        #use sliding scale for lossless threshold
        #(high speed favours switching to lossy sooner)
        #take into account how many pixels need to be encoder:
        #more pixels means we switch to lossless more easily
        lossless_q = min(100, self._lossless_threshold_base + self._lossless_threshold_pixel_boost * pixel_count / (ww*wh))
        if quality<lossless_q:
            #lossy options:
            if "jpeg" in options:
                #assume that we have "turbojpeg",
                #which beats everything in terms of efficiency for lossy compression:
                return "jpeg"
            #avoid large areas (too slow), especially at low speed and high quality:
            if "webp" in options and pixel_count>16384:
                max_webp = 1024*1024 * (200-quality)/100 * speed/100
                if speed>30 and pixel_count<max_webp:
                    return "webp"
        else:
            #lossless options:
            #webp: don't enable it for "true" lossless (q>99) unless speed is high enough
            #because webp forces speed=100 for true lossless mode
            #also avoid very small and very large areas (both slow)
            if "webp" in options and (quality<100 or speed>=50) and pixel_count>16384:
                max_webp = 1024*1024 * (200-quality)/100 * speed/100
                if pixel_count<max_webp:
                    return "webp"
            if speed>75:
                if "rgb24" in options:
                    return "rgb24"
                if "rgb32" in options:
                    return "rgb32"
            if "png" in options:
                return "png"
        #we failed to find a good match, default to the first of the options..
        if options:
            return options[0]
        return None #can happen during cleanup!


    def cancel_damage(self):
        self.cancel_encode_from_queue()
        self.free_encode_queue_images()
        self.video_subregion.cancel_refresh_timer()
        self.scroll_data = None
        self.last_scroll_time = 0
        WindowSource.cancel_damage(self)
        #we must clean the video encoder to ensure
        #we will resend a key frame because we may be missing a frame
        self.cleanup_codecs()


    def full_quality_refresh(self, damage_options={}):
        if self.video_subregion.rectangle:
            if self.video_subregion.detection:
                #reset the video region on full quality refresh
                self.video_subregion.reset()
            else:
                #keep the region, but cancel the refresh:
                self.video_subregion.cancel_refresh_timer()
        self.scroll_data = None
        self.last_scroll_time = 0
        if self.non_video_encodings:
            #refresh the whole window in one go:
            damage_options["novideo"] = True
        WindowSource.full_quality_refresh(self, damage_options)


    def must_batch(self, delay):
        #force batching when using video region
        #because the video region code is in the send_delayed path
        return self.video_subregion.rectangle is not None or WindowSource.must_batch(self, delay)


    def get_speed(self, encoding):
        s = WindowSource.get_speed(self, encoding)
        #give a boost if we have a video region and this is not video:
        if self.video_subregion.rectangle and encoding not in self.video_encodings:
            s += 25
        return min(100, s)

    def get_quality(self, encoding):
        q = WindowSource.get_quality(self, encoding)
        #give a boost if we have a video region and this is not video:
        if self.video_subregion.rectangle and encoding not in self.video_encodings:
            q += 40
        return min(100, q)


    def client_decode_error(self, error, message):
        #maybe the stream is now corrupted..
        self.cleanup_codecs()
        WindowSource.client_decode_error(self, error, message)


    def timer_full_refresh(self):
        self.flush_video_encoder_now()
        WindowSource.timer_full_refresh(self)


    def get_refresh_exclude(self):
        #exclude video region (if any) from lossless refresh:
        return self.video_subregion.rectangle

    def refresh_subregion(self, regions):
        #callback from video subregion to trigger a refresh of some areas
        sublog("refresh_subregion(%s)", regions)
        if not regions or not self.can_refresh():
            return
        self.flush_video_encoder_now()
        now = time.time()
        encoding = self.auto_refresh_encodings[0]
        options = self.get_refresh_options()
        WindowSource.do_send_delayed_regions(self, now, regions, encoding, options, get_best_encoding=self.get_refresh_subregion_encoding)

    def get_refresh_subregion_encoding(self, *args):
        ww, wh = self.window_dimensions
        w, h = ww, wh
        vr = self.video_subregion.rectangle
        #could have been cleared by another thread:
        if vr:
            w, h = vr.width, vr.height
        return self.get_best_nonvideo_encoding(ww*wh, w, h, AUTO_REFRESH_SPEED, AUTO_REFRESH_QUALITY, self.auto_refresh_encodings[0], self.auto_refresh_encodings)

    def remove_refresh_region(self, region):
        #override so we can update the subregion timers / regions tracking:
        WindowSource.remove_refresh_region(self, region)
        self.video_subregion.remove_refresh_region(region)

    def add_refresh_region(self, region):
        #Note: this does not run in the UI thread!
        #returns the number of pixels in the region update
        #don't refresh the video region as part of normal refresh,
        #use subregion refresh for that
        vr = self.video_subregion.rectangle
        if vr is None:
            #no video region, normal code path:
            return WindowSource.add_refresh_region(self, region)
        if vr.contains_rect(region):
            #all of it is in the video region:
            self.video_subregion.add_video_refresh(region)
            return 0
        ir = vr.intersection_rect(region)
        if ir is None:
            #region is outside video region, normal code path:
            return WindowSource.add_refresh_region(self, region)
        #add intersection (rectangle in video region) to video refresh:
        self.video_subregion.add_video_refresh(ir)
        #add any rectangles not in the video region
        #(if any: keep track if we actually added anything)
        pixels_modified = 0
        for r in region.substract_rect(vr):
            pixels_modified += WindowSource.add_refresh_region(self, r)
        return pixels_modified

    def matches_video_subregion(self, width, height):
        vr = self.video_subregion.rectangle
        if not vr:
            return None
        mw = abs(width - vr.width) & self.width_mask
        mh = abs(height - vr.height) & self.height_mask
        if mw!=0 or mh!=0:
            return None
        return vr

    def subregion_is_video(self):
        vr = self.video_subregion.rectangle
        if not vr:
            return False
        events_count = self.statistics.damage_events_count - self.video_subregion.set_at
        if events_count<MIN_VIDEO_EVENTS:
            return False
        if self.video_subregion.fps<MIN_VIDEO_FPS:
            return False
        return True


    def do_send_delayed_regions(self, damage_time, regions, coding, options):
        """
            Overriden here so we can try to intercept the video_subregion if one exists.
        """
        vr = self.video_subregion.rectangle
        #overrides the default method for finding the encoding of a region
        #so we can ensure we don't use the video encoder when we don't want to:
        def send_nonvideo(regions=regions, encoding=coding, exclude_region=None, get_best_encoding=self.get_best_nonvideo_encoding):
            if self.b_frame_flush_timer and exclude_region is None:
                #a b-frame is already due, don't clobber it!
                exclude_region = vr
            WindowSource.do_send_delayed_regions(self, damage_time, regions, encoding, options, exclude_region=exclude_region, get_best_encoding=get_best_encoding)

        if self.is_tray:
            sublog("BUG? video for tray - don't use video region!")
            return send_nonvideo(encoding=None)

        if coding!="auto" and coding not in self.video_encodings:
            sublog("not a video encoding: %s" % coding)
            #keep current encoding selection function
            return send_nonvideo(get_best_encoding=self.get_best_encoding)

        if options.get("novideo"):
            sublog("video disabled in options")
            return send_nonvideo(encoding=None)

        if not vr:
            sublog("no video region, we may use the video encoder for something else")
            WindowSource.do_send_delayed_regions(self, damage_time, regions, coding, options)
            return
        assert not self.full_frames_only

        actual_vr = None
        if vr in regions:
            #found the video region the easy way: exact match in list
            actual_vr = vr
        else:
            #find how many pixels are within the region (roughly):
            #find all unique regions that intersect with it:
            inter = [x for x in (vr.intersection_rect(r) for r in regions) if x is not None]
            if len(inter)>0:
                #merge all regions into one:
                in_region = merge_all(inter)
                pixels_in_region = vr.width*vr.height
                pixels_intersect = in_region.width*in_region.height
                if pixels_intersect>=pixels_in_region*40/100:
                    #we have at least 40% of the video region
                    #that needs refreshing, do it:
                    actual_vr = vr

            #still no luck?
            if actual_vr is None:
                #try to find one that has the same dimensions:
                same_d = [r for r in regions if r.width==vr.width and r.height==vr.height]
                if len(same_d)==1:
                    #probably right..
                    actual_vr = same_d[0]
                elif len(same_d)>1:
                    #find one that shares at least one coordinate:
                    same_c = [r for r in same_d if r.x==vr.x or r.y==vr.y]
                    if len(same_c)==1:
                        actual_vr = same_c[0]

        if actual_vr is None:
            sublog("send_delayed_regions: video region %s not found in: %s", vr, regions)
        else:
            #found the video region:
            #sanity check in case the window got resized since:
            ww, wh = self.window.get_dimensions()
            if actual_vr.x+actual_vr.width>ww or actual_vr.y+actual_vr.height>wh:
                sublog("video region partially outside the window")
                return send_nonvideo(encoding=None)
            #send this using the video encoder:
            video_options = options.copy()
            video_options["av-sync"] = True
            self.process_damage_region(damage_time, actual_vr.x, actual_vr.y, actual_vr.width, actual_vr.height, coding, video_options, 0)

            #now substract this region from the rest:
            trimmed = []
            for r in regions:
                trimmed += r.substract_rect(actual_vr)
            if not trimmed:
                sublog("send_delayed_regions: nothing left after removing video region %s", actual_vr)
                return
            sublog("send_delayed_regions: substracted %s from %s gives us %s", actual_vr, regions, trimmed)
            regions = trimmed

        #merge existing damage delayed region if there is one:
        #(this codepath can fire from a video region refresh callback)
        dr = self._damage_delayed
        if dr:
            regions = dr[1] + regions
            damage_time = min(damage_time, dr[0])
            self._damage_delayed = None
            self.cancel_expire_timer()
        #decide if we want to send the rest now or delay some more,
        #only delay once the video encoder has dealt with a few frames:
        event_count = max(0, self.statistics.damage_events_count - self.video_subregion.set_at)
        if event_count<100:
            delay = 0
        else:
            #non-video is delayed at least 50ms, 4 times the batch delay, but no more than non_max_wait:
            elapsed = int(1000.0*(time.time()-damage_time))
            delay = max(self.batch_config.delay*4, 50)
            delay = min(delay, self.video_subregion.non_max_wait-elapsed)
        if delay<=25:
            send_nonvideo(regions=regions, encoding=None, exclude_region=actual_vr)
        else:
            self._damage_delayed = damage_time, regions, coding, options or {}
            sublog("send_delayed_regions: delaying non video regions %s some more by %ims", regions, delay)
            self.expire_timer = self.timeout_add(int(delay), self.expire_delayed_region, delay)

    def must_encode_full_frame(self, encoding):
        return self.full_frames_only or (encoding in self.video_encodings) or not self.non_video_encodings


    def process_damage_region(self, damage_time, x, y, w, h, coding, options, flush=0):
        """
            Called by 'damage' or 'send_delayed_regions' to process a damage region.

            Actual damage region processing:
            we extract the rgb data from the pixmap and:
            * if doing av-sync, we place the data on the encode queue with a timer,
              when the timer fires, we queue the work for the damage thread
            * without av-sync, we just queue the work immediately
            The damage thread will call make_data_packet_cb which does the actual compression.
            This runs in the UI thread.
        """
        assert self.ui_thread == threading.current_thread()
        assert coding is not None
        if w==0 or h==0:
            return
        if not self.window.is_managed():
            log("the window %s is not composited!?", self.window)
            return
        self._sequence += 1
        sequence = self._sequence
        if self.is_cancelled(sequence):
            log("get_window_pixmap: dropping damage request with sequence=%s", sequence)
            return

        rgb_request_time = time.time()
        image = self.window.get_image(x, y, w, h, logger=log)
        if image is None:
            log("get_window_pixmap: no pixel data for window %s, wid=%s", self.window, self.wid)
            return
        if self.is_cancelled(sequence):
            image.free()
            return
        self.pixel_format = image.get_pixel_format()
        #image may have been clipped to the new window size during resize:
        w = image.get_width()
        h = image.get_height()

        av_delay = self.get_frame_encode_delay(options)
        must_freeze = av_delay>=0 or coding in self.video_encodings
        if must_freeze:
            newstride = image.get_width()*4
            image.restride(newstride)
        av_delay = self.get_frame_encode_delay(options)
        def call_encode(ew, eh, eimage, encoding, eflush):
            self._sequence += 1
            sequence = self._sequence
            if self.is_cancelled(sequence):
                log("get_window_pixmap: dropping damage request with sequence=%s", sequence)
                return
            now = time.time()
            log("process_damage_region: wid=%i, adding pixel data to encode queue (%4ix%-4i - %5s), elapsed time: %.1f ms, request time: %.1f ms, frame delay=%ims",
                    self.wid, ew, eh, encoding, 1000*(now-damage_time), 1000*(now-rgb_request_time), av_delay)
            item = (ew, eh, damage_time, now, eimage, encoding, sequence, options, eflush)
            if av_delay<0:
                self.call_in_encode_thread(True, self.make_data_packet_cb, *item)
            else:
                self.encode_queue.append(item)
                self.schedule_encode_from_queue(av_delay)
        #now figure out if we need to send edges separately:
        if coding in self.video_encodings and self.edge_encoding and not VIDEO_SKIP_EDGE:
            dw = w - (w & self.width_mask)
            dh = h - (h & self.height_mask)
            if dw>0 and h>0:
                sub = image.get_sub_image(w-dw, 0, dw, h)
                call_encode(dw, h, sub, self.edge_encoding, flush+1+int(dh>0))
                w = w & self.width_mask
            if dh>0 and w>0:
                sub = image.get_sub_image(0, h-dh, w, dh)
                call_encode(dw, h, sub, self.edge_encoding, flush+1)
                h = h & self.height_mask
        #the main area:
        if w>0 and h>0:
            call_encode(w, h, image, coding, flush)

    def get_frame_encode_delay(self, options):
        if FORCE_AV_DELAY>0:
            return FORCE_AV_DELAY
        if options.get("av-sync", False):
            return -1
        l = len(self.encode_queue)
        if l>=self.encode_queue_max_size:
            #we must free some space!
            return 0
        return self.av_sync_delay

    def cancel_encode_from_queue(self):
        #free all items in the encode queue:
        self.encode_from_queue_due = 0
        eqt = self.encode_from_queue_timer
        avsynclog("cancel_encode_from_queue() timer=%s for wid=%i", eqt, self.wid)
        if eqt:
            self.encode_from_queue_timer = None
            self.source_remove(eqt)

    def free_encode_queue_images(self):
        eq = self.encode_queue
        avsynclog("free_encode_queue_images() freeing %i images for wid=%i", len(eq), self.wid)
        if not eq:
            return
        self.encode_queue = []
        for item in eq:
            try:
                self.free_image_wrapper(item[4])
            except:
                log.error("Error: cannot free image wrapper %s", item[4], exc_info=True)

    def schedule_encode_from_queue(self, av_delay):
        #must be called from the UI thread for synchronization
        #we ensure that the timer will fire no later than av_delay
        #re-scheduling it if it was due later than that
        due = time.time()+av_delay/1000.0
        if self.encode_from_queue_due==0 or due<self.encode_from_queue_due:
            self.cancel_encode_from_queue()
            self.encode_from_queue_due = due
            def timer_encode_from_queue():
                self.encode_from_queue_timer = None
                self.encode_from_queue_due = 0
                self.call_in_encode_thread(True, self.encode_from_queue)
            self.encode_from_queue_timer = self.timeout_add(av_delay, timer_encode_from_queue)

    def encode_from_queue(self):
        #note: we use a queue here to ensure we preserve the order
        #(so we encode frames in the same order they were grabbed)
        eq = self.encode_queue
        avsynclog("encode_from_queue: %s items for wid=%i", len(eq), self.wid)
        if not eq:
            return      #nothing to encode, must have been picked off already
        self.update_av_sync_delay()
        #find the first item which is due
        #in seconds, same as time.time():
        if len(self.encode_queue)>=self.encode_queue_max_size:
            av_delay = 0        #we must free some space!
        elif FORCE_AV_DELAY>0:
            av_delay = FORCE_AV_DELAY/1000.0
        else:
            av_delay = self.av_sync_delay/1000.0
        now = time.time()
        still_due = []
        remove = []
        index = 0
        item = None
        sequence = None
        done_packet = False     #only one packet per iteration
        try:
            for index,item in enumerate(eq):
                #item = (w, h, damage_time, now, image, coding, sequence, options, flush)
                sequence = item[6]
                if self.is_cancelled(sequence):
                    self.free_image_wrapper(item[4])
                    remove.append(index)
                    continue
                ts = item[3]
                due = ts + av_delay
                if due<=now and not done_packet:
                    #found an item which is due
                    remove.append(index)
                    avsynclog("encode_from_queue: processing item %s/%s (overdue by %ims)", index+1, len(self.encode_queue), int(1000*(now-due)))
                    self.make_data_packet_cb(*item)
                    done_packet = True
                else:
                    #we only process only one item per call (see "done_packet")
                    #and just keep track of extra ones:
                    still_due.append(int(1000*(due-now)))
        except Exception:
            if not self.is_cancelled(sequence):
                avsynclog.error("error processing encode queue at index %i", index)
                avsynclog.error("item=%s", item, exc_info=True)
        #remove the items we've dealt with:
        #(in reverse order since we pop them from the queue)
        if remove:
            for x in reversed(remove):
                eq.pop(x)
        #if there are still some items left in the queue, re-schedule:
        if len(still_due)==0:
            avsynclog("encode_from_queue: nothing due")
            return
        first_due = max(0, min(still_due))
        avsynclog("encode_from_queue: first due in %ims, due list=%s (av-sync delay=%i, actual=%i, for wid=%i)", first_due, still_due, self.av_sync_delay, av_delay, self.wid)
        self.idle_add(self.schedule_encode_from_queue, first_due)


    def update_encoding_options(self, force_reload=False):
        """
            This is called when we want to force a full re-init (force_reload=True)
            or from the timer that allows to tune the quality and speed.
            (this tuning is done in WindowSource.reconfigure)
            Here we re-evaluate if the pipeline we are currently using
            is really the best one, and if not we invalidate it.
            This uses get_video_pipeline_options() to get a list of pipeline
            options with a score for each.

            Can be called from any thread.
        """
        WindowSource.update_encoding_options(self, force_reload)
        self.common_video_encodings = [x for x in PREFERED_ENCODING_ORDER if x in self.video_encodings and x in self.core_encodings]
        log("update_encoding_options(%s) common_video_encodings=%s, csc_encoder=%s, video_encoder=%s", force_reload, self.common_video_encodings, self._csc_encoder, self._video_encoder)
        if (self.encoding!="auto" and self.encoding not in self.common_video_encodings) or \
            self.full_frames_only or STRICT_MODE or not self.non_video_encodings or not self.common_video_encodings or \
            (self._mmap and self._mmap_size>0):
            #cannot use video subregions
            #FIXME: small race if a refresh timer is due when we change encoding - meh
            self.video_subregion.reset()
        else:
            ww, wh = self.window_dimensions
            self.video_subregion.identify_video_subregion(ww, wh, self.statistics.damage_events_count, self.statistics.last_damage_events, self.statistics.last_resized)
            if self.video_subregion.rectangle:
                #when we have a video region, lower the lossless threshold
                #especially for small regions
                self._lossless_threshold_base = min(80, 10+self._current_speed//5)
                self._lossless_threshold_pixel_boost = 90-self._current_speed//5
        if force_reload:
            self.cleanup_codecs()
        self.check_pipeline_score(force_reload)

    def check_pipeline_score(self, force_reload):
        """
            Calculate pipeline scores using get_video_pipeline_options(),
            and schedule the cleanup of the current video pipeline elements
            which are no longer the best options.

            Can be called from any thread.
        """
        if self._mmap and self._mmap_size>0:
            scorelog("cannot score: mmap enabled")
            return
        elapsed = time.time()-self._last_pipeline_check
        if not force_reload and elapsed<0.75:
            scorelog("cannot score: only %ims since last check", 1000*elapsed)
            #already checked not long ago
            return
        if not self.pixel_format:
            scorelog("cannot score: no pixel format!")
            #we need to know what pixel format we create pipelines for!
            return
        def checknovideo(*info):
            #for whatever reason, we shouldn't be using a video encoding,
            #get_best_encoding() should ensure we don't end up with one
            #it duplicates some of these same checks
            scorelog(*info)
            self.cleanup_codecs()
        #do some sanity checks to see if there is any point in finding a suitable video encoding pipeline:
        if self._sequence<2 or self._damage_cancelled>=float("inf"):
            #too early, or too late!
            return checknovideo("sequence=%s (cancelled=%s)", self._sequence, self._damage_cancelled)
        #which video encodings to evaluate:
        if self.encoding=="auto":
            eval_encodings = self.common_video_encodings
        else:
            if self.encoding not in self.common_video_encodings:
                return checknovideo("non-video / unsupported encoding: %s", self.encoding)
            eval_encodings = [self.encoding]
        ww, wh = self.window_dimensions
        w = ww & self.width_mask
        h = wh & self.height_mask
        vs = self.video_subregion
        if vs:
            r = vs.rectangle
            if r:
                w = r.width & self.width_mask
                h = r.height & self.width_mask
        if w<self.min_w or w>self.max_w or h<self.min_h or h>self.max_h:
            return checknovideo("out of bounds: %sx%s (min %sx%s, max %sx%s)", w, h, self.min_w, self.min_h, self.max_w, self.max_h)
        #if time.time()-self.statistics.last_resized<0.500:
        #    return checknovideo("resized just %.1f seconds ago", time.time()-self.statistics.last_resized)

        #must copy reference to those objects because of threading races:
        ve = self._video_encoder
        csce = self._csc_encoder
        if ve is not None and ve.is_closed():
            scorelog("cannot score: video encoder %s is closed or closing", ve)
            return
        if csce is not None and csce.is_closed():
            scorelog("cannot score: csc %s is closed or closing", csce)
            return

        scores = self.get_video_pipeline_options(eval_encodings, w, h, self.pixel_format, force_reload)
        if len(scores)==0:
            scorelog("check_pipeline_score(%s) no pipeline options found!", force_reload)
            return

        scorelog("check_pipeline_score(%s) best=%s", force_reload, scores[0])
        _, _, _, csc_width, csc_height, csc_spec, enc_in_format, _, enc_width, enc_height, encoder_spec = scores[0]
        clean = False
        if csce:
            if csc_spec is None:
                scorelog("check_pipeline_score(%s) csc is no longer needed: %s", force_reload, scores[0])
                clean = True
            elif csce.get_dst_format()!=enc_in_format:
                scorelog("check_pipeline_score(%s) change of csc output format from %s to %s", force_reload, csce.get_dst_format(), enc_in_format)
                clean = True
            elif csce.get_src_width()!=csc_width or csce.get_src_height()!=csc_height:
                scorelog("check_pipeline_score(%s) change of csc input dimensions from %ix%i to %ix%i", force_reload, csce.get_src_width(), csce.get_src_height(), csc_width, csc_height)
                clean = True
            elif csce.get_dst_width()!=enc_width or csce.get_dst_height()!=enc_height:
                scorelog("check_pipeline_score(%s) change of csc ouput dimensions from %ix%i to %ix%i", force_reload, csce.get_dst_width(), csce.get_dst_height(), enc_width, enc_height)
                clean = True
        if ve is None or clean:
            pass    #nothing to check or clean
        elif ve.get_src_format()!=enc_in_format:
            scorelog("check_pipeline_score(%s) change of video input format from %s to %s", force_reload, ve.get_src_format(), enc_in_format)
            clean = True
        elif ve.get_width()!=enc_width or ve.get_height()!=enc_height:
            scorelog("check_pipeline_score(%s) change of video input dimensions from %ix%i to %ix%i", force_reload, ve.get_width(), ve.get_height(), enc_width, enc_height)
            clean = True
        elif type(ve)!=encoder_spec.codec_class:
            scorelog("check_pipeline_score(%s) found a better video encoder class than %s: %s", force_reload, type(ve), scores[0])
            clean = True
        if clean:
            self.csc_encoder_clean()
            self.video_encoder_clean()
        self._last_pipeline_check = time.time()


    def get_video_pipeline_options(self, encodings, width, height, src_format, force_refresh=False):
        """
            Given a picture format (width, height and src pixel format),
            we find all the pipeline options that will allow us to compress
            it using the given encodings.
            First, we try with direct encoders (for those that support the
            source pixel format natively), then we try all the combinations
            using csc encoders to convert to an intermediary format.
            Each solution is rated and we return all of them in descending
            score (best solution comes first).
            Because this function is expensive to call, we cache the results.
            This allows it to run more often from the timer thread.

            Can be called from any thread.
        """
        if not force_refresh and (time.time()-self.last_pipeline_time<1) and self.last_pipeline_params and self.last_pipeline_params==(encodings, width, height, src_format):
            #keep existing scores
            scorelog("get_video_pipeline_options%s using cached values from %ims ago", (encodings, width, height, src_format, force_refresh), 1000.0*(time.time()-self.last_pipeline_time))
            return self.last_pipeline_scores
        scorelog("get_video_pipeline_options%s last params=%s", (encodings, width, height, src_format, force_refresh), self.last_pipeline_params)

        vh = self.video_helper
        if vh is None:
            return []       #closing down

        target_q = int(self._current_quality)
        min_q = self._fixed_min_quality
        target_s = int(self._current_speed)
        min_s = self._fixed_min_speed
        #tune quality target for (non-)video region:
        vr = self.matches_video_subregion(width, height)
        if vr and target_q<100:
            if self.subregion_is_video():
                #lower quality a bit more:
                fps = self.video_subregion.fps
                f = min(90, 2*fps)
                target_q = max(min_q, int(target_q*(100-f)//100))
                scorelog("lowering quality target %i by %i%% for video %s (fps=%i)", target_q, f, vr, fps)
            else:
                #not the video region, or not really video content, raise quality a bit:
                target_q = int(sqrt(target_q/100.0)*100)
                scorelog("raising quality for video encoding of non-video region")
        scorelog("get_video_pipeline_options%s speed: %s (min %s), quality: %s (min %s)", (encodings, width, height, src_format), target_s, min_s, target_q, min_q)
        scores = []
        for encoding in encodings:
            #these are the CSC modes the client can handle for this encoding:
            #we must check that the output csc mode for each encoder is one of those
            supported_csc_modes = self.full_csc_modes.get(encoding)
            if not supported_csc_modes:
                scorelog("get_video_pipeline_options: no supported csc modes for %s", encoding)
                continue
            encoder_specs = vh.get_encoder_specs(encoding)
            if not encoder_specs:
                scorelog("get_video_pipeline_options: no encoder specs for %s", encoding)
                continue
            def add_scores(info, csc_spec, enc_in_format):
                scorelog("add_scores(%s, %s, %s)", info, csc_spec, enc_in_format)
                #find encoders that take 'enc_in_format' as input:
                colorspace_specs = encoder_specs.get(enc_in_format)
                if not colorspace_specs:
                    #scorelog("add_scores: no matching colorspace specs for %s", enc_in_format)
                    return
                #log("%s encoding from %s: %s", info, pixel_format, colorspace_specs)
                for encoder_spec in colorspace_specs:
                    #ensure that the output of the encoder can be processed by the client:
                    matches = set(encoder_spec.output_colorspaces) & set(supported_csc_modes)
                    if not matches:
                        scorelog("add_scores: no matches for %s (%s and %s)", encoder_spec, encoder_spec.output_colorspaces, supported_csc_modes)
                        continue
                    scaling = self.calculate_scaling(width, height, encoder_spec.max_w, encoder_spec.max_h)
                    client_score_delta = self.encoding_options.get("%s.score-delta" % encoding, 0)
                    score_data = get_pipeline_score(enc_in_format, csc_spec, encoder_spec, width, height, scaling,
                                                    target_q, min_q, target_s, min_s,
                                                    self._csc_encoder, self._video_encoder, client_score_delta)
                    if score_data:
                        scores.append(score_data)
            if not FORCE_CSC or src_format==FORCE_CSC_MODE:
                add_scores("direct (no csc)", None, src_format)

            #now add those that require a csc step:
            csc_specs = vh.get_csc_specs(src_format)
            if csc_specs:
                #log("%s can also be converted to %s using %s", pixel_format, [x[0] for x in csc_specs], set(x[1] for x in csc_specs))
                #we have csc module(s) that can get us from pixel_format to out_csc:
                for out_csc, l in csc_specs.items():
                    actual_csc = self.csc_equiv(out_csc)
                    if not bool(FORCE_CSC_MODE) or FORCE_CSC_MODE==out_csc:
                        for csc_spec in l:
                            add_scores("via %s (%s)" % (out_csc, actual_csc), csc_spec, out_csc)
        s = sorted(scores, key=lambda x : -x[0])
        scorelog("get_video_pipeline_options%s scores=%s", (encodings, width, height, src_format), s)
        self.last_pipeline_params = (encodings, width, height, src_format)
        self.last_pipeline_scores = s
        self.last_pipeline_time = time.time()
        return s

    def csc_equiv(self, csc_mode):
        #in some places, we want to check against the subsampling used
        #and not the colorspace itself.
        #and NV12 uses the same subsampling as YUV420P...
        return {"NV12" : "YUV420P",
                "BGRX" : "YUV444P"}.get(csc_mode, csc_mode)


    def calculate_scaling(self, width, height, max_w=4096, max_h=4096):
        if width==0 or height==0:
            return (1, 1)
        q = self._current_quality
        s = self._current_speed
        actual_scaling = self.scaling
        now = time.time()
        def get_min_required_scaling():
            if width<=max_w and height<=max_h:
                return (1, 1)       #no problem
            #most encoders can't deal with that!
            TRY_SCALE = ((2, 3), (1, 2), (1, 3), (1, 4), (1, 8), (1, 10))
            for op, d in TRY_SCALE:
                if width*op/d<=max_w and height*op/d<=max_h:
                    return (op, d)
            raise Exception("BUG: failed to find a scaling value for window size %sx%s", width, height)
        if not SCALING or not self.supports_video_scaling:
            #not supported by client or disabled by env
            #FIXME: what to do if width>max_w or height>max_h?
            actual_scaling = 1, 1
        elif self.scaling_control==0:
            #only enable if we have to:
            actual_scaling = get_min_required_scaling()
        elif SCALING_HARDCODED:
            actual_scaling = tuple(SCALING_HARDCODED)
            scalinglog("using hardcoded scaling: %s", actual_scaling)
        elif actual_scaling is None and (width>max_w or height>max_h):
            #most encoders can't deal with that!
            actual_scaling = get_min_required_scaling()
        elif actual_scaling is None and not self.is_shadow and self.statistics.damage_events_count>50 \
             and (now-self.statistics.last_resized>0.5) and (now-self.last_scroll_time)>5:
            #no scaling window attribute defined, so use heuristics to enable:
            if self.matches_video_subregion(width, height):
                ffps = self.video_subregion.fps
                if self.subregion_is_video():
                    #enable scaling more aggressively
                    sc = (self.scaling_control+50)*2
                else:
                    sc = (self.scaling_control+25)
            else:
                #not the video region, so much less aggressive scaling:
                sc = max(0, (self.scaling_control-50)//2)
                #calculate full frames per second (measured in pixels vs window size):
                ffps = 0
                stime = now-5           #only look at the last 5 seconds max
                lde = [x for x in list(self.statistics.last_damage_events) if x[0]>stime]
                if len(lde)>10:
                    #the first event's first element is the oldest event time:
                    otime = lde[0][0]
                    if now>otime:
                        pixels = sum(w*h for _,_,_,w,h in lde)
                        ffps = int(pixels/(width*height)/(now - otime))

            #if scaling_control is high (scaling_control=100 -> er=2)
            #then we will match the heuristics more quickly:
            er = sc/50.0
            if self.actual_scaling!=(1, 1):
                #if we are already downscaling, boost so we will stick with it a bit longer:
                #more so if we are downscaling a lot (1/3 -> er=1.5 + ..)
                er += (0.5 * self.actual_scaling[1] / self.actual_scaling[0])
            qs = s>(q-er*10) and q<(50+er*15)
            #scalinglog("calculate_scaling: er=%.1f, qs=%s, ffps=%s", er, qs, ffps)

            if self.fullscreen and (qs or ffps>=max(2, 10-er*3)):
                actual_scaling = 1,3
            elif self.maximized and (qs or ffps>=max(2, 10-er*3)):
                actual_scaling = 1,2
            elif width*height>=(2560-er*768)*1600 and (qs or ffps>=max(4, 25-er*5)):
                actual_scaling = 1,3
            elif width*height>=(1920-er*384)*1200 and (qs or ffps>=max(5, 30-er*10)):
                actual_scaling = 2,3
            elif width*height>=(1200-er*256)*1024 and (qs or ffps>=max(10, 50-er*15)):
                actual_scaling = 2,3
            if actual_scaling:
                scalinglog("calculate_scaling value %s enabled by heuristics for %ix%i q=%i, s=%i, er=%.1f, qs=%s, ffps=%i, scaling-control(%i)=%i", actual_scaling, width, height, q, s, er, qs, ffps, self.scaling_control, sc)
        if actual_scaling is None:
            actual_scaling = 1, 1
        v, u = actual_scaling
        if v/u>1.0:
            #never upscale before encoding!
            actual_scaling = 1, 1
        elif float(v)/float(u)<0.1:
            #don't downscale more than 10 times! (for each dimension - that's 100 times!)
            actual_scaling = 1, 10
        scalinglog("calculate_scaling%s=%s (q=%s, s=%s, scaling_control=%s)", (width, height, max_w, max_h), actual_scaling, q, s, self.scaling_control)
        return actual_scaling


    def check_pipeline(self, encoding, width, height, src_format):
        """
            Checks that the current pipeline is still valid
            for the given input. If not, close it and make a new one.

            Runs in the 'encode' thread.
        """
        if encoding=="auto":
            encodings = self.common_video_encodings
        else:
            encodings = [encoding]
        if self.do_check_pipeline(encodings, width, height, src_format):
            return True  #OK!

        videolog("check_pipeline%s setting up a new pipeline as check failed - encodings=%s", (encoding, width, height, src_format), encodings)
        #cleanup existing one if needed:
        csce = self._csc_encoder
        if csce:
            self._csc_encoder = None
            csce.clean()
        ve = self._video_encoder
        if ve:
            self._video_encoder = None
            ve.clean()
        #and make a new one:
        w = width & self.width_mask
        h = height & self.height_mask
        scores = self.get_video_pipeline_options(encodings, w, h, src_format)
        return self.setup_pipeline(scores, width, height, src_format)

    def do_check_pipeline(self, encodings, width, height, src_format):
        """
            Checks that the current pipeline is still valid
            for the given input.

            Runs in the 'encode' thread.
        """
        #use aliases, not because of threading (we are in the encode thread anyway)
        #but to make the code less dense:
        ve = self._video_encoder
        csce = self._csc_encoder
        if ve is None or ve.is_closed() or (csce and csce.is_closed()):
            return False

        if csce:
            csc_width = width & self.width_mask
            csc_height = height & self.height_mask
            if csce.get_src_format()!=src_format:
                csclog("do_check_pipeline csc: switching source format from %s to %s",
                                    csce.get_src_format(), src_format)
                return False
            elif csce.get_src_width()!=csc_width or csce.get_src_height()!=csc_height:
                csclog("do_check_pipeline csc: window dimensions have changed from %sx%s to %sx%s, csc info=%s",
                                    csce.get_src_width(), csce.get_src_height(), csc_width, csc_height, csce.get_info())
                return False
            elif csce.get_dst_format()!=ve.get_src_format():
                csclog.error("Error: CSC intermediate format mismatch: %s vs %s, csc info=%s",
                                    csce.get_dst_format(), ve.get_src_format(), csce.get_info())
                return False

            #encoder will take its input from csc:
            encoder_src_width = csce.get_dst_width()
            encoder_src_height = csce.get_dst_height()
        else:
            #direct to video encoder without csc:
            encoder_src_width = width & self.width_mask
            encoder_src_height = height & self.height_mask

            if ve.get_src_format()!=src_format:
                videolog("do_check_pipeline video: invalid source format %s, expected %s",
                                                ve.get_src_format(), src_format)
                return False

        if ve.get_encoding() not in encodings:
            videolog("do_check_pipeline video: invalid encoding %s, expected one of: %s",
                                            ve.get_encoding(), csv(encodings))
            return False
        elif ve.get_width()!=encoder_src_width or ve.get_height()!=encoder_src_height:
            videolog("do_check_pipeline video: window dimensions have changed from %sx%s to %sx%s",
                                            ve.get_width(), ve.get_height(), encoder_src_width, encoder_src_height)
            return False
        return True


    def setup_pipeline(self, scores, width, height, src_format):
        """
            Given a list of pipeline options ordered by their score
            and an input format (width, height and source pixel format),
            we try to create a working video pipeline (csc + encoder),
            trying each option until one succeeds.
            (some may not be suitable because of scaling?)

            Runs in the 'encode' thread.
        """
        assert width>0 and height>0, "invalid dimensions: %sx%s" % (width, height)
        start = time.time()
        if len(scores)==0:
            if not self.is_cancelled():
                log.error("Error: no video pipeline options found for %s at %ix%i", src_format, width, height)
            return False
        videolog("setup_pipeline%s", (scores, width, height, src_format))
        for option in scores:
            try:
                videolog("setup_pipeline: trying %s", option)
                if self.setup_pipeline_option(width, height, src_format, *option):
                    #success!
                    return True
                else:
                    #skip cleanup below
                    continue
            except TransientCodecException as e:
                videolog.warn("setup_pipeline failed for %s: %s", option, e)
            except:
                videolog.warn("setup_pipeline failed for %s", option, exc_info=True)
            #we're here because an exception occurred, cleanup before trying again:
            csce = self._csc_encoder
            if csce:
                self._csc_encoder = None
                csce.clean()
            ve = self._video_encoder
            if ve:
                self._video_encoder = None
                ve.clean()
        end = time.time()
        if not self.is_cancelled():
            videolog("setup_pipeline(..) failed! took %.2fms", (end-start)*1000.0)
            videolog.error("Error: failed to setup a video pipeline for %s at %ix%i", src_format, width, height)
            videolog.error(" tried the following option%s", engs(scores))
            for option in scores:
                videolog.error(" %s", option)
        return False

    def setup_pipeline_option(self, width, height, src_format,
                      _score, scaling, _csc_scaling, csc_width, csc_height, csc_spec,
                      enc_in_format, encoder_scaling, enc_width, enc_height, encoder_spec):
        speed = self._current_speed
        quality = self._current_quality
        min_w = 1
        min_h = 1
        max_w = 16384
        max_h = 16384
        if csc_spec:
            #TODO: no need to OR encoder mask if we are scaling...
            width_mask = csc_spec.width_mask & encoder_spec.width_mask
            height_mask = csc_spec.height_mask & encoder_spec.height_mask
            min_w = max(min_w, csc_spec.min_w)
            min_h = max(min_h, csc_spec.min_h)
            max_w = min(max_w, csc_spec.max_w)
            max_h = min(max_h, csc_spec.max_h)
            #csc speed is not very important compared to encoding speed,
            #so make sure it never degrades quality
            csc_speed = min(speed, 100-quality/2.0)
            csc_start = time.time()
            csce = csc_spec.make_instance()
            csce.init_context(csc_width, csc_height, src_format,
                                   enc_width, enc_height, enc_in_format, csc_speed)
            csc_end = time.time()
            csclog("setup_pipeline: csc=%s, info=%s, setup took %.2fms",
                  csce, csce.get_info(), (csc_end-csc_start)*1000.0)
        else:
            csce = None
            #use the encoder's mask directly since that's all we have to worry about!
            width_mask = encoder_spec.width_mask
            height_mask = encoder_spec.height_mask
            #restrict limits:
            min_w = max(min_w, encoder_spec.min_w)
            min_h = max(min_h, encoder_spec.min_h)
            max_w = min(max_w, encoder_spec.max_w)
            max_h = min(max_h, encoder_spec.max_h)
            if encoder_scaling!=(1,1) and not encoder_spec.can_scale:
                videolog("scaling is now enabled, so skipping %s", encoder_spec)
                return False
        self._csc_encoder = csce
        enc_start = time.time()
        #FIXME: filter dst_formats to only contain formats the encoder knows about?
        dst_formats = self.full_csc_modes.get(encoder_spec.encoding)
        ve = encoder_spec.make_instance()
        options = self.encoding_options.copy()
        options.update(self.get_video_encoder_options(encoder_spec.encoding, width, height))
        ve.init_context(enc_width, enc_height, enc_in_format, dst_formats, encoder_spec.encoding, quality, speed, encoder_scaling, options)
        #record new actual limits:
        self.actual_scaling = scaling
        self.width_mask = width_mask
        self.height_mask = height_mask
        self.min_w = min_w
        self.min_h = min_h
        self.max_w = max_w
        self.max_h = max_h
        enc_end = time.time()
        self.start_video_frame = 0
        self._video_encoder = ve
        videolog("setup_pipeline: csc=%s, video encoder=%s, info: %s, setup took %.2fms",
                csce, ve, ve.get_info(), (enc_end-enc_start)*1000.0)
        scalinglog("setup_pipeline: scaling=%s, encoder_scaling=%s", scaling, encoder_scaling)
        return  True

    def get_video_encoder_options(self, encoding, width, height):
        #tweaks for "real" video:
        if self.matches_video_subregion(width, height) and self.subregion_is_video() and (time.time()-self.last_scroll_time)>5:
            return {
                       "source"     : "video",
                       #could take av-sync into account here to choose the number of b-frames:
                       "b-frames"   : int(B_FRAMES and (encoding in self.supports_video_b_frames)),
                       }
        return {}


    def make_draw_packet(self, x, y, w, h, coding, data, outstride, client_options={}, options={}):
        #overriden so we can invalidate the scroll data:
        #log.error("make_draw_packet%s", (x, y, w, h, coding, "..", outstride, client_options)
        packet = WindowSource.make_draw_packet(self, x, y, w, h, coding, data, outstride, client_options)
        lsd = self.scroll_data
        if lsd and not options.get("scroll"):
            rect, csums = lsd
            if client_options.get("scaled_size") or client_options.get("quality", 100)<20:
                #don't scroll low quality content, better to refresh it
                scrolllog("low quality %s update, invalidating all scroll data (scaled_size=%s, quality=%s)", coding, client_options.get("scaled_size"), client_options.get("quality", 100))
                self.scroll_data = None
            else:
                #do they intersect?
                inter = rect.intersection(x, y, w, h)
                if inter:
                    #remove any lines that have been updated
                    #by zeroing out their checksums:
                    assert len(csums)==rect.height
                    assert inter.y>=rect.y and inter.y+inter.height<=rect.y+rect.height
                    #the array indexes are relative to rect.y:
                    start_y = inter.y-rect.y
                    for iy in range(start_y, start_y+inter.height):
                        csums[iy] = 0
                    nonzero = len([True for v in csums if v!=0])
                    scrolllog("removed %i lines checksums from intersection of scroll area %s and %s draw packet %s, remains %i", inter.height, rect, coding, (x, y, w, h), nonzero)
                    #if more than half has already been invalidated, drop it completely:
                    if nonzero<=rect.height//2:
                        scrolllog("invalidating whole scroll data as only %i of it remains valid", 100*nonzero//rect.height)
                        self.scroll_data = None
        return packet


    def encode_scrolling(self, image, distances, old_csums, csums, options):
        start = time.time()
        try:
            del options["av-sync"]
        except:
            pass
        #tells make_data_packet not to invalidate the scroll data:
        scrolllog("encode_scrolling(%s, {..}, [], [], %s) window-dimensions=%s", image, options, self.window_dimensions)
        x, y, w, h = image.get_geometry()[:4]
        yscroll_values = []
        max_scroll_regions = 50
        #process distances with the highest score first:
        for hits in reversed(sorted(distances.values())):
            for scroll in (d for d,v in distances.items() if v==hits):
                assert scroll<h
                yscroll_values.append(scroll)
            if len(yscroll_values)>=max_scroll_regions:
                break
        assert yscroll_values
        #always add zero=no-change so we can drop those updates!
        if 0 not in yscroll_values and 0 in distances:
            #(but do this last so we don't end up cutting too many rectangles)
            yscroll_values.append(0)
        scrolllog(" will send scroll packets for yscroll=%s", yscroll_values)
        #keep track of the lines we have handled already:
        #(the same line may be available from multiple scroll directions)
        handled = set()
        scrolls = []
        max_scrolls = 1000
        for s in yscroll_values:
            #find all the lines that scroll by this much:
            slines = match_distance(old_csums, csums, s)
            assert slines, "no lines matching distance %i" % s
            #remove any lines we have already handled:
            lines = [v+s for v in slines if ((v+s) not in handled and v not in handled)]
            if not lines:
                continue
            #and them to the handled set so we don't try to paint them again:
            handled = handled.union(set(lines))
            if s==0:
                scrolllog(" %i lines have not changed", len(lines))
            else:
                #things have actually moved
                #aggregate consecutive lines into rectangles:
                cl = consecutive_lines(lines)
                #scrolllog(" scroll groups for distance=%i : %s=%s", s, lines, cl)
                scrolllog(" scroll groups for distance=%i : %s", s, cl)
                for sy,sh in cl:
                    #new rectangle
                    scrolls.append((x, y+sy-s, w, sh, 0, s))
                    if len(scrolls)>max_scrolls:
                        break
                if len(scrolls)>max_scrolls:
                    break

        non_scroll = []
        remaining = set(range(h))-handled
        if remaining:
            damaged_lines = sorted(list(remaining))
            non_scroll = consecutive_lines(damaged_lines)
            scrolllog(" non scroll: %i packets: %s", len(non_scroll), non_scroll)
        if len(scrolls)>=20 or len(non_scroll)>20:
            #avoid fragmentation, which is too costly
            #(too many packets, too many loops through the encoder code)
            scrolllog("too many items: %i scrolls, %i non-scrolls - sending just one image instead", len(scrolls), len(non_scroll))
            scrolls = []
            non_scroll = [(0, h)]
        flush = len(non_scroll)
        #send as scroll paints packets:
        if scrolls:
            client_options = options.copy()
            if flush>0 and self.supports_flush:
                client_options["flush"] = flush
            coding = "scroll"
            end = time.time()
            packet = self.make_draw_packet(x, y, w, h, coding, LargeStructure(coding, scrolls), 0, client_options, options)
            self.queue_damage_packet(packet)
            compresslog("compress: %5.1fms for %4ix%-4i pixels at %4i,%-4i for wid=%-5i using %6s as %3i rectangles  (%5iKB)           , sequence %5i, client_options=%s",
                 (end-start)*1000.0, w, h, x, y, self.wid, coding, len(scrolls), w*h*4/1024, self._damage_packet_sequence, client_options)
        #send the rest as rectangles:
        if non_scroll:
            start = time.time()
            for sy, sh in non_scroll:
                sub = image.get_sub_image(0, sy, w, sh)
                flush -= 1
                ret = self.video_fallback(sub, options)
                if not ret:
                    self.free_image_wrapper(sub)
                    #cancelled?
                    return None
                coding, data, client_options, outw, outh, outstride, _ = ret
                assert data
                client_options = options.copy()
                if flush>0 and self.supports_flush:
                    client_options["flush"] = flush
                packet = self.make_draw_packet(sub.get_x(), sub.get_y(), outw, outh, coding, data, outstride, client_options, options)
                self.queue_damage_packet(packet)
                self.free_image_wrapper(sub)
                psize = w*sh*4
                csize = len(data)
                end = time.time()
                compresslog("compress: %5.1fms for %4ix%-4i pixels at %4i,%-4i for wid=%-5i using %6s with ratio %5.1f%%  (%5iKB to %5iKB), sequence %5i, client_options=%s",
                     (end-start)*1000.0, w, sh, 0, sy, self.wid, coding, 100.0*csize/psize, psize/1024, csize/1024, self._damage_packet_sequence, client_options)
            scrolllog("non-scroll encoding took %ims for %i rectangles", (time.time()-start)*1000, len(non_scroll))
        assert flush==0
        self.last_scroll_time = time.time()
        return None

    def video_fallback(self, image, options, order=PREFERED_ENCODING_ORDER):
        #find one that is not video:
        fallback_encodings = [x for x in order if (x in self.non_video_encodings and x in self._encoders and x!="mmap")]
        if not fallback_encodings:
            if not self.is_cancelled():
                log.error("no non-video fallback encodings are available!")
            return None
        fallback_encoding = fallback_encodings[0]
        encode_fn = self._encoders[fallback_encoding]
        return encode_fn(fallback_encoding, image, options)

    def video_encode(self, encoding, image, options):
        try:
            return self.do_video_encode(encoding, image, options)
        finally:
            self.free_image_wrapper(image)

    def do_video_encode(self, encoding, image, options):
        """
            This method is used by make_data_packet to encode frames using video encoders.
            Video encoders only deal with fixed dimensions,
            so we must clean and reinitialize the encoder if the window dimensions
            has changed.

            Runs in the 'encode' thread.
        """
        x, y, w, h = image.get_geometry()[:4]
        src_format = image.get_pixel_format()
        stride = image.get_rowstride()
        img_data = None
        if self.pixel_format!=src_format:
            if self.is_cancelled():
                return None
            videolog.warn("Warning: image pixel format unexpectedly changed from %s to %s", self.pixel_format, src_format)
            self.pixel_format = src_format

        if SAVE_VIDEO_FRAMES:
            from xpra.os_util import memoryview_to_bytes
            from PIL import Image
            img_data = image.get_pixels()
            rgb_format = image.get_pixel_format() #ie: BGRA
            img = Image.frombuffer("RGBA", (w, h), memoryview_to_bytes(img_data), "raw", rgb_format.replace("BGRX", "BGRA"), stride)
            kwargs = {}
            if SAVE_VIDEO_FRAMES=="jpeg":
                kwargs = {
                          "quality"     : 0,
                          "optimize"    : False,
                          }
            t = time.time()
            tstr = time.strftime("%H-%M-%S", time.localtime(t))
            filename = "./W%i-VDO-%s.%03i.%s" % (self.wid, tstr, (t*1000)%1000, SAVE_VIDEO_FRAMES)
            videolog("do_present_fbo: saving %4ix%-4i pixels, %7i bytes to %s", w, h, (stride*h), filename)
            img.save(filename, SAVE_VIDEO_FRAMES, **kwargs)

        test_scrolling = self.supports_scrolling and not STRICT_MODE
        if test_scrolling and self.b_frame_flush_timer:
            scrolllog("not testing scrolling: b_frame_flush_timer=%s", self.b_frame_flush_timer)
            test_scrolling = False
            self.scroll_data = None
        elif test_scrolling:
            lsd = self.scroll_data
            try:
                start = time.time()
                if not image.is_thread_safe():
                    stride = image.get_width()*4
                    image.restride(stride)
                img_data = img_data or image.get_pixels()
                if not img_data:
                    return None
                csums = CRC_Image(img_data, w, h, stride)
                if csums:
                    self.scroll_data = rectangle(x, y, w, h), csums
                    options["scroll"] = True
                    scrolllog("updated scroll data, previously set: %s", bool(lsd))
                if lsd and csums:
                    rect, lcsums = lsd
                    if rect.x!=x or rect.y!=y:
                        #TODO: just adjust the scrolling packet for this offset
                        scrolllog("scroll data position mismatch: %s vs %i,%i", rect, x, y)
                    elif rect.width!=w or rect.height!=h:
                        scrolllog("scroll data size mismatch: %s vs %ix%i", rect, w, h)
                    else:
                        #same size, try to find scrolling value
                        assert len(csums)==len(lcsums), "mismatch between checksums lists: %i vs %i items!" % (len(csums), len(lcsums))
                        distances = calculate_distances(csums, lcsums, 2, 1000)
                        if len(distances)==0:
                            scrolllog("no scroll distances found")
                        else:
                            best = max(distances.values())
                            scroll = distances.keys()[distances.values().index(best)]
                            end = time.time()
                            best_pct = int(100*best/h)
                            scrolllog("best scroll guess took %ims, matches %i%% of %i lines: %s", (end-start)*1000, best_pct, h, scroll)
                            #if enough scrolling is detected, use scroll encoding for this frame:
                            if best_pct>=SCROLL_MIN_PERCENT:
                                return self.encode_scrolling(image, distances, lcsums, csums, options)
            except Exception:
                scrolllog.error("Error during scrolling detection!", exc_info=True)
                #make sure we start again from scratch next time:
                self.scroll_data = None

        def video_fallback():
            videolog.warn("using non-video fallback encoding")
            return self.video_fallback(image, options)

        vh = self.video_helper
        if vh is None:
            return None         #shortcut when closing down
        if not self.check_pipeline(encoding, w, h, src_format):
            if self.is_cancelled():
                return None
            #just for diagnostics:
            supported_csc_modes = self.full_csc_modes.get(encoding, [])
            encoder_specs = vh.get_encoder_specs(encoding)
            encoder_types = []
            ecsc = []
            for csc in supported_csc_modes:
                if csc not in encoder_specs:
                    continue
                if csc not in ecsc:
                    ecsc.append(csc)
                for especs in encoder_specs.get(csc, []):
                    if especs.codec_type not in encoder_types:
                        encoder_types.append(especs.codec_type)
            videolog.error("Error: failed to setup a video pipeline for %s encoding with source format %s", encoding, src_format)
            videolog.error(" all encoders: %s", ", ".join(list(set([es.codec_type for sublist in encoder_specs.values() for es in sublist]))))
            videolog.error(" supported CSC modes: %s", ", ".join(supported_csc_modes))
            videolog.error(" supported encoders: %s", ", ".join(encoder_types))
            videolog.error(" encoders CSC modes: %s", ", ".join(ecsc))
            if FORCE_CSC:
                log.error(" forced csc mode: %s", FORCE_CSC_MODE)
            return video_fallback()
        ve = self._video_encoder
        if not ve:
            return video_fallback()

        #dw and dh are the edges we don't handle here
        width = w & self.width_mask
        height = h & self.height_mask
        videolog("video_encode%s image size: %sx%s, encoder/csc size: %sx%s", (encoding, image, options), w, h, width, height)

        csce, csc_image, csc, enc_width, enc_height = self.csc_image(image, width, height)

        start = time.time()
        quality = max(0, min(100, self._current_quality))
        speed = max(0, min(100, self._current_speed))
        options.update(self.get_video_encoder_options(ve.get_encoding(), width, height))
        try:
            ret = ve.compress_image(csc_image, quality, speed, options)
        except Exception as e:
            log("%s.compress_image%s", ve, (csc_image, quality, speed, options), exc_info=True)
            if self.is_cancelled():
                return None
            log.error("Error: failed to encode %s video frame:", ve.get_type())
            log.error(" %s", e)
            log.error(" source: %s", csc_image)
            log.error(" options: %s", options)
            log.error(" encoder:")
            for k,v in ve.get_info().items():
                log.error("   %-20s: %s", k, v)
            if csce:
                log.error(" csc %s:", csce.get_type())
                for k,v in csce.get_info().items():
                    log.error("   %-20s: %s", k, v)
            return None
        finally:
            if image!=csc_image:
                self.free_image_wrapper(csc_image)
            del csc_image
        if ret is None:
            if not self.is_cancelled():
                videolog.error("Error: %s video compression failed", encoding)
            return None
        data, client_options = ret
        end = time.time()

        #populate client options:
        frame = client_options.get("frame", 0)
        if frame<self.start_video_frame:
            #tell client not to bother updating the screen,
            #as it must have received a non-video frame already
            client_options["paint"] = False

        #tell the client which colour subsampling we used:
        #(note: see csc_equiv!)
        client_options["csc"] = self.csc_equiv(csc)
        #tell the client about scaling (the size of the encoded picture):
        #(unless the video encoder has already done so):
        scaled_size = None
        if csce and ("scaled_size" not in client_options) and (enc_width!=width or enc_height!=height):
            scaled_size = enc_width, enc_height
            client_options["scaled_size"] = scaled_size

        #deal with delayed b-frames:
        delayed = client_options.get("delayed", 0)
        self.cancel_video_encoder_flush()
        if delayed>0:
            self.schedule_video_encoder_flush(ve, csc, frame, x, y, scaled_size)
            if not data and self.non_video_encodings:
                if frame==0:
                    #first frame has not been sent yet,
                    #so send something as non-video
                    #and skip painting this video frame when it does come out:
                    self.start_video_frame = delayed
                    return self.video_fallback(image, options, order=FAST_ORDER)
                return None
        actual_encoding = ve.get_encoding()
        videolog("video_encode %s encoder: %s %sx%s result is %s bytes (%.1f MPixels/s), client options=%s",
                            ve.get_type(), actual_encoding, enc_width, enc_height, len(data or ""), (enc_width*enc_height/(end-start+0.000001)/1024.0/1024.0), client_options)
        return actual_encoding, Compressed(actual_encoding, data), client_options, width, height, 0, 24

    def cancel_video_encoder_flush(self):
        self.cancel_video_encoder_flush_timer()
        self.b_frame_flush_data = None

    def cancel_video_encoder_flush_timer(self):
        bft = self.b_frame_flush_timer
        if bft:
            self.b_frame_flush_timer = None
            self.source_remove(bft)

    def schedule_video_encoder_flush(self, ve, csc, frame, x , y, scaled_size):
        flush_delay = max(150, min(500, int(self.batch_config.delay*10)))
        self.b_frame_flush_data = (ve, csc, frame, x, y, scaled_size)
        self.b_frame_flush_timer = self.timeout_add(flush_delay, self.flush_video_encoder)

    def flush_video_encoder_now(self):
        #this can be called before the timer is due
        self.cancel_video_encoder_flush_timer()
        self.flush_video_encoder()

    def flush_video_encoder(self):
        #this runs in the UI thread as scheduled by schedule_video_encoder_flush,
        #but we want to run from the encode thread to access the encoder:
        self.b_frame_flush_timer = None
        if self.b_frame_flush_data:
            self.call_in_encode_thread(True, self.do_flush_video_encoder)

    def do_flush_video_encoder(self):
        flush_data = self.b_frame_flush_data
        videolog("do_flush_video_encoder: %s", flush_data)
        if not flush_data:
            return
        ve, csc, frame, x, y, scaled_size = flush_data
        if self._video_encoder!=ve or ve.is_closed():
            return
        if frame==0 and ve.get_type()=="x264":
            #x264 has problems if we try to re-use a context after flushing the first IDR frame
            self._video_encoder = None
            ve.clean()
            if self.non_video_encodings:
                self.idle_add(self.refresh, {"novideo" : True})
                videolog("flushed frame 0, novideo refresh requested")
            return
        w = ve.get_width()
        h = ve.get_height()
        encoding = ve.get_encoding()
        v = ve.flush(frame)
        if ve.is_closed():
            videolog("do_flush_video_encoder encoder %s is closed following the flush", ve)
            self.cleanup_codecs()
        if not v:
            videolog("do_flush_video_encoder: %s flush=%s", flush_data, v)
            return
        data, client_options = v
        if not data:
            videolog("do_flush_video_encoder: %s no data: %s", flush_data, v)
            return
        client_options["csc"] = self.csc_equiv(csc)
        if frame<self.start_video_frame:
            client_options["paint"] = False
        if scaled_size:
            client_options["scaled_size"] = scaled_size
        client_options["flush-encoder"] = True
        videolog("do_flush_video_encoder %s : (%s %s bytes, %s)", flush_data, len(data or ()), type(data), client_options)
        packet = self.make_draw_packet(x, y, w, h, encoding, Compressed(encoding, data), 0, client_options, {})
        self.queue_damage_packet(packet)
        #check for more delayed frames since we want to support multiple b-frames:
        if not self.b_frame_flush_timer and client_options.get("delayed", 0)>0:
            self.schedule_video_encoder_flush(ve, csc, frame, x, y, scaled_size)


    def csc_image(self, image, width, height):
        """
            Takes a source image and converts it
            using the current csc_encoder.
            If there are no csc_encoders (because the video
            encoder can process the source format directly)
            then the image is returned unchanged.

            Runs in the 'encode' thread.
        """
        csce = self._csc_encoder
        if csce is None:
            #no csc step!
            return None, image, image.get_pixel_format(), width, height

        start = time.time()
        csc_image = csce.convert_image(image)
        end = time.time()
        csclog("csc_image(%s, %s, %s) converted to %s in %.1fms (%.1f MPixels/s)",
                        image, width, height,
                        csc_image, (1000.0*end-1000.0*start), (width*height/(end-start+0.000001)/1024.0/1024.0))
        if not csc_image:
            raise Exception("csc_image: conversion of %s to %s failed" % (image, csce.get_dst_format()))
        assert csce.get_dst_format()==csc_image.get_pixel_format()
        return csce, csc_image, csce.get_dst_format(), csce.get_dst_width(), csce.get_dst_height()
