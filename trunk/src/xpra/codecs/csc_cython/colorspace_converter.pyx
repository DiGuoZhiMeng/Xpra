# This file is part of Xpra.
# Copyright (C) 2013 Arthur Huillet
# Copyright (C) 2012-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import time
import struct

from xpra.log import Logger
log = Logger("csc", "cython")

from xpra.codecs.codec_constants import codec_spec
from xpra.codecs.image_wrapper import ImageWrapper

cdef extern from "../memalign/memalign.h":
    int pad(int size) nogil
    void *xmemalign(size_t size) nogil

cdef extern from "stdlib.h":
    void free(void *ptr)

cdef extern from "Python.h":
    ctypedef int Py_ssize_t
    ctypedef object PyObject
    object PyBuffer_FromMemory(void *ptr, Py_ssize_t size)
    object PyBuffer_FromReadWriteMemory(void *ptr, Py_ssize_t size)
    int PyObject_AsReadBuffer(object obj, void ** buffer, Py_ssize_t * buffer_len) except -1


from libc.stdint cimport uint8_t

cdef int roundup(int n, int m):
    return (n + m - 1) & ~(m - 1)

#precalculate indexes in native endianness:
tmp = str(struct.pack("=BBBB", 0, 1, 2, 3))
cdef uint8_t BGRA_B = tmp.find('\0')
cdef uint8_t BGRA_G = tmp.find('\1')
cdef uint8_t BGRA_R = tmp.find('\2')
cdef uint8_t BGRA_A = tmp.find('\3')
cdef uint8_t BGRX_R = BGRA_R
cdef uint8_t BGRX_G = BGRA_G
cdef uint8_t BGRX_B = BGRA_B
cdef uint8_t BGRX_X = BGRA_A

tmp = str(struct.pack("=BBBB", 0, 1, 2, 3))
cdef uint8_t RGBX_R = tmp.find('\0')
cdef uint8_t RGBX_G = tmp.find('\1')
cdef uint8_t RGBX_B = tmp.find('\2')
cdef uint8_t RGBX_X = tmp.find('\3')


COLORSPACES = {"BGRX" : ["YUV420P"], "YUV420P" : ["RGBX", "BGRX"]}

def init_module():
    #nothing to do!
    log("csc_cython.init_module()")

def get_type():
    return "cython"

def get_version():
    return (0, 2)

def get_info():
    return {"version"   : get_version()}

def get_input_colorspaces():
    return COLORSPACES.keys()

def get_output_colorspaces(input_colorspace):
    return COLORSPACES[input_colorspace]

def get_spec(in_colorspace, out_colorspace):
    assert in_colorspace in COLORSPACES, "invalid input colorspace: %s (must be one of %s)" % (in_colorspace, get_input_colorspaces())
    assert out_colorspace in COLORSPACES.get(in_colorspace), "invalid output colorspace: %s (must be one of %s)" % (out_colorspace, get_output_colorspaces(in_colorspace))
    #low score as this should be used as fallback only:
    return codec_spec(ColorspaceConverter, codec_type=get_type(), quality=50, speed=10, setup_cost=10, min_w=2, min_h=2, can_scale=False)


class CythonImageWrapper(ImageWrapper):

    def free(self):                             #@DuplicatedSignature
        log("CythonImageWrapper.free() cython_buffer=%#x", <unsigned long> self.cython_buffer)
        ImageWrapper.free(self)
        if self.cython_buffer>0:
            free(<void *> (<unsigned long> self.cython_buffer))
            self.cython_buffer = 0


DEF STRIDE_ROUNDUP = 16

#Pre-calculate some coefficients and defined them as constants
#We use integer calculations so everything is multipled by 2**16
#To get the result as a byte, we just bitshift:
DEF shift = 16

#RGB to YUV
#Y[o] = clamp(0.257 * R + 0.504 * G + 0.098 * B + 16)
# Y = 0.257 * R + 0.504 * G + 0.098 * B + 16
DEF YR = 16843      # 0.257 * 2**16
DEF YG = 33030      # 0.504 * 2**16
DEF YB = 6423       # 0.098 * 2**16
DEF Yc = 16
DEF YC = 1048576    # 16    * 2**16
#U[y*self.dst_strides[1] + x] = clamp(-0.148 * Rsum/sum - 0.291 * Gsum/sum + 0.439 * Bsum/sum + 128)
# U = -0.148 * R - 0.291 * G + 0.439 * B + 128
DEF UR = -9699      #-0.148 * 2**16
DEF UG = -19071     #-0.291 * 2**16
DEF UB = 28770      # 0.439 * 2**16
DEF Uc = 128
DEF UC = 8388608    # 128   * 2**16
#V[y*self.dst_strides[2] + x] = clamp(0.439 * Rsum/sum - 0.368 * Gsum/sum - 0.071 * Bsum/sum + 128)
# V = 0.439 * R - 0.368 * G - 0.071 * B + 128
DEF VR = 28770      # 0.439  * 2**16
DEF VG = -24117     #-0.368  * 2**16
DEF VB = -4653      #-0.071  * 2**16
DEF Vc = 128
DEF VC = 8388608    # 128    * 2**16

DEF max_clamp = 16777216    #2**(16+8)

#YUV to RGB:
#Y, Cb and Cr are adjusted as:
#Y'  = Y - 16
#Cb' = Cb - 128
#Cr' = Cr - 128
# (see YC, UC and VC above)
#RGB:
#R = Y' + 1.5958  * Cb' 
#G = Y' - 0.81290 * Cb' - 0.39173 * Cr' 
#B = Y'                 + 2.017   * Cr'
DEF RY = 65536      #1        * 2**16
DEF RU = 104582     #1.5958   * 2**16
DEF RV = 0

DEF GY = 65536      #1        * 2**16
DEF GU = -53274     #-0.81290 * 2**16
DEF GV = -25672     #-0.39173 * 2**16

DEF BY = 65536      #1        * 2**16
DEF BU = 0
DEF BV = 132186     #2.017    * 2**16


cdef unsigned char clamp(long v) nogil:
    if v<=0:
        return 0
    elif v>=max_clamp:
        return 255
    else:
        return <unsigned char> (v>>shift)


cdef class ColorspaceConverter:
    cdef int src_width
    cdef int src_height
    cdef object src_format
    cdef int dst_width
    cdef int dst_height
    cdef object dst_format
    cdef int[3] dst_strides
    cdef int[3] dst_sizes
    cdef int[3] offsets

    cdef convert_image_function

    cdef int frames
    cdef double time
    cdef int buffer_size

    cdef object __weakref__

    def init_context(self, int src_width, int src_height, src_format,
                           int dst_width, int dst_height, dst_format, int speed=100):    #@DuplicatedSignature
        cdef int i
        assert src_format in get_input_colorspaces(), "invalid input colorspace: %s (must be one of %s)" % (src_format, get_input_colorspaces())
        assert dst_format in get_output_colorspaces(src_format), "invalid output colorspace: %s (must be one of %s)" % (dst_format, get_output_colorspaces(src_format))
        assert src_width==dst_width
        assert src_height==dst_height
        log("csc_cython.ColorspaceConverter.init_context%s", (src_width, src_height, src_format, dst_width, dst_height, dst_format, speed))
        self.src_width = src_width
        self.src_height = src_height
        self.dst_width = dst_width
        self.dst_height = dst_height
        self.src_format = src_format[:]
        self.dst_format = dst_format[:]

        self.time = 0
        self.frames = 0

        if src_format=="BGRX" and dst_format=="YUV420P":
            self.dst_strides[0] = roundup(self.dst_width,   STRIDE_ROUNDUP)
            self.dst_strides[1] = roundup(self.dst_width/2, STRIDE_ROUNDUP)
            self.dst_strides[2] = roundup(self.dst_width/2, STRIDE_ROUNDUP)
            self.dst_sizes[0] = self.dst_strides[0] * self.dst_height
            self.dst_sizes[1] = self.dst_strides[1] * self.dst_height/2
            self.dst_sizes[2] = self.dst_strides[2] * self.dst_height/2
            #U channel follows Y with 1 line padding, V follows U with another line of padding:
            self.offsets[0] = 0
            self.offsets[1] = self.dst_strides[0] * (self.dst_height+1)
            self.offsets[2] = self.offsets[1] + (self.dst_strides[1] * (self.dst_height/2+1))
            #output buffer ends after V + 1 line of padding:
            self.buffer_size = self.offsets[2] + (self.dst_strides[2] * (self.dst_height/2+1))

            self.convert_image_function = self.BGRX_to_YUV420P
        elif src_format=="YUV420P" and dst_format in ("RGBX", "BGRX"):
            #4 bytes per pixel:
            self.dst_strides[0] = roundup(self.dst_width*4, STRIDE_ROUNDUP)
            self.dst_sizes[0] = self.dst_strides[0] * self.dst_height
            self.offsets[0] = 0
            #clear the rest:
            for i in range(2):
                self.dst_strides[i+1] = 0
                self.dst_sizes[i+1] = 0
                self.offsets[i+1] = 0
            #output buffer ends after 1 line of padding:
            self.buffer_size = self.dst_sizes[0] + roundup(dst_width*4, STRIDE_ROUNDUP)

            if dst_format=="RGBX":
                self.convert_image_function = self.YUV420P_to_RGBX
            else:
                assert dst_format=="BGRX"
                self.convert_image_function = self.YUV420P_to_BGRX
        else:
            raise Exception("BUG: src_format=%s, dst_format=%s", src_format, dst_format)


    def get_info(self):                     #@DuplicatedSignature
        info = {
                "frames"    : self.frames,
                "src_width" : self.src_width,
                "src_height": self.src_height,
                "dst_width" : self.dst_width,
                "dst_height": self.dst_height}
        if self.src_format:
            info["src_format"] = self.src_format
        if self.dst_format:
            info["dst_format"] = self.dst_format
        if self.frames>0 and self.time>0:
            pps = float(self.src_width) * float(self.src_height) * float(self.frames) / self.time
            info["total_time_ms"] = int(self.time*1000.0)
            info["pixels_per_second"] = int(pps)
        return info

    def __str__(self):
        return "csc_cython(%s %sx%s - %s %sx%s)" % (self.src_format, self.src_width, self.src_height,
                                                 self.dst_format, self.dst_width, self.dst_height)

    def __dealloc__(self):                  #@DuplicatedSignature
        self.clean()

    def get_src_width(self):
        return self.src_width

    def get_src_height(self):
        return self.src_height

    def get_src_format(self):
        return self.src_format

    def get_dst_width(self):
        return self.dst_width

    def get_dst_height(self):
        return self.dst_height

    def get_dst_format(self):
        return self.dst_format

    def get_type(self):                     #@DuplicatedSignature
        return  "cython"


    def clean(self):                        #@DuplicatedSignature
        pass


    def convert_image(self, image):
        return self.convert_image_function(image)


    def BGRX_to_YUV420P(self, image):
        cdef Py_ssize_t pic_buf_len = 0
        cdef const unsigned char *input_image
        cdef unsigned char *output_image
        cdef int input_stride
        cdef int x,y,i,o,dx,dy,sum          #@DuplicatedSignature
        cdef int workw, workh
        cdef int Ystride, Ustride, Vstride
        cdef unsigned char R, G, B
        cdef unsigned short Rsum
        cdef unsigned short Gsum
        cdef unsigned short Bsum
        cdef unsigned char *Y
        cdef unsigned char *U
        cdef unsigned char *V

        start = time.time()
        iplanes = image.get_planes()
        assert iplanes==ImageWrapper.PACKED, "invalid input format: %s planes" % iplanes
        assert image.get_width()>=self.src_width, "invalid image width: %s (minimum is %s)" % (image.get_width(), self.src_width)
        assert image.get_height()>=self.src_height, "invalid image height: %s (minimum is %s)" % (image.get_height(), self.src_height)
        input = image.get_pixels()
        input_stride = image.get_rowstride()
        log("convert_image(%s) input=%s, strides=%s" % (image, len(input), input_stride))

        PyObject_AsReadBuffer(input, <const void**> &input_image, &pic_buf_len)
        #allocate output buffer:
        output_image = <unsigned char*> xmemalign(self.buffer_size)
        Y = output_image + self.offsets[0]
        U = output_image + self.offsets[1]
        V = output_image + self.offsets[2]
        #copy to local variables (ensures C code will be optimized correctly)
        Ystride = self.dst_strides[0]
        Ustride = self.dst_strides[1]
        Vstride = self.dst_strides[2]
        #we process 4 pixels at a time:
        workw = roundup(self.dst_width/2, 2)
        workh = roundup(self.dst_height/2, 2)
        #from now on, we can release the gil:
        #log("work: %sx%s from %sx%s, RGB indexes: %s", workw, workh, self.dst_width, self.dst_height, (BGRA_R, BGRA_G, BGRA_B))
        with nogil:
            for y in xrange(workh):
                for x in xrange(workw):
                    R = 0
                    G = 0
                    B = 0
                    Rsum = 0
                    Gsum = 0
                    Bsum = 0
                    sum = 0
                    for i in range(4):
                        dx = i%2
                        dy = i/2
                        if x*2+dx<self.src_width and y*2+dy<self.src_height:
                            o = (y*2+dy)*input_stride + (x*2+dx)*4
                            R = input_image[o + BGRA_R]
                            G = input_image[o + BGRA_G]
                            B = input_image[o + BGRA_B]
                            o = (y*2+dy)*Ystride + (x*2+dx)

                            Y[o] = clamp(YR * R + YG * G + YB * B + YC)
                            sum += 1
                            Rsum += R
                            Gsum += G
                            Bsum += B
                    #write 1U and 1V:
                    if sum>0:
                        Rsum /= sum
                        Gsum /= sum
                        Bsum /= sum
                        U[y*Ustride + x] = clamp(UR * Rsum + UG * Gsum + UB * Bsum + UC)
                        V[y*Vstride + x] = clamp(VR * Rsum + VG * Gsum + VB * Bsum + VC)
        #create python buffer from each plane:
        planes = []
        strides = []
        for i in range(3):
            strides.append(self.dst_strides[i])
            plane = PyBuffer_FromMemory(<void *> (<unsigned long> (output_image + self.offsets[i])), self.dst_sizes[i])
            planes.append(plane)
        elapsed = time.time()-start
        log("%s took %.1fms", self, 1000.0*elapsed)
        self.time += elapsed
        self.frames += 1
        out_image = CythonImageWrapper(0, 0, self.dst_width, self.dst_height, planes, self.dst_format, 24, strides, ImageWrapper._3_PLANES)
        out_image.cython_buffer = <unsigned long> output_image
        return out_image

    def YUV420P_to_RGBX(self, image):
        return self.do_YUV420P_to_RGB(image, RGBX_R, RGBX_G, RGBX_B, RGBX_X)

    def YUV420P_to_BGRX(self, image):
        return self.do_YUV420P_to_RGB(image, BGRX_R, BGRX_G, BGRX_B, BGRX_X)

    cdef do_YUV420P_to_RGB(self, image, uint8_t Rindex, uint8_t Gindex, uint8_t Bindex, uint8_t Xindex):
        cdef Py_ssize_t buf_len = 0
        cdef unsigned char *output_image        #
        cdef int x,y,i,o,dx,dy,sum              #@DuplicatedSignature
        cdef int workw, workh                   #
        cdef int stride
        cdef unsigned char *Ybuf
        cdef unsigned char *Ubuf
        cdef unsigned char *Vbuf
        cdef short Y, U, V
        cdef int Ystride, Ustride, Vstride      #
        cdef object rgb

        start = time.time()
        iplanes = image.get_planes()
        assert iplanes==ImageWrapper._3_PLANES, "invalid input format: %s planes" % iplanes
        assert image.get_width()>=self.src_width, "invalid image width: %s (minimum is %s)" % (image.get_width(), self.src_width)
        assert image.get_height()>=self.src_height, "invalid image height: %s (minimum is %s)" % (image.get_height(), self.src_height)
        planes = image.get_pixels()
        input_strides = image.get_rowstride()
        log("convert_image(%s) strides=%s" % (image, input_strides))

        #copy to local variables:
        stride = self.dst_strides[0]
        Ystride = input_strides[0]
        Ustride = input_strides[1]
        Vstride = input_strides[2]

        PyObject_AsReadBuffer(planes[0], <const void**> &Ybuf, &buf_len)
        assert buf_len>=Ystride*image.get_height(), "buffer for Y plane is too small: %s bytes, expected at least %s" % (buf_len, Ystride*image.get_height())
        PyObject_AsReadBuffer(planes[1], <const void**> &Ubuf, &buf_len)
        assert buf_len>=Ustride*image.get_height()/2, "buffer for U plane is too small: %s bytes, expected at least %s" % (buf_len, Ustride*image.get_height()/2)
        PyObject_AsReadBuffer(planes[2], <const void**> &Vbuf, &buf_len)
        assert buf_len>=Vstride*image.get_height()/2, "buffer for V plane is too small: %s bytes, expected at least %s" % (buf_len, Vstride*image.get_height()/2)

        #allocate output buffer:
        output_image = <unsigned char*> xmemalign(self.buffer_size)

        #we process 4 pixels at a time:
        workw = roundup(self.dst_width/2, 2)
        workh = roundup(self.dst_height/2, 2)
        #from now on, we can release the gil:
        with nogil:
            for y in xrange(workh):
                for x in xrange(workw):
                    #assert x*2<=self.src_width and y*2<=self.src_height
                    #read U and V for the next 4 pixels:
                    U = Ubuf[y*Ustride + x] - Uc
                    V = Vbuf[y*Vstride + x] - Vc
                    #now read up to 4 Y values and write an RGB pixel for each:
                    for i in range(4):
                        dx = i%2
                        dy = i/2
                        if x*2+dx<self.src_width and y*2+dy<self.src_height:
                            Y = Ybuf[(y*2+dy)*Ystride + (x*2+dx)] - Yc
                            o = ((y*2) + dy)*stride + ((x*2) + dx)*4
                            output_image[o + Rindex] = clamp(RY * Y + RU * U + RV * V)
                            output_image[o + Gindex] = clamp(GY * Y + GU * U + GV * V)
                            output_image[o + Bindex] = clamp(BY * Y + BU * U + BV * V)
                            output_image[o + Xindex] = 255

        rgb = PyBuffer_FromReadWriteMemory(<void *> output_image, self.dst_sizes[0])
        elapsed = time.time()-start
        log("%s took %.1fms", self, 1000.0*elapsed)
        self.time += elapsed
        self.frames += 1
        out_image = CythonImageWrapper(0, 0, self.dst_width, self.dst_height, rgb, self.dst_format, 24, stride, ImageWrapper.PACKED)
        out_image.cython_buffer = <unsigned long> output_image
        return out_image
